"""Tests of the SoundCloud plugin (ported from soundiya) and of the streams of the server: no network, a local server stands in for SoundCloud.
Run from the project folder:  python -I server/tests_soundcloud.py"""
import asyncio
import importlib.util
import os
import sys
import types

os.environ["LOSTOS_STREAMS_ALLOW_PRIVATE"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or not extra else f"  {extra}"))
    OK = OK and bool(cond)


# ---- the plugin, with a stand-in for the SDK
calls = []
fake = types.ModuleType("lostos_sdk")
fake.PluginError = type("PluginError", (Exception,), {})
fake.call = lambda method, timeout=15.0, **kw: calls.append((method, kw)) or {"tools.register": True}.get(method)
fake.handle = lambda method: (lambda fn: fn)
fake.page = lambda fn: fn
fake.on = lambda event: (lambda fn: fn)
fake.run = lambda: None
sys.modules["lostos_sdk"] = fake
spec = importlib.util.spec_from_file_location("sc_plugin", os.path.join(HERE, "plugins", "user", "ru.lostos.soundcloud", "main.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

check("the plugin gives its commands to the assistant", [kw["name"] for m, kw in calls if m == "tools.register"] == ["play_soundcloud", "play_soundcloud_favorites", "soundcloud_next", "soundcloud_previous"])
check("the plugin shows a tile of the shelf of the sources", any(m == "tile.set" and kw.get("shelf") == "sources" and kw.get("tint") == "sc" for m, kw in calls))
check("a client_id is found in a script", sc.CLIENT_ID_RE.search('a={client_id:"' + "A" * 32 + '",x:1}').group(1) == "A" * 32)
check("a client_id in JSON form is found", sc.CLIENT_ID_RE.search('{"client_id":"' + "b1" * 16 + '"}').group(1) == "b1" * 16)
check("scripts of the home page are listed", sc.SCRIPT_RE.findall('<script crossorigin src="https://a-v2.sndcdn.com/assets/0-abc.js"></script><script src="https://x.com/a.js">') == ["https://a-v2.sndcdn.com/assets/0-abc.js"])

mp3p = {"url": "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/aaa/stream/progressive", "preset": "mp3_0_1", "format": {"protocol": "progressive", "mime_type": "audio/mpeg"}}
mp3h = {"url": "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/bbb/stream/hls", "preset": "mp3_0_1", "format": {"protocol": "hls", "mime_type": "audio/mpeg"}}
aac = {"url": "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/ccc/stream/hls", "preset": "aac_160k", "format": {"protocol": "hls", "mime_type": 'audio/mp4; codecs="mp4a.40.2"'}}
opus = {"url": "u", "preset": "opus_0_0", "format": {"protocol": "hls", "mime_type": "audio/ogg"}}
raw = {"id": 7, "title": "Song", "user": {"username": "Artist"}, "full_duration": 185000, "artwork_url": "https://i1.sndcdn.com/artworks-x-large.jpg", "track_authorization": "AUTH",
       "media": {"transcodings": [opus, aac, mp3h, mp3p]}}
t = sc.parse_track(raw)
check("a track is parsed (title, artist, seconds, a smaller cover)", t and t["title"] == "Song" and t["artist"] == "Artist" and t["duration"] == 185 and "t500x500" in t["cover"], t)
check("a plain mp3 file is the first choice", sc.pick(t) is mp3p)
check("without it an mp3 playlist, then AAC", sc.pick({"media": [opus, aac, mp3h]}) is mp3h and sc.pick({"media": [opus, aac]}) is aac)
check("Opus is the last choice but a choice", sc.pick({"media": [opus]}) is opus and sc.pick({"media": [opus, aac]}) is aac)
check("policy BLOCK is dropped", sc.parse_track({**raw, "policy": "BLOCK"}) is None)
check("policy SNIP (a 30 s preview) is dropped", sc.parse_track({**raw, "policy": "SNIP"}) is None)
check("a track with only protected formats is not offered", sc.parse_track({**raw, "media": {"transcodings": [{"url": "u", "preset": "aac_256k", "format": {"protocol": "ctr-encrypted-hls", "mime_type": "audio/mp4"}}]}}) is None)
check("a track without a stream is dropped", sc.parse_track({**raw, "media": {"transcodings": []}}) is None)

asked = []
sc.api = lambda path, params=None: asked.append((path, params)) or {"url": "https://cf-media.sndcdn.com/x.mp3"}
url, kind = sc.resolve(t)
check("the stream address is asked with the authorisation of the track", asked == [("/media/soundcloud:tracks:1/aaa/stream/progressive", {"track_authorization": "AUTH"})] and url.endswith("x.mp3") and kind == "http", asked)
import urllib.error  # noqa: E402


def refuse_first(path, params=None):
    if path.endswith("/progressive"):
        raise urllib.error.HTTPError("u", 404, "gone", {}, None)
    return {"url": "https://cf-hls.sndcdn.com/p.m3u8"}


sc.api = refuse_first
check("a file that SoundCloud refuses (404) falls back to the playlist", sc.resolve(t) == ("https://cf-hls.sndcdn.com/p.m3u8", "hls"))
sc.api = lambda path, params=None: {"url": "https://cf-hls.sndcdn.com/p.m3u8"}
check("a playlist is told as hls", sc.resolve({**t, "media": [aac]})[1] == "hls")

check("a pasted token is cleaned (a header, quotes, OAuth in front)", sc.clean_token('authorization: OAuth 2-293847-123456-AbCdEf') == "2-293847-123456-AbCdEf" and sc.clean_token('"2-1-a_b"') == "2-1-a_b" and sc.clean_token("мусор") == "")
# the profile and the likes (public, no login)
store = {}
fake.call = lambda method, timeout=15.0, **kw: calls.append((method, kw)) or ({"storage.get": store.get(kw.get("key")), "tools.register": True}.get(method))


def put(method, timeout=15.0, **kw):
    if method == "storage.set":
        store[kw["key"]] = kw["value"]
    return fake_get(method, **kw)


def fake_get(method, **kw):
    calls.append((method, kw))
    return store.get(kw.get("key")) if method == "storage.get" else True


sc.call = put
sc.api = lambda path, params=None: {"kind": "user", "id": 55, "username": "Me"} if path == "/resolve" else {"collection": [{"track": raw}]}
check("a profile is taken by its address", sc.set_profile("https://soundcloud.com/me") == "Me" and store.get("profile_id") == 55)


def refused(x):
    try:
        sc.set_profile(x)
    except ValueError:
        return True
    return False


check("an address that is not a profile is refused", refused("https://evil.example/me") and refused("soundcloud.com/me/likes"))
check("the likes of the profile are listed without a login", [x["title"] for x in sc.favorites()] == ["Song"])
check("without a profile and a token the likes ask for the profile", "профиль" in (lambda: (store.clear(), sc.start_favorites())[1])())
sc.call = lambda method, timeout=15.0, **kw: calls.append((method, kw)) or None

# the likes by pages
pool = [{**raw, "id": 1000 + i, "title": f"Like {i}"} for i in range(45)]


def likes_api(path, params=None, need_login=False):
    if path.endswith("/track_likes"):
        at = int((params or {}).get("offset") or 0)
        part = pool[at:at + 20]
        out = {"collection": [{"track": x} for x in part]}
        if at + 20 < len(pool):
            out["next_href"] = f"https://api-v2.soundcloud.com/users/55/track_likes?offset={at + 20}&limit=20"
        return out
    return {"kind": "user", "id": 55, "username": "Me"}


sc.api = likes_api
store["profile_id"] = 55
sc.state.update(favs=[], fav_cursor="", fav_done=False, fav_page=0, fav_open=True)
sc.favs_load(21)
check("the first page of likes is read as far as it needs (and one more)", len(sc.state["favs"]) >= 21 and not sc.state["fav_done"])
page2 = sc.ui({"event": "press", "id": "favnext"})
fav_list = next(b for b in page2["blocks"] if b["id"] == "favs")
check("the second page shows the next twenty likes", [i["title"] for i in fav_list["items"]][:2] == ["Like 20", "Like 21"] and "21–40" in fav_list["title"], fav_list["title"])
page3 = sc.ui({"event": "press", "id": "favnext"})
check("the last page ends the list and has no way on", len(next(b for b in page3["blocks"] if b["id"] == "favs")["items"]) == 5 and not any(i["id"] == "favnext" for b in page3["blocks"] if b["type"] == "buttons" for i in b["items"]))
back = sc.ui({"event": "press", "id": "favprev"})
check("a page back", next(b for b in back["blocks"] if b["id"] == "favs")["items"][0]["title"] == "Like 20")
sc.state.update(favs=[], fav_open=False)
sc.call = lambda method, timeout=15.0, **kw: calls.append((method, kw)) or None

# the queue and the commands
calls.clear()
sc.search = lambda q, limit=10: [t, {**t, "id": 8, "title": "Two"}] if q == "lofi" else []
sc.related = lambda tr, limit=12: []
sc.resolve = lambda tr: ("https://cdn.sndcdn.com/a.mp3", "http")
msg = sc.start("lofi")
check("a search starts the first track", "Artist — Song" in msg and any(m == "audio.play_url" and kw["kind"] == "http" and kw["title"] == "Song" and kw["artist"] == "Artist" for m, kw in calls), msg)
check("the queue holds the found tracks", [x["title"] for x in sc.state["queue"]] == ["Song", "Two"])
check("nothing found is said plainly", "ничего не нашлось" in sc.start("пусто"))
check("the next track of the queue plays", "Two" in sc.tool({"name": "soundcloud_next"}) and sc.state["index"] == 1)
check("the page lists the queue and marks the playing track", [i["on"] for i in sc.ui({"view": "player"})["items"]] == [False, True])
check("the page of the panel has a search, the buttons and the queue", [b["type"] for b in sc.ui({"event": "open"})["blocks"]][:3] == ["search", "buttons", "list"])

# ---- the streams of the server
import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
import streams  # noqa: E402


async def main():
    body = bytes(range(256)) * 400                           # 102400 bytes

    async def file(req):
        rng = req.headers.get("Range")
        if rng:
            a = int(rng.split("=")[1].split("-")[0])
            return web.Response(body=body[a:], status=206, headers={"Content-Range": f"bytes {a}-{len(body) - 1}/{len(body)}", "Content-Type": "audio/mpeg"})
        return web.Response(body=body, content_type="audio/mpeg")

    async def playlist(req):
        return web.Response(text='#EXTM3U\n#EXT-X-MAP:URI="/init.mp4"\n#EXTINF:5,\n/seg1.m4s\n#EXTINF:5,\n/seg2.m4s\n#EXT-X-ENDLIST\n')

    async def init(req):
        return web.Response(body=b"INIT")

    async def seg(req):
        return web.Response(body=req.match_info["n"].encode() * 10, content_type="audio/mp4")

    up = web.Application()
    up.router.add_get("/a.mp3", file)
    up.router.add_get("/p.m3u8", playlist)
    up.router.add_get("/init.mp4", init)
    up.router.add_get("/{n:seg[12]}.m4s", seg)
    up_runner = web.AppRunner(up)
    await up_runner.setup()
    site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    st = streams.Streams(lambda *_: None)
    app = web.Application()
    app.router.add_get("/stream/{token}", st.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site2 = web.TCPSite(runner, "127.0.0.1", 0)
    await site2.start()
    sp = site2._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession() as ses:
        tok = st.add(f"http://127.0.0.1:{port}/a.mp3", "http", "t", "", "p")
        async with ses.get(f"http://127.0.0.1:{sp}/stream/{tok}") as r:
            data = await r.read()
            check("a file passes through the server whole", r.status == 200 and data == body)
        async with ses.get(f"http://127.0.0.1:{sp}/stream/{tok}", headers={"Range": "bytes=100-"}) as r:
            check("a range is passed on", r.status == 206 and (await r.read()) == body[100:])
        tok2 = st.add(f"http://127.0.0.1:{port}/p.m3u8", "hls", "t", "", "p")
        async with ses.get(f"http://127.0.0.1:{sp}/stream/{tok2}") as r:
            data = await r.read()
            check("the segments of a playlist are joined (the init segment first)", r.status == 200 and data == b"INIT" + b"seg1" * 10 + b"seg2" * 10, (r.status, data[:30]))
        async with ses.get(f"http://127.0.0.1:{sp}/stream/{tok2}?from=6000") as r:
            check("a start from a place begins at the segment that holds it (the init segment first)", r.status == 200 and (await r.read()) == b"INIT" + b"seg2" * 10)
        check("a place is aligned to the start of its segment", st.align(tok2, 7300) == 5000 and st.align(tok2, 100) == 0 and st.align(tok, 4321) == 4321)
        tok3 = st.add(f"http://127.0.0.1:{port}/a.mp3", "http", "t", "", "p", None, 100000)
        async with ses.get(f"http://127.0.0.1:{sp}/stream/{tok3}?from=50000") as r:
            half = len(body) // 2
            check("a start from a place of a file begins in proportion (half the track: half the bytes)", r.status == 200 and (await r.read()) == body[half:])
        check("a playlist track keeps none of its bytes in memory", "data" not in st.items[tok2] and "init" not in st.items[tok2])
        async with ses.get(f"http://127.0.0.1:{sp}/stream/nonexistent") as r:
            check("an unknown token is 404", r.status == 404)
        st.items[tok]["exp"] = 0
        async with ses.get(f"http://127.0.0.1:{sp}/stream/{tok}") as r:
            check("an expired token is 404", r.status == 404)
    for i in range(streams.MAX_TOKENS + 5):
        st.add("https://x.test/a.mp3")
    check("the table of tokens is bounded", len(st.items) <= streams.MAX_TOKENS)
    check("the best variant of a master playlist is found", streams.hls_variant("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=128000\nhigh.m3u8\n", "https://a/dir/m.m3u8") == "https://a/dir/high.m3u8" and streams.hls_variant("#EXTM3U\n#EXTINF:5,\ns.m4s\n", "https://a/b") == "")
    box1 = lambda v: (20).to_bytes(4, "big") + b"tfdt" + bytes([1, 0, 0, 0]) + v.to_bytes(8, "big")
    box0 = lambda v: (16).to_bytes(4, "big") + b"tfdt" + bytes([0, 0, 0, 0]) + v.to_bytes(4, "big")
    moved = streams.rebase_tfdt(b"xx" + box1(480000) + b"data" + box1(960000) + box0(500) + b"tail")
    check("the time of the first fragment is taken off every fragment (a stream that starts in the middle starts at zero)", moved == b"xx" + box1(0) + b"data" + box1(480000) + box0(0) + b"tail")
    check("a tfdt inside other data is left alone", streams.rebase_tfdt(b"abtfdtcd") == b"abtfdtcd")
    check("hls_parts refuses a master playlist", streams.hls_parts("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n", "https://a/b.m3u8") == [])
    check("hls_parts makes the addresses absolute", streams.hls_parts('#EXTM3U\n#EXT-X-MAP:URI="i.mp4"\ns1.m4s\n', "https://a/dir/p.m3u8") == ["https://a/dir/i.mp4", "https://a/dir/s1.m4s"])
    await runner.cleanup()
    await up_runner.cleanup()
    streams.ALLOW_PRIVATE = False
    check("the server does not fetch an address of its own network", not await streams.public("https://127.0.0.1/x") and not await streams.public("http://example.com/x"))
    check("plain http is refused", not await streams.public("http://93.184.216.34/x"))


# ---- the end of a track is decided by what the speaker plays, not by the clock
import time as _time

def end_scenario(media_state, saw=True):
    """Runs check() once with this answer of media.state: -> (the track that was started next or None, the watch that was scheduled or None)"""
    started, watched = [], []
    sc.state.update(queue=[{"id": 1, "title": "a", "artist": "x", "cover": "", "duration": 100}, {"id": 2, "title": "b", "artist": "x", "cover": "", "duration": 100}], index=0, play_id=7)
    old_call, old_play, old_watch = sc.call, sc.play_index, sc.watch
    sc.call = lambda method, timeout=15.0, **kw: media_state if method == "media.state" else None
    sc.play_index = lambda i: started.append(i) or "ok"
    sc.watch = lambda pid, delay, saw_playing, until: watched.append((delay, saw_playing))
    try:
        sc.check(7, saw, _time.time() + 3600)
    finally:
        sc.call, sc.play_index, sc.watch = old_call, old_play, old_watch
    return (started[0] if started else None), (watched[0] if watched else None)


mine = {"plugin": "ru.lostos.soundcloud", "duration": 100000}
check("a track that played to its end and stopped goes on with the next", end_scenario({**mine, "playing": False, "progress": 99000})[0] == 1)
check("a track that was PAUSED in the middle does not (the clock must not undo a pause)", end_scenario({**mine, "playing": False, "progress": 40000})[0] is None)
check("a paused track is looked at again later", end_scenario({**mine, "playing": False, "progress": 40000})[1] == (sc.PAUSED_POLL_S, False))
check("a track that is still playing is looked at when it should be over", end_scenario({**mine, "playing": True, "progress": 60000})[1] == (42.0, True))
check("another source that plays (Spotify) is left alone", end_scenario({"plugin": "", "source": "Spotify", "playing": True, "duration": 200000, "progress": 1000})[0] is None)
check("nothing playing from us and nothing known does nothing", end_scenario({})[0] is None)
check("a pause in the last seconds without ever seeing it play is not an end", end_scenario({**mine, "playing": False, "progress": 99000}, saw=False)[0] is None)

asyncio.run(main())
print("ALL PASS" if OK else "SOME FAILED")
