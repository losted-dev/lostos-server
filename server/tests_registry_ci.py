"""The checks of the plugin registry (tools/registry_ci.py): the cases of a pull request. Run: python tests_registry_ci.py"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import plugin_sign as ps
import registry_ci as ci

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)[:300]))
    OK = OK and bool(cond)


def package(pid="ru.example.demo", version="1.0.0", perms=None, device_perms=None, extra_files=None, author="Demo", name="Demo"):
    man = {"id": pid, "name": name, "version": version, "author": author, "api": "1", "description": "demo", "permissions": perms or [], "entry": "main.py"}
    if device_perms:
        man["device"] = {"binary": "device/plugin", "permissions": device_perms}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(man))
        z.writestr("main.py", "print('hi')\n")
        z.writestr("README.md", "# demo\n")
        if device_perms:
            z.writestr("device/plugin", b"\x7fELF")
        for n, c in (extra_files or {}).items():
            z.writestr(n, c)
    return buf.getvalue()


def registry(root, devs, packages):
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)
    json.dump(devs, open(os.path.join(root, "developers.json"), "w"))
    json.dump([], open(os.path.join(root, "revoked.json"), "w"))
    for fn, data in packages.items():
        open(os.path.join(root, "plugins", fn), "wb").write(data)


def run(folder, base=None):
    rep = os.path.join(tempfile.gettempdir(), "registry-ci-report.md")
    with contextlib.redirect_stdout(io.StringIO()):
        code = ci.validate(folder, base, rep)
    return code, open(rep, encoding="utf-8").read()


def main():
    tmp = tempfile.mkdtemp(prefix="lostos-regci-")
    try:
        # the keys of the team in this test are none: only the developers of the registry count
        ps.TRUST_FILE = os.path.join(tmp, "no-team.json")
        pem, pub = ps.new_keypair()
        other_pem, other_pub = ps.new_keypair()
        priv = ps.load_private(pem)
        other = ps.load_private(other_pem)
        dev = {"id": "demo", "name": "Demo", "key": pub}
        signed = lambda data, key=priv, who="Demo": ps.sign_package(data, key, who)

        def case(name, devs, packages, base_packages=None, base_devs=None, extra=None):
            r, b = os.path.join(tmp, name, "pr"), None
            registry(r, devs, packages)
            if base_packages is not None:
                b = os.path.join(tmp, name, "main")
                registry(b, base_devs if base_devs is not None else devs, base_packages)
            if extra:
                extra(r, b)
            return run(r, b)

        good = signed(package())
        code, txt = case("good", [dev], {"ru.example.demo-1.0.0.lstplugin": good})
        check("a signed plugin of a registered developer passes", code == 0, txt)

        code, txt = case("unsigned", [dev], {"ru.example.demo-1.0.0.lstplugin": package()})
        check("an unsigned package is refused", code == 1 and "нет подписи" in txt, txt)

        code, txt = case("stranger", [dev], {"ru.example.demo-1.0.0.lstplugin": signed(package(), other, "Eve")})
        check("a key that is not in developers.json is refused", code == 1 and "не записан в developers.json" in txt, txt)

        code, txt = case("both", [dev, {"id": "eve", "name": "Eve", "key": other_pub}], {"ru.example.demo-1.0.0.lstplugin": signed(package(), other, "Eve")})
        check("the same package passes when the key is added in the same pull request", code == 0, txt)

        z = zipfile.ZipFile(io.BytesIO(good))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zz:
            for i in z.infolist():
                zz.writestr(i.filename, z.read(i.filename) + (b"#x" if i.filename == "main.py" else b""))
        code, txt = case("tampered", [dev], {"ru.example.demo-1.0.0.lstplugin": buf.getvalue()})
        check("a package changed after signing is refused", code == 1 and "не сходится" in txt, txt)

        code, txt = case("name", [dev], {"ru.example.demo-1.2.0.lstplugin": good})
        check("the name of the file must say what the manifest says", code == 1 and "имя файла говорит" in txt, txt)

        code, txt = case("secret", [dev], {"ru.example.demo-1.0.0.lstplugin": signed(package(extra_files={"keys.txt": "-----BEGIN " + "PRIVATE KEY-----\nabc"}))})
        check("a private key inside a package is refused", code == 1 and "ключ или токен" in txt, txt)

        code, txt = case("slip", [dev], {"ru.example.demo-1.0.0.lstplugin": signed(package(extra_files={"../evil.py": "x"}))})
        check("a path that climbs out of the folder is refused (the same check as the installer)", code == 1 and "недопустимый путь" in txt, txt)

        code, txt = case("perm", [dev], {"ru.example.demo-1.0.0.lstplugin": signed(package(perms=["root.everything"]))})
        check("an unknown permission is refused", code == 1 and "неизвестное разрешение" in txt, txt)

        # an update against what is on main
        code, txt = case("immutable", [dev], {"ru.example.demo-1.0.0.lstplugin": signed(package(extra_files={"a.txt": "other"}))},
                         base_packages={"ru.example.demo-1.0.0.lstplugin": good})
        check("a published version cannot be changed", code == 1 and "уже опубликована" in txt, txt)

        code, txt = case("older", [dev], {"ru.example.demo-1.0.0.lstplugin": good, "ru.example.demo-0.9.0.lstplugin": signed(package(version="0.9.0"))},
                         base_packages={"ru.example.demo-1.0.0.lstplugin": good})
        check("a version below the published one is refused", code == 1 and "должна быть больше" in txt, txt)

        update = signed(package(version="1.1.0", perms=["assistant.intercept"]))
        code, txt = case("update", [dev], {"ru.example.demo-1.0.0.lstplugin": good, "ru.example.demo-1.1.0.lstplugin": update},
                         base_packages={"ru.example.demo-1.0.0.lstplugin": good})
        check("an update passes and says which permission is new (a person looks at it)", code == 0 and "обновление добавило чувствительные права" in txt and "assistant.intercept" in txt, txt)

        code, txt = case("untouched", [dev], {"ru.example.demo-1.0.0.lstplugin": good}, base_packages={"ru.example.demo-1.0.0.lstplugin": good})
        check("packages that the pull request did not touch are not checked again", code == 0 and "Пакеты" not in txt, txt)

        def touch_index(r, b):
            open(os.path.join(r, "index.json"), "w").write("{}")
        code, txt = case("index", [dev], {"ru.example.demo-1.0.0.lstplugin": good}, base_packages={"ru.example.demo-1.0.0.lstplugin": good}, extra=touch_index)
        check("index.json belongs to the release, a pull request may not change it", code == 1 and "index.json" in txt, txt)

        code, txt = case("newdev", [dev, {"id": "eve", "name": "Eve", "key": other_pub}], {"ru.example.demo-1.0.0.lstplugin": good},
                         base_packages={"ru.example.demo-1.0.0.lstplugin": good}, base_devs=[dev])
        check("a new developer is flagged for a person", code == 0 and "новый разработчик" in txt and "eve" in txt, txt)

        code, txt = case("baddev", [{"id": "X", "name": "", "key": "AAAA"}], {})
        check("a broken developers.json is refused", code == 1 and "developers.json" in txt, txt)

        # plugapi: the list of verified developers
        import datetime, functools, http.server, threading
        list_pem, list_pub = ps.new_keypair()
        json.dump({"team": [{"publisher": "losted.dev", "key": list_pub, "scope": "developers"}]}, open(ps.TRUST_FILE, "w"))
        site = os.path.join(tmp, "plugapi")
        os.makedirs(site)

        def publish(devs, revoked=()):
            now = datetime.datetime.now(datetime.timezone.utc)
            raw = json.dumps({"v": 1, "serial": 1, "updated": "x", "expires": (now + datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "developers": devs, "revoked": list(revoked)}).encode()
            open(os.path.join(site, "developers.json"), "wb").write(raw)
            open(os.path.join(site, "developers.json.sig"), "w").write(ps.sign_developers(raw, ps.load_private(list_pem)))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=site))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["LOSTOS_PLUGAPI"] = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            publish([])
            code, txt = case("api-unlisted", [dev], {"ru.example.demo-1.0.0.lstplugin": good})
            check("a developer that plugapi does not know yet is flagged for a person (the pull request may add him, the team confirms before the merge)", code == 0 and "не подтверждён на plugapi" in txt, txt)
            publish([dev])
            code, txt = case("api-listed", [dev], {"ru.example.demo-1.0.0.lstplugin": good})
            check("a verified developer passes without a remark", code == 0 and "plugapi" not in txt, txt)
            publish([dev], [ps.fingerprint(pub)])
            code, txt = case("api-revoked", [dev], {"ru.example.demo-1.0.0.lstplugin": good})
            check("a key that plugapi revoked is refused", code == 1 and "отозван" in txt, txt)
        finally:
            os.environ.pop("LOSTOS_PLUGAPI", None)
            srv.shutdown()

        # the release: the index is made and signed, and the server accepts it
        reg = os.path.join(tmp, "release")
        registry(reg, [dev], {"ru.example.demo-1.0.0.lstplugin": good})
        team_pem, team_pub = ps.new_keypair()
        open(os.path.join(tmp, "team.pem"), "w").write(team_pem)
        rc = ci.release(reg, os.path.join(tmp, "team.pem"))
        idx = json.load(open(os.path.join(reg, "index.json")))
        raw = open(os.path.join(reg, "index.json"), "rb").read()
        sig = open(os.path.join(reg, "index.json.sig")).read().strip()
        check("the release builds and signs the index", rc == 0 and idx["plugins"] and ps.verify_index(raw, sig, [team_pub]), (rc, idx))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if OK else "SOME FAILED")
    sys.exit(0 if OK else 1)


main()
