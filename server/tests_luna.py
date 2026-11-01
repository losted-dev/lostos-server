"""Import of a Luna plugin (tools/luna_import.py): plugin.toml + main.lua -> a lostOS package that the server accepts. Run from the server folder."""
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import luna_compat
import tarfile
import types
import zipfile
import plugins

OK = True


def check(name, cond):
    global OK
    print(("PASS " if cond else "FAIL ") + name)
    OK = OK and bool(cond)


TOML = '''id = "vasya.clock"
name = "Часы"
version = "1.2.0"
entry = "main.lua"
description = "Показывает время"
permissions = ["clock", "led", "audio", "bluetooth"]

[compatibility]
features = ["display.clock.segment.v1"]

[launch]
mode = "service"
'''
LUA = 'alice.clock.time("12:34", 170)\nwhile true do alice.sleep(60000) end\n'

def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


pkg = luna_compat.to_package(zipped({"share/luna/plugins/vasya.clock/plugin.toml": TOML, "share/luna/plugins/vasya.clock/main.lua": LUA, "share/luna/plugins/vasya.clock/web/index.html": "<p>x</p>", "share/luna/plugins/vasya.clock/notes.md": "no"}))
check("a Luna archive becomes a package", pkg is not None)
z = zipfile.ZipFile(io.BytesIO(pkg))
m = json.loads(z.read("manifest.json"))
check("id and version are taken over", m["id"] == "vasya.clock" and m["version"] == "1.2.0")
check("what the speaker can do is granted", m["device"]["permissions"] == ["device.display", "device.led", "audio.play"])
check("its web pages travel with it, other files do not", z.read("web/index.html") == b"<p>x</p>" and "notes.md" not in z.namelist())
code = z.read("device/main.js").decode()
check("the script follows the Lua marker", code.startswith("--lua" + chr(10)) and code.endswith(LUA))
man, err = plugins.validate_package(pkg)
check("the server accepts the package: %s" % err, man is not None and man["device"]["runtime"] == "js")

tbuf = io.BytesIO()
with tarfile.open(fileobj=tbuf, mode="w") as t:
    for n, d in (("plugin.toml", TOML), ("main.lua", LUA)):
        i = tarfile.TarInfo(n)
        i.size = len(d.encode())
        t.addfile(i, io.BytesIO(d.encode()))
check("a tar archive works too", luna_compat.to_package(tbuf.getvalue()) is not None)
check("a lostOS package is left alone", luna_compat.to_package(zipped({"manifest.json": "{}", "plugin.toml": TOML})) is None)
check("a stranger zip is left alone", luna_compat.to_package(zipped({"a.txt": "x"})) is None)
for name, files in (("a path out of the package", {"../plugin.toml": TOML, "main.lua": LUA}), ("no script", {"plugin.toml": TOML}), ("a bad id", {"plugin.toml": TOML.replace("vasya.clock", "x"), "main.lua": LUA})):
    try:
        r = luna_compat.to_package(zipped(files))
        check(name + " is refused", r is None)
    except ValueError:
        check(name + " is refused", True)

BIN = TOML.replace('entry = "main.lua"', 'entry = "bin/x"').replace('mode = "service"', 'kind = "binary"' + chr(10) + 'mode = "service"').replace('permissions = ["clock", "led", "audio", "bluetooth"]', 'permissions = ["exec", "clock"]')


def elf(machine=183, interp=False):
    h = bytearray(64 + 56)
    h[0:4] = b"ELF"
    h[4], h[5] = 2, 1
    h[16:18] = (2).to_bytes(2, "little")
    h[18:20] = machine.to_bytes(2, "little")
    h[32:40] = (64).to_bytes(8, "little")
    h[54:56] = (56).to_bytes(2, "little")
    h[56:58] = (1).to_bytes(2, "little")
    h[64:68] = (3 if interp else 1).to_bytes(4, "little")
    return bytes(h)


pkgb = luna_compat.to_package(zipped({"plugin.toml": BIN, "bin/x": elf()}))
zb = zipfile.ZipFile(io.BytesIO(pkgb))
mb = json.loads(zb.read("manifest.json"))
check("a static aarch64 program becomes a bin64 package", mb["device"]["runtime"] == "bin64" and mb["device"]["binary"] == "device/plugin" and "exec.bin64" in mb["device"]["permissions"] and "device.display" in mb["device"]["permissions"])
check("the program travels as device/plugin", zb.read("device/plugin") == elf())
man64, err64 = plugins.validate_package(pkgb)
check("the server accepts a bin64 manifest: %s" % err64, man64 is not None and man64["device"]["runtime"] == "bin64" and man64["entry"] == "")
for name, files in (("a program of another architecture", {"plugin.toml": BIN, "bin/x": elf(machine=40)}), ("a dynamic program", {"plugin.toml": BIN, "bin/x": elf(interp=True)}), ("not an executable", {"plugin.toml": BIN, "bin/x": b"#!/bin/sh"}), ("a missing program", {"plugin.toml": BIN})):
    try:
        luna_compat.to_package(zipped(files))
        check(name + " is refused", False)
    except ValueError as e:
        check(name + " is refused", True)

check("every device permission has its explanation for the page of a plugin", all(k in plugins.DEVICE_PERMISSION_INFO for k in plugins.DEVICE_PERMISSIONS))
os.environ["LOSTOS_MODE"] = "tenant"
stub = types.SimpleNamespace(state={})
mk = lambda **m: types.SimpleNamespace(kind="user", id="x.y", m=dict({"side": "server", "entry": "main.py"}, **m))
check("on a shared server a plugin with Python is blocked", plugins.Manager.blocked(stub, mk()))
check("on a shared server a speaker-only plugin is not", not plugins.Manager.blocked(stub, mk(side="speaker", entry="")))
del os.environ["LOSTOS_MODE"]

print("OK" if OK else "FAILED")
sys.exit(0 if OK else 1)
