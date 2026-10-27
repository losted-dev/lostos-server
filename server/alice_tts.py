"""Alice's voice from Yandex's cloud: the same uniproxy websocket the stock aliced of the speaker uses for TTS
(wss://uniproxy.alice.yandex.net/uni.ws, event TTS.Generate, the answer is Ogg/Opus). Called from worker threads; on any
failure it returns None and the caller falls back to Silero."""
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid

import numpy as np

URL = "wss://uniproxy.alice.yandex.net/uni.ws"
_DEV = uuid.uuid4().hex                      # a made-up device id, nothing of the account is involved
_FFMPEG_FALLBACKS = [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")]


def ffmpeg_path() -> str:
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg") or next((p for p in _FFMPEG_FALLBACKS if os.path.exists(p)), "")


def _event(ns: str, name: str, payload: dict) -> str:
    return json.dumps({"event": {"header": {"namespace": ns, "name": name, "messageId": str(uuid.uuid4())}, "payload": payload}})


async def _fetch(text: str, voice: str, speed: float, timeout: float) -> bytes:
    import aiohttp
    audio = bytearray()
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, headers={"User-Agent": "Yandex Station"}, timeout=timeout) as ws:
            await ws.send_str(_event("System", "SynchronizeState", {
                "auth_token": os.environ.get("UNI_KEY", "x"), "uuid": _DEV, "lang": "ru-RU", "voice": voice,
                "application": {"app_id": "aliced", "app_version": "1.0", "platform": "linux", "os_version": "1", "uuid": _DEV, "device_id": _DEV}}))
            await ws.send_str(_event("TTS", "Generate", {"text": text, "voice": voice, "lang": "ru-RU", "format": "audio/opus",
                                                         "quality": "UltraHigh", "speed": speed, "emotion": "neutral"}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                m = await asyncio.wait_for(ws.receive(), max(0.5, deadline - time.time()))
                if m.type == aiohttp.WSMsgType.BINARY:
                    audio.extend(m.data[4:])                  # every frame starts with a 4-byte stream id
                elif m.type == aiohttp.WSMsgType.TEXT:
                    if "streamcontrol" in m.data:             # the end of the stream
                        break
                    if "EventException" in m.data:
                        raise RuntimeError(m.data[:200])
                else:
                    break
    return bytes(audio)


def synth(text: str, rate: int, voice: str = "shitova", speed: float = 1.0, timeout: float = 10.0):
    """int16 mono PCM at `rate`, or None when the cloud or ffmpeg is not available."""
    ff = ffmpeg_path()
    if not ff or not text.strip():
        return None
    try:
        ogg = asyncio.run(_fetch(text, voice, speed, timeout))
        if not ogg:
            return None
        out = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", str(rate), "-ac", "1", "pipe:1"],
                             input=ogg, capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return np.frombuffer(out.stdout, dtype=np.int16) if out.stdout else None
    except Exception as e:
        print(f"alice tts: {e!r}")
        return None
