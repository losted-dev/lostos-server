"""The tenants of a server: one light server process for every person (household), started and watched by the gateway.

A tenant is `voice_server.py` in LOSTOS_MODE=tenant: his own keys (the vault), settings, scenarios, reminders, voice prints and installed plugins live in his own folder
(data/tenants/<id>/), and the process has its own memory limit (200 MB by default, set per person in data/tenants.json, which the admin tool writes). The heavy models are NOT in the
tenants: they ask the one engine service (engine_service.py).

Config data/tenants.json (read again every few seconds, written by the admin tool):
  {"default_mb": 200, "max_tenants": 10, "idle_minutes": 20, "registration": "open" | "approval" | "invite", "overrides": {"<telegram id>": <mb>}}
  registration: "open" = anybody who asks the bot gets in at once; "approval" = closed, a stranger asks and the operator decides; "invite" = closed, only by a link that the operator made
  (the old "closed" is read as "invite")
The memory is enforced by looking at the resident memory of the process every 5 seconds: three readings in a row above the limit and the process is restarted (the person is told in his
journal). Linux only (/proc); on other systems the limit is not enforced.
A tenant that has no connected speaker and nobody who looked at his panel for `idle_minutes` is stopped to give the memory back; the next visit (or the speaker coming back) starts it.
On Linux the processes are forked by one parent that has loaded the libraries once (zygote.py): their imported code is shared, not copied per person
(LOSTOS_ZYGOTE=0 starts each household as a fresh python, as on other systems)."""
import asyncio
import json
import os
import secrets
import signal
import socket
import sys
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"default_mb": 200, "max_tenants": 10, "idle_minutes": 20, "registration": "approval", "overrides": {}}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def rss_mb(pid: int) -> float:
    try:
        for line in open(f"/proc/{pid}/status", encoding="utf-8"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def pss_mb(pid: int) -> float:
    """The memory of a process with the pages it shares (the libraries of the zygote) divided among their users (Pss); 0 when it cannot be read."""
    try:
        for line in open(f"/proc/{pid}/smaps_rollup", encoding="utf-8"):
            if line.startswith("Pss:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0.0


def own_mb(walls, tag, pid: int) -> float:
    """What a household really takes: its process and its plugins as the kernel counts them in its cgroup, without the libraries shared through
    the zygote (RSS counts those in full for every household) and without the file cache the kernel gives back by itself.
    Without a cgroup: Pss of the process alone, else its RSS."""
    return (walls.own_mb(tag) if walls.ok else 0.0) or pss_mb(pid) or rss_mb(pid)


class Cgroups:
    """A hard memory wall around every person's process (cgroup v2; the service unit needs Delegate=yes and a writable /sys/fs/cgroup). The kernel then kills THAT process the moment it
    passes limit * HARD, whatever the watch below does, and nothing else: neither the gateway (the link to the speakers) nor the neighbours. Without cgroups (another system, no rights)
    `ok` stays False and only the watch below limits the memory."""
    HARD = 1.3

    def __init__(self, log=print):
        self.ok, self.root, self.log = False, "", log
        try:
            line = [l for l in open("/proc/self/cgroup", encoding="utf-8") if l.startswith("0::")][0].strip()[3:]
            root = "/sys/fs/cgroup" + line
            gw = os.path.join(root, "gateway")
            os.makedirs(gw, exist_ok=True)
            with open(os.path.join(gw, "cgroup.procs"), "w") as f:                # a cgroup that has children may not hold processes itself: the gateway steps into its own leaf
                f.write(str(os.getpid()))
            with open(os.path.join(root, "cgroup.subtree_control"), "w") as f:
                f.write("+memory +pids")
            self.root, self.ok = root, True
            self.log("memory walls: cgroup v2 is on")
        except (OSError, IndexError):
            self.log("memory walls: no cgroup v2 here, only the watch limits the memory")

    def folder(self, tag) -> str:
        return os.path.join(self.root, f"tenant-{tag}")

    def prepare(self, tid, limit_mb: int) -> str:
        """The cgroup of the person with its limit; returns the file the new process writes its pid into ("" when walls are off)."""
        if not self.ok:
            return ""
        try:
            d = self.folder(tid)
            os.makedirs(d, exist_ok=True)
            self.set_limit(tid, limit_mb)
            for name, value in (("memory.swap.max", "0"), ("memory.oom.group", "1"), ("pids.max", "256")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(value)
            return os.path.join(d, "cgroup.procs")
        except OSError as e:
            self.log(f"tenant {tid}: no memory wall: {e!r}")
            return ""

    def set_limit(self, tid, limit_mb: int):
        try:
            with open(os.path.join(self.folder(tid), "memory.max"), "w") as f:
                f.write(str(int(limit_mb * self.HARD) * 1024 * 1024))
        except OSError:
            pass

    def kills(self, tid) -> int:
        """How many times the kernel killed this person's process for the wall (memory.events oom_kill)."""
        try:
            for l in open(os.path.join(self.folder(tid), "memory.events"), encoding="utf-8"):
                if l.startswith("oom_kill "):
                    return int(l.split()[1])
        except OSError:
            pass
        return 0

    def own_mb(self, tid) -> float:
        """memory.current of the person's cgroup without the file cache (memory.stat "file"): his processes' own memory and the kernel's for them."""
        try:
            d = self.folder(tid)
            cur = int(open(os.path.join(d, "memory.current"), encoding="utf-8").read())
            cache = next((int(l.split()[1]) for l in open(os.path.join(d, "memory.stat"), encoding="utf-8") if l.startswith("file ")), 0)
            return max(0, cur - cache) / 1048576
        except (OSError, ValueError):
            return 0.0

    def peak_mb(self, tid) -> float:
        try:
            return int(open(os.path.join(self.folder(tid), "memory.peak"), encoding="utf-8").read()) / 1048576
        except (OSError, ValueError):
            return 0.0


class Full(Exception):
    pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ForkedProc:
    """A household forked by the zygote: the part of asyncio.subprocess.Process the manager uses (pid, returncode, terminate, kill, wait).
    The zygote reports the exit; if the zygote itself is gone, the exit is noticed by the pid disappearing (init reaps the orphan)."""

    def __init__(self, pid: int, zygote):
        self.pid, self._code, self._zygote = pid, None, zygote
        self._done = asyncio.Event()

    def ended(self, code: int):
        if self._code is None:
            self._code = code
        self._done.set()

    @property
    def returncode(self):
        if self._code is None and not self._zygote.alive() and not pid_alive(self.pid):
            self.ended(-1)
        return self._code

    def send_signal(self, sig):
        if self.returncode is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    async def wait(self):
        while self.returncode is None:
            try:
                await asyncio.wait_for(self._done.wait(), 1)
            except asyncio.TimeoutError:
                pass
        return self._code


class Zygote:
    """zygote.py, started on the first household and again whenever it is gone (the households it forked keep running).
    Its socket `sock` is where the households ask it for their plugin processes (plugins.fork_plugin)."""

    def __init__(self, python: str, app_dir: str, sock: str, log=print):
        self.python, self.app_dir, self.sock, self.log = python, app_dir, sock, log
        self.proc = None
        self.children: dict = {}                # pid -> ForkedProc
        self.waiting: dict = {}                 # request number -> future of {"pid"} / {"error"}
        self.n = 0
        self.lock = asyncio.Lock()
        self.failed_at = 0.0

    def alive(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)

    async def _start(self) -> bool:
        if time.time() - self.failed_at < 60:      # it just failed: the old way for a minute, then try again
            return False
        try:
            self.proc = await asyncio.create_subprocess_exec(self.python, "-u", "zygote.py", cwd=self.app_dir, stdin=asyncio.subprocess.PIPE,
                                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 20,
                                                             env=dict(os.environ, LOSTOS_ZYGOTE_SOCK=self.sock))
            line = await asyncio.wait_for(self._ready(), 90)
        except (OSError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"zygote: did not start ({e!r}), households start the old way")
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
            self.proc, self.failed_at = None, time.time()
            return False
        self.log(f"zygote: ready (pid {line.get('pid')}, {line.get('preloaded')} libraries loaded once for all households)")
        asyncio.get_running_loop().create_task(self._read(self.proc))
        return True

    async def _ready(self) -> dict:
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise ValueError("it exited")
            msg = json.loads(raw)
            if msg.get("ready"):
                return msg
            if msg.get("warning"):
                self.log("zygote: " + msg["warning"])

    async def _read(self, proc):
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "spawned" in msg:
                fut = self.waiting.pop(msg["spawned"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif "exit" in msg:
                child = self.children.pop(msg["exit"], None)
                if child:
                    child.ended(msg["code"])
        await proc.wait()
        self.log(f"zygote: exited ({proc.returncode}); the next household starts a new one")
        for fut in self.waiting.values():
            if not fut.done():
                fut.set_result({"error": "the zygote exited"})
        self.waiting.clear()

    async def spawn(self, env: dict, cwd: str, log_path: str, cgroup: str):
        """-> ForkedProc, or None when the zygote cannot be used (the caller starts the process the old way)."""
        async with self.lock:
            if not self.alive() and not await self._start():
                return None
            self.n += 1
            n = self.n
            fut = asyncio.get_running_loop().create_future()
            self.waiting[n] = fut
            try:
                self.proc.stdin.write((json.dumps({"spawn": n, "env": env, "cwd": cwd, "log": log_path, "cgroup": cgroup}) + "\n").encode())
                await self.proc.stdin.drain()
            except (ConnectionError, RuntimeError):
                self.waiting.pop(n, None)
                return None
        try:
            msg = await asyncio.wait_for(fut, 20)
        except asyncio.TimeoutError:
            self.waiting.pop(n, None)
            return None
        if "pid" not in msg:
            self.log(f"zygote: fork failed: {msg.get('error')}")
            return None
        child = ForkedProc(msg["pid"], self)
        self.children[msg["pid"]] = child
        return child

    async def stop(self):
        if self.alive():
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


class Tenant:
    """One server process: of a household's station (sid) or, before the household has a station, of the household alone (sid "": it only serves the first-run wizard)."""

    def __init__(self, tid: int, sid: str, folder: str):
        self.id, self.sid, self.dir = tid, sid, folder           # dir: the folder of the HOUSEHOLD, shared by the processes of all its stations (keys, settings, scenarios, voices)
        self.tag = f"{tid}-{sid or 'house'}"
        self.logdir = os.path.join(folder, "stations", sid) if sid else folder
        self.port = 0
        self.key = secrets.token_urlsafe(24)
        self.proc = None
        self.started = 0.0
        self.last_used = time.time()
        self.over = 0                           # readings above the memory limit in a row
        self.restarts = 0
        self.starting = None                    # a future while the process starts
        self.mem = 0.0                          # own memory with its plugins (own_mb)
        self.peak = 0.0


class Tenants:
    def __init__(self, data_dir: str, public_url: str, gateway_url: str, engine_addr: str, log=print, python: str = None, app_dir: str = HERE):
        self.data, self.public_url, self.gateway_url, self.engine, self.log = data_dir, public_url, gateway_url, engine_addr, log
        self.python, self.app_dir = python or sys.executable, app_dir
        self.root = os.path.join(data_dir, "tenants")
        os.makedirs(self.root, exist_ok=True)
        self.items: dict = {}                   # (household id, station id or "") -> Tenant
        self.cfg = dict(DEFAULTS)
        self.cfg_mtime = 0.0
        self.http: aiohttp.ClientSession = None
        self.on_restart = None                  # called (tenant id, why) when a tenant is restarted by the watch
        self.primary_of = lambda tid: ""        # the gateway tells which station of a household is its main one (reminders without an owner ring there)
        self.walls = Cgroups(log)
        self.zygote = Zygote(self.python, app_dir, os.path.join(data_dir, "zygote.sock"), log) if os.name == "posix" and os.environ.get("LOSTOS_ZYGOTE", "1") != "0" else None
        self.gates: dict = {}                    # (tenant, station, heavy) -> semaphore of the requests in flight, kept by the gateway
        self.load_config()

    # ---- config
    def path(self) -> str:
        return os.path.join(self.data, "tenants.json")

    def load_config(self):
        try:
            m = os.path.getmtime(self.path())
            if m == self.cfg_mtime:
                return
            self.cfg = {**DEFAULTS, **json.load(open(self.path(), encoding="utf-8"))}
            self.cfg_mtime = m
        except (OSError, ValueError):
            pass

    def limit_mb(self, tid: int) -> int:
        return int(self.cfg.get("overrides", {}).get(str(tid), self.cfg.get("default_mb", 200)))

    def registration(self) -> str:
        v = self.cfg.get("registration", "approval")
        return "invite" if v == "closed" else v if v in ("open", "approval", "invite") else "approval"

    def houses(self) -> set:
        return {t for t, _ in self.items}

    def capacity_left(self) -> int:
        return max(0, int(self.cfg.get("max_tenants", 10)) - len(self.houses()))

    # ---- the processes
    def of(self, tid: int) -> list:
        return [t for (i, _), t in self.items.items() if i == tid]

    def running(self, tid: int, sid: str = None) -> bool:
        """Does a process of the household run (sid=None: any of its processes)?"""
        ts = self.of(tid) if sid is None else [t for t in [self.items.get((tid, sid))] if t]
        return any(t.proc and t.proc.returncode is None for t in ts)

    async def ensure(self, tid: int, sid: str = "") -> Tenant:
        """The process of the station (or of the house), started and answering. Raises Full when there is no room for another household."""
        t = self.items.get((tid, sid))
        if t is None:
            if tid not in self.houses() and len(self.houses()) >= int(self.cfg.get("max_tenants", 10)):
                raise Full()
            folder = os.path.join(self.root, str(tid))
            t = self.items[(tid, sid)] = Tenant(tid, sid, folder)
            os.makedirs(t.logdir, exist_ok=True)
        t.last_used = time.time()
        if self.running(tid, sid) and t.starting is None:
            return t
        if t.starting is None:
            t.starting = asyncio.get_running_loop().create_task(self._start(t))
        await asyncio.shield(t.starting)
        return t

    async def _start(self, t: Tenant):
        try:
            t.port = free_port()
            primary = "1" if (not t.sid or t.sid == self.primary_of(t.id)) else "0"
            env = dict(os.environ, LOSTOS_MODE="tenant", LOSTOS_TENANT=str(t.id), LOSTOS_STATION=t.sid, LOSTOS_PRIMARY=primary, LOSTOS_TENANT_DIR=t.dir, LOSTOS_DATA=t.dir,
                       LOSTOS_INTERNAL_KEY=t.key, LOSTOS_GATEWAY=self.gateway_url, LOSTOS_PUBLIC_URL=self.public_url, TTS_PORT=str(t.port), LOSTOS_ENGINE=self.engine,
                       LOSTOS_NO_LOCAL_ENGINES="1", LOSTOS_PUSH_KEY=os.path.join(self.data, "vapid_private.pem"), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            for k in ("LOSTOS_AUTH", "LOSTOS_LITE", "LOSTOS_TG_TOKEN", "LOSTOS_TG_CLIENT_SECRET", "LOSTOS_ZYGOTE_SOCK"):   # the secrets of the gateway never reach a tenant
                env.pop(k, None)
            if self.zygote:
                env["LOSTOS_ZYGOTE_SOCK"] = self.zygote.sock                   # its plugins are forked there too
            logf = open(os.path.join(t.logdir, "server.log"), "ab")
            if logf.tell() > 2_000_000:
                logf.close()
                logf = open(os.path.join(t.logdir, "server.log"), "wb")
            procs_file = self.walls.prepare(t.tag, self.limit_mb(t.id))

            def into_the_wall():                                              # runs in the child before the program: the memory wall, and the first to be chosen if the system ever runs out of memory
                if procs_file:
                    with open(procs_file, "w") as f:
                        f.write(str(os.getpid()))
                try:
                    with open("/proc/self/oom_score_adj", "w") as f:
                        f.write("500")
                except OSError:
                    pass
            t.proc = await self.zygote.spawn(env, self.app_dir, logf.name, procs_file) if self.zygote else None
            if t.proc:
                logf.close()                                                  # the forked process opened the log itself
            else:
                t.proc = await asyncio.create_subprocess_exec(self.python, "-u", "voice_server.py", cwd=self.app_dir, env=env, stdout=logf, stderr=asyncio.subprocess.STDOUT,
                                                              preexec_fn=into_the_wall if os.name == "posix" else None)
            t.started, t.over = time.time(), 0
            self.log(f"tenant {t.id}{'/' + t.sid[:8] if t.sid else ''}: started (pid {t.proc.pid}, port {t.port}, limit {self.limit_mb(t.id)} MB)")
            for _ in range(180):
                if t.proc.returncode is not None:
                    raise RuntimeError(f"the process of tenant {t.id} stopped at start (exit {t.proc.returncode}), see {t.logdir}/server.log")
                if await self.ping(t):
                    return
                await asyncio.sleep(0.25)
            raise RuntimeError(f"tenant {t.id} does not answer")
        finally:
            t.starting = None

    async def ping(self, t: Tenant) -> bool:
        try:
            async with self.http.get(f"http://127.0.0.1:{t.port}/internal/status", headers={"X-Lostos-Key": t.key}, timeout=aiohttp.ClientTimeout(total=1.5)) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def stop(self, tid: int, sid: str = "", why: str = "stopped"):
        t = self.items.get((tid, sid))
        if not t:
            return
        if t.proc and t.proc.returncode is None:
            t.proc.terminate()
            try:
                await asyncio.wait_for(t.proc.wait(), 8)
            except asyncio.TimeoutError:
                t.proc.kill()
                await t.proc.wait()
        self.log(f"tenant {tid}{'/' + sid[:8] if sid else ''}: {why}")
        self.items.pop((tid, sid), None)

    async def restart(self, tid: int, sid: str = "", why: str = "restart"):
        await self.stop(tid, sid, why)
        if self.on_restart:
            self.on_restart(tid, why)
        return await self.ensure(tid, sid)

    async def call(self, tid: int, sid: str, method: str, path: str, body=None, timeout: float = 20):
        """A request to the internal API of a station's process (the bot asks the player of a person this way). -> (status, json)"""
        t = await self.ensure(tid, sid)
        async with self.http.request(method, f"http://127.0.0.1:{t.port}{path}", json=body, headers={"X-Lostos-Key": t.key}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                return r.status, await r.json(content_type=None)
            except ValueError:
                return r.status, None

    # ---- the watch
    async def watch(self, busy):
        """Every 5 seconds: restart the ones that died or ate too much memory, stop the idle ones. busy(tid, sid) -> True when that station is connected."""
        while True:
            await asyncio.sleep(5)
            try:
                self.load_config()
                for tid in sorted(self.houses()):
                    flag = os.path.join(self.root, str(tid), "restart")
                    if os.path.exists(flag):                                 # the admin tool asks for a restart of the whole household
                        os.remove(flag)
                        for t in self.of(tid):
                            if t.starting is None:
                                await self.restart(tid, t.sid, "restart asked by the admin")
                for (tid, sid), t in list(self.items.items()):
                    if t.starting is not None:
                        continue
                    if t.proc and t.proc.returncode is not None:
                        self.log(f"tenant {tid}{'/' + sid[:8] if sid else ''}: died (exit {t.proc.returncode}), starting again")
                        t.restarts += 1
                        if t.restarts > 5 and time.time() - t.started < 60:
                            self.log(f"tenant {tid}: keeps dying, left stopped")
                            self.items.pop((tid, sid), None)
                            continue
                        t.proc = None
                        asyncio.get_running_loop().create_task(self.ensure(tid, sid))
                        continue
                    if t.proc:
                        self.walls.set_limit(t.tag, self.limit_mb(tid))
                        t.mem = own_mb(self.walls, t.tag, t.proc.pid)
                        t.peak = max(t.peak, self.walls.peak_mb(t.tag), t.mem)
                        t.over = t.over + 1 if t.mem > self.limit_mb(tid) else 0
                        if t.over >= 3:
                            self.log(f"tenant {tid}: {t.mem:.0f} MB is over its limit of {self.limit_mb(tid)} MB, restarting")
                            await self.restart(tid, sid, f"память {t.mem:.0f} МБ выше лимита {self.limit_mb(tid)} МБ")
                            continue
                    idle = time.time() - t.last_used > float(self.cfg.get("idle_minutes", 20)) * 60
                    if idle and not busy(tid, sid):
                        await self.stop(tid, sid, "stopped (nobody uses it)")
                with open(os.path.join(self.data, "tenants_status.json"), "w", encoding="utf-8") as f:      # what the admin tool shows
                    json.dump({"ts": int(time.time()), "tenants": self.status(), "max": self.cfg.get("max_tenants")}, f)
            except Exception as e:                                           # the watch must never die
                self.log(f"tenants: watch: {e!r}")

    def status(self) -> list:
        return [{"id": tid, "station": sid, "running": self.running(tid, sid), "mem_mb": round(t.mem), "limit_mb": self.limit_mb(tid), "restarts": t.restarts, "peak_mb": round(t.peak),
                 "wall_kills": self.walls.kills(t.tag), "port": t.port, "uptime": int(time.time() - t.started) if t.started else 0} for (tid, sid), t in self.items.items()]
