"""The registry of lostOS plugins: a repository (GitHub, `lostos-plugins`) with a signed index of packages. The server reads it, shows what can be installed and updated, and installs a
package only when the index is signed by a team key, the file matches the SHA-256 of the index and the package carries a signature of a trusted developer.

Address: LOSTOS_REGISTRY (default https://raw.githubusercontent.com/losted-dev/lostos-plugins/main). The registry is READ only: nothing of the person leaves the server except the request for two files.
The last good index is kept (registry.json in the folder of the person), so that the list is there when the repository is not reachable.

The developers whose signatures count come from plugapi (LOSTOS_PLUGAPI, default https://plugapi.losted.dev; "off" switches it off): two files, developers.json and developers.json.sig.
The list is trusted for its signature (a key of the team with the scope "developers"), not for the address; it has a serial number (an older list than the one already seen is refused)
and an end date. The last good list is kept (plugapi.json). Where plugapi was never reached, or its list is out of date, the developers of the signed index are used as before.
Revoked keys are always the sum of both lists: a revocation can only be added."""
import asyncio
import datetime
import json
import os
import time

import aiohttp

import plugin_sign

DEFAULT_URL = "https://raw.githubusercontent.com/losted-dev/lostos-plugins/main"
PLUGAPI_URL = "https://plugapi.losted.dev"
MAX_DEVS = 256 * 1024
MAX_INDEX = 512 * 1024
MAX_PACKAGE = 6 * 1024 * 1024
TTL = 3600


def version_key(v: str) -> tuple:
    out = []
    for part in str(v).replace("-", ".").split("."):
        out.append((0, int(part)) if part.isdigit() else (-1, 0))
    return tuple(out)


class Registry:
    def __init__(self, folder: str, log=print, url: str = None):
        self.file = os.path.join(folder, "registry.json")
        self.url = (url or os.environ.get("LOSTOS_REGISTRY") or DEFAULT_URL).rstrip("/")
        self.log = log
        self.index: dict = {}
        self.error = ""
        self.checked = 0.0
        self.api = (os.environ.get("LOSTOS_PLUGAPI") or PLUGAPI_URL).rstrip("/")
        self.devs_file = os.path.join(folder, "plugapi.json")
        self.devs_raw_file, self.devs_sig_file = os.path.join(folder, "plugapi.raw"), os.path.join(folder, "plugapi.sig")
        self.devs_raw, self.devs_sig = b"", ""                # the list as plugapi signed it: a speaker checks the signature itself, it does not take this server's word
        self.devs: dict = {}                 # the last good list of verified developers from plugapi
        self.devs_error = ""
        self.devs_etag = ""                  # of the file we hold: the next look is a conditional request, an unchanged list costs a few hundred bytes
        try:
            self.index = json.load(open(self.file, encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            self.devs = json.load(open(self.devs_file, encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            self.devs_raw, self.devs_sig = open(self.devs_raw_file, "rb").read(), open(self.devs_sig_file, encoding="ascii").read()
        except OSError:
            pass

    @staticmethod
    def _fresh(doc: dict) -> bool:
        try:
            return bool(doc) and datetime.datetime.fromisoformat(str(doc["expires"]).replace("Z", "+00:00")) > datetime.datetime.now(datetime.timezone.utc)
        except (KeyError, ValueError):
            return False

    def developers(self) -> list:
        """Verified developers: the list of plugapi while it is good and in date, else the developers of the signed index."""
        if self._fresh(self.devs):
            return self.devs.get("developers", [])
        return self.index.get("developers", [])

    def revoked(self) -> list:
        return sorted(set(self.index.get("revoked", [])) | set(self.devs.get("revoked", []) if self.devs else []))

    def developers_source(self) -> str:
        return "plugapi" if self._fresh(self.devs) else "index"

    async def refresh_developers(self) -> bool:
        """Reads the list of verified developers from plugapi. False (and the old list stays) when it is not reachable, not signed, out of date or older than the one already seen."""
        if self.api.lower() in ("", "off"):
            return False
        try:
            etag = ""
            async with aiohttp.ClientSession() as s:
                async def get(path, limit):
                    nonlocal etag
                    headers = {"If-None-Match": self.devs_etag} if path == "developers.json" and self.devs_etag and self.devs else {}
                    async with s.get(f"{self.api}/{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 304:
                            return None
                        if r.status != 200:
                            raise RuntimeError(f"plugapi {path}: HTTP {r.status}")
                        if path == "developers.json":
                            etag = r.headers.get("ETag", "")
                        data = await r.content.read(limit + 1)
                        if len(data) > limit:
                            raise RuntimeError("plugapi: файл слишком большой")
                        return data
                raw = await get("developers.json", MAX_DEVS)
                if raw is None:                                          # unchanged since the last good look
                    self.devs_error = ""
                    return True
                sig = (await get("developers.json.sig", 4096)).decode("ascii", "ignore")
            if not plugin_sign.verify_developers(raw, sig):
                raise RuntimeError("подпись списка разработчиков не сходится: список не принят")
            doc = json.loads(raw.decode("utf-8"))
            if doc.get("v") != 1 or not isinstance(doc.get("developers"), list) or not isinstance(doc.get("revoked", []), list):
                raise RuntimeError("список разработчиков в неизвестном формате")
            if not self._fresh(doc):
                raise RuntimeError("список разработчиков просрочен")
            if int(doc.get("serial", 0)) < int(self.devs.get("serial", 0) if self.devs else 0):
                raise RuntimeError("список разработчиков старее уже виденного: не принят")
            self.devs, self.devs_error, self.devs_etag = doc, "", etag
            self.devs_raw, self.devs_sig = raw, sig.strip()
            with open(self.devs_file, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            with open(self.devs_raw_file, "wb") as f:
                f.write(raw)
            with open(self.devs_sig_file, "w", encoding="ascii") as f:
                f.write(self.devs_sig)
            return True
        except (aiohttp.ClientError, RuntimeError, ValueError, TypeError, OSError, TimeoutError) as e:
            self.devs_error = str(e)[:160] or e.__class__.__name__
            self.log(f"registry: {self.devs_error}")
            return False

    async def _get(self, s: aiohttp.ClientSession, path: str, limit: int) -> bytes:
        async with s.get(f"{self.url}/{path}", timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError(f"{path}: HTTP {r.status}")
            data = await r.content.read(limit + 1)
            if len(data) > limit:
                raise RuntimeError(f"{path}: больше {limit // 1024} КБ")
            return data

    async def refresh(self, force: bool = False) -> bool:
        """Reads the index again (at most once an hour unless forced). True when a good, signed index is there."""
        if not force and self.index and time.time() - self.checked < TTL:
            return True
        self.checked = time.time()
        try:
            async with aiohttp.ClientSession() as s:
                raw = await self._get(s, "index.json", MAX_INDEX)
                sig = (await self._get(s, "index.json.sig", 4096)).decode("ascii", "ignore")
            if not plugin_sign.verify_index(raw, sig, plugin_sign.team_public_keys()):
                raise RuntimeError("подпись списка плагинов не сходится: список не принят")
            idx = json.loads(raw.decode("utf-8"))
            if idx.get("v") != 1 or not isinstance(idx.get("plugins"), list):
                raise RuntimeError("список плагинов в неизвестном формате")
            self.index, self.error = idx, ""
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
            await self.refresh_developers()
            return True
        except (aiohttp.ClientError, RuntimeError, ValueError, OSError, TimeoutError) as e:
            self.error = str(e)[:160] or e.__class__.__name__
            self.log(f"registry: {self.error}")
            return bool(self.index)

    async def watch_developers(self, on_change=None, interval: float = 15.0):
        """Looks at plugapi every 15 seconds for as long as the server runs (a conditional request: an unchanged list costs a few hundred bytes). on_change (a function or a coroutine function) is called when the list or the revoked keys changed:
        a revoked key must stop working within seconds, not within an hour."""
        while True:
            before = (self.devs.get("serial"), tuple(self.revoked()), self.developers_source())
            try:
                await self.refresh_developers()
                after = (self.devs.get("serial"), tuple(self.revoked()), self.developers_source())
                if after != before and on_change is not None:
                    res = on_change()
                    if asyncio.iscoroutine(res):
                        await res
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # the watch never stops for a bad answer or a bug in the callback
                self.log(f"registry: plugapi watch: {e!r}"[:200])
            await asyncio.sleep(interval)

    def view(self, installed: dict) -> dict:
        """installed: plugin id -> installed version. -> what the panel shows"""
        out = []
        for p in self.index.get("plugins", []):
            have = installed.get(p["id"])
            out.append({**{k: p.get(k, "") for k in ("id", "name", "version", "about", "publisher", "size", "min_firmware")},
                        "installed": have, "update": bool(have and version_key(p["version"]) > version_key(have))})
        return {"url": self.url, "updated": self.index.get("updated", ""), "error": self.error, "plugins": out,
                "developers": self.developers_source(), "developers_error": self.devs_error}

    async def package(self, pid: str) -> tuple:
        """-> (the bytes of the package, the entry of the index) after the checks of the index (the size and the SHA-256)"""
        import hashlib
        e = next((p for p in self.index.get("plugins", []) if p["id"] == pid), None)
        if not e:
            raise ValueError("такого плагина нет в репозитории")
        path = str(e["file"])
        if path.startswith(("/", "\\")) or ".." in path.split("/") or ":" in path:
            raise ValueError("странный адрес файла в списке")
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, path, MAX_PACKAGE)
        if hashlib.sha256(data).hexdigest() != e["sha256"]:
            raise ValueError("файл не совпадает с контрольной суммой списка: установка отменена")
        return data, e
