"""Plugins forked by the zygote (zygote.py + plugins.fork_plugin): a plugin talks over its stdin/stdout, its stderr and exit code reach
the household, terminate works, and without the zygote the caller gets None (it starts a fresh python). Linux only.
Run:  python -I server/tests_zygote.py"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import plugins

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


ECHO = '''import os, sys
from lostos_sdk import handle, run
print("hello on stderr", file=sys.stderr)

@handle("echo")
def echo(p):
    return {"text": p["text"], "pid": os.getpid(), "dir": os.environ.get("LOSTOS_PLUGIN_DIR"), "secret": os.environ.get("SECRET", "")}

@handle("quit")
def quit(p):
    sys.stdout.flush()
    os._exit(int(p["code"]))

run()
'''


async def ask(proc, n, method, params):
    proc.stdin.write((json.dumps({"call": n, "method": method, "params": params}) + "\n").encode())
    await proc.stdin.drain()
    line = await asyncio.wait_for(proc.stdout.readline(), 10)
    return json.loads(line)


async def main():
    tmp = tempfile.mkdtemp(prefix="lostos-zyg-")
    sock = os.path.join(tmp, "zygote.sock")
    folder = os.path.join(tmp, "plugin")
    os.makedirs(folder)
    with open(os.path.join(folder, "main.py"), "w") as f:
        f.write(ECHO)
    zyg = await asyncio.create_subprocess_exec(sys.executable, "-u", "zygote.py", cwd=HERE, env=dict(os.environ, LOSTOS_ZYGOTE_SOCK=sock, SECRET="gateway"),
                                               stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(zyg.stdout.readline(), 90))
            if msg.get("ready"):
                break
        os.environ["LOSTOS_ZYGOTE_SOCK"] = sock
        env = dict(plugins.plugin_env(), LOSTOS_PLUGIN_ID="t.echo", LOSTOS_PLUGIN_DIR=os.path.join(folder, "data"), PYTHONIOENCODING="utf-8", PYTHONPATH=HERE)
        p = await plugins.fork_plugin("main.py", folder, env)
        check("the zygote forks a plugin", isinstance(p, plugins.ZygoteChild))
        if p:
            r = await ask(p, 1, "echo", {"text": "привет"})
            check("a call goes there and back", r.get("reply_to") == 1 and r.get("result", {}).get("text") == "привет", r)
            res = r.get("result", {})
            check("the plugin is a process of its own", res.get("pid") == p.pid and p.pid != zyg.pid, res)
            check("it has the environment of plugins, not the zygote's", res.get("dir") == env["LOSTOS_PLUGIN_DIR"] and res.get("secret") == "", res)
            line = await asyncio.wait_for(p.stderr.readline(), 5)
            check("its stderr reaches the household", line.strip() == b"hello on stderr", line)
            p.stdin.write((json.dumps({"call": 2, "method": "quit", "params": {"code": 7}}) + "\n").encode())
            code = await asyncio.wait_for(p.wait(), 10)
            check("its exit code reaches the household", code == 7, code)
            eof = await asyncio.wait_for(p.stdout.readline(), 5)
            check("its stdout ends with it", eof == b"", eof)
        p = await plugins.fork_plugin("main.py", folder, env)
        if p:
            p.terminate()
            code = await asyncio.wait_for(p.wait(), 10)
            check("terminate stops it", code == -15, code)
            try:
                p.terminate()
                check("a second terminate says it is gone", False)
            except ProcessLookupError:
                check("a second terminate says it is gone", True)
        p = await plugins.fork_plugin("main.py", folder, env)
        if p:
            p.stdin.close()                                  # what the plugin sees when its household dies
            code = await asyncio.wait_for(p.wait(), 10)
            check("it ends when its household's end of stdin closes", code == 0, code)
        p = await plugins.fork_plugin("missing.py", folder, env)
        if p:
            code = await asyncio.wait_for(p.wait(), 10)
            err = await p.stderr.read()
            check("a missing script ends with an error and a traceback", code == 1 and b"missing.py" in err, (code, err[-200:]))
        edge = os.path.join(HERE, "plugins", "user", "ru.lostos.edge-voice")
        if os.path.isdir(edge):
            d = os.path.join(tmp, "edge")
            shutil.copytree(edge, d)
            p = await plugins.fork_plugin("main.py", d, dict(env, LOSTOS_PLUGIN_DIR=os.path.join(d, "data")))
            await asyncio.sleep(3)
            alive = bool(p) and p.returncode is None
            err = b"" if alive or not p else await p.stderr.read()
            check("the Microsoft voice plugin runs forked", alive, (p and p.returncode, err[-400:]))
            if alive:
                p.terminate()
                await p.wait()
        zyg.stdin.close()
        await asyncio.wait_for(zyg.wait(), 10)
        p = await plugins.fork_plugin("main.py", folder, env)
        check("without the zygote the caller gets None", p is None, p)
    finally:
        if zyg.returncode is None:
            zyg.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not FAILS else f"SOME FAILED: {FAILS}")


asyncio.run(main())
