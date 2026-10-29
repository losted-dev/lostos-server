"""JavaScript plugins that live on the speaker: the server takes the package without a server script, starts no process for it, asks the
speaker to install it with the JS runtime, offers the commands the speaker reported to the assistant and runs them on the speaker.
No speaker: device_command is recorded and the speaker's answers are fed to device_report. Run: python -I server/tests_station_plugins.py"""
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import plugins

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    OK = OK and bool(cond)


def write_plugin(root, pid, manifest, files):
    d = os.path.join(root, pid)
    os.makedirs(d, exist_ok=True)
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
        open(os.path.join(d, rel), "w", encoding="utf-8").write(text)
    open(os.path.join(d, "manifest.json"), "w", encoding="utf-8").write(json.dumps(manifest))


async def main():
    tmp = tempfile.mkdtemp(prefix="lostos-jsplug-")
    try:
        user = os.path.join(tmp, "user")
        js = {"id": "ru.test.js", "name": "JS", "version": "1.0.0", "author": "t", "permissions": [], "side": "speaker",
              "device": {"runtime": "js", "permissions": ["network.connect", "assistant.tools"]}}
        write_plugin(user, "ru.test.js", js, {"device/main.js": "await lostos.tool({name:'hello'}, () => 'hi');"})
        bad = dict(js, id="ru.test.noentry", device={"permissions": ["storage"], "binary": "device/plugin"})
        write_plugin(user, "ru.test.noentry", bad, {"device/plugin": "bin"})
        plugins.USER_ROOT, plugins.STATE_FILE = user, os.path.join(tmp, "state.json")
        sent = []
        srv = types.SimpleNamespace(PANEL={"cli": None}, log=lambda *a: None, GLOBALS={"base_url": "http://server.test"}, state_services={})
        mgr = plugins.Manager(srv)
        mgr.scan()
        mgr.loop = asyncio.get_running_loop()

        async def device_command(line):
            sent.append(line)
            return True

        mgr.device_command = device_command
        p = mgr.items.get("ru.test.js")
        check("a JS plugin of the speaker needs no server script", p is not None and p.m["entry"] == "" and p.m["device"]["runtime"] == "js"
              and p.m["device"]["binary"] == "device/main.js", mgr.bad)
        check("a native package without a server script is still refused", "ru.test.noentry" not in mgr.items)
        mgr.state.setdefault(p.id, {})["granted"] = []
        await mgr.set_enabled(p.id, True)
        check("no process is started on the server for it", p.proc is None)
        inst = [l for l in sent if l.startswith("install ru.test.js ")]
        check("the speaker is asked to install it with the JS runtime", inst and inst[0].endswith(" js") and "/plugins-dl/ru.test.js/plugin" in inst[0], sent)

        decl = {"name": "hello", "description": "says hi", "parameters": {"type": "object", "properties": {}, "required": []}}
        mgr.device_report(f"ru.test.js\trunning\tработает\nru.test.js\ttool\t{json.dumps(decl)}\nru.unknown\ttool\t{json.dumps(dict(decl, name='ghost'))}\n")
        names = [d["name"] for _, d in mgr.plugin_tools()]
        check("the commands the speaker reported reach the assistant", names == ["hello"], names)
        check("the state of the speaker's copy is kept", mgr.device.get("ru.test.js", "").startswith("running"))

        async def answer_later():
            for _ in range(50):
                await asyncio.sleep(0.02)
                calls = [l for l in sent if l.startswith("call ru.test.js ")]
                if calls:
                    _, _, cid, b64 = calls[-1].split(" ", 3)
                    req = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
                    check("the call carries the command and its arguments", req == {"name": "hello", "args": {"who": "мир"}}, req)
                    mgr.device_report(f"ru.test.js\trunning\t\nru.test.js\ttool\t{json.dumps(decl)}\nru.test.js\tresult\t{cid}\t{json.dumps('привет, мир')}\n")
                    return

        asyncio.get_running_loop().create_task(answer_later())
        res = await mgr.run_plugin_tool("hello", {"who": "мир"})
        check("a command runs on the speaker and its answer comes back", res == "привет, мир", res)
        try:
            await mgr.device_call("ru.test.js", "hello", {}, timeout=0.3)
            check("a call without an answer times out", False)
        except asyncio.TimeoutError:
            check("a call without an answer times out", True)
        mgr.device_report("ru.test.js\toff\t\n")
        check("commands vanish when the plugin stops on the speaker", mgr.plugin_tools() == [])
        st = mgr.state.setdefault(p.id, {})
        st["granted_device"] = [x for x in st.get("granted_device", []) if x != "assistant.tools"]
        mgr.device_report(f"ru.test.js\trunning\t\nru.test.js\ttool\t{json.dumps(decl)}\n")
        check("without assistant.tools the commands are not offered", mgr.plugin_tools() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if OK else "SOME FAILED")
    sys.exit(0 if OK else 1)


asyncio.run(main())
