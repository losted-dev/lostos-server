"""Vault and station link test: a fake station (what the firmware does) against a lite server started here.
Run: python tests_vault.py      (starts voice_server.py in lite mode on a free port with a temporary data folder)"""
import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 18777
BASE = f"http://127.0.0.1:{PORT}"
OK = True


def check(name, cond):
    global OK
    print(("PASS " if cond else "FAIL ") + name)
    OK = OK and cond


CODE = os.path.join(tempfile.gettempdir(), "lostos-test-code")


def start_server(data):
    """Runs a clean copy of the code (tools/stage-server.sh: no .env of the owner, no tokens), so the test never touches or reads the real settings."""
    if not os.path.isdir(CODE):
        subprocess.run(["bash", os.path.join(HERE, "..", "tools", "stage-server.sh"), CODE], check=True, stdout=subprocess.DEVNULL)
    env = dict(os.environ, LOSTOS_LITE="1", LOSTOS_AUTH="1", LOSTOS_PUBLIC_URL=BASE, TTS_PORT=str(PORT), LOSTOS_DATA=data, PYTHONIOENCODING="utf-8")
    for k in [k for k in env if k.endswith("_API_KEY") or k.startswith(("SPOTIFY_", "HA_"))]:
        env.pop(k)
    p = subprocess.Popen([sys.executable, "-u", os.path.join(CODE, "voice_server.py")], cwd=CODE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/auth/whoami", timeout=1)
            return p
        except Exception as e:
            if getattr(e, "code", 0):
                return p
            time.sleep(0.5)
    raise RuntimeError("server did not start")


class Station:
    def __init__(self, priv: Ed25519PrivateKey, vault_key: bytes, api_key: str):
        self.priv, self.vault_key, self.api_key = priv, vault_key, api_key
        self.pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.id = hashlib.sha256(self.pub).hexdigest()[:20]
        self.state = ""

    async def run(self, ws_ready: asyncio.Event, stop: asyncio.Event):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(BASE + "/link/ws") as ws:
                ch = await ws.receive_json()
                await ws.send_json({"t": "hello", "ver": 1, "pub": base64.b64encode(self.pub).decode(), "name": "test station",
                                    "sig": base64.b64encode(self.priv.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")).decode()})
                ws_ready.set()
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    m = json.loads(msg.data)
                    if m["t"] in ("welcome", "state"):
                        self.state = m["state"]
                        if m["state"] == "confirmed":
                            await ws.send_json({"t": "vaultkey", "key": base64.b64encode(self.vault_key).decode()})
                            await ws.send_json({"t": "apikey", "key": self.api_key})
                    if stop.is_set():
                        break
                    if m["t"] == "state":
                        await asyncio.sleep(0.2)


def _json(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:                                  # an HTML page (the callback answers with one)
        return {}


def api(path, cookie, data=None, method=None):
    req = urllib.request.Request(BASE + path, data=None if data is None else json.dumps(data).encode(), method=method,
                                 headers={"Cookie": cookie, "Content-Type": "application/json", "Origin": BASE})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())


async def main():
    data = tempfile.mkdtemp()
    vault_key = os.urandom(32)
    api_key = base64.b64encode(os.urandom(32)).decode()
    priv = Ed25519PrivateKey.generate()
    srv = start_server(data)
    try:
        link = open(os.path.join(data, "setup-link.txt")).read().strip()
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(link.replace("https://", "http://"), timeout=5)
        except urllib.error.HTTPError as e:
            cookie = e.headers["Set-Cookie"].split(";")[0]
        check("owner logs in with the one-time link", cookie.startswith("lostos_session="))
        st, d = api("/api/v1/secrets", cookie)
        check("the keys are out of reach until a station is confirmed (423)", st == 423 and d.get("locked") is True)
        st, d = api("/api/v1/account", cookie)
        check("the account page still works while locked", st == 200 and d["locked"] is True)
        st, d = api("/api/v1/secrets", cookie, {"name": "GEMINI_API_KEY", "value": "SECRET-GEMINI-123"})
        check("cannot save while no station is confirmed (423)", st == 423)

        station = Station(priv, vault_key, api_key)
        ready, stop = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(station.run(ready, stop))
        await ready.wait()
        await asyncio.sleep(0.5)
        st, d = api("/api/v1/stations", cookie)
        check("a waiting station is listed to nobody", d["stations"] == [])
        st, d = api("/api/v1/secrets", cookie)
        check("a pending station does not open the vault", d["locked"] is True)
        task.cancel()

        st, d = api("/api/v1/stations/claim", cookie, {"id": station.id}, "POST")        # the owner types the identifier of his station
        check("the identifier connects the station (old firmware: at once)", d.get("ok") is True)
        station2 = Station(priv, vault_key, api_key)
        ready, stop = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(station2.run(ready, stop))
        await ready.wait()
        await asyncio.sleep(1.5)
        st, d = api("/api/v1/secrets", cookie)
        check("a confirmed station opens the vault", d["locked"] is False)
        st, d = api("/api/v1/secrets", cookie, {"name": "GEMINI_API_KEY", "value": "SECRET-GEMINI-123"})
        check("a key is saved", st == 200)
        st, d = api("/api/v1/secrets", cookie)
        check("the panel shows that it is saved but never the value", any(k["name"] == "GEMINI_API_KEY" and k["saved"] for k in d["keys"]) and "SECRET-GEMINI-123" not in json.dumps(d))
        raw = open(os.path.join(data, "secrets.enc"), "rb").read()
        check("the file on the disk holds no plain text", b"SECRET-GEMINI-123" not in raw and api_key.encode() not in raw and b"GEMINI" not in raw)
        st_json = open(os.path.join(data, "stations.json"), encoding="utf-8").read()
        check("stations.json holds no API key", api_key not in st_json)
        # ---- Spotify login (the part that does not need Spotify itself)
        st, d = api("/api/v1/spotify", cookie)
        check("spotify: not logged in, no client id yet", st == 200 and d["logged_in"] is False and d["client_id"] is False and d["redirect"] == BASE + "/spotify/callback")
        st, d = api("/api/v1/spotify/login", cookie, {}, "POST")
        check("spotify: login refused without a client id", st == 400)
        api("/api/v1/secrets", cookie, {"name": "SPOTIFY_CLIENT_ID", "value": "cid123"})
        st, d = api("/api/v1/spotify/login", cookie, {}, "POST")
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(d.get("url", "")).query)
        check("spotify: login url has PKCE and our redirect", st == 200 and q.get("code_challenge_method") == ["S256"] and q.get("redirect_uri") == [BASE + "/spotify/callback"] and q.get("client_id") == ["cid123"] and "state" in q)
        st, _ = api("/spotify/callback?code=x&state=forged", cookie)
        check("spotify: a forged state is refused", st == 400)
        st, _ = api("/spotify/callback?code=x&state=whatever", "")
        check("spotify: callback needs the owner's session", st == 401)
        task.cancel()
        await asyncio.sleep(0.5)
        st, d = api("/api/v1/secrets", cookie)
        check("the vault closes when the station leaves", d["locked"] is True)

        srv.terminate(); srv.wait(10)
        srv = start_server(data)                                                       # a restart of the server
        st, d = api("/api/v1/secrets", cookie)
        check("after a restart the vault is locked again", st == 200 and d["locked"] is True)
        ready, stop = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(Station(priv, vault_key, api_key).run(ready, stop))
        await ready.wait()
        await asyncio.sleep(1.5)
        st, d = api("/api/v1/secrets", cookie)
        check("the same station opens it again with the saved key inside", d["locked"] is False and any(k["name"] == "GEMINI_API_KEY" and k["saved"] for k in d["keys"]))
        task.cancel()
        await asyncio.sleep(0.5)

        ready, stop = asyncio.Event(), asyncio.Event()                                 # the same station id but another vault key (a reset speaker)
        task = asyncio.create_task(Station(priv, os.urandom(32), api_key).run(ready, stop))
        await ready.wait()
        await asyncio.sleep(1.5)
        st, d = api("/api/v1/secrets", cookie)
        check("a wrong vault key does not open it", d["locked"] is True)
        task.cancel()
    finally:
        shutil.rmtree(CODE, ignore_errors=True)
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)
    print("ALL PASS" if OK else "SOME FAILED")
    sys.exit(0 if OK else 1)


asyncio.run(main())
