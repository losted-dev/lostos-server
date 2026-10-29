"""Plugin packages: strict paths and limits, updates keep the data, a shared server runs only the team's plugins. Run from the server folder.
(First written by sorried in losted-dev/lostos-server#2.)"""
import asyncio
import io
import json
import os
import sys
import tempfile
import types
import warnings
import zipfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plugins

OK = True


def check(name, cond):
    global OK
    print(("PASS " if cond else "FAIL ") + name)
    OK = OK and bool(cond)


def package(extra=(), prefix=""):
    manifest = {"id": "ru.test.plugin", "version": "1.0.0", "name": "Test", "author": "Test", "permissions": []}
    buf = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        z.writestr(prefix + "manifest.json", json.dumps(manifest))
        z.writestr(prefix + "main.py", "print('hello')")
        for name, data in extra:
            z.writestr(name, data)
    return buf.getvalue()


def rejected(data):
    try:
        plugins.inspect_package(data)
    except ValueError:
        return True
    return False


async def main():
    with tempfile.TemporaryDirectory(prefix="lostos-plugins-") as folder:
        with patch.object(plugins, "USER_ROOT", os.path.join(folder, "user")), patch.object(plugins, "STATE_FILE", os.path.join(folder, "state.json")):
            srv = types.SimpleNamespace(PANEL={"cli": None}, log=lambda *a: None)
            mgr = plugins.Manager(srv)
            mgr.scan()
            for prefix in ("", "pkg/"):
                manifest = await mgr.install(package(prefix=prefix))
                check("a normal package installs: " + (prefix or "root"), manifest["id"] == "ru.test.plugin")

            target = os.path.join(folder, "escaped.py")
            data = package([("pkg/" + target, "escaped")], prefix="pkg/")
            try:
                await mgr.install(data)
            except ValueError:
                pass
            check("a wrapped path cannot write outside the plugin", not os.path.exists(target))
            for name in ("pkg//root.txt", "pkg/./main.py", "pkg/../main.py", "pkg\\main.py", "pkg/main.py", "pkg/MAIN.py", "pkg/main.py/child"):
                check("ambiguous member is refused: " + name, rejected(package([(name, "other")], prefix="pkg/")))

            with patch.object(plugins.Manager, "MAX_UNPACKED", 1024):
                data = package([("large.txt", b"x" * 1025)])
                with patch("plugin_sign.check_package") as verify:
                    try:
                        mgr.trust_of(data)
                    except ValueError:
                        pass
                    check("unpacked size is checked before signature reads", not verify.called)
            with patch.object(plugins.Manager, "MAX_PACKAGE", 10):
                with patch("plugin_sign.check_package") as verify:
                    try:
                        mgr.trust_of(package())
                    except ValueError:
                        pass
                    check("compressed size is checked before signature reads", not verify.called)
            check("too many archive entries are refused", rejected(package([(f"file-{i}", "") for i in range(601)])))

            await mgr.install(package([("data/default.txt", "old default")]))
            saved = os.path.join(plugins.USER_ROOT, "ru.test.plugin", "data", "saved.txt")
            with open(saved, "w") as f:
                f.write("user data")
            await mgr.install(package([("data/default.txt", "new default")]))
            check("updates keep existing data at its original path", os.path.isfile(saved))
            with open(saved) as f:
                check("updates preserve saved contents", f.read() == "user data")
            with open(os.path.join(os.path.dirname(saved), "default.txt")) as f:
                check("existing data takes precedence over packaged defaults", f.read() == "old default")
            check("updates do not nest the data folder", not os.path.exists(os.path.join(os.path.dirname(saved), "data")))

            await mgr.install(package())
            p = mgr.items["ru.test.plugin"]
            with patch.dict(os.environ, {"LOSTOS_MODE": "tenant"}):
                mgr.state[p.id]["enabled"] = True
                check("saved enablement cannot authorize a tenant plugin", not mgr.enabled(p))
                with patch.object(plugins.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn, patch.object(mgr, "device_install", new_callable=AsyncMock) as device:
                    check("tenant enablement is refused", not await mgr.set_enabled(p.id, True))
                    await mgr.run(p)
                    check("direct startup cannot spawn a tenant plugin", not spawn.called)
                    check("refused enablement cannot install a device program", not device.called)
                check("the restriction is explained", bool(mgr.requirement(p)) and bool(p.error))
                check("a tenant plugin can still be switched off", await mgr.set_enabled(p.id, False))
                p.m["side"] = "speaker"
                with patch.object(plugins.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn:
                    await mgr.run(p)
                    check("speaker metadata cannot authorize a tenant process", not spawn.called)
                check("built-in services remain enabled", mgr.enabled(mgr.items["ru.lostos.llm"]))
                mgr.state[p.id]["signature"] = {"state": "trusted", "publisher": "lostOS", "fingerprint": "x", "team": True}
                check("a plugin signed by the team still runs on a shared server", not mgr.blocked(p) and mgr.requirement(p) == "")
                mgr.state[p.id]["signature"] = {"state": "trusted", "publisher": "Dev", "fingerprint": "y", "team": False}
                check("a plugin of another verified developer waits for the sandbox", mgr.blocked(p))
                mgr.state[p.id]["signature"] = {"state": "none"}

            with patch.dict(os.environ, {"LOSTOS_MODE": ""}):
                with patch.object(mgr, "run", new_callable=AsyncMock) as run:
                    check("a standalone server can enable its plugin", await mgr.set_enabled(p.id, True) and run.called)


asyncio.run(main())
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
