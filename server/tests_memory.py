"""How much memory the households of a shared server take: starts a gateway, connects N fake speakers that belong to N people (one
process per household), lets them settle and prints the memory of every process: RSS, USS (only its own pages) and PSS (its own pages
plus its share of the pages it shares with the others, the honest per-household cost when many run side by side).
Linux only (USS/PSS come from /proc/<pid>/smaps). No engine is started: the households do not need it until they hear something.
With a list of plugin ids every household installs those plugins from server/plugins/user and switches them on, and their processes are counted apart.
Run:  python -I server/tests_memory.py [households=5] [settle seconds=25] [plugin,plugin...]"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aiohttp
import psutil
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import accounts as acc_mod

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
SETTLE = float(sys.argv[2]) if len(sys.argv) > 2 else 25
PLUGINS = [x for x in (sys.argv[3] if len(sys.argv) > 3 else "").split(",") if x]
DATA = tempfile.mkdtemp(prefix="lostos-mem-")
GW_PORT = 18965
BASE = f"http://127.0.0.1:{GW_PORT}"


def mb(x):
    return x / 1048576


def package(pid: str) -> bytes:
    folder = os.path.join(HERE, "plugins", "user", pid)
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ("data", "__pycache__")]
            for f in files:
                z.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), folder))
    return buf.getvalue()


async def install(http, ck, pid):
    h = {"Cookie": f"lostos_session={ck}"}
    async with http.post(BASE + "/api/v1/plugins/install", data=package(pid), headers={**h, "X-File-Name": pid + ".lostos"}) as r:
        a = await r.json()
    async with http.post(BASE + f"/api/v1/plugins/{pid}", json={"enabled": True}, headers=h) as r:
        b = await r.json()
    if not (a.get("ok") and b.get("ok")):
        print(f"plugin {pid}: install {a}, switch on {b}")


async def speaker(http, n, people, report):
    """One fake speaker of person 100+n: links, is claimed by its owner, hands over its keys, then stays connected."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    async with http.ws_connect(BASE + "/link/ws") as ws:
        ch = await ws.receive_json()
        await ws.send_json({"t": "hello", "ver": 1, "pub": base64.b64encode(pub).decode(),
                            "sig": base64.b64encode(key.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")).decode(), "name": f"mem{n}"})
        w = await ws.receive_json()
        ck = people.new_session(100 + n, "mem", "127.0.0.1")
        async with http.post(BASE + "/api/v1/stations/claim", headers={"Cookie": f"lostos_session={ck}"}, json={"id": w["id"]}) as r:
            await r.read()
        report.append(ck)
        async for m in ws:
            if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("state") == "confirmed":
                await ws.send_json({"t": "vaultkey", "key": base64.b64encode(os.urandom(32)).decode()})
                await ws.send_json({"t": "apikey", "key": base64.b64encode(os.urandom(32)).decode()})


async def main():
    procs = []
    try:
        people = acc_mod.Accounts(os.path.join(DATA, "accounts.db"))
        people.bind_owner(1, "Оператор", "op")
        for n in range(N):
            people.request_access(100 + n, f"Человек {n}", f"p{n}")
            people.decide(100 + n, True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", LOSTOS_DATA=DATA, LOSTOS_PUBLIC_URL=BASE, TTS_PORT=str(GW_PORT),
                   LOSTOS_ENGINE="127.0.0.1:1")
        gw = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "gateway.py")], env=env, cwd=HERE,
                              stdout=open(os.path.join(DATA, "gateway.out"), "wb"), stderr=subprocess.STDOUT)
        procs.append(gw)
        http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))
        for _ in range(100):
            try:
                async with http.get(BASE + "/api/v1/hello") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.3)
        cookies = []
        tasks = [asyncio.create_task(speaker(http, n, people, cookies)) for n in range(N)]
        ok = 0
        for _ in range(120):                                     # every household answers its panel: its process runs
            await asyncio.sleep(0.5)
            if len(cookies) < N:
                continue
            ok = 0
            for ck in cookies:
                try:
                    async with http.get(BASE + "/api/v1/setup", headers={"Cookie": f"lostos_session={ck}"}) as r:
                        ok += r.status == 200
                except aiohttp.ClientError:
                    pass
            if ok == N:
                break
        for ck in cookies:
            for pid in PLUGINS:
                await install(http, ck, pid)
        await asyncio.sleep(SETTLE)
        g = psutil.Process(gw.pid)
        rows, plugs = [], []
        for c in g.children(recursive=True):
            try:
                fi = c.memory_full_info()
                cwd = c.cwd()
                row = (c.pid, (os.path.basename(cwd) if "plugins" in cwd else " ".join(c.cmdline()[1:3]))[:40], mb(fi.rss), mb(fi.uss), mb(fi.pss))
                (plugs if "plugins" in cwd else rows).append(row)    # a plugin runs in its own folder, forked or not
            except psutil.Error:
                pass
        print(f"households answering their panel: {ok}/{N}")
        for ck in cookies[:1] if PLUGINS else []:
            async with http.get(BASE + "/api/v1/plugins", headers={"Cookie": f"lostos_session={ck}"}) as r:
                for x in (await r.json())["plugins"]:
                    if x["id"] in PLUGINS:
                        print(f"plugin {x['id']} in the first household: running {x['running']}, error {x['error']!r}")
        gfi = g.memory_full_info()
        print(f"gateway: RSS {mb(gfi.rss):.1f}  USS {mb(gfi.uss):.1f}  PSS {mb(gfi.pss):.1f} MB")
        for pid, cmd, rss, uss, pss in rows + plugs:
            print(f"  {pid:>7} {cmd:<40} RSS {rss:6.1f}  USS {uss:6.1f}  PSS {pss:6.1f} MB")
        zyg = [c.pid for c in g.children() if "zygote.py" in " ".join(c.cmdline())]
        houses = [r for r in rows if r[0] not in zyg and ("voice_server" in r[1] or "zygote" in r[1])]     # forked households keep the zygote's command line
        if houses:
            k = len(houses)
            print(f"households: {k}; per household RSS {sum(r[2] for r in houses) / k:.1f}, USS {sum(r[3] for r in houses) / k:.1f}, "
                  f"PSS {sum(r[4] for r in houses) / k:.1f} MB; all processes PSS {sum(r[4] for r in rows + plugs) + mb(gfi.pss):.1f} MB")
        try:                                                     # what the admin shows (the watch writes it every 5 s)
            st = json.load(open(os.path.join(DATA, "tenants_status.json"), encoding="utf-8"))["tenants"]
            print("admin, memory now: " + ", ".join(f"{t['mem_mb']} MB" for t in st if t["running"]) + f"  (peak {max(t['peak_mb'] for t in st)} MB)")
        except (OSError, ValueError, KeyError) as e:
            print(f"admin status: {e!r}")
        for name in sorted({r[1] for r in plugs}):
            rs = [r for r in plugs if r[1] == name]
            k = len(rs)
            print(f"plugin {name}: {k} processes; per process RSS {sum(r[2] for r in rs) / k:.1f}, USS {sum(r[3] for r in rs) / k:.1f}, PSS {sum(r[4] for r in rs) / k:.1f} MB")
        if os.environ.get("MEM_LOGS"):                           # what the households said about their plugins
            for root, _, files in os.walk(os.path.join(DATA, "tenants")):
                for f in files:
                    if f == "server.log":
                        for line in open(os.path.join(root, f), encoding="utf-8", errors="replace"):
                            if "plugin" in line.lower() or "Traceback" in line or "Error" in line:
                                print("   log:", line.rstrip()[:200])
        for t in tasks:
            t.cancel()
        await http.close()
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(15)
            except Exception:
                p.kill()
        shutil.rmtree(DATA, ignore_errors=True)


asyncio.run(main())
