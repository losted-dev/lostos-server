"""The shared engines of the server: ONE process with the models that are too heavy to load for every person (Silero VAD and the SpeechBrain voice encoder).

Every person has his own light server process (tenant, ~200 MB); they all ask this service. Nothing about the person is kept here: the service only turns
sound into numbers (is it speech? which voice is it?); who the voice belongs to is decided by the tenant with HIS OWN voice prints.

Protocol over one stream (a unix socket, or a loopback TCP port where unix sockets do not exist; the address is in LOSTOS_ENGINE):
  request  : op (1 byte) | request id (4) | payload length (4) | payload
  response : request id (4) | status (1: 0 ok, 1 error) | payload length (4) | payload
  op 1 VAD  : payload = session id (8 bytes) | flags (1: bit0 = reset the state first) | 512 samples int16 (16 kHz)  ->  float32 probability of speech
  op 2 EMBED: payload = s16le 16 kHz audio                                                                         ->  192 float32 (a unit vector), empty when too short
  op 3 PING : -> b"pong"
The VAD keeps one small model state per session (a session is one utterance of one tenant); idle sessions are dropped after two minutes.
Run:  python engine_service.py            (address from LOSTOS_ENGINE, default 127.0.0.1:8771)"""
import asyncio
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ADDR = os.environ.get("LOSTOS_ENGINE", "127.0.0.1:8771")
MAX_SESSIONS = 256
IDLE_S = 120
MIN_SECONDS = 0.8


def log(text: str):
    print(time.strftime("%H:%M:%S ") + text, flush=True)


class Engines:
    def __init__(self):
        import torch
        torch.set_num_threads(max(1, int(os.environ.get("LOSTOS_ENGINE_THREADS", "1"))))
        self.torch = torch
        from silero_vad import load_silero_vad
        self.load_vad = load_silero_vad
        self.sessions: dict = {}                      # session id -> [model, last used]
        self.lock = threading.Lock()
        self.voices = None
        self.embed_lock = threading.Lock()

    def vad(self, sid: bytes, reset: bool, pcm: bytes) -> float:
        with self.lock:
            now = time.time()
            for k in [k for k, v in self.sessions.items() if now - v[1] > IDLE_S]:
                self.sessions.pop(k)
            if len(self.sessions) >= MAX_SESSIONS and sid not in self.sessions:
                self.sessions.pop(min(self.sessions, key=lambda k: self.sessions[k][1]))
            ent = self.sessions.get(sid)
            if ent is None:
                ent = self.sessions[sid] = [self.load_vad(), now]
            ent[1] = now
            model = ent[0]
        if reset:
            model.reset_states()
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        with self.torch.no_grad():
            return float(model(self.torch.from_numpy(a), 16000).item())

    def embed(self, pcm: bytes) -> bytes:
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(a) < 16000 * MIN_SECONDS:
            return b""
        with self.embed_lock:
            if self.voices is None:
                from speechbrain.inference.speaker import EncoderClassifier
                self.voices = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=os.path.join(HERE, "voice_models", "ecapa"),
                                                             run_opts={"device": "cpu"})
            with self.torch.no_grad():
                e = self.voices.encode_batch(self.torch.from_numpy(a)[None, :]).squeeze().cpu().numpy()
        n = float(np.linalg.norm(e))
        return (e / n).astype(np.float32).tobytes() if n else b""


async def handle(eng: Engines, pool: ThreadPoolExecutor, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    loop = asyncio.get_running_loop()
    wlock = asyncio.Lock()

    async def answer(rid: int, status: int, payload: bytes):
        async with wlock:
            writer.write(struct.pack(">IBI", rid, status, len(payload)) + payload)
            await writer.drain()

    async def work(op: int, rid: int, payload: bytes):
        try:
            if op == 1 and len(payload) == 9 + 1024:
                p = await loop.run_in_executor(pool, eng.vad, payload[:8], bool(payload[8] & 1), payload[9:])
                await answer(rid, 0, struct.pack(">f", p))
            elif op == 2:
                await answer(rid, 0, await loop.run_in_executor(pool, eng.embed, payload))
            elif op == 3:
                await answer(rid, 0, b"pong")
            else:
                await answer(rid, 1, b"bad request")
        except Exception as e:                                           # one broken request must not take the others down
            log(f"engine: op {op}: {e!r}")
            try:
                await answer(rid, 1, repr(e).encode()[:200])
            except Exception:
                pass

    try:
        while True:
            head = await reader.readexactly(9)
            op, rid, n = struct.unpack(">BII", head)
            if n > 8 * 1024 * 1024:
                break
            payload = await reader.readexactly(n)
            asyncio.get_running_loop().create_task(work(op, rid, payload))
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def main():
    eng = Engines()
    pool = ThreadPoolExecutor(max_workers=int(os.environ.get("LOSTOS_ENGINE_WORKERS", "3")))
    cb = lambda r, w: handle(eng, pool, r, w)
    if ADDR.startswith("/") or ADDR.startswith("unix:"):
        path = ADDR.replace("unix:", "")
        if os.path.exists(path):
            os.remove(path)
        server = await asyncio.start_unix_server(cb, path)
        os.chmod(path, 0o660)
    else:
        host, port = ADDR.rsplit(":", 1)
        server = await asyncio.start_server(cb, host, int(port))
    log(f"engine service on {ADDR}")
    await loop_forever(eng, pool)
    server.close()


async def loop_forever(eng: Engines, pool: ThreadPoolExecutor):
    # warm up: the first calls load the models, so the first person does not wait for them
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(pool, eng.vad, b"warmup00", True, bytes(1024))
    await loop.run_in_executor(pool, eng.embed, bytes(32000 * 2))
    log("engine service ready (VAD and the voice encoder are loaded)")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
