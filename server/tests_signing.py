"""Signatures of plugin packages and the registry of plugins (plugin_sign.py, registry.py). No network except a fake repository on the loopback.
Run: python -I server/tests_signing.py"""
import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import plugin_sign as ps
import registry as regmod
import plugins
from aiohttp import web

OK = True
TMP = tempfile.mkdtemp(prefix="lostos-sign-")


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


def package(files: dict, order=None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k in (order or list(files)):
            z.writestr(k, files[k])
    return out.getvalue()


team_pem, team_pub = ps.new_keypair()
dev_pem, dev_pub = ps.new_keypair()
stranger_pem, stranger_pub = ps.new_keypair()
trust_file = os.path.join(TMP, "trusted_keys.json")
json.dump({"team": [{"publisher": "losted.dev", "key": team_pub}]}, open(trust_file, "w"))
ps.TRUST_FILE = trust_file
files = {"manifest.json": json.dumps({"id": "ru.demo.hello", "name": "Hello", "version": "1.2.0", "about": "demo"}), "main.py": "print('hi')", "data/a.txt": "a"}
raw = package(files)

check("a package without a signature says so", ps.check_package(raw)["state"] == "none")
signed = ps.sign_package(raw, ps.load_private(team_pem), "losted.dev")
c = ps.check_package(signed)
check("a signed package is valid", c["state"] == "valid" and c["publisher"] == "losted.dev" and c["fingerprint"] == ps.fingerprint(team_pub), c)
check("the key of the team is trusted", ps.trust_state(c, ps.trusted_keys())["state"] == "trusted")
check("the digest does not depend on how the zip is packed", ps.check_package(ps.sign_package(package(files, list(reversed(list(files)))), ps.load_private(team_pem), "losted.dev"))["state"] == "valid"
      and ps.digest_of({k: v.encode() for k, v in files.items()}) == ps.digest_of({k: v.encode() for k, v in reversed(list(files.items()))}))
z = zipfile.ZipFile(io.BytesIO(signed))
z2 = {n: z.read(n) for n in z.namelist()}
check("a package changed after signing is bad", ps.check_package(package({**z2, "main.py": b"print('evil')"}))["state"] == "bad")
sig2 = dict(json.loads(z2[ps.SIG_FILE]), key=stranger_pub)
check("a signature with another key is bad", ps.check_package(package({**z2, ps.SIG_FILE: json.dumps(sig2)}))["state"] == "bad")
check("an added file is noticed", ps.check_package(package({**z2, "extra.py": b"x"}))["state"] == "bad")
check("garbage in the signature file is bad, not a crash", ps.check_package(package({**z2, ps.SIG_FILE: b"{not json"}))["state"] == "bad")
by_stranger = ps.sign_package(raw, ps.load_private(stranger_pem), "unknown dev")
st = ps.trust_state(ps.check_package(by_stranger), ps.trusted_keys())
check("a valid signature of a stranger is known, not trusted", st["state"] == "known" and st["publisher"] == "unknown dev", st)
dev_keys = ps.trusted_keys([{"id": "demo", "name": "Demo Dev", "key": dev_pub}])
by_dev = ps.sign_package(raw, ps.load_private(dev_pem), "demo")
st = ps.trust_state(ps.check_package(by_dev), dev_keys)
check("a developer of the registry is trusted", st["state"] == "trusted" and not st["team"], st)
rv = [ps.fingerprint(dev_pub)]
check("a revoked key is revoked", ps.trust_state(ps.check_package(by_dev), ps.trusted_keys([{"id": "demo", "name": "Demo Dev", "key": dev_pub}], rv), rv)["state"] == "revoked")
resigned = ps.sign_package(signed, ps.load_private(dev_pem), "demo")
check("signing again replaces the old signature", ps.check_package(resigned)["fingerprint"] == ps.fingerprint(dev_pub))
check("a package in one top folder is read the same", ps.check_package(ps.sign_package(package({"pkg/" + k: v for k, v in files.items()}), ps.load_private(team_pem), "losted.dev"))["state"] == "valid")

idx = json.dumps({"v": 1, "plugins": []}).encode()
isig = ps.sign_index(idx, ps.load_private(team_pem))
check("an index signed by the team is accepted", ps.verify_index(idx, isig, [team_pub]))
check("a changed index is refused", not ps.verify_index(idx + b" ", isig, [team_pub]))
check("an index signed by a stranger is refused", not ps.verify_index(idx, ps.sign_index(idx, ps.load_private(stranger_pem)), [team_pub]))
check("garbage instead of a signature is refused", not ps.verify_index(idx, "!!!", [team_pub]))


async def _raises(coro):
    try:
        await coro
    except (ValueError, RuntimeError):
        return True
    return False


async def main():
    pkg = ps.sign_package(raw, ps.load_private(dev_pem), "demo")
    entry = {"id": "ru.demo.hello", "name": "Hello", "version": "1.2.0", "about": "demo", "publisher": "Demo Dev", "file": "plugins/hello-1.2.0.lstplugin", "sha256": hashlib.sha256(pkg).hexdigest(), "size": len(pkg)}
    index = json.dumps({"v": 1, "updated": "2026-10-27T00:00:00Z", "developers": [{"id": "demo", "name": "Demo Dev", "key": dev_pub}], "revoked": [], "plugins": [entry]}).encode()
    served = {"/index.json": index, "/index.json.sig": ps.sign_index(index, ps.load_private(team_pem)).encode(), "/plugins/hello-1.2.0.lstplugin": pkg}

    async def get(req):
        body = served.get(req.path)
        return web.Response(body=body) if body is not None else web.Response(status=404)
    app = web.Application()
    app.router.add_get("/{tail:.*}", get)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    reg = regmod.Registry(TMP, lambda *a: None, url)
    check("the registry reads a signed index", await reg.refresh(force=True) and reg.error == "" and len(reg.index["plugins"]) == 1, reg.error)
    v = reg.view({"ru.demo.hello": "1.0.0"})
    check("it shows an update for an older installed version", v["plugins"][0]["update"] is True and v["plugins"][0]["installed"] == "1.0.0")
    check("and none for the same version", reg.view({"ru.demo.hello": "1.2.0"})["plugins"][0]["update"] is False and reg.view({})["plugins"][0]["installed"] is None)
    data, e = await reg.package("ru.demo.hello")
    check("the package comes with a good checksum", data == pkg)
    mgr = types.SimpleNamespace(registry=reg)
    check("a developer of the signed index is trusted by the manager", plugins.Manager.trust_of(mgr, data)["state"] == "trusted")
    check("an unknown plugin is refused", await _raises(reg.package("nope")))
    served["/plugins/hello-1.2.0.lstplugin"] = pkg + b"x"
    check("a file that differs from the checksum is refused", await _raises(reg.package("ru.demo.hello")))
    served["/plugins/hello-1.2.0.lstplugin"] = pkg
    reg.index["plugins"][0]["file"] = "../../etc/passwd"
    check("a path that climbs out of the repository is refused", await _raises(reg.package("ru.demo.hello")))
    served["/index.json"] = index.replace(b"Hello", b"Evil!")
    reg2 = regmod.Registry(tempfile.mkdtemp(prefix="lostos-sign2-"), lambda *a: None, url)
    check("an index with a wrong signature is not accepted", not await reg2.refresh(force=True) and "подпись" in reg2.error and not reg2.index, reg2.error)
    served["/index.json"] = index
    reg3 = regmod.Registry(TMP, lambda *a: None, "http://127.0.0.1:9")
    check("a repository that does not answer leaves the saved list", await reg3.refresh(force=True) and len(reg3.index["plugins"]) == 1 and reg3.error)
    await runner.cleanup()




async def manager_test():
    """The manager itself: a changed package is refused, a signed one is installed (off) and remembers who signed it, an unsigned one still installs but says so."""
    class Stub:
        PANEL = {"cli": None}
        state_services = {}
        def log(self, *a): pass
        def notify(self, *a, **k): pass
    base = tempfile.mkdtemp(prefix="lostos-mgr-")
    plugins.STATE_FILE = os.path.join(base, "state.json")
    plugins.USER_ROOT = os.path.join(base, "user")
    os.makedirs(plugins.USER_ROOT, exist_ok=True)
    mgr = plugins.Manager(Stub())
    mgr.scan()
    body = {"manifest.json": json.dumps({"id": "ru.demo.hello", "name": "Hello", "version": "1.0.0", "about": "demo", "author": "demo", "permissions": []}), "main.py": "print(1)"}
    good = ps.sign_package(package(body), ps.load_private(team_pem), "losted.dev")
    zz = zipfile.ZipFile(io.BytesIO(good))
    bad = package({**{n: zz.read(n) for n in zz.namelist()}, "main.py": b"print(2)"})
    try:
        await mgr.install(bad, "bad.lstplugin")
        check("a changed package is refused by the installer", False)
    except ValueError as e:
        check("a changed package is refused by the installer", "подпись" in str(e), e)
    man = await mgr.install(good, "good.lstplugin")
    sig = mgr.state[man["id"]]["signature"]
    check("a signed package is installed and the signer is remembered", sig["state"] == "trusted" and sig["publisher"] == "losted.dev", sig)
    check("it stays off until the owner allows it", mgr.state[man["id"]]["enabled"] is False)
    check("the source line says who signed it", "Подписан: losted.dev" in mgr.source_of(mgr.items[man["id"]])["detail"], mgr.source_of(mgr.items[man["id"]]))
    man2 = await mgr.install(package(body), "plain.lstplugin")
    check("an unsigned package still installs and says that it is unsigned", mgr.state[man2["id"]]["signature"]["state"] == "none" and "Без подписи" in mgr.source_of(mgr.items[man2["id"]])["detail"])
    org = await mgr.install(good, "good.lstplugin", origin={"type": "repository", "url": "x", "version": "1.0.0"})
    check("an installation from the repository is labelled so", mgr.source_of(mgr.items[org["id"]])["type"] == "repository")



def folder_test():
    import shutil, tempfile
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins", "system")
    saved, ps.TRUST_FILE = ps.TRUST_FILE, os.path.join(os.path.dirname(os.path.abspath(__file__)), "trusted_keys.json")   # the real keys of the team
    keys = ps.trusted_keys()
    ps.TRUST_FILE = saved
    for pid in sorted(os.listdir(root)):
        r = ps.check_folder(os.path.join(root, pid))
        check("built-in " + pid + " is signed by the team", ps.trust_state(r, keys)["state"] == "trusted" and r["publisher"] == "losted.dev", r)
        man = json.load(open(os.path.join(root, pid, "manifest.json"), encoding="utf-8"))
        check(pid + " names losted.dev as the author", man.get("author") == "losted.dev", man.get("author"))
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "x")
    shutil.copytree(os.path.join(root, sorted(os.listdir(root))[0]), d)
    with open(os.path.join(d, "README.md"), "a", encoding="utf-8") as f:
        f.write("changed")
    check("a changed built-in plugin no longer verifies", ps.check_folder(d)["state"] == "bad")


folder_test()


def scope_test():
    import tempfile
    robot_pem, robot_pub = ps.new_keypair()
    tf = os.path.join(tempfile.mkdtemp(), "trusted.json")
    json.dump({"team": [{"publisher": "losted.dev", "key": robot_pub, "scope": "index"}]}, open(tf, "w"))
    saved, ps.TRUST_FILE = ps.TRUST_FILE, tf
    try:
        raw = b'{"v": 1}'
        sig = ps.sign_index(raw, ps.load_private(robot_pem))
        check("the key of the release robot signs the index of the registry", ps.verify_index(raw, sig, ps.team_public_keys()))
        check("... but it is not a publisher of packages", ps.fingerprint(robot_pub) not in ps.trusted_keys())
    finally:
        ps.TRUST_FILE = saved


scope_test()


async def plugapi_test():
    """The list of verified developers on plugapi: what the registry takes from it and what it refuses."""
    import datetime
    robot_pem, robot_pub = ps.new_keypair()
    stranger_pem, _ = ps.new_keypair()
    tf = os.path.join(tempfile.mkdtemp(), "trusted.json")
    json.dump({"team": [{"publisher": "losted.dev", "key": robot_pub, "scope": "developers"}]}, open(tf, "w"))
    saved, ps.TRUST_FILE = ps.TRUST_FILE, tf
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        in_index = {"id": "idx", "name": "From the index", "key": ps.new_keypair()[1]}
        on_plugapi = {"id": "api", "name": "From plugapi", "key": ps.new_keypair()[1]}
        index = json.dumps({"v": 1, "updated": "x", "developers": [in_index], "revoked": ["1111111111111111"], "plugins": []}).encode()
        team_pem, team_pub = ps.new_keypair()
        # the index is signed by a team key of the usual kind; for this test it goes to the file of keys as well
        json.dump({"team": [{"publisher": "losted.dev", "key": robot_pub, "scope": "developers"}, {"publisher": "losted.dev", "key": team_pub}]}, open(tf, "w"))

        def doc(serial=1, days=30, devs=None, revoked=None):
            return json.dumps({"v": 1, "serial": serial, "updated": iso(now), "expires": iso(now + datetime.timedelta(days=days)),
                               "developers": devs if devs is not None else [on_plugapi], "revoked": revoked or []}).encode()
        served = {}

        def put(raw, key=robot_pem):
            served["/developers.json"] = raw
            served["/developers.json.sig"] = ps.sign_developers(raw, ps.load_private(key)).encode()
        put(doc())
        served["/index.json"] = index
        served["/index.json.sig"] = ps.sign_index(index, ps.load_private(team_pem)).encode()

        async def get(req):
            body = served.get(req.path)
            return web.Response(body=body) if body is not None else web.Response(status=404)
        app = web.Application()
        app.router.add_get("/{tail:.*}", get)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        os.environ["LOSTOS_PLUGAPI"] = url
        folder = tempfile.mkdtemp(prefix="lostos-plugapi-")
        reg = regmod.Registry(folder, lambda *a: None, url)
        check("the registry takes the developers from plugapi", await reg.refresh(force=True) and [d["id"] for d in reg.developers()] == ["api"] and reg.developers_source() == "plugapi", reg.devs_error)
        check("and not those of the index while plugapi is good", "idx" not in [d["id"] for d in reg.developers()])
        # a list signed by somebody else
        put(doc(serial=2, devs=[in_index]), stranger_pem)
        check("a list signed by a stranger is refused, the last good one stays", not await reg.refresh_developers() and [d["id"] for d in reg.developers()] == ["api"] and "подпись" in reg.devs_error, reg.devs_error)
        # the key of the index (not of the developers) must not sign the list either
        served["/developers.json.sig"] = ps.sign_index(served["/developers.json"], ps.load_private(team_pem)).encode()
        check("a signature of another kind of file (the index) is not taken for the list", not await reg.refresh_developers())
        # out of date
        put(doc(serial=3, days=-1))
        check("a list that is out of date is refused", not await reg.refresh_developers() and "просрочен" in reg.devs_error, reg.devs_error)
        # an older list than the one seen
        put(doc(serial=5))
        await reg.refresh_developers()
        put(doc(serial=4, devs=[in_index]))
        check("an older list (smaller serial) is refused: no rollback to a list with a developer that was removed", not await reg.refresh_developers() and "старее" in reg.devs_error and [d["id"] for d in reg.developers()] == ["api"], reg.devs_error)
        # a revocation is only added
        put(doc(serial=6, revoked=["2222222222222222"]))
        await reg.refresh_developers()
        check("revoked keys of the index and of plugapi are added together", set(reg.revoked()) == {"1111111111111111", "2222222222222222"}, reg.revoked())
        # the manager trusts a package of a developer of the list
        pkg_pem, pkg_pub = ps.new_keypair()
        put(doc(serial=7, devs=[{"id": "api", "name": "From plugapi", "key": pkg_pub}]))
        await reg.refresh_developers()
        # plugapi gone: the saved list is used
        reg2 = regmod.Registry(folder, lambda *a: None, "http://127.0.0.1:9")
        reg2.api = "http://127.0.0.1:9"
        check("a restart keeps the last good list", [d["key"] for d in reg2.developers()] == [pkg_pub] and reg2.developers_source() == "plugapi")
        check("plugapi that does not answer changes nothing", not await reg2.refresh_developers() and reg2.developers_source() == "plugapi")
        # never reached: the index is the fallback
        reg3 = regmod.Registry(tempfile.mkdtemp(prefix="lostos-plugapi3-"), lambda *a: None, url)
        reg3.index = json.loads(index)
        reg3.api = "off"
        check("without plugapi the developers of the signed index count", [d["id"] for d in reg3.developers()] == ["idx"] and reg3.developers_source() == "index")
        await runner.cleanup()
    finally:
        ps.TRUST_FILE = saved
        os.environ.pop("LOSTOS_PLUGAPI", None)



async def revoke_test():
    """A key revoked on plugapi switches its installed plugins off on the next look; a developer taken off the list only changes the mark."""
    class Stub:
        PANEL = {"cli": None}
        state_services = {}
        def log(self, *a): pass
        def notify(self, *a, **k): pass
    base = tempfile.mkdtemp(prefix="lostos-revoke-")
    plugins.STATE_FILE = os.path.join(base, "state.json")
    plugins.USER_ROOT = os.path.join(base, "user")
    os.makedirs(plugins.USER_ROOT, exist_ok=True)
    mgr = plugins.Manager(Stub())
    mgr.scan()
    dpem, dpub = ps.new_keypair()
    devs = [{"id": "dev", "name": "Dev", "key": dpub}]
    reg = regmod.Registry(base, lambda *a: None, "http://127.0.0.1:9")
    reg.api = "off"
    reg.index = {"developers": devs, "revoked": []}
    mgr.registry = reg
    body = {"manifest.json": json.dumps({"id": "ru.demo.rev", "name": "Rev", "version": "1.0.0", "author": "Dev", "permissions": []}), "main.py": "print(1)"}
    pkg = ps.sign_package(package(body), ps.load_private(dpem), "Dev")
    man = await mgr.install(pkg, "rev.lstplugin")
    mgr.state[man["id"]]["enabled"] = True
    check("a plugin of a verified developer is trusted", mgr.state[man["id"]]["signature"]["state"] == "trusted")
    reg.index = {"developers": [], "revoked": []}
    await mgr.recheck_trust()
    check("a developer taken off the list: the mark changes, the plugin stays on", mgr.state[man["id"]]["signature"]["state"] == "known" and mgr.state[man["id"]]["enabled"] is True)
    reg.index = {"developers": devs, "revoked": []}
    await mgr.recheck_trust()
    check("and back on the list: trusted again", mgr.state[man["id"]]["signature"]["state"] == "trusted")
    reg.index = {"developers": devs, "revoked": [ps.fingerprint(dpub)]}
    await mgr.recheck_trust()
    check("a revoked key: the mark says so and the plugin is switched off", mgr.state[man["id"]]["signature"]["state"] == "revoked" and mgr.state[man["id"]]["enabled"] is False, mgr.state[man["id"]])
    check("the page says why", "отозван" in mgr.source_of(mgr.items[man["id"]])["detail"], mgr.source_of(mgr.items[man["id"]]))


async def watch_test():
    """The watch: a conditional request costs nothing when nothing changed, and a change reaches the callback within one look."""
    import datetime
    kpem, kpub = ps.new_keypair()
    tf = os.path.join(tempfile.mkdtemp(), "trusted.json")
    json.dump({"team": [{"publisher": "losted.dev", "key": kpub, "scope": "developers"}]}, open(tf, "w"))
    saved, ps.TRUST_FILE = ps.TRUST_FILE, tf
    now = datetime.datetime.now(datetime.timezone.utc)
    state = {"serial": 1, "etag": '"v1"', "hits": 0, "full": 0}
    def doc():
        return json.dumps({"v": 1, "serial": state["serial"], "updated": "x", "expires": (now + datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"), "developers": [], "revoked": []}).encode()
    async def get(req):
        state["hits"] += 1
        if req.path == "/developers.json":
            if req.headers.get("If-None-Match") == state["etag"]:
                return web.Response(status=304)
            state["full"] += 1
            return web.Response(body=doc(), headers={"ETag": state["etag"]})
        return web.Response(body=ps.sign_developers(doc(), ps.load_private(kpem)).encode())
    app = web.Application()
    app.router.add_get("/{tail:.*}", get)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    reg = regmod.Registry(tempfile.mkdtemp(prefix="lostos-watch-"), lambda *a: None, "http://127.0.0.1:9")
    reg.api = url
    changes = []
    task = asyncio.get_running_loop().create_task(reg.watch_developers(on_change=lambda: changes.append(reg.devs.get("serial")), interval=0.2))
    try:
        await asyncio.sleep(0.7)
        check("the first look fetches the list and tells the callback", changes == [1] and state["full"] == 1, (changes, state))
        full = state["full"]
        await asyncio.sleep(0.7)
        check("when nothing changed the answer is 304 and nothing is read again", state["full"] == full and changes == [1], (changes, state))
        state["serial"], state["etag"] = 2, '"v2"'
        await asyncio.sleep(0.5)
        check("a new list is taken within one look", changes == [1, 2], changes)
    finally:
        task.cancel()
        await runner.cleanup()
        ps.TRUST_FILE = saved



def devlist_test():
    """The list on the server: every change is signed and published at once, the daily refresh keeps it from running out."""
    import datetime
    import devlist
    base = tempfile.mkdtemp(prefix="lostos-devlist-")
    dl = devlist.DevList(base, os.path.join(base, "site"))
    pub = dl.init()
    tf = os.path.join(base, "trusted.json")
    json.dump({"team": [{"publisher": "losted.dev", "key": pub, "scope": "developers"}]}, open(tf, "w"))
    saved, ps.TRUST_FILE = ps.TRUST_FILE, tf
    try:
        check("the key of the list is made once", mode_ok(dl.key) and _raises_sync(dl.init))
        def published():
            raw = open(os.path.join(base, "site", "developers.json"), "rb").read()
            sig = open(os.path.join(base, "site", "developers.json.sig")).read()
            return json.loads(raw), ps.verify_developers(raw, sig)
        doc = dl.build()
        d, ok = published()
        check("the first list is signed and published", ok and d["serial"] == 1 and d["developers"] == [])
        _, k = ps.new_keypair()
        fp = dl.add("alice", "Alice", k)
        d, ok = published()
        check("adding a developer signs the list again at once", ok and d["serial"] == 2 and [x["id"] for x in d["developers"]] == ["alice"] and fp == ps.fingerprint(k))
        check("the same id or key twice is refused", _raises_sync(lambda: dl.add("alice", "Alice", ps.new_keypair()[1])) and _raises_sync(lambda: dl.add("bob", "Bob", k)))
        check("a badly written id is refused", _raises_sync(lambda: dl.add("Bad Id!", "X", ps.new_keypair()[1])))
        dl.revoke(fp, "leaked")
        d, ok = published()
        check("revoking removes the developer and lists the key", ok and d["developers"] == [] and d["revoked"] == [fp])
        check("a revoked key can not be added again", _raises_sync(lambda: dl.add("alice", "Alice", k)))
        dl.add("carol", "Carol", ps.new_keypair()[1])
        dl.remove("carol")
        d, ok = published()
        check("removing a developer does not revoke anything", ok and d["developers"] == [] and d["revoked"] == [fp])
        check("a fresh list is not signed again by the daily refresh", dl.refresh() is False)
        s = dl.state()
        s["expires"] = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dl._save(s)
        before = published()[0]["serial"]
        check("a list that runs low is signed again", dl.refresh() is True and published()[0]["serial"] == before + 1 and dl.days_left() > devlist.REFRESH_BELOW)
        raw = open(os.path.join(base, "site", "developers.json"), "rb").read()
        check("a changed list does not match its signature", not ps.verify_developers(raw + b" ", open(os.path.join(base, "site", "developers.json.sig")).read()))
        # the list of known servers (the installer warns about every other one)
        def servers():
            r = open(os.path.join(base, "site", "servers.json"), "rb").read()
            return json.loads(r), ps.verify_servers(r, open(os.path.join(base, "site", "servers.json.sig")).read()), r
        d, ok, raw_s = servers()
        check("the list of servers is published and signed with the first list", ok and d["servers"] == [] and d["serial"] == dl.state()["serial"])
        h = dl.add_server("HTTPS://OS.Example.org:8443/panel", "Example")
        d, ok, raw_s = servers()
        check("a server is listed by the hash of its host, not by its address", ok and d["servers"] == [{"h": h, "name": "Example"}] and "example" not in raw_s.decode().lower().replace("example\"", ""), d)
        check("the hash is the same for every way of writing the address", devlist.host_hash("os.example.org") == devlist.host_hash("https://os.example.org/x") == devlist.host_hash("OS.EXAMPLE.ORG.:1") == h)
        check("a server twice is refused", _raises_sync(lambda: dl.add_server("os.example.org", "Again")))
        check("a signature of the list of developers is not taken for the list of servers", not ps.verify_servers(raw, open(os.path.join(base, "site", "developers.json.sig")).read()) and not ps.verify_developers(raw_s, open(os.path.join(base, "site", "servers.json.sig")).read()))
        dl.remove_server("Example")
        check("a server leaves the list by its name", servers()[0]["servers"] == [] and servers()[1])
    finally:
        ps.TRUST_FILE = saved


def mode_ok(path):
    return os.name == "nt" or (os.stat(path).st_mode & 0o077) == 0


def _raises_sync(fn):
    try:
        fn()
    except ValueError:
        return True
    return False





async def device_install_test():
    """The install command that goes to the speaker: where it finds the signature files, and what the signature file carries (the table of files the speaker recomputes the digest from)."""
    import hashlib
    sent = []
    class Stub:
        PANEL = {"cli": None}
        state_services = {}
        GLOBALS = {"base_url": "http://srv/t/1/s/abc"}
        def log(self, *a): pass
        def notify(self, *a, **k): pass
    base = tempfile.mkdtemp(prefix="lostos-devinst-")
    plugins.STATE_FILE = os.path.join(base, "state.json")
    plugins.USER_ROOT = os.path.join(base, "user")
    os.makedirs(plugins.USER_ROOT, exist_ok=True)
    mgr = plugins.Manager(Stub())
    mgr.scan()
    async def fake(line):
        sent.append(line)
        return True
    mgr.device_command = fake
    man = {"id": "ru.demo.dev", "name": "Dev", "version": "1.0.0", "author": "Dev", "permissions": [], "device": {"binary": "device/plugin", "permissions": ["events.subscribe", "network.connect"]}}
    pkg = ps.sign_package(package({"manifest.json": json.dumps(man), "main.py": "print(1)", "device/plugin": b"ELF-demo"}), ps.load_private(team_pem), "losted.dev")
    res = await mgr.install(pkg, "dev.lstplugin")
    p = mgr.items[res["id"]]
    mgr.state[p.id]["granted_device"] = ["events.subscribe", "network.connect"]
    await mgr.device_install(p)
    w = sent[-1].split()
    check("the install command names where the speaker finds the files to check the signature itself", w[0] == "install" and w[2] == "http://srv/t/1/s/abc/plugins-dl/ru.demo.dev/plugin" and w[-1] == "http://srv/t/1/s/abc/plugins-dl", sent[-1])
    check("the permissions that were asked go in the fourth word", w[4] == "events.subscribe,network.connect", w)
    mgr.state[p.id]["granted_device"] = []
    p.m["device"]["permissions"] = []
    await mgr.device_install(p)
    check("no permissions: a dash keeps the place of the word (the speaker reads the base after it)", sent[-1].split()[4] == "-" and sent[-1].split()[-1].endswith("/plugins-dl"), sent[-1])
    sigfile = json.loads(open(os.path.join(p.folder, ps.SIG_FILE), encoding="utf-8").read())
    check("the signature file carries the table of the files it covers", set(sigfile["files"]) >= {"manifest.json", "device/plugin"} and sigfile["files"]["device/plugin"] == hashlib.sha256(b"ELF-demo").hexdigest(), sigfile.get("files"))
    # the speaker recomputes the digest from the table: that is what plugins/trust.cpp does
    lines = b"".join(k.encode() + bytes([0]) + v.encode() + bytes([10]) for k, v in sorted(sigfile["files"].items()))
    check("the digest of the signature can be recomputed from the table alone", hashlib.sha256(lines).hexdigest() == sigfile["digest"])


asyncio.run(manager_test())
asyncio.run(main())
asyncio.run(plugapi_test())
asyncio.run(revoke_test())
asyncio.run(watch_test())
asyncio.run(device_install_test())
devlist_test()
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
