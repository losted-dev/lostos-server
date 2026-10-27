"""Checks the interception and brain plugin paths with a stub of the voice server (no speaker, no microphone).
Run: python tests_brain.py   (uses a temporary copy of the plugin folder and its own state file)"""
import asyncio
import os
import shutil
import sys
import tempfile

import plugins

HERE = os.path.dirname(os.path.abspath(__file__))


class Stub:
    PANEL: dict = {}
    logs: list = []

    def log(self, text):
        self.logs.append(text)
        print("  log:", text)

    def notify(self, *a, **k):
        print("  notify:", a)

    def builtin_tool_names(self):
        return {"get_weather"}

    def tools_neutral(self):
        return [{"name": "get_weather", "description": "weather", "parameters": {"type": "object", "properties": {}}}]

    async def run_tool(self, name, args):
        return "+5, пасмурно"


async def main():
    tmp = tempfile.mkdtemp()
    plugins.ROOT = os.path.join(tmp, "plugins")
    plugins.STATE_FILE = os.path.join(tmp, "state.json")
    shutil.copytree(os.path.join(HERE, "plugins", "user", "ru.lostos.test-brain"), os.path.join(plugins.ROOT, "user", "ru.lostos.test-brain"))
    os.makedirs(os.path.join(plugins.ROOT, "system"), exist_ok=True)
    mgr = plugins.Manager(Stub())
    mgr.scan()
    p = mgr.items["ru.lostos.test-brain"]
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    check("switched off: no brain, no interception", mgr.brain() is None and await mgr.intercept("тест перехват раз", "") is None)
    await mgr.set_enabled(p.id, True)
    await asyncio.sleep(1.0)
    check("brain found once allowed", mgr.brain() is p)
    check("phrase taken", await mgr.intercept("Тест перехват, раз два", "Настя") == "Фразу перехватил плагин.")
    check("other phrase passes on", await mgr.intercept("включи свет", "") is None)
    res = await mgr.ask(p, "brain.respond", {"text": "привет", "tools": mgr.srv.tools_neutral(), "history": []}, 10)
    check("brain answers", res.startswith("Тестовый мозг слышал: привет"))
    res = await mgr.ask(p, "brain.respond", {"text": "какая погода", "tools": mgr.srv.tools_neutral(), "history": []}, 10)
    check("brain runs a command", "пасмурно" in res)
    try:
        await mgr.ask(p, "unknown.method", {}, 5)
        check("unknown question refused", False)
    except RuntimeError:
        check("unknown question refused", True)
    # the hub: commands, voices and recognizers from another plugin
    shutil.copytree(os.path.join(HERE, "plugins", "user", "ru.lostos.test-speech"), os.path.join(plugins.ROOT, "user", "ru.lostos.test-speech"))
    mgr.scan()
    q = mgr.items["ru.lostos.test-speech"]
    await mgr.set_enabled(q.id, True)
    await asyncio.sleep(1.0)
    check("plugin command is registered", [d["name"] for _, d in mgr.plugin_tools()] == ["coin_flip"])
    check("plugin command runs", await mgr.run_plugin_tool("coin_flip", {}) in ("орёл", "решка"))
    try:
        await mgr._call(q, "tools.register", {"name": "get_weather", "description": "x"})
        check("a built-in name cannot be taken", False)
    except ValueError:
        check("a built-in name cannot be taken", True)
    try:
        await mgr._call(q, "tools.register", {"name": "Bad Name", "description": "x"})
        check("bad names are refused", False)
    except ValueError:
        check("bad names are refused", True)
    voice = await mgr.ask(q, "tts.synthesize", {"text": "раз два три"}, 5)
    check("voice returns sound", voice["rate"] == 24000 and len(voice["pcm_b64"]) > 1000)
    import base64
    heard = await mgr.ask(q, "stt.transcribe", {"pcm_b64": base64.b64encode(bytes(64000)).decode(), "rate": 16000}, 5)
    check("recognizer answers", "2.0 секунды" in heard["text"])
    check("engines are listed", mgr.engine_plugins("speech.tts") == [{"id": q.id, "name": q.m["name"]}] and mgr.engine_plugin("plugin:" + q.id, "speech.stt") is q
          and mgr.engine_plugin("plugin:nobody", "speech.stt") is None and mgr.engine_plugin("yandex", "speech.stt") is None)
    big = await mgr.ask(q, "tts.synthesize", {"text": " ".join(["слово"] * 40)}, 5)
    check("long answer (>64 KB line) arrives", len(big["pcm_b64"]) > 70000)
    await mgr.set_enabled(q.id, False)
    check("commands vanish with the plugin", mgr.plugin_tools() == [])
    # permission withdrawn: no more questions
    mgr.state[p.id]["granted"] = []
    check("without permission nothing is asked", mgr.brain() is None and await mgr.intercept("тест перехват", "") is None)
    # a plugin that never answers must not hold the phrase for long
    await mgr.halt(p)
    await mgr.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
