"""The parent of the household processes on a shared server (Linux): it loads the libraries every household uses ONCE and then
forks a household for each request, so their ~50 MB of imported code and data are shared pages instead of a private copy per person
(measured with tests_memory.py: a household's own memory went from ~56 MB to the part it really changes).

It talks to the gateway (tenants.py) over its stdin/stdout, one JSON object per line:
    gateway -> zygote   {"spawn": n, "env": {...}, "cwd": "...", "log": "<file>", "cgroup": "<cgroup.procs or ''>"}
    zygote -> gateway   {"spawned": n, "pid": <pid>}   or   {"spawned": n, "error": "..."}
                        {"exit": <pid>, "code": <exit status, -signal when killed>}
The households fork their server plugins here too (plugins.py): over the unix socket LOSTOS_ZYGOTE_SOCK a household sends
{"plugin": {"entry", "cwd", "env", "cgroup"}} with four descriptors (SCM_RIGHTS): the plugin's stdin, stdout, stderr and a status pipe on
which the zygote writes the exit code when it reaps the plugin. The answer is {"pid": n} or {"error": "..."}. The plugin joins the cgroup of
its household (its memory wall) and gets the household's environment of plugins, nothing else.
The zygote never runs an event loop and never starts a thread: fork() copies only the calling thread, so a child of a process
with threads (an asyncio executor, a BLAS pool) could inherit a lock that nobody will ever release. numpy gets one BLAS thread here.
Only libraries are preloaded, never our own modules: those read the environment of the household (paths, keys) when they are imported
(lostos_sdk even starts a thread). If anything here fails, households and plugins start the old way (a fresh python each)."""
import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")       # before numpy: no BLAS threads in a process that forks
os.environ.setdefault("OMP_NUM_THREADS", "1")

import gc
import json
import select
import signal
import socket
import sys

PRELOAD = ("asyncio", "json", "ssl", "hashlib", "hmac", "sqlite3", "wave", "uuid", "zipfile", "collections", "random", "re", "socket", "struct",
           "concurrent.futures", "urllib.parse", "base64", "datetime", "threading",
           "aiohttp", "aiohttp.web", "numpy", "soundfile", "cryptography.hazmat.primitives.asymmetric.ed25519",
           "cryptography.hazmat.primitives.asymmetric.x25519", "cryptography.hazmat.primitives.ciphers.aead", "cryptography.hazmat.primitives.serialization",
           "cryptography.hazmat.primitives.hashes", "cryptography.hazmat.primitives.asymmetric.ec", "cryptography.hazmat.primitives.asymmetric.utils",
           "cryptography.exceptions", "cryptography.x509", "cryptography.x509.oid", "aioesphomeapi", "noise.connection",
           "secrets", "queue", "difflib", "shlex", "http.server", "webbrowser",
           "edge_tts",                                      # the library of the Microsoft voice plugin, when the server has it
           "scipy.signal",                                  # resample_poly for a plugin's voice: ~50 MB per household when imported after the fork
           "charset_normalizer")


def preload():
    loaded = []
    for name in PRELOAD:
        try:
            __import__(name)
            loaded.append(name)
        except Exception as e:                                # a missing optional library only costs its sharing
            print(json.dumps({"warning": f"{name}: {e!r}"[:200]}), flush=True)
    gc.collect()
    gc.freeze()       # what exists now is never scanned by the collector again: its pages are not written, they stay shared with the children
    return loaded


def enter(cgroup: str, close: list):
    """In a forked child: its own session, the signals of a fresh python, none of the zygote's descriptors, the memory wall of its household,
    first in line for the OOM killer."""
    os.setsid()
    signal.set_wakeup_fd(-1)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)              # the rest stays as python set it: SIGPIPE ignored (a closed connection is an error, not death)
    for fd in close:
        try:
            os.close(fd)
        except OSError:
            pass
    if cgroup:
        try:
            with open(cgroup, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("500")
    except OSError:
        pass


def reseed():
    import random
    random.seed()                                              # the parent's state would be the same in every child
    try:
        import numpy
        numpy.random.seed()
    except Exception:
        pass


def plugin_child(req: dict, fds: list, close: list):
    """In the forked child: becomes a server plugin of a household (its entry script, the SDK on stdin/stdout); never returns."""
    code = 1
    try:
        for i, fd in enumerate(fds[:3]):                       # stdin, stdout, stderr of the plugin
            os.dup2(fd, i)
        enter(req.get("cgroup", ""), [fd for fd in fds + close if fd > 2])
        os.environ.clear()
        os.environ.update(req["env"])
        os.chdir(req["cwd"])
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [req["cwd"], here]
        sys.argv = [req["entry"]]
        import io
        sys.stdin = io.TextIOWrapper(io.BufferedReader(io.FileIO(0, "r", closefd=False)), encoding="utf-8")
        sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False), encoding="utf-8", line_buffering=True, write_through=True)
        sys.stderr = io.TextIOWrapper(io.FileIO(2, "w", closefd=False), encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
        reseed()
        import runpy
        runpy.run_path(os.path.join(req["cwd"], req["entry"]), run_name="__main__")
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)


def child(req: dict, ctl_in: int, ctl_out: int, close: list):
    """In the forked child: becomes the household's voice_server.py; never returns."""
    code = 1
    try:
        enter(req.get("cgroup", ""), close)                     # the memory wall of the person (tenants.Cgroups)
        log = os.open(req["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        for fd in (log, null, ctl_in, ctl_out):
            if fd > 2:
                os.close(fd)
        os.environ.clear()
        os.environ.update(req["env"])
        os.chdir(req["cwd"])
        sys.path[:0] = [req["cwd"]]
        sys.argv = ["voice_server.py"]
        sys.stdout = os.fdopen(1, "w", buffering=1, encoding="utf-8", errors="replace")
        sys.stderr = os.fdopen(2, "w", buffering=1, encoding="utf-8", errors="replace")
        import time
        if "TZ" in os.environ:
            time.tzset()
        reseed()
        import runpy
        runpy.run_path(os.path.join(req["cwd"], "voice_server.py"), run_name="__main__")
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)


def main():
    out = sys.stdout
    ctl_in, ctl_out = sys.stdin.fileno(), out.fileno()
    loaded = preload()

    def send(obj):
        out.write(json.dumps(obj) + "\n")
        out.flush()

    rfd, wfd = os.pipe()                                       # SIGCHLD wakes the select below
    os.set_blocking(wfd, False)
    signal.set_wakeup_fd(wfd)
    signal.signal(signal.SIGCHLD, lambda *a: None)
    listener = None
    path = os.environ.get("LOSTOS_ZYGOTE_SOCK", "")
    if path:                                                   # the households ask here for their plugins
        try:
            os.unlink(path)
        except OSError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        listener.bind(path)
        os.umask(old)
        listener.listen(16)
    send({"ready": True, "preloaded": len(loaded), "pid": os.getpid()})
    status: dict = {}                                          # pid of a plugin -> the write end of its status pipe
    buf = b""
    while True:
        try:
            ready, _, _ = select.select([ctl_in, rfd] + ([listener] if listener else []), [], [])
        except InterruptedError:
            ready = []
        own = [rfd, wfd] + ([listener.fileno()] if listener else []) + list(status.values())   # what a child must not keep
        if listener in ready:
            serve_plugin(listener, status, own)
        if rfd in ready:
            try:
                os.read(rfd, 512)
            except BlockingIOError:
                pass
        while True:                                            # report every child that ended (also the ones a missed signal hid)
            try:
                pid, st = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            code = os.waitstatus_to_exitcode(st)
            if pid in status:                                   # a plugin: its household learns it on the status pipe
                try:
                    os.write(status[pid], f"{code}\n".encode())
                except OSError:
                    pass
                os.close(status.pop(pid))
            else:
                send({"exit": pid, "code": code})
        if ctl_in in ready:
            chunk = os.read(ctl_in, 65536)
            if not chunk:                                      # the gateway is gone: the households stay, the zygote leaves
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    req = json.loads(line)
                    n = req["spawn"]
                except (ValueError, KeyError, TypeError):
                    continue
                try:
                    pid = os.fork()
                except OSError as e:
                    send({"spawned": n, "error": str(e)[:200]})
                    continue
                if pid == 0:
                    child(req, ctl_in, ctl_out, own)
                send({"spawned": n, "pid": pid})


def serve_plugin(listener, status: dict, own: list):
    """One request of a household for a plugin process (see the docstring of the module)."""
    try:
        conn, _ = listener.accept()
    except OSError:
        return
    fds = []
    with conn:
        try:
            conn.settimeout(3)
            msg, fds, _, _ = socket.recv_fds(conn, 1 << 16, 4)
            req = json.loads(msg.split(b"\n", 1)[0])["plugin"]
            if len(fds) != 4 or not all(isinstance(req.get(k), str) for k in ("entry", "cwd")) or not isinstance(req.get("env"), dict):
                raise ValueError("bad request")
            pid = os.fork()
            if pid == 0:
                plugin_child(req, fds, own + [conn.fileno()])
            status[pid] = fds[3]
            for fd in fds[:3]:
                os.close(fd)
            fds = []
            conn.sendall((json.dumps({"pid": pid}) + "\n").encode())
        except (OSError, ValueError, KeyError, TypeError) as e:
            for fd in fds:
                os.close(fd)
            try:
                conn.sendall((json.dumps({"error": str(e)[:200]}) + "\n").encode())
            except OSError:
                pass


if __name__ == "__main__":
    main()
