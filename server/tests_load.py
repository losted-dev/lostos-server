"""The worst a person can do to his server process: measures the peak memory of the process of one person (and of the gateway and the engine) while he hammers the server.
Run:  python -I server/tests_load.py [people]      (starts an engine, a gateway and one fake speaker; takes a couple of minutes). Prints the peak RSS of every process and PASS/FAIL against the limits."""
import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aiohttp
import psutil
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import accounts as acc_mod

DATA = tempfile.mkdtemp(prefix="lostos-load-")
GW_PORT, ENGINE_PORT = 18865, 18871
BASE = f"http://127.0.0.1:{GW_PORT}"
LIMIT_MB = 200
OK = True
PROCS = []
PEAK = {}


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


def start(args, env_extra, tag):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **env_extra)
    p = subprocess.Popen([sys.executable, "-u"] + args, env=env, stdout=open(os.path.join(DATA, f"{tag}.log"), "wb"), stderr=subprocess.STDOUT, cwd=HERE)
    PROCS.append(p)
    return p


def sampler(gw_pid, stop):
    """Every 50 ms: the resident memory of the gateway, its children (the people's processes) and the engine; the highest value of each is kept."""
    while not stop.is_set():
        try:
            g = psutil.Process(gw_pid)
            PEAK["gateway"] = max(PEAK.get("gateway", 0), g.memory_info().rss / 1048576)
            for c in g.children(recursive=True):
                try:
                    PEAK["tenant"] = max(PEAK.get("tenant", 0), c.memory_info().rss / 1048576)
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        time.sleep(0.05)


async def main():
    stop = threading.Event()
    try:
        env = {"LOSTOS_DATA": DATA, "LOSTOS_PUBLIC_URL": BASE, "TTS_PORT": str(GW_PORT), "LOSTOS_ENGINE": f"127.0.0.1:{ENGINE_PORT}"}
        if os.environ.get("LOSTOS_TEST_ENGINE"):                                  # an engine that already runs (the live one of the VDS): no second copy of the models
            env["LOSTOS_ENGINE"] = os.environ["LOSTOS_TEST_ENGINE"]
        else:
            start([os.path.join(HERE, "engine_service.py")], {"LOSTOS_ENGINE": env["LOSTOS_ENGINE"]}, "engine")
        people = acc_mod.Accounts(os.path.join(DATA, "accounts.db"))
        people.bind_owner(1, "Оператор", "op")
        people.request_access(100, "Анна", "anna")
        people.decide(100, True)
        ck = people.new_session(100, "load", "127.0.0.1")
        gw = start([os.path.join(HERE, "gateway.py")], env, "gateway")
        threading.Thread(target=sampler, args=(gw.pid, stop), daemon=True).start()
        http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))
        for _ in range(100):
            try:
                async with http.get(BASE + "/api/v1/hello") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.3)
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ready = asyncio.Event()
        sid = {}

        async def station():
            async with http.ws_connect(BASE + "/link/ws") as ws:
                ch = await ws.receive_json()
                await ws.send_json({"t": "hello", "ver": 1, "pub": base64.b64encode(pub).decode(), "sig": base64.b64encode(key.sign(bytes.fromhex(ch["nonce"]) + b"lostos-link-v1")).decode(), "name": "load"})
                w = await ws.receive_json()
                sid["id"] = w["id"]
                ready.set()
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("state") == "confirmed":
                        await ws.send_json({"t": "vaultkey", "key": base64.b64encode(os.urandom(32)).decode()})
                        await ws.send_json({"t": "apikey", "key": base64.b64encode(os.urandom(32)).decode()})

        st_task = asyncio.create_task(station())
        await ready.wait()
        H = {"Cookie": f"lostos_session={ck}"}
        async with http.post(BASE + "/api/v1/stations/claim", headers=H, json={"id": sid["id"]}) as r:
            await r.read()
        for _ in range(60):
            async with http.get(BASE + "/api/v1/setup", headers=H) as r:
                if r.status == 200:
                    break
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)
        PEAK.clear()
        base = {}
        for n in range(20):
            await asyncio.sleep(0.1)
        quiet = PEAK.get("tenant", 0)
        print(f"quiet tenant: {quiet:.0f} MB")

        async def hit(method, path, **kw):
            try:
                async with http.request(method, BASE + path, headers=H, timeout=aiohttp.ClientTimeout(total=120), **kw) as r:
                    await r.read()
                    return r.status
            except Exception:
                return 0

        # 1. a flood of ordinary requests
        codes = await asyncio.gather(*[hit("GET", p) for _ in range(300) for p in ("/api/state", "/api/health", "/api/v1/setup", "/api/home")])
        print("flood of 1200 requests:", {c: codes.count(c) for c in set(codes)})
        # 2. fourteen-megabyte uploads, as many as he can start
        junk = io.BytesIO()
        with zipfile.ZipFile(junk, "w", zipfile.ZIP_STORED) as z:
            z.writestr("settings.json", os.urandom(13 * 1024 * 1024))
        body = junk.getvalue()
        codes = await asyncio.gather(*[hit("POST", "/api/restore", data=body) for _ in range(30)] + [hit("POST", "/api/v1/plugins/install", data=body) for _ in range(30)])
        print("60 uploads of 13 MB at once:", {c: codes.count(c) for c in set(codes)})
        # 3. a body that is too large and one with a huge key value
        codes = await asyncio.gather(*[hit("POST", "/api/v1/secrets", data=b"x" * (3 * 1024 * 1024)) for _ in range(20)])
        print("20 bodies of 3 MB to an ordinary route:", {c: codes.count(c) for c in set(codes)})
        # 4. a backup asked again and again
        codes = await asyncio.gather(*[hit("GET", "/api/backup") for _ in range(40)])
        print("40 backups at once:", {c: codes.count(c) for c in set(codes)})
        await asyncio.sleep(3)
        after = PEAK.get("tenant", 0)
        print(f"PEAK tenant {PEAK.get('tenant', 0):.0f} MB, gateway {PEAK.get('gateway', 0):.0f} MB (limit of a person {LIMIT_MB} MB, wall {LIMIT_MB * 1.3:.0f} MB)")
        async with http.get(BASE + "/api/v1/account", headers=H) as r:
            check("the gateway and the panel still answer after the attack", r.status == 200)
        check("the gateway stayed small", PEAK.get("gateway", 0) < 150, PEAK.get("gateway"))
        check("the process of the person stayed within his wall", PEAK.get("tenant", 0) < LIMIT_MB * 1.3, PEAK.get("tenant"))
        st_task.cancel()
        await http.close()
    finally:
        stop.set()
        for p in PROCS:
            try:
                p.terminate()
            except Exception:
                pass
        for p in PROCS:
            try:
                p.wait(10)
            except Exception:
                p.kill()
        print("ALL PASS" if OK else "SOME FAILED")
        shutil.rmtree(DATA, ignore_errors=True)
        sys.exit(0 if OK else 1)


asyncio.run(main())
