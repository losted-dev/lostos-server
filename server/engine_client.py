"""The client of the shared engine service (engine_service.py): what a tenant uses instead of loading Silero and SpeechBrain itself.

Thread-safe, blocking (a call takes about a millisecond on the same machine), reconnects by itself. When the service is not there, a VAD call answers "no speech" and an
embedding call answers None, so one broken service never stops a speaker's other functions."""
import os
import socket
import struct
import threading
import time

import numpy as np

ADDR = os.environ.get("LOSTOS_ENGINE", "127.0.0.1:8771")


class EngineClient:
    def __init__(self, addr: str = None, log=print):
        self.addr = addr or ADDR
        self.log = log
        self.sock = None
        self.lock = threading.Lock()
        self.rid = 0
        self.down_until = 0.0

    def _connect(self):
        if self.addr.startswith("/") or self.addr.startswith("unix:"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.addr.replace("unix:", ""))
        else:
            host, port = self.addr.rsplit(":", 1)
            s = socket.create_connection((host, int(port)), timeout=3)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(15)
        return s

    @staticmethod
    def _read(sock, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("engine closed the connection")
            buf += chunk
        return buf

    def call(self, op: int, payload: bytes):
        """The answer payload, or None when the service is not reachable or refused."""
        if time.time() < self.down_until:
            return None
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self.sock = self._connect()
                    self.rid = (self.rid + 1) & 0xFFFFFFFF
                    self.sock.sendall(struct.pack(">BII", op, self.rid, len(payload)) + payload)
                    rid, status, n = struct.unpack(">IBI", self._read(self.sock, 9))
                    data = self._read(self.sock, n)
                    if rid != self.rid:
                        raise ConnectionError("out of step")
                    return data if status == 0 else None
                except (OSError, ConnectionError, struct.error):
                    try:
                        self.sock and self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
            self.down_until = time.time() + 2.0                          # do not hammer a service that is down: the next try in two seconds
            self.log("engine service is not reachable")
            return None

    def ping(self) -> bool:
        return self.call(3, b"") == b"pong"

    def vad(self, session: bytes, reset: bool, pcm: bytes) -> float:
        r = self.call(1, session + bytes([1 if reset else 0]) + pcm)
        return struct.unpack(">f", r)[0] if r and len(r) == 4 else 0.0

    def embed(self, pcm: bytes):
        """A unit vector (192 numbers) for the speech in `pcm` (s16le 16 kHz), None when there is too little of it or the service is away."""
        r = self.call(2, pcm)
        return np.frombuffer(r, dtype=np.float32).copy() if r else None


class RemoteVad:
    """Looks like the Silero model (`model(x, 16000).item()`, `reset_states()`), but the work is done by the shared service. One instance = one utterance (one session)."""

    class _P:
        def __init__(self, v):
            self.v = v

        def item(self):
            return self.v

    def __init__(self, client: EngineClient):
        self.client = client
        self.sid = os.urandom(8)
        self.fresh = True

    def __call__(self, x, sr=16000):
        a = np.asarray(x, dtype=np.float32)
        if len(a) != 512:
            return self._P(0.0)
        pcm = np.clip(a * 32768.0, -32768, 32767).astype(np.int16).tobytes()
        p = self.client.vad(self.sid, self.fresh, pcm)
        self.fresh = False
        return self._P(p)

    def reset_states(self):
        self.fresh = True


class VADIterator:
    """The streaming wrapper of Silero (silero_vad.VADIterator), written without torch: {"start": n} when speech begins, {"end": n} after enough silence."""

    def __init__(self, model, threshold=0.5, sampling_rate=16000, min_silence_duration_ms=100, speech_pad_ms=30):
        self.model, self.threshold, self.sr = model, threshold, sampling_rate
        self.min_silence = sampling_rate * min_silence_duration_ms / 1000
        self.pad = sampling_rate * speech_pad_ms / 1000
        self.reset_states()

    def reset_states(self):
        self.model.reset_states()
        self.triggered, self.temp_end, self.current = False, 0, 0

    def __call__(self, x):
        window = len(x)
        self.current += window
        p = self.model(x, self.sr).item()
        if p >= self.threshold and self.temp_end:
            self.temp_end = 0
        if p >= self.threshold and not self.triggered:
            self.triggered = True
            return {"start": int(max(0, self.current - self.pad - window))}
        if p < self.threshold - 0.15 and self.triggered:
            if not self.temp_end:
                self.temp_end = self.current
            if self.current - self.temp_end < self.min_silence:
                return None
            end = self.temp_end + self.pad - window
            self.temp_end, self.triggered = 0, False
            return {"end": int(end)}
        return None
