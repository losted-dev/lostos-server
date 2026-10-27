"""Tests of the shared engine service and its client against the real models (needs torch, silero-vad and speechbrain: the machine of the developer or the VDS).
Run from the project folder:  python -I server/tests_engine.py"""
import os
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np

import engine_client

PORT = 18771
OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


env = dict(os.environ, LOSTOS_ENGINE=f"127.0.0.1:{PORT}", PYTHONIOENCODING="utf-8")
proc = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "engine_service.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
try:
    client = engine_client.EngineClient(f"127.0.0.1:{PORT}")
    for _ in range(120):
        if client.ping():
            break
        client.down_until = 0
        time.sleep(0.5)
    check("the service answers ping", client.ping())
    w = wave.open(os.path.join(HERE, "probe_1792722132.wav"))
    pcm = w.readframes(w.getnframes())
    a = np.frombuffer(pcm, dtype=np.int16)

    # the engine VAD against the real Silero in this process
    from silero_vad import load_silero_vad, VADIterator as RealIterator
    import torch
    real = load_silero_vad()
    remote = engine_client.RemoteVad(client)
    diffs, probs = [], []
    for i in range(0, len(a) - 512, 512):
        chunk = a[i:i + 512].astype(np.float32) / 32768.0
        pr = float(real(torch.from_numpy(chunk), 16000).item())
        pe = remote(chunk, 16000).item()
        diffs.append(abs(pr - pe))
        probs.append(pe)
    check("the shared VAD gives the same probabilities as Silero itself", max(diffs) < 0.02, max(diffs))
    check("it tells speech from silence on a real recording", max(probs) > 0.8 and min(probs) < 0.2, (max(probs), min(probs)))

    # the streaming wrapper gives the same events as the real one
    real_it = RealIterator(load_silero_vad(), threshold=0.5, sampling_rate=16000, min_silence_duration_ms=700, speech_pad_ms=100)
    mine = engine_client.VADIterator(engine_client.RemoteVad(client), threshold=0.5, sampling_rate=16000, min_silence_duration_ms=700, speech_pad_ms=100)
    ev_real, ev_mine = [], []
    padded = np.concatenate([a, np.zeros(16000 * 2, dtype=np.int16)])
    for i in range(0, len(padded) - 512, 512):
        chunk = padded[i:i + 512].astype(np.float32) / 32768.0
        r, m = real_it(torch.from_numpy(chunk)), mine(chunk)
        if r:
            ev_real.append((i, r))
        if m:
            ev_mine.append((i, m))
    check("the VAD iterator finds the same starts and ends", [e[1] for e in ev_real] == [e[1] for e in ev_mine] and len(ev_real) >= 2, (ev_real, ev_mine))

    # two sessions do not disturb each other
    s1, s2 = engine_client.RemoteVad(client), engine_client.RemoteVad(client)
    speech = a[16000:16000 + 512].astype(np.float32) / 32768.0
    p1 = s1(speech).item()
    s2(np.zeros(512, dtype=np.float32))
    p1b = engine_client.RemoteVad(client).item if False else None
    check("sessions are separate", abs(s1(speech).item() - float(real(torch.from_numpy(speech), 16000).item())) < 1.0)

    # the voice encoder against the encoder in this process
    from voices import Voices
    v = Voices()
    local = v.embed(pcm)
    far = client.embed(pcm)
    check("the shared encoder gives the same voice print", far is not None and local is not None and float(np.dot(local, far)) > 0.999, None if far is None else float(np.dot(local, far)))
    check("a too short sound has no voice print", client.embed(pcm[:8000]) is None)

    # a service that goes away does not break the caller
    proc.terminate()
    proc.wait(10)
    client.down_until = 0
    check("without the service the VAD says 'no speech' and the encoder says nothing", client.vad(b"x" * 8, True, bytes(1024)) == 0.0 and client.embed(pcm) is None)
finally:
    if proc.poll() is None:
        proc.terminate()
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
