"""The multi-person server end to end: the engine service, the gateway and the tenant processes of two people, each with a fake speaker. Checks that nothing of one person
reaches another (stations, keys, settings, files, processes) and that the gateway only lets a person talk to his own process.
Run from the project folder:  python -I server/tests_gateway.py     (needs the torch / silero / speechbrain of this machine for the engine; takes about a minute)"""
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import accounts as acc_mod

DATA = tempfile.mkdtemp(prefix="lostos-gw-")
GW_PORT, ENGINE_PORT = 18765, 18771
BASE = f"http://127.0.0.1:{GW_PORT}"
OK = True
PROCS = []


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


def start(args, env_extra, tag):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **env_extra)
    f = open(os.path.join(DATA, f"{tag}.log"), "wb")
    p = subprocess.Popen([sys.executable, "-u"] + args, env=env, stdout=f, stderr=subprocess.STDOUT, cwd=HERE)
    PROCS.append(p)
    return p


class FakeStation:
    """A speaker as the gateway sees it: signs in, may be confirmed, hands over its keys."""

    def __init__(self, name, ver=1):
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.name, self.ver, self.id = name, ver, ""
        self.vault_key = os.urandom(32)
        self.api_key = base64.b64encode(os.urandom(32)).decode()
        self.ws = None
        self.got = []                                        # what the server sent to this speaker, in order

    async def run(self, http, ready):
        async with http.ws_connect(BASE + "/link/ws") as ws:
            self.ws = ws
            ch = await ws.receive_json()
            sig = self.key.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")
            await ws.send_json({"t": "hello", "ver": self.ver, "pub": base64.b64encode(self.pub).decode(), "sig": base64.b64encode(sig).decode(), "name": self.name})
            w = await ws.receive_json()
            self.id = w["id"]
            if w["state"] == "confirmed":
                await self.keys()
            ready.set()
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    self.got.append(m)
                    if m.get("t") == "state" and m.get("state") == "confirmed":
                        await self.keys()

    async def keys(self):
        await self.ws.send_json({"t": "vaultkey", "key": base64.b64encode(self.vault_key).decode()})
        await self.ws.send_json({"t": "apikey", "key": self.api_key})


def mgr_cfg():
    try:
        return json.load(open(os.path.join(DATA, "tenants.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def main():
    try:
        import devlist
        site_dir = os.path.join(DATA, "plugapi-site")
        dl = devlist.DevList(DATA, site_dir)
        dl.init()
        dl.build()
        open(os.path.join(DATA, "edition"), "w").write("center")        # this test server is the center of the team: the sections of the lists exist
        env_common = {"PLUGAPI_SITE": site_dir, "LOSTOS_ADMIN_HOST": "admin.test", "LOSTOS_DATA": DATA, "LOSTOS_PUBLIC_URL": BASE, "TTS_PORT": str(GW_PORT), "LOSTOS_ENGINE": f"127.0.0.1:{ENGINE_PORT}"}
        start([os.path.join(HERE, "engine_service.py")], {"LOSTOS_ENGINE": f"127.0.0.1:{ENGINE_PORT}"}, "engine")
        # the operator and two people with a household each (the accounts of the gateway; the people come in as the bot or the login would let them)
        people = acc_mod.Accounts(os.path.join(DATA, "accounts.db"))
        people.bind_owner(1, "Оператор", "op")
        cks = {}
        for tg, name in ((100, "Анна"), (200, "Борис")):
            people.request_access(tg, name, f"u{tg}")
            people.decide(tg, True)
        people.add_member(101, "Муж Анны", "m101", tenant=100)
        cks["admin"] = people.new_session(1, "test", "127.0.0.1")
        cks["anna"] = people.new_session(100, "test-a", "127.0.0.1")
        cks["boris"] = people.new_session(200, "test-b", "127.0.0.1")
        cks["muzh"] = people.new_session(101, "test-m", "127.0.0.1")
        check("a new person gets a household of his own", people.tenant_of(100) == 100 and people.tenant_of(200) == 200 and people.tenant_of(101) == 100 and people.tenant_of(1) == 1)
        start([os.path.join(HERE, "gateway.py")], env_common, "gateway")
        http = aiohttp.ClientSession()
        for _ in range(100):
            try:
                async with http.get(BASE + "/api/v1/hello") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.3)

        async def api(path, who, method="GET", body=None, headers=None):
            h = {"Cookie": f"lostos_session={cks[who]}"} if who else {}
            h.update(headers or {})
            async with http.request(method, BASE + path, headers=h, json=body, allow_redirects=False) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except Exception:
                    return r.status, None

        st, d = await api("/api/v1/hello", None)
        check("the gateway says hello", st == 200 and d["multi"] is True)
        import brand
        check("and says its own line (what the installer shows under the name), the same as the panel", d.get("tagline") == brand.TAGLINE and ('cw-tag">' + brand.TAGLINE) in open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read(), d.get("tagline"))
        st, _ = await api("/api/state", None)
        check("without a login nothing is reachable", st == 401)

        # ---- the locked panel: no station in the household yet, a tenant answers the wizard
        st, d = await api("/api/v1/account", "anna")
        check("a person without a speaker has a locked panel", st == 200 and d["locked"] is True and d["me"]["name"] == "Анна")
        st, d = await api("/api/state", "anna")
        check("the rest of the panel answers 423 until his speaker is connected", st == 423)

        st, d = await api("/api/v1/secrets", "anna")
        check("the keys are out of reach until a station is confirmed (423)", st == 423 and d.get("locked") is True, (st, d))
        st, d = await api("/api/v1/secrets", "anna", "POST", {"name": "GEMINI_API_KEY", "value": "EARLY"})
        check("a key cannot be saved while no station is confirmed (423)", st == 423, (st, d))

        # ---- two speakers, each claimed by the person who types its identifier
        a_st, b_st = FakeStation("Колонка Анны"), FakeStation("Колонка Бориса")
        ra, rb = asyncio.Event(), asyncio.Event()
        ta = asyncio.create_task(a_st.run(http, ra))
        tb = asyncio.create_task(b_st.run(http, rb))
        await ra.wait()
        await rb.wait()
        await asyncio.sleep(0.5)
        for who in ("anna", "boris", "muzh", "admin"):
            st, d = await api("/api/v1/stations", who)
            check(f"waiting speakers are not listed to {who}", d["stations"] == [])
        st, d = await api("/api/v1/stations/claim", "anna", "POST", {"id": a_st.id})
        check("Anna connects her speaker with its identifier", d.get("ok") is True)
        st, d = await api("/api/v1/stations/claim", "boris", "POST", {"id": b_st.id})
        check("Boris connects his speaker with its identifier", d.get("ok") is True, d)
        await asyncio.sleep(2.5)
        for who, mine, other in (("anna", a_st.id, b_st.id), ("boris", b_st.id, a_st.id)):
            st, d = await api("/api/v1/stations", who)
            ids = [x["id"] for x in d["stations"]]
            check(f"{who} sees his station and not the other's", mine in ids and other not in ids, ids)
        st, d = await api("/api/v1/stations", "muzh")
        check("the member of Anna's household sees Anna's speaker (the household's), not Boris's", [x["id"] for x in d["stations"]] in ([a_st.id], []) and b_st.id not in [x["id"] for x in d["stations"]])
        st, d = await api("/api/v1/stations", "boris", "GET")
        st, d2 = await api(f"/api/v1/stations/{a_st.id}", "boris", "DELETE")
        check("Boris cannot remove Anna's speaker", st == 404)

        # ---- each has a process of his own with his own keys
        st, d = await api("/api/v1/account", "anna")
        check("the panel of Anna is open now", d["locked"] is False)
        await asyncio.sleep(1)
        st, d = await api("/api/v1/setup", "anna")
        check("Anna's tenant answers the wizard and sees Anna's speaker", st == 200 and d["station"] and d["station"]["id"] == a_st.id, d)
        st, d = await api("/api/v1/setup", "boris")
        check("Boris's tenant sees Boris's speaker", st == 200 and d["station"] and d["station"]["id"] == b_st.id, d)
        st, d = await api("/api/v1/secrets", "anna")
        check("Anna's vault is open (her speaker gave the key)", st == 200 and d["locked"] is False, d)
        st, d = await api("/api/v1/secrets", "anna", "POST", {"name": "GEMINI_API_KEY", "value": "ANNA-SECRET-KEY"})
        check("Anna saves a key", st == 200 and d.get("ok") is True, d)
        st, d = await api("/api/v1/secrets", "boris")
        saved = {k["name"]: k["saved"] for k in d["keys"]}
        check("Boris does not have it", st == 200 and saved.get("GEMINI_API_KEY") is False, d)
        st, d = await api("/api/v1/secrets", "anna")
        check("Anna still does", {k["name"]: k["saved"] for k in d["keys"]}.get("GEMINI_API_KEY") is True)
        files = {t: os.listdir(os.path.join(DATA, "tenants", t)) for t in os.listdir(os.path.join(DATA, "tenants"))}
        check("every person has a folder of his own", "secrets.enc" in files.get("100", []) and "secrets.enc" in files.get("200", []) and os.path.getsize(os.path.join(DATA, "tenants", "100", "secrets.enc")) > os.path.getsize(os.path.join(DATA, "tenants", "200", "secrets.enc")), files)
        with open(os.path.join(DATA, "tenants", "100", "secrets.enc"), "rb") as f:
            check("the file of Anna holds no plain text of her key", b"ANNA-SECRET-KEY" not in f.read())
        with open(os.path.join(DATA, "tenants", "100", "secrets.enc"), "rb") as f:
            raw = f.read()
        check("the file holds no name of a key and no API key of the station", b"GEMINI" not in raw and a_st.api_key.encode() not in raw)
        found = [os.path.join(dp, f) for dp, _, fs in os.walk(DATA) for f in fs if f.endswith((".json", ".db", ".enc"))]
        leaks = [f for f in found if a_st.api_key.encode() in open(f, "rb").read() or b"ANNA-SECRET-KEY" in open(f, "rb").read()]
        check("no file of the data folder holds the API key of a station or a saved key in plain text", not leaks, leaks)
        # Spotify sign-in: the part that does not need Spotify itself
        import urllib.parse as up
        st, d = await api("/api/v1/spotify", "anna")
        check("spotify: not logged in", st == 200 and d["logged_in"] is False and d["redirect"].endswith("/spotify/callback"), (st, d))
        st, d = await api("/api/v1/secrets", "anna", "POST", {"name": "SPOTIFY_CLIENT_ID", "value": "test-client-id"})
        check("spotify: the owner saves a client id", st == 200 and d.get("ok") is True, d)
        st, d = await api("/api/v1/spotify/login", "anna", "POST", {})
        q = up.parse_qs(up.urlparse((d or {}).get("url", "")).query)
        check("spotify: login url has PKCE, a client id and a state", st == 200 and q.get("code_challenge_method") == ["S256"] and q.get("client_id") and "state" in q, (st, d))
        st, _ = await api("/spotify/callback?code=x&state=forged", "anna")
        check("spotify: a forged state is refused", st == 400, st)
        st, _ = await api("/spotify/callback?code=x&state=whatever", None)
        check("spotify: the callback needs a session", st == 401, st)
        st, d = await api("/api/v1/setup", "muzh")
        check("a member of Anna's household works in Anna's process", st == 403 or (st == 200 and d["station"] and d["station"]["id"] == a_st.id), (st, d))

        # ---- settings are separate
        st, d = await api("/api/v1/setup", "anna", "POST", {"style": "short"})
        st, d = await api("/api/v1/setup", "anna")
        st2, d2 = await api("/api/v1/setup", "boris")
        check("the answer style of Anna is not the style of Boris", d["style"] == "short" and d2["style"] == "normal", (d["style"], d2["style"]))

        # ---- a second station in Anna's house: its own process, the same keys, settings and scenarios
        c_st = FakeStation("Вторая колонка Анны")
        rc = asyncio.Event()
        tc = asyncio.create_task(c_st.run(http, rc))
        await rc.wait()
        st, d = await api("/api/v1/stations/claim", "anna", "POST", {"id": c_st.id})
        check("Anna connects a second speaker", d.get("ok") is True, d)
        await asyncio.sleep(4)
        st, d = await api("/api/v1/stations", "anna")
        check("Anna sees two stations", sorted(x["id"] for x in d["stations"]) == sorted([a_st.id, c_st.id]), d)
        st, d = await api("/api/v1/stations", "boris")
        check("Boris still sees only his own", [x["id"] for x in d["stations"]] == [b_st.id])
        st, d = await api("/api/v1/setup", "anna")
        check("without a choice the panel talks to the first station", d["served"] == a_st.id, d.get("served"))
        st, d = await api("/api/v1/setup", "anna", headers={"X-Lostos-Station": c_st.id})
        check("the chosen station answers (its own process)", st == 200 and d["served"] == c_st.id, (st, d.get("served")))
        st, d = await api("/api/v1/setup", "anna", headers={"X-Lostos-Station": b_st.id})
        check("a station of another house cannot be chosen", st == 200 and d["served"] == a_st.id, d.get("served"))
        await asyncio.sleep(3)
        st, d = await api("/api/v1/secrets", "anna", headers={"X-Lostos-Station": c_st.id})
        check("the second station opens the same vault (its own wrap of the key)", st == 200 and d["locked"] is False and {k["name"]: k["saved"] for k in d["keys"]}.get("GEMINI_API_KEY") is True, d)
        await api("/api/v1/setup", "anna", "POST", {"style": "detailed"}, headers={"X-Lostos-Station": c_st.id})
        await asyncio.sleep(5)
        st, d = await api("/api/v1/setup", "anna")
        check("a setting made at one station is the house's: the other sees it", d["style"] == "detailed", d["style"])
        await api("/api/v1/setup", "anna", "POST", {"style": "short"})
        # ---- the wake word is decided between the stations of one house, through the server (they may share no network)
        await asyncio.sleep(1)
        check("a station learns that the house has another one on line", any(m.get("t") == "peers" and m.get("n") == 1 for m in c_st.got), c_st.got[-3:])
        await a_st.ws.send_json({"t": "wake", "station": "mini-a", "word": "alisa", "busy": 0, "level": "-31.5"})
        await asyncio.sleep(1)
        got = [m for m in c_st.got if m.get("t") == "wake"]
        check("a claim of Anna's first station reaches her second one", got and got[-1]["station"] == "mini-a" and got[-1]["level"] == "-31.5" and got[-1]["word"] == "alisa", got)
        check("but never the station of another house", not [m for m in b_st.got if m.get("t") == "wake"], b_st.got[-3:])
        check("and not the sender itself", not [m for m in a_st.got if m.get("t") == "wake"])
        tc.cancel()

        # ---- the display of the speaker is pushed to the panel (server-sent events through the gateway)
        async with http.get(BASE + "/api/v1/display/stream", headers={"Cookie": f"lostos_session={cks['anna']}"}, timeout=aiohttp.ClientTimeout(total=15)) as r:
            first = await asyncio.wait_for(r.content.readuntil(b"\n\n"), 8)
            check("the display stream opens through the gateway and answers at once", r.status == 200 and "text/event-stream" in r.headers.get("Content-Type", "") and first.startswith(b"data:"), (r.status, first))

        # ---- the settings of the brain are the person's own
        st, d = await api("/api/v1/ai", "anna")
        check("the brain settings answer", st == 200 and d["providers"] and d["contexts"], (st, d))
        st, d = await api("/api/v1/ai", "anna", "POST", {"context": {"id": "music", "on": False}, "temperature": 0.4})
        st, d2 = await api("/api/v1/ai", "boris")
        check("a switched-off context of Anna is not off for Boris", [c["on"] for c in d["contexts"] if c["id"] == "music"] == [False] and [c["on"] for c in d2["contexts"] if c["id"] == "music"] == [True] and d2["temperature"] == 0.7)
        st, _ = await api("/api/v1/ai", "muzh", "POST", {"temperature": 1.2})
        check("a member cannot change the brain settings", st == 403)

        # ---- nobody can speak to a process but through the gateway
        mgr_ports = re.findall(r"tenant (\d+)(?:/\w+)?: started \(pid \d+, port (\d+)", open(os.path.join(DATA, "gateway.log"), encoding="utf-8", errors="ignore").read())
        check("two processes are running", len(mgr_ports) >= 2, mgr_ports)
        if mgr_ports:
            port = int(mgr_ports[0][1])
            async with http.get(f"http://127.0.0.1:{port}/api/state") as r:
                check("a tenant refuses a request without the key of the gateway", r.status == 403)
            async with http.get(f"http://127.0.0.1:{port}/api/state", headers={"X-Lostos-Key": "guess", "X-Lostos-Tg": "100", "X-Lostos-Role": "owner"}) as r:
                check("and a wrong key", r.status == 403)
        st, d = await api("/api/v1/secrets", "muzh", headers={"X-Lostos-Role": "owner", "X-Lostos-Tg": "100"})
        check("headers a person sends himself are not believed (a member stays a member)", st == 403)
        st, _ = await api("/internal/station?tenant=100", None)
        check("the internal door is closed from outside", st == 403)
        st, _ = await api("/t/999/tts/x.wav", None)
        check("the speaker asking for a stranger's tenant that does not run gets nothing", st == 404)

        # ---- the people of the household and the sessions are the household's own
        st, d = await api("/api/v1/account", "anna")
        check("Anna sees her household only", [u["tg_id"] for u in d["users"]] in ([100, 101], [101, 100]) and all(s["tg_id"] in (100, 101) for s in d["sessions"]), d.get("users"))
        st, d = await api("/api/v1/account", "boris")
        check("Boris sees his own only", [u["tg_id"] for u in d["users"]] == [200])
        st, _ = await api("/api/v1/account/users/101", "boris", "DELETE")
        check("Boris cannot remove Anna's member", st == 404 and people.user(101) is not None)

        # ---- registration
        st, d = await api("/api/v1/hello", None)
        check("registration is by approval", d["registration"] == "approval")
        r = people.request_access(300, "Третий", "u300")
        check("a stranger's request waits for the operator", r == "new" and [x["tg_id"] for x in people.pending_requests()] == [300])
        people.decide(300, False)
        check("a refused request stays refused", people.request_access(300, "Третий", "u300") == "denied")

        # ---- the admin panel: its own host, the operator only
        AH = {"Host": "admin.test"}
        st, d = await api("/admin/api/overview", "admin", headers=AH)
        check("the operator sees the admin panel on its host", st == 200 and d["ok"] and {h["id"] for h in d["houses"]} >= {1, 100, 200}, (st, str(d)[:200]))
        st, d2 = await api("/admin/api/overview", "anna", headers=AH)
        check("a person who is not the operator is refused", st == 403)
        st, _ = await api("/admin/api/overview", "admin")
        check("the admin panel does not exist on the host of the people", st == 404)
        st, _ = await api("/api/state", "admin", headers=AH)
        check("the panel of the people does not exist on the admin host", st == 404)
        async with http.get(BASE + "/admin/", headers={"Cookie": f"lostos_session={cks['admin']}", **AH}) as r:
            html = await r.text()
            check("the page of the admin panel is served", r.status == 200 and "админка" in html)
        st, d = await api("/admin/api/ram", "admin", "POST", {"id": 100, "mb": 180}, headers=AH)
        check("the operator sets a memory limit of a person", st == 200 and [h["limit_mb"] for h in d["houses"] if h["id"] == 100] == [180] and mgr_cfg()["overrides"].get("100") == 180, (st, d))
        st, d = await api("/admin/api/ram", "admin", "POST", {"id": 100, "mb": 128, "reset": True}, headers=AH)
        check("and takes it back", [h["own_limit"] for h in d["houses"] if h["id"] == 100] == [False])
        st, d = await api("/admin/api/ram", "admin", "POST", {"id": 100, "mb": 5}, headers=AH)
        check("a limit that is too small is refused", st == 400)
        st, d = await api("/admin/api/config", "admin", "POST", {"registration": "approval", "max_tenants": 9}, headers=AH)
        check("the registration and the number of places are changed", st == 200 and d["config"]["registration"] == "approval" and d["config"]["max_tenants"] == 9, d.get("config"))
        people.request_access(400, "Новый", "new400")
        # ---- the sections of the admin panel
        st, d = await api("/admin/api/config", "admin", "POST", {"registration": "open"}, headers=AH)
        check("registration: open to everybody", st == 200 and d["config"]["registration"] == "open", d)
        st, d = await api("/admin/api/config", "admin", "POST", {"registration": "invite"}, headers=AH)
        check("registration: closed, only by a link", st == 200 and d["config"]["registration"] == "invite", d)
        st, d = await api("/admin/api/config", "admin", "POST", {"registration": "closed"}, headers=AH)
        check("the old word 'closed' means 'by a link'", st == 200 and d["config"]["registration"] == "invite", d)
        st, d = await api("/admin/api/config", "admin", "POST", {"registration": "whatever"}, headers=AH)
        check("another kind of registration is refused", st == 400)
        st, d = await api("/api/v1/hello", None)
        check("the hello of the server says the kind", d["registration"] == "invite", d)
        await api("/admin/api/config", "admin", "POST", {"registration": "approval"}, headers=AH)
        raw_link = people.new_token("reg", by_tg=1)
        st, d = await api("/admin/api/overview", "admin", headers=AH)
        check("an open link of the operator is listed (never its token)", len(d["invites"]) == 1 and raw_link not in json.dumps(d), d.get("invites"))
        st, d2 = await api("/admin/api/invite", "admin", "POST", {}, headers=AH)
        check("a link can not be made without the Telegram bot", st == 503, (st, d2))
        st, d = await api("/admin/api/invite/revoke", "admin", "POST", {"id": d["invites"][0]["id"]}, headers=AH)
        check("a link is revoked", st == 200 and d["invites"] == [] and people.token(raw_link, "reg") is None, d)
        st, d = await api("/admin/api/overview", "admin", headers=AH)
        check("every station of the server is listed with its state", {x["id"] for x in d["stations"]} >= {a_st.id, b_st.id} and all("confirmed" in x and "online" in x for x in d["stations"]), d.get("stations"))
        st, d = await api("/admin/api/security", "admin", headers=AH)
        check("the security section answers with the key and the lists", st == 200 and d["key"] is True and d["serial"] >= 1 and d["developers"] == [] and any(k["role"].startswith("команда") for k in d["trusted_keys"]), d)
        st, _ = await api("/admin/api/security", "anna", headers=AH)
        check("only the operator sees it", st == 403)
        import plugin_sign as psign
        dev_pub = psign.new_keypair()[1]
        st, d = await api("/admin/api/developers", "admin", "POST", {"action": "add", "id": "alice", "name": "Alice", "key": dev_pub}, headers=AH)
        check("a developer is verified from the panel and the list is signed at once", st == 200 and [x["id"] for x in d["developers"]] == ["alice"], d)
        published = json.load(open(os.path.join(site_dir, "developers.json"), encoding="utf-8"))
        check("... the published file has him", [x["id"] for x in published["developers"]] == ["alice"])
        st, d = await api("/admin/api/developers", "admin", "POST", {"action": "add", "id": "bad id", "name": "X", "key": dev_pub}, headers=AH)
        check("a bad id is refused with the reason", st == 400 and d["error"], d)
        st, d = await api("/admin/api/developers", "admin", "POST", {"action": "revoke", "fingerprint": psign.fingerprint(dev_pub), "why": "test"}, headers=AH)
        check("a key is revoked from the panel", st == 200 and d["developers"] == [] and d["revoked"][0]["fingerprint"] == psign.fingerprint(dev_pub), d)
        st, d = await api("/admin/api/servers", "admin", "POST", {"action": "add", "address": "https://os.example.org:8443", "name": "Example"}, headers=AH)
        check("a known server is added (by the hash of its host)", st == 200 and d["servers"][0]["name"] == "Example" and "example" not in json.dumps(d["servers"]).replace("Example", ""), d)
        st, d = await api("/admin/api/servers", "admin", "POST", {"action": "ban", "address": "evil.example.net", "why": "крадёт ключи"}, headers=AH)
        check("a server is banned with the reason", st == 200 and d["banned"][0]["why"] == "крадёт ключи", d)
        st, d = await api("/admin/api/servers", "admin", "POST", {"action": "ban", "address": "os.example.org", "why": "теперь плохой"}, headers=AH)
        check("a banned server leaves the known ones", st == 200 and d["servers"] == [] and len(d["banned"]) == 2, d)
        served = json.load(open(os.path.join(site_dir, "servers.json"), encoding="utf-8"))
        check("... both lists are in the published file", len(served["banned"]) == 2 and served["servers"] == [])
        st, d = await api("/admin/api/servers", "admin", "POST", {"action": "unban", "ident": d["banned"][0]["h"][:12]}, headers=AH)
        check("a server is unbanned", st == 200 and len(d["banned"]) == 1, d)
        st, d = await api("/admin/api/servers", "admin", "POST", {"action": "fly"}, headers=AH)
        check("an unknown action is refused", st == 400)
        # ---- the same server as a HOST (anybody else who runs the published server): no sections of the team, whatever the page asks
        st, d = await api("/admin/api/overview", "admin", headers=AH)
        check("the center says so", d["edition"] == "center")
        open(os.path.join(DATA, "edition"), "w").write("host")
        st, d = await api("/admin/api/overview", "admin", headers=AH)
        check("a host says so (and keeps the people, the registration, the stations, the limits)", st == 200 and d["edition"] == "host" and d["config"]["registration"] and "stations" in d and "houses" in d, d.get("edition"))
        for path, body in (("/admin/api/security", None), ("/admin/api/developers", {"action": "add", "id": "mallory", "name": "M", "key": dev_pub}),
                           ("/admin/api/servers", {"action": "add", "address": "x.example.org", "name": "X"}), ("/admin/api/servers", {"action": "ban", "address": "x.example.org", "why": "no"})):
            st, d = await api(path, "admin", "POST" if body else "GET", body, headers=AH)
            check("a host is refused " + path + (" " + body["action"] if body else ""), st == 403 and "центральном" in d["error"], (st, d))
        check("... and nothing was written", not any(x["id"] == "mallory" for x in json.load(open(os.path.join(site_dir, "developers.json"), encoding="utf-8"))["developers"]))
        for path, body in (("/admin/api/invite/revoke", {"id": "00000000"}),):
            st, d = await api(path, "admin", "POST", body, headers=AH)
            check("what is about his own server still answers for a host", st == 404, (st, d))
        os.environ["LOSTOS_EDITION"] = "center"
        import edition as edmod
        check("the environment can say it too", edmod.edition(DATA) == "center")
        os.environ.pop("LOSTOS_EDITION")
        open(os.path.join(DATA, "edition"), "w").write("center")
        st, d = await api("/admin/api/overview", "admin", headers=AH)
        check("a request shows up", [r["id"] for r in d["requests"]] == [400])
        st, d = await api("/admin/api/decide", "admin", "POST", {"id": 400, "approve": True}, headers=AH)
        check("the operator lets him in and he has a house of his own", st == 200 and people.tenant_of(400) == 400 and not d["requests"], (st, d.get("requests")))
        st, d = await api("/admin/api/remove", "admin", "POST", {"id": 1}, headers=AH)
        check("the operator cannot be removed", st == 400)
        st, d = await api("/admin/api/config", "anna", "POST", {"max_tenants": 1}, headers=AH)
        check("nobody else changes the settings", st == 403)

        # ---- the processes stop and start
        st, d = await api("/api/v1/setup", "boris", "POST", {"style": "detailed"})
        os.makedirs(os.path.join(DATA, "tenants", "200"), exist_ok=True)
        open(os.path.join(DATA, "tenants", "200", "restart"), "w").close()
        await asyncio.sleep(8)
        log = open(os.path.join(DATA, "gateway.log"), encoding="utf-8", errors="ignore").read()
        check("a restart asked by the admin restarts that household only", len(re.findall(r"tenant 200(?:/\w+)?: restart asked by the admin", log)) >= 1
              and not re.findall(r"tenant 100(?:/\w+)?: restart asked by the admin", log), log[-600:])
        st, d = await api("/api/v1/setup", "boris")
        check("after the restart Boris's settings are still there", st == 200 and d["style"] == "detailed")
        # ---- the vault follows the speaker: it closes when the speaker leaves and a reset speaker (another vault key) does not open it
        tb.cancel()
        await asyncio.sleep(3)
        st, d = await api("/api/v1/secrets", "boris")
        check("the vault closes when the speaker leaves", st == 423 or d.get("locked") is True, (st, d))
        b_reset = FakeStation("Колонка Бориса после сброса")
        b_reset.key, b_reset.pub = b_st.key, b_st.pub                   # the same speaker identity, a new vault key
        rr = asyncio.Event()
        tr = asyncio.create_task(b_reset.run(http, rr))
        await rr.wait()
        await asyncio.sleep(4)
        st, d = await api("/api/v1/secrets", "boris")
        check("a speaker with another vault key does not open it", st == 423 or d.get("locked") is True, (st, d))
        tr.cancel()
        ta.cancel()
        await http.close()
    finally:
        for p in PROCS:
            try:
                p.terminate()
            except Exception:
                pass
        for p in PROCS:
            try:
                p.wait(10)
            except Exception:
                p.kill()
        print("ALL PASS" if OK else "SOME FAILED")
        if not OK:
            for tag in ("gateway", "engine"):
                try:
                    print(f"--- {tag}.log tail ---")
                    print(open(os.path.join(DATA, tag + ".log"), encoding="utf-8", errors="ignore").read()[-2500:])
                except OSError:
                    pass
            for t in os.listdir(os.path.join(DATA, "tenants")) if os.path.isdir(os.path.join(DATA, "tenants")) else []:
                try:
                    print(f"--- tenant {t} server.log tail ---")
                    print(open(os.path.join(DATA, "tenants", t, "server.log"), encoding="utf-8", errors="ignore").read()[-1500:])
                except OSError:
                    pass
        shutil.rmtree(DATA, ignore_errors=True) if OK else print("data kept in", DATA)
        sys.exit(0 if OK else 1)


asyncio.run(main())
