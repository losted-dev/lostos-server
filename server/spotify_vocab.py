"""The music of a person as the voice assistant should spell it: favourite artists and tracks collected from his Spotify into music_vocab.json (in his own folder).
The server refreshes it by itself every few hours while he is logged in to Spotify (voice_server.music_refresher); `python spotify_vocab.py` does it once by hand."""
import asyncio
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "music_vocab.json")


async def collect(s) -> dict:
    """s: a spotify_client.Spotify with a login. -> {"artists": [...], "tracks": ["Artist - Title", ...]}"""
    artists, tracks = [], []

    def add(lst, x):
        if x and x not in lst:
            lst.append(x)

    def track(t):
        if t and t.get("artists"):
            add(tracks, f"{t['artists'][0]['name']} - {t['name']}")
            add(artists, t["artists"][0]["name"])
    for rng in ("short_term", "medium_term", "long_term"):
        for a in (await s._req("GET", "/me/top/artists", {"limit": 30, "time_range": rng})).get("items", []):
            add(artists, a["name"])
        for t in (await s._req("GET", "/me/top/tracks", {"limit": 30, "time_range": rng})).get("items", []):
            track(t)
    for it in (await s._req("GET", "/me/player/recently-played", {"limit": 50})).get("items", []):
        track(it.get("track"))
    for it in (await s._req("GET", "/me/tracks", {"limit": 50})).get("items", []):
        track(it.get("track"))
    return {"artists": artists[:80], "tracks": tracks[:80]}


def save(data: dict):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, FILE)


if __name__ == "__main__":
    from spotify_client import Spotify

    async def main():
        out = await collect(Spotify(""))
        save(out)
        print(len(out["artists"]), "artists,", len(out["tracks"]), "tracks:", ", ".join(out["artists"][:12]))
    asyncio.run(main())
