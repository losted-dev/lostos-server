"""The vault: the keys of the owner (Gemini, Home Assistant, Spotify ...) and the speaker's API key, encrypted on the disk of the server.

Who can read them: nobody who has only the files. The file `secrets.enc` is encrypted with AES-256-GCM under a key that the SERVER DOES NOT KEEP.
The key lives on the speaker (/data/lostos/vault.key, made on the speaker, never leaves it except over the authenticated, owner-confirmed link)
Every time the station connects, it hands the key to the server, which holds it in memory only, for as long as the station is connected.
So a copy of the disk, a backup, a snapshot of the VDS or a stolen database gives nothing: the keys can be opened only while the speaker itself is
connected and has given its key. When the speaker is away, the vault is locked and the server cannot use the keys (and has no phrases to answer anyway)

What it does not protect from, honestly: somebody with root on the running server who reads the memory of the process while the speaker is connected.
The panel never shows a value: it only says that a key is saved. A key is replaced or deleted, never read back.

File: DATA/secrets.enc = JSON {"v": 2, "nonce", "data": AES-GCM(K, JSON object name -> value), "wraps": {station id: {"nonce", "data": AES-GCM(key of that station, K)}}}.
K is a random key of the household's vault; every station of the household holds its own key and has its own wrap of K, so any of them opens the vault and nobody else does.
A station that joins later gets its wrap from a station that is open at that moment (wrap_for_peers). Version 1 (encrypted directly with the key of the only station) is converted at the first opening.
Written atomically, mode 0600"""
import base64
import json
import os
import secrets as rnd
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("LOSTOS_DATA", HERE)
FILE = os.path.join(DATA, "secrets.enc")
ENV_NAMES = {                                     # what is put into the environment of the process (memory only) when the vault opens
    "GEMINI_API_KEY": "Gemini", "ANTHROPIC_API_KEY": "Claude", "DEEPSEEK_API_KEY": "DeepSeek", "OPENAI_API_KEY": "ChatGPT",
    "TAVILY_API_KEY": "Tavily, поиск", "HA_TOKEN": "Home Assistant, токен", "SPEAKER_ROOM": "Комната колонки",
    "CUSTOM_API_KEY": "Свой сервис, ключ", "SPOTIFY_CLIENT_ID": "Spotify, Client ID", "SPOTIFY_CLIENT_SECRET": "Spotify, Client Secret",
}


class Vault:
    def __init__(self, log=print):
        self.log = log
        self._key = None                                  # K: the key of the data (None = locked)
        self._data: dict = {}
        self._lock = threading.Lock()
        self._station_key = None                          # the key of the station that opened it
        self._sid = ""
        self._mtime = 0.0
        self._wraps: dict = {}

    @property
    def locked(self) -> bool:
        return self._key is None

    def has_file(self) -> bool:
        return os.path.exists(FILE)

    def _wrap(self, station_key: bytes, k: bytes) -> dict:
        nonce = rnd.token_bytes(12)
        return {"nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(AESGCM(station_key).encrypt(nonce, k, b"lostos-vault-wrap")).decode()}

    def unlock(self, key: bytes, sid: str = "") -> bool:
        """The key from the speaker `sid`. True when the vault is open (a new one is created empty when there is no file yet).
        False when this speaker has no wrap of the household's key yet: an open station of the house adds it within seconds (wrap_for_peers)."""
        if len(key) != 32:
            return False
        with self._lock:
            if self._key is not None and self._station_key == key:
                return True
            data, k, wraps = {}, None, {}
            if os.path.exists(FILE):
                try:
                    blob = json.load(open(FILE, encoding="utf-8"))
                    if blob.get("v") == 2:
                        w = blob.get("wraps", {}).get(sid)
                        if not w:
                            self.log(f"vault: the station {sid[:8]} has no key to the vault yet (another station of the house adds it when it is on line)")
                            return False
                        k = AESGCM(key).decrypt(base64.b64decode(w["nonce"]), base64.b64decode(w["data"]), b"lostos-vault-wrap")
                        wraps = blob.get("wraps", {})
                        data = json.loads(AESGCM(k).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["data"]), b"lostos-vault-v2"))
                    else:                                                   # version 1: the key of the speaker encrypted the data itself
                        data = json.loads(AESGCM(key).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["data"]), b"lostos-vault-v1"))
                except (InvalidTag, ValueError, KeyError, OSError):
                    self.log("vault: this key does not open the vault (a different speaker or a reset one); the vault stays locked")
                    return False
            self._key, self._data = k or rnd.token_bytes(32), data
            self._station_key, self._sid = key, sid
            wraps[sid] = self._wrap(key, self._key)
            self._wraps = wraps
            self._save()                                                    # creates the file, converts version 1, or adds the wrap of this station
        self.apply_env()
        self.log(f"vault: opened by the speaker ({len(self._data)} secrets)")
        return True

    def wrap_for_peers(self, peers: dict):
        """Open vault + the keys {station id: key} of the OTHER stations of the house that are on line now: give each of them a wrap of K so they open it too."""
        if self._key is None or not peers:
            return
        with self._lock:
            self._reload_locked()
            new = False
            for sid, kb in peers.items():
                if sid not in self._wraps and len(kb) == 32:
                    self._wraps[sid] = self._wrap(kb, self._key)
                    new = True
            if new:
                self._save()
                self.log("vault: a key for another station of the house is added")

    def _reload_locked(self):
        """Another process of the house may have changed the file: read it again when it is newer than what this process wrote or read."""
        try:
            m = os.path.getmtime(FILE)
        except OSError:
            return
        if m == self._mtime or self._key is None:
            return
        try:
            blob = json.load(open(FILE, encoding="utf-8"))
            self._data = json.loads(AESGCM(self._key).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["data"]), b"lostos-vault-v2"))
            self._wraps = blob.get("wraps", self._wraps)
            self._mtime = m
        except (InvalidTag, ValueError, KeyError, OSError):
            return
        for name in ENV_NAMES:
            if name in self._data:
                os.environ[name] = self._data[name]
            else:
                os.environ.pop(name, None)

    def refresh(self):
        """Called every few seconds: the keys saved by another station's process of the house become visible here."""
        with self._lock:
            self._reload_locked()

    def lock(self):
        with self._lock:
            self._key, self._data, self._station_key = None, {}, None
        for name in ENV_NAMES:
            os.environ.pop(name, None)

    def _save(self):
        nonce = rnd.token_bytes(12)
        blob = {"v": 2, "nonce": base64.b64encode(nonce).decode(), "wraps": self._wraps,
                "data": base64.b64encode(AESGCM(self._key).encrypt(nonce, json.dumps(self._data, ensure_ascii=False).encode(), b"lostos-vault-v2")).decode()}
        os.makedirs(DATA, exist_ok=True)
        tmp = FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, FILE)
        try:
            self._mtime = os.path.getmtime(FILE)
        except OSError:
            pass

    def set(self, name: str, value: str) -> bool:
        with self._lock:
            if self._key is None:
                return False
            self._reload_locked()
            self._data[name] = value
            self._save()
        if name in ENV_NAMES:
            os.environ[name] = value
        return True

    def delete(self, name: str) -> bool:
        with self._lock:
            self._reload_locked()
            if self._key is None or name not in self._data:
                return False
            self._data.pop(name)
            self._save()
        os.environ.pop(name, None)
        return True

    def get(self, name: str, default: str = "") -> str:
        return self._data.get(name, default)

    def apply_env(self):
        for name in ENV_NAMES:
            if name in self._data:
                os.environ[name] = self._data[name]

    def listing(self) -> dict:
        """What the panel may know: which keys are saved, never their values."""
        return {"locked": self.locked, "exists": self.has_file(),
                "keys": [{"name": n, "title": t, "saved": (not self.locked) and n in self._data} for n, t in ENV_NAMES.items()]}


def install_api(app, vault: Vault):
    from aiohttp import web

    async def listing(req):
        return web.json_response(vault.listing())

    async def put(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "нужен JSON"}, status=400)
        name, value = str(b.get("name", "")), str(b.get("value", "")).strip()
        if name not in ENV_NAMES or not 1 <= len(value) <= 4096:
            return web.json_response({"ok": False, "error": "неизвестное имя или пустое значение"}, status=400)
        if vault.locked:
            return web.json_response({"ok": False, "error": "хранилище закрыто: колонка не на связи, ключи открывает только она"}, status=409)
        vault.set(name, value)
        return web.json_response({"ok": True})

    async def remove(req):
        ok = vault.delete(req.match_info["name"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    app.router.add_get("/api/v1/secrets", listing)
    app.router.add_post("/api/v1/secrets", put)
    app.router.add_delete("/api/v1/secrets/{name}", remove)
