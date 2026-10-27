#!/usr/bin/env python3
"""The checks of the plugin registry (the repository lostos-plugins), run by GitHub Actions on every pull request and by hand.

  registry_ci.py validate <registry-folder> [--base <folder of the registry as it is on main>] [--report report.md]
        checks the packages (all of them, or only the new and changed ones when --base is given), developers.json and revoked.json;
        writes a report in Markdown (for the comment under the pull request); the exit code is 1 when something is wrong.
  registry_ci.py release <registry-folder> <key.pem>
        after a merge: builds index.json from plugins/*.lstplugin and signs it (the key comes from the secrets of the repository, never from a pull request)

It uses the same code as the server that installs the packages (server/plugins.py: inspect_package, validate_package), so what passes here installs there.
Where to find it: LOSTOS_SERVER (the folder server/ of a checkout of lostos-server), else ../server (this project), else ../_server/server."""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8")                # the report has Cyrillic and marks; a Windows console would refuse them
except (AttributeError, OSError):
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.environ.get("LOSTOS_SERVER", ""), os.path.join(os.environ.get("LOSTOS_SERVER", "/nonexistent"), "server"), os.path.join(HERE, "..", "server"),
             os.path.join(HERE, "..", "_server", "server")):          # a checkout of lostos-server keeps its code in server/
    if cand and os.path.isfile(os.path.join(cand, "plugin_sign.py")) and os.path.isfile(os.path.join(cand, "plugins.py")):
        sys.path.insert(0, os.path.abspath(cand))
        break
else:
    sys.exit("registry_ci: plugin_sign.py not found; set LOSTOS_SERVER to a checkout of lostos-server")
import plugin_sign as ps
import plugins as server_plugins

NAME_RE = re.compile(r"^([a-z0-9]+(?:[.\-][a-z0-9]+)+)-(\d+\.\d+\.\d+)\.lstplugin$")
# what must never be in a package (text files are searched)
SECRET_RE = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bAKIA[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_\-]{30,}",
    r"\bxox[abprs]-[0-9A-Za-z\-]{10,}", r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",
)]
MAX_FILES = 600


def vtuple(v: str):
    return tuple(int(x) for x in v.split("."))


def perms_of(man: dict) -> dict:
    return {"server": set(man.get("permissions", [])), "speaker": set((man.get("device") or {}).get("permissions", []))}


class Report:
    def __init__(self):
        self.errors, self.notes, self.info = [], [], []

    def error(self, where, text):
        self.errors.append(f"- **{where}**: {text}")

    def note(self, where, text):
        self.notes.append(f"- **{where}**: {text}")

    def text(self, title="Проверка реестра плагинов") -> str:
        out = [f"## {title}", ""]
        out.append("✅ Ошибок нет." if not self.errors else f"❌ Ошибок: {len(self.errors)}")
        out.append("")
        if self.errors:
            out += ["### Что нужно исправить", *self.errors, ""]
        if self.notes:
            out += ["### Что должен посмотреть человек", *self.notes, ""]
        if self.info:
            out += ["### Пакеты", *self.info, ""]
        out.append("_Подпись реестра ставится только после слияния и только на `index.json`; этот отчёт её не заменяет._")
        return "\n".join(out)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def check_developers(folder, base, rep):
    devs = load_json(os.path.join(folder, "developers.json"), [])
    if not isinstance(devs, list):
        rep.error("developers.json", "должен быть списком")
        return {}
    keys, ids = {}, set()
    for d in devs:
        try:
            raw = base64.b64decode(d["key"], validate=True)
            if len(raw) != 32:
                raise ValueError("ключ не 32 байта")
            did = str(d["id"])
            if not re.fullmatch(r"[a-z0-9][a-z0-9._\-]{1,39}", did) or did in ids:
                raise ValueError("id: латиница, цифры, точка, дефис; уникальный")
            if not str(d.get("name", "")).strip():
                raise ValueError("name обязателен")
            ids.add(did)
            keys[ps.fingerprint(d["key"])] = d
        except (KeyError, ValueError, TypeError) as e:
            rep.error("developers.json", f"{d!r:.80}: {e}")
    if base is not None:
        before = {ps.fingerprint(d["key"]) for d in load_json(os.path.join(base, "developers.json"), []) if isinstance(d, dict) and "key" in d}
        for fp, d in keys.items():
            if fp not in before:
                rep.note("новый разработчик", f"`{d['id']}` ({d.get('name','')}), отпечаток ключа `{fp}`: проверьте, что человек отвечает за этот ключ")
        for fp in before - set(keys):
            rep.note("разработчик убран", f"отпечаток `{fp}`: его пакеты перестанут считаться известными")
    return keys


def plugapi_verified(rep):
    """The fingerprints of the developers that plugapi vouches for (LOSTOS_PLUGAPI, e.g. https://plugapi.losted.dev), or None when it is not asked or not reachable."""
    import json as _json
    import urllib.request
    url = (os.environ.get("LOSTOS_PLUGAPI") or "").rstrip("/")
    if not url or url.lower() == "off":
        return None
    try:
        def get(path):
            with urllib.request.urlopen(f"{url}/{path}", timeout=15) as r:
                return r.read(262144)
        raw, sig = get("developers.json"), get("developers.json.sig").decode("ascii", "ignore")
        if not ps.verify_developers(raw, sig):
            raise ValueError("подпись списка не сходится")
        doc = _json.loads(raw.decode("utf-8"))
        return {ps.fingerprint(d["key"]) for d in doc.get("developers", [])} | {"revoked:" + f for f in doc.get("revoked", [])}
    except Exception as e:
        rep.note("plugapi", f"список подтверждённых разработчиков не получен ({str(e)[:80]}): проверка идёт только по developers.json")
        return None


def check_revoked(folder, rep):
    rv = load_json(os.path.join(folder, "revoked.json"), [])
    if not isinstance(rv, list) or any(not isinstance(x, str) for x in rv):
        rep.error("revoked.json", "должен быть списком отпечатков")
        return []
    return rv


def package_files(folder):
    d = os.path.join(folder, "plugins")
    return sorted(f for f in os.listdir(d) if f.endswith(".lstplugin")) if os.path.isdir(d) else []


def describe_perms(man, before=None):
    lines = []
    now = perms_of(man)
    old = perms_of(before) if before else {"server": set(), "speaker": set()}
    for side, label in (("server", "на сервере"), ("speaker", "на колонке")):
        for p in sorted(now[side]):
            info = server_plugins.PERMISSION_INFO.get(p) if side == "server" else None
            level = info[0] if info else ("dangerous" if p in server_plugins.RISKY else "normal")
            names = server_plugins.PERMISSIONS if side == "server" else server_plugins.DEVICE_PERMISSIONS
            new = "" if p in old[side] else " **(новое)**"
            lines.append(f"  - {label}: `{p}` — {names.get(p, '')} [{level}]{new}")
    return lines


def validate_package(fname, data, devs, revoked, rep, verified=None):
    trusted = ps.trusted_keys(list(devs.values()), revoked)      # the keys of the team (trusted_keys.json of the server) and the developers of this registry, minus the revoked ones
    m = NAME_RE.match(fname)
    if not m:
        rep.error(fname, "имя файла: `<id>-<версия>.lstplugin`, например `ru.example.demo-1.0.0.lstplugin`")
        return None
    pid, version = m.groups()
    man, err = server_plugins.validate_package(data)
    if not man:
        rep.error(fname, err)
        return None
    if man["id"] != pid or man["version"] != version:
        rep.error(fname, f"имя файла говорит {pid} {version}, а манифест — {man['id']} {man['version']}")
    z = zipfile.ZipFile(io.BytesIO(data))
    if len(z.infolist()) > MAX_FILES:
        rep.error(fname, f"больше {MAX_FILES} файлов")
    for info in z.infolist():
        if info.is_dir() or info.file_size > 2_000_000:
            continue
        raw = z.read(info.filename)
        if b"\0" in raw[:2048]:
            continue
        text = raw.decode("utf-8", "ignore")
        for rx in SECRET_RE:
            if rx.search(text):
                rep.error(fname, f"в `{info.filename}` похоже на ключ или токен (`{rx.pattern[:28]}…`): уберите и смените ключ, он уже мог попасть в историю")
                break
    chk = ps.check_package(data)
    if chk["state"] != "valid":
        rep.error(fname, "нет подписи разработчика" if chk["state"] == "none" else "подпись не сходится с содержимым: после подписания пакет изменили")
    else:
        fp = chk["fingerprint"]
        if fp in revoked:
            rep.error(fname, f"ключ `{fp}` отозван")
        elif fp not in trusted:
            rep.error(fname, f"ключ `{fp}` не записан в developers.json: добавьте свой публичный ключ в этом же PR")
        elif fp in devs and devs[fp].get("id") != chk["publisher"] and devs[fp].get("name") != chk["publisher"]:
            rep.note(fname, f"подписан именем «{chk['publisher']}», а в developers.json этот ключ записан как «{devs[fp].get('id')}»")
        elif fp not in devs:
            rep.note(fname, f"подписан ключом команды lostOS (`{chk['publisher']}`)")
        if verified is not None and fp in trusted and not trusted[fp]["team"]:
            if "revoked:" + fp in verified:
                rep.error(fname, f"ключ `{fp}` отозван на plugapi")
            elif fp not in verified:
                rep.note(fname, f"ключ `{fp}` ещё не подтверждён на plugapi: подтвердите разработчика до слияния (`python tools/plugapi.py add …`), иначе станции этому пакету не поверят")
    return man


def validate(folder, base, report_path):
    rep = Report()
    devs = check_developers(folder, base, rep)
    revoked = check_revoked(folder, rep)
    verified = plugapi_verified(rep)
    names = package_files(folder)
    base_names = package_files(base) if base else []
    base_man = {}
    for bn in base_names:
        mm = NAME_RE.match(bn)
        if mm:
            try:
                bm, _ = server_plugins.validate_package(open(os.path.join(base, "plugins", bn), "rb").read())
            except OSError:
                bm = None
            base_man[mm.groups()] = bm
    if base:
        for fn in ("index.json", "index.json.sig"):
            a, b = os.path.join(folder, fn), os.path.join(base, fn)
            if (os.path.exists(a) and open(a, "rb").read()) != (os.path.exists(b) and open(b, "rb").read()):
                rep.error(fn, "этот файл собирает и подписывает команда после слияния; в PR его менять нельзя")
        for bn in base_names:
            if bn not in names:
                rep.note(bn, "пакет удалён из реестра: станции с ним сохранят свою копию, новые не смогут его поставить")
    for fname in names:
        data = open(os.path.join(folder, "plugins", fname), "rb").read()
        same = base and os.path.exists(os.path.join(base, "plugins", fname)) and open(os.path.join(base, "plugins", fname), "rb").read() == data
        if same:
            continue                                                       # untouched by this pull request
        if base and fname in base_names:
            rep.error(fname, "такая версия уже опубликована; опубликованные версии не меняются, выпустите следующую")
            continue
        man = validate_package(fname, data, devs, revoked, rep, verified)
        if not man:
            continue
        previous = sorted((k for k in base_man if k[0] == man["id"]), key=lambda k: vtuple(k[1]))
        if previous and vtuple(man["version"]) <= vtuple(previous[-1][1]):
            rep.error(fname, f"версия должна быть больше опубликованной ({previous[-1][1]})")
        before = base_man.get(previous[-1]) if previous else None
        title = f"`{man['id']}` {man['version']} — {man['name']}, автор {man['author']}" + (" (новый плагин)" if not previous else f" (обновление с {previous[-1][1]})")
        rep.info.append("- " + title)
        added = []
        now, old = perms_of(man), perms_of(before) if before else {"server": set(), "speaker": set()}
        for side in ("server", "speaker"):
            added += sorted(now[side] - old[side])
        rep.info += describe_perms(man, before) or ["  - прав не запрашивает"]
        risky = [p for p in added if p in server_plugins.RISKY]
        if not previous and (now["server"] or now["speaker"]):
            rep.note(man["id"], "новый плагин просит права, посмотрите, зачем: " + ", ".join(f"`{p}`" for p in sorted(now["server"] | now["speaker"])))
        elif risky:
            rep.note(man["id"], "обновление добавило чувствительные права: " + ", ".join(f"`{p}`" for p in risky))
        elif added:
            rep.note(man["id"], "обновление добавило права: " + ", ".join(f"`{p}`" for p in added))
        signer = ps.check_package(data)
        names_ok = {signer.get("publisher", "")} | {n for d in devs.values() for n in (d.get("name"), d.get("id"))}
        if man.get("author") and man["author"] not in names_ok:
            rep.note(man["id"], f"автор в манифесте «{man['author']}» не совпадает с именем в подписи («{signer.get('publisher', '')}») и с developers.json")
    text = rep.text()
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 1 if rep.errors else 0


def release(folder, key_path):
    """Builds index.json and signs it: the same as tools/lostos-sign.py index (which is kept in the repository next to this file)."""
    tool = os.path.join(HERE, "lostos-sign.py")
    if not os.path.isfile(tool):
        sys.exit("lostos-sign.py is missing next to registry_ci.py")
    return subprocess.call([sys.executable, tool, "index", folder, key_path])


def main(argv):
    if len(argv) >= 2 and argv[0] == "validate":
        base = report = None
        rest = argv[2:]
        while rest:
            if rest[0] == "--base" and len(rest) > 1:
                base, rest = rest[1], rest[2:]
            elif rest[0] == "--report" and len(rest) > 1:
                report, rest = rest[1], rest[2:]
            else:
                sys.exit(__doc__)
        return validate(argv[1], base, report)
    if len(argv) == 3 and argv[0] == "release":
        return release(argv[1], argv[2])
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
