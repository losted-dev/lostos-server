"""Synced lyrics for the player: looked up on LRCLIB (free, no key) by artist, title and length, parsed from the LRC format.
Only lyrics with time stamps are used: a text that does not run with the music is not shown at all.
Found lyrics are cached in lyrics_cache.json so a track is asked for once."""
import json
import os
import re
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "lyrics_cache.json")
API = "https://lrclib.net/api"
MAX_CACHE = 400
_cache: dict = {}
_loaded = False


def _load():
    global _loaded, _cache
    if _loaded:
        return
    _loaded = True
    try:
        _cache = json.load(open(CACHE_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        _cache = {}


def _save():
    if len(_cache) > MAX_CACHE:                                   # forget the oldest entries
        for k in sorted(_cache, key=lambda k: _cache[k].get("t", 0))[:len(_cache) - MAX_CACHE]:
            del _cache[k]
    try:
        json.dump(_cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass


def parse_lrc(text: str) -> list:
    """"[01:23.45] words" -> [{"t": milliseconds, "text": "words"}], sorted; empty lines stay (a pause in the singing)."""
    lines = []
    for raw in (text or "").splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        if not stamps:
            continue
        words = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for m, s in stamps:
            lines.append({"t": int((int(m) * 60 + float(s)) * 1000), "text": words})
    lines.sort(key=lambda x: x["t"])
    return lines


def _clean_title(title: str) -> str:
    """"Song (slowed) (Song (slowed))" -> "Song"; "Song - Remastered 2011" -> "Song"."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip()
    return re.sub(r"\s+-\s+.*$", "", t).strip() or title


async def _get(ses: aiohttp.ClientSession, path: str, params: dict):
    async with ses.get(API + path, params=params) as r:
        if r.status != 200:
            return None
        return await r.json(content_type=None)


async def find(artist: str, title: str, duration_ms: int) -> list | None:
    """Synced lines of the track or None. `duration_ms` makes the match exact (LRCLIB allows a few seconds of difference)."""
    _load()
    artist = (artist or "").split(",")[0].strip()
    key = f"{artist}|{title}|{round(duration_ms / 1000)}".casefold()
    hit = _cache.get(key)
    if hit and (hit["lines"] or time.time() - hit["t"] < 6 * 3600):    # a miss is asked again after six hours
        return hit["lines"]
    lines = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers={"User-Agent": "lostOS"}) as ses:
            for name in dict.fromkeys([title, _clean_title(title)]):
                d = await _get(ses, "/get", {"artist_name": artist, "track_name": name, "duration": round(duration_ms / 1000)})
                if d and d.get("syncedLyrics"):
                    lines = parse_lrc(d["syncedLyrics"])
                    break
            if not lines:
                found = await _get(ses, "/search", {"track_name": _clean_title(title), "artist_name": artist}) or []
                for d in found:
                    if d.get("syncedLyrics") and abs((d.get("duration") or 0) * 1000 - duration_ms) <= 4000:
                        lines = parse_lrc(d["syncedLyrics"])
                        break
    except Exception:
        return None                                                    # no network: not cached, try again later
    _cache[key] = {"t": time.time(), "lines": lines or None}
    _save()
    return lines or None
