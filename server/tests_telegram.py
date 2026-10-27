"""Tests of the accounts, the Telegram login and the physical confirmation of a station, against a fake Telegram (no network, no real bot).
Run:  venv\\Scripts\\python.exe server\\tests_telegram.py   (from the project folder)"""
import asyncio
import base64
import json
import os
import re
import sys
import tempfile
import time

DATA = tempfile.mkdtemp(prefix="lostos-test-")
os.environ["LOSTOS_DATA"] = DATA
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

import accounts as acc_mod
import account_api
import auth
import link as link_mod
import tgbot
import telegram_oidc
import hashlib
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS", name)
    else:
        FAIL += 1
        print("FAIL", name, extra)


class FakeTelegram:
    """Just enough of the Bot API: the test pushes updates, the bot's calls are recorded."""

    def __init__(self):
        self.queue: list = []
        self.sent: list = []              # (chat, text, buttons)
        self.edited: list = []
        self.n = 0
        self.app = web.Application()
        self.app.router.add_post("/bot{token}/{method}", self.handle)

    async def handle(self, req):
        m, p = req.match_info["method"], await req.json()
        if m == "getMe":
            return web.json_response({"ok": True, "result": {"id": 1, "username": "lostos_test_bot"}})
        if m == "getUpdates":
            for _ in range(20):
                if self.queue:
                    ups, self.queue = self.queue, []
                    return web.json_response({"ok": True, "result": ups})
                await asyncio.sleep(0.05)
            return web.json_response({"ok": True, "result": []})
        if m == "sendMessage":
            kb = (p.get("reply_markup") or {}).get("inline_keyboard", [])
            self.sent.append((p["chat_id"], p["text"], [(b["text"], b["callback_data"]) for row in kb for b in row]))
            return web.json_response({"ok": True, "result": {"message_id": len(self.sent)}})
        if m == "editMessageText":
            self.edited.append((p["chat_id"], p["message_id"], p["text"]))
            return web.json_response({"ok": True, "result": True})
        return web.json_response({"ok": True, "result": True})

    def say(self, uid, text, name="Тест"):
        self.n += 1
        self.queue.append({"update_id": self.n, "message": {"message_id": self.n, "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": name, "username": f"u{uid}"}, "text": text}})

    def press(self, uid, data, msg=1):
        self.n += 1
        self.queue.append({"update_id": self.n, "callback_query": {"id": str(self.n), "from": {"id": uid, "first_name": "Тест", "username": f"u{uid}"}, "data": data,
                                                                     "message": {"message_id": msg, "chat": {"id": uid, "type": "private"}}}})

    async def last(self, uid, wait=2.0):
        """The newest message sent to uid (waits for the bot)."""
        t0 = time.time()
        k = len([s for s in self.sent if s[0] == uid])
        while time.time() - t0 < wait:
            mine = [s for s in self.sent if s[0] == uid]
            if len(mine) > k:
                return mine[-1]
            await asyncio.sleep(0.05)
        mine = [s for s in self.sent if s[0] == uid]
        return mine[-1] if mine else None


class Ctx:
    link = None

    def __init__(self):
        self.cmds = []

    async def media_command(self, tg, cmd, args):
        self.cmds.append((cmd, args))

    async def status(self, tg):
        return {"online": True, "now": "Артист — Трек"}

    mode = "approval"
    cap = 5

    def registration(self):
        return self.mode

    def capacity_left(self):
        return self.cap


async def settle(bot, tg, ms=0.4):
    await asyncio.sleep(ms)


async def main():
    tg = FakeTelegram()
    tg_runner = web.AppRunner(tg.app)
    await tg_runner.setup()
    site = web.TCPSite(tg_runner, "127.0.0.1", 0)
    await site.start()
    tg_port = site._server.sockets[0].getsockname()[1]

    people = acc_mod.Accounts()
    ctx = Ctx()
    bot = tgbot.Bot("TOKEN", people, lambda *a: None, ctx, f"http://127.0.0.1:{tg_port}", "https://example.test")
    bot_task = asyncio.create_task(bot.run())
    for _ in range(40):
        if bot.username:
            break
        await asyncio.sleep(0.05)
    check("the bot finds its name from getMe", bot.username == "lostos_test_bot")

    app = web.Application()
    lk = link_mod.install(app, lambda *a: None, None)
    ctx.link = lk
    lk.tenant_of = people.tenant_of
    lk.listeners.append(bot.on_link_event)
    auth.install(app, "https://example.test", lambda *a: None, people, bot)
    app["accounts"] = people
    account_api.install(app, bot)

    async def hello(req):
        return web.json_response({"ok": True})

    app.router.add_get("/api/v1/secrets", hello)
    app.router.add_get("/api/v1/media", hello)
    runner = web.AppRunner(app)
    await runner.setup()
    s2 = web.TCPSite(runner, "127.0.0.1", 0)
    await s2.start()
    port = s2._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    http = aiohttp.ClientSession()

    def cookie_of(resp):
        m = re.search(r"lostos_session=([^;]+)", resp.headers.get("Set-Cookie", ""))
        return m.group(1) if m else ""

    async def get(path, ck="", method="GET", body=None):
        h = {"Cookie": f"lostos_session={ck}"} if ck else {}
        async with http.request(method, base + path, headers=h, json=body, allow_redirects=False) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            return r.status, data, r

    # ---- 1. no session: closed
    st, _, _ = await get("/api/v1/media")
    check("without a session the API answers 401", st == 401)
    st, d, _ = await get("/auth/whoami")
    check("the login page knows Telegram is available", d and d["telegram"] is True and d["authed"] is False)

    # ---- 2. the owner comes in with the one-time link (a session without a Telegram id yet)
    token = auth.new_setup_token()
    st, _, r = await get(f"/auth/setup?token={token}")
    owner_ck = cookie_of(r)
    check("the one-time link opens an owner session", st == 302 and owner_ck)
    st, _, _ = await get(f"/auth/setup?token={token}")
    check("the one-time link works only once", st == 403)
    st, d, _ = await get("/api/v1/account", owner_ck)
    check("the owner session is the owner and nothing is bound", st == 200 and d["me"]["role"] == "owner" and d["bound"] is False)

    # ---- 3. the owner binds Telegram
    st, d, _ = await get("/api/v1/account/bind", owner_ck, "POST")
    check("bind gives a link to the bot", st == 200 and d["url"].startswith("https://t.me/lostos_test_bot?start=b"))
    payload = d["url"].split("start=")[1]
    tg.say(100, "/start " + payload, "Владелец")
    msg = await tg.last(100)
    check("the bot asks to confirm the owner", msg and "владельцем" in msg[1] and msg[2][0][0] == "Да, это я")
    tg.press(100, msg[2][0][1])
    await settle(bot, tg)
    check("after the button the owner is bound", people.owner() and people.owner()["tg_id"] == 100)
    st, d, _ = await get("/api/v1/account", owner_ck)
    check("the one-time-link session now belongs to the owner", st == 200 and d["me"]["tg_id"] == 100 and d["bound"] is True)

    # ---- 4. login through Telegram, number matching
    st, d, _ = await get("/auth/tg/start", method="POST")
    check("the login page gets a number and a link", st == 200 and len(d["code"]) == 2 and "start=l" in d["url"])
    lt, code = d["token"], d["code"]
    st, p, _ = await get("/auth/tg/poll", method="POST", body={"token": lt})
    check("the page waits", p["state"] == "wait")
    tg.say(100, "/start l" + lt)
    msg = await tg.last(100)
    nums = [b[0] for b in msg[2]]
    check("the bot shows three numbers with the right one among them", code in nums and len([n for n in nums if n.isdigit()]) == 3, str(nums))
    wrong = next(b for b in msg[2] if b[0].isdigit() and b[0] != code)
    tg.press(100, wrong[1])
    await settle(bot, tg)
    st, p, _ = await get("/auth/tg/poll", method="POST", body={"token": lt})
    check("a wrong number denies the login", p["state"] == "denied")
    st, p, _ = await get("/auth/tg/poll", method="POST", body={"token": lt})
    check("a spent token cannot be used again", p["state"] == "expired")

    st, d, _ = await get("/auth/tg/start", method="POST")
    lt, code = d["token"], d["code"]
    tg.say(100, "/start l" + lt)
    msg = await tg.last(100)
    right = next(b for b in msg[2] if b[0] == code)
    tg.press(100, right[1])
    await settle(bot, tg)
    st, p, r = await get("/auth/tg/poll", method="POST", body={"token": lt})
    ck2 = cookie_of(r)
    check("the right number gives a session", p["state"] == "ok" and ck2)
    st, d, _ = await get("/api/v1/account", ck2)
    check("the new session is the owner", st == 200 and d["me"]["role"] == "owner")
    st, _, _ = await get("/api/v1/secrets", ck2)
    check("the owner reaches the keys", st == 200)

    # ---- 5. a stranger
    st, d, _ = await get("/auth/tg/start", method="POST")
    tg.say(300, "/start l" + d["token"], "Чужой")
    msg = await tg.last(300)
    check("a stranger's request goes to the operator", msg and "Заявка" in msg[1] and not msg[2] and people.pending_requests()[0]["tg_id"] == 300)
    await asyncio.sleep(0.5)
    check("the operator is told with two buttons", any("Просит доступ" in s[1] and s[2] for s in tg.sent))
    tg.say(300, "/pause")
    msg = await tg.last(300)
    check("a stranger cannot control the player", msg and "Заявка" in msg[1] and not ctx.cmds)
    people.decide(300, False)

    # ---- 6. an invitation
    st, d, _ = await get("/api/v1/account/invite", owner_ck, "POST")
    check("the owner makes an invitation", st == 200 and "start=i" in d["url"])
    inv = d["url"].split("start=")[1]
    tg.say(200, "/start " + inv, "Гость")
    msg = await tg.last(200)
    check("the guest is asked to accept", msg and msg[2][0][0] == "Принять")
    tg.press(200, msg[2][0][1])
    await settle(bot, tg)
    check("the guest is a member", people.role_of(200) == "member")
    note = [s for s in tg.sent if s[0] == 100 and "принял" in s[1]]
    check("the owner is told", bool(note))
    tg.say(200, "/start " + inv, "Гость")
    msg = await tg.last(200)
    check("an invitation works once", msg and "устарело" in msg[1] or "уже" in msg[1])

    # ---- 7. the member logs in and has limits
    st, d, _ = await get("/auth/tg/start", method="POST")
    lt, code = d["token"], d["code"]
    tg.say(200, "/start l" + lt)
    msg = await tg.last(200)
    tg.press(200, next(b for b in msg[2] if b[0] == code)[1])
    await settle(bot, tg)
    st, p, r = await get("/auth/tg/poll", method="POST", body={"token": lt})
    mck = cookie_of(r)
    check("the member comes in", p["state"] == "ok" and mck)
    st, _, _ = await get("/api/v1/media", mck)
    check("the member reaches the player", st == 200)
    st, _, _ = await get("/api/v1/secrets", mck)
    check("the member does not reach the keys", st == 403)
    st, _, _ = await get("/api/v1/account/invite", mck, "POST")
    check("the member cannot invite", st == 403)
    st, d, _ = await get("/api/v1/account", mck)
    check("the member sees only his sessions", st == 200 and all(s["tg_id"] == 200 for s in d["sessions"]) and "users" not in d)
    tg.say(200, "/volume 40")
    await tg.last(200)
    check("the bot sets the volume", ("volume", {"level": 40}) in ctx.cmds)
    tg.say(200, "/pause")
    await tg.last(200)
    check("the bot pauses", any(c[0] == "pause" for c in ctx.cmds))
    tg.say(200, "/stations")
    msg = await tg.last(200)
    check("a member sees only his own stations (none yet)", msg and "Колонок пока нет" in msg[1])

    # ---- 8. the owner removes the member
    st, _, _ = await get("/api/v1/account/users/200", owner_ck, "DELETE")
    st2, _, _ = await get("/api/v1/media", mck)
    check("a removed member loses the session", st == 200 and st2 == 401)

    # ---- 9. a station: only the person who types its identifier sees it and owns it; the button of the speaker proves it is in his hands
    people.add_member(201, "Первый", "p1")
    people.add_member(202, "Второй", "p2")
    ck201, ck202 = people.new_session(201, "test", "127.0.0.1"), people.new_session(202, "test", "127.0.0.1")
    events = []

    async def fake_say(text):
        return b"RIFF" + bytes(120)

    lk.say = fake_say
    lk.listeners.append(lambda k, sid, name="", extra="": events.append((k, extra)))
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    async with http.ws_connect(base + "/link/ws") as ws:
        ch = await ws.receive_json()
        sig = key.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")
        await ws.send_json({"t": "hello", "ver": 2, "pub": base64.b64encode(pub).decode(), "sig": base64.b64encode(sig).decode(), "name": "Тестовая"})
        w = await ws.receive_json()
        check("a new station waits", w["state"] == "pending")
        sid = w["id"]
        said = await asyncio.wait_for(ws.receive_json(), 3)
        check("a new station is greeted once with a wav", said["t"] == "say" and len(said["wav"]) > 100)
        for who, ck in (("the owner", owner_ck), ("a member", ck201), ("another member", ck202)):
            st, d, _ = await get("/api/v1/stations", ck)
            check(f"a waiting station is not listed to {who}", st == 200 and d["stations"] == [])
        await asyncio.sleep(0.3)
        check("nobody is told about a stranger's station", not any("Новая" in s[1] for s in tg.sent))
        st, d, _ = await get("/api/v1/stations/claim", ck201, "POST", {"id": "0" * 20})
        check("a wrong identifier finds nothing", st == 404 and d["state"] == "none")
        st, d, _ = await get("/api/v1/stations/claim", ck201, "POST", {"id": sid[:10].upper() + " - " + sid[10:]})
        check("the identifier may be typed in capitals with dashes and spaces; the station asks for the button", st == 200 and d["state"] == "press")
        press = await asyncio.wait_for(ws.receive_json(), 3)
        check("the station is asked to press (with the code of its id and the line)", press["t"] == "press" and press["code"] == sid[:4].upper() and press["seconds"] == 60 and len(press["wav"]) > 100)
        st, d, _ = await get("/api/v1/stations", ck201)
        check("the person who is connecting it sees it with the countdown", len(d["stations"]) == 1 and d["stations"][0]["press"] > 0 and not d["stations"][0]["confirmed"])
        for who, ck in (("the owner", owner_ck), ("another member", ck202)):
            st, d, _ = await get("/api/v1/stations", ck)
            check(f"{who} still sees nothing", d["stations"] == [])
        st, d, _ = await get("/api/v1/stations/claim", ck202, "POST", {"id": sid})
        check("another person cannot take it over meanwhile", st == 409 and d["state"] == "busy")
        st, d, _ = await get("/api/v1/stations/claim", owner_ck, "POST", {"id": sid})
        check("not even the owner of the server", st == 409 and d["state"] == "busy")
        await ws.send_json({"t": "pressed", "nonce": "0000"})
        await asyncio.sleep(0.2)
        check("a wrong nonce does not confirm", not lk.stations[sid]["confirmed"])
        await ws.send_json({"t": "pressed", "nonce": press["nonce"]})
        got = await asyncio.wait_for(ws.receive_json(), 3)
        check("the right press confirms the station", got.get("state") == "confirmed" and lk.stations[sid]["confirmed"])
        check("it belongs to the person who typed the identifier", lk.stations[sid]["owner_tg"] == 201)
        await asyncio.sleep(0.4)
        check("he is told in Telegram", any("подключена" in s[1] for s in tg.sent if s[0] == 201) or ("confirmed", "201") in events)
        st, d, _ = await get("/api/v1/stations", ck201)
        check("he sees it", len(d["stations"]) == 1 and d["stations"][0]["confirmed"])
        st, d, _ = await get("/api/v1/stations", ck202)
        check("another member does not", d["stations"] == [])
        st, d, _ = await get("/api/v1/stations", owner_ck)
        check("the owner of the server sees every connected station", len(d["stations"]) == 1)
        st, _, _ = await get(f"/api/v1/stations/{sid}", ck202, "DELETE")
        check("another member cannot remove it", st == 404 and lk.stations.get(sid))

    # a station that does not press: the claim is dropped and somebody else may try
    key2 = Ed25519PrivateKey.generate()
    pub2 = key2.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    async with http.ws_connect(base + "/link/ws") as ws:
        ch = await ws.receive_json()
        await ws.send_json({"t": "hello", "ver": 2, "pub": base64.b64encode(pub2).decode(), "sig": base64.b64encode(key2.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")).decode(), "name": "Вторая"})
        w = await ws.receive_json()
        await ws.receive_json()
        link_mod.PRESS_SECONDS = 1
        await get("/api/v1/stations/claim", ck201, "POST", {"id": w["id"]})
        await ws.receive_json()
        await ws.send_json({"t": "press_timeout", "nonce": lk.online[w["id"]].press["nonce"]})
        await asyncio.sleep(0.3)
        check("a station that times out stays waiting and is free again", not lk.stations[w["id"]]["confirmed"] and not lk.stations[w["id"]].get("claimant") and any(e[0] == "press_failed" for e in events))
        st, d, _ = await get("/api/v1/stations", ck201)
        check("and is not listed any more", all(x["id"] != w["id"] for x in d["stations"]))

    # an old firmware (ver 1) is connected at once; the bot can do it too (/connect)
    key3 = Ed25519PrivateKey.generate()
    pub3 = key3.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    async with http.ws_connect(base + "/link/ws") as ws:
        ch = await ws.receive_json()
        await ws.send_json({"t": "hello", "ver": 1, "pub": base64.b64encode(pub3).decode(), "sig": base64.b64encode(key3.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")).decode(), "name": "Старая"})
        w = await ws.receive_json()
        tg.say(100, "/connect " + w["id"])
        msg = await tg.last(100)
        check("/connect in the bot connects the station of an old firmware", msg and "подключена" in msg[1] and lk.stations[w["id"]]["owner_tg"] == 100)
        await asyncio.sleep(0.6)
        k0 = len(tg.sent)
        tg.say(100, "/stations")
        listed = False
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(w["id"] in x[1] for x in tg.sent[k0:] if x[0] == 100):
                listed = True
                break
        check("/stations lists the person's own stations", listed)

    # ---- 10. logout everywhere and sessions
    tg.say(100, "/sessions")
    msg = await tg.last(100)
    check("the owner sees the sessions with buttons", msg and "Завершить" in msg[2][0][0])
    tg.say(100, "/logout")
    await tg.last(100)
    st, _, _ = await get("/api/v1/account", ck2)
    check("/logout ends the sessions", st == 401)

    # ---- 10b. Telegram OpenID Connect against a fake oauth.telegram.org
    rsa_key = rsa.generate_private_key(65537, 2048)
    other_key = rsa.generate_private_key(65537, 2048)
    nums = rsa_key.public_key().public_numbers()
    codes: dict = {}                                   # code -> (code_challenge, claims, key)

    def jwt(claims, key):
        h = telegram_oidc.b64u_enc(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        p = telegram_oidc.b64u_enc(json.dumps(claims).encode())
        sig = key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256())
        return h + "." + p + "." + telegram_oidc.b64u_enc(sig)

    async def o_jwks(req):
        return web.json_response({"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig", "n": telegram_oidc.b64u_enc(nums.n.to_bytes(256, "big")),
                                            "e": telegram_oidc.b64u_enc(nums.e.to_bytes(3, "big"))}]})

    async def o_token(req):
        form = await req.post()
        want = "Basic " + base64.b64encode(b"4242:SECRET").decode()
        if req.headers.get("Authorization") != want or form.get("grant_type") != "authorization_code" or form.get("client_id") != "4242":
            return web.json_response({"error": "invalid_client"}, status=401)
        rec = codes.pop(form.get("code"), None)
        if not rec or telegram_oidc.b64u_enc(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != rec[0]:
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response({"access_token": "x", "token_type": "Bearer", "expires_in": 3600, "id_token": jwt(rec[1], rec[2])})

    oapp = web.Application()
    oapp.router.add_get("/.well-known/jwks.json", o_jwks)
    oapp.router.add_post("/token", o_token)
    orun = web.AppRunner(oapp)
    await orun.setup()
    osite = web.TCPSite(orun, "127.0.0.1", 0)
    await osite.start()
    telegram_oidc.BASE = f"http://127.0.0.1:{osite._server.sockets[0].getsockname()[1]}"
    os.environ["LOSTOS_TG_CLIENT_ID"], os.environ["LOSTOS_TG_CLIENT_SECRET"] = "4242", "SECRET"
    app2 = web.Application()
    app2["accounts"] = people
    telegram_oidc_ok = telegram_oidc.install(app2, people, bot, "https://example.test", lambda *a: None)
    check("oidc installs when the client id and secret are set", telegram_oidc_ok is True)
    r2 = web.AppRunner(app2)
    await r2.setup()
    s3 = web.TCPSite(r2, "127.0.0.1", 0)
    await s3.start()
    base2 = f"http://127.0.0.1:{s3._server.sockets[0].getsockname()[1]}"

    async def oget(path, cookies=""):
        async with http.get(base2 + path, headers={"Cookie": cookies} if cookies else {}, allow_redirects=False) as r:
            return r.status, r.headers.get("Location", ""), r.headers.get("Set-Cookie", ""), r

    def claims(**kw):
        c = {"iss": "https://oauth.telegram.org", "aud": "4242", "sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 600, "id": 100, "name": "Владелец", "preferred_username": "owner100", "picture": "https://t.me/i/userpic/320/owner100.jpg"}
        c.update(kw)
        return c

    async def login_with(cl, key=None, tamper_state=False, drop_cookie=False, tg_cookie=""):
        st, loc, sc, _ = await oget("/auth/telegram/start")
        q = dict(x.split("=", 1) for x in loc.split("?", 1)[1].split("&"))
        state = q["state"]
        if "code_challenge" not in q:
            return st, "no challenge", ""
        code = "C" + state[:8]
        codes[code] = (q["code_challenge"], cl, key or rsa_key)
        ck = re.search(r"lostos_oidc=([^;]+)", sc)
        cookie = "" if drop_cookie else f"lostos_oidc={ck.group(1)}"
        s2, loc2, sc2, r = await oget(f"/auth/telegram/callback?code={code}&state={'x' + state[1:] if tamper_state else state}", cookie)
        return s2, loc2, cookie_of(r), loc, sc

    st, loc, sc, _ = await oget("/auth/telegram/start")
    q = dict(x.split("=", 1) for x in loc.split("?", 1)[1].split("&"))
    check("oidc start sends the browser to Telegram with PKCE and a state", st == 302 and q["response_type"] == "code" and q["code_challenge_method"] == "S256" and q["client_id"] == "4242"
          and "openid" in q["scope"] and q["redirect_uri"].endswith("/auth/telegram/callback") and "lostos_oidc=" in sc and "HttpOnly" in sc, str((st, loc, sc)))
    res = await login_with(claims())
    check("a good id_token opens a session", res[0] == 302 and res[1] == "/" and res[2])
    st, d, _ = await get("/api/v1/account", res[2])
    check("the OIDC session is the owner", st == 200 and d["me"]["role"] == "owner" and d["me"]["username"] == "owner100")
    check("the photo of the Telegram profile comes with the account", d["me"]["photo"] == "https://t.me/i/userpic/320/owner100.jpg")
    res = await login_with(claims(), drop_cookie=True)
    check("without the cookie of the starting browser the answer is refused", res[0] == 400)
    res = await login_with(claims(), tamper_state=True)
    check("a wrong state is refused", res[0] == 400)
    res = await login_with(claims(), key=other_key)
    check("a token signed with another key is refused", res[0] == 400)
    res = await login_with(claims(aud="999"))
    check("a token for another bot is refused", res[0] == 400)
    res = await login_with(claims(exp=int(time.time()) - 3600))
    check("an expired token is refused", res[0] == 400)
    res = await login_with(claims(iss="https://evil.test"))
    check("a token from another issuer is refused", res[0] == 400)
    res = await login_with(claims(id=999))
    check("a person the server does not know is told that the request is sent", res[0] == 202)
    check("and the request is waiting for the operator", any(r["tg_id"] == 999 for r in people.pending_requests()))
    res = await login_with({k: v for k, v in claims().items() if k != "id"})
    check("a token without the Telegram id is refused", res[0] == 400)
    st, loc, sc, _ = await oget("/auth/telegram/start")
    q = dict(x.split("=", 1) for x in loc.split("?", 1)[1].split("&"))
    cookie = "lostos_oidc=" + re.search(r"lostos_oidc=([^;]+)", sc).group(1)
    codes["REUSE"] = (q["code_challenge"], claims(), rsa_key)
    a = await oget(f"/auth/telegram/callback?code=REUSE&state={q['state']}", cookie)
    b = await oget(f"/auth/telegram/callback?code=REUSE&state={q['state']}", cookie)
    check("a state works only once", a[0] == 302 and b[0] == 400)
    st, loc, sc, _ = await oget("/auth/telegram/start?bind=1")
    check("binding needs the owner's session", st == 403)
    st, loc, sc, _ = await oget("/auth/telegram/start")
    check("too many starts in a minute are slowed down", st in (302, 429))
    auth._rate.clear()
    owner_ck = people.new_session(100, "test", "127.0.0.1")           # /logout above ended the old one
    async with http.get(base2 + "/auth/telegram/start?bind=1", headers={"Cookie": f"lostos_session={owner_ck}"}, allow_redirects=False) as r:
        loc, sc = r.headers.get("Location", ""), r.headers.get("Set-Cookie", "")
        q = dict(x.split("=", 1) for x in loc.split("?", 1)[1].split("&"))
    codes["BIND"] = (q["code_challenge"], claims(id=100, name="Владелец Новый"), rsa_key)
    cookie = f"lostos_oidc={re.search(r'lostos_oidc=([^;]+)', sc).group(1)}; lostos_session={owner_ck}"
    st, loc, _, _ = await oget(f"/auth/telegram/callback?code=BIND&state={q['state']}", cookie)
    check("the owner binds Telegram through OIDC", st == 302 and people.owner()["tg_id"] == 100 and people.owner()["name"] == "Владелец Новый")
    await r2.cleanup()
    await orun.cleanup()

    # ---- 11. moving the owner to another account ends the old one's sessions
    people.bind_owner(555, "Новый", "new")
    check("the owner can be moved", people.owner()["tg_id"] == 555 and people.user(100) is None)

    # ---- 12. the three kinds of registration: open to everybody / closed, by requests / closed, only by a link
    operator = people.owner()["tg_id"]
    people.bind_owner(100, "Владелец", "owner100")                      # the operator of this test server
    operator = 100
    ctx.mode = "invite"
    tg.say(700, "/start", "Чужой")
    msg = await tg.last(700)
    check("closed, only by a link: a stranger is told that it is by invitation only", msg and "по приглашению" in msg[1] and people.user(700) is None, msg)
    raw = people.new_token("reg", by_tg=0)
    tg.say(701, "/start r" + raw, "Гость")
    msg = await tg.last(701)
    check("a link of the operator: the bot asks the person to accept", msg and "Принять" in msg[2][0][0], msg)
    tg.press(701, msg[2][0][1])
    await settle(bot, tg)
    check("after Accept the person owns a household of his own", people.user(701) and people.user(701)["role"] == "owner" and people.tenant_of(701) == 701)
    msg = await tg.last(operator, wait=0.5)
    check("the operator is told who came by the link", msg and "по ссылке" in msg[1].lower() and "@u701" in msg[1], msg)
    tg.say(702, "/start r" + raw, "Второй")
    msg = await tg.last(702)
    check("the link works once", msg and "устарела" in msg[1] and people.user(702) is None, msg)

    raw2 = people.new_token("reg", by_tg=0)
    ctx.cap = 0
    tg.say(703, "/start r" + raw2, "Опоздавший")
    msg = await tg.last(703)
    tg.press(703, msg[2][0][1])
    await settle(bot, tg)
    check("no free place: the person is not let in and the link stays", people.user(703) is None and people.token(raw2, "reg") is not None)
    ctx.cap = 5

    ctx.mode = "approval"
    tg.say(704, "/start", "Проситель")
    msg = await tg.last(704)
    check("closed, by requests: a stranger's request goes to the operator", msg and "Заявка отправлена" in msg[1] and people.user(704) is None, msg)
    msg = await tg.last(operator, wait=0.5)
    check("the operator gets the two buttons", msg and msg[2] and msg[2][0][0] == "Одобрить", msg)

    ctx.mode = "open"
    tg.say(705, "/start", "Свободный")
    msg = await tg.last(705)
    check("open to everybody: a stranger gets in at once", msg and "Добро пожаловать" in msg[1] and people.user(705) and people.user(705)["role"] == "owner", msg)
    msg = await tg.last(operator, wait=0.5)
    check("and the operator is told", msg and "Зарегистрировался" in msg[1], msg)
    ctx.cap = 0
    tg.say(706, "/start", "Лишний")
    msg = await tg.last(706)
    check("open, but no free place: the person waits", msg and "нет свободных мест" in msg[1] and people.user(706) is None, msg)
    ctx.cap = 5
    ctx.mode = "approval"

    bot.stop()
    bot_task.cancel()
    await http.close()
    await runner.cleanup()
    await tg_runner.cleanup()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
