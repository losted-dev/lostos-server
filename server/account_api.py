"""The account part of the panel API (a server open to the internet only; the pages are in Settings, "Аккаунт и доступ")

  GET    /api/v1/account                  who I am, the bot, the people and the sessions (the owner sees all, a member only his own)
  POST   /api/v1/account/bind             owner: a link to the bot that ties his Telegram account to the server
  POST   /api/v1/account/invite           owner: a one-time link that makes whoever opens it (and agrees in Telegram) a member
  DELETE /api/v1/account/users/{tg_id}    owner: remove a member (his sessions end with it)
  DELETE /api/v1/account/sessions/{id}    end a session (the owner any, a member his own)
  POST   /api/v1/account/logout-all       end all my sessions
Who may do what is decided here and in auth.py (a member cannot reach the owner's pages at all)"""
from aiohttp import web


def install_local(app: web.Application, remote_url: str):
    """A server on the home network has no accounts (nobody comes in from outside); the page still answers, so the panel can say so and point to the server on the internet."""
    async def info(req):
        return web.json_response({"local": True, "remote": remote_url, "me": {"tg_id": 0, "role": "owner", "name": "Домашняя сеть", "username": ""}})

    app.router.add_get("/api/v1/account", info)


def install(app: web.Application, bot):
    acc = app["accounts"]

    def me(req):
        s = req.get("session") or {}
        return s.get("tg_id", 0), s.get("role", "")

    def household(req):
        """The household of the person: what the owner of it manages (his members, their sessions), and nothing of other households."""
        return (req.get("session") or {}).get("tenant", acc.tenant_of(me(req)[0]))

    def need_owner(req):
        if me(req)[1] != "owner":
            raise web.HTTPForbidden(text='{"ok": false, "error": "это может только владелец"}', content_type="application/json")

    async def info(req):
        tg, role = me(req)
        u = acc.user(tg) if tg else None
        out = {"me": {"tg_id": tg, "role": role, "name": (u or {}).get("name", "владелец (по ссылке)" if role == "owner" else ""), "username": (u or {}).get("username", ""), "photo": (u or {}).get("photo") or ""},
               "bot": {"username": bot.username if bot else "", "configured": bool(bot)}, "oidc": bool(req.app.get("oidc")),
               "locked": bool(req.app.get("locked") and req.app["locked"](req.get("session") or {})), "bound": bool(acc.owner()),
               "sessions": acc.sessions_of(tenant=household(req)) if role == "owner" else acc.sessions_of(tg)}
        for sess in out["sessions"]:
            sess["mine"] = sess["tg_id"] == tg
            sess["who"] = (acc.user(sess["tg_id"]) or {}).get("name", "владелец (по ссылке)")
        if role == "owner":
            out["users"] = acc.users(household(req))
        return web.json_response(out)

    async def bind(req):
        need_owner(req)
        if not (bot and bot.username):
            return web.json_response({"ok": False, "error": "бот Telegram не настроен на сервере"}, status=503)
        raw = acc.new_token("bind", by_tg=me(req)[0])
        if not raw:
            return web.json_response({"ok": False, "error": "слишком много открытых запросов"}, status=429)
        return web.json_response({"ok": True, "url": f"https://t.me/{bot.username}?start=b{raw}"})

    async def invite(req):
        need_owner(req)
        if not (bot and bot.username):
            return web.json_response({"ok": False, "error": "бот Telegram не настроен на сервере"}, status=503)
        if not acc.owner():
            return web.json_response({"ok": False, "error": "сначала привяжите свой Telegram"}, status=409)
        raw = acc.new_token("invite", by_tg=me(req)[0])
        if not raw:
            return web.json_response({"ok": False, "error": "слишком много открытых запросов"}, status=429)
        return web.json_response({"ok": True, "url": f"https://t.me/{bot.username}?start=i{raw}", "hours": 24})

    async def remove_user(req):
        need_owner(req)
        try:
            ok = acc.remove_user(int(req.match_info["tg"]), tenant=household(req))
        except ValueError:
            ok = False
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def end_session(req):
        tg, role = me(req)
        ok = acc.revoke(req.match_info["sid"], tenant=household(req)) if role == "owner" else acc.revoke(req.match_info["sid"], tg)
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def logout_all(req):
        tg, _ = me(req)
        keep = (req.get("session") or {}).get("hash", "")
        return web.json_response({"ok": True, "ended": acc.revoke_all(tg, keep)})

    app.router.add_get("/api/v1/account", info)
    app.router.add_post("/api/v1/account/bind", bind)
    app.router.add_post("/api/v1/account/invite", invite)
    app.router.add_delete("/api/v1/account/users/{tg}", remove_user)
    app.router.add_delete("/api/v1/account/sessions/{sid}", end_session)
    app.router.add_post("/api/v1/account/logout-all", logout_all)
