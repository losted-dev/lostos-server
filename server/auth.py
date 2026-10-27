"""Access to the panel of a server that is open to the internet (LOSTOS_AUTH=1; a server on the home network does not need it)

Two ways in, one kind of session:
  1. The one-time link (https://<server>/auth/setup?token=...): the server writes it to data/setup-link.txt when it has no owner, and the owner
     asks for a new one with `lostos-link`. It works once and for 24 hours and opens a session of the OWNER. It is how the owner comes in the
     first time and how he comes back when the Telegram account is lost.
  2. Telegram (needs the bot of tgbot.py): the login page asks for a request, shows a two digit number and a link to the bot; the bot asks the
     person in Telegram to tap the SAME number among three (so a link that somebody else sent cannot log his browser in) and the page gets
     a session. Only people the server knows can come in: the owner and the members he invited.
A session is a random value in the cookie `lostos_session` (HttpOnly, Secure, SameSite=Lax); the server keeps only its SHA-256 (accounts.py)

What stays open without a session: the login page, static files of the panel shell, and what the SPEAKER fetches by itself
(answers /tts/<random>.wav, reminders /rem/<id>.wav, plugin programs /plugins-dl/...) and the link of a station (/link/ws proves itself with the station's key)
Everything else answers 401. A member (not the owner) cannot touch keys, stations, plugins, backups or the console (MEMBER_DENIED)"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

from aiohttp import web

import accounts as acc

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("LOSTOS_DATA", HERE)
FILE = os.path.join(DATA, "auth.json")
SETUP_HOURS = 24
COOKIE = "lostos_session"
PUBLIC_PREFIXES = ("/auth/", "/link/", "/tts/", "/rem/", "/plugins-dl/", "/static/", "/icons/", "/t/", "/internal/")      # /t/<id>/tts|rem|plugins-dl: what the speaker fetches from the tenant of its owner; /internal/: tenants and the gateway, checked by a key
PUBLIC_PATHS = {"/sw.js", "/manifest.webmanifest", "/favicon.ico", "/api/v1/hello"}
# what only the owner may do: a member gets 403
MEMBER_DENIED = ("/api/v1/setup", "/api/v1/ai", "/api/v1/secrets", "/api/v1/spotify", "/api/v1/console", "/api/v1/account/owner", "/api/backup", "/api/restore", "/spotify/")
MEMBER_DENIED_WRITE = ("/api/v1/plugins", "/api/v1/registry", "/api/v1/speaker", "/api/v1/widgets")
# While no speaker is linked, only these work: the account, the station list (to confirm the first one) and the state of the wizard
OPEN_WHILE_LOCKED = ("/api/v1/account", "/api/v1/stations", "/api/v1/setup", "/admin")

LOGIN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>lostOS: вход</title><style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#000;color:#fff;font:17px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:420px;padding:24px;text-align:center}h1{font-weight:300;letter-spacing:-.03em;font-size:34px;margin:0 0 6px}h1 b{font-weight:800;color:#8b5cf6}
p{color:rgba(235,235,245,.62)}code{background:rgba(120,120,128,.28);padding:2px 7px;border-radius:7px;font-size:14px}
a.btn,button{display:inline-block;margin:14px 0 4px;padding:13px 26px;border:0;border-radius:26px;background:#2aabee;color:#fff;font:600 17px -apple-system,system-ui,sans-serif;text-decoration:none;cursor:pointer}
.num{font-size:64px;font-weight:700;letter-spacing:.04em;margin:6px 0}small{color:rgba(235,235,245,.4)}[hidden]{display:none!important}</style></head><body><main>
<img src="/icons/logo-mark.svg" alt="" width="84" height="84" style="border-radius:22.5%"><h1><img src="/icons/logo-wordmark.svg" alt="lostOS" width="150" height="50"></h1>
<div id="start"><p>Нужен вход</p><a class="btn" id="oidc" href="/auth/telegram/start" hidden>Войти через Telegram</a><br><button id="go" hidden>Войти через Telegram</button>
<p id="hint">Или откройте одноразовую ссылку входа, которую выдал сервер. Если её нет или она устарела, владелец получает новую на сервере командой <code>lostos-link</code>.</p></div>
<div id="wait" hidden><p>Откройте Telegram и подтвердите вход. В боте нажмите число:</p><div class="num" id="num"></div>
<a class="btn" id="open" target="_blank" rel="noopener">Открыть бота</a><p><small id="msg">Жду подтверждения…</small></p></div>
<script>
const $=(i)=>document.getElementById(i);let tok='',timer=0;
fetch('/auth/whoami').then(r=>r.json()).then(d=>{
  if(d.oidc){$('oidc').hidden=false;$('hint').hidden=true;if(d.telegram){$('go').hidden=false;$('go').textContent='Войти через бота';$('go').style.background='transparent';$('go').style.color='#8b5cf6';$('go').style.fontSize='15px'}}
  else if(d.telegram){$('go').hidden=false;$('hint').hidden=true;}}).catch(()=>{});
$('go').onclick=async()=>{
  const r=await fetch('/auth/tg/start',{method:'POST'});const d=await r.json().catch(()=>({}));
  if(!d.token){$('hint').hidden=false;$('hint').textContent=d.error||'Не получилось, попробуйте позже';return}
  tok=d.token;$('num').textContent=d.code;$('open').href=d.url;$('start').hidden=true;$('wait').hidden=false;
  timer=setInterval(async()=>{
    const p=await fetch('/auth/tg/poll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})}).then(r=>r.json()).catch(()=>({}));
    if(p.state==='ok'){clearInterval(timer);location.replace('/')}
    else if(p.state&&p.state!=='wait'){clearInterval(timer);$('msg').textContent=p.state==='denied'?'Вход отклонён':'Время вышло. Обновите страницу'}
  },2000);
};
</script></main></body></html>"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _load() -> dict:
    try:
        return json.load(open(FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict):
    os.makedirs(DATA, exist_ok=True)
    tmp = FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def new_setup_token() -> str:
    """A new one-time link token (the old one stops working). Returns the token itself, only its hash is stored."""
    token = secrets.token_urlsafe(32)
    state = _load()
    state["setup"] = {"hash": _hash(token), "expires": time.time() + SETUP_HOURS * 3600}
    _save(state)
    return token


def ensure_setup_link(public_url: str, log=print):
    """Starts of the server: no owner and no valid link yet means a link is made and written where only the server's owner reads."""
    state = _load()
    setup = state.get("setup") or {}
    if state.get("owner") or setup.get("expires", 0) > time.time():
        return
    token = new_setup_token()
    link = f"{public_url}/auth/setup?token={token}"
    path = os.path.join(DATA, "setup-link.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(link + "\n")
    log(f"auth: no owner yet. One-time login link (24 h): {path}")


def client_ip(req: web.Request) -> str:
    return req.headers.get("X-Forwarded-For", req.remote or "").split(",")[0].strip()          # behind Caddy


_SECURE = not os.environ.get("LOSTOS_PUBLIC_URL", "").startswith("http://")      # a home server on plain http (LAN) cannot set a Secure cookie: the browser would drop it


def _set_cookie(resp: web.Response, token: str):
    resp.set_cookie(COOKIE, token, max_age=acc.SESSION_DAYS * 86400, httponly=True, secure=_SECURE, samesite="Lax", path="/")


_failures = {"n": 0, "until": 0.0}
_rate: dict = {}


def _limited(key: str, per_minute: int) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(key, []) if t > now - 60]
    _rate[key] = hits + [now]
    return len(hits) >= per_minute


def install(app: web.Application, public_url: str, log=print, accounts: "acc.Accounts" = None, bot=None):
    """bot: the Telegram bot (tgbot.Bot) or None; it tells the username for the link and is told about logins."""
    accounts = accounts or acc.Accounts()
    accounts.import_old(FILE)
    ensure_setup_link(public_url, log)
    app["accounts"] = accounts

    async def setup(req: web.Request):
        if time.time() < _failures["until"]:
            return web.Response(status=429, text="Слишком много попыток, подождите минуту")
        state = _load()
        setup_state = state.get("setup") or {}
        given = req.query.get("token", "")
        ok = bool(given) and setup_state.get("expires", 0) > time.time() and hmac.compare_digest(_hash(given), setup_state.get("hash", ""))
        if not ok:
            _failures["n"] += 1
            if _failures["n"] >= 5:
                _failures.update(n=0, until=time.time() + 60)
            return web.Response(status=403, text="Ссылка не подходит или устарела", content_type="text/plain", charset="utf-8")
        _failures["n"] = 0
        state.pop("setup", None)                              # one time only
        if not state.get("owner"):
            state["owner"] = {"created": int(time.time())}
        _save(state)
        try:
            os.remove(os.path.join(DATA, "setup-link.txt"))
        except OSError:
            pass
        owner = accounts.owner()
        resp = web.HTTPFound("/")
        _set_cookie(resp, accounts.new_session(owner["tg_id"] if owner else 0, req.headers.get("User-Agent", ""), client_ip(req)))
        accounts.log(owner["tg_id"] if owner else 0, "login", "по одноразовой ссылке")
        log("auth: a session was opened with the one-time link")
        raise resp

    async def logout(req: web.Request):
        accounts.drop_session(req.cookies.get(COOKIE, ""))
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def whoami(req: web.Request):
        s = accounts.session(req.cookies.get(COOKIE, ""))
        u = accounts.user(s["tg_id"]) if s and s["tg_id"] else None
        return web.json_response({"authed": bool(s), "role": s["role"] if s else "", "name": (u or {}).get("name", ""), "username": (u or {}).get("username", ""),
                                  "owner": bool(accounts.owner() or _load().get("owner")), "telegram": bool(bot and bot.username), "oidc": bool(req.app.get("oidc")) and req.host.split(":")[0].lower() != os.environ.get("LOSTOS_ADMIN_HOST", "-").lower(), "bound": bool(accounts.owner())})

    # ---- Telegram: the browser asks, the bot confirms
    async def tg_start(req: web.Request):
        if not (bot and bot.username):
            return web.json_response({"error": "вход через Telegram не настроен"}, status=503)
        ip = client_ip(req)
        if _limited("tgstart:" + ip, 6):
            return web.json_response({"error": "слишком много попыток, подождите минуту"}, status=429)
        code = f"{secrets.randbelow(90) + 10}"                                     # two digits that the person has to pick in Telegram
        raw = accounts.new_token("login", req.headers.get("User-Agent", ""), ip, code=code)
        if not raw:
            return web.json_response({"error": "сервер занят, попробуйте через минуту"}, status=429)
        return web.json_response({"token": raw, "code": code, "url": f"https://t.me/{bot.username}?start=l{raw}"})

    async def tg_poll(req: web.Request):
        try:
            raw = str((await req.json()).get("token", ""))
        except ValueError:
            return web.json_response({"state": "expired"}, status=400)
        if _limited("tgpoll:" + client_ip(req), 90):
            return web.json_response({"state": "wait"}, status=429)
        t = accounts.token(raw, "login")
        if not t:
            return web.json_response({"state": "expired"})
        if t["state"] == "wait":
            return web.json_response({"state": "wait"})
        accounts.use_token(raw)                                                    # whatever the answer, the token is spent now
        if t["state"] != "ok" or not accounts.role_of(t["tg_id"]):
            return web.json_response({"state": "denied"})
        resp = web.json_response({"state": "ok"})
        _set_cookie(resp, accounts.new_session(t["tg_id"], t["agent"], t["ip"]))
        accounts.log(t["tg_id"], "login", "через Telegram")
        if bot:
            bot.after_login(t["tg_id"], t["agent"], t["ip"])
        return resp

    @web.middleware
    async def gate(req: web.Request, handler):
        path = req.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await handler(req)
        s = accounts.session(req.cookies.get(COOKIE, ""))
        if s:
            origin = req.headers.get("Origin")
            if req.method not in ("GET", "HEAD", "OPTIONS") and origin and origin.split("://", 1)[-1] != req.host:
                return web.json_response({"ok": False, "error": "чужой источник запроса"}, status=403)       # a form of another site
            if s["role"] != "owner" and (path.startswith(MEMBER_DENIED) or (req.method != "GET" and path.startswith(MEMBER_DENIED_WRITE))):
                return web.json_response({"ok": False, "error": "это может только владелец"}, status=403)
            locked = req.app.get("locked")
            is_locked = bool(locked and locked(s))                      # no speaker connected for THIS person's household yet
            if is_locked and path != "/" and not path.startswith(OPEN_WHILE_LOCKED):
                return web.json_response({"ok": False, "locked": True, "error": "сначала подключите колонку"}, status=423)
            if is_locked and path.startswith("/api/v1/setup") and req.method != "GET":
                return web.json_response({"ok": False, "locked": True, "error": "сначала подключите колонку"}, status=423)
            req["session"] = s
            return await handler(req)
        if req.method == "GET" and (path == "/" or "text/html" in req.headers.get("Accept", "")):
            return web.Response(text=LOGIN_PAGE, content_type="text/html", charset="utf-8", status=401)
        return web.json_response({"ok": False, "error": "нужен вход"}, status=401)

    app.middlewares.insert(0, gate)
    app.router.add_get("/auth/setup", setup)
    app.router.add_post("/auth/logout", logout)
    app.router.add_get("/auth/whoami", whoami)
    app.router.add_post("/auth/tg/start", tg_start)
    app.router.add_post("/auth/tg/poll", tg_poll)
    return accounts


if __name__ == "__main__":
    # python auth.py new-link [public-url]   -> prints a fresh one-time login link (run on the server)
    if len(sys.argv) >= 2 and sys.argv[1] == "new-link":
        url = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("LOSTOS_PUBLIC_URL", "")).rstrip("/")
        print(f"{url}/auth/setup?token={new_setup_token()}")
    else:
        print(__doc__)
