"""Login through Telegram as an OpenID Connect provider (https://core.telegram.org/bots/telegram-login): authorization code flow with PKCE.

  GET /auth/telegram/start            the browser leaves for oauth.telegram.org (a state, a PKCE verifier and a cookie that ties the answer to THIS browser)
  GET /auth/telegram/start?bind=1     the same for the owner who ties his Telegram account to the server (needs his session)
  GET /auth/telegram/callback         Telegram sends the browser back with a code; the server swaps it for an id_token at the token endpoint (Basic auth with the
                                      client id and secret), checks the signature against Telegram's keys, `iss`, `aud` (= the client id) and the time, takes the
                                      Telegram id from the `id` claim and opens a session for a known person (accounts.py)
The client id and the secret of the bot (BotFather, Login Widget) are in the environment of the service (LOSTOS_TG_CLIENT_ID, LOSTOS_TG_CLIENT_SECRET), not in the vault:
the vault opens only while a speaker is connected, and a person must be able to come in when the speaker is off.
The address to register in BotFather (Allowed URLs): <public url>/auth/telegram/callback.
Only RS256 and ES256 are accepted; a token without a valid signature, a wrong audience or an expired one is refused. The state is used once and lives 10 minutes."""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse

import aiohttp
from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

BASE = os.environ.get("LOSTOS_TG_OIDC", "https://oauth.telegram.org").rstrip("/")
COOKIE = "lostos_oidc"
STATE_SECONDS = 600


def b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u_enc(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def verify_jwt(token: str, jwks: dict) -> dict:
    """The claims of a JWT whose signature is valid for one of the keys. Raises ValueError."""
    try:
        h, p, s = token.split(".")
        header, claims, sig = json.loads(b64u(h)), json.loads(b64u(p)), b64u(s)
    except (ValueError, TypeError):
        raise ValueError("не токен")
    alg = header.get("alg")
    if alg not in ("RS256", "ES256"):
        raise ValueError(f"алгоритм {alg} не принимается")
    keys = [k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid") or not header.get("kid")]
    data = (h + "." + p).encode()
    for k in keys:
        try:
            if alg == "RS256" and k.get("kty") == "RSA":
                rsa.RSAPublicNumbers(int.from_bytes(b64u(k["e"]), "big"), int.from_bytes(b64u(k["n"]), "big")).public_key().verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
                return claims
            if alg == "ES256" and k.get("kty") == "EC" and k.get("crv") == "P-256" and len(sig) == 64:
                pub = ec.EllipticCurvePublicNumbers(int.from_bytes(b64u(k["x"]), "big"), int.from_bytes(b64u(k["y"]), "big"), ec.SECP256R1()).public_key()
                pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), data, ec.ECDSA(hashes.SHA256()))
                return claims
        except (InvalidSignature, KeyError, ValueError):
            continue
    raise ValueError("подпись не подходит")


def check_claims(c: dict, client_id: str, now: float = None) -> int:
    """The Telegram id of the person from verified claims. Raises ValueError."""
    now = now or time.time()
    if c.get("iss") != BASE and c.get("iss") != "https://oauth.telegram.org":
        raise ValueError("чужой издатель токена")
    aud = c.get("aud")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("токен выдан не для этого бота")
    if float(c.get("exp", 0)) < now - 30:
        raise ValueError("токен просрочен")
    if float(c.get("iat", 0)) > now + 300:
        raise ValueError("токен из будущего")
    try:
        tg = int(c.get("id"))
    except (TypeError, ValueError):
        raise ValueError("в токене нет номера Telegram (нужна область profile)")
    if tg <= 0:
        raise ValueError("странный номер Telegram")
    return tg


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>lostOS</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#000;color:#fff;font:17px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:420px;padding:24px;text-align:center}p{color:rgba(235,235,245,.62)}a{color:#8b5cf6}</style></head><body><main><h2>%s</h2><p>%s</p><p><a href="/">Вернуться</a></p></main></body></html>"""


def page(title: str, text: str, status: int = 400) -> web.Response:
    import html
    return web.Response(text=PAGE % (html.escape(title), html.escape(text)), content_type="text/html", charset="utf-8", status=status)


def install(app: web.Application, accounts, bot, public_url: str, log=print):
    from auth import COOKIE as SESSION_COOKIE, _limited, client_ip, _set_cookie
    client_id, secret = os.environ.get("LOSTOS_TG_CLIENT_ID", "").strip(), os.environ.get("LOSTOS_TG_CLIENT_SECRET", "").strip()
    if not (client_id and secret):
        return False
    redirect = public_url.rstrip("/") + "/auth/telegram/callback"
    jwks_cache = {"t": 0.0, "keys": {}}
    app["oidc"] = True

    async def jwks(force: bool = False) -> dict:
        if force or time.time() - jwks_cache["t"] > 3600 or not jwks_cache["keys"]:
            async with aiohttp.ClientSession() as s, s.get(BASE + "/.well-known/jwks.json", timeout=aiohttp.ClientTimeout(total=15)) as r:
                jwks_cache["keys"], jwks_cache["t"] = await r.json(content_type=None), time.time()
        return jwks_cache["keys"]

    async def start(req: web.Request):
        if _limited("oidc:" + client_ip(req), 12):
            return page("Слишком много попыток", "Подождите минуту", 429)
        bind = req.query.get("bind") == "1"
        if bind:
            s = accounts.session(req.cookies.get(SESSION_COOKIE, ""))
            if not s or s["role"] != "owner":
                return page("Нужен вход владельца", "Привязать Telegram может только владелец, уже вошедший на сервер", 403)
        verifier = secrets.token_urlsafe(48)
        raw = accounts.new_token("oidc", req.headers.get("User-Agent", ""), client_ip(req), by_tg=1 if bind else 0, code=verifier)
        if not raw:
            return page("Сервер занят", "Попробуйте через минуту", 429)
        q = urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": "openid profile", "state": raw,
                                    "code_challenge": b64u_enc(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
        resp = web.HTTPFound(f"{BASE}/auth?{q}")
        resp.set_cookie(COOKIE, raw, max_age=STATE_SECONDS, httponly=True, secure=True, samesite="Lax", path="/auth/telegram")
        raise resp

    async def callback(req: web.Request):
        state, code = req.query.get("state", ""), req.query.get("code", "")
        t = accounts.token(state, "oidc") if state else None
        cookie = req.cookies.get(COOKIE, "")
        if not t or not cookie or not secrets.compare_digest(cookie, state):                 # not started in this browser, or used already, or too old
            return page("Вход не получился", "Начните вход заново на странице сервера")
        accounts.use_token(state)                                                         # the state is spent whatever happens next
        if req.query.get("error") or not code:
            return page("Вход отклонён", "Telegram не подтвердил вход")
        verifier, bind = t["code"], bool(t["by_tg"])
        try:
            auth_hdr = "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()
            async with aiohttp.ClientSession() as s, s.post(BASE + "/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": client_id,
                                                                                     "code_verifier": verifier}, headers={"Authorization": auth_hdr}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                tok = await r.json(content_type=None)
            if "id_token" not in tok:
                raise ValueError("Telegram не выдал токен")
            try:
                claims = verify_jwt(tok["id_token"], await jwks())
            except ValueError:
                claims = verify_jwt(tok["id_token"], await jwks(force=True))               # the keys may have been changed
            tg = check_claims(claims, client_id)
        except (ValueError, aiohttp.ClientError, KeyError) as e:
            log(f"telegram oidc: refused: {e}")
            return page("Вход не получился", "Не удалось проверить ответ Telegram.")
        name = str(claims.get("name") or claims.get("preferred_username") or tg)[:60]
        username = str(claims.get("preferred_username") or "")[:40]
        photo = str(claims.get("picture") or "")
        photo = photo if photo.startswith("https://") and len(photo) < 300 else ""
        if bind:                                                                           # the owner ties this Telegram account to the server
            s = accounts.session(req.cookies.get(SESSION_COOKIE, ""))
            if not s or s["role"] != "owner":
                return page("Нужен вход владельца", "Сессия владельца не найдена", 403)
            accounts.bind_owner(tg, name, username, photo)
            log(f"telegram oidc: the owner is now {tg}")
            raise web.HTTPFound("/")
        if not accounts.role_of(tg):
            accounts.log(tg, "refused", "вход через Telegram: нет доступа")
            st = bot.register_request(tg, name, username) if bot else "closed"
            if st in ("new", "pending"):
                return page("Заявка отправлена", "Владелец сервера получил вашу заявку. Когда её одобрят, бот lostOS напишет вам в Telegram, и сюда можно будет войти", 202)
            return page("Нет доступа", "Этот аккаунт Telegram не знаком серверу, а регистрация закрыта или заявка отклонена", 403)
        accounts.touch(tg, name, username, photo)
        resp = web.HTTPFound("/")
        _set_cookie(resp, accounts.new_session(tg, req.headers.get("User-Agent", ""), client_ip(req)))
        resp.del_cookie(COOKIE, path="/auth/telegram")
        accounts.log(tg, "login", "через Telegram OIDC")
        if bot:
            bot.after_login(tg, req.headers.get("User-Agent", ""), client_ip(req))
        raise resp

    app.router.add_get("/auth/telegram/start", start)
    app.router.add_get("/auth/telegram/callback", callback)
    log(f"telegram oidc: on, register {redirect} in BotFather")
    return True
