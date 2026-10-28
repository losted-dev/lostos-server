"""The gateway: the front of a server that many people use (what Caddy talks to). Every person has a server process of his own (a tenant, tenants.py); this is the door.

  - login: one-time link, Telegram (OpenID Connect and the bot), registration of new people with the approval of the operator (auth.py, telegram_oidc.py, tgbot.py, accounts.py)
  - stations: the WebSocket of every speaker ends here (link.py); a speaker belongs to the person who typed its identifier, and works for HIS tenant
  - the panel: the files of the page are served here; every other request of a logged-in person is forwarded to HIS tenant with a header that says who he is (the tenant trusts nothing else)
  - the speaker's own requests (/t/<id>/tts|rem|plugins-dl/...) are forwarded to the tenant whose id is in the address
  - /internal/...: what the tenants ask of the gateway (their speaker's ports and keys), only from this machine and only with the key of that tenant
No person's key, setting or scenario is ever in this process: it holds the accounts, the stations and the one-time tokens, nothing else.
Run:  LOSTOS_DATA=/srv/lostos/data LOSTOS_PUBLIC_URL=https://host TTS_PORT=8765 LOSTOS_ENGINE=/run/lostos/engine.sock python gateway.py"""
import asyncio
import hmac
import os
import re
import sys
import time

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
WEB = os.path.join(HERE, "web")
DATA = os.environ.get("LOSTOS_DATA", HERE)
PUBLIC_URL = os.environ.get("LOSTOS_PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("TTS_PORT", "8765"))
ENGINE = os.environ.get("LOSTOS_ENGINE", "127.0.0.1:8771")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length", "cookie", "x-forwarded-for",
       "x-lostos-key", "x-lostos-tg", "x-lostos-role", "x-lostos-name"}
HEAVY_PATHS = ("/api/restore", "/api/v1/plugins/install")           # uploads of up to 14 MB: one at a time per person, so a person cannot make his process (or the gateway) swell
MAX_BODY, MAX_BODY_HEAVY, MAX_INFLIGHT = 1 * 1024 * 1024, 14 * 1024 * 1024, 16
PUBLIC_TENANT_PATH = re.compile(r"^/t/(\d+)(?:/s/([0-9a-f]{8,40}))?(/(?:tts|rem|plugins-dl)/[^?]*)$")


def log(text: str):
    print(time.strftime("%H:%M:%S ") + text, flush=True)


async def say_wav(text: str) -> bytes:
    """A wav of the voice of Alice (the cloud, no key needed) for the speaker: what it says when it is connected. b"" when the cloud or ffmpeg is not there."""
    import io
    import wave
    import numpy as np
    from alice_tts import synth
    rate = 48000
    pcm = await asyncio.get_running_loop().run_in_executor(None, synth, text, rate, "shitova")
    if pcm is None:
        return b""
    pcm = np.concatenate([np.zeros(int(0.25 * rate), np.int16), pcm, np.zeros(int(0.4 * rate), np.int16)])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class GwCtx:
    """What the Telegram bot may ask: the player and the state of the speaker of a person (asked from his tenant), the registration settings."""

    def __init__(self, people, link, mgr):
        self.acc, self.link, self.mgr = people, link, mgr

    async def status(self, tg: int) -> dict:
        tid = self.acc.tenant_of(tg)
        sid = self.link.default_station(tid)
        online = sid in self.link.tenant_online(tid)
        now = ""
        if online and self.mgr.running(tid, sid):
            try:
                st, d = await self.mgr.call(tid, sid, "GET", "/internal/status", timeout=6)
                now = (d or {}).get("now", "") if st == 200 else ""
            except Exception:
                pass
        return {"online": online, "now": now}

    async def media_command(self, tg: int, cmd: str, args: dict):
        tid = self.acc.tenant_of(tg)
        st, d = await self.mgr.call(tid, self.link.default_station(tid), "POST", "/internal/media", {"cmd": cmd, "args": args})
        if st != 200:
            raise ValueError((d or {}).get("error", "колонка не отвечает"))
        return (d or {}).get("result")

    def registration(self) -> str:
        return self.mgr.registration()

    def capacity_left(self) -> int:
        return self.mgr.capacity_left()


def build(people, mgr, link_obj_holder: dict, bot):
    """The application: everything the gateway itself answers, then the forwarding to the tenants."""
    import auth
    import account_api
    import link as link_mod
    import telegram_oidc

    app = web.Application(client_max_size=14 * 1024 * 1024)

    @web.middleware
    async def fresh(req, handler):
        resp = await handler(req)
        if req.path.startswith(("/static/", "/icons/")) or req.path in ("/", "/sw.js", "/manifest.webmanifest"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.middlewares.append(fresh)

    def file(path: str, ctype: str = None):
        async def h(req):
            return web.FileResponse(path, headers={"Content-Type": ctype} if ctype else None)
        return h

    app.router.add_get("/", file(os.path.join(WEB, "index.html")))
    app.router.add_get("/manifest.webmanifest", file(os.path.join(WEB, "manifest.webmanifest"), "application/manifest+json"))
    app.router.add_get("/sw.js", file(os.path.join(WEB, "sw.js"), "text/javascript"))
    app.router.add_static("/static", WEB, show_index=False, append_version=False)
    app.router.add_static("/icons", os.path.join(WEB, "icons"), show_index=False)

    async def hello(req):
        import update as upd
        import brand
        return web.json_response({"lostos": "server", "version": upd.version(), "lite": True, "multi": True, "registration": mgr.registration(), "tagline": brand.TAGLINE})

    app.router.add_get("/api/v1/hello", hello)
    lk = link_mod.install(app, log, None)
    link_obj_holder["link"] = lk
    lk.legacy_tenant = (people.admin() or {}).get("tg_id", 0)
    lk.tenant_of = people.tenant_of
    app["locked"] = lambda s: not lk.has_station(s.get("tenant"))                    # no connected speaker in the person's household yet: only the account and the connection
    auth.install(app, PUBLIC_URL, log, people, bot)
    telegram_oidc.install(app, people, bot, PUBLIC_URL, log)
    account_api.install(app, bot)
    import admin_web
    admin_web.install(app, people, mgr, lk, bot, log, os.environ.get("LOSTOS_ADMIN_HOST", ""), registry=link_obj_holder.get("registry"))      # the admin panel on its own host name

    # ---- what the tenants ask of the gateway (only from this machine, with the key of the tenant)
    def tenant_of_request(req):
        if req.headers.get("X-Forwarded-For") or req.remote not in ("127.0.0.1", "::1"):
            return None
        try:
            tid = int(req.query.get("tenant", "0"))
        except ValueError:
            return None
        sid = req.query.get("station", "")
        t = mgr.items.get((tid, sid))
        if t and hmac.compare_digest(req.headers.get("X-Lostos-Key", ""), t.key):
            return tid, sid
        return None

    async def internal_station(req):
        who = tenant_of_request(req)
        if who is None:
            return web.Response(status=403)
        return web.json_response(lk.tenant_view(who[0], who[1]))

    app.router.add_get("/internal/station", internal_station)

    # ---- everything else goes to the tenant of the person
    async def proxy(req: web.Request):
        s = req.get("session")
        m = PUBLIC_TENANT_PATH.match(req.path)
        extra = {}
        if m:                                                                    # the speaker fetches an answer, a reminder, a plugin of the station whose address it is
            tid, sid, path = int(m.group(1)), m.group(2) or "", m.group(3)
            if not mgr.running(tid, sid):
                return web.Response(status=404)
        elif s and not req.path.startswith(("/t/", "/internal/", "/auth/", "/link/")):
            tid, path = s["tenant"], req.path
            want = req.headers.get("X-Lostos-Station", "")                      # the station the person looks at (the panel remembers the choice); only one of his own counts
            sid = want if want and lk.owns(tid, want) else lk.default_station(tid)
            u = people.user(s["tg_id"]) if s["tg_id"] else None
            extra = {"X-Lostos-Tg": str(s["tg_id"]), "X-Lostos-Role": s["role"], "X-Lostos-Name": (u or {}).get("name", "")}
        else:
            return web.Response(status=404)
        import tenants as tmod
        try:
            t = await mgr.ensure(tid, sid)
        except tmod.Full:
            return web.json_response({"ok": False, "error": "на сервере нет места для ещё одного человека"}, status=503)
        except Exception as e:
            log(f"tenant {tid}: could not start: {e!r}")
            return web.json_response({"ok": False, "error": "ваш сервер не запустился, попробуйте через минуту"}, status=503)
        heavy = req.path in HEAVY_PATHS
        if req.method in ("POST", "PUT", "PATCH", "DELETE") and req.can_read_body:
            if req.content_length is None:
                return web.json_response({"ok": False, "error": "нужна длина запроса"}, status=411)
            if req.content_length > (MAX_BODY_HEAVY if heavy else MAX_BODY):
                return web.json_response({"ok": False, "error": "запрос слишком большой"}, status=413)
        gate = mgr.gates.setdefault((tid, sid, heavy), asyncio.Semaphore(1 if heavy else MAX_INFLIGHT))
        if gate.locked():                                                    # too many at once for one person: refused at once, nothing is queued in memory
            return web.json_response({"ok": False, "error": "слишком много запросов сразу, подождите секунду"}, status=429)
        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
        headers.update(extra)
        headers["X-Lostos-Key"] = t.key
        url = f"http://127.0.0.1:{t.port}{path}" + (("?" + req.query_string) if req.query_string else "")
        try:
            async with gate, mgr.http.request(req.method, url, data=req.content if req.can_read_body else None, headers=headers, allow_redirects=False,
                                              timeout=aiohttp.ClientTimeout(total=None, sock_read=45) if path.endswith("/stream") else aiohttp.ClientTimeout(total=180)) as r:   # a stream lives as long as it gets its keep-alive lines (every 15 s)
                resp = web.StreamResponse(status=r.status)
                for k, v in r.headers.items():
                    if k.lower() not in HOP and k.lower() != "content-encoding":
                        resp.headers[k] = v
                await resp.prepare(req)
                async for chunk in r.content.iter_chunked(65536):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log(f"tenant {tid}: {req.method} {req.path}: {e!r}")
            return web.json_response({"ok": False, "error": "ваш сервер не отвечает"}, status=502)

    app.router.add_route("*", "/{tail:.*}", proxy)
    return app, lk


async def main():
    import accounts as acc_mod
    import tenants as tmod
    import tgbot

    os.makedirs(DATA, exist_ok=True)
    people = acc_mod.Accounts()
    mgr = tmod.Tenants(DATA, PUBLIC_URL, f"http://127.0.0.1:{PORT}", ENGINE, log)
    mgr.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=200))
    holder: dict = {}
    import registry as registry_mod
    holder["registry"] = registry_mod.Registry(DATA, log)                          # the signed list of plugins, for the numbers of the admin panel
    token = os.environ.get("LOSTOS_TG_TOKEN", "").strip()
    bot = None
    if token:
        ctx = GwCtx(people, None, mgr)
        bot = tgbot.Bot(token, people, log, ctx, os.environ.get("LOSTOS_TG_API", tgbot.API), PUBLIC_URL)
    app, lk = build(people, mgr, holder, bot)
    if bot:
        bot.ctx.link = lk

    def on_station(kind: str, sid: str, name: str = "", extra: str = ""):
        if kind in ("online", "confirmed"):                                          # a speaker came: its server process is there for it
            r = lk.stations.get(sid, {})
            if r.get("confirmed"):
                asyncio.get_event_loop().create_task(_ensure(lk.tenant_of_station(r), sid))

    async def _ensure(tid: int, sid: str):
        try:
            await mgr.ensure(tid, sid)
            if sid and (tid, "") in mgr.items:                                       # the house has a station process now: the process that served the wizard alone is not needed
                await mgr.stop(tid, "", "stopped (the house has a station now)")
        except Exception as e:
            log(f"tenant {tid}: {e!r}")

    lk.listeners.append(on_station)
    if bot:
        lk.listeners.append(bot.on_link_event)
    lk.say = say_wav                                                                 # the lines the speaker says while it is being connected: Alice's voice from the cloud
    mgr.on_restart = lambda tid, why: people.log(tid, "event", f"ваш сервер перезапущен: {why}"[:190])
    mgr.primary_of = lk.default_station
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    log(f"gateway: http://127.0.0.1:{PORT}, public address {PUBLIC_URL}, data {DATA}")
    if bot:
        asyncio.get_running_loop().create_task(bot.run())
    asyncio.get_running_loop().create_task(mgr.watch(lambda tid, sid: bool(sid) and sid in lk.tenant_online(tid)))
    for sid, r in list(lk.stations.items()):                                           # the people who have a speaker get their server process at once
        if r.get("confirmed"):
            await _ensure(lk.tenant_of_station(r), sid)
            await asyncio.sleep(0.5)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
