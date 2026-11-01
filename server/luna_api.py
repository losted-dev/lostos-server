"""The HTTP API of the Luna framework (docs/API.md of nyakokitsu/luna), as far as a lostOS speaker can do it, so that the web panel of Luna and tools written for its API work with this server.

Under /luna/api/... (the same login as the panel; the owner only); the web panel of Luna itself is /luna/ (web/luna/index.html, with the developer's permission, see web/luna/NOTICE).
What the speaker cannot do answers 501 {"error": ...} (a test sound from a file, bluetooth, waiting for a key press, the microphone, Lua files, packages of Luna).
The lights and the clock go to the speaker as its own actions (led_show, clock_show_text), the volume and the music through the same calls as the panel."""
import json
import math
import os
import re

from aiohttp import web

COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
NOT_HERE = "этого у колонки lostOS нет"


def register(app: web.Application, m, full: bool = None):
    if full is None:
        full = os.environ.get("LOSTOS_LUNA_PANEL") == "1"
    last_rec = {"t": 0.0}
    room_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "luna_room.json")

    def info() -> dict:
        s, d = m.SYSSTAT["s"], m.SYSSTAT["d"]
        num = lambda k: int(float(d.get(k) if d.get(k) is not None else s.get(k) or 0))
        return {"model": s.get("code", ""), "name": s.get("name", ""), "ring": num("ring"), "logo": num("logo"), "clock": num("clock") == 1}

    def err(text: str, status: int = 400):
        return web.json_response({"error": text}, status=status)

    async def send(action: str, args: dict):
        svc, cli = m.state_services.get(action), m.PANEL.get("cli")
        if not (svc and cli):
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": "колонка не на связи"}), content_type="application/json")
        await cli.execute_service(svc, args)

    async def api_device(req):
        i = info()
        features = (["led.ring.rgb"] if i["ring"] else []) + (["led.logo"] if i["logo"] else []) + (["display.clock.segment.v1"] if i["clock"] else [])
        room = ""
        try:
            room = json.load(open(room_file, encoding="utf-8")).get("room") or ""
        except (OSError, ValueError):
            pass
        return web.json_response({"model": i["model"], "architecture": "aarch64", "features": features, "source": "lostos",
                                  "led": {"ring_count": i["ring"], "has_logo": i["logo"] > 0, "logo_count": i["logo"]}, "room": room or None})

    async def api_room(req):
        room = ""
        try:
            room = json.load(open(room_file, encoding="utf-8")).get("room") or ""
        except (OSError, ValueError):
            pass
        if req.method == "PUT":
            try:
                body = await req.json()
            except ValueError:
                return err("нужен JSON")
            room = str(body.get("room") or "").strip()
            if len(room) > 64:
                return err("room: до 64 символов")
            os.makedirs(os.path.dirname(room_file), exist_ok=True)
            json.dump({"room": room}, open(room_file, "w", encoding="utf-8"))
        return web.json_response({"room": room or None})

    async def api_settings(req):
        return web.json_response({"led": {}, "web": {"ui": True, "api": True}, "lua": {"enabled": False}, "updates": {}})

    async def api_sensors(req):
        return web.json_response({})

    def colors(body: dict, limit: int):
        c = body.get("colors")
        if not isinstance(c, list) or not c or not all(isinstance(x, str) and COLOR.match(x) for x in c):
            raise web.HTTPBadRequest(text=json.dumps({"error": "colors: список цветов #RRGGBB"}), content_type="application/json")
        if limit and len(c) > limit:
            raise web.HTTPBadRequest(text=json.dumps({"error": "цветов больше, чем светодиодов (%d)" % limit}), content_type="application/json")
        return c

    def mean(c: list) -> str:
        rgb = [sum(int(x[i:i + 2], 16) for x in c) // len(c) for i in (1, 3, 5)]   # one colour for the whole ring: the mean of the list
        return "#%02x%02x%02x" % tuple(rgb)

    async def api_led(req):
        i = info()
        zone = req.match_info.get("zone", "")
        if req.path.endswith("/off") or req.path.endswith("/off/ring") or req.path.endswith("/off/logo"):
            await send("led_show", {"effect": "None", "color": "#000000", "seconds": 1})
            return web.json_response({"ok": True})
        try:
            body = await req.json()
        except ValueError:
            return err("нужен JSON {colors}")
        if zone == "ring" and not i["ring"]:                      # as Luna 0.2.12: a station without a ring refuses the ring endpoint (a logo-only one does not write its logo through it)
            return err("у этой колонки нет кольца")
        limit = i["ring"] if zone == "ring" else i["logo"] if zone == "logo" else 0
        if zone == "logo" and not i["logo"]:
            return err("у этой колонки нет логотипа")
        await send("led_show", {"effect": "None", "color": mean(colors(body, limit)), "seconds": 30})
        return web.json_response({"ok": True})

    async def api_clock(req):
        i = info()
        if req.method == "GET":
            return web.json_response({"available": i["clock"], "text_available": i["clock"], "segments": []})
        if not i["clock"]:
            return err("у этой колонки нет часов", 400)
        if req.path.endswith("/off"):
            await send("clock_show_text", {"text": "", "seconds": 1})
            return web.json_response({"ok": True})
        try:
            body = await req.json()
        except ValueError:
            return err("нужен JSON")
        if req.path.endswith("/text"):
            text = str(body.get("text", ""))
            if not text or len(text) > 32 or not text.isascii():
                return err("text: 1–32 символа ASCII")
            seconds = max(1, min(15, math.ceil(int(body.get("duration_ms", 2000)) / 1000)))
            await send("clock_show_text", {"text": text, "seconds": seconds})
        else:
            if "time" not in body or not re.fullmatch(r"\d{1,2}:\d{2}", str(body["time"])):
                return err("time: ЧЧ:ММ (сегменты не поддерживаются)")
            await send("clock_show_text", {"text": str(body["time"]), "seconds": 15})
        return web.json_response({"ok": True})

    async def api_volume(req):
        act = req.match_info.get("act") or ("get" if req.method == "GET" else "set")   # /api/volume: GET reads, POST sets
        if act == "get":
            v = m.PANEL.get("volume")
            return web.json_response({"percent": None if v is None else round(float(v) * 100)})
        if act == "set":
            try:
                p = int((await req.json())["percent"])
            except (ValueError, KeyError, TypeError):
                return err("percent: 0–100")
            if not 0 <= p <= 100:
                return err("percent: 0–100")
            await m.run_tool("set_volume", {"level": p})
            return web.json_response({"percent": p})
        if act in ("up", "down"):
            await m.run_tool("set_volume", {"change": 5 if act == "up" else -5})
            return web.json_response({"ok": True})
        return err(NOT_HERE, 501)

    async def api_media(req):
        if req.method == "GET":
            now = (getattr(m, "MEDIA", None) or {}).get("now") or {}
            return web.json_response({"playing": bool(now.get("playing")), "title": now.get("title", ""), "source": now.get("source", "")})
        if req.path.endswith("/command"):
            import media
            try:
                act = str((await req.json()).get("action", ""))
                await media.command(m, {"pause": "pause", "play": "play", "next": "next", "previous": "previous"}[act], {})
            except (ValueError, KeyError):
                return err("action: play, pause, next или previous")
            return web.json_response({"ok": True}, status=202)
        return err(NOT_HERE, 501)

    async def api_say(req):
        """POST {"text"} (ours, /luna/api/lostos/say): the speaker says it with the voice of the assistant."""
        try:
            text = str((await req.json()).get("text", "")).strip()
        except ValueError:
            return err("нужен JSON {text}")
        if not text or len(text) > 1000:
            return err("text: 1–1000 символов")
        speak = m.GLOBALS.get("speak")
        if not speak:
            return err("колонка не на связи", 503)
        await speak(text)
        return web.json_response({"ok": True})

    async def api_ask(req):
        """POST {"text"} (ours, /luna/api/lostos/command): the text is a voice command: the assistant answers it and does what it says."""
        try:
            text = str((await req.json()).get("text", "")).strip()
        except ValueError:
            return err("нужен JSON {text}")
        if not text or len(text) > 500:
            return err("text: 1–500 символов")
        speak = m.GLOBALS.get("speak")
        if not speak:
            return err("колонка не на связи", 503)
        reply = await m.think(text)
        await speak(reply)
        return web.json_response({"ok": True, "reply": reply})

    def timeout_ms(req) -> float:
        try:
            return max(0, min(60000, int(req.query.get("timeout_ms", 60000)))) / 1000
        except ValueError:
            return 60.0

    async def api_alice_wait(req):
        """GET ?timeout_ms=&cancel=: waits for the next phrase the speaker hears (after the wake word) and returns {"transcript", "source", "final"}; with cancel=true the assistant
        does not answer that phrase."""
        import asyncio
        fut = asyncio.get_running_loop().create_future()
        entry = (fut, req.query.get("cancel", "").lower() in ("1", "true", "yes"))
        m.LUNA_SPEECH["w"].append(entry)
        try:
            text = await asyncio.wait_for(fut, timeout=timeout_ms(req))
        except asyncio.TimeoutError:
            if entry in m.LUNA_SPEECH["w"]:
                m.LUNA_SPEECH["w"].remove(entry)
            return err("timeout waiting for speech", 408)
        return web.json_response({"transcript": text, "source": "voice", "final": True})

    async def listen_pcm(why: str):
        """The speaker listens (chime and ring as for the wake word, never with the microphones muted) and the next phrase comes back as PCM (16 kHz, 16 bit, mono) or b"".
        Raises web errors for a refusal. Once in 30 seconds, written to the journal."""
        import asyncio
        import time
        if time.time() - last_rec["t"] < 30:
            raise web.HTTPTooManyRequests(text=json.dumps({"error": "запись не чаще раза в 30 секунд"}), content_type="application/json")
        svc, cli = m.state_services.get("assistant"), m.PANEL.get("cli")
        if not (svc and cli):
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": "колонка не на связи"}), content_type="application/json")
        if m.LUNA_REC["fut"] is not None and not m.LUNA_REC["fut"].done():
            raise web.HTTPConflict(text=json.dumps({"error": "колонка уже слушает"}), content_type="application/json")
        last_rec["t"] = time.time()
        fut = asyncio.get_running_loop().create_future()
        m.LUNA_REC["fut"] = fut
        m.journal(why)
        await cli.execute_service(svc, {"command": "listen"})
        try:
            return await asyncio.wait_for(fut, timeout=20)
        except asyncio.TimeoutError:
            m.LUNA_REC["fut"] = None
            raise web.HTTPConflict(text=json.dumps({"error": "колонка не начала слушать (микрофоны выключены или занята)"}), content_type="application/json")

    async def api_recognize(req):
        """POST: the speaker listens to one phrase and the answer is its text, without a dialog with the assistant: {"transcript", "source", "final"}."""
        pcm = await listen_pcm("распознавание речи по API Luna")
        if not pcm:
            return err("timeout waiting for recognition", 408)
        import base64
        try:
            res = await m.alice_api_stt({"pcm_b64": base64.b64encode(pcm).decode()})
        except (ValueError, RuntimeError) as e:
            return err(str(e), 503)
        return web.json_response({"transcript": res.get("text", ""), "source": "voice", "final": True})

    async def api_alice_cancel(req):
        svc, cli = m.state_services.get("assistant"), m.PANEL.get("cli")
        if not (svc and cli):
            return err("колонка не на связи", 503)
        await cli.execute_service(svc, {"command": "interrupt"})
        return web.json_response({"ok": True})

    async def api_mic_devices(req):
        return web.json_response([{"name": "микрофоны колонки", "id": "default"}])

    def plugin_row(p):
        st = m.PLUGINS.state.get(p.id, {})
        return {"id": p.id, "name": p.m["name"], "version": p.m["version"], "enabled": bool(st.get("enabled")) or p.m["system"], "source": "lostos"}

    async def api_plugins(req):
        return web.json_response([plugin_row(p) for p in m.PLUGINS.items.values()])

    async def api_plugin_toggle(req):
        pid = req.match_info["pid"]
        p = m.PLUGINS.items.get(pid)
        if not p or p.m["system"]:
            return err("нет такого плагина", 404)
        on = not bool(m.PLUGINS.state.get(pid, {}).get("enabled"))
        if on and m.PLUGINS.requirement(p):
            return err(m.PLUGINS.requirement(p), 409)
        await m.PLUGINS.set_enabled(pid, on)
        return web.json_response({"ok": True, "enabled": on})

    async def api_plugin_remove(req):
        ok = await m.PLUGINS.remove(req.match_info["pid"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    def web_pages():
        """Plugins that came with pages (web/*.html): [(id, the first page)], only the switched on ones."""
        out = []
        for p in m.PLUGINS.items.values():
            if p.m["system"] or not m.PLUGINS.state.get(p.id, {}).get("enabled"):
                continue
            wd = os.path.join(p.folder, "web")
            if not os.path.isdir(wd):
                continue
            pages = sorted(f for f in os.listdir(wd) if f.lower().endswith((".html", ".htm")))
            if pages:
                out.append((p.id, "index.html" if "index.html" in pages else pages[0]))
        return out

    async def api_custom_web(req):
        return web.json_response([{"id": pid, "url": f"/luna/web/{pid}/web/{page}"} for pid, page in web_pages()])

    async def plugin_web(req):
        import mimetypes
        pid, rel = req.match_info["pid"], req.match_info["path"]
        p = m.PLUGINS.items.get(pid)
        root = os.path.realpath(os.path.join(p.folder, "web")) if p else ""
        full = os.path.realpath(os.path.join(p.folder, rel)) if p else ""
        if not p or p.m["system"] or not m.PLUGINS.state.get(pid, {}).get("enabled") or not rel.startswith("web/") or not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise web.HTTPNotFound()
        ctype = (mimetypes.guess_type(full)[0] or "application/octet-stream")
        # a page of a plugin is the plugin's own code: it runs in a sandbox without the rights of the panel (no cookies, no requests as the owner)
        return web.Response(body=open(full, "rb").read(), content_type=ctype, charset="utf-8" if ctype.startswith("text/") else None,
                            headers={"Content-Security-Policy": "sandbox allow-scripts allow-forms", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    async def api_bluetooth(req):
        """The speaker is a Bluetooth speaker (a sink): it is switched on and off, and while it is on it can be found and paired from a phone. Looking for other devices and
        pairing with them from here is not what it does (501)."""
        act = req.match_info["act"]
        if act == "status":
            bt = getattr(m, "BT", {})
            return web.json_response({"power": bool(bt.get("on")), "pairable": bool(bt.get("on")), "connected": bool(bt.get("connected")), "playing": bool(bt.get("playing")),
                                      "now": bt.get("now", "")})
        if act in ("power", "pairable") and req.method == "PUT":
            try:
                on = bool((await req.json())["on"])
            except (ValueError, KeyError, TypeError):
                return err("нужен JSON {on: true|false}")
            if act == "pairable" and not on:
                return web.json_response({"ok": True})          # it is visible while it is on: there is no separate switch
            await m.run_tool("_bluetooth_now", {"action": "on" if on else "off"})
            return web.json_response({"ok": True})
        return err("колонка сама ищет и подключает устройства не так: включите Bluetooth и подключитесь с телефона", 501)

    async def api_record(req):
        """POST {"seconds": 1..30, "device"}: the speaker listens (chime and ring, never with the microphones muted) and the next phrase, until the pause, comes back as a WAV
        (16 kHz, 16 bit, mono; "seconds" is the longest it may be). Once in 30 seconds, every record is written to the journal; the owner only."""
        import io
        import wave
        try:
            seconds = int((await req.json()).get("seconds", 10))
        except (ValueError, TypeError, AttributeError):
            return err("нужен JSON {seconds}")
        if not 1 <= seconds <= 30:
            return err("microphone capture duration must be 1..=30 seconds")
        pcm = await listen_pcm("запись с микрофона по API Luna")
        if not pcm:
            return err("ничего не сказано", 404)
        pcm = pcm[:seconds * 16000 * 2]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        return web.Response(body=buf.getvalue(), content_type="audio/wav", headers={"Content-Disposition": "attachment; filename=microphone.wav"})

    async def api_empty(req):
        return web.json_response([])

    async def api_none(req):
        return err(NOT_HERE, 501)

    async def page(req):
        raw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "luna", "index.html"), encoding="utf-8").read()
        raw = raw.replace("const api = '/api';", "const api = '/luna/api';", 1)       # the only change: the panel talks to this server's API
        return web.Response(text=raw, content_type="text/html", headers={"Cache-Control": "no-store"})

    r = app.router
    r.add_get("/luna/web/{pid}/{path:.*}", plugin_web)           # the pages of plugins written for Luna (register.customweb): the one thing a plugin itself needs from here
    if not full:                                                 # compatibility is plugins that run here; Luna's own panel and API are not served unless asked for (LOSTOS_LUNA_PANEL=1)
        return

    async def to_page(req):
        raise web.HTTPFound("/luna/")

    r.add_get("/luna", to_page)
    r.add_get("/luna/", page)
    r.add_get("/luna/api/device", api_device)
    r.add_route("*", "/luna/api/room", api_room)
    r.add_route("*", "/luna/api/settings", api_settings)
    r.add_get("/luna/api/sensors", api_sensors)
    for p in ("/luna/api/led", "/luna/api/led/{zone:ring|logo}", "/luna/api/led/off", "/luna/api/led/off/{zone:ring|logo}"):
        r.add_post(p, api_led)
    r.add_get("/luna/api/clock", api_clock)
    for p in ("/luna/api/clock", "/luna/api/clock/text", "/luna/api/clock/off"):
        r.add_post(p, api_clock)
    r.add_route("*", "/luna/api/volume", api_volume)
    r.add_route("*", "/luna/api/volume/{act}", api_volume)
    r.add_get("/luna/api/media", api_media)
    r.add_post("/luna/api/media/command", api_media)
    r.add_post("/luna/api/microphone/record", api_record)
    r.add_route("*", "/luna/api/bluetooth/{act}", api_bluetooth)
    r.add_get("/luna/api/alice/speech", api_alice_wait)
    r.add_get("/luna/api/alice/command", api_alice_wait)
    r.add_post("/luna/api/alice/recognize", api_recognize)
    r.add_post("/luna/api/alice/speech/cancel", api_alice_cancel)
    r.add_post("/luna/api/lostos/say", api_say)
    r.add_post("/luna/api/lostos/command", api_ask)
    r.add_get("/luna/api/microphone/devices", api_mic_devices)
    r.add_get("/luna/api/plugins", api_plugins)
    r.add_post("/luna/api/plugins/{pid}/toggle", api_plugin_toggle)
    r.add_delete("/luna/api/plugins/{pid}", api_plugin_remove)
    r.add_get("/luna/api/custom-web", api_custom_web)
    for p in ("/luna/api/plugins/sources", "/luna/api/lua/files", "/luna/api/input/devices"):
        r.add_get(p, api_empty)
    r.add_route("*", "/luna/api/{tail:.*}", api_none)       # everything else: said honestly
