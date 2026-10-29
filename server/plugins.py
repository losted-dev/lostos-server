"""lostOS plugins (server side): finding them, their permissions, one process each, and a small JSON-lines protocol over stdio.

A plugin is a folder with manifest.json and an entry script (Python):

    plugins/system/<id>/   built in, cannot be switched off or removed ("system": true counts only there)
    plugins/user/<id>/     installed by the user, switched off until the user allows its permissions

manifest.json:
    {"id": "ru.example.stats", "name": "Статистика", "version": "1.0.0", "author": "Имя", "api": "1",
     "icon": "chart", "permissions": ["media.read", "events.subscribe", "storage", "ui.tile"], "entry": "main.py"}

The plugin talks to the server with one JSON object per line: it sends {"id": 1, "method": "media.state", "params": {}} on its stdout
and gets {"id": 1, "result": ...} or {"id": 1, "error": "..."} on its stdin; the server also pushes {"event": "media", "data": {...}}.
Every method needs a permission: a call without it is refused, counted as a violation (three of them switch the plugin off and the user is told).
Honest limit of this first version: the plugin is a normal process of the PC and could open sockets or files on its own; the permissions
govern what the server does for it. The signatures and the OS-level sandbox are the next step (docs/lostos-architecture.md, 4.4)"""
import asyncio
import datetime
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import time
import urllib.parse
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "plugins")                                    # the built-in packages (shared code)
USER_ROOT = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "plugins", "user")     # the packages a person installed himself
STATE_FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "plugins_state.json")


def kind_root(kind: str) -> str:
    return USER_ROOT if kind == "user" else os.path.join(ROOT, kind)

PERMISSIONS = {
    "media.read": "видеть, что играет",
    "media.control": "управлять музыкой (пауза, дальше, громкость)",
    "device.read": "читать состояние колонки (громкость)",
    "events.subscribe": "получать события (смена трека)",
    "storage": "хранить свои данные",
    "ui.tile": "показывать плитку на главной",
    "weather.read": "читать погоду",
    "assistant.intercept": "перехватывать фразы, сказанные ассистенту: слышит всё, что сказано после «Алиса», и может ответить сам, не пуская штатного",
    "assistant.tools": "добавлять свои команды ассистенту: их смогут вызывать встроенная модель и любые «мозги»",
    "speech.tts": "озвучивать ответы ассистента своим голосом (получает тексты ответов)",
    "speech.stt": "распознавать речь вместо штатного распознавателя (получает записи голоса)",
    "brain.replace": "заменить нейросеть ассистента своей: получает фразы, историю и контекст дома и может запускать все команды колонки",
    "alice.api": "пользоваться голосом и слухом Алисы через alice-api: озвучить текст и распознать запись (всё уходит в облако Яндекса)",
}
# what a plugin program ON THE SPEAKER may be allowed (the firmware checks every request, see satellite/src/plugins/host.h)
DEVICE_PERMISSIONS = {
    "device.led": "светить кольцом колонки",
    "device.display": "показывать текст на часах колонки",
    "media.control": "управлять музыкой на колонке",
    "device.read": "узнавать громкость колонки",
    "events.subscribe": "получать события колонки (кнопки, музыка, громкость)",
    "events.publish": "отправлять события в Home Assistant",
    "storage": "хранить свои данные на колонке",
    "network.connect": "подключаться по сети к другим устройствам и сайтам (через прошивку; сама колонка и её службы закрыты)",
    "network.listen": "принимать подключения из сети и слушать широковещание (нужно для приёмников вроде Chromecast)",
    "audio.play": "играть звук через динамик колонки как источник музыки",
    "assistant.intercept": "забирать кнопку Алисы: нажатие не запускает ассистента, плагин решает сам",
    "assistant.tools": "добавлять ассистенту свои команды (их выполняет колонка)",
}
RISKY = {"media.control", "network.connect", "network.listen", "audio.play", "assistant.intercept", "brain.replace", "assistant.tools", "speech.tts", "speech.stt", "alice.api"}                         # shown with a warning when the user allows them
# What a permission means to a person: (level, what the plugin gets, what it risks). Shown on the page of the plugin.
# Levels: normal, sensitive (it can do things), dangerous (it sees private data or reaches outside).
PERMISSION_INFO = {
    "media.read": ("normal", "Видеть, что играет", "Название трека, исполнитель, источник и играет ли сейчас", ""),
    "media.control": ("sensitive", "Управлять музыкой", "Пауза, следующий и предыдущий трек, громкость", "Может сам останавливать и включать музыку"),
    "device.read": ("normal", "Читать состояние колонки", "Узнаёт текущую громкость", ""),
    "events.subscribe": ("normal", "Получать события", "Узнаёт, когда меняется трек или громкость", ""),
    "storage": ("normal", "Хранить свои данные", "Небольшой файл в своей папке (до 200 КБ на сервере)", ""),
    "ui.tile": ("normal", "Плитка на главной", "Показывает блок с короткой надписью на странице «Сейчас»", ""),
    "weather.read": ("normal", "Читать погоду", "Видит текущую погоду вашего города", ""),
    "assistant.intercept": ("dangerous", "Перехватывать фразы", "Слышит каждую фразу, сказанную ассистенту, и может ответить вместо него",
                            "Видит ваши фразы целиком и может подменять ответы"),
    "brain.replace": ("dangerous", "Заменить нейросеть ассистента", "Получает фразы, историю разговора, описание умного дома и список команд; сам решает, что ответить и что запустить",
                      "Видит содержимое разговоров и данные дома и может управлять устройствами так же, как ассистент"),
    "assistant.tools": ("dangerous", "Добавлять команды ассистенту", "Своими командами ассистент сможет управлять тем, что делает плагин",
                        "Описания команд попадают в запрос к нейросети, а вызвать команду может любая сказанная фраза"),
    "speech.tts": ("dangerous", "Озвучивать ответы", "Читает вслух ответы ассистента своим голосом", "Получает текст каждого ответа; если плагин отправляет его в интернет, он уходит туда"),
    "speech.stt": ("dangerous", "Распознавать речь", "Распознаёт сказанное вместо штатного распознавателя", "Получает записи вашего голоса"),
    "alice.api": ("dangerous", "Голос и слух Алисы", "Через alice-api озвучивает любой текст голосом Алисы и распознаёт записи речи",
                  "Тексты и записи голоса уходят в облако Яндекса; пока alice-api выключен, это не работает"),
}
DEVICE_PERMISSION_INFO = {
    "device.led": ("sensitive", "Светить кольцом", "Зажигает кольцо колонки любым цветом", ""),
    "device.display": ("sensitive", "Писать на часах", "Показывает свой текст на дисплее колонки", ""),
    "media.control": ("sensitive", "Управлять музыкой", "Пауза, следующий трек, громкость", "Может сам останавливать и включать музыку"),
    "device.read": ("normal", "Узнавать громкость", "Видит громкость колонки", ""),
    "events.subscribe": ("normal", "Получать события колонки", "Нажатия кнопок, смена трека, изменение громкости", ""),
    "events.publish": ("sensitive", "Отправлять события в Home Assistant", "Посылает сообщения, на которые умный дом может реагировать", ""),
    "storage": ("normal", "Хранить свои данные", "Несколько записей в памяти колонки (до 32 штук)", ""),
    "network.connect": ("dangerous", "Выходить в сеть", "Подключается к сайтам и устройствам в сети через прошивку; саму колонку и её службы открыть не может",
                        "Может отправить в интернет всё, что знает сам (события колонки, которые вы ему разрешили)"),
    "network.listen": ("dangerous", "Принимать подключения", "Открывает порт на колонке и слушает сеть (для приёмников вроде Chromecast)",
                       "К открытому порту смогут подключиться другие устройства сети"),
    "assistant.intercept": ("dangerous", "Забирать кнопку Алисы", "Нажатие центральной кнопки не запускает ассистента: плагин получает его и решает, что делать",
                            "Пока плагин работает, кнопка не вызовет Алису (остановка таймера и отмена диалога работают). При остановке или удалении плагина кнопка возвращается"),
    "audio.play": ("sensitive", "Играть звук", "Играет звук через динамик как источник музыки", "Может прервать вашу музыку и играть что угодно"),
    "assistant.tools": ("sensitive", "Команды для ассистента", "Ассистент может вызывать команды плагина по вашей просьбе; их выполняет сама колонка",
                        "Плагин получает то, что ассистент передаёт в команду (например, название станции из вашей фразы)"),
}
IMAGE_TYPES = {".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".webp": b"RIFF", ".gif": b"GIF8"}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_README = 100_000
MAX_LOGO = 300_000
TILE_SWITCHES = {"airplay"}          # a tile may carry the switch of what its plugin runs on the speaker (the plugin stays on, the receiver stops)
ICONS = {"sun", "cloud", "music", "chart", "bolt", "bell", "home", "star", "clock", "note", "airplay", "spotify", "cpu", "memory", "disk"}
TILE_SHELVES = {"now", "sources"}                           # the shelves of the home page a tile can be put on
TILE_TINTS = {"", "air", "sp", "bt", "alarm"}
VIOLATIONS_LIMIT = 3
# Built-in packages have no program of their own in a folder: the service is part of the server ("llm", "alice-api") or of the firmware of
# the speaker ("speaker": AirPlay, Spotify). They are plugins for the owner: a page, permissions, a switch where the package allows one.
BUILTINS = {"llm", "alice-api", "speaker"}
_question_ids = itertools.count(1)
STREAM_LIMIT = 32 * 1024 * 1024        # one line from a plugin may carry a few seconds of speech (base64)
MAX_TOOLS = 16


class ZygoteChild:
    """A plugin process forked by the zygote of the shared server (zygote.py): the part of asyncio.subprocess.Process the manager uses.
    Its exit code comes on the status pipe; when the zygote is gone first (the gateway restarted) the plugin is an orphan, and its end is its pid disappearing."""

    def __init__(self, pid: int, stdin, stdout, stderr, status):
        self.pid, self.stdin, self.stdout, self.stderr = pid, stdin, stdout, stderr
        self.returncode = None
        self._end = asyncio.get_running_loop().create_task(self._watch(status))

    async def _watch(self, status):
        line = await status.readline()
        try:
            self.returncode = int(line)
            return
        except ValueError:
            pass
        while True:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
            await asyncio.sleep(1)
        self.returncode = -1                                # its real code went with the zygote

    async def wait(self) -> int:
        await asyncio.shield(self._end)
        return self.returncode

    def send_signal(self, sig):
        if self.returncode is not None:
            raise ProcessLookupError(self.pid)
        os.kill(self.pid, sig)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)


def own_cgroup() -> str:
    """The cgroup.procs of this household (its memory wall, tenants.Cgroups): its plugins go into it, as children of the household would."""
    try:
        line = [l for l in open("/proc/self/cgroup", encoding="utf-8") if l.startswith("0::")][0].strip()[3:]
        path = "/sys/fs/cgroup" + line + "/cgroup.procs"
        return path if os.access(path, os.W_OK) else ""
    except (OSError, IndexError):
        return ""


def _ask_zygote(path: str, req: bytes, fds: list) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(path)
        socket.send_fds(s, [req], fds)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0])


async def fork_plugin(entry: str, cwd: str, env: dict, log=print):
    """A plugin process forked by the zygote, sharing its preloaded libraries (-> ZygoteChild); None when this household has no zygote
    (a speaker's own server, another system, LOSTOS_ZYGOTE=0, the zygote gone): the caller starts a fresh python."""
    path = os.environ.get("LOSTOS_ZYGOTE_SOCK", "")
    if not path or not hasattr(socket, "send_fds") or not os.path.exists(path):
        return None
    pipes = [os.pipe() for _ in range(4)]                   # stdin, stdout, stderr, status
    theirs = [pipes[0][0], pipes[1][1], pipes[2][1], pipes[3][1]]
    ours = [pipes[0][1], pipes[1][0], pipes[2][0], pipes[3][0]]
    req = json.dumps({"plugin": {"entry": entry, "cwd": cwd, "env": env, "cgroup": own_cgroup()}}).encode() + b"\n"
    try:
        answer = await asyncio.to_thread(_ask_zygote, path, req, theirs)
    except (OSError, ValueError) as e:
        answer = {"error": repr(e)}
    for fd in theirs:
        os.close(fd)
    if "pid" not in answer:
        for fd in ours:
            os.close(fd)
        log(f"plugins: the zygote did not fork {entry} ({answer.get('error')}), a fresh python instead")
        return None
    loop = asyncio.get_running_loop()

    async def reader(fd, limit):
        r = asyncio.StreamReader(limit=limit, loop=loop)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(r, loop=loop), os.fdopen(fd, "rb", 0))
        return r
    transport, protocol = await loop.connect_write_pipe(lambda: asyncio.streams.FlowControlMixin(loop=loop), os.fdopen(ours[0], "wb", 0))
    stdin = asyncio.StreamWriter(transport, protocol, None, loop)
    return ZygoteChild(answer["pid"], stdin, await reader(ours[1], STREAM_LIMIT), await reader(ours[2], STREAM_LIMIT), await reader(ours[3], 64))


def _check_image(folder: str, rel: str, limit: int, kinds: tuple) -> str:
    """Empty when rel is a real picture of an allowed kind inside the plugin folder (checked by its first bytes, not by its name), else the reason.
    SVG is not allowed anywhere: it can carry a script, and the pictures of a plugin are shown on the origin of the panel."""
    rel = rel.replace("\\", "/")
    ext = os.path.splitext(rel)[1].lower()
    if rel.startswith("/") or ".." in rel.split("/") or ":" in rel or ext[1:] not in kinds:
        return "картинка: " + ", ".join(kinds) + ", путь внутри плагина"
    path = os.path.realpath(os.path.join(folder, rel))
    if not path.startswith(os.path.realpath(folder) + os.sep) or not os.path.isfile(path):
        return "файла нет"
    if os.path.getsize(path) > limit:
        return f"больше {limit // 1000} КБ"
    with open(path, "rb") as f:
        if ext not in IMAGE_TYPES or not f.read(8).startswith(IMAGE_TYPES[ext]):
            return "содержимое не похоже на картинку"
    return ""


def _valid_manifest(d: dict, kind: str, folder: str):
    """-> (manifest, error)"""
    try:
        if not re.fullmatch(r"[a-z0-9]+([.\-][a-z0-9]+)+", str(d.get("id", ""))):
            return None, "id: что-то вроде ru.example.name"
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(d.get("version", ""))):
            return None, "version: 1.0.0"
        name, author = str(d.get("name", "")).strip(), str(d.get("author", "")).strip()
        if not (1 <= len(name) <= 40) or not (1 <= len(author) <= 60):
            return None, "name и author обязательны"
        perms = d.get("permissions", [])
        if not isinstance(perms, list) or any(p not in PERMISSIONS for p in perms):
            return None, "неизвестное разрешение: " + ", ".join(str(p) for p in perms if p not in PERMISSIONS)
        builtin = d.get("builtin")
        if builtin is not None and (kind != "system" or builtin not in BUILTINS):
            return None, "builtin: только у встроенных пакетов (" + ", ".join(sorted(BUILTINS)) + ")"
        dev_js = isinstance(d.get("device"), dict) and d["device"].get("runtime") == "js"
        entry = str(d.get("entry", "" if dev_js else "main.py"))          # a JavaScript plugin of the speaker may have no server part at all
        if not builtin and entry and (entry.startswith(("/", "\\")) or ".." in entry.replace("\\", "/").split("/") or not os.path.isfile(os.path.join(folder, entry))):
            return None, "entry: нет такого файла"
        if not builtin and not entry and not dev_js:
            return None, "entry: нужен скрипт плагина"
        tile = d.get("tile")                                # the tile of a built-in package on the home page: it exists while the plugin is on
        if tile is not None:
            if not builtin or not isinstance(tile, dict) or tile.get("shelf") not in TILE_SHELVES or tile.get("tint", "") not in TILE_TINTS \
                    or not 1 <= len(str(tile.get("title", ""))) <= 24:
                return None, "tile: только у встроенных пакетов, {shelf: now|sources, title, icon, tint}"
            tile = {"shelf": tile["shelf"], "title": str(tile["title"]), "icon": tile.get("icon") if tile.get("icon") in ICONS else "star", "tint": tile.get("tint", ""),
                    "switch": tile.get("switch") if tile.get("switch") in TILE_SWITCHES else ""}
        min_fw = str(d.get("min_firmware", "")).strip()           # the oldest lostOS of the speaker that the plugin works with: "0.3.0"
        if min_fw and not re.fullmatch(r"\d+\.\d+(\.\d+)?", min_fw):
            return None, "min_firmware: версия прошивки lostOS вида 0.3.0"
        requires = d.get("requires", [])
        if not isinstance(requires, list) or any(not isinstance(x, str) for x in requires):
            return None, "requires: список id плагинов"
        dev = d.get("device")
        device = None
        if dev is not None:
            if not isinstance(dev, dict):
                return None, "device: объект с binary и permissions"
            runtime = "js" if dev.get("runtime") == "js" else "native"
            binary = None if builtin else str(dev.get("binary", "device/main.js" if runtime == "js" else "device/plugin"))   # a built-in package brings no program: the firmware has it
            dperms = dev.get("permissions", [])
            if binary and (binary.startswith(("/", "\\")) or ".." in binary.replace("\\", "/").split("/") or not os.path.isfile(os.path.join(folder, binary))):
                return None, "device.binary: нет такого файла"
            if not isinstance(dperms, list) or any(p not in DEVICE_PERMISSIONS for p in dperms):
                return None, "device.permissions: неизвестное разрешение колонки"
            device = {"binary": binary, "permissions": list(dict.fromkeys(dperms)), "runtime": runtime}
        readme = str(d.get("readme", "README.md"))
        if readme.startswith(("/", "\\")) or ".." in readme.replace("\\", "/").split("/") or not readme.lower().endswith(".md"):
            return None, "readme: относительный путь к .md"
        logo = d.get("logo")
        if logo is not None:
            problem = _check_image(folder, str(logo), MAX_LOGO, ("png", "jpg", "jpeg", "webp"))
            if problem:
                return None, "logo: " + problem
        return {"id": d["id"], "name": name, "device": device, "readme": readme, "logo": str(logo) if logo else None, "version": d["version"], "author": author, "api": str(d.get("api", "1")),
                "icon": d.get("icon") if d.get("icon") in ICONS else "star", "permissions": list(dict.fromkeys(perms)), "entry": entry,
                "description": str(d.get("description", ""))[:200], "system": kind == "system" and bool(d.get("system", True)),
                "min_firmware": min_fw, "tile": tile, "builtin": builtin, "switchable": kind == "system" and bool(d.get("switchable")), "requires": list(requires),
                "side": "speaker" if d.get("side") == "speaker" else "server"}, ""
    except Exception as e:                                  # a broken manifest must never break the server
        return None, repr(e)


# What a plugin process inherits from the environment of the server: what the OS and Python need to run and reach the network, nothing else.
# The vault puts the owner's secrets (GEMINI_API_KEY, HA_TOKEN, ...) into os.environ of the server: a copy of it would hand them to every plugin.
PLUGIN_ENV = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
              "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
              "https_proxy", "no_proxy", "VIRTUAL_ENV")


def plugin_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in PLUGIN_ENV}


class Plugin:
    def __init__(self, manifest: dict, folder: str, kind: str):
        self.m, self.folder, self.kind = manifest, folder, kind
        self.sig = {"state": "none"}                         # the signature of a built-in plugin (a user plugin keeps it in the state file, see Manager.install)
        self.proc = None
        self.reader = None
        self.tile = None
        self.subscribed = False
        self.error = ""
        self.stderr_tail = ""                               # the last lines it wrote to stderr (see Manager._drain_stderr)
        self.tools: dict = {}                               # commands the plugin gave to the assistant: name -> declaration (JSON-schema)
        self.asking: dict = {}                              # id of a question the server put to the plugin -> the future of its answer

    @property
    def id(self) -> str:
        return self.m["id"]



def inspect_package(data: bytes):
    """The checks of a package that need no state (the installer of the server and the checks of the registry on GitHub share them):
    -> (zip, names of the files, top folder prefix, the manifest as it is). Raises ValueError with a message for the person."""
    if len(data) > Manager.MAX_PACKAGE:
        raise ValueError("пакет больше 6 МБ")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("это не zip-пакет")
    if len(z.infolist()) > Manager.MAX_FILES:
        raise ValueError("в пакете слишком много файлов")
    names = [i.filename for i in z.infolist() if not i.is_dir()]
    if sum(i.file_size for i in z.infolist()) > Manager.MAX_UNPACKED:
        raise ValueError("после распаковки слишком много данных")
    prefix = ""                                          # the package may sit in one top folder
    if "manifest.json" not in names:
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(tops) == 1 and (next(iter(tops)) + "/manifest.json") in names:
            prefix = next(iter(tops)) + "/"
        else:
            raise ValueError("в пакете нет manifest.json")
    seen = set()
    for n in names:
        parts = n.split("/")
        # no absolute paths, "..", drive letters, NTFS streams ("a.txt:x"), control characters, names that Windows treats specially
        if ("\\" in n or any(x in ("", ".", "..") for x in parts) or ":" in n or any(ord(c) < 32 for c in n)
                or any(x != "." and x != x.rstrip(" .") for x in parts) or any(x.split(".")[0].upper() in WINDOWS_RESERVED for x in parts)):
            raise ValueError("недопустимый путь в пакете: " + n[:60])
        key = n.casefold()                               # "main.py" and "MAIN.py" are one file on Windows and macOS
        if key in seen:
            raise ValueError("повторяющийся путь в пакете: " + n[:60])
        seen.add(key)
    for n in seen:
        parts = n.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ValueError("файл используется как папка в пакете: " + n[:60])
    for i in z.infolist():
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("ссылки в пакете не допускаются: " + i.filename[:60])
    try:
        d = json.loads(z.read(prefix + "manifest.json").decode("utf-8"))
    except (ValueError, KeyError):
        raise ValueError("manifest.json не читается")
    return z, names, prefix, d


def validate_package(data: bytes):
    """Everything the installer checks without installing: -> (manifest, "") or (None, reason). Used by the checks of the plugin registry."""
    import tempfile
    try:
        z, names, prefix, d = inspect_package(data)
    except ValueError as e:
        return None, str(e)
    stage = tempfile.mkdtemp(prefix="lostos-validate-")
    try:
        for n in names:
            if n.startswith(prefix):
                dst = os.path.join(stage, n[len(prefix):])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(z.read(n))
        man, err = _valid_manifest(d, "user", stage)
        return (man, "") if man else (None, "манифест: " + err)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class Manager:
    def __init__(self, srv):
        self.srv = srv                                      # the voice_server module (media, notify, settings ...)
        self.items: dict = {}
        self.state: dict = {}
        self.bad: list = []                                 # manifests that were refused: [{"folder", "error"}]
        self._task = None
        self.loop = None                                    # the server's event loop (for code that runs in threads, see ask_sync)
        self.device: dict = {}                              # id -> what the speaker last said about its copy of the plugin
        self.device_tools: dict = {}                        # id -> {name: declaration} of the commands its copy on the speaker declared
        self.device_waiting: dict = {}                      # call id -> future of the answer of a command on the speaker

    # ---- the device part: a native program of the speaker (the firmware keeps it in the sandbox)
    def device_sha(self, p: Plugin) -> str:
        import hashlib
        return hashlib.sha256(open(os.path.join(p.folder, p.m["device"]["binary"]), "rb").read()).hexdigest()

    async def device_command(self, line: str) -> bool:
        """Sends a plugin command to the speaker; False when the speaker is not connected (nothing was sent)"""
        svc = self.srv.state_services.get("assistant")
        cli = self.srv.PANEL.get("cli")
        if not (svc and cli):
            return False
        await cli.execute_service(svc, {"command": "plugin " + line})
        return True

    async def device_install(self, p: Plugin):
        """Asks the speaker to download the program (over http from this server, checked by its sha-256) and to start it in the sandbox."""
        dev = p.m["device"]
        if not dev or not dev["binary"]:
            return
        perms = ",".join(k for k in dev["permissions"] if k in self.granted(p) or k in self.state.get(p.id, {}).get("granted_device", []))
        dl = f"{self.srv.GLOBALS['base_url']}/plugins-dl"
        self.device[p.id] = "скачивается"
        # the last word: where the speaker finds what it needs to check the signature of the package ITSELF (the signature file, the manifest, the signed list of developers):
        # it grants the dangerous permissions only to a package that a trusted key has signed, whatever this server asks for
        js = " js" if dev.get("runtime") == "js" else ""                # the speaker keeps main.js and runs it with its JavaScript runtime
        await self.device_command(f"install {p.id} {dl}/{p.id}/plugin {self.device_sha(p)} {perms or '-'} {dl}{js}")

    # ---- discovery and state
    def scan(self):
        old = self.items                                    # install and remove scan again: the plugins that run keep their process, commands and tile
        self.items, self.bad = {}, []
        for kind in ("system", "user"):
            base = kind_root(kind)
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                folder = os.path.join(base, name)
                mf = os.path.join(folder, "manifest.json")
                if not os.path.isfile(mf):
                    continue
                try:
                    d = json.load(open(mf, encoding="utf-8"))
                except (OSError, ValueError) as e:
                    self.bad.append({"folder": name, "error": str(e)[:100]})
                    continue
                man, err = _valid_manifest(d, kind, folder)
                if not man:
                    self.bad.append({"folder": name, "error": err})
                elif man["id"] in self.items:
                    self.bad.append({"folder": name, "error": "такой id уже есть"})
                else:
                    prev = old.get(man["id"])
                    if prev and prev.folder == folder and prev.kind == kind:
                        prev.m = man
                        self.items[man["id"]] = prev
                    else:
                        self.items[man["id"]] = Plugin(man, folder, kind)
                    if kind == "system":                                                # a built-in plugin carries the signature of the team too
                        import plugin_sign
                        self.items[man["id"]].sig = plugin_sign.trust_state(plugin_sign.check_folder(folder), plugin_sign.trusted_keys())
        try:
            self.state = json.load(open(STATE_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    def _save(self):
        try:
            json.dump(self.state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except OSError:
            pass

    def enabled(self, p: Plugin) -> bool:
        if self.blocked(p):
            return False
        if p.m["system"] and p.m["builtin"] == "speaker":    # the speaker keeps this switch (it survives a reboot there); unknown counts as on
            return not self.device.get(p.id, "").startswith("off")
        if p.m["system"]:                                   # a system plugin is always on unless its package says the owner may switch it off
            return bool(self.state.get(p.id, {}).get("enabled", True)) if p.m["switchable"] else True
        return bool(self.state.get(p.id, {}).get("enabled"))

    def plugin_on(self, pid: str) -> bool:
        """Is this plugin on (a missing one counts as on: the caller has its own way to notice that)?"""
        p = self.items.get(pid)
        return self.enabled(p) if p else True

    def service_on(self, key: str) -> bool:
        """Is the built-in service ("llm", "alice-api") on? A missing package counts as on: the server must not lose its voice because a folder is gone."""
        return all(self.enabled(p) for p in self.items.values() if p.m["builtin"] == key)

    def is_running(self, p: Plugin) -> bool:
        if p.m["builtin"] == "speaker":                      # the speaker says it (the plugin list of the firmware)
            return self.device.get(p.id, "").startswith("running")
        if p.m["builtin"]:
            return self.enabled(p)
        return bool(p.proc and p.proc.returncode is None)

    def device_text(self, p: Plugin) -> str:
        raw = self.device.get(p.id, "")
        if p.m["builtin"] != "speaker":
            return raw
        return {"running": "работает", "paused": "приёмник остановлен (его уступили на время)", "off": "выключен", "": "колонка не на связи"}.get(raw.split(":")[0], raw)

    def verified(self, p: Plugin) -> bool:
        """Made by the lostOS team or by a developer it trusts: a built-in plugin, or a package whose signature checks against a trusted key."""
        return p.m["system"] or ((p.sig if p.kind == "system" else self.state.get(p.id, {}).get("signature")) or {}).get("state") == "trusted"

    def granted(self, p: Plugin) -> list:
        return p.m["permissions"] if p.m["system"] else [x for x in self.state.get(p.id, {}).get("granted", []) if x in p.m["permissions"]]

    def listing(self) -> list:
        out = []
        for p in self.items.values():
            out.append({**p.m, "logo": f"/plugins-asset/{p.id}/{urllib.parse.quote(p.m['logo'])}" if p.m["logo"] else None, "kind": p.kind, "enabled": self.enabled(p), "granted": self.granted(p), "running": self.is_running(p),
                        "violations": self.state.get(p.id, {}).get("violations", 0), "error": p.error, "verified": self.verified(p), "signature": (p.sig if p.kind == "system" else self.state.get(p.id, {}).get("signature")) or {"state": "none"},
                        "permission_names": {k: PERMISSIONS[k] for k in p.m["permissions"]}, "risky": [k for k in p.m["permissions"] if k in RISKY],
                        "device_permission_names": {k: DEVICE_PERMISSIONS[k] for k in (p.m["device"] or {}).get("permissions", [])},
                        "device_state": self.device_text(p) if p.m["device"] else None})
        return out

    def source_of(self, p: Plugin) -> dict:
        """Where the plugin came from: a built-in package, the repository, or a manual upload (a .lstplugin file or a folder put in by hand)"""
        if p.m["system"]:
            sig = p.sig
            who = {"trusted": "Подписан: " + str(sig.get("publisher", "")), "bad": "Подпись не сходится: файлы изменены после подписания", "none": "Без подписи"}.get(sig.get("state"), "")
            return {"type": "builtin", "title": "Встроенный пакет",
                    "detail": "Входит в состав lostOS и работает с доверием системы, а не в песочнице: как сама прошивка. " + who, "signature": sig.get("state", "none")}
        src = self.state.get(p.id, {}).get("source") or {}
        sig = self.state.get(p.id, {}).get("signature") or {"state": "none"}
        mark = {"trusted": "Подписан: " + str(sig.get("publisher", "")), "known": "Подпись верна, но автор неизвестен: " + str(sig.get("publisher", "")), "none": "Без подписи",
                "revoked": "Ключ автора отозван командой lostOS: плагин выключен"}.get(sig.get("state"), "")
        if src.get("type") == "repository":
            return {"type": "repository", "title": "Репозиторий lostOS", "detail": (f"версия {src['version']}, " if src.get("version") else "") + mark, "signature": sig.get("state", "none")}
        if src.get("type") == "file":
            return {"type": "manual", "title": "Ручная загрузка",
                    "detail": (f"файл «{src['name']}», " if src.get("name") else "файл, ") + f"{src.get('at', '')}, SHA-256 {src.get('sha256', '')[:16]}… · {mark}", "signature": sig.get("state", "none")}
        return {"type": "manual", "title": "Ручная загрузка", "detail": "папка plugins/user, положена вручную"}

    def page(self, pid: str):
        """Everything the page of a plugin shows (the README goes out as text: the panel draws it without any HTML)"""
        p = self.items.get(pid)
        if not p:
            return None
        granted = self.granted(p)
        dev = p.m["device"] or {}

        def rows(names, table, have):
            out = []
            for k in names:
                level, title, what, risk = table[k]
                out.append({"id": k, "level": level, "title": title, "what": what, "risk": risk, "granted": k in have})
            return out

        readme = ""
        try:
            with open(os.path.join(p.folder, p.m["readme"]), "rb") as f:
                readme = f.read(MAX_README + 1).decode("utf-8", "replace")
            if len(readme) > MAX_README:
                readme = readme[:MAX_README] + "\n\n…(описание обрезано)"
        except OSError:
            pass
        granted_dev = dev.get("permissions", []) if p.m["system"] else self.state.get(p.id, {}).get("granted_device", [])
        return {"id": p.id, "name": p.m["name"], "version": p.m["version"], "author": p.m["author"], "description": p.m["description"], "icon": p.m["icon"],
                "logo": f"/plugins-asset/{p.id}/{urllib.parse.quote(p.m['logo'])}" if p.m["logo"] else None, "system": p.m["system"], "verified": self.verified(p),
                "source": self.source_of(p), "signature": (p.sig if p.kind == "system" else self.state.get(p.id, {}).get("signature")) or {"state": "none"}, "enabled": self.enabled(p), "running": self.is_running(p), "error": p.error,
                "builtin": p.m["builtin"], "switchable": p.m["switchable"], "side": p.m["side"],
                "min_firmware": p.m["min_firmware"], "firmware": ".".join(map(str, self.speaker_version())), "firmware_problem": self.requirement(p),
                "requires": [{"id": q.id, "name": q.m["name"], "enabled": self.enabled(q)} for q in (self.items.get(i) for i in p.m["requires"]) if q],
                "used_by": [{"id": q.id, "name": q.m["name"], "enabled": self.enabled(q)} for q in self.items.values() if p.id in q.m["requires"]],
                "violations": self.state.get(p.id, {}).get("violations", 0),
                "permissions": rows(p.m["permissions"], PERMISSION_INFO, granted),
                "device_permissions": rows(dev.get("permissions", []), DEVICE_PERMISSION_INFO, granted_dev),
                "device_state": self.device_text(p) if p.m["device"] else None,
                "readme": readme, "assets": f"/plugins-asset/{p.id}/"}

    def asset_path(self, pid: str, rel: str):
        """The file of a picture inside a plugin (README images, the logo) or None. Only pictures, never the data folder, never outside the plugin."""
        p = self.items.get(pid)
        if not p:
            return None
        rel = rel.replace("\\", "/")
        if rel.split("/")[0].lower() == "data" or _check_image(p.folder, rel, 3_000_000, ("png", "jpg", "jpeg", "webp", "gif")):
            return None
        return os.path.join(p.folder, rel)

    # ---- manual installation: a package is a zip (.lostos) with manifest.json at its root
    MAX_PACKAGE = 6_000_000          # bytes of the zip
    MAX_UNPACKED = 24_000_000
    MAX_FILES = 600

    def trust_of(self, data: bytes) -> dict:
        """What the signature of a package says and whom it is trusted to (see plugin_sign.py): the keys of the team and the developers of the signed registry count."""
        import plugin_sign
        inspect_package(data)                                # the limits first: the signature check reads every file
        reg = getattr(self, "registry", None)
        devs, revoked = (reg.developers(), reg.revoked()) if reg else ([], [])
        return plugin_sign.trust_state(plugin_sign.check_package(data), plugin_sign.trusted_keys(devs, revoked), revoked)

    async def install(self, data: bytes, filename: str = "", origin: dict = None) -> dict:
        """Unpack a package into plugins/user/<id>/. The plugin stays switched off until the user allows its permissions.
        Raises ValueError with a message for the user when the package is not acceptable."""
        trust = self.trust_of(data)
        if trust["state"] == "bad":
            raise ValueError("подпись пакета не совпадает с его содержимым: после подписания пакет изменили, установка отменена")
        if trust["state"] == "revoked":
            raise ValueError("ключ, которым подписан пакет, отозван командой lostOS: установка отменена")
        z, names, prefix, d = inspect_package(data)
        stage = os.path.join(USER_ROOT, ".stage")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        try:
            for n in names:
                if n.startswith(prefix):
                    dst = os.path.join(stage, n[len(prefix):])
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(z.read(n))
            man, err = _valid_manifest(d, "user", stage)
            if not man:
                raise ValueError("манифест: " + err)
            pid = man["id"]
            if pid in self.items and self.items[pid].kind == "system":
                raise ValueError("id занят системным плагином")
            target = os.path.join(USER_ROOT, pid)
            if pid in self.items:
                await self.halt(self.items[pid])                # an update: the running copy stops first (its files are in use)
            keep = os.path.join(target, "data")                # the data of an updated plugin stay
            if os.path.isdir(keep):
                shutil.copytree(keep, os.path.join(stage, "data"), dirs_exist_ok=True)   # move() would nest it when the package has a data/ too
            if os.path.isdir(target):
                shutil.rmtree(target)                           # fails loudly rather than leaving two versions mixed
            shutil.move(stage, target)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        self.state.setdefault(pid, {})["enabled"] = False       # always off after an installation or update: the user decides
        clean = "".join(c for c in os.path.basename(filename.replace("\\", "/")) if c.isprintable())[:80]
        self.state[pid]["source"] = origin or {"type": "file", "name": clean, "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "sha256": hashlib.sha256(data).hexdigest()}
        self.state[pid]["source"].setdefault("at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.state[pid]["source"].setdefault("sha256", hashlib.sha256(data).hexdigest())
        self.state[pid]["signature"] = trust
        self._save()
        self.scan()
        return self.items[pid].m

    async def remove(self, pid: str) -> bool:
        p = self.items.get(pid)
        if not p or p.m["system"]:
            return False
        await self.halt(p)
        if p.m["device"]:
            await self.device_command("remove " + pid)
        shutil.rmtree(p.folder, ignore_errors=True)
        self.state.pop(pid, None)
        self._save()
        self.scan()
        return True

    # ---- life
    async def start(self, app=None):
        self.scan()
        for p in self.items.values():
            if self.enabled(p):
                await self.run(p)
        self.loop = asyncio.get_running_loop()
        self._task = self.loop.create_task(self._events())

    async def stop(self, app=None):
        if self._task:
            self._task.cancel()
        for p in self.items.values():
            await self.halt(p)

    async def run(self, p: Plugin):
        if self.blocked(p):
            p.error = self.requirement(p)
            return
        if p.m["builtin"] or not p.m["entry"] or (p.proc and p.proc.returncode is None):
            return                                          # built in, or a plugin that lives on the speaker only (device.runtime "js"): no process here
        env = dict(plugin_env(), LOSTOS_PLUGIN_ID=p.id, LOSTOS_PLUGIN_DIR=os.path.join(p.folder, "data"), PYTHONIOENCODING="utf-8", PYTHONPATH=HERE)
        os.makedirs(env["LOSTOS_PLUGIN_DIR"], exist_ok=True)
        try:
            p.proc = await fork_plugin(p.m["entry"], p.folder, env, self.srv.log) or await asyncio.create_subprocess_exec(
                sys.executable, "-u", p.m["entry"], cwd=p.folder, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT)
        except OSError as e:
            p.error = str(e)[:100]
            return
        p.error = ""
        p.stderr_tail = ""
        p.reader = asyncio.get_running_loop().create_task(self._serve(p))
        asyncio.get_running_loop().create_task(self._drain_stderr(p, p.proc))

    async def _drain_stderr(self, p: Plugin, proc):
        """Keeps the last lines a plugin wrote to stderr: when it dies, the last one (an ImportError, a traceback) says why."""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                if p.proc is proc:
                    p.stderr_tail = (p.stderr_tail + line.decode("utf-8", "replace"))[-2000:]
        except (asyncio.CancelledError, ValueError):
            return

    async def halt(self, p: Plugin):
        if p.reader:
            p.reader.cancel()
            p.reader = None
        if p.proc and p.proc.returncode is None:
            try:
                p.proc.terminate()
                await asyncio.wait_for(p.proc.wait(), 3)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    p.proc.kill()
                except ProcessLookupError:
                    pass
        p.proc, p.tile, p.subscribed = None, None, False
        p.tools.clear()                                     # its commands go away with it; it registers them again when it starts
        for fut in p.asking.values():
            if not fut.done():
                fut.cancel()
        p.asking.clear()

    async def recheck_trust(self):
        """The list of verified developers or the revoked keys changed (see registry.watch_developers): look again at the signature of every installed package.
        A key that has been revoked switches its plugins off at once; a developer who is not on the list any more only changes the mark."""
        reg = getattr(self, "registry", None)
        if reg is None:
            return
        import plugin_sign
        revoked = set(reg.revoked())
        keys = plugin_sign.trusted_keys(reg.developers(), revoked)
        changed = False
        for pid, st in list(self.state.items()):
            sig = st.get("signature") or {}
            fp = sig.get("fingerprint")
            p = self.items.get(pid)
            if not fp or p is None or p.kind != "user":
                continue
            if fp in revoked:
                new = {"state": "revoked", "publisher": sig.get("publisher", ""), "fingerprint": fp}
            elif fp in keys:
                k = keys[fp]
                new = {"state": "trusted", "publisher": k["publisher"] if k["team"] else sig.get("publisher", ""), "fingerprint": fp, "team": k["team"]}
            else:
                new = {"state": "known", "publisher": sig.get("publisher", ""), "fingerprint": fp}
            if new["state"] == sig.get("state"):
                continue
            st["signature"] = new
            changed = True
            self.srv.log(f"plugin {pid}: the signature is now '{new['state']}' (the list of verified developers changed)")
            if new["state"] == "revoked" and self.enabled(p):
                self.srv.log(f"plugin {pid}: the key of its author is revoked, the plugin is switched off")
                await self.set_enabled(pid, False)
        if changed:
            self._save()

    async def set_enabled(self, pid: str, on: bool, grant: bool = True) -> bool:
        p = self.items.get(pid)
        if not p or (p.m["system"] and not p.m["switchable"]):
            return False
        if on and self.blocked(p):
            p.error = self.requirement(p)
            return False
        st = self.state.setdefault(pid, {})
        st["enabled"] = bool(on)
        if p.m["system"] and p.m["builtin"] == "speaker":    # the receiver runs on the speaker: it is told, and it says back what it did
            if not await self.device_command(("enable " if on else "disable ") + pid):
                return False
            self.device[pid] = "running" if on else "off"   # the speaker confirms (or corrects) this in a moment
            self.srv.log(f"plugin {pid}: {'on' if on else 'off'} (the speaker was told)")
            return True
        if p.m["system"]:                                   # a built-in package: the server looks at the flag (service_on), nothing is started
            self._save()
            self.srv.log(f"plugin {pid}: {'on' if on else 'off'}")
            return True
        if on:
            st["granted"] = p.m["permissions"] if grant else st.get("granted", [])
            st["violations"] = 0
        self._save()
        if on:
            st["granted_device"] = (p.m["device"] or {}).get("permissions", [])
            await self.run(p)
            await self.device_install(p)
        else:
            await self.halt(p)
            if p.m["device"]:
                await self.device_command("disable " + p.id)
        return True

    # ---- the protocol
    async def _serve(self, p: Plugin):
        try:
            while True:
                line = await p.proc.stdout.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8", "replace"))
                    if "reply_to" in req:                   # the answer to a question the server asked (brain, interception)
                        fut = p.asking.pop(req["reply_to"], None)
                        if fut and not fut.done():
                            fut.set_result(req)
                        continue
                    rid, method, params = req.get("id"), str(req.get("method", "")), req.get("params") or {}
                except (ValueError, AttributeError):
                    continue
                try:
                    res = await self._call(p, method, params)
                    reply = {"id": rid, "result": res}
                except PermissionError as e:
                    reply = {"id": rid, "error": "permission: " + str(e)}
                    await self._violation(p, str(e))
                except Exception as e:
                    reply = {"id": rid, "error": str(e)[:200]}
                if rid is not None and p.proc and p.proc.stdin:
                    p.proc.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            p.error = repr(e)[:100]
        if p.proc and p.proc.returncode is None:              # stdout closed: the process is exiting, wait for its code
            try:
                await asyncio.wait_for(p.proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
        if p.proc and p.proc.returncode not in (None, 0) and not p.error:
            await asyncio.sleep(0.2)                            # the rest of stderr
            last = next((l.strip() for l in reversed(p.stderr_tail.splitlines()) if l.strip()), "")
            p.error = f"процесс завершился с кодом {p.proc.returncode}" + (": " + last[:160] if last else "")
            self.srv.log(f"plugin {p.id}: {p.error}")

    async def _violation(self, p: Plugin, what: str):
        st = self.state.setdefault(p.id, {})
        st["violations"] = st.get("violations", 0) + 1
        self._save()
        self.srv.log(f"plugin {p.id}: no permission for {what} ({st['violations']})")
        if st["violations"] >= VIOLATIONS_LIMIT and not p.m["system"]:
            await self.set_enabled(p.id, False)
            self.srv.notify("warn", f"Плагин «{p.m['name']}» отключён", "Он несколько раз просил то, чего ему не разрешено: " + what, cooldown=60)

    def _need(self, p: Plugin, perm: str):
        if perm not in self.granted(p):
            raise PermissionError(perm)

    async def _call(self, p: Plugin, method: str, params: dict):
        s = self.srv
        if method == "media.state":
            self._need(p, "media.read")
            import panel
            return panel.from_hub(s, {}) or {}
        if method == "media.command":
            self._need(p, "media.control")
            import media
            return await media.command(s, str(params.get("cmd", "")), params.get("args") or {})
        if method == "device.volume":
            self._need(p, "device.read")
            return s.PANEL.get("volume")
        if method == "weather.get":
            self._need(p, "weather.read")
            import weather
            return await weather.weather()
        if method == "events.subscribe":
            self._need(p, "events.subscribe")
            p.subscribed = True
            return True
        if method in ("storage.get", "storage.set"):
            self._need(p, "storage")
            path = os.path.join(p.folder, "data", "storage.json")
            try:
                data = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if method == "storage.get":
                return data.get(str(params.get("key", "")))
            data[str(params.get("key", ""))] = params.get("value")
            if len(json.dumps(data)) > 200_000:
                raise ValueError("слишком много данных")
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            return True
        if method == "tile.set":
            self._need(p, "ui.tile")
            icon = params.get("icon") if params.get("icon") in ICONS else p.m["icon"]
            p.tile = {"title": str(params.get("title", p.m["name"]))[:30], "value": str(params.get("value", ""))[:20],
                      "subtitle": str(params.get("subtitle", ""))[:60], "icon": icon, "accent": str(params.get("accent", ""))[:9]}
            return True
        if method == "tools.list":                          # every command of the assistant (the brain decides which to call)
            self._need(p, "brain.replace")
            return s.tools_neutral()
        if method == "tools.call":
            self._need(p, "brain.replace")
            name = str(params.get("name", ""))
            if name not in {t["name"] for t in s.tools_neutral()}:      # only the commands the assistant itself has; internal ones (_...) are closed
                raise ValueError("нет такой команды: " + name)
            s.log(f"plugin {p.id}: tool {name}({params.get('args', {})})")
            return await s.run_tool(name, params.get("args") or {})
        if method == "tools.register":
            self._need(p, "assistant.tools")
            return self._register_tool(p, params)
        if method == "tools.unregister":
            self._need(p, "assistant.tools")
            return p.tools.pop(str(params.get("name", "")), None) is not None
        if method in ("alice.tts", "alice.stt"):             # the voice and the ears of Alice for plugins (a brain of its own, a skill ...)
            self._need(p, "alice.api")
            if not self.service_on("alice-api"):
                raise RuntimeError("alice-api выключен владельцем")
            return await (s.alice_api_tts(str(params.get("text", ""))) if method == "alice.tts" else s.alice_api_stt(params))
        if method == "log":
            s.log(f"plugin {p.id}: {str(params.get('text', ''))[:200]}")
            return True
        raise ValueError("нет такого метода: " + method)

    # ---- commands that plugins add to the assistant (every brain sees them: the built-in model and the brain plugins)
    def _register_tool(self, p: Plugin, params: dict) -> bool:
        name = str(params.get("name", ""))
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name):
            raise ValueError("name: строчные латинские буквы, цифры и _, от 3 до 41 знака")
        if name in self.srv.builtin_tool_names() or any(name in q.tools for q in self.items.values() if q is not p):
            raise ValueError("такая команда уже есть: " + name)
        if name not in p.tools and len(p.tools) >= MAX_TOOLS:
            raise ValueError(f"не больше {MAX_TOOLS} команд на плагин")
        description = str(params.get("description", "")).strip()[:600]
        schema = params.get("parameters") or {"type": "object", "properties": {}}
        if not description or not isinstance(schema, dict) or len(json.dumps(schema)) > 4000:
            raise ValueError("description обязателен, parameters: JSON-схема до 4 КБ")
        p.tools[name] = {"name": name, "description": description, "parameters": schema, "restricted": bool(params.get("restricted"))}
        return True

    def plugin_tools(self) -> list:
        """-> [(plugin, declaration)] of the commands of the running plugins that may add commands: those of the server's plugins and
        those that the plugins on the speaker declared (they run there, see device_call)."""
        out = [(p, d) for p in self.with_permission("assistant.tools") for d in p.tools.values()]
        for pid, decls in self.device_tools.items():
            p = self.items.get(pid)
            if p and self.enabled(p) and "assistant.tools" in self.state.get(pid, {}).get("granted_device", []):
                out += [(p, d) for d in decls.values()]
        return out

    async def run_plugin_tool(self, name: str, args: dict) -> str:
        for p, d in self.plugin_tools():
            if d["name"] == name:
                if p.id in self.device_tools and name in self.device_tools[p.id]:
                    return await self.device_call(p.id, name, args)
                res = await self.ask(p, "tool.call", {"name": name, "args": args}, 30)
                return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        raise ValueError("команда недоступна: " + name)

    async def device_call(self, pid: str, name: str, args: dict, timeout: float = 30) -> str:
        """Runs a command of a plugin on the speaker: "plugin call <id> <call id> <base64 json>" goes out, the answer comes back in the
        speaker's plugins_state (device_report)."""
        import base64
        cid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.device_waiting[cid] = fut
        try:
            req = base64.urlsafe_b64encode(json.dumps({"name": name, "args": args}, ensure_ascii=False).encode()).decode().rstrip("=")
            if not await self.device_command(f"call {pid} {cid} {req}"):
                raise RuntimeError("колонка не на связи")
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.device_waiting.pop(cid, None)

    def device_report(self, text: str):
        """The speaker's plugins_state: "id<TAB>state<TAB>detail" per plugin, "id<TAB>tool<TAB><json>" per command of a running plugin,
        "id<TAB>result<TAB><call id><TAB><json string>" per recent answer to a call."""
        tools: dict = {}
        for ln in (text or "").splitlines():
            f = ln.split("\t")
            if len(f) >= 3 and f[1] == "tool":
                try:
                    d = json.loads(f[2])
                    tools.setdefault(f[0], {})[str(d["name"])] = {"name": str(d["name"])[:40], "description": str(d.get("description", ""))[:500],
                                                                  "parameters": d.get("parameters") or {"type": "object", "properties": {}}, "restricted": False}
                except (ValueError, KeyError, TypeError):
                    pass
            elif len(f) >= 4 and f[1] == "result":
                fut = self.device_waiting.get(f[2])
                if fut and not fut.done():
                    try:
                        fut.set_result(str(json.loads(f[3])))
                    except ValueError:
                        fut.set_result(f[3])
            elif len(f) >= 2:
                self.device[f[0]] = f[1] + (": " + f[2] if len(f) > 2 and f[2] else "")
        self.device_tools = tools

    def is_restricted_tool(self, name: str) -> bool:
        return any(d["name"] == name and d["restricted"] for _, d in self.plugin_tools())

    # ---- speech engines (speech.stt / speech.tts)
    def engine_plugins(self, perm: str) -> list:
        """Every enabled plugin allowed to be this engine; one that is not running is listed with why, so it does not just vanish from the choice.
        Built-in services are not engines of their own (alice-api is the cloud voice and ears behind "Облако Яндекса" / "Алиса")."""
        return [{"id": p.id, "name": p.m["name"], "running": bool(p.proc and p.proc.returncode is None), "error": p.error}
                for p in self.items.values() if not p.m["builtin"] and self.enabled(p) and perm in self.granted(p)]

    def engine_plugin(self, engine: str, perm: str):
        """"plugin:<id>" -> the running plugin that may serve as this engine, else None."""
        if not engine.startswith("plugin:"):
            return None
        return next((p for p in self.with_permission(perm) if p.id == engine[7:]), None)

    def ask_sync(self, p: Plugin, method: str, params: dict, timeout: float):
        """ask() for code that runs in a thread of the server (speech engines are called from executors)"""
        fut = asyncio.run_coroutine_threadsafe(self.ask(p, method, params, timeout), self.loop)
        return fut.result(timeout + 2)

    # ---- questions from the server to a plugin (it answers with {"reply_to": id, "result": ...})
    async def ask(self, p: Plugin, method: str, params: dict, timeout: float):
        """-> the plugin's result; raises asyncio.TimeoutError, RuntimeError (the plugin answered with an error or is gone)"""
        if not (p.proc and p.proc.returncode is None and p.proc.stdin):
            raise RuntimeError("плагин не запущен")
        qid = next(_question_ids)
        fut = asyncio.get_running_loop().create_future()
        p.asking[qid] = fut
        p.proc.stdin.write((json.dumps({"call": qid, "method": method, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await p.proc.stdin.drain()
            msg = await asyncio.wait_for(fut, timeout)
        finally:
            p.asking.pop(qid, None)
        if "error" in msg:
            raise RuntimeError(str(msg["error"])[:200])
        return msg.get("result")

    def with_permission(self, perm: str) -> list:
        return [p for p in self.items.values() if self.enabled(p) and perm in self.granted(p) and p.proc and p.proc.returncode is None]

    def brain(self):
        """The plugin that thinks instead of the built-in model (the first one that is allowed and running), or None."""
        found = self.with_permission("brain.replace")
        return found[0] if found else None

    async def intercept(self, text: str, who: str):
        """Offers a heard phrase to the plugins that may intercept it. -> the reply to speak when one of them takes it, else None.
        A plugin answers {"claim": true, "reply": "..."}; anything else (or a late answer) lets the phrase go on to the assistant."""
        for p in self.with_permission("assistant.intercept"):
            try:
                res = await self.ask(p, "assistant.heard", {"text": text, "who": who}, 2.5)
            except Exception as e:
                self.srv.log(f"plugin {p.id}: interception: {e!r}")
                continue
            if isinstance(res, dict) and res.get("claim"):
                self.srv.log(f"plugin {p.id}: took the phrase {text!r}")
                return str(res.get("reply") or "")[:600]
        return None

    async def _events(self):
        """The plugins that subscribed get the music state whenever it changes."""
        last = None
        while True:
            await asyncio.sleep(1.0)
            try:
                import panel
                now = panel.from_hub(self.srv, {}) or {}
                key = (now.get("source"), now.get("title"), now.get("artist"), now.get("playing"))
                if key != last:
                    last = key
                    msg = (json.dumps({"event": "media", "data": now}, ensure_ascii=False) + "\n").encode("utf-8")
                    for p in self.items.values():
                        if p.subscribed and p.proc and p.proc.returncode is None and "events.subscribe" in self.granted(p) and "media.read" in self.granted(p):
                            p.proc.stdin.write(msg)
            except Exception:
                pass

    def tiles(self) -> list:
        """The tiles that exist now: of the plugins that are on. Off or removed plugin, no tile."""
        out = []
        for p in self.items.values():
            if not self.enabled(p):
                continue
            t = p.m["tile"]
            if t:                                           # a built-in package brings its tile (the live line comes from the state of the panel)
                out.append({"id": p.id, "plugin_id": p.id, "plugin": p.m["name"], "shelf": t["shelf"], "title": t["title"], "icon": t["icon"],
                            "tint": t["tint"], "switch": t.get("switch", ""), "accent": "", "live": True, "label": t["title"], "about": p.m["description"],
                            "group": "control" if t["shelf"] == "sources" else "info"})
            elif p.tile and "ui.tile" in self.granted(p):   # a plugin that set its tile itself (tile.set): on the shelf "Сейчас"
                out.append({"id": p.id, "plugin_id": p.id, "plugin": p.m["name"], "shelf": "now", "tint": "", "label": p.tile.get("title", p.m["name"]),
                            "about": p.m["description"], "group": "info", **p.tile})
        return out

    def speaker_version(self) -> tuple:
        """The version of lostOS on the speaker as numbers ((0, 3, 0)), () while it is not known."""
        ver = getattr(self.srv, "SYSSTAT", {}).get("s", {}).get("ver", "")
        return tuple(int(x) for x in re.findall(r"\d+", ver.split("-")[0])[:3]) if ver else ()

    def blocked(self, p: Plugin) -> bool:
        """On a shared server (tenant) a plugin's Python runs as the same user as every household: until there is a sandbox only
        the team's own signed plugins may run there."""
        if p.kind != "user" or os.environ.get("LOSTOS_MODE") != "tenant":
            return False
        sig = self.state.get(p.id, {}).get("signature") or {}
        return not (sig.get("state") == "trusted" and sig.get("team"))

    def requirement(self, p: Plugin) -> str:
        """"" when the plugin may run on this speaker, else why not (the lostOS of the speaker is older than the plugin needs)"""
        if self.blocked(p):
            return "На общем сервере работают только плагины команды lostOS. Для остальных нужен свой сервер"
        need = p.m["min_firmware"]
        have = self.speaker_version()
        if not need or not have:
            return ""
        want = tuple(int(x) for x in need.split("."))
        return "" if have >= want + (0,) * (3 - len(want)) else f"Нужна прошивка lostOS {need} или новее, на колонке {'.'.join(map(str, have))}. Обновите её"

    def tile_gone_reason(self, pid: str) -> str:
        """Why the tiles of this plugin are not on the page (for the place holders of the edit mode)"""
        p = self.items.get(pid)
        if not p:
            return "плагин удалён"
        return "плагин отключён" if not self.enabled(p) else "плагин пока не показывает виджет"
