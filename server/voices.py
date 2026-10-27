"""Who is speaking: voice prints (SpeechBrain ECAPA-TDNN, 192 numbers per voice), kept on this PC in voices.json.
The assistant knows the people it was introduced to, addresses each in his own way (notes per person) and, within one
conversation, listens to the voice that woke her and ignores the others."""
import json
import os
import threading

import numpy as np

REMOTE = None            # the client of the shared engine service (engine_client.EngineClient): a tenant has no torch, the one service of the server turns sound into voice prints

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(os.environ.get("LOSTOS_DATA", HERE), "voices.json")        # a home PC keeps the prints in this file; a server with accounts keeps them in its database (attach)
KNOWN_T = float(os.environ.get("VOICE_THRESHOLD", "0.32"))   # cosine similarity to call a voice "known" (short phrases at night scored 0.30-0.35, other people up to 0.26)
SAME_T = float(os.environ.get("VOICE_SAME_THRESHOLD", "0.22"))   # ... to call it the same voice within a conversation (the owner scored 0.27 at night, other voices 0.04-0.24; "is it meant for her" is judged by the model as well)
MIN_SECONDS = 0.8


class Voices:
    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self.on_change = None                        # called after every change (the recognizer's name hints refresh)
        self.people: dict = {}
        self.store = None                            # a database that keeps the prints instead of the file (accounts.VoiceStore); every print has its "owner" (a Telegram id)
        try:
            self.people = json.load(open(FILE, encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def attach(self, store):
        """From now on the prints live in the database: what it has is used, what this process had (the old file) is moved into it once."""
        data = store.load()
        if data:
            self.people = data
        else:
            store.save(self.people)
        self.store = store
        self.on_change and self.on_change()

    def visible(self, tg_id: int, role: str) -> list:
        """The names a person may see and change: the owner of the server all of them, anybody else only his own."""
        return [n for n, p in self.people.items() if role == "owner" or p.get("owner") == tg_id]

    def load(self):
        if REMOTE is not None:
            return True
        if self._model is None:
            try:
                import torch
                from speechbrain.inference.speaker import EncoderClassifier
            except ImportError:                          # a lite server (a VDS) has no voice recognition: nobody is told apart, everything else works
                self._model = False
                return self._model
            self._model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb", savedir=os.path.join(HERE, "voice_models", "ecapa"),
                run_opts={"device": "cuda:0" if torch.cuda.is_available() else "cpu"})
        return self._model

    @staticmethod
    def trim(a):
        """Only the speech: the quiet frames before and after it are cut (they dilute the voice print of a short phrase)."""
        n = 320                                                  # 20 ms
        frames = len(a) // n
        if frames < 10:
            return a
        rms = np.sqrt((a[:frames * n].reshape(frames, n) ** 2).mean(1))
        loud = np.where(rms > max(float(rms.max()) * 0.12, 0.003))[0]
        if len(loud) == 0:
            return a
        return a[max(0, loud[0] - 5) * n:min(frames, loud[-1] + 6) * n]

    def embed(self, pcm: bytes, trim: bool = False):
        """Unit vector for the speech in `pcm` (s16le 16 kHz), None when there is too little of it."""
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if trim:
            a = self.trim(a)
        if len(a) < 16000 * MIN_SECONDS:
            return None
        if REMOTE is not None:                                   # the shared service (the same model for everybody)
            return REMOTE.embed((a * 32768.0).astype(np.int16).tobytes())
        import torch
        with self._lock, torch.no_grad():
            model = self.load()
            if not model:
                return None
            e = model.encode_batch(torch.from_numpy(a)[None, :].to(model.device)).squeeze().cpu().numpy()
        n = float(np.linalg.norm(e))
        return e / n if n else None

    @staticmethod
    def sim(a, b) -> float:
        return float(np.dot(a, b))

    def identify(self, emb):
        """(name, score) of the closest known voice, name is None when none is close enough."""
        best, score = None, -1.0
        for name, p in self.people.items():
            s = self.sim(emb, np.asarray(p["emb"], dtype=np.float32))
            if s > score:
                best, score = name, s
        return (best if score >= KNOWN_T else None), score

    def add(self, name: str, embs: list, owner: int = 0):
        v = np.mean(np.stack(embs), axis=0)
        v = v / np.linalg.norm(v)
        p = self.people.setdefault(name, {"note": "", "owner": owner})
        if "emb" in p:                                    # more samples of a known voice refine it
            old = np.asarray(p["emb"], dtype=np.float32)
            n = p.get("n", 1)
            v = (old * n + v * len(embs)) / (n + len(embs))
            v = v / np.linalg.norm(v)
            p["n"] = n + len(embs)
        else:
            p["n"] = len(embs)
        p["emb"] = [float(x) for x in v]
        self.save()

    def note(self, name: str, text: str):
        if name in self.people:
            old = self.people[name].get("note", "")
            self.people[name]["note"] = (old + " " + text).strip()[-600:]
            self.save()

    def rename(self, old: str, new: str):
        new = new.strip()
        if old in self.people and new and new not in self.people:
            self.people[new] = self.people.pop(old)
            self.save()

    def delete(self, name: str):
        if self.people.pop(name, None) is not None:
            self.save()

    def save(self):
        self.on_change and self.on_change()
        if self.store is not None:
            self.store.save(self.people)
        else:
            json.dump(self.people, open(FILE, "w", encoding="utf-8"), ensure_ascii=False)