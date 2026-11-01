"""The plugins' own UI (manifest "ui", command panel_ui): the manifest check, what the panel gets from a plugin's answer, the call to a plugin on
the speaker and on the server, and that the assistant never sees panel_* commands. Run from the server folder."""
import asyncio
import json
import os
import sys
import tempfile
import types
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugins

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


def manifest(folder, **extra):
    open(os.path.join(folder, "main.js"), "w").close()
    d = {"id": "ru.test.ui", "version": "1.0.0", "name": "Тест", "author": "Тест", "permissions": [], "side": "speaker",
         "device": {"runtime": "js", "binary": "main.js", "permissions": ["assistant.tools"]}, **extra}
    return plugins._valid_manifest(d, "user", folder)


with tempfile.TemporaryDirectory() as folder:
    m, err = manifest(folder, ui={"page": {"title": "Радио", "icon": "radio", "about": "станции"}, "player": {"title": "Станции"}})
    check("a page and a player menu are declared", m and m["ui"] == {"page": {"title": "Радио", "icon": "radio", "about": "станции"}, "player": {"title": "Станции"}}, err)
    m, err = manifest(folder, ui={"page": {"title": "x", "icon": "<script>"}})
    check("an unknown icon becomes the star", m and m["ui"]["page"]["icon"] == "star", err)
    for bad in ({"page": {"title": ""}}, {"page": "Радио"}, {"tab": {}}, {"player": {"title": "x" * 30}}, "ui"):
        m, err = manifest(folder, ui=bad)
        check(f"a wrong ui is refused: {bad!r}"[:70], m is None and err.startswith("ui"), (m, err))
    m, _ = manifest(folder)
    check("no ui: none", m and m["ui"] is None)

d = plugins.sanitize_ui({"title": "Радио" * 20, "toast": "ок", "blocks": [
    {"type": "list", "id": "mine", "items": [{"id": 1, "title": "<b>a</b>", "image": "http://x/y.png", "on": 1, "remove": "yes"}, "junk"]},
    {"type": "script", "text": "alert(1)"}, {"type": "buttons", "items": [{"id": "a", "title": "A", "style": "evil"}]},
    {"type": "search", "id": "q", "value": "j" * 500}, {"type": "toggle", "id": "t", "on": True}, "junk"],
    "items": [{"id": "0", "title": "Станция", "on": True}]})
check("the answer keeps only known blocks", [b["type"] for b in d["blocks"]] == ["list", "buttons", "search", "toggle"], d["blocks"])
check("list items are cut to their fields", d["blocks"][0]["items"] == [{"id": "1", "title": "<b>a</b>", "subtitle": "", "image": "http://x/y.png", "on": True, "remove": True}])
check("an unknown button style is dropped", d["blocks"][1]["items"][0]["style"] == "")
check("long texts are cut", len(d["title"]) == 40 and len(d["blocks"][2]["value"]) == 80)
check("the player menu passes", d["items"][0]["title"] == "Станция" and d["items"][0]["on"] is True)
check("not a dict of blocks: nothing breaks", plugins.sanitize_ui({"blocks": "x"})["blocks"] == [])


async def main():
    with tempfile.TemporaryDirectory() as folder:
        with patch.object(plugins, "USER_ROOT", os.path.join(folder, "user")), patch.object(plugins, "STATE_FILE", os.path.join(folder, "state.json")):
            mgr = plugins.Manager(types.SimpleNamespace(PANEL={"cli": None}, log=lambda *a: None))
            mgr.scan()
            m, _ = manifest(folder, ui={"page": {"title": "Радио"}})
            p = plugins.Plugin(m, folder, "user")
            mgr.items[p.id] = p
            mgr.state[p.id] = {"enabled": True, "granted_device": ["assistant.tools"]}
            try:
                await mgr.ui_call(p.id, {"view": "page", "event": "open"})
                check("a plugin that does not answer panel_ui is reported", False)
            except ValueError as e:
                check("a plugin that does not answer panel_ui is reported", "колонка" in str(e), e)
            mgr.device_report("ru.test.ui\ttool\t" + json.dumps({"name": "panel_ui", "description": "x"}) + "\nru.test.ui\ttool\t" + json.dumps({"name": "play", "description": "y"}))
            names = [d["name"] for _, d in mgr.plugin_tools()]
            check("the assistant sees the plugin's commands but not panel_ui", "play" in names and "panel_ui" not in names, names)
            with patch.object(mgr, "device_call", new=AsyncMock(return_value=json.dumps({"title": "Радио", "blocks": [{"type": "text", "text": "hi"}]}))) as dc:
                d = await mgr.ui_call(p.id, {"view": "page", "event": "open"})
                check("a speaker plugin is asked through panel_ui on the speaker", dc.call_args[0][:2] == (p.id, "panel_ui") and d["blocks"][0]["text"] == "hi", dc.call_args)
            with patch.object(mgr, "device_call", new=AsyncMock(return_value="Включаю")):
                d = await mgr.ui_call(p.id, {"view": "page", "event": "select"})
                check("a plain text answer becomes a toast", d["toast"] == "Включаю" and d["blocks"] == [])
            mgr.state[p.id]["enabled"] = False
            check("a switched-off plugin has no page in the list", not any(x["id"] == p.id for x in mgr.ui_pages()))
            # a plugin on the server: the same command through tool.call
            m2, _ = manifest(folder, ui={"page": {"title": "Сервер"}})
            m2.update(id="ru.test.srv", side="server")
            q = plugins.Plugin(m2, folder, "user")
            q.tools["panel_ui"] = {"name": "panel_ui", "description": "x", "parameters": {}, "restricted": False}
            q.proc = types.SimpleNamespace(returncode=None)
            mgr.items[q.id] = q
            mgr.state[q.id] = {"enabled": True}
            with patch.object(mgr, "ask", new=AsyncMock(return_value=json.dumps({"blocks": [{"type": "toggle", "id": "a", "title": "A", "on": True}]}))) as ask:
                d = await mgr.ui_call(q.id, {"view": "page", "event": "open"})
                check("a server plugin is asked through tool.call panel_ui", ask.call_args[0][1] == "tool.call" and ask.call_args[0][2]["name"] == "panel_ui" and d["blocks"][0]["on"] is True, ask.call_args)


asyncio.run(main())
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
