"""lostOS panel: http://<this PC>:8765/ (same port as the answers the speaker downloads).
A phone-first PWA (web/): what plays, transport, volume, sources, alarms, voices, Wi-Fi, the log. This module serves it
and answers its two calls: GET /api/state and POST /api/do. LAN only, no password: do not expose the port to the internet."""
import asyncio
import json
import os
import urllib.parse
import time

from aiohttp import web

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
NO_CACHE = {"Cache-Control": "no-cache"}


def SPOT_NOW_OVERRIDE(m):
    return bool(m.SPOT_NOW["text"])


def split_track(text: str):
    """'Artist - Title' -> (title, artist)"""
    artist, _, title = (text or "").partition(" - ")
    return (title, artist) if title else (artist, "")


def parse_now(text: str) -> dict:
    """lostOS's "Now playing": "Artist — Title · Source[ · пауза]" -> {source, title, artist, paused}; {} when nothing plays or the text is unknown."""
    text = (text or "").strip()
    if not text or text == "Ничего не играет" or " · " not in text:
        return {}
    parts = text.split(" · ")
    paused = parts[-1] == "пауза"
    if paused:
        parts.pop()
    source = parts.pop()
    title, _, artist = "", "", ""
    body = " · ".join(parts)
    if " — " in body:
        artist, _, title = body.partition(" — ")
    else:
        title = body
    return {"source": source, "title": title, "artist": artist, "paused": paused}


AIR_COVER = {"ver": "0"}      # which picture the speaker holds (its id): the panel reloads the image when it changes
SP_ST = {"t": 0.0, "st": {}, "until": 0.0}      # the Web API answer, kept for a few seconds: the panel polls often and Spotify's quota is small


async def spot_state(m):
    now = time.time()
    if now - SP_ST["t"] < 5 or now < SP_ST["until"]:
        return SP_ST["st"]
    SP_ST["t"] = now
    try:
        SP_ST["st"] = await m.asyncio.wait_for(m.SPOT._req("GET", "/me/player"), 2.5) or {}
    except Exception as e:
        if "429" in str(e):
            SP_ST["until"] = now + 120               # over the quota: leave Spotify alone for a while
        SP_ST["st"] = {} if "429" not in str(e) else SP_ST["st"]
    return SP_ST["st"]


COVERS = {}      # "artist|title" -> url ("" = none found): AirPlay brings the text only, the picture is looked up in iTunes


async def _fetch_cover(key: str, artist: str, title: str):
    import re

    import aiohttp
    url = ""
    clean = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip() or title          # "Song (slowed) (Song (slowed))" -> "Song"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as ses:
            for term in (f"{artist} {title}", f"{artist} {clean}"):          # Deezer knows many tracks iTunes does not (remixes, Russian rap)
                async with ses.get("https://api.deezer.com/search", params={"q": term.strip(), "limit": 1}) as r:
                    items = (await r.json(content_type=None)).get("data") or []
                if items and (not artist or artist.casefold()[:4] in items[0]["artist"]["name"].casefold()):
                    url = items[0]["album"].get("cover_xl") or items[0]["album"].get("cover_big") or ""
                    break
            if not url:
                async with ses.get("https://itunes.apple.com/search", params={"term": f"{artist} {clean}".strip(), "entity": "song", "limit": 1}) as r:
                    items = (await r.json(content_type=None)).get("results") or []
                if items:
                    url = (items[0].get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
    except Exception:
        url = None                         # no network: try again later
    if url is None:
        COVERS.pop(key, None)
    else:
        COVERS[key] = url


LOOKUP = {"on": True}      # the speaker's "Cover look-up online" switch (kept in its config)


def cover_for(artist: str, title: str) -> str:
    import asyncio
    if not LOOKUP["on"]:
        return ""
    key = f"{artist}|{title}".casefold()
    if key not in COVERS:
        COVERS[key] = ""
        asyncio.get_running_loop().create_task(_fetch_cover(key, artist, title))
    return COVERS[key]


def from_hub(m, sp: dict):
    """What plays, from the speaker's own picture of the music (MediaHub); None while the speaker does not send it (an older build)"""
    d, t = m.MEDIA["data"], m.MEDIA["t"]
    if not t:
        return None
    src = {"spotify": "Spotify", "airplay": "AirPlay", "bluetooth": "Bluetooth"}.get(d.get("source"))
    if not src:
        return {}
    playing = bool(d.get("playing"))
    progress = m.MEDIA["pos"] + (int((time.time() - m.MEDIA["pos_t"]) * 1000) if playing else 0)
    dur = d.get("dur") or 0
    out = {"source": src, "title": d.get("title") or src, "artist": d.get("artist", ""), "art": "", "playing": playing,
           "progress": min(progress, dur) if dur else 0, "duration": dur}
    if src == "Spotify":
        out["art"] = d.get("cover") or sp.get("art", "")
        out["liked"] = sp.get("liked")
    elif src == "AirPlay":
        out["art"] = "/api/cover?v=" + str(abs(hash(out["artist"] + out["title"])) % 10**8) + "-" + str(d.get("air_cover", 0))
    elif src == "Bluetooth" and out["title"] != src:
        out["art"] = cover_for(out["artist"], out["title"])
    return out


def now_playing(m, sp: dict) -> dict:
    """The one thing that plays right now, whatever the source. The speaker's own "Now playing" sensor decides which source it is and
    whether it plays (AirPlay and Bluetooth show up there even without a title); the cover and the progress come from Spotify."""
    hub = from_hub(m, sp)
    if hub is not None:
        return hub
    info = parse_now(m.NOWP["text"])
    if info:
        src = info["source"]
        if src == "Spotify":
            out = {"source": src, "title": sp.get("title") or info["title"], "artist": sp.get("artist") or info["artist"], "art": sp.get("art", ""),
                   "progress": sp.get("progress", 0), "duration": sp.get("duration", 0), "liked": sp.get("liked")}
        else:
            out = {"source": src, "title": info["title"] or src, "artist": info["artist"], "art": "", "progress": sp.get("pb", (0, 0))[0], "duration": sp.get("pb", (0, 0))[1]}
            if src == "AirPlay" and info["title"]:
                out["art"] = "/api/cover?v=" + str(abs(hash(info["artist"] + info["title"])) % 10**8) + "-" + AIR_COVER["ver"]
            elif src == "Bluetooth" and info["title"]:     # the phone sends no picture over Bluetooth: it is looked up by artist and title
                out["art"] = cover_for(info["artist"], info["title"])
        out["playing"] = not info["paused"]
        return out
    if m.NOWP["text"].strip() == "Ничего не играет":
        return {}
    bt = m.BT
    if bt["playing"]:
        title, artist = split_track(bt["now"])
        return {"source": "Bluetooth", "title": title or "Bluetooth", "artist": artist, "art": "", "playing": True, "progress": 0, "duration": 0}
    if m.AIR_NOW["text"]:
        title, artist = split_track(m.AIR_NOW["text"])
        return {"source": "AirPlay", "title": title, "artist": artist, "art": "", "playing": True, "progress": 0, "duration": 0}
    if sp.get("title"):
        return {"source": "Spotify", "title": sp["title"], "artist": sp.get("artist", ""), "art": sp.get("art", ""),
                "playing": bool(sp.get("playing")), "progress": sp.get("progress", 0), "duration": sp.get("duration", 0)}
    return {}


PANEL_ACTIONS = {"previous": "панель: предыдущий трек", "next": "панель: следующий трек", "toggle": "панель: пауза или пуск", "volume": "панель: громкость", "like": "панель: лайк",
                 "unlike": "панель: убрал лайк", "eq": "панель: эквалайзер", "seek": "панель: перемотка", "bluetooth": "панель: Bluetooth", "airplay": "панель: приёмник AirPlay",
                 "voice_enroll": "панель: запомнить голос", "voice_delete": "панель: удалил голос", "voice_rename": "панель: переименовал голос", "weather": "панель: погода на часах"}


def my_voices(req, m) -> set:
    s = req.get("session") or {}
    return set(m.VOICES.visible(s.get("tg_id", 0), s.get("role", "owner")))


def mine_item(req, item: dict) -> bool:
    """Dialogs and the last phrase belong to the person whose voice it was: everybody sees his own, the owner also the ones of voices nobody knows.
    A home server without accounts shows everything to everybody (one household)."""
    s = req.get("session")
    if not s:
        return True
    tg = item.get("tg")
    return tg == s.get("tg_id") or (s.get("role") == "owner" and not tg)


def last_for(req, m) -> dict:
    return m.LAST if mine_item(req, m.LAST) else {"t": "", "text": "", "pre": "", "who": "", "score": 0.0, "outcome": ""}


def own_log(req, m) -> list:
    """The logs page of a server with accounts: everybody, the owner too, sees only his own events (logins, what she heard from his voice) and nothing of the
    server itself (that journal stays on the machine: journalctl). A home server without accounts shows its journal, there is one household."""
    s = req.get("session")
    acc = req.app.get("accounts")
    if s and acc is not None:
        return acc.events_text(s["tg_id"])
    return list(m.LOG_RING)[-80:]


def reminder_json(m, i: dict) -> dict:
    """One timer / alarm / reminder for the panel (the panel draws the lists itself; `desc` is the sentence the assistant would say)"""
    return {"id": i["id"], "kind": i["kind"], "desc": m.REM.describe(i), "label": i.get("label", ""), "text": i.get("text", ""),
            "time": i.get("time", ""), "repeat": i.get("repeat", "once"), "at": i["at"], "total": i.get("total", 0),
            "enabled": i.get("enabled", True), "snooze_min": i.get("snooze_min", 5), "snooze": bool(i.get("snooze"))}


_AIR = {"t": 0.0, "on": False}


def mirror_tiles(m) -> list:
    """The clock and the 3D model of the speaker: they repeat what the speaker shows (the time or a text on the display, the light of the voice assistant). Off until the owner turns them on in the gallery."""
    if m.PANEL["cli"] is None:
        return []
    s = m.SYSSTAT["s"]
    base = {"plugin_id": "lostos", "plugin": "lostOS", "shelf": "now", "tint": "", "off_by_default": True, "group": "info"}
    return [{**base, "id": "sys.clock", "title": "Часы", "label": "Часы", "about": "Часы так, как их показывает колонка: время, а когда она что-то выводит, этот текст", "icon": "clock", "kind": "clock", "accent": "#a78bfa"}]


def stat_tiles(m) -> list:
    """The system widgets with the numbers of the speaker (load and temperature, memory, free room). They are off until the owner turns them on in
    the edit mode of the home page ("off_by_default"); they exist only while the speaker tells its numbers."""
    d = m.SYSSTAT["d"]
    if not d or time.time() - m.SYSSTAT["t"] > 30:
        return []
    base = {"plugin_id": "lostos", "plugin": "lostOS", "shelf": "now", "tint": "", "off_by_default": True, "group": "info"}
    out = []
    hw = {}
    if "cpu" in d:
        temp = d.get("temp")
        hw.update(cpu=d["cpu"], temp=temp)
        out.append({**base, "id": "sys.cpu", "title": "Процессор", "label": "Процессор", "about": "Нагрузка процессора и температура колонки", "icon": "cpu", "value": f"{int(d['cpu'])}%",
                    "subtitle": f"{temp:.0f} °C" if temp is not None else "нагрузка", "bar": d["cpu"] / 100,
                    "accent": "#ef4444" if temp is not None and temp >= 75 else "#f97316"})
    if d.get("mem_total"):
        used, total = d.get("mem_used", 0), d["mem_total"]
        hw.update(mem_used=used, mem_total=total)
        out.append({**base, "id": "sys.ram", "title": "Память", "label": "Память", "about": "Сколько оперативной памяти занято", "icon": "memory", "value": f"{round(100 * used / total)}%",
                    "subtitle": f"{used / 1024:.0f} из {total / 1024:.0f} МБ", "bar": used / total, "accent": "#8b5cf6"})
    if d.get("disk_total"):
        free, total = d.get("disk_free", 0), d["disk_total"]
        hw.update(disk_free=free, disk_total=total)
        out.append({**base, "id": "sys.disk", "title": "Свободно", "label": "Накопитель", "about": "Свободное место на накопителе колонки", "icon": "disk", "value": f"{free / 1024:.0f} МБ" if free < 1 << 20 else f"{free / (1 << 20):.1f} ГБ",
                    "subtitle": f"из {total / 1024:.0f} МБ", "bar": 1 - free / total, "accent": "#22c55e"})
    if len(hw) >= 4:                                                    # everything about the hardware in one widget
        out.append({**base, "id": "sys.hw", "title": "Железо", "label": "Железо", "about": "Процессор, температура, память и накопитель колонки в одном виджете",
                    "icon": "cpu", "kind": "hw", "hw": hw, "accent": "#6366f1"})
    return out


# What lostOS draws on the home page by itself (id, title, shelf, icon). "page" is a whole section, not an item of a shelf.
# "label" is the caption under the widget, "about" the line in the gallery of widgets, "group": info | control (the control centre).
SYSTEM_TILES = [
    {"id": "sys.now", "title": "Сейчас играет", "label": "Плеер", "about": "Что играет сейчас: обложка, трек, управление", "shelf": "now", "icon": "music", "group": "info"},
    {"id": "sys.alarm", "title": "Ближайший будильник", "label": "Будильник", "about": "Когда прозвенит ближайший будильник", "shelf": "now", "icon": "clock", "group": "info"},
    {"id": "sys.bluetooth", "title": "Bluetooth", "label": "Bluetooth", "about": "Включить или выключить Bluetooth колонки", "shelf": "sources", "icon": "note", "group": "control"},
    {"id": "sys.dialogs", "title": "Что она слышала", "label": "Диалоги", "about": "Последние фразы, которые она слышала", "shelf": "page", "icon": "note", "group": "info", "off_by_default": True},
]


def tile_status(now: dict, sp: dict, air_probe: bool, air_text: str) -> dict:
    """The live line of the tiles of the music sources that plugins show (tile id -> text)"""
    now_air = bool(now and now.get("source") == "AirPlay")
    air_on = air_probe or bool(air_text) or now_air          # a sender that plays proves AirPlay is on, whatever the port probe says
    air = "Играет" if now_air and now.get("playing") else "Подключён, пауза" if now_air else "Готов к трансляции" if air_on else "Приёмник остановлен"
    spot = ("Играет" if sp.get("playing") else "На паузе") if sp.get("title") else "Готов, выбери колонку в Spotify"
    return {"ru.lostos.airplay": air, "ru.lostos.spotify": spot}


async def air_enabled(m) -> bool:
    """Is AirPlay switched on on the speaker? Switched on it listens on port 7000 (the speaker has no sensor for it, only for what plays)"""
    import asyncio
    if time.time() - _AIR["t"] < 2.0:
        return _AIR["on"]
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(m.SPK['host'], m.SPK['airplay']), 0.8)
        w.close()
        on = True
    except Exception:
        on = False
    _AIR.update(t=time.time(), on=on)
    return on


def register(app: web.Application, m):
    import plugins
    import tiles as home_tiles
    m.HOME_TILES = home_tiles.HomeTiles(os.path.join(os.path.dirname(plugins.STATE_FILE), "home_tiles.json"))
    m.HOME_TILES.forget("sys.model")                                    # the 3D model widget existed for a day and was taken away: no place holder is left for it
    m.PLUGINS = m.PANEL["plugins"] = plugins.Manager(m)                    # every plugin is a process of its own; they start with the server
    import registry as registry_mod
    m.PLUGINS.registry = registry_mod.Registry(os.path.dirname(plugins.STATE_FILE), m.log)    # the signed list of plugins of the repository lostos-plugins
    app.on_startup.append(m.PLUGINS.start)
    app.on_cleanup.append(m.PLUGINS.stop)

    async def watch_developers(app):                   # the list of verified developers: a look every 15 seconds, a revoked key switches its plugins off within that time
        reg = m.PLUGINS.registry
        await m.PLUGINS.recheck_trust()                # the saved list first: a key revoked while this server was down
        app["developers_watch"] = asyncio.get_running_loop().create_task(reg.watch_developers(on_change=m.PLUGINS.recheck_trust))

    async def stop_watch(app):
        t = app.get("developers_watch")
        if t:
            t.cancel()
    app.on_startup.append(watch_developers)
    app.on_cleanup.append(stop_watch)
    @web.middleware
    async def fresh(req, handler):                       # the panel's files are small: always ask for the newest ones (no stale css after an update)
        resp = await handler(req)
        if req.path.startswith(("/static/", "/icons/")) or req.path in ("/", "/sw.js", "/manifest.webmanifest"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    app.middlewares.append(fresh)
    def file(path: str, ctype: str = None):
        async def h(req):
            return web.FileResponse(path, headers=NO_CACHE) if ctype is None else web.Response(body=open(path, "rb").read(), content_type=ctype, headers=NO_CACHE)
        return h

    async def state(req):
        sp = {}
        if m.SPOT.available:
            try:
                st = await spot_state(m)
                item = (st or {}).get("item") or {}
                here = ((st or {}).get("device") or {}).get("name", "").casefold() == m.SPOT.device_name.casefold()
                override = None
                uri0, name0 = item.get("uri"), item.get("name")
                if SPOT_NOW_OVERRIDE(m) and here:          # librespot's own track info (the Web API lags behind)
                    full, _, cover0 = m.SPOT_NOW["text"].partition("\t")     # (an older lostOS put the cover after a tab)
                    cover = m.SPOT_COVER["text"] or cover0
                    title, artist = split_track(full)
                    override = (title, artist, cover)
                if override:
                    sp = {"title": override[0], "artist": override[1], "playing": bool(st.get("is_playing")), "art": override[2],
                          "progress": 0, "duration": 0}
                    item = {}
                if item and here:
                    imgs = (item.get("album") or {}).get("images") or []
                    sp = {"title": item.get("name"), "artist": ", ".join(a["name"] for a in item.get("artists", [])[:2]),
                          "playing": bool(st.get("is_playing")), "art": imgs[0]["url"] if imgs else "",
                          "progress": max(0, st.get("progress_ms") or 0), "duration": item.get("duration_ms") or 0}
                if uri0 and here:
                    liked = await m.SPOT.is_liked(uri0)
                    if sp and liked is not None and name0 and (sp.get("title") or "").casefold()[:12] == name0.casefold()[:12]:
                        sp["liked"] = liked
            except Exception:
                pass
        hubd = m.MEDIA["data"] if m.MEDIA["t"] else {}
        uri = hubd.get("uri") or m.SPOT_COVER.get("uri")
        if uri and (hubd.get("source") == "spotify" or (parse_now(m.NOWP["text"]) or {}).get("source") == "Spotify"):      # the speaker's own track id: no waiting for the lagging Web API
            try:
                liked = await m.SPOT.is_liked(uri)
            except Exception:
                liked = None
            if liked is not None:
                sp = dict(sp, liked=liked)
        info = parse_now(m.NOWP["text"]) or {}
        if info.get("source") == "Spotify" and m.SPOT_COVER.get("ms"):      # the speaker's own clock: position at the last event plus the time since
            sc = m.SPOT_COVER
            pos = sc["pos"] + (0 if info.get("paused") else int((time.time() - sc["t"]) * 1000))
            sp = dict(sp, progress=min(pos, sc["ms"]), duration=sc["ms"])
        src = {"AirPlay": "airplay", "Bluetooth": "bluetooth"}.get(info.get("source"))
        pb = m.PLAYBACK
        if src and pb["src"] == src and pb["dur"] and time.time() - pb["t"] < 6 * 3600:
            if info.get("paused"):                                  # it stands where it was when the pause was noticed
                if pb.get("frozen_for") != pb["t"]:
                    pb["frozen_for"], pb["frozen"] = pb["t"], pb["pos"] + int((time.time() - pb["t"]) * 1000)
                pos = pb["frozen"]
            else:
                pb["frozen_for"] = None
                pos = pb["pos"] + int((time.time() - pb["t"]) * 1000)
            sp = dict(sp, pb=(min(pos, pb["dur"]), pb["dur"]))
        cov = m.SPOT_COVER["text"]
        if cov and not sp.get("art"):                       # the speaker's own sensors: the picture needs no Web API
            sp = dict(sp, art=cov.replace("ab67616d00004851", "ab67616d00001e02"))
        if parse_now(m.NOWP["text"]).get("source") == "AirPlay" if parse_now(m.NOWP["text"]) else False:
            try:
                import aiohttp
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as ses:
                    async with ses.get(f"http://{m.SPK['host']}:{m.SPK['cover']}/cover/ver") as r:
                        AIR_COVER["ver"] = (await r.text()).strip() or "0"
            except Exception:
                pass
        sw = m.PANEL["switches"].get("cover_lookup")
        LOOKUP["on"] = bool(m.PANEL["state"].get(sw["key"], True)) if sw else True
        import update as upd
        repo = os.environ.get("LOSTOS_REPO") or m._envvar("LOSTOS_REPO")
        if repo and time.time() - upd.INFO["checked"] > 6 * 3600:
            m.asyncio.get_running_loop().create_task(upd.check(repo))
        sels = []
        for oid, e in m.PANEL["selects"].items():
            sels.append({"id": oid, "name": e["name"], "options": e["options"], "current": m.PANEL["state"].get(e["key"], "")})
        wifi = {"cur": {}, "nets": [], "aps": []}
        for line in (m.WIFI["text"] or "").splitlines():
            f = line.split("\t")
            if f[0] == "cur" and len(f) >= 6:
                wifi["cur"] = {"ssid": f[1], "ip": f[2], "state": f[3], "pending": f[4], "last": f[5]}
            elif f[0] == "net" and len(f) >= 4:
                wifi["nets"].append({"id": f[1], "ssid": f[2], "flags": f[3]})
            elif f[0] == "ap" and len(f) >= 4:
                wifi["aps"].append({"ssid": f[1], "signal": f[2], "security": f[3]})
        sess = req.get("session") or {}
        mine = set(m.VOICES.visible(sess.get("tg_id", 0), sess.get("role", "owner")))                # each person sees his own voices, the owner all of them
        voices = [{"name": n, "n": v.get("n", 1), "note": v.get("note", "")} for n, v in m.VOICES.people.items() if n in mine]
        session = {"active": m.SESSION["emb"] is not None and time.time() - m.SESSION["t"] < m.SESSION_S, "name": m.SESSION["name"]}
        return web.json_response({
            "barge": m.SETTINGS["barge_in"], "last": last_for(req, m),
            "voices": voices, "session": session,
            "reminders": [reminder_json(m, i) for i in sorted(m.REM.items, key=lambda i: i["at"])],
            "wifi": wifi, "now": now_playing(m, sp), "t": time.time(), "engines": m.engines_state(), "airplay_on": await air_enabled(m),
            "tile_status": tile_status(now_playing(m, sp), sp, bool(await air_enabled(m)), m.AIR_NOW["text"]),
            "notifs": m.NOTIFS[-40:], "push": {"subs": __import__("push").count()}, "https": m.HTTPS, "scenarios": m.SCEN.items, "sleep": {"until": m.SLEEP["until"], "minutes": m.SLEEP["minutes"], "smooth": m.SLEEP["smooth"]},
            "phase": m.PHASE["p"] if time.time() < m.PHASE["until"] else "idle",
            "clock": {"text": m.CLOCKTXT["text"], "left": round(m.CLOCKTXT["until"] - time.time(), 1)} if time.time() < m.CLOCKTXT["until"] else None,
            "connected": m.PANEL["cli"] is not None, "spotify": sp, "airplay": m.AIR_NOW["text"], "bluetooth": dict(m.BT),
            "volume": m.PANEL["volume"], "app": {"version": upd.version(), "repo": bool(repo), "latest": upd.INFO["latest"], "newer": upd.newer(), "notes": upd.INFO["notes"], "url": upd.INFO["url"]}, "eq": {"on": m.EQ["on"], "preamp": m.EQ["pre"], "gains": m.EQ["gains"], "known": m.EQ["known"]}, "selects": sels, "switches": {oid: bool(m.PANEL["state"].get(e["key"])) for oid, e in m.PANEL["switches"].items()}, "context": m.CONTEXT["line"],
            "dialogs": [d for d in m.DIALOG if mine_item(req, d)], "log": own_log(req, m), "enroll": dict(m.ENROLL_STATUS)})

    async def cover(req):
        """The AirPlay sender's own picture (the speaker keeps it); without one, a look-up by artist and title."""
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as ses:
                async with ses.get(f"http://{m.SPK['host']}:{m.SPK['cover']}/cover/current.jpg") as r:
                    if r.status == 200:
                        return web.Response(body=await r.read(), content_type=r.content_type or "image/jpeg", headers={"Cache-Control": "no-store"})
        except Exception:
            pass
        info = parse_now(m.NOWP["text"])
        # AirPlay brings its own picture: nothing is looked up for it on the internet (only a phone on Bluetooth has none)
        url = cover_for(info["artist"], info["title"]) if info and info["title"] and info["source"] == "Bluetooth" else ""
        if url:
            raise web.HTTPFound(url)
        raise web.HTTPNotFound()

    async def api_media(req):
        import media
        d = json.loads((await state(req)).body)
        return web.json_response(media.view(d.get("now") or {}, d.get("volume"), bool(d.get("airplay_on")), d.get("bluetooth") or {}))

    async def api_lyrics(req):
        """/api/v1/lyrics?artist=&title=&duration=<ms>: the lines with their times, or null when no synced text exists."""
        import lyrics
        q = req.query
        try:
            dur = int(q.get("duration", "0"))
        except ValueError:
            dur = 0
        lines = await lyrics.find(q.get("artist", ""), q.get("title", ""), dur) if q.get("title") and dur > 0 else None
        return web.json_response({"lines": lines})

    async def api_eq(req):
        """/api/v1/eq: GET the equalizer of the speaker, POST {on, preamp, gains[10]} to change it."""
        import media
        if req.method == "POST":
            try:
                b = await req.json()
                await media.set_eq(m, bool(b.get("on")), float(b.get("preamp", 0)), [float(x) for x in b.get("gains", [])])
            except (ValueError, TypeError) as ex:
                return web.json_response({"ok": False, "error": str(ex)}, status=400)
        return web.json_response({"on": m.EQ["on"], "preamp": m.EQ["pre"], "gains": m.EQ["gains"], "bands_hz": media.EQ_BANDS_HZ})

    # ---- plugins and the tiles of the home page
    async def api_plugins(req):
        mgr = m.PLUGINS
        return web.json_response({"plugins": mgr.listing(), "bad": mgr.bad, "permissions": __import__("plugins").PERMISSIONS,
                                  "widgets": {"weather": m.SETTINGS.get("widget_weather", True)}})

    async def api_plugin_set(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        pid = req.match_info["pid"]
        p0 = m.PLUGINS.items.get(pid)
        if p0 and b.get("enabled") and not p0.m["system"] and m.PLUGINS.requirement(p0):
            return web.json_response({"ok": False, "error": m.PLUGINS.requirement(p0)}, status=409)
        ok = await m.PLUGINS.set_enabled(pid, bool(b.get("enabled")))
        p = m.PLUGINS.items.get(pid)
        why = "Колонка не на связи: переключатель на ней не сработал" if not ok and p and p.m["builtin"] == "speaker" and p.m["switchable"] else ""
        return web.json_response({"ok": ok, **({"error": why} if why else {})}, status=200 if ok else 404)

    async def api_plugin_install(req):
        """POST the bytes of a .lostos package (zip) to install it by hand."""
        import plugins
        data = b""
        while len(data) <= plugins.Manager.MAX_PACKAGE:
            chunk = await req.content.read(65536)
            if not chunk:
                break
            data += chunk
        try:
            man = await m.PLUGINS.install(data, urllib.parse.unquote(req.headers.get("X-File-Name", "")))
        except (ValueError, OSError) as ex:
            return web.json_response({"ok": False, "error": str(ex)}, status=400)
        return web.json_response({"ok": True, "plugin": man})

    async def api_registry(req):
        """GET: what the repository of plugins offers (a signed list), with what is installed here and where an update is. ?refresh=1 asks the repository again."""
        reg = m.PLUGINS.registry
        await reg.refresh(force=req.query.get("refresh") == "1")
        have = {pid: str(p.m.get("version", "")) for pid, p in m.PLUGINS.items.items() if p.kind != "system"}
        return web.json_response({"ok": True, **reg.view(have)})

    async def api_registry_install(req):
        """POST {"id"}: downloads the package from the repository, checks the list, the checksum and the signature, installs it (off until the owner allows it)."""
        try:
            pid = str((await req.json()).get("id", ""))
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        reg = m.PLUGINS.registry
        await reg.refresh()
        try:
            data, entry = await reg.package(pid)
            st = m.PLUGINS.trust_of(data)
            if st["state"] != "trusted":
                raise ValueError("пакет подписан не доверенным ключом: установка из репозитория отменена")
            man = await m.PLUGINS.install(data, entry["file"].rsplit("/", 1)[-1], origin={"type": "repository", "url": f"{reg.url}/{entry['file']}", "version": entry["version"]})
        except (ValueError, OSError, RuntimeError) as ex:
            return web.json_response({"ok": False, "error": str(ex)[:300]}, status=400)
        except Exception as ex:                                                     # the network: the repository is not reachable
            return web.json_response({"ok": False, "error": "репозиторий не отвечает: " + str(ex)[:120]}, status=502)
        return web.json_response({"ok": True, "plugin": man})

    async def api_plugin_page(req):
        page = m.PLUGINS.page(req.match_info["pid"])
        return web.json_response(page, status=200 if page else 404)

    async def plugin_asset(req):
        """Pictures of a plugin. They go out as pictures only: the type comes from the extension, which was checked against the first bytes."""
        path = m.PLUGINS.asset_path(req.match_info["pid"], req.match_info["path"])
        if not path:
            return web.Response(status=404)
        kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}[os.path.splitext(path)[1].lower()]
        return web.FileResponse(path, headers={"Content-Type": kind, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache",
                                               "Content-Security-Policy": "default-src 'none'; sandbox"})

    async def api_plugin_remove(req):
        ok = await m.PLUGINS.remove(req.match_info["pid"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def api_plugin_download(req):
        """The speaker fetches the program of a plugin here (only the file the manifest names, only for a plugin that exists)"""
        p = m.PLUGINS.items.get(req.match_info["pid"])
        if not p or not p.m["device"] or not p.m["device"]["binary"]:
            return web.Response(status=404)
        return web.FileResponse(os.path.join(p.folder, p.m["device"]["binary"]))

    async def api_plugin_trust(req):
        """What the speaker needs to check a package by itself: the signature file and the manifest of the package (the files the signature covers)"""
        p = m.PLUGINS.items.get(req.match_info["pid"])
        name = req.match_info["name"]
        if not p or not p.m["device"] or name not in ("lostos-signature.json", "manifest.json"):
            return web.Response(status=404)
        path = os.path.join(p.folder, name)
        return web.FileResponse(path) if os.path.isfile(path) else web.Response(status=404)

    async def api_developers_list(req):
        """The list of verified developers exactly as plugapi signed it (the speaker verifies the signature with the keys that are built into it)"""
        reg = m.PLUGINS.registry
        if not reg.devs_raw or not reg.devs_sig:
            return web.Response(status=404)
        return web.Response(body=reg.devs_raw if req.path.endswith(".json") else reg.devs_sig.encode("ascii"), content_type="application/octet-stream")

    async def api_widgets(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        if "weather" in b:
            m.SETTINGS["widget_weather"] = bool(b["weather"])
            m.save_settings()
        return web.json_response({"ok": True})

    async def api_tiles(req):
        """The widgets of the home page: the built-in weather and the tiles of the plugins that are on. Each belongs to a plugin ("plugin_id");
        the answer also has the place holders of tiles that used to be there (their plugin is off or removed) for the edit mode.
        The parts of the page that lostOS itself draws come first as "static" tiles: the owner can put them away and move them like the others."""
        tiles = [dict(t, plugin_id="lostos", plugin="lostOS", tint="", accent="", **{"static": True}) for t in SYSTEM_TILES]
        if m.SETTINGS.get("widget_weather", True):
            base = {"id": "weather", "plugin_id": "lostos", "plugin": "lostOS", "shelf": "now", "tint": "", "title": "Погода", "label": "Погода", "group": "info",
                    "about": "Погода сейчас и ближайшие части суток, с живым небом", "accent": "#3b82f6"}
            try:
                import weather
                fc = dict(await m.asyncio.wait_for(weather.forecast(), 8))
                demo = req.query.get("wx", "")                        # ?wx=storm-night on the page: look at any weather without waiting for it
                if "-" in demo:
                    fc["kind"], fc["night"] = demo.split("-", 1)[0], demo.endswith("night")
                tiles.append({**base, "icon": "sun", "kind": "weather", "weather": fc})
            except Exception:
                try:                                                  # the forecast is not there: the weather of the assistant (Yandex) as a plain tile
                    w = await m.asyncio.wait_for(weather.weather(), 4)
                    t = w["temp"]
                    tiles.append({**base, "value": ("+" if t > 0 else "") + f"{t}°", "subtitle": (w["note"] or w["condition"]).capitalize(),
                                  "icon": "sun" if "ясно" in w["condition"] else "cloud"})
                except Exception:
                    pass                                              # no weather this time: the tile is simply absent
        tiles += stat_tiles(m)
        tiles += mirror_tiles(m)
        def why(pid):
            if pid == "lostos":
                return "виджет выключен или пока нет данных (погода, колонка не на связи)"
            return m.PLUGINS.tile_gone_reason(pid)
        return web.json_response(m.HOME_TILES.view(tiles + m.PLUGINS.tiles(), why))

    async def api_display(req):
        """What the clock display of the speaker shows right now (the clock widget repeats it): {"f": "<30 hex digits>", "age": seconds since it changed, "on": the speaker is connected}.
        f is empty when the firmware of the speaker has no such sensor (the widget then draws the time itself)."""
        d = m.DISPLAY
        return web.json_response({"f": d["f"] if d["t"] else "", "age": round(time.time() - d["t"], 1) if d["t"] else None, "on": m.PANEL["cli"] is not None},
                                 headers={"Cache-Control": "no-store"})

    async def api_display_stream(req):
        """The same as /api/v1/display, but pushed: every frame of the display as it comes (server-sent events), so that the widget moves as smoothly as the speaker. A comment line every
        15 s keeps the connection alive; the first line is the frame that is on now."""
        import asyncio
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q = asyncio.Queue(maxsize=8)
        m.DISPLAY.setdefault("subs", set()).add(q)
        try:
            await resp.write(f"data: {m.DISPLAY['f'] if m.DISPLAY['t'] and m.PANEL['cli'] is not None else ''}\n\n".encode())
            while True:
                try:
                    f = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"data: {f}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keep\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            m.DISPLAY["subs"].discard(q)
        return resp

    async def api_speaker(req):
        """GET: who the speaker is and how it is doing (the "About" page). POST {"name"}: the name that AirPlay and Spotify show ("" = the name of the model)"""
        s, d = m.SYSSTAT["s"], m.SYSSTAT["d"]
        if req.method == "GET":
            return web.json_response({"online": m.PANEL["cli"] is not None, "name": s.get("name", ""), "model": s.get("model", ""), "codename": s.get("code", ""),
                                      "lostos": s.get("ver", ""), "firmware": s.get("fw", ""), "chip": s.get("chip", ""), "cpu": d.get("cpu"), "temp": d.get("temp"),
                                      "mem_used": d.get("mem_used"), "mem_total": d.get("mem_total"), "disk_free": d.get("disk_free"), "disk_total": d.get("disk_total")})
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        name = " ".join(str(b.get("name", "")).split())
        if len(name) > 40 or any(ord(c) < 32 for c in name):
            return web.json_response({"ok": False, "error": "имя до 40 знаков, без служебных символов"}, status=400)
        svc, cli = m.state_services.get("assistant"), m.PANEL.get("cli")
        if not (svc and cli):
            return web.json_response({"ok": False, "error": "колонка не на связи"}, status=503)
        await cli.execute_service(svc, {"command": "name " + name})
        m.log(f"speaker name: {name!r}")
        return web.json_response({"ok": True})

    async def api_tiles_layout(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        ok = m.HOME_TILES.set_layout(b.get("hidden"), b.get("order"))
        return web.json_response({"ok": ok}, status=200 if ok else 400)

    async def api_tiles_forget(req):
        ok = m.HOME_TILES.forget(req.match_info["tid"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def api_media_cmd(req):
        import media
        try:
            b = await req.json()
            cmd = str(b.get("cmd", ""))
            if cmd == "toggle":
                d = json.loads((await state(req)).body)
                cmd = "pause" if (d.get("now") or {}).get("playing") else "play"
            res = await media.command(m, cmd, b.get("args") or {})
        except ValueError as ex:
            return web.json_response({"ok": False, "error": str(ex)}, status=400)
        return web.json_response({"ok": True, "result": res})

    async def home(req):
        ha = m.HA
        if not ha.configured:
            return web.json_response({"configured": False, "devices": []})
        return web.json_response({"configured": True, "devices": await ha.devices(), "scenes": await ha.scenes(), "error": ha.error})

    async def backup(req):
        """Everything the user made, as one file: scenarios, alarms, the panel's settings, the notes about the voices (not the voice prints)"""
        data = {"app": "lostOS", "version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "settings": {k: v for k, v in m.SETTINGS.items() if k in ("barge_in", "alice_voice")},
                "scenarios": m.SCEN.items,
                "alarms": [{"time": i["time"], "repeat": i.get("repeat", "once"), "label": i.get("label", ""), "snooze_min": i.get("snooze_min", 5),
                            "enabled": i.get("enabled", True)} for i in m.REM.items if i["kind"] == "alarm" and not i.get("snooze")],
                "voice_notes": {n: v.get("note", "") for n, v in m.VOICES.people.items() if v.get("note")}}
        return web.Response(text=json.dumps(data, ensure_ascii=False, indent=1), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="lostos-backup-{time.strftime("%Y%m%d")}.json"'})

    async def restore(req):
        try:
            d = await req.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "Это не файл копии lostOS"})
        if not isinstance(d, dict) or d.get("app") != "lostOS":
            return web.json_response({"ok": False, "error": "Это не файл копии lostOS"})
        n = {"scenarios": 0, "alarms": 0, "notes": 0}
        st = d.get("settings") or {}
        if isinstance(st.get("barge_in"), bool):
            m.SETTINGS["barge_in"] = st["barge_in"]
        if st.get("alice_voice") in m.ALICE_VOICES:
            m.SETTINGS["alice_voice"] = st["alice_voice"]
        m.save_settings()
        for sc in d.get("scenarios") or []:
            if isinstance(sc, dict):
                m.SCEN.upsert(sc)
                n["scenarios"] += 1
        have = {(i.get("time"), i.get("repeat", "once"), i.get("label", "")) for i in m.REM.items if i["kind"] == "alarm"}
        for a in d.get("alarms") or []:
            key = (a.get("time"), a.get("repeat", "once"), a.get("label", ""))
            if key in have or not a.get("time"):
                continue
            await m.run_tool("set_alarm", {"time": str(a["time"]), "repeat": str(a.get("repeat", "once")), "label": str(a.get("label", "")),
                                           "snooze_minutes": int(a.get("snooze_min", 5))}, trusted=True)
            if a.get("enabled") is False:
                it = next((i for i in m.REM.items if i["kind"] == "alarm" and (i.get("time"), i.get("repeat", "once"), i.get("label", "")) == key), None)
                if it:
                    await m.run_tool("_alarm_toggle", {"id": it["id"], "on": False}, trusted=True)
            n["alarms"] += 1
        for name, note in (d.get("voice_notes") or {}).items():
            if name in m.VOICES.people:
                m.VOICES.people[name]["note"] = str(note)[:600]
                n["notes"] += 1
        if n["notes"]:
            m.VOICES.save()
        return web.json_response({"ok": True, "result": n})

    async def ca_cert(req):
        """The public certificate of the local CA (to install on the phone); the key never leaves the PC."""
        import certs
        if not os.path.exists(certs.CA_CRT):
            return web.Response(status=404)
        return web.Response(body=open(certs.CA_CRT, "rb").read(), content_type="application/x-x509-ca-cert", headers={"Content-Disposition": 'attachment; filename="lostOS-CA.crt"'})

    async def push_key(req):
        import push
        return web.json_response({"key": push.public_key()})

    async def push_sub(req):
        import push
        try:
            d = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        if req.path.endswith("unsubscribe"):
            push.unsubscribe(str(d.get("endpoint", "")))
            m.log(f"push: unsubscribed, {push.count()} left")
            return web.json_response({"ok": True})
        ok = push.subscribe(d, req.headers.get("User-Agent", ""))
        m.log("push: subscription " + ("saved" if ok else "refused") + f" ({str(d.get('endpoint', ''))[:48]}...), {push.count()} in total")
        return web.json_response({"ok": ok, "error": "" if ok else "Подписка не похожа на настоящую"})

    async def push_test(req):
        import push
        m.log(f"push: test for {push.count()} subscription(s)")
        push.send_async("Проверка уведомлений", "Если вы это видите, пуши работают", tag="test", log=m.log)
        return web.json_response({"ok": True})

    async def health(req):
        return web.json_response({"items": await m.health_check(req.query.get("force") == "1")})

    async def do(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        a = b.get("a")
        who_tg = (req.get("session") or {}).get("tg_id", 0) or m.household_owner()
        title = PANEL_ACTIONS.get(a)
        if title:                                                       # what a person does with the speaker from the panel goes into his journal
            m.journal(title + (f": {b['level']}%" if a == "volume" and "level" in b else ""), who_tg)
        try:
            if a in ("previous", "next"):
                import media
                res = await media.command(m, a, {})
            elif a == "toggle" and m.MEDIA["t"]:
                import media
                res = await media.command(m, "playpause", {})
            elif a == "toggle":
                st = await m.SPOT.state() if m.SPOT.available else {}
                info = parse_now(m.NOWP["text"])
                playing = (not info["paused"]) if info else (bool((st or {}).get("is_playing")) or bool(m.AIR_NOW["text"]) or bool(m.BT["playing"]))
                import media
                res = await media.command(m, "pause" if playing else "play", {})
            elif a == "eq":
                import media
                res = await media.set_eq(m, bool(b.get("on")), float(b.get("preamp", 0)), [float(x) for x in b.get("gains", [])])
            elif a == "seek":
                import media
                res = await media.command(m, "seek", {"ms": b.get("ms", 0)})
            elif a in ("like", "unlike"):
                res = await m.run_tool("spotify_like", {"action": a}, trusted=True)
            elif a == "volume":
                res = await m.run_tool("set_volume", {"level": int(b.get("level", 30))})
            elif a == "bluetooth":
                m.PENDING_BT.clear()
                res = await m.run_tool("_bluetooth_now", {"action": "on" if b.get("on") else "off"})
            elif a == "airplay":
                res = await m.run_tool("_airplay_now", {"action": "on" if b.get("on") else "off"})
            elif a == "weather":
                res = await m.run_tool("get_weather", {})
                text = m.PENDING_DISPLAY.pop("text", None)
                svc = m.state_services.get("clock_show_text")
                if text and svc:
                    await m.PANEL["cli"].execute_service(svc, {"text": text, "seconds": 8})
            elif a == "rem_cancel":
                res = await m.run_tool("cancel_reminder", {"which": str(b.get("id", ""))})
            elif a == "rem_add":
                kind = b.get("kind")
                if kind == "timer":
                    res = await m.run_tool("set_timer", {"minutes": float(b.get("minutes") or 0), "label": str(b.get("text", ""))}, trusted=True)
                elif kind == "reminder":
                    args = {"text": str(b.get("text", "")), "repeat": str(b.get("repeat", "once"))}
                    if b.get("minutes"):
                        args["in_minutes"] = float(b["minutes"])
                    else:
                        args["time"] = str(b.get("time", ""))
                    res = await m.run_tool("set_reminder", args, trusted=True)
                else:
                    res = await m.run_tool("set_alarm", {"time": str(b.get("time", "")), "repeat": str(b.get("repeat", "once")),
                                                         "label": str(b.get("text", "")), "snooze_minutes": int(b.get("snooze_min", 5)),
                                                         "replace_id": str(b.get("replace_id", ""))}, trusted=True)
            elif a == "sc_save":
                sc = m.SCEN.upsert(b.get("scenario") or {})
                res = sc["id"]
            elif a == "sc_delete":
                m.SCEN.delete(str(b.get("id", "")))
                res = "ok"
            elif a == "sc_toggle":
                sc = next((i for i in m.SCEN.items if i["id"] == b.get("id")), None)
                if sc:
                    sc["enabled"] = bool(b.get("on"))
                    m.SCEN.save()
                res = "ok"
            elif a == "sc_run":
                res = await m.run_tool("_scenario_run", {"id": str(b.get("id", ""))}, trusted=True)
            elif a == "sleep_start":
                res = await m.run_tool("_sleep_start", {"minutes": int(b.get("minutes", 30)), "smooth": bool(b.get("smooth", True))}, trusted=True)
            elif a == "sleep_cancel":
                res = await m.run_tool("_sleep_cancel", {}, trusted=True)
            elif a == "home_set":
                eid, svc = str(b.get("id", "")), str(b.get("service", "toggle"))
                data = {}
                if b.get("brightness_pct") is not None:
                    data["brightness_pct"] = max(1, min(100, int(b["brightness_pct"])))
                if b.get("rgb"):
                    data["rgb_color"] = [max(0, min(255, int(x))) for x in b["rgb"][:3]]
                if b.get("kelvin"):
                    data["color_temp_kelvin"] = int(b["kelvin"])
                res = await m.HA.call([eid], svc, data)
                if not res.startswith("готово"):
                    return web.json_response({"ok": False, "error": res})
            elif a == "engine_set":
                res = m.engine_set(str(b.get("kind", "")), str(b.get("value", "")))
                if res != "ok":
                    return web.json_response({"ok": False, "error": res})
            elif a == "engine_install":
                res = m.engine_install(str(b.get("name", "")))
                if res != "ok":
                    return web.json_response({"ok": False, "error": res})
            elif a == "alarm_toggle":
                res = await m.run_tool("_alarm_toggle", {"id": str(b.get("id", "")), "on": bool(b.get("on"))}, trusted=True)
            elif a == "barge":
                m.SETTINGS["barge_in"] = bool(b.get("on"))
                m.save_settings()
                svc = m.state_services.get("assistant")
                if svc:
                    await m.PANEL["cli"].execute_service(svc, {"command": "barge on" if b.get("on") else "barge off"})
                res = "ok"
            elif a == "voice_enroll":
                n = str(b.get("name", "")).strip()
                if n in m.VOICES.people and n not in my_voices(req, m):
                    return web.json_response({"ok": False, "error": "это чужой голос"}, status=403)
                fn = m.SPK_CALLS.get("start_enroll")
                if not n or not fn:
                    return web.json_response({"ok": False, "error": "колонка не в сети" if not fn else "как вас зовут?"}, status=503 if not fn else 400)
                owner = (req.get("session") or {}).get("tg_id", 0) or m.household_owner()
                asyncio.get_running_loop().create_task(fn(n, owner))             # she speaks and listens; the panel follows it in the state ("enroll")
                res = "ok"
            elif a == "voice_delete":
                if str(b.get("name", "")) in my_voices(req, m):
                    m.VOICES.delete(str(b.get("name", "")))
                res = "ok"
            elif a == "voice_rename":
                if str(b.get("name", "")) in my_voices(req, m):
                    m.VOICES.rename(str(b.get("name", "")), str(b.get("new", "")))
                res = "ok"
            elif a == "voice_note":
                n = str(b.get("name", ""))
                if n in m.VOICES.people and n in my_voices(req, m):
                    m.VOICES.people[n]["note"] = str(b.get("note", ""))[:600]
                    m.VOICES.save()
                res = "ok"
            elif a in ("wifi_add", "wifi_forget", "wifi_scan"):
                svc = m.state_services.get("wifi")
                if not svc:
                    return web.json_response({"ok": False, "error": "У колонки нет службы Wi-Fi (обнови lostOS на ней)"})
                clean = lambda s: str(s or "").replace("\t", " ").replace("\n", " ")
                if a == "wifi_add":
                    cmd = "\t".join(["add", clean(b.get("ssid")), clean(b.get("password")), "now" if b.get("now") else "store"])
                elif a == "wifi_forget":
                    cmd = "\t".join(["forget", clean(b.get("ssid"))])
                else:
                    cmd = "scan"
                await m.PANEL["cli"].execute_service(svc, {"command": cmd})
                res = "sent"
            elif a == "switch":
                e = m.PANEL["switches"][b["id"]]
                m.PANEL["cli"].switch_command(e["key"], bool(b.get("on")))
                res = "ok"
            elif a == "select":
                e = m.PANEL["selects"][b["id"]]
                if b["option"] not in e["options"]:
                    return web.json_response({"ok": False}, status=400)
                m.PANEL["cli"].select_command(e["key"], b["option"])
                res = "ok"
            else:
                return web.json_response({"ok": False}, status=400)
        except Exception as ex:
            return web.json_response({"ok": False, "error": str(ex)})
        return web.json_response({"ok": True, "result": res})

    app.router.add_get("/", file(os.path.join(WEB, "index.html")))
    app.router.add_get("/manifest.webmanifest", file(os.path.join(WEB, "manifest.webmanifest"), "application/manifest+json"))
    app.router.add_get("/sw.js", file(os.path.join(WEB, "sw.js"), "text/javascript"))
    app.router.add_static("/static", WEB, show_index=False, append_version=False)
    app.router.add_static("/icons", os.path.join(WEB, "icons"), show_index=False)
    async def hello(req):
        """Who answers here: the installer asks this before it links a station to the address (nothing private in the answer)"""
        import update as upd
        import brand
        return web.json_response({"lostos": "server", "version": upd.version(), "lite": bool(getattr(m, "LITE", False)), "registration": "owner", "tagline": brand.TAGLINE})

    app.router.add_get("/api/v1/hello", hello)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/home", home)
    app.router.add_get("/api/v1/media", api_media)
    app.router.add_post("/api/v1/media/command", api_media_cmd)
    app.router.add_get("/api/v1/lyrics", api_lyrics)
    app.router.add_get("/api/v1/speaker", api_speaker)
    app.router.add_get("/api/v1/display", api_display)
    app.router.add_get("/api/v1/display/stream", api_display_stream)
    app.router.add_post("/api/v1/speaker", api_speaker)
    app.router.add_post("/api/v1/tiles/layout", api_tiles_layout)
    app.router.add_delete("/api/v1/tiles/{tid}", api_tiles_forget)
    app.router.add_get("/api/v1/plugins", api_plugins)
    app.router.add_post("/api/v1/plugins/install", api_plugin_install)
    app.router.add_get("/api/v1/registry", api_registry)
    app.router.add_post("/api/v1/registry/install", api_registry_install)
    app.router.add_get("/api/v1/plugins/{pid}/page", api_plugin_page)
    app.router.add_get("/plugins-asset/{pid}/{path:.+}", plugin_asset)
    app.router.add_post("/api/v1/plugins/{pid}", api_plugin_set)
    app.router.add_delete("/api/v1/plugins/{pid}", api_plugin_remove)
    app.router.add_post("/api/v1/widgets", api_widgets)
    app.router.add_get("/plugins-dl/{pid}/plugin", api_plugin_download)
    app.router.add_get("/plugins-dl/{pid}/trust/{name}", api_plugin_trust)
    app.router.add_get("/plugins-dl/developers.json", api_developers_list)
    app.router.add_get("/plugins-dl/developers.json.sig", api_developers_list)
    app.router.add_get("/api/v1/tiles", api_tiles)
    app.router.add_get("/api/v1/eq", api_eq)
    app.router.add_post("/api/v1/eq", api_eq)
    app.router.add_get("/api/cover", cover)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/push/key", push_key)
    app.router.add_post("/api/push/subscribe", push_sub)
    app.router.add_post("/api/push/unsubscribe", push_sub)
    app.router.add_post("/api/push/test", push_test)
    app.router.add_get("/lostos-ca.crt", ca_cert)
    app.router.add_get("/api/backup", backup)
    app.router.add_post("/api/restore", restore)
    app.router.add_post("/api/do", do)
