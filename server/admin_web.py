"""The admin panel of the server (admin.example.org): for the operator only, outside the panel of the people. It is the web face of what `lostos-admin` does on the command line:
who has a server process and how much memory it takes, the requests to come in, the limits, the registration, a restart of a person's process.

It runs inside the gateway, on its own host name (LOSTOS_ADMIN_HOST, e.g. admin.example.org; empty = off). The host gate keeps the two worlds apart: on the admin host nothing but /admin, /auth and the
files of the page is served, on every other host /admin does not exist. The login is the one of the gateway (the bot with the number, or a one-time link) and the session must be the operator's."""
import json
import os
import time

from aiohttp import web

import edition as edmod
import plugin_sign

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")


def meminfo() -> dict:
    out = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                out[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    return out


def uptime() -> int:
    try:
        return int(float(open("/proc/uptime", encoding="utf-8").read().split()[0]))
    except OSError:
        return 0


def load() -> list:
    try:
        return [float(x) for x in open("/proc/loadavg", encoding="utf-8").read().split()[:3]]
    except OSError:
        return []


def install(app: web.Application, people, mgr, lk, bot, log, host: str, registry=None):
    """The routes and the host gate. `host` is the name of the admin site ("" = the admin panel is not there)."""
    import tenants as tmod

    def me(req):
        s = req.get("session")
        if not s or not s.get("admin"):
            raise web.HTTPForbidden(text=json.dumps({"ok": False, "error": "только оператор сервера"}), content_type="application/json")
        return s

    def edition() -> str:
        return edmod.edition(getattr(mgr, "data", None))

    def center(req):
        """the sections of the team (the lists of plugapi, the keys): the server of a host has none of them, whatever the page asks"""
        s = me(req)
        if edition() != "center":
            raise web.HTTPForbidden(text=json.dumps({"ok": False, "error": edmod.CENTER_ONLY}), content_type="application/json")
        return s

    def save_cfg(changes: dict):
        cfg = dict(tmod.DEFAULTS)
        try:
            cfg.update(json.load(open(mgr.path(), encoding="utf-8")))
        except (OSError, ValueError):
            pass
        cfg.update(changes)
        tmp = mgr.path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, mgr.path())
        mgr.cfg_mtime = 0                                           # read again at once
        mgr.load_config()

    @web.middleware
    async def gate(req: web.Request, handler):
        on_admin = bool(host) and req.host.split(":")[0].lower() == host.lower()
        path = req.path
        if on_admin:
            if path == "/":
                raise web.HTTPFound("/admin/")
            if not (path.startswith(("/admin", "/auth/", "/static/", "/icons/")) or path in ("/api/v1/hello", "/sw.js", "/manifest.webmanifest", "/favicon.ico")):
                return web.Response(status=404)
        elif path.startswith("/admin"):
            return web.Response(status=404)
        return await handler(req)

    app.middlewares.insert(0, gate)

    async def page(req):
        me(req)
        return web.FileResponse(os.path.join(WEB, "admin.html"), headers={"Cache-Control": "no-store"})

    async def overview(req):
        me(req)
        by_house = {}
        for t in mgr.status():
            by_house.setdefault(t["id"], []).append(t)
        houses = []
        for owner in people.tenants():
            tid = owner["tg_id"]
            procs = by_house.get(tid, [])
            stations = []
            for sid, r in lk.stations.items():
                if lk.tenant_of_station(r) == tid and r.get("confirmed"):
                    stations.append({"id": sid, "name": r.get("name", ""), "online": sid in lk.online})
            houses.append({"id": tid, "name": owner["name"], "username": owner["username"], "admin": bool(owner.get("admin")), "limit_mb": mgr.limit_mb(tid),
                           "own_limit": str(tid) in mgr.cfg.get("overrides", {}), "people": [{"id": u["tg_id"], "name": u["name"], "role": u["role"]} for u in people.users(tid)],
                           "stations": stations, "processes": procs})
        reg = {}
        if registry is not None:
            await registry.refresh()
            reg = {"url": registry.url, "plugins": len(registry.index.get("plugins", [])), "developers": len(registry.developers()), "revoked": len(registry.revoked()),
                   "updated": registry.index.get("updated", ""), "error": registry.error, "developers_source": registry.developers_source(), "developers_error": registry.devs_error,
                   "list": [{"id": p.get("id"), "name": p.get("name"), "version": p.get("version"), "publisher": p.get("publisher", "")} for p in registry.index.get("plugins", [])]}
        owners = {h["tg_id"]: h["name"] for h in people.tenants()}
        stations = []
        for sid, r in lk.stations.items():
            tid = lk.tenant_of_station(r) if r.get("confirmed") else 0
            stations.append({"id": sid, "name": r.get("name", ""), "confirmed": bool(r.get("confirmed")), "online": sid in lk.online, "house": owners.get(tid, ""), "house_id": tid})
        links = [{"id": x["hash"][:16], "created": x["created"], "expires": x["expires"]} for x in people.reg_links()]
        return web.json_response({
            "ok": True, "me": {"id": req["session"]["tg_id"], "name": (people.user(req["session"]["tg_id"]) or {}).get("name", "")},
            "server": {"mem": meminfo(), "load": load(), "uptime": uptime(), "time": int(time.time())},
            "config": {"default_mb": mgr.cfg.get("default_mb"), "max_tenants": mgr.cfg.get("max_tenants"), "idle_minutes": mgr.cfg.get("idle_minutes"), "registration": mgr.registration()},
            "capacity_left": mgr.capacity_left(), "houses": houses,
            "requests": [{"id": r["tg_id"], "name": r["name"], "username": r["username"], "ts": r["ts"]} for r in people.pending_requests()], "registry": reg,
            "stations": stations, "invites": links, "bot": getattr(bot, "username", "") if bot else "", "edition": edition()})

    async def body(req) -> dict:
        try:
            b = await req.json()
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"ok": False}), content_type="application/json")
        return b if isinstance(b, dict) else {}

    async def set_config(req):
        me(req)
        b = await body(req)
        ch = {}
        for key, lo, hi in (("default_mb", 48, 2048), ("max_tenants", 1, 500), ("idle_minutes", 1, 1440)):
            if key in b:
                try:
                    ch[key] = int(min(hi, max(lo, int(b[key]))))
                except (TypeError, ValueError):
                    return web.json_response({"ok": False, "error": f"{key}: нужно число"}, status=400)
        if "registration" in b:
            word = "invite" if b["registration"] == "closed" else b["registration"]
            if word not in ("open", "approval", "invite"):
                return web.json_response({"ok": False, "error": "регистрация: open (открыта всем), approval (закрыта, по заявкам) или invite (закрыта, только по ссылке)"}, status=400)
            ch["registration"] = word
        if ch:
            save_cfg(ch)
            people.log(req["session"]["tg_id"], "admin", "настройки сервера: " + ", ".join(f"{k}={v}" for k, v in ch.items()))
        return await overview(req)

    async def set_ram(req):
        me(req)
        b = await body(req)
        try:
            mb = int(b["mb"])
            tid = int(b["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "нужны id и mb"}, status=400)
        if not 48 <= mb <= 4096 or not people.user(tid):
            return web.json_response({"ok": False, "error": "память от 48 до 4096 МБ, человек должен существовать"}, status=400)
        ov = dict(mgr.cfg.get("overrides", {}))
        if b.get("reset"):
            ov.pop(str(tid), None)
        else:
            ov[str(tid)] = mb
        save_cfg({"overrides": ov})
        people.log(req["session"]["tg_id"], "admin", f"лимит памяти {tid}: {'по умолчанию' if b.get('reset') else str(mb) + ' МБ'}")
        return await overview(req)

    async def restart(req):
        me(req)
        b = await body(req)
        try:
            tid = int(b["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        if not people.user(tid):
            return web.json_response({"ok": False, "error": "такого человека нет"}, status=404)
        os.makedirs(os.path.join(mgr.root, str(tid)), exist_ok=True)
        open(os.path.join(mgr.root, str(tid), "restart"), "w").close()          # the watch of the manager restarts all processes of the house within 5 seconds
        people.log(req["session"]["tg_id"], "admin", f"перезапуск процессов {tid}")
        return web.json_response({"ok": True})

    async def decide(req):
        me(req)
        b = await body(req)
        try:
            tid = int(b["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        approve = bool(b.get("approve"))
        if approve and mgr.capacity_left() <= 0:
            return web.json_response({"ok": False, "error": "мест нет: увеличьте максимум людей"}, status=409)
        r = people.decide(tid, approve)
        if not r:
            return web.json_response({"ok": False, "error": "заявка уже решена"}, status=404)
        if bot:
            import asyncio
            asyncio.get_event_loop().create_task(bot.send(tid, "Доступ открыт ✅ Откройте страницу сервера и войдите через Telegram" if approve else "Доступ не одобрен"))
        people.log(req["session"]["tg_id"], "admin", f"заявка {tid}: {'одобрена' if approve else 'отклонена'}")
        return await overview(req)

    async def remove(req):
        me(req)
        b = await body(req)
        try:
            tid = int(b["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        u = people.user(tid)
        if not u or u.get("admin"):
            return web.json_response({"ok": False, "error": "оператора удалить нельзя"}, status=400)
        for m_ in people.users(tid):
            people.remove_user(m_["tg_id"], tid)
        people.remove_user(tid)
        for t in mgr.of(tid):
            await mgr.stop(tid, t.sid, "removed by the operator")
        people.log(req["session"]["tg_id"], "admin", f"удалён человек {tid} вместе с домом (папка data/tenants/{tid} осталась)")
        return await overview(req)

    # ---- links of the operator that let a new person in
    async def make_invite(req):
        me(req)
        if not (bot and getattr(bot, "username", "")):
            return web.json_response({"ok": False, "error": "бот Telegram не настроен или ещё не назвал себя"}, status=503)
        raw = people.new_token("reg", by_tg=req["session"]["tg_id"])
        if not raw:
            return web.json_response({"ok": False, "error": "слишком много открытых запросов, подождите"}, status=429)
        people.log(req["session"]["tg_id"], "admin", "создана ссылка-приглашение")
        j = json.loads((await overview(req)).body)
        j["url"] = f"https://t.me/{bot.username}?start=r{raw}"                     # shown once: the server keeps only a hash
        return web.json_response(j)

    async def revoke_invite(req):
        me(req)
        b = await body(req)
        if not people.revoke_reg_link(str(b.get("id", ""))):
            return web.json_response({"ok": False, "error": "такой ссылки нет"}, status=404)
        people.log(req["session"]["tg_id"], "admin", "ссылка-приглашение отозвана")
        return await overview(req)

    # ---- the stations of the whole server
    async def station_remove(req):
        me(req)
        b = await body(req)
        sid = str(b.get("id", ""))
        if sid not in lk.stations:
            return web.json_response({"ok": False, "error": "такой станции нет"}, status=404)
        await lk.remove(sid)
        people.log(req["session"]["tg_id"], "admin", f"станция {sid[:8]} убрана оператором")
        return await overview(req)

    # ---- the signed lists on plugapi: the verified developers, the revoked keys, the known and the banned servers
    def devlist():
        import devlist as dlmod
        return dlmod.DevList(getattr(mgr, "data", None) or os.environ.get("LOSTOS_DATA") or ".")

    def trusted_view() -> list:
        try:
            raw = json.load(open(plugin_sign.TRUST_FILE, encoding="utf-8")).get("team", [])
        except (OSError, ValueError):
            raw = []
        names = {"": "команда: подписывает пакеты и всё остальное", "index": "робот релизов: только индекс реестра", "developers": "plugapi: только списки разработчиков и серверов"}
        return [{"publisher": k.get("publisher", ""), "fingerprint": plugin_sign.fingerprint(k["key"]), "role": names.get(k.get("scope", ""), k.get("scope", ""))} for k in raw if k.get("key")]

    async def security(req):
        center(req)
        v = devlist().view()
        v["trusted_keys"] = trusted_view()
        v["plugapi"] = {"developers": plugin_sign.team_public_keys("developers") != [], "address": os.environ.get("LOSTOS_PLUGAPI", "https://plugapi.losted.dev")}
        return web.json_response({"ok": True, **v})

    async def developers(req):
        center(req)
        b = await body(req)
        act, dl = str(b.get("action", "")), devlist()
        try:
            if act == "add":
                fp = dl.add(str(b.get("id", "")).strip(), str(b.get("name", "")).strip(), str(b.get("key", "")).strip())
                people.log(req["session"]["tg_id"], "admin", f"разработчик {b.get('id')} подтверждён ({fp})")
            elif act == "remove":
                dl.remove(str(b.get("id", "")))
                people.log(req["session"]["tg_id"], "admin", f"разработчик {b.get('id')} убран из списка")
            elif act == "revoke":
                dl.revoke(str(b.get("fingerprint", "")).strip().lower(), str(b.get("why", "")))
                people.log(req["session"]["tg_id"], "admin", f"ключ {b.get('fingerprint')} отозван")
            elif act == "refresh":
                dl.build()
            else:
                return web.json_response({"ok": False, "error": "неизвестное действие"}, status=400)
        except (ValueError, OSError, FileNotFoundError, KeyError) as e:
            return web.json_response({"ok": False, "error": str(e) or "не получилось"}, status=400)
        return await security(req)

    async def servers(req):
        center(req)
        b = await body(req)
        act, dl = str(b.get("action", "")), devlist()
        try:
            if act == "add":
                dl.add_server(str(b.get("address", "")), str(b.get("name", "")))
            elif act == "remove":
                dl.remove_server(str(b.get("ident", "")))
            elif act == "ban":
                dl.ban_server(str(b.get("address", "")), str(b.get("why", "")))
            elif act == "unban":
                dl.unban_server(str(b.get("ident", "")))
            else:
                return web.json_response({"ok": False, "error": "неизвестное действие"}, status=400)
        except (ValueError, OSError, FileNotFoundError, KeyError) as e:
            return web.json_response({"ok": False, "error": str(e) or "не получилось"}, status=400)
        people.log(req["session"]["tg_id"], "admin", f"список серверов: {act}")
        return await security(req)

    app.router.add_get("/admin/", page)
    app.router.add_get("/admin", page)
    app.router.add_get("/admin/api/overview", overview)
    app.router.add_post("/admin/api/config", set_config)
    app.router.add_post("/admin/api/ram", set_ram)
    app.router.add_post("/admin/api/restart", restart)
    app.router.add_post("/admin/api/decide", decide)
    app.router.add_post("/admin/api/remove", remove)
    app.router.add_post("/admin/api/invite", make_invite)
    app.router.add_post("/admin/api/invite/revoke", revoke_invite)
    app.router.add_post("/admin/api/station/remove", station_remove)
    app.router.add_get("/admin/api/security", security)
    app.router.add_post("/admin/api/developers", developers)
    app.router.add_post("/admin/api/servers", servers)
