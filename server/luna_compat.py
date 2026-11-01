"""Plugins written for the Luna framework (plugin.toml + a Lua script) installed as they are: the package is turned into a lostOS package on the way in.

Made from Luna's public documentation (plugin.toml: id, name, version, entry, description, permissions). The Lua script goes to the speaker as device/main.js with the
line "--lua" in front: lostos-js runs it as Lua 5.4 (docs/plugins-lua.md). Whatever the speaker cannot do is left out of the permissions and told in `skipped`."""
import io
import json
import os
import re
import tarfile
import tomllib
import zipfile

# Luna permission -> permission of the speaker; None: this speaker cannot do it
PERMISSIONS = {"led": "device.led", "clock": "device.display", "display": "device.display", "storage": "storage",
               "audio": "audio.play", "volume": "media.control", "input": "events.subscribe", "microphone": None, "bluetooth": None, "web": None, "exec": "exec.bin64", "network": "network.connect",
               "sensors": None, "ipc_read": None, "ipc_control": None, "commands": None, "system": None}
WEB_EXT = {".html", ".htm", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff2", ".txt"}
MAX_SCRIPT = 900_000
MAX_FILES = 200
MAX_FILE = 12_000_000                    # a program of a package is a few MB


def _safe(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or any(p in ("", ".", "..") for p in parts) or ":" in name)


def read_files(data: bytes) -> dict:
    """-> {path: bytes} of a zip or tar archive (only plain files with safe paths); {} when it is neither."""
    files = {}
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for i in z.infolist()[:MAX_FILES * 4]:
                    if not i.is_dir() and _safe(i.filename) and i.file_size <= MAX_FILE and len(files) < MAX_FILES:
                        files[i.filename] = z.read(i)
        else:
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                for m in t.getmembers()[:MAX_FILES * 4]:
                    if m.isfile() and _safe(m.name) and m.size <= MAX_FILE and len(files) < MAX_FILES:
                        files[m.name] = t.extractfile(m).read()
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
        return {}
    return files


def convert(files: dict) -> dict:
    """Files of a Luna plugin -> {"manifest": dict, "code": str, "skipped": [...]}. Raises ValueError with a message for the person."""
    tomls = sorted((n for n in files if n.split("/")[-1] == "plugin.toml"), key=len)
    if not tomls:
        raise ValueError("в пакете нет plugin.toml")
    base = tomls[0][:-len("plugin.toml")]
    try:
        toml = tomllib.loads(files[tomls[0]].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("plugin.toml не читается")
    pid = str(toml.get("id", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9]+([.\-][a-z0-9]+)+", pid):
        raise ValueError("id плагина должен быть вида namespace.name, а здесь: %r" % pid[:60])
    launch = toml.get("launch") if isinstance(toml.get("launch"), dict) else {}
    if launch.get("kind") == "binary" or not str(toml.get("entry", "main.lua")).endswith(".lua"):
        return convert_binary(files, base, toml, launch)
    entry = str(toml.get("entry", "main.lua"))
    if not _safe(entry) or (base + entry) not in files:
        raise ValueError("в пакете нет файла entry: " + entry[:60])
    try:
        code = files[base + entry].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("скрипт не в UTF-8")
    if len(code) > MAX_SCRIPT:
        raise ValueError("скрипт слишком большой")
    granted, skipped = [], []
    perms = toml.get("permissions", [])
    for p in perms if isinstance(perms, list) else []:
        if p not in PERMISSIONS:
            skipped.append("%s (неизвестное разрешение)" % p)
        elif PERMISSIONS[p] is None:
            skipped.append("%s (на этой станции нет)" % p)
        elif PERMISSIONS[p] not in granted:
            granted.append(PERMISSIONS[p])
    net = toml.get("network") if isinstance(toml.get("network"), dict) else {}
    allow = [str(h).lower() for h in net.get("allow", []) if isinstance(h, str) and re.fullmatch(r"(\*\.)?[a-z0-9.-]{1,120}", str(h).lower())]
    if "network" in (perms if isinstance(perms, list) else []) and not allow:
        raise ValueError("разрешение network требует непустой список [network].allow в plugin.toml")
    version = str(toml.get("version", "1.0.0"))
    if re.fullmatch(r"\d+\.\d+", version):
        version += ".0"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        version = "1.0.0"
    manifest = {
        "id": pid, "name": str(toml.get("name", pid))[:60], "version": version, "author": str(toml.get("author", "Luna"))[:60], "api": "1", "icon": "star",
        "description": (str(toml.get("description", "")) + " (плагин для Luna, работает через совместимость с Luna)").strip()[:300],
        "permissions": [], "side": "speaker",
        "device": {"runtime": "js", "binary": "device/main.js", "permissions": granted},
        "min_firmware": "0.3.7",
    }
    web = {}                                                 # the pages of the plugin (register.customweb): web/... travel with the package, the server shows them
    for n, data in files.items():
        rel = n[len(base):] if n.startswith(base) else ""
        if rel.startswith("web/") and os.path.splitext(rel)[1].lower() in WEB_EXT and len(web) < 50:
            web[rel] = data
    head = "--lua\n__LUNA = {allow = {%s}, insecure = %s}\n" % (", ".join('"%s"' % h for h in allow), "true" if net.get("allow_insecure_http") is True else "false")
    return {"manifest": manifest, "code": head + code, "skipped": skipped, "web": web}


def is_aarch64_elf(raw: bytes) -> bool:
    """A 64-bit little-endian ARM (aarch64) executable, statically linked (no interpreter): the only program that runs on the speakers as it is."""
    if len(raw) < 64 or raw[:4] != b"\x7fELF" or raw[4] != 2 or raw[5] != 1:
        return False
    if int.from_bytes(raw[18:20], "little") != 183 or int.from_bytes(raw[16:18], "little") not in (2, 3):   # EM_AARCH64; an executable
        return False
    phoff, phentsize, phnum = int.from_bytes(raw[32:40], "little"), int.from_bytes(raw[54:56], "little"), int.from_bytes(raw[56:58], "little")
    for i in range(phnum):
        off = phoff + i * phentsize
        if off + 4 <= len(raw) and int.from_bytes(raw[off:off + 4], "little") == 3:                           # PT_INTERP: needs a dynamic loader the speaker does not have
            return False
    return True


def convert_binary(files: dict, base: str, toml: dict, launch: dict) -> dict:
    """A plugin of Luna that is a ready program ([launch] kind = "binary"): it runs on the speaker as a 64-bit program, under its own user in a folder of its own, without a
    sandbox filter, and only when the owner switched "programs of plugins" on. It talks to the speaker through a local API (YAM_API_URL / YAM_API_TOKEN)."""
    pid = str(toml.get("id", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9]+([.\-][a-z0-9]+)*", pid) or not pid:
        raise ValueError("id плагина: латинские буквы, цифры, точка и дефис, а здесь: %r" % pid[:60])
    if "." not in pid:
        pid = "luna." + pid                                              # a package id of lostOS looks like a.b
    path = str(launch.get("path") or toml.get("entry", ""))
    if not _safe(path) or (base + path) not in files:
        raise ValueError("в пакете нет программы: " + path[:60])
    raw = files[base + path]
    if not is_aarch64_elf(raw):
        raise ValueError("программа не годится: нужен статический исполняемый файл aarch64 (под штатную систему Яндекса «под другую систему» такие не подходят)")
    perms = toml.get("permissions", [])
    perms = perms if isinstance(perms, list) else []
    granted, skipped = ["exec.bin64"], []
    for p in perms:
        if p == "exec":
            continue
        if p not in PERMISSIONS:
            skipped.append("%s (неизвестное разрешение)" % p)
        elif PERMISSIONS[p] is None:
            skipped.append("%s (на этой станции нет)" % p)
        elif PERMISSIONS[p] not in granted:
            granted.append(PERMISSIONS[p])
    version = str(toml.get("version", "1.0.0"))
    if re.fullmatch(r"\d+\.\d+", version):
        version += ".0"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        version = "1.0.0"
    manifest = {
        "id": pid, "name": str(toml.get("name", pid))[:60], "version": version, "author": str(toml.get("author", "Luna"))[:60], "api": "1", "icon": "star",
        "description": (str(toml.get("description", "")) + " (программа плагина Luna: запускается на колонке как есть)").strip()[:300],
        "permissions": [], "side": "speaker",
        "device": {"runtime": "bin64", "binary": "device/plugin", "permissions": granted, "args": [str(a)[:200] for a in (launch.get("args") if isinstance(launch.get("args"), list) else [])[:16]]},
        "min_firmware": "0.3.7",
    }
    return {"manifest": manifest, "code": "", "skipped": skipped, "web": {}, "binary": raw}


def to_package(data: bytes):
    """The bytes of an archive with plugin.toml (and no manifest.json) -> the bytes of a lostOS package; None when it is not a Luna package.
    Raises ValueError when it is one but cannot be used."""
    files = read_files(data)
    names = list(files)
    if not any(n.split("/")[-1] == "plugin.toml" for n in names) or any(n.split("/")[-1] == "manifest.json" for n in names):
        return None
    r = convert(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(r["manifest"], ensure_ascii=False, indent=2) + "\n")
        if r.get("binary"):
            z.writestr("device/plugin", r["binary"])
        else:
            z.writestr("device/main.js", r["code"])
        for rel, data in r["web"].items():
            z.writestr(rel, data)
    return out.getvalue()
