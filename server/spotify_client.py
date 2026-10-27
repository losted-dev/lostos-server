"""Spotify Web API (Premium) for the speaker: find the speaker's Connect device, search, play, pause, skip.
Tokens come from spotify_auth.py (spotify_token.json) and are refreshed here."""
import asyncio
import difflib
import json
import random
import os
import re
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "spotify_token.json")
API = "https://api.spotify.com/v1"


def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class SpotifyError(Exception):
    pass


# A lite server (a VDS) keeps the token in its encrypted vault: it sets (load, save) here; at home the token is the file next to the code.
STORE = None


def _read_token():
    if STORE:
        raw = STORE[0]()
        return json.loads(raw) if raw else None
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def _write_token(tok: dict):
    if STORE:
        STORE[1](json.dumps(tok))
        return
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f)


class Spotify:
    def __init__(self, device_name: str):
        self.device_name = device_name
        self.tok = None
        self._device_id = None

    @property
    def client_id(self) -> str:
        return _env("SPOTIFY_CLIENT_ID")

    @property
    def available(self) -> bool:
        return bool(self.client_id) and (self.tok is not None or _read_token() is not None)

    def forget(self):
        self.tok = None

    def _load(self):
        if self.tok is None:
            self.tok = _read_token()
            if self.tok is None:
                raise SpotifyError("вход в Spotify не выполнен")

    async def _token(self, s: aiohttp.ClientSession) -> str:
        self._load()
        if time.time() > self.tok["obtained_at"] + self.tok.get("expires_in", 3600) - 60:
            data = {"grant_type": "refresh_token", "refresh_token": self.tok["refresh_token"],
                    "client_id": self.client_id}
            async with s.post("https://accounts.spotify.com/api/token", data=data) as r:
                j = await r.json()
                if r.status != 200:
                    raise SpotifyError(f"token refresh failed: {j}")
            j.setdefault("refresh_token", self.tok["refresh_token"])
            j["obtained_at"] = int(time.time())
            self.tok = j
            _write_token(j)
        return self.tok["access_token"]

    async def _req(self, method: str, path: str, params=None, body=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            h = {"Authorization": "Bearer " + await self._token(s)}
            async with s.request(method, API + path, params=params, json=body, headers=h) as r:
                text = await r.text()
                if r.status in (200, 201, 202):
                    try:
                        return json.loads(text) if text else {}
                    except ValueError:
                        return {}
                if r.status == 204:
                    return {}
                try:
                    err = json.loads(text).get("error", {})
                except ValueError:
                    err = {}
                raise SpotifyError(f"{r.status} {err.get('reason') or err.get('message') or text[:120]}")

    # ---- devices ----
    async def devices(self) -> list:
        return (await self._req("GET", "/me/player/devices")).get("devices", [])

    async def speaker(self):
        """The speaker's Spotify Connect device (None until the Spotify app has used it once)."""
        want = self.device_name.casefold()
        devs = await self.devices()
        for d in devs:
            if d["name"].casefold() == want:
                self._device_id = d["id"]
                return d
        for d in devs:                                    # tolerate a renamed speaker
            if "станци" in d["name"].casefold():
                self._device_id = d["id"]
                return d
        return None

    async def state(self) -> dict:
        try:
            return await self._req("GET", "/me/player")
        except SpotifyError:
            return {}

    async def is_playing_here(self) -> bool:
        st = await self.state()
        dev = (st or {}).get("device") or {}
        return bool(st.get("is_playing")) and dev.get("name", "").casefold() == self.device_name.casefold()

    async def _target(self) -> str:
        d = await self.speaker()
        if not d:
            raise SpotifyError("колонки нет в списке устройств Spotify: один раз выберите её в приложении Spotify на телефоне")
        return d["id"]

    # ---- control ----
    async def control(self, action: str):
        dev = await self._target()
        if action == "pause":
            await self._req("PUT", "/me/player/pause", {"device_id": dev})
        elif action in ("play", "resume"):
            try:
                await self._req("PUT", "/me/player/play", {"device_id": dev})
                # After a long pause Spotify takes this call (204) and starts nothing: the session of the speaker is gone. Look, and wake it up the long way.
                await asyncio.sleep(2.0)
                if (await self.state() or {}).get("is_playing"):
                    return
            except SpotifyError:
                pass
            await self.resume_last(dev)
        elif action == "next":
            await self._req("POST", "/me/player/next", {"device_id": dev})
        elif action == "previous":
            # Spotify's "previous" restarts the track when it has played for more than ~3 s: then press it twice
            st = await self.state()
            again = ((st or {}).get("progress_ms") or 0) > 3000
            await self._req("POST", "/me/player/previous", {"device_id": dev})
            if again:
                await asyncio.sleep(0.4)
                await self._req("POST", "/me/player/previous", {"device_id": dev})
        else:
            raise SpotifyError("неизвестная команда " + action)

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\W_]+", " ", s).casefold().strip()

    @classmethod
    def _score(cls, item: dict, queries: list) -> float:
        names = [item["name"]] + ([item["name"] + " " + item["artists"][0]["name"]] if item.get("artists") else []) \
            + ([item["artists"][0]["name"] + " " + item["name"]] if item.get("artists") else [])
        return max(difflib.SequenceMatcher(None, cls._norm(n), cls._norm(q)).ratio() for n in names for q in queries)

    async def is_liked(self, uri: str):
        """Is the track a liked song (the heart): cached for a few seconds, None when Spotify does not say."""
        c = getattr(self, "_liked", None)
        if c is None:
            c = self._liked = {}
        hit = c.get(uri)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        try:
            val = bool((await self._req("GET", "/me/library/contains", {"uris": uri}))[0])
        except (SpotifyError, IndexError, TypeError, KeyError):
            return None
        c[uri] = (time.time(), val)
        return val

    async def like_uri(self, uri: str, add: bool = True):
        """Like / unlike a track by its uri (the speaker reports it, no need to ask the Web API what plays)."""
        await self._req("PUT" if add else "DELETE", "/me/library", {"uris": uri})
        getattr(self, "_liked", {}).pop(uri, None)

    async def like(self, add: bool = True) -> str:
        """Like / unlike the track that is playing now (the Spotify heart). Needs the user-library-modify scope."""
        st = await self.state()
        item = (st or {}).get("item") or {}
        if not item.get("id"):
            raise SpotifyError("сейчас ничего не играет")
        # /me/tracks answers 403 for apps made after the 2026 API changes: the library endpoint takes URIs
        await self._req("PUT" if add else "DELETE", "/me/library", {"uris": item["uri"]})
        getattr(self, "_liked", {}).pop(item["uri"], None)
        who = item["artists"][0]["name"] if item.get("artists") else ""
        return f"{item['name']}, {who}"

    async def resume_last(self, dev: str = None):
        """After a long pause Spotify forgets the session: put on what was playing last (same context, same track)
        instead of starting something random."""
        dev = dev or await self._target()
        items = (await self._req("GET", "/me/player/recently-played", {"limit": 1})).get("items", [])
        if not items:
            raise SpotifyError("нечего возобновлять: истории прослушивания нет")
        last = items[0]
        track = last["track"]["uri"]
        ctx = (last.get("context") or {}).get("uri")
        body = {"context_uri": ctx, "offset": {"uri": track}} if ctx and not ctx.endswith(":collection") else {"uris": [track]}
        try:
            await self._req("PUT", "/me/player", None, {"device_ids": [dev], "play": False})   # wake the device up
        except SpotifyError:
            pass
        await self._req("PUT", "/me/player/play", {"device_id": dev}, body)

    async def play(self, query: str, kind: str = "any", variants: list = None) -> str:
        """Find `query` (or any spelling in `variants`) and start the best match on the speaker.
        Returns what is playing, for the spoken answer."""
        dev = await self._target()
        if kind == "library":                                # "включи мою музыку": random liked songs
            total = (await self._req("GET", "/me/tracks", {"limit": 1})).get("total", 0)
            if not total:
                raise SpotifyError("в избранном пусто")
            items = []
            for off in random.sample(range(0, max(total, 1), 50), min(2, (total + 49) // 50)):
                items += (await self._req("GET", "/me/tracks", {"limit": 50, "offset": off})).get("items", [])
            uris = [i["track"]["uri"] for i in items if i.get("track")]
            random.shuffle(uris)
            await self._req("PUT", "/me/player/play", {"device_id": dev}, {"uris": uris[:80]})
            return "твою музыку из избранного"
        if kind in ("playlist", "any"):                      # the user's own playlists come first
            mine = []
            try:
                for off in (0, 50):
                    mine += (await self._req("GET", "/me/playlists", {"limit": 50, "offset": off})).get("items", [])
            except SpotifyError:
                pass
            qs = [query] + list(variants or [])
            best = max((p for p in mine if p), key=lambda p: self._score({"name": p["name"]}, qs), default=None)
            if best and self._score({"name": best["name"]}, qs) >= 0.75:
                await self._req("PUT", "/me/player/play", {"device_id": dev}, {"context_uri": best["uri"]})
                return "плейлист " + best["name"]
        types = {"track": "track", "artist": "artist", "album": "album", "playlist": "playlist"}.get(kind, "track,artist,album,playlist")
        queries = [q for q in dict.fromkeys([query] + [v for v in (variants or []) if v and v.strip()])][:8]
        cands = []
        for q in queries:
            res = await self._req("GET", "/search", {"q": q, "type": types, "limit": 5})
            for key in ("tracks", "artists", "albums", "playlists"):
                cands += [i for i in (res.get(key) or {}).get("items", []) if i]
        best = max(cands, key=lambda i: self._score(i, queries), default=None)
        if not best or self._score(best, queries) < 0.6:
            raise SpotifyError("точно такого не нашла, ближайшее: " + (best["name"] if best else "ничего") + " - переспроси название по буквам")
        pick = best
        if pick["type"] == "track" and (pick.get("album") or {}).get("uri"):
            # inside its album: it goes on with the album, then Spotify's own non-stop mode
            body = {"context_uri": pick["album"]["uri"], "offset": {"uri": pick["uri"]}}
        elif pick["type"] == "track":
            body = {"uris": [pick["uri"]]}
        else:
            body = {"context_uri": pick["uri"]}
        try:
            await self._req("PUT", "/me/player/play", {"device_id": dev}, body)
        except SpotifyError as e:
            if "NO_ACTIVE_DEVICE" not in str(e) and "404" not in str(e):
                raise
            await self._req("PUT", "/me/player", None, {"device_ids": [dev], "play": False})   # wake the device up
            await self._req("PUT", "/me/player/play", {"device_id": dev}, body)
        who = pick.get("artists") and pick["artists"][0]["name"]
        return f"{pick['name']}" + (f", {who}" if who and pick["type"] in ("track", "album") else "")

    async def _queue_safely(self, seed: str):
        try:
            await self.queue_similar(seed)
        except Exception:
            pass

    async def queue_similar(self, seed: str, n: int = 14):
        """"Non-stop": ask Gemini for songs like `seed` (Spotify's own recommendations are closed to new apps),
        find them on Spotify and put them in the queue behind the current track."""
        key = _env("GEMINI_API_KEY")
        if not key:
            return
        prompt = (f"Назови {n} песен, похожих по звучанию и жанру на «{seed}» (разные исполнители, без самого этого трека). "
                  "Только список, по одной на строку, формат: Исполнитель - Название. Без нумерации и пояснений.")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            lines = []
            for model in ("gemini-3.1-flash-lite", "gemini-flash-lite-latest"):
                try:
                    async with s.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                      params={"key": key}, json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]}) as r:
                        data = await r.json()
                    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                    lines = [l.strip(" -•*\t") for l in text.splitlines() if " - " in l][:n]
                    if lines:
                        break
                except Exception:
                    continue
        dev = await self._target()
        for line in lines:
            try:
                res = await self._req("GET", "/search", {"q": line.replace(" - ", " "), "type": "track", "limit": 1})
                items = res.get("tracks", {}).get("items", [])
                if items and self._score(items[0], [line, line.replace(" - ", " ")]) >= 0.5:
                    await self._req("POST", "/me/player/queue", {"uri": items[0]["uri"], "device_id": dev})
            except SpotifyError:
                continue

    @staticmethod
    def _choose(res: dict, query: str, kind: str):
        def items(key):
            return [i for i in (res.get(key) or {}).get("items", []) if i]
        tracks, artists, albums, lists = items("tracks"), items("artists"), items("albums"), items("playlists")
        if kind == "track" and tracks:
            return tracks[0]
        if kind == "artist" and artists:
            return artists[0]
        if kind == "album" and albums:
            return albums[0]
        if kind == "playlist" and lists:
            return lists[0]
        q = re.sub(r"\W+", " ", query).casefold().strip()
        for a in artists:                                   # "включи Metallica" -> the artist, not a track of that name
            if re.sub(r"\W+", " ", a["name"]).casefold().strip() == q:
                return a
        return (tracks or artists or albums or lists or [None])[0]
