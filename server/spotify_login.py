"""Spotify login on a server that is not at home: Authorization Code with PKCE (no client secret), through the panel.

The owner opens Settings > Keys, pushes "Log in with Spotify", signs in at Spotify and comes back to <server>/spotify/callback, where the code is exchanged
for the tokens. The tokens go into the encrypted vault (vault.py), never into a plain file. The address /spotify/callback must be added by the owner
to the Redirect URIs of his Spotify app (developer.spotify.com/dashboard); the Client ID is saved in Settings > Keys (SPOTIFY_CLIENT_ID)"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import aiohttp
from aiohttp import web

import spotify_auth
import spotify_client

TOKEN_NAME = "spotify:token"
PENDING: dict = {}                                  # state -> {"verifier": ..., "t": ...}


def install(app: web.Application, vault, public_url: str, spot, log=print):
    redirect = public_url + "/spotify/callback"
    spotify_client.STORE = (lambda: vault.get(TOKEN_NAME), lambda raw: vault.set(TOKEN_NAME, raw))

    async def status(req):
        return web.json_response({"client_id": bool(spotify_client._env("SPOTIFY_CLIENT_ID")), "logged_in": bool(vault.get(TOKEN_NAME)),
                                  "locked": vault.locked, "redirect": redirect})

    async def login(req):
        cid = spotify_client._env("SPOTIFY_CLIENT_ID")
        if vault.locked:
            return web.json_response({"ok": False, "error": "хранилище закрыто: колонка не на связи"}, status=409)
        if not cid:
            return web.json_response({"ok": False, "error": "сначала сохраните Client ID Spotify в этом разделе"}, status=400)
        for k in [k for k, v in PENDING.items() if time.time() - v["t"] > 600]:
            PENDING.pop(k)
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        PENDING[state] = {"verifier": verifier, "t": time.time()}
        url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": cid, "response_type": "code", "redirect_uri": redirect, "scope": spotify_auth.SCOPES,
            "state": state, "code_challenge_method": "S256", "code_challenge": challenge})
        return web.json_response({"ok": True, "url": url})

    async def callback(req):
        q = req.query
        page = lambda text, status=200: web.Response(status=status, content_type="text/html", charset="utf-8", text=(
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            + ("<meta http-equiv='refresh' content='1.4;url=/'>" if status == 200 else "") +             # a good login goes straight back to the panel (to the step it left)
            "<body style='background:#000;color:#fff;font:18px system-ui;display:grid;place-items:center;min-height:100vh;margin:0;text-align:center'>"
            f"<div><p>{text}</p><p><a style='color:#8b5cf6' href='/'>Вернуться в панель</a></p></div></body>"))
        pend = PENDING.pop(q.get("state", ""), None)
        if "error" in q:
            return page("Spotify не дал доступ: " + q["error"].replace("<", ""), 400)
        if not pend or "code" not in q:
            return page("Вход не удался: ссылка устарела. Начните заново из панели", 400)
        if vault.locked:
            return page("Хранилище закрыто (колонка не на связи). Попробуйте ещё раз", 409)
        data = {"grant_type": "authorization_code", "code": q["code"], "redirect_uri": redirect,
                "client_id": spotify_client._env("SPOTIFY_CLIENT_ID"), "code_verifier": pend["verifier"]}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post("https://accounts.spotify.com/api/token", data=data) as r:
                tok = await r.json()
                if r.status != 200:
                    log(f"spotify login: {r.status} {tok.get('error')}")
                    return page("Spotify отклонил вход: " + str(tok.get("error_description") or tok.get("error"))[:120].replace("<", ""), 400)
        tok["obtained_at"] = int(time.time())
        vault.set(TOKEN_NAME, json.dumps(tok))
        spot.forget()
        log("spotify: logged in, the token is in the vault")
        return page("Готово: Spotify подключён")

    async def logout(req):
        vault.delete(TOKEN_NAME)
        spot.forget()
        return web.json_response({"ok": True})

    app.router.add_get("/api/v1/spotify", status)
    app.router.add_post("/api/v1/spotify/login", login)
    app.router.add_post("/api/v1/spotify/logout", logout)
    app.router.add_get("/spotify/callback", callback)
