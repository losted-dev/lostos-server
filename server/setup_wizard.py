"""The first-run wizard of the panel: what the owner sets up after his speaker is linked, step by step (Settings > First setup, it opens by itself until it is finished)

  GET  /api/v1/setup        the state of every step (what is done, what is missing) and the lists the pages need (models, commands, city ...); no secret values
  POST /api/v1/setup        {"done": true}  the wizard is finished or skipped
                            {"brain": "deepseek"}  which model answers
                            {"style": "short" | "normal" | "detailed"}  how long the answers are
                            {"tool": "search_web", "on": false}  a command of the assistant on / off (plugins' commands too)
                            {"city": "Краснодар"}  the city for the weather (looked up at Open-Meteo)
  POST /api/v1/setup/test   {"provider": "gemini"}  one small request with the saved key: "" in "error" means the key works
Keys themselves are saved through /api/v1/secrets (vault.py); the owner only (auth.py keeps members out of /api/v1/setup)
Only a server with a vault (the one on the internet) has the wizard; on the home server the keys are in .env and the page answers {"available": false}."""
import aiohttp
from aiohttp import web

import brains
import weather

TITLES = {"music_control": "Управление музыкой", "spotify_play": "Включить в Spotify", "get_weather": "Погода", "home_control": "Умный дом",
          "set_timer": "Таймеры", "set_alarm": "Будильники", "set_reminder": "Напоминания", "run_scenario": "Сценарии", "list_reminders": "Список напоминаний",
          "cancel_reminder": "Отмена напоминаний", "search_web": "Поиск в интернете", "enroll_voice": "Запомнить голос", "remember_about_speaker": "Запомнить о человеке",
          "forget_voice": "Забыть голос", "spotify_like": "Лайк в Spotify", "clock_equalizer": "Часы и эквалайзер", "airplay_control": "AirPlay", "bluetooth_control": "Bluetooth",
          "set_volume": "Громкость"}


def commands(m) -> list:
    off = m.tools_off()
    out = [{"name": d["name"], "title": TITLES.get(d["name"], d["name"]), "about": d.get("description", "").split(".")[0][:140], "plugin": "", "on": d["name"] not in off}
           for d in m.TOOLS[0]["functionDeclarations"]]
    for p, d in (m.PLUGINS.plugin_tools() if m.PLUGINS else []):
        out.append({"name": d["name"], "title": d["name"], "about": str(d.get("description", "")).split(".")[0][:140], "plugin": p.m["name"] if hasattr(p, "m") else "", "on": d["name"] not in off})
    return out


async def find_city(name: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": name, "count": 1, "language": "ru", "format": "json"}) as r:
            res = (await r.json(content_type=None)).get("results") or []
    if not res:
        raise ValueError("такого города не нашла")
    r0 = res[0]
    return {"name": r0["name"] + (", " + r0["country"] if r0.get("country") else ""), "lat": r0["latitude"], "lon": r0["longitude"], "tz": r0.get("timezone", "")}


def install(app: web.Application, m, vault, public_url: str):
    import os

    def spotify_receiver(m) -> dict:
        """Spotify Connect on the speaker (the system plugin ru.lostos.spotify, its state as the speaker reported it): without it music
        asked for by voice has nowhere to play, whatever the Web API login says."""
        plugins = getattr(m, "PLUGINS", None)
        p = plugins.items.get("ru.lostos.spotify") if plugins else None
        if not p:
            return {"known": False, "running": False, "text": "плагина Spotify Connect нет на сервере"}
        return {"known": True, "enabled": plugins.enabled(p), "running": plugins.is_running(p), "text": plugins.device_text(p)}

    async def state(req):
        link = getattr(m, "LINK", None)
        sess = req.get("session") or {}
        stations = link.listing(sess.get("tg_id", 0), sess.get("role", "owner")) if link else []
        st = stations[0] if stations else None
        sel = m.SETTINGS.get("brain", "")
        import homeassistant as hamod
        ha_forward = bool(hamod.FORWARD and hamod.FORWARD())
        return web.json_response({
            "available": True, "done": bool(m.SETTINGS.get("setup_done")), "public_url": public_url, "served": getattr(m, "STATION_ID", ""),   # the station this process serves
            "station": st, "stations": len(stations), "vault_locked": vault.locked,
            "brain": {"providers": brains.listing(sel, with_custom=True), "selected": brains.pick(sel), "custom": brains.custom()},
            "search": {"on": "search_web" not in m.tools_off(), "tavily": bool(os.environ.get("TAVILY_API_KEY"))},
            "style": m.SETTINGS.get("answer_style", "normal") or "normal",
            "commands": commands(m),
            "spotify": {"redirect": public_url + "/spotify/callback", "client_id": bool(os.environ.get("SPOTIFY_CLIENT_ID")), "logged_in": bool(vault.get("spotify:token")),
                        "receiver": spotify_receiver(m)},
            "ha": {"token": bool(os.environ.get("HA_TOKEN")), "room": bool(os.environ.get("SPEAKER_ROOM")), "reachable": ha_forward, "addr": (link.forward_addr("ha") if link and hasattr(link, "forward_addr") else ""),"configured": bool(m.HA.configured), "error": m.HA.error or ""},
            "city": m.SETTINGS.get("weather"),
            "voices": len(m.VOICES.people),
        })

    async def post(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        S = m.SETTINGS
        if "done" in b:
            S["setup_done"] = bool(b["done"])
        if "custom" in b:                                               # the address and the model of "Свой сервис" (its key goes to /api/v1/secrets)
            import ai_settings
            ai = dict(S.get("ai") or {})
            err = ai_settings.set_custom(ai, b["custom"])
            if err:
                return web.json_response({"ok": False, "error": err}, status=400)
            S["ai"] = ai
        if "brain" in b:
            if b["brain"] not in brains.PROVIDERS:
                return web.json_response({"ok": False, "error": "такой модели нет"}, status=400)
            S["brain"] = b["brain"]
        if "style" in b:
            if b["style"] not in ("short", "normal", "detailed"):
                return web.json_response({"ok": False}, status=400)
            S["answer_style"] = b["style"]
        if "tool" in b:
            names = {c["name"] for c in commands(m)}
            if b["tool"] not in names:
                return web.json_response({"ok": False, "error": "такой команды нет"}, status=404)
            off = set(S.get("tools_off", []))
            (off.discard if b.get("on") else off.add)(b["tool"])
            S["tools_off"] = sorted(off)
        out = {"ok": True}
        if "city" in b:
            try:
                loc = await find_city(str(b["city"]).strip()[:80])
            except (ValueError, aiohttp.ClientError):
                return web.json_response({"ok": False, "error": "такого города не нашла, попробуйте написать иначе"}, status=404)
            S["weather"] = loc
            weather.reset()
            out["city"] = loc
            m.save_settings()
            await m.timezone_changed()                                  # the clock of the server and of the speaker follow the city
        m.save_settings()
        return web.json_response(out)

    async def test(req):
        try:
            provider = str((await req.json()).get("provider", ""))
        except ValueError:
            provider = ""
        if provider not in brains.PROVIDERS:
            return web.json_response({"ok": False, "error": "такой модели нет"}, status=400)
        err = await brains.ping(provider)
        return web.json_response({"ok": not err, "error": err})

    app.router.add_get("/api/v1/setup", state)
    app.router.add_post("/api/v1/setup", post)
    app.router.add_post("/api/v1/setup/test", test)


def install_unavailable(app: web.Application):
    async def state(req):
        return web.json_response({"available": False})

    app.router.add_get("/api/v1/setup", state)
