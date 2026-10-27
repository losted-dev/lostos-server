"""Speech recognition of Yandex's cloud: the uniproxy websocket the stock Alice uses (event ASR.Recognize, 16 kHz s16le).
recognize() returns the text, "" when nothing was recognised, None when the cloud failed (the caller falls back to Whisper)."""
import asyncio
import json
import time
import uuid

URL = "wss://uniproxy.alice.yandex.net/uni.ws"
_DEV = uuid.uuid4().hex                      # a made-up device id, nothing of an account is involved


def _event(ns: str, name: str, payload: dict, **hdr) -> str:
    return json.dumps({"event": {"header": {"namespace": ns, "name": name, "messageId": str(uuid.uuid4()), **hdr}, "payload": payload}})


def _words(t: str) -> list:
    return "".join(c if c.isalnum() or c in " -" else " " for c in t.lower()).split()


def longest(final: str, partials: list) -> str:
    """The cloud's final text, unless it is only the beginning of what the partial results had reached (a phrase cut short: "Включи." for "Включи онда андрей"):
    then the longest partial that starts with the same words."""
    fw = _words(final)
    best, bw = final, fw
    for t in partials:
        w = _words(t)
        if len(w) > len(bw) and w[:len(fw)] == fw:
            best, bw = t, w
    if best is not final and best:
        best = best[:1].upper() + best[1:]
    return best


async def _recognize(pcm16: bytes, timeout: float) -> str:
    import aiohttp
    deadline = time.time() + timeout
    text = ""
    partials = []                                           # what the cloud thought on the way: the final answer sometimes drops the end of a phrase that the partial ones had
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, headers={"User-Agent": "Yandex Station"}, timeout=timeout) as ws:
            await ws.send_str(_event("System", "SynchronizeState", {
                "auth_token": "x", "uuid": _DEV, "lang": "ru-RU",
                "application": {"app_id": "aliced", "app_version": "1.0", "platform": "linux", "os_version": "1", "uuid": _DEV, "device_id": _DEV}}))
            await ws.send_str(_event("ASR", "Recognize", {"lang": "ru-RU", "topic": "general", "format": "audio/x-pcm;bit=16;rate=16000",
                                                          "punctuation": True, "advancedASROptions": {"partial_results": True}}, streamId=1))
            for i in range(0, len(pcm16), 6400):                    # 200 ms pieces; every frame = 4-byte big-endian stream id + audio
                await ws.send_bytes((1).to_bytes(4, "big") + pcm16[i:i + 6400])
            await ws.send_str(json.dumps({"streamcontrol": {"messageId": str(uuid.uuid4()), "streamId": 1, "action": 0, "reason": 0}}))
            while time.time() < deadline:
                m = await asyncio.wait_for(ws.receive(), max(0.5, deadline - time.time()))
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    d = json.loads(m.data).get("directive", {})
                except ValueError:
                    continue
                p = d.get("payload", {})
                if d.get("header", {}).get("name") == "EventException" or "error" in p:
                    raise RuntimeError(str(p)[:200])
                if d.get("header", {}).get("namespace") == "ASR":
                    rec = p.get("recognition") or []
                    if rec:
                        text = (rec[0].get("normalized") or "").strip()
                        if not p.get("endOfUtt"):
                            partials.append(text)
                    if p.get("endOfUtt"):
                        return longest(text, partials)
    raise TimeoutError("no final result")


def recognize(pcm16: bytes, timeout: float = 8.0):
    try:
        return asyncio.run(_recognize(pcm16, timeout))
    except Exception as e:
        print(f"yandex asr: {e!r}")
        return None
