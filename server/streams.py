"""Streams for plugins of the server: a plugin names an address of a sound (a track of a service that the network of the speaker does not reach,
or that is simply better fetched by the server), the server fetches it and gives it to the speaker under its own short-lived address
({base_url}/stream/<token>), then the speaker plays it as the music of the house.

  add(url, kind)  -> token        kind "http": one file (mp3, aac, ...); kind "hls": a playlist whose segments are joined into one stream
  handle(request) -> the answer for GET /stream/<token>  (Range is passed on for "http"; "hls" is played from the start)

Only public addresses are fetched (every hop of a redirect is checked: the server must not become a way into its own network), only https
(plain http only for what the plugin names itself when LOSTOS_STREAMS_HTTP=1, for tests), and a token is valid for a few hours."""
import asyncio
import ipaddress
import os
import re
import secrets
import socket
import time
import urllib.parse

import aiohttp
from aiohttp import web

TTL = 4 * 3600
MAX_TOKENS = 40
HLS_MAX_SEGMENTS = 4000
MAX_SEGMENT = 8 * 1024 * 1024
PREFETCH = 3                           # segments fetched ahead (the only part of a track that is in memory)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
ALLOW_PRIVATE = os.environ.get("LOSTOS_STREAMS_ALLOW_PRIVATE") == "1"        # tests only: a local server stands in for the service


class StreamError(Exception):
    pass


async def public(url: str) -> bool:
    """Is the address one of the public internet (https, a host that resolves to global addresses only)?"""
    u = urllib.parse.urlparse(url)
    if u.scheme not in (("http", "https") if ALLOW_PRIVATE else ("https",)) or not u.hostname:
        return False
    if ALLOW_PRIVATE:
        return True
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class Streams:
    def __init__(self, log=print):
        self.items: dict = {}
        self.log = log

    def add(self, url: str, kind: str = "http", title: str = "", cover: str = "", plugin: str = "", headers: dict = None, duration_ms: int = 0) -> str:
        kind = "hls" if kind == "hls" else "http"
        now = time.time()
        for t in [t for t, v in self.items.items() if v["exp"] < now]:
            self.items.pop(t, None)
        while len(self.items) >= MAX_TOKENS:                                  # the oldest go first
            self.items.pop(min(self.items, key=lambda t: self.items[t]["exp"]), None)
        token = secrets.token_urlsafe(16)
        self.items[token] = {"url": url, "kind": kind, "title": title[:120], "cover": cover[:400], "plugin": plugin, "exp": now + TTL, "dur": max(0, int(duration_ms)),
                             "headers": {k: str(v)[:300] for k, v in (headers or {}).items() if k.lower() in ("referer", "origin", "user-agent", "authorization")}}
        return token

    def get(self, token: str):
        v = self.items.get(token)
        return v if v and v["exp"] > time.time() else None

    # ---- the answer to the speaker
    async def handle(self, req: web.Request) -> web.StreamResponse:
        item = self.get(req.match_info["token"])
        if not item:
            return web.Response(status=404)
        try:
            return await (self._hls if item["kind"] == "hls" else self._http)(req, item)
        except (aiohttp.ClientError, asyncio.TimeoutError, StreamError, ConnectionResetError) as e:
            self.log(f"stream {item['plugin']}: {e.__class__.__name__}: {str(e)[:120]}")
            return web.Response(status=502)

    def _headers(self, item: dict, extra: dict = None) -> dict:
        h = {"User-Agent": UA}
        h.update(item.get("headers") or {})
        h.update(extra or {})
        return h

    async def _open(self, ses: aiohttp.ClientSession, url: str, headers: dict):
        """GET with the redirects followed by hand: each hop must be a public address."""
        for _ in range(5):
            if not await public(url):
                raise StreamError("address is not public")
            r = await ses.get(url, headers=headers, allow_redirects=False)
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                loc = r.headers["Location"]
                r.release()
                url = urllib.parse.urljoin(url, loc)
                continue
            return r
        raise StreamError("too many redirects")

    async def _http(self, req: web.Request, item: dict) -> web.StreamResponse:
        frm = start_ms(req)
        extra = {"Range": req.headers["Range"]} if req.headers.get("Range") and not frm else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)) as ses:
            if frm and item.get("dur"):                       # a place in the track: the bytes of that place (a file of constant bit rate: in proportion)
                probe = await self._open(ses, item["url"], self._headers(item, {"Range": "bytes=0-0"}))
                try:
                    m = re.search(r"/(\d+)$", probe.headers.get("Content-Range", ""))
                    total = int(m.group(1)) if m else int(probe.headers.get("Content-Length", 0)) if probe.status == 200 else 0
                finally:
                    probe.release()
                if total:
                    extra = {"Range": f"bytes={min(total - 1, int(total * min(frm, item['dur']) / item['dur']))}-"}
            r = await self._open(ses, item["url"], self._headers(item, extra))
            try:
                if r.status not in (200, 206):
                    raise StreamError(f"upstream answered {r.status}")
                from_here = bool(frm and extra)               # the answer to a start from a place is a new stream, not a part of the old one
                resp = web.StreamResponse(status=200 if from_here else r.status)
                for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if r.headers.get(k) and not (from_here and k == "Content-Range"):
                        resp.headers[k] = r.headers[k]
                await resp.prepare(req)
                async for chunk in r.content.iter_chunked(65536):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
            finally:
                r.release()

    async def prepare(self, token: str):
        """The playlist of a track is read (once): its segments and their times are known (a start from a place is aligned to a segment)."""
        item = self.get(token)
        if item and item["kind"] == "hls" and item.get("segs") is None:
            lock = item.setdefault("lock", asyncio.Lock())
            async with lock:
                if item.get("segs") is None:
                    await self._fetch_hls(item)
        return item

    def align(self, token: str, ms: int) -> int:
        """The place where a start from `ms` really begins: the segment of a playlist that holds it (a file: the very place)."""
        item = self.get(token)
        if not item or item["kind"] != "hls" or not item.get("segs"):
            return max(0, int(ms))
        return max((st for st, _ in item["segs"] if st <= ms), default=0)

    async def _hls(self, req: web.Request, item: dict) -> web.StreamResponse:
        """The segments of a playlist as one stream (fMP4 with its init segment, or MPEG-TS / MP3 / Ogg segments), passed on as they come: a few segments are in memory,
        never the whole track (a mix of hours, several tracks at once). `?from=<ms>`: the stream starts at the segment that holds that place (that is how a jump in a track is done)."""
        await self.prepare(next((t for t, v in self.items.items() if v is item), ""))
        return await self._hls_stream(req, item, start_ms(req))

    async def _hls_stream(self, req: web.Request, item: dict, frm: int) -> web.StreamResponse:
        """A long track: the segments are fetched a few ahead and written as they come (memory: a few segments), without a length."""
        media, init = item["media"], item["init_seg"]
        k = max((i for i, (st, _) in enumerate(item["segs"]) if st <= frm), default=0)
        resp = web.StreamResponse(status=200, headers={"Content-Type": item["ctype"]})
        await resp.prepare(req)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)) as ses:
            gate = asyncio.Semaphore(PREFETCH)

            async def fetch(url: str) -> bytes:
                async with gate:
                    return await self._segment(ses, url, item)

            if init:
                await resp.write(await fetch(init))
            queue = [asyncio.ensure_future(fetch(media[i][0])) for i in range(k, min(len(media), k + PREFETCH))]
            nxt, base = k + PREFETCH, None
            try:
                while queue:
                    chunk = await queue.pop(0)
                    if nxt < len(media):
                        queue.append(asyncio.ensure_future(fetch(media[nxt][0])))
                        nxt += 1
                    if item["ctype"] == "audio/mp4" and frm:
                        chunk, base = rebase_tfdt(chunk, base, True)
                    await resp.write(chunk)
            finally:
                for t in queue:
                    t.cancel()
        await resp.write_eof()
        return resp

    async def _segment(self, ses: aiohttp.ClientSession, url: str, item: dict) -> bytes:
        seg = await self._open(ses, url, self._headers(item))
        try:
            if seg.status != 200:
                raise StreamError(f"segment answered {seg.status}")
            out = bytearray()
            async for chunk in seg.content.iter_chunked(65536):
                out += chunk
                if len(out) > MAX_SEGMENT:
                    raise StreamError("segment is too big")
            return bytes(out)
        finally:
            seg.release()

    async def _fetch_hls(self, item: dict):
        """Reads the playlist (a few KB) and fills item: segs [(start ms, index)], media [(address, seconds)], init_seg, ctype."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)) as ses:
            text = await self._text(ses, item["url"], item)
            variant = hls_variant(text, item["url"])
            item_url = item["url"]
            if variant:                                       # a master playlist: the best of its variants is the playlist of the segments
                text, item_url = await self._text(ses, variant, item), variant
            segs = hls_segments(text, item_url)
            if not segs:
                raise StreamError("empty playlist")
            media = [(u, d) for u, d, is_init in segs if not is_init]
            starts, t = [], 0.0
            for _, d in media:
                starts.append(int(t * 1000))
                t += d
            item["ctype"] = ("audio/mp4" if any(".m4s" in u or ".mp4" in u or ".aac" in u for u, _, _ in segs[:3])
                             else "audio/ogg" if any(".ogg" in u or ".opus" in u for u, _, _ in segs[:3]) else "audio/mpeg")
            item["media"], item["init_seg"] = media, next((u for u, _, is_init in segs if is_init), "")
            item["segs"] = [(st, i) for i, st in enumerate(starts)]

    async def _text(self, ses: aiohttp.ClientSession, url: str, item: dict) -> str:
        r = await self._open(ses, url, self._headers(item))
        try:
            if r.status != 200:
                raise StreamError(f"playlist answered {r.status}")
            out = bytearray()
            async for chunk in r.content.iter_chunked(65536):    # (read(n) gives only what has come so far: a cut playlist would end the track early)
                out += chunk
                if len(out) > 2 * 1024 * 1024:
                    raise StreamError("playlist is too big")
            return out.decode("utf-8", "replace")
        finally:
            r.release()


def rebase_tfdt(buf: bytes, base=None, with_base: bool = False):
    """The fragments of an fMP4 stream carry their place in the track (tfdt, baseMediaDecodeTime). A stream that starts in the middle must start at zero, else the
    player lets the first fragment wait for its time to come and plays nothing until then: the first value is taken off every one."""
    out, i = bytearray(buf), 0
    while True:
        i = out.find(b"tfdt", i)
        if i < 0:
            break
        size = int.from_bytes(out[i - 4:i], "big") if i >= 4 else 0
        ver = out[i + 4] if i + 4 < len(out) else -1
        if ver == 1 and size == 20 and i + 16 <= len(out):
            v = int.from_bytes(out[i + 8:i + 16], "big")
            base = v if base is None else base
            out[i + 8:i + 16] = max(0, v - base).to_bytes(8, "big")
        elif ver == 0 and size == 16 and i + 12 <= len(out):
            v = int.from_bytes(out[i + 8:i + 12], "big")
            base = v if base is None else base
            out[i + 8:i + 12] = max(0, v - base).to_bytes(4, "big")
        i += 4
    return (bytes(out), base) if with_base else bytes(out)


def start_ms(req: web.Request) -> int:
    try:
        return max(0, int(req.query.get("from", "0")))
    except ValueError:
        return 0


def hls_segments(text: str, base: str) -> list:
    """[(address, seconds, is_init)] of a media playlist: the init segment (#EXT-X-MAP) first, then the segments in order (#EXTINF gives the seconds)."""
    if "#EXT-X-STREAM-INF" in text:
        return []
    out, dur = [], 0.0
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'#EXT-X-MAP:.*URI="([^"]+)"', line)
        if m:
            out.append((urllib.parse.urljoin(base, m.group(1)), 0.0, True))
        elif line.startswith("#EXTINF:"):
            try:
                dur = float(line[8:].split(",")[0])
            except ValueError:
                dur = 0.0
        elif line and not line.startswith("#"):
            out.append((urllib.parse.urljoin(base, line), dur, False))
            dur = 0.0
        if len(out) >= HLS_MAX_SEGMENTS:
            break
    return out


def hls_variant(text: str, base: str) -> str:
    """The address of the best variant of a master playlist (the highest BANDWIDTH), "" when it is a media playlist."""
    best, best_bw, lines = "", -1, text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(m.group(1)) if m else 0
            nxt = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), "")
            if nxt and bw > best_bw:
                best, best_bw = urllib.parse.urljoin(base, nxt), bw
    return best


def hls_parts(text: str, base: str) -> list:
    """The addresses to fetch for a media playlist, in order (the init segment first)."""
    return [u for u, _, _ in hls_segments(text, base)]
