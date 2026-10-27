"""Preview of the lostOS panel without the voice server: the real panel.py on a fake `m` with demo data.
Run: python panel_dev.py  ->  http://localhost:8766/   (PORT env changes it; DEMO=empty shows the idle state; HOST=0.0.0.0 opens it on a phone)"""
import asyncio
import io
import os
import sys
import time
from types import SimpleNamespace

from aiohttp import web

import tempfile
os.environ["LOSTOS_SCEN_FILE"] = os.path.join(tempfile.gettempdir(), "lostos_demo_scenarios.json")      # the demo never touches the real scenarios
import panel
from scenarios import Scenarios

PORT = int(os.environ.get("PORT", "8766"))
EMPTY = os.environ.get("DEMO") == "empty"

TRACKS = [("Midnight City", "M83", (250, 45, 72), (60, 20, 140)), ("Blinding Lights", "The Weeknd", (255, 140, 40), (120, 10, 40)),
          ("Instant Crush", "Daft Punk, Julian Casablancas", (40, 190, 220), (20, 40, 110))]
S = {"i": 0, "playing": not EMPTY, "t0": time.time(), "base": 41000, "liked": False}


def cover_png(i: int) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter
    a, b = TRACKS[i % len(TRACKS)][2:]
    n = 640
    img = Image.new("RGB", (n, n))
    d = ImageDraw.Draw(img)
    for k in range(2 * n):
        t = k / (2 * n)
        d.line([(k, 0), (0, k)], fill=tuple(int(a[j] + (b[j] - a[j]) * t) for j in range(3)), width=2)
    ov = Image.new("RGB", (n, n), (0, 0, 0))
    ImageDraw.Draw(ov).ellipse([n * .2, n * .2, n * .8, n * .8], fill=(255, 255, 255))
    img = Image.blend(img, Image.composite(Image.new("RGB", (n, n), (255, 255, 255)), img, ov.convert("L").filter(ImageFilter.GaussianBlur(60))), 0.28)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Spot:
    available = True
    device_name = "lostOS"

    async def state(self):
        if EMPTY:
            return {}
        title, artist, _, _ = TRACKS[S["i"] % len(TRACKS)]
        prog = S["base"] + (int((time.time() - S["t0"]) * 1000) if S["playing"] else 0)
        return {"is_playing": S["playing"], "progress_ms": prog % 210000, "device": {"name": "lostOS"},
                "item": {"name": title, "artists": [{"name": a} for a in artist.split(", ")], "duration_ms": 210000,
                         "album": {"images": [{"url": f"/dev-cover/{S['i'] % len(TRACKS)}.png?ab67616d0000b273"}]}}}


class Rem:
    def __init__(self):
        now = time.time()
        self.items = [] if EMPTY else [
            {"id": "a1", "kind": "alarm", "time": "07:30", "repeat": "weekdays", "label": "Работа", "enabled": True, "snooze_min": 9, "at": now + 3600 * 9},
            {"id": "a2", "kind": "alarm", "time": "09:15", "repeat": "days:сб,вс", "label": "", "enabled": False, "snooze_min": 0, "at": now + 3600 * 40},
            {"id": "a3", "kind": "alarm", "time": "22:45", "repeat": "once", "label": "Таблетки", "enabled": True, "snooze_min": 5, "at": now + 3600 * 3},
            {"id": "t1", "kind": "timer", "label": "чай", "total": 600, "at": now + 540},
            {"id": "r1", "kind": "reminder", "text": "Позвонить маме", "label": "Позвонить маме", "repeat": "once", "at": now + 3600 * 2}]

    def describe(self, i):
        return f"{i['kind']} {i.get('time', '')} {i.get('label', '')}"

REM = Rem()
SELECTS = {"s1": {"name": "Яндекс Станция Мини 3 Режим часов", "options": ["Цифровые", "Аналоговые", "Эквалайзер"], "key": 1},
           "s2": {"name": "Яндекс Станция Мини 3 Подсветка", "options": ["Выкл", "Пульс", "Радуга", "Спектр"], "key": 2}}
class FakeHA:
    configured = True
    error = ""

    def __init__(self):
        self.items = [
            {"id": "light.zal_lenta", "name": "Лента в зале", "area": "Зал", "state": "on", "bri": 255, "rgb": [138, 98, 232], "modes": ["rgb"], "kelvin": None, "kmin": None, "kmax": None, "color": True, "ct": False, "dim": True, "bri_pct": 100},
            {"id": "light.koridor_lenta", "name": "Лента в коридоре", "area": "Коридор", "state": "off", "bri": None, "rgb": None, "modes": ["rgb"], "kelvin": None, "kmin": None, "kmax": None, "color": True, "ct": False, "dim": True, "bri_pct": None},
            {"id": "switch.kettle", "name": "Чайник", "area": "Кухня", "state": "off", "color": False, "ct": False, "dim": False, "bri_pct": None}]

    async def scenes(self):
        return [{"id": "scene.movie", "name": "Кино", "area": "Зал"}, {"id": "script.goodnight", "name": "Спокойной ночи", "area": ""}]

    async def devices(self, max_age=2.0):
        return self.items

    async def call(self, ids, service, data):
        for d in self.items:
            if d["id"] in ids:
                d["state"] = "off" if service == "turn_off" else ("on" if service == "turn_on" else ("off" if d["state"] == "on" else "on"))
                if "brightness_pct" in data:
                    d["bri_pct"] = data["brightness_pct"]
                elif d["state"] == "on" and d["id"].startswith("light.") and d["bri_pct"] is None:
                    d["bri_pct"] = 100
                if "rgb_color" in data:
                    d["rgb"] = data["rgb_color"]
        return "готово: " + ", ".join(ids)

m = SimpleNamespace(
    asyncio=asyncio, SPOT=Spot(), HA=FakeHA(), SCEN=Scenarios(), SLEEP={"until": 0.0, "minutes": 0, "smooth": True, "task": None}, SPEAKER="127.0.0.1", NOWP={"text": "M83 — Midnight City · AirPlay" if not EMPTY else "Ничего не играет"}, SPOT_NOW={"text": ""}, SPOT_COVER={"text": ""}, AIR_NOW={"text": ""},
    BT={"key": None, "on": True, "connected": False, "playing": False, "now": ""},
    PANEL={"cli": object(), "selects": SELECTS, "state": {1: "Эквалайзер", 2: "Радуга"}, "volume": 0.34},
    WIFI={"text": "cur\tHome-5G\t192.168.1.20\tconnected\t\tОК\nnet\t0\tHome-5G\t[CURRENT]\nnet\t1\tOffice\t[DISABLED]\nap\tNeighbour\t-71\twpa2\nap\tCafe Free\t-80\topen"},
    VOICES=SimpleNamespace(people={"Артём": {"n": 6, "note": "хозяин, любит Daft Punk"}, "Маша": {"n": 3, "note": ""}}, delete=lambda n: None, rename=lambda a, b: None, save=lambda: None),
    SESSION={"emb": object(), "t": time.time(), "name": "Артём"}, SESSION_S=120,
    SETTINGS={"barge_in": True}, save_settings=lambda: None, LAST={} if EMPTY else {"t": "21:14:05", "who": "Артём", "score": 0.52, "text": "Алиса, включи что-нибудь спокойное", "pre": "", "outcome": "ответила"},
    REM=REM, CONTEXT={"line": "Вечер, +12°, переменная облачность. Дома: Артём."},
    DIALOG=[] if EMPTY else [{"t": "21:14", "heard": "Алиса, который час?", "reply": "Четырнадцать минут десятого."},
                             {"t": "21:09", "heard": "поставь таймер на девять минут", "reply": "Поставила таймер на девять минут."}],
    LOG_RING=[f"21:14:{i:02d} [dev] demo log line {i}" for i in range(24)], PENDING_BT={}, PENDING_DISPLAY={}, ENROLL_PENDING={}, state_services={})


E = {"stt": "yandex", "tts": "alice", "voice": "shitova", "whisper": {"installed": False, "busy": False, "text": ""}, "silero": {"installed": True, "busy": False, "text": ""}}


def engines_state():
    return {**{k: E[k] for k in ("stt", "tts", "voice")}, "voices": {"shitova": "Алиса", "oksana": "Оксана", "jane": "Джейн", "zahar": "Захар"},
            "whisper": {**E["whisper"], "model": "x"}, "silero": E["silero"]}


def engine_set(kind, value):
    if value == "whisper" and not E["whisper"]["installed"]:
        return "Whisper не установлен: сначала нажми «Установить»"
    E[{"stt": "stt", "tts": "tts", "voice": "voice"}[kind]] = value
    return "ok"


def engine_install(name):
    import threading
    def work():
        E[name]["busy"] = True; E[name]["text"] = "Скачиваю модель…"; time.sleep(6)
        E[name].update(busy=False, text="", installed=True)
    threading.Thread(target=work, daemon=True).start()
    return "ok"

async def health_check(force=False):
    return [{"name": "Колонка", "ok": True, "detail": "на связи", "ms": 5}, {"name": "Облако Яндекса: озвучка", "ok": True, "detail": "голос Алисы отвечает", "ms": 840},
            {"name": "Облако Яндекса: распознавание", "ok": True, "detail": "распознавание отвечает", "ms": 650}, {"name": "Gemini", "ok": False, "detail": "User location is not supported for the API use.", "ms": 420},
            {"name": "Home Assistant", "ok": True, "detail": "3 устройства", "ms": 190}, {"name": "Whisper (локально)", "ok": False, "optional": True, "detail": "не установлен (необязателен)", "ms": 0}]


async def run_tool(name, args, trusted=False):
    if name == "music_control":
        a = args.get("action")
        if a == "next":
            S.update(i=S["i"] + 1, base=0, t0=time.time())
        elif a == "previous":
            S.update(i=max(0, S["i"] - 1), base=0, t0=time.time())
        else:
            S["base"] = S["base"] + (int((time.time() - S["t0"]) * 1000) if S["playing"] else 0)
            S.update(playing=(a == "play"), t0=time.time())
    elif name == "set_volume":
        m.PANEL["volume"] = args["level"] / 100
    elif name == "_bluetooth_now":
        m.BT["on"] = args["action"] == "on"
    elif name == "_airplay_now":
        pass
    elif name == "set_timer":
        REM.items.append({"id": f"t{int(time.time())}", "kind": "timer", "label": args.get("label", ""), "total": int(args["minutes"] * 60), "at": time.time() + args["minutes"] * 60})
    elif name == "set_alarm":
        enabled = True
        if args.get("replace_id"):
            old = next((i for i in REM.items if i["id"] == args["replace_id"]), None)
            if old:
                enabled = old.get("enabled", True)
                REM.items.remove(old)
        REM.items.append({"id": f"a{int(time.time())}", "kind": "alarm", "time": args["time"], "repeat": args.get("repeat", "once"), "label": args.get("label", ""),
                          "enabled": enabled, "snooze_min": args.get("snooze_minutes", 5), "at": time.time() + 3600 * 5})
    elif name == "set_reminder":
        REM.items.append({"id": f"r{int(time.time())}", "kind": "reminder", "text": args["text"], "label": args["text"], "repeat": args.get("repeat", "once"),
                          "at": time.time() + (args.get("in_minutes") or 600) * 60})
    elif name == "_sleep_start":
        m.SLEEP.update(until=time.time() + args["minutes"] * 60, minutes=args["minutes"], smooth=args["smooth"])
    elif name == "_sleep_cancel":
        m.SLEEP["until"] = 0.0
    elif name == "_scenario_run":
        print("demo: scenario run", args)
    elif name == "_alarm_toggle":
        for i in REM.items:
            if i["id"] == args["id"]:
                i["enabled"] = bool(args["on"])
    elif name == "cancel_reminder":
        REM.items[:] = [i for i in REM.items if i["id"] != args["which"]]
    return "ok"


m.run_tool = run_tool
m.health_check = health_check
m.log = print
m.HTTPS = {"on": True, "port": 8443}
m.ALICE_VOICES = {"shitova": "Алиса"}
m.NOTIFS = [{"id": 1, "t": time.time() - 600, "kind": "alarm", "title": "Будильник", "text": "Работа"}, {"id": 2, "t": time.time() - 120, "kind": "warn", "title": "Нейросеть не отвечает", "text": "User location is not supported"}, {"id": 3, "t": time.time() - 30, "kind": "scenario", "title": "Сценарий «Спокойной ночи»", "text": "по расписанию"}]
m.engines_state, m.engine_set, m.engine_install = engines_state, engine_set, engine_install


def main():
    app = web.Application()
    panel.register(app, m)
    import ai_settings
    os.environ.setdefault("GEMINI_API_KEY", "dev")
    m.SYSSTAT = {"key": None, "d": {}, "s": {"code": os.environ.get("CODE", "bergamot"), "model": "Yandex Station Mini 3"}, "t": time.time()}
    m.PHASE = {"p": os.environ.get("PHASE", "idle"), "until": time.time() + 3600}
    m.CLOCKTXT = {"text": "", "until": 0.0}
    m.DISPLAY = {"key": None, "f": os.environ.get("FRAME", ""), "t": time.time() if os.environ.get("FRAME") else 0.0}
    m.GEMINI_BEST = {"m": None}
    m.default_persona = lambda: "Ты Алиса, домашний голосовой помощник. Отвечай коротко и по делу, обращайся на «ты»."
    m.system_prompt = lambda: m.default_persona() + " Контекст: " + m.CONTEXT["line"]

    async def refresh_context():
        pass
    m.refresh_context = refresh_context
    ai_settings.install(app, m)

    import brains
    setup = {"done": os.environ.get("DEMO") != "setup", "brain": ""}

    async def setup_get(req):
        return web.json_response({
            "available": True, "done": setup["done"], "public_url": "https://example.test", "station": {"id": "aaa35e631d626784469e", "name": "Яндекс Станция Мини 3", "confirmed": True, "online": True},
            "stations": 1, "vault_locked": False, "brain": {"providers": brains.listing(setup["brain"]), "selected": setup["brain"]}, "search": {"on": True, "tavily": False},
            "style": "normal", "commands": [{"name": "get_weather", "title": "Погода", "about": "Погода по городу", "plugin": "", "on": True}],
            "spotify": {"redirect": "https://example.test/spotify/callback", "client_id": False, "logged_in": False},
            "ha": {"token": False, "room": False, "reachable": False, "configured": False, "error": ""}, "city": None, "voices": 0, "served": ""})

    async def setup_post(req):
        b = await req.json()
        if "done" in b:
            setup["done"] = bool(b["done"])
        if "brain" in b:
            setup["brain"] = b["brain"]
        return web.json_response({"ok": True})
    app.router.add_get("/api/v1/setup", setup_get)
    app.router.add_post("/api/v1/setup", setup_post)

    async def cover(req):
        return web.Response(body=cover_png(int(req.match_info["i"])), content_type="image/png")

    app.router.add_get("/dev-cover/{i}.png", cover)
    host = os.environ.get("HOST", "127.0.0.1")      # HOST=0.0.0.0 to open the preview on a phone in the same Wi-Fi
    web.run_app(app, host=host, port=PORT, print=lambda *_: print(f"lostOS panel preview: http://{'localhost' if host == '127.0.0.1' else host}:{PORT}/"))


if __name__ == "__main__":
    sys.exit(main())
