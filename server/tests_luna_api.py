"""The Luna-compatible API (luna_api.py) against a stand-in speaker. Run from the server folder."""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import luna_api

OK = True


def check(name, cond):
    global OK
    print(("PASS " if cond else "FAIL ") + name)
    OK = OK and bool(cond)


class Cli:
    def __init__(self):
        self.calls = []
        self.rec = None
        self.pcm = bytes(3200)

    async def execute_service(self, svc, args):
        self.calls.append((svc, args))
        if args.get("command") == "listen" and self.rec is not None:       # the speaker listened and the dialog gave the phrase to the record
            self.rec["fut"].set_result(self.pcm)


async def main():
    cli = Cli()
    tools = []

    async def run_tool(name, args, **kw):
        tools.append((name, args))
        return "ok"

    said = []

    async def stt(params):
        return {"text": "привет"}

    async def speak(text):
        said.append(text)

    async def think(text):
        return "ответ на " + text

    import tempfile
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "web"))
    open(os.path.join(tmp, "web", "index.html"), "w").write("<h1>hi</h1>")
    open(os.path.join(tmp, "secret.txt"), "w").write("no")
    plug = types.SimpleNamespace(id="a.b", folder=tmp, m={"name": "A", "version": "1.0.0", "system": False})
    plugins_ns = types.SimpleNamespace(items={"a.b": plug}, state={"a.b": {"enabled": True}})
    m = types.SimpleNamespace(GLOBALS={"speak": speak}, think=think, PLUGINS=plugins_ns, SYSSTAT={"s": {"code": "bergamot", "name": "x", "ring": "6", "logo": "0", "clock": "1"}, "d": {}}, state_services={"led_show": "L", "clock_show_text": "C", "assistant": "A"},
                              PANEL={"cli": cli, "volume": 0.3}, LUNA_REC={"fut": None}, LUNA_SPEECH={"w": []}, alice_api_stt=stt, journal=lambda *a, **k: None, run_tool=run_tool, MEDIA={"t": 0, "now": {}})
    app = web.Application()
    luna_api.register(app, m, full=True)
    async with TestClient(TestServer(app)) as c:
        d = await (await c.get("/luna/api/device")).json()
        check("device: model, ring, clock", d["model"] == "bergamot" and d["led"]["ring_count"] == 6 and "display.clock.segment.v1" in d["features"] and "led.ring.rgb" in d["features"] and d["source"] == "lostos")
        r = await c.post("/luna/api/led/ring", json={"colors": ["#ff0000", "#0000ff"]})
        check("led ring: the mean colour goes to the speaker", r.status == 200 and cli.calls[-1] == ("L", {"effect": "None", "color": "#7f007f", "seconds": 30}))
        check("led: more colours than LEDs is 400", (await c.post("/luna/api/led/ring", json={"colors": ["#ffffff"] * 7})).status == 400)
        m.SYSSTAT["s"]["ring"] = "0"
        check("led ring: a station without a ring refuses it", (await c.post("/luna/api/led/ring", json={"colors": ["#ffffff"]})).status == 400)
        m.SYSSTAT["s"]["ring"] = "6"
        check("led: a bad colour is 400", (await c.post("/luna/api/led", json={"colors": ["red"]})).status == 400)
        check("led logo: this speaker has none", (await c.post("/luna/api/led/logo", json={"colors": ["#ffffff"]})).status == 400)
        check("led off", (await c.post("/luna/api/led/off")).status == 200 and cli.calls[-1][1]["color"] == "#000000")
        check("clock text", (await c.post("/luna/api/clock/text", json={"text": "HELLO", "duration_ms": 2500})).status == 200 and cli.calls[-1] == ("C", {"text": "HELLO", "seconds": 3}))
        check("clock time", (await c.post("/luna/api/clock", json={"time": "12:34", "brightness": 100})).status == 200 and cli.calls[-1][1]["text"] == "12:34")
        check("clock text: not ASCII is 400", (await c.post("/luna/api/clock/text", json={"text": "привет"})).status == 400)
        check("volume get", (await (await c.get("/luna/api/volume/get")).json())["percent"] == 30)
        check("volume set", (await c.post("/luna/api/volume/set", json={"percent": 55})).status == 200 and tools[-1] == ("set_volume", {"level": 55}))
        check("volume set: out of range is 400", (await c.post("/luna/api/volume/set", json={"percent": 300})).status == 400)
        check("things the speaker cannot do answer 501", (await c.post("/luna/api/audio/test")).status == 501 and (await c.post("/luna/api/bluetooth/pair", json={"address": "AA:BB"})).status == 501)
        tabs = await (await c.get("/luna/api/custom-web")).json()
        check("custom-web lists the pages of plugins", tabs == [{"id": "a.b", "url": "/luna/web/a.b/web/index.html"}])
        r = await c.get("/luna/web/a.b/web/index.html")
        check("a page of a plugin is served in a sandbox", r.status == 200 and "sandbox" in r.headers["Content-Security-Policy"] and (await r.text()) == "<h1>hi</h1>")
        check("a file outside web/ is not served", (await c.get("/luna/web/a.b/secret.txt")).status == 404 and (await c.get("/luna/web/a.b/web/../secret.txt")).status == 404)
        page = await (await c.get("/luna/")).text()
        check("the panel is served with the API address changed", "const api = '/luna/api';" in page and "const api = '/api';" not in page)
        check("speech goes to the speaker voice", (await c.post("/luna/api/lostos/say", json={"text": "привет"})).status == 200 and said[-1] == "привет")
        r = await c.post("/luna/api/lostos/command", json={"text": "который час"})
        check("command: the assistant answers aloud", r.status == 200 and said[-1] == "ответ на который час" and (await r.json())["reply"] == "ответ на который час")
        check("speech: empty text is 400", (await c.post("/luna/api/lostos/say", json={"text": " "})).status == 400)
        rows = await (await c.get("/luna/api/plugins")).json()
        check("plugins: the list", rows == [{"id": "a.b", "name": "A", "version": "1.0.0", "enabled": True, "source": "lostos"}])
        m.BT = {"on": True, "connected": False, "playing": False, "now": ""}
        st = await (await c.get("/luna/api/bluetooth/status")).json()
        check("bluetooth status", st["power"] is True and st["connected"] is False)
        check("bluetooth power goes through the same call as the panel", (await c.put("/luna/api/bluetooth/power", json={"on": False})).status == 200 and tools[-1] == ("_bluetooth_now", {"action": "off"}))
        cli.rec = m.LUNA_REC
        r = await c.post("/luna/api/microphone/record", json={"seconds": 10})
        body = await r.read()
        check("microphone/record gives the phrase as a WAV and asks the speaker to listen", r.status == 200 and body[:4] == b"RIFF" and cli.calls[-1] == ("A", {"command": "listen"}))
        check("a second record within 30 s is refused", (await c.post("/luna/api/microphone/record", json={"seconds": 10})).status == 429)
        async def phrase():
            await asyncio.sleep(0.2)
            for fut, cancel in m.LUNA_SPEECH["w"]:
                fut.set_result("включи свет")

        asyncio.ensure_future(phrase())
        r = await c.get("/luna/api/alice/speech?timeout_ms=3000")
        check("alice/speech waits for the next phrase", r.status == 200 and (await r.json())["transcript"] == "включи свет")
        check("alice/speech: timeout is 408", (await c.get("/luna/api/alice/command?timeout_ms=100")).status == 408)
        check("alice/speech/cancel stops the dialog", (await c.post("/luna/api/alice/speech/cancel")).status == 200 and cli.calls[-1] == ("A", {"command": "interrupt"}))
        check("microphone/record needs seconds 1..30", (await c.post("/luna/api/microphone/record", json={"seconds": 99})).status == 400)
        check("microphone/devices", (await (await c.get("/luna/api/microphone/devices")).json())[0]["id"] == "default")
        m.PANEL["cli"] = None
        check("no speaker: 503", (await c.post("/luna/api/led", json={"colors": ["#ffffff"]})).status == 503)


async def recognize():
    cli = Cli()
    m = types.SimpleNamespace(GLOBALS={}, SYSSTAT={"s": {}, "d": {}}, state_services={"assistant": "A"}, PANEL={"cli": cli}, LUNA_REC={"fut": None}, LUNA_SPEECH={"w": []},
                              journal=lambda *a, **k: None, alice_api_stt=None)

    async def stt(params):
        return {"text": "привет"}

    m.alice_api_stt = stt
    cli.rec = m.LUNA_REC
    app = web.Application()
    luna_api.register(app, m, full=True)
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/luna/api/alice/recognize")
        check("alice/recognize listens and returns the text", r.status == 200 and (await r.json()) == {"transcript": "привет", "source": "voice", "final": True} and cli.calls[-1] == ("A", {"command": "listen"}))


async def default_is_off():
    m = types.SimpleNamespace(PLUGINS=types.SimpleNamespace(items={}, state={}))
    app = web.Application()
    luna_api.register(app, m)
    async with TestClient(TestServer(app)) as c:
        check("by default Luna's own panel and API are not served", (await c.get("/luna/")).status == 404 and (await c.get("/luna/api/device")).status == 404)
        check("the pages of plugins still are (404 for a plugin that is not there)", (await c.get("/luna/web/a.b/web/index.html")).status == 404)


asyncio.run(main())
asyncio.run(recognize())
asyncio.run(default_is_off())
print("OK" if OK else "FAILED")
sys.exit(0 if OK else 1)
