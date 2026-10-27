"""Voice server for the Yandex Station running lostOS (no Home Assistant).

    speaker (lostOS: wake word "Алиса" + echo cancellation) --ESPHome API, 16 kHz audio--> this PC
    PC: end-of-speech detection -> Whisper (GPU) -> answer -> Silero TTS (GPU) -> wav over HTTP
    speaker plays the wav URL we hand back (TTS_END event).

The "brain" is pluggable: with ANTHROPIC_API_KEY set the answer comes from Claude, otherwise the server
just repeats what it heard (to test the whole chain and the voice).

    python voice_server.py [speaker_ip]
"""
import asyncio
import json
import threading
import collections
from weather import weather as get_weather
from voices import Voices, SAME_T
from reminders import Reminders
from homeassistant import HomeAssistant
from aioesphomeapi import VoiceAssistantTimerEventType as TE
from translit import latin_to_ru
import websearch
import brains
import io
import os
import random
import re
import socket
import sys
import time
import uuid
import wave

import numpy as np
TENANT = os.environ.get("LOSTOS_MODE") == "tenant"         # the server of ONE person behind the gateway (gateway.py): no torch, no login, the sound goes to the shared engine service
if TENANT:
    torch = None
else:
    import torch
from aiohttp import web
from aioesphomeapi import APIClient, VoiceAssistantEventType as Ev
from spotify_client import Spotify, SpotifyError

def load_env():
    """KEY=VALUE lines from .env next to this file (keeps the API key out of chats and shell history)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
LITE = os.environ.get("LOSTOS_LITE") == "1" or TENANT          # a server on a VDS: no GPU engines, TLS by Caddy, loopback only, the panel behind a login
PUBLIC_URL = os.environ.get("LOSTOS_PUBLIC_URL", "").rstrip("/")   # how the speaker and the browser reach this server from outside (lite mode)
SPEAKER = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LOSTOS_SPEAKER", "")   # lite mode: the station links itself (link.py), nothing to connect to yet
SPK = {"host": SPEAKER, "api": 6053, "airplay": 7000, "cover": 49153}   # where the speaker's ports are reached from here (lite server: loopback listeners of the station link)
LINK = None                                                            # the station link of a lite server (link.py)


ACCOUNTS = None                                                        # the accounts of a server with a login (accounts.py)


def household_owner() -> int:
    """The Telegram id of the owner (0 when there are no accounts): a voice taught by speaking to the speaker belongs to him."""
    if TENANT:
        return TENANT_ID                                      # a tenant is the household of one person
    o = ACCOUNTS.owner() if ACCOUNTS else None
    return o["tg_id"] if o else 0


class BotCtx:
    """What the Telegram bot (tgbot.py) may ask of the server: the player, the state of the speaker, the stations."""
    link = None

    async def media_command(self, cmd: str, args: dict):
        import media
        return await media.command(sys.modules[__name__], cmd, args)

    def speaker_online(self) -> bool:
        return PANEL["cli"] is not None

    def now_text(self) -> str:
        return NOWP["text"]
HTTP_PORT = int(os.environ.get("TTS_PORT", "8765"))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "8443"))        # the panel over TLS (a service worker and push on the iPhone need it); 0 = off
HTTPS = {"on": False, "port": 0}
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "openai/whisper-large-v3-turbo")
TTS_SPEAKER = os.environ.get("TTS_SPEAKER", "kseniya")      # silero v5_ru: aidar, baya, kseniya, xenia, eugene
TTS_RATE = 48000
SPEECH_RMS = float(os.environ.get("SPEECH_RMS", "1200"))     # per 32 ms window, int16 units
SILENCE_END_S = float(os.environ.get("SILENCE_END_S", "0.9"))
ANIM_LEAD_MS = int(os.environ.get("ANIM_LEAD_MS", "0"))   # shift the eyes if they lead/lag the voice
NO_SPEECH_S = 5.0                  # how long the speaker waits for a phrase (also for the follow-up)
MAX_S = 14.0
MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DEVICE = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"


def as_tensor(a):
    """What the VAD takes: a torch tensor, or in a tenant (no torch) the plain array."""
    return a if torch is None else torch.from_numpy(a)


ENGINE = None                                                 # the client of the shared engine service (a tenant only)
if TENANT:
    import engine_client
    import voices as _voices_module
    ENGINE = engine_client.EngineClient(log=lambda t: print(time.strftime("%H:%M:%S ") + t, flush=True))
    _voices_module.REMOTE = ENGINE
TENANT_ID = int(os.environ.get("LOSTOS_TENANT", "0") or 0)    # the Telegram id of the person this process serves
STATION_ID = os.environ.get("LOSTOS_STATION", "")            # the station of his household this process serves ("" = none yet: only the first-run wizard)
PRIMARY = os.environ.get("LOSTOS_PRIMARY", "1") != "0"        # the main station of the household: reminders that belong to no station ring here
TENANT_KEY = os.environ.get("LOSTOS_INTERNAL_KEY", "")        # what the gateway shows in every request: nobody else may talk to this process

tts_files: dict[str, bytes] = {}
REM_DIR = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or os.path.dirname(os.path.abspath(__file__)), "reminder_wavs")   # the phrases of the reminders (the speaker fetches its copy)
from spotify_client import _env as _envvar
PULSAR_KEY = _envvar("LOSTOS_API_KEY") or _envvar("PULSAR_API_KEY")
SPOT = Spotify(os.environ.get("SPEAKER_NAME", "Я.Станция Мини 3"))


LOG_RING = collections.deque(maxlen=300)     # for the web panel
LAST = {"t": "", "text": "", "pre": "", "who": "", "score": 0.0, "outcome": "", "tg": None}   # what she heard last and what became of it
ENROLL_PENDING = {"name": None, "owner": 0}
ENROLL_STATUS = {"state": "", "name": "", "step": 0, "need": 0, "error": "", "t": 0.0}      # what the panel shows while "remember my voice" runs: waiting | sample | done | error
SPK_CALLS: dict = {}                                    # what the panel may ask of the connected speaker (filled by run_speaker): "start_enroll"                # set from the panel: the next phrase starts "remember this voice"
DIALOG = collections.deque(maxlen=40)         # [{t, heard, reply}] for the web panel
PANEL = {"cli": None, "selects": {}, "switches": {}, "state": {}, "volume": None}
DISPLAY = {"key": None, "f": "", "t": 0.0, "subs": set()}          # the clock display of the speaker as it is lit right now (the sensor display_frame: 28 hex digits of segments, colon, degree LED)
PHASE = {"p": "idle", "until": 0.0}                 # what the voice assistant is doing right now (listen / think / speak), for the live model of the speaker in the panel
CLOCKTXT = {"text": "", "until": 0.0}                # the text the speaker shows on its clock display instead of the time (weather, an alarm time ...)


def log(*a):
    line = time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a)
    LOG_RING.append(line)
    print(line, flush=True)


# ---------------------------------------------------------------- speech to text
def load_stt():
    from transformers import pipeline
    t0 = time.time()
    pipe = pipeline("automatic-speech-recognition", model=WHISPER_MODEL,
                    torch_dtype=torch.float16 if DEVICE == "cuda" else torch.float32, device=DEVICE)
    log(f"whisper {WHISPER_MODEL} loaded on {DEVICE} in {time.time() - t0:.1f}s")
    return pipe


# Words Whisper tends to mangle ("выключи эйрплей" -> "виклище эйрплей"): a short vocabulary prompt steers it.
VOCAB = ("Алиса, поставь на паузу. Включи эйрплей. Выключи эйрплей. Поток звука, музыка с айфона. "
         "Следующий трек, предыдущий трек. Сделай громче, сделай тише.")
# The user's own music (written by spotify_vocab.py): names Whisper / Gemini should recognise.
MUSIC = {}


def load_music():
    """The person's own music names (his folder; a single-person server may still have the file next to the code). Updates MUSIC in place."""
    import json as _json
    import spotify_vocab
    for path in (spotify_vocab.FILE, os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_vocab.json")):
        try:
            MUSIC.clear()
            MUSIC.update(_json.load(open(path, encoding="utf-8")))
            return
        except (OSError, ValueError):
            continue
    MUSIC.clear()


load_music()
if MUSIC:
    VOCAB += " Включи " + ", ".join(MUSIC.get("artists", [])[:22]) + "."


async def music_refresher():
    """While the person is logged in to Spotify: collect his music into music_vocab.json at start and then every 6 hours, so that "Включи Ван Ти" is spelt from HIS library."""
    import spotify_vocab
    while True:
        try:
            stale = not os.path.exists(spotify_vocab.FILE) or time.time() - os.path.getmtime(spotify_vocab.FILE) > 5 * 3600
            if SPOT.available and stale:
                data = await spotify_vocab.collect(SPOT)
                if data["artists"] or data["tracks"]:
                    spotify_vocab.save(data)
                    load_music()
                    log(f"music vocabulary: {len(data['artists'])} artists, {len(data['tracks'])} tracks from his Spotify")
        except Exception as e:
            log(f"music vocabulary: {e!r}")
        await asyncio.sleep(60)                          # the vault (and with it the Spotify login) opens only when the speaker is connected
_PROMPT_IDS = None


def prompt_ids(pipe):
    global _PROMPT_IDS
    if _PROMPT_IDS is None:
        try:
            _PROMPT_IDS = pipe.tokenizer.get_prompt_ids(VOCAB + (" Меня зовут " + ", ".join(VOICES.people) + "." if VOICES.people else ""), return_tensors="pt").to(DEVICE)
        except Exception as e:
            log(f"whisper vocabulary prompt unavailable: {e!r}")
            _PROMPT_IDS = False
    return _PROMPT_IDS


import engines
import ai_settings
import shared
def stt_engine() -> str:
    return SETTINGS.get("stt_engine") or os.environ.get("STT_ENGINE", "yandex")      # yandex = the cloud recognizer of Alice, whisper = local (installed from the panel)


def tts_engine() -> str:
    return SETTINGS.get("tts_engine") or os.environ.get("TTS_ENGINE", "alice")       # alice = Alice's voice from the cloud, silero = local (installed from the panel)


def alice_voice() -> str:
    return SETTINGS.get("alice_voice") or os.environ.get("ALICE_VOICE", "shitova")   # shitova = Alice; also oksana, jane, zahar


SILERO_PATH = os.path.join(MODELS, "silero_v5_ru.pt")
def whisper_ok() -> bool:
    return engines.whisper_installed(WHISPER_MODEL)


def silero_ok() -> bool:
    return engines.silero_installed(SILERO_PATH)


YA_ASR_FAILS = {"n": 0, "until": 0.0}                       # after a failure the cloud is skipped for a while


class LazyStt:
    """Whisper is loaded on the first use: not installed or not chosen = no GPU memory taken."""
    def __init__(self):
        self.pipe = None
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.pipe is None:
                self.pipe = load_stt()
            return self.pipe


def alice_api_on() -> bool:
    """alice-api is the plugin that owns the cloud voice and ears of Alice (TTS and STT); the owner may switch it off."""
    return not PLUGINS or PLUGINS.service_on("alice-api")


def llm_on() -> bool:
    """The stock brain (Gemini / Claude of the owner's key) is a plugin that the owner may switch off; other brains (brain.replace) go on."""
    return not PLUGINS or PLUGINS.service_on("llm")


def plugin_engine(kind: str):
    """The plugin chosen as the recognizer / voice ("plugin:<id>" in the settings) when it is allowed and running, else None."""
    if not PLUGINS:
        return None
    engine = stt_engine() if kind == "stt" else tts_engine()
    return PLUGINS.engine_plugin(engine, "speech.stt" if kind == "stt" else "speech.tts")


def transcribe_by_plugin(plugin, pcm16: bytes):
    """-> the text (maybe empty: nothing heard) or None when the plugin failed: the cloud recognizer then takes over."""
    import base64
    try:
        t0 = time.time()
        res = PLUGINS.ask_sync(plugin, "stt.transcribe", {"pcm_b64": base64.b64encode(pcm16).decode(), "rate": 16000, "language": "ru"}, 20)
        text = str(res.get("text", "") if isinstance(res, dict) else res or "").strip()
        log(f"stt plugin {plugin.id}: {len(pcm16) / 32000:.1f} s of audio in {time.time() - t0:.2f} s")
        return text
    except Exception as e:
        log(f"stt plugin {plugin.id}: {e!r}")
        notify("warn", f"Распознавание «{plugin.m['name']}» не отвечает", "Подхватило облако Яндекса", cooldown=600)
        return None


def transcribe(stt, pcm16: bytes) -> str:
    plugin = plugin_engine("stt")
    if plugin:
        text = transcribe_by_plugin(plugin, pcm16)
        if text is not None:
            return text
    if (stt_engine() == "yandex" or stt_engine().startswith("plugin:")) and alice_api_on():     # a plugin that is gone or failed: the cloud recognizer
        if time.time() >= YA_ASR_FAILS["until"]:
            from yandex_asr import recognize
            t0 = time.time()
            text = recognize(pcm16)
            if text is not None:
                YA_ASR_FAILS["n"] = 0
                log(f"yandex asr: {len(pcm16) / 32000:.1f} s of audio in {time.time() - t0:.2f} s")
                return text.strip()
            YA_ASR_FAILS["n"] += 1
            YA_ASR_FAILS["until"] = time.time() + min(90, 10 * YA_ASR_FAILS["n"])
            notify("warn", "Облако распознавания не отвечает", "Whisper подхватит, если он установлен", cooldown=600)
            log(f"yandex asr failed ({YA_ASR_FAILS['n']}): " + ("Whisper" if whisper_ok() else "nothing else installed") + f" for the next {int(YA_ASR_FAILS['until'] - time.time())} s")
        if not whisper_ok():
            return ""                                        # the cloud is down and there is no local engine: nothing is heard
    elif not whisper_ok():
        log("nothing recognizes speech: " + ("alice-api is off and " if not alice_api_on() else "") + "Whisper is not installed")
        return ""
    pipe = stt.get()
    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    kwargs = {"language": "russian", "task": "transcribe"}
    ids = prompt_ids(pipe)
    if ids is not False and ids is not None:
        kwargs["prompt_ids"] = ids
    out = pipe({"raw": audio, "sampling_rate": 16000}, generate_kwargs=kwargs)
    return out["text"].strip()

# ---------------------------------------------------------------- text to speech
def load_tts():
    os.makedirs(MODELS, exist_ok=True)
    path = os.path.join(MODELS, "silero_v5_ru.pt")
    if not os.path.exists(path):
        log("downloading Silero v5_ru ...")
        torch.hub.download_url_to_file("https://models.silero.ai/models/tts/ru/v5_ru.pt", path)
    t0 = time.time()
    imp = torch.package.PackageImporter(path)
    model = imp.load_pickle("tts_models", "model")
    model.to(DEVICE)
    log(f"silero tts loaded in {time.time() - t0:.1f}s, speakers: {model.speakers}")
    return model


JINGLES = {}
for _k in ("home_on", "home_off", "home_change"):          # the stock Station jingles (Bluetooth on / off, the volume tick) for smart-home actions
    try:
        with wave.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "jingles", _k + ".wav")) as _w:
            JINGLES[_k] = np.frombuffer(_w.readframes(_w.getnframes()), dtype=np.int16)
    except (OSError, EOFError):
        pass


def make_chime(kind: str) -> np.ndarray:
    """Two soft notes, rising for "on", falling for "off" (int16 @ TTS_RATE)."""
    if kind in JINGLES:
        return JINGLES[kind]
    def note(freq: float, dur: float) -> np.ndarray:
        t = np.arange(int(dur * TTS_RATE)) / TTS_RATE
        env = np.exp(-t * 9.0) * (1.0 - np.exp(-t * 500.0))
        return env * (np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t))
    lo, hi = 659.25, 880.0
    a, b = (lo, hi) if kind == "on" else (hi, lo)
    first, second = note(a, 0.22), note(b, 0.40)
    out = np.zeros(int(0.12 * TTS_RATE) + len(second))
    out[:len(first)] += first
    out[int(0.12 * TTS_RATE):] += second
    out /= max(1e-6, float(np.abs(out).max()))
    return (out * 0.6 * 32767).astype(np.int16)


PENDING_AIR: dict = {}            # AirPlay on/off waits until the spoken confirmation has finished (stopping the
                                  # stack while the answer starts swallowed its first moments)
PENDING_CHIME: dict = {}          # set by the AirPlay on/off tool, consumed by the next synthesized answer


def spell_numbers(text: str) -> str:
    """Silero does not read digits: "15 градусов" -> "пятнадцать градусов"."""
    from num2words import num2words
    return re.sub(r"\d+", lambda m: num2words(int(m.group()), lang="ru"), text)


def tts_chunks(text: str, limit: int = 300):
    """Silero refuses long texts: sentences are packed into pieces of at most `limit` characters."""
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    piece = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        while len(s) > limit:                              # a very long sentence: cut at a comma or a space
            cut = max(s.rfind(", ", 0, limit), s.rfind(" ", 0, limit))
            cut = cut if cut > 40 else limit
            if piece:
                yield piece
                piece = ""
            yield s[:cut + 1].strip()
            s = s[cut + 1:].strip()
        if len(piece) + len(s) + 1 > limit and piece:
            yield piece
            piece = s
        else:
            piece = (piece + " " + s).strip()
    if piece:
        yield piece


def tts_long(model, text: str) -> np.ndarray:
    parts = []
    gap = np.zeros(int(0.12 * TTS_RATE), np.int16)
    for chunk in tts_chunks(text):
        try:
            audio = model.apply_tts(text=chunk, speaker=TTS_SPEAKER, sample_rate=TTS_RATE, put_accent=True, put_yo=True)
        except Exception as e:                             # one bad piece must not silence the whole answer
            log(f"tts piece skipped ({e}): {chunk[:50]!r}")
            continue
        pcm = (audio.cpu().numpy() * 32767).clip(-32768, 32767).astype(np.int16)
        k = min(len(pcm) // 4, int(0.01 * TTS_RATE))          # 10 ms fades: no clicks where the pieces meet
        if k > 1:
            ramp = np.linspace(0.0, 1.0, k)
            pcm = pcm.astype(np.float32)
            pcm[:k] *= ramp
            pcm[-k:] *= ramp[::-1]
            pcm = pcm.astype(np.int16)
        parts += [pcm, gap]
    return np.concatenate(parts) if parts else np.zeros(int(0.3 * TTS_RATE), np.int16)


ALICE_FAILS = {"n": 0, "until": 0.0}                        # after a failure the cloud is skipped for a while (no waiting on every phrase)


class LazyTts:
    """Silero is loaded on the first use; get() is None when it is not installed."""
    def __init__(self):
        self.model = None
        self.lock = threading.Lock()

    def get(self):
        if not silero_ok():
            return None
        with self.lock:
            if self.model is None:
                self.model = load_tts()
            return self.model


def alice_pcm(text: str):
    if not alice_api_on() or time.time() < ALICE_FAILS["until"]:
        return None
    from alice_tts import synth
    t0 = time.time()
    pcm = synth(latin_to_ru(text), TTS_RATE, alice_voice())
    if pcm is None or len(pcm) < TTS_RATE // 5:
        ALICE_FAILS["n"] += 1
        ALICE_FAILS["until"] = time.time() + min(120, 15 * ALICE_FAILS["n"])
        notify("warn", "Облако озвучки не отвечает", "Silero подхватит, если он установлен", cooldown=600)
        log(f"alice tts failed ({ALICE_FAILS['n']}): " + ("Silero" if silero_ok() else "no local voice installed") + f" for the next {int(ALICE_FAILS['until'] - time.time())} s")
        return None
    ALICE_FAILS["n"] = 0
    log(f"alice tts: {len(pcm) / TTS_RATE:.1f} s of speech in {time.time() - t0:.2f} s")
    return pcm


async def alice_api_tts(text: str) -> dict:
    """alice-api for the plugins that have the permission alice.api: a text -> the voice of Alice, {"pcm_b64", "rate"} (16-bit mono)."""
    import base64
    text = text.strip()[:1000]
    if not text:
        raise ValueError("text: пусто")
    pcm = await asyncio.get_running_loop().run_in_executor(None, alice_pcm, text)
    if pcm is None:
        raise RuntimeError("облако озвучки не отвечает")
    return {"pcm_b64": base64.b64encode(np.asarray(pcm, dtype=np.int16).tobytes()).decode(), "rate": TTS_RATE}


async def alice_api_stt(params: dict) -> dict:
    """alice-api for the plugins: a record of speech (pcm_b64, 16-bit mono 16 kHz, up to 30 s) -> {"text"}."""
    import base64
    try:
        pcm = base64.b64decode(str(params.get("pcm_b64", "")), validate=True)
    except ValueError:
        raise ValueError("pcm_b64: base64")
    if not 3200 <= len(pcm) <= 16000 * 2 * 30:
        raise ValueError("запись от 0,1 до 30 секунд, 16 кГц, 16 бит, моно")
    from yandex_asr import recognize
    text = await asyncio.get_running_loop().run_in_executor(None, recognize, pcm)
    if text is None:
        raise RuntimeError("облако распознавания не отвечает")
    return {"text": text.strip()}


def speech_from_plugin(res) -> "np.ndarray | None":
    """The plugin's voice as 16-bit mono at TTS_RATE: it returns {"wav_b64"} (any wav) or {"pcm_b64", "rate"} (16-bit mono)."""
    import base64
    if not isinstance(res, dict):
        return None
    if res.get("wav_b64"):
        with wave.open(io.BytesIO(base64.b64decode(res["wav_b64"])), "rb") as w:
            rate, ch, width, raw = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
        if width != 2:
            return None
        pcm = np.frombuffer(raw, dtype=np.int16)
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    elif res.get("pcm_b64"):
        pcm, rate = np.frombuffer(base64.b64decode(res["pcm_b64"]), dtype=np.int16), int(res.get("rate", 24000))
    else:
        return None
    if len(pcm) < rate // 10:
        return None
    if rate != TTS_RATE:
        try:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(TTS_RATE, rate)
            pcm = np.clip(resample_poly(pcm.astype(np.float32), TTS_RATE // g, rate // g), -32768, 32767).astype(np.int16)
        except ImportError:
            pcm = np.interp(np.linspace(0, len(pcm) - 1, int(len(pcm) * TTS_RATE / rate)), np.arange(len(pcm)), pcm).astype(np.int16)
    return pcm


def plugin_pcm(plugin, text: str):
    try:
        t0 = time.time()
        pcm = speech_from_plugin(PLUGINS.ask_sync(plugin, "tts.synthesize", {"text": text, "rate": TTS_RATE}, 25))
        if pcm is not None:
            log(f"tts plugin {plugin.id}: {len(pcm) / TTS_RATE:.1f} s of speech in {time.time() - t0:.2f} s")
            return pcm
        log(f"tts plugin {plugin.id}: no sound in the answer")
    except Exception as e:
        log(f"tts plugin {plugin.id}: {e!r}")
    notify("warn", f"Озвучка «{plugin.m['name']}» не отвечает", "Говорит голос Алисы из облака", cooldown=600)
    return None


def synthesize(tts, text: str, chime: str = None) -> bytes:
    plugin = plugin_engine("tts")
    if plugin:
        pcm = plugin_pcm(plugin, text)
        if pcm is None:
            pcm = alice_pcm(text)
    else:
        pcm = alice_pcm(text) if tts_engine() == "alice" or tts_engine().startswith("plugin:") else None   # a plugin voice that is gone: the cloud one
    if pcm is None:
        model = tts.get()
        if model is not None:
            pcm = tts_long(model, spell_numbers(latin_to_ru(text)))
        else:
            log("no voice available (the cloud is down and Silero is not installed)")
            pcm = np.zeros(int(0.3 * TTS_RATE), np.int16)    # silence around the speech: the amp wakes up / the pipeline closes right at the edges, which ate the
    # first and the last syllable
    head = [np.zeros(int(0.25 * TTS_RATE), np.int16)]
    if chime:
        head += [make_chime(chime), np.zeros(int(0.15 * TTS_RATE), np.int16)]
    pcm = np.concatenate(head + [pcm, np.zeros(int(0.5 * TTS_RATE), np.int16)])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TTS_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def envelope_hex(wav: bytes, lead_ms: int = 0) -> str:
    """Loudness of the answer, one byte per 40 ms, for the eyes on the display (speak:<hex>)."""
    pcm = np.frombuffer(wav[44:], dtype=np.int16).astype(np.float32)
    step = TTS_RATE * 40 // 1000
    n = len(pcm) // step
    if n == 0:
        return ""
    rms = np.sqrt((pcm[:n * step].reshape(n, step) ** 2).mean(axis=1))
    ref = max(float(np.percentile(rms, 95)), 1.0)
    env = np.clip(rms / ref, 0.0, 1.0)
    env = np.convolve(env, np.array([0.25, 0.5, 0.25]), mode="same")      # smooth: no flicker
    env = np.concatenate([np.zeros(lead_ms // 40), env])
    return (env * 255).astype(np.uint8)[:4000].tobytes().hex()


# ---------------------------------------------------------------- the brain
HISTORY: list[dict] = []          # last turns, forgotten after HISTORY_IDLE_S of silence
HISTORY_IDLE_S = 300
_last_turn = 0.0


DEFAULT_PROMPT = ("Ты голосовой ассистент на умной колонке. Отвечай по-русски, коротко и разговорно, "
                  "без списков, markdown и эмодзи. Числа и сокращения пиши словами.")
PERSONA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.txt")


WIFI = {"text": "", "key": None}   # lostOS text sensor "Wi-Fi status" (written by /data/lostos/wifi.sh status)
SPOT_NOW = {"text": "", "key": None}   # librespot's own "Artist - Title" (the Web API state lags and is sometimes stale)
SPOT_COVER = {"text": "", "key": None, "uri": "", "ms": 0, "pos": 0, "t": 0.0}   # the cover url of that track (a separate sensor of the speaker)
DEVPLUG = {"key": None}      # the sensor of the speaker with the state of its native plugins
SYSSTAT = {"key": None, "d": {}, "s": {}, "t": 0.0}   # what the speaker tells about itself: "d" numbers (load, temperature, memory, room), "s" texts (name, model, codename, versions)
EQ = {"key": None, "on": False, "pre": 0.0, "gains": [0.0] * 10, "known": False}   # the equalizer of the speaker (its eq.conf)
MEDIA = {"key": None, "data": {}, "t": 0.0, "pos": 0, "pos_t": 0.0, "raw_pos": None}   # the speaker's one JSON picture of the music (MediaHub)
PLAYBACK = {"key": None, "src": "", "pos": 0, "dur": 0, "t": 0.0}   # AirPlay / Bluetooth: where the track is (the speaker tells it at the moment of an event)
NOWP = {"text": "", "key": None}        # lostOS's readable "Now playing" sensor: "Artist — Title · Source[ · пауза]" / "Ничего не играет" (all sources, the one that counts)
AIR_NOW = {"text": "", "key": None}    # what the AirPlay sender plays (lostOS text sensor "AirPlay now playing")
BT = {"key": None, "on": None, "connected": False, "playing": False, "now": ""}   # lostOS text sensor "Bluetooth status"
PENDING_BT: dict = {}            # Bluetooth on/off waits until the spoken confirmation has finished
CONTEXT = {"line": ""}     # what is going on right now (time, music): refreshed before every answer


def media_update(text: str):
    """A new JSON picture from the speaker. The position it carries is authoritative only when it changed (the speaker publishes
    it on events: start, pause, seek); between events it is counted on from the clock, and frozen when the music stops."""
    try:
        d = json.loads(text)
    except ValueError:
        return
    now = time.time()
    prev = MEDIA["data"]
    same = prev and prev.get("source") == d.get("source") and prev.get("title") == d.get("title") and prev.get("artist") == d.get("artist")
    if same and MEDIA["raw_pos"] == d.get("pos") and prev.get("dur") == d.get("dur"):
        eff = MEDIA["pos"] + (int((now - MEDIA["pos_t"]) * 1000) if prev.get("playing") else 0)    # nothing new about the position
    else:
        eff = d.get("pos", 0)
    MEDIA.update(data=d, t=now, pos=eff, pos_t=now, raw_pos=d.get("pos"))


async def refresh_context():
    """Time of day and what plays on the speaker, so the assistant talks with the situation in mind."""
    import datetime
    now = datetime.datetime.now()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    part = ("ночь" if now.hour < 5 else "утро" if now.hour < 12 else "день" if now.hour < 18 else "вечер")
    on = lambda name: ai_settings.ctx_on(SETTINGS, name)
    line = f"Сейчас {now:%d.%m.%Y %H:%M}, {days[now.weekday()]}, {part}. " if on("time") else ""
    if SCEN.describe() and on("scenarios"):
        line += "Сценарии пользователя (запускаются инструментом run_scenario, когда человек просит такое или говорит похожую фразу): " + SCEN.describe() + ". "
    if REM.items and on("reminders"):
        line += "Стоят: " + "; ".join(REM.describe(i) for i in sorted(REM.items, key=lambda i: i["at"])[:6]) + ". Сколько осталось до будильника или таймера, бери только отсюда и от текущего времени выше, а не из своих прошлых ответов: время идёт. "
    if not on("playing"):
        pass
    elif SPOT.available and not AIR_NOW["text"] and not BT["playing"]:      # while AirPlay plays, Spotify's API state may be stale: only AirPlay counts
        try:
            st = await asyncio.wait_for(SPOT.state(), 2.0)
            item = (st or {}).get("item") or {}
            name = item.get("name")
            who = ", ".join(a["name"] for a in item.get("artists", [])[:2])
            if SPOT_NOW["text"]:                       # librespot knows better than the lagging Web API
                full = SPOT_NOW["text"].split("\t", 1)[0]
                who, _, name = full.partition(" - ")
                if not name:
                    name, who = full, ""
            here = ((st or {}).get("device") or {}).get("name", "").casefold() == SPOT.device_name.casefold()
            if name and here and st.get("is_playing"):
                line += f"На колонке сейчас играет Spotify: {name} — {who}. "
            elif AIR_NOW["text"]:
                pass                                   # AirPlay is the one that plays; Spotify's idle state is not news
            elif name and here:
                line += f"Spotify на колонке на паузе (был трек {name} — {who}). "
            else:
                line += "На колонке сейчас ничего не играет (но AirPlay-музыка с телефона могла бы идти отдельно). "
        except Exception:
            pass
    if not on("playing"):
        pass
    elif AIR_NOW["text"]:
        line += f"Сейчас на колонке играет музыка по AirPlay (с телефона или компьютера, это НЕ Spotify): {AIR_NOW['text']}. "
    if not on("playing"):
        pass
    elif BT["playing"]:
        line += ("Сейчас на колонке играет музыка по Bluetooth (телефон подключён к колонке напрямую, это НЕ Spotify и НЕ AirPlay)"
                 + (f": {BT['now']}" if BT["now"] else "") + ". ")
    elif BT["on"] and BT["connected"]:
        line += "Bluetooth включён, телефон подключён, но музыка по нему сейчас не играет. "
    elif BT["on"]:
        line += "Bluetooth на колонке включён, ждёт подключения телефона. "
    elif BT["on"] is False:
        line += "Bluetooth на колонке выключен. "
    CONTEXT["line"] = line
    if HA.configured:
        await HA.refresh()


def keep_suspect(pcm: bytes, text: str):
    """The recognizer sometimes answers one or two words for a phrase that lasted several seconds ("Включи." for "Включи Ван вэй тикет"). Such a phrase is kept (the last three, in the
    folder of this station) so that the cause can be looked at: is the cloud cutting it or is the recording short. Nothing else is ever recorded."""
    secs = len(pcm) / 32000
    if secs < 3.0 or len(text.split()) > 2:
        return
    try:
        folder = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or os.path.dirname(os.path.abspath(__file__)), *(["stations", STATION_ID] if STATION_ID else []), "suspect")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with wave.open(os.path.join(folder, stamp + ".wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        with open(os.path.join(folder, stamp + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"{secs:.1f} s of audio, recognized: {text!r}\n")
        for old in sorted(x for x in os.listdir(folder) if x.endswith(".wav"))[:-3]:
            for ext in (".wav", ".txt"):
                try:
                    os.remove(os.path.join(folder, old[:-4] + ext))
                except OSError:
                    pass
        log(f"a short answer ({len(text.split())} words) for {secs:.1f} s of speech: the recording is kept in {os.path.basename(folder)}/{stamp}.wav")
    except OSError as e:
        log(f"suspect recording: {e!r}")


def default_persona() -> str:
    try:
        return open(PERSONA_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""


def system_prompt() -> str:
    """The assistant's character: his own text from Panel > LLM > Настройки ИИ, else persona.txt (re-read on every answer: edit it any time). The blocks about his life are switched there too."""
    try:
        text = (ai_settings.cfg(SETTINGS)["persona"] or default_persona()).strip()
        if CONTEXT["line"] and text:
            text += "\nКонтекст: " + CONTEXT["line"]
        if HA.configured and text and ai_settings.ctx_on(SETTINGS, "home"):
            home = HA.context()
            if home:
                text += ("\nУмный дом (Home Assistant). Колонка стоит в комнате: " + (HA.room or "не задана") + ". Устройства по комнатам "
                         "(название [entity_id] состояние):\n" + home)
        if MUSIC and text and ai_settings.ctx_on(SETTINGS, "music"):
            text += ("\nПодсказка: что пользователь уже слушает (если услышанное СИЛЬНО похоже на это по звучанию, бери отсюда точное написание; но он может просить и любую другую музыку мира, которой тут нет - тогда ищи как обычно, не подгоняй под этот список): исполнители: "
                     + "; ".join(MUSIC.get("artists", [])) + ". Треки: " + "; ".join(MUSIC.get("tracks", [])) + ".")
        if text and STYLES.get(SETTINGS.get("answer_style", "")):
            text += chr(10) + STYLES[SETTINGS["answer_style"]]
        return text or DEFAULT_PROMPT
    except OSError:
        return DEFAULT_PROMPT

# Measured on the user's key: gemini-3.1-flash-lite answers in ~1.4 s; 3.8-flash / flash-latest hang for 25 s+,
# 3.5-flash is often overloaded, 2.5-flash is closed to new keys. Model names change: override in .env.
GEMINI_MODELS = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-flash-lite-latest", "gemini-3.1-flash-lite") if m]   # 3.1 kept timing out: it is the fallback
GEMINI_BEST = {"m": None}      # the model that answered last: a model that keeps timing out does not delay every answer


# What the assistant can do on the speaker. run_speaker() fills ACTIONS with callables bound to the live connection.
ACTIONS: dict = {}
PLUGINS = None            # the plugin manager (panel.py sets it when the panel starts)
TOOLS = [{"functionDeclarations": [
    {"name": "music_control",
     "description": "Управление музыкой, которая сейчас играет на колонке (Spotify, AirPlay или Bluetooth с телефона): пауза, "
                    "продолжить, следующий или предыдущий трек. Само выберет, чем управлять.",
     "parameters": {"type": "OBJECT", "properties": {"action": {
         "type": "STRING", "enum": ["pause", "play", "next", "previous"],
         "description": "pause - пауза, play - продолжить, next/previous - трек вперёд/назад"}}, "required": ["action"]}},
    {"name": "spotify_play",
     "description": "Найти в Spotify и включить на колонке трек, исполнителя, альбом или плейлист.",
     "parameters": {"type": "OBJECT", "properties": {
         "query": {"type": "STRING", "description": "что включить, например название песни, группа или плейлист"},
         "kind": {"type": "STRING", "enum": ["track", "artist", "album", "playlist", "library", "any"],
                  "description": "тип запроса, если понятен; library - когда просят «мою музыку» / «мои любимые» (query тогда пустой); playlist - плейлист пользователя или чужой; any - если неясно"},
         "variants": {"type": "ARRAY", "items": {"type": "STRING"},
                      "description": "2-4 другие вероятные написания того же (латиница/кириллица, цифры вместо букв, "
                                     "как услышано и как это может быть на самом деле), потому что распознавание речи часто путает редкие названия"}},
         "required": []}},
    {"name": "get_weather",
     "description": "Погода сейчас в городе пользователя (Яндекс Погода). Вызывай на любой вопрос о погоде, температуре, дожде, зонте, «что на улице».",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "home_control",
     "description": "Управление умным домом (Home Assistant): свет, подсветка, розетки, шторы, климат, телевизор, сцены. В блоке «Умный дом» перечислены устройства по комнатам с их entity_id. По умолчанию действуй ТОЛЬКО с устройствами комнаты колонки. Если подходящего устройства в её комнате нет, НЕ ищи в других комнатах: скажи, что в этой комнате такого нет, и спроси, можно ли в другой. Устройства другой комнаты можно трогать только если человек сам назвал эту комнату («свет в коридоре») или разрешил: тогда other_room_ok=true.",
     "parameters": {"type": "OBJECT", "properties": {
         "entity_ids": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "entity_id из блока «Умный дом»"},
         "service": {"type": "STRING", "enum": ["turn_on", "turn_off", "toggle", "open_cover", "close_cover", "stop_cover", "set_cover_position", "set_temperature", "set_hvac_mode", "volume_set", "media_play", "media_pause", "media_next_track", "media_previous_track"]},
         "brightness_pct": {"type": "NUMBER", "description": "яркость света 1-100"},
         "color": {"type": "STRING", "description": "цвет света: английское название (red, green, blue, orange, purple, pink, white, yellow, cyan) или R,G,B"},
         "color_temp_kelvin": {"type": "NUMBER", "description": "белый свет: 2000 тёплый - 6500 холодный"},
         "position": {"type": "NUMBER", "description": "шторы 0-100"},
         "temperature": {"type": "NUMBER"}, "hvac_mode": {"type": "STRING"}, "volume": {"type": "NUMBER", "description": "0..1"},
         "other_room_ok": {"type": "BOOLEAN", "description": "true только если человек назвал другую комнату или разрешил"}},
         "required": ["entity_ids", "service"]}},
    {"name": "set_timer",
     "description": "Поставить таймер на отсчёт времени («таймер на десять минут», «поставь таймер на полчаса, макароны»). Колонка покажет обратный отсчёт на часах и зазвонит. minutes и/или seconds - длительность, label - название (необязательно). Если длительность не названа, НЕ вызывай инструмент, а спроси «На сколько поставить таймер?».",
     "parameters": {"type": "OBJECT", "properties": {"minutes": {"type": "NUMBER"}, "seconds": {"type": "NUMBER"}, "label": {"type": "STRING"}}}},
    {"name": "set_alarm",
     "description": "Поставить будильник на время суток («разбуди меня в семь тридцать», «будильник на восемь утра по будням»). time - HH:MM в 24-часовом формате. date - YYYY-MM-DD, только если названа конкретная дата. repeat: once, daily, weekdays, weekends или days:пн,ср,пт. Если время не названо, НЕ вызывай инструмент, а спроси «На какое время поставить будильник?».",
     "parameters": {"type": "OBJECT", "properties": {"time": {"type": "STRING"}, "date": {"type": "STRING"}, "repeat": {"type": "STRING"}, "label": {"type": "STRING"}}, "required": ["time"]}},
    {"name": "set_reminder",
     "description": "Поставить напоминание: в нужный момент колонка сама скажет текст вслух («напомни через двадцать минут вынуть пиццу», «напомни завтра в девять позвонить маме»). text - что напомнить (от второго лица: «вынуть пиццу»). Либо in_minutes (через сколько минут), либо time HH:MM (+ date YYYY-MM-DD если не сегодня и не завтра). repeat как у будильника. Время обязательно: если человек не назвал, когда напомнить, НЕ вызывай инструмент и не выдумывай, а спроси «Когда напомнить?».",
     "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "in_minutes": {"type": "NUMBER"}, "time": {"type": "STRING"}, "date": {"type": "STRING"}, "repeat": {"type": "STRING"}}, "required": ["text"]}},
    {"name": "run_scenario",
     "description": "Запустить сценарий пользователя из списка в контексте («я дома», «включи режим кино», «спокойной ночи»). name - название сценария из контекста или его фраза. Если подходящего сценария в контексте нет, НЕ вызывай.",
     "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}},
    {"name": "list_reminders",
     "description": "Какие таймеры, будильники и напоминания сейчас стоят («какие у меня будильники», «что ты мне напомнишь»).",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "cancel_reminder",
     "description": "Отменить таймер, будильник или напоминание. kind: timer, alarm или reminder (необязательно). which: слово из названия или текста, либо «все».",
     "parameters": {"type": "OBJECT", "properties": {"kind": {"type": "STRING"}, "which": {"type": "STRING"}}}},
    {"name": "search_web",
     "description": "Поиск в интернете. Вызывай для фактов и всего, что нужно проверить или узнать: характеристики и цены, автомобили и тюнинг, прошивки и настройки, инструкции «как сделать», новости, рецепты, адреса, события. Не отказывай словами «я не могу искать в интернете»: ищи. Если ничего не нашлось, перефразируй запрос (в том числе по-английски) и вызови ещё раз.",
     "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING", "description": "поисковый запрос как для поисковика: ключевые слова с моделью, годом и точным вопросом (например «Skoda Octavia A7 1.8 TSI Stage 1 сколько прибавка мощности»)"}}, "required": ["query"]}},
    {"name": "enroll_voice",
     "description": "Запомнить голос человека («запомни мой голос», «я Настя, запомни меня»). name - как его зовут. После вызова попроси его сказать ещё две-три любые фразы подряд.",
     "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}},
    {"name": "remember_about_speaker",
     "description": "Запомнить факт о человеке, который сейчас с тобой говорит (как к нему обращаться, что любит, как с ним шутить). Только если голос знаком.",
     "parameters": {"type": "OBJECT", "properties": {"note": {"type": "STRING"}}, "required": ["note"]}},
    {"name": "forget_voice",
     "description": "Забыть голос и всё, что о человеке известно («забудь мой голос», «забудь Настю»).",
     "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}},
    {"name": "spotify_like",
     "description": "Поставить лайк (добавить в «Любимые треки» Spotify) или убрать лайк у трека, который играет сейчас. Вызывай на «лайкни», «добавь в любимые», «мне нравится этот трек», «убери лайк». Работает ТОЛЬКО для музыки из Spotify: треки из AirPlay и Bluetooth лайкнуть нельзя (у них нет сердечка), тогда не вызывай инструмент, а скажи об этом.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "enum": ["like", "unlike"]}}, "required": []}},
    {"name": "clock_equalizer",
     "description": "Визуализация трека (полоски или волна, раньше называли эквалайзером) на дисплее часов. mode: always - визуализация вместо часов всегда пока играет музыка; track_change - только 5 секунд при смене трека и старте музыки; off - выключить. style: wave (волна, по умолчанию) или bars (столбики спектра).",
     "parameters": {"type": "OBJECT", "properties": {
         "mode": {"type": "STRING", "enum": ["always", "track_change", "off"]},
         "style": {"type": "STRING", "enum": ["wave", "bars"]}}}},
    {"name": "airplay_control",
     "description": "Включить или выключить сам приём AirPlay на колонке. AirPlay пользователь может называть эйрплей, "
                    "поток звука, трансляция, музыка с айфона или телефона.",
     "parameters": {"type": "OBJECT", "properties": {"action": {
         "type": "STRING", "enum": ["on", "off"], "description": "on - включить AirPlay, off - выключить"}},
         "required": ["action"]}},    {"name": "bluetooth_control",
     "description": "Включить или выключить Bluetooth на колонке (телефон подключается к ней как к обычной Bluetooth-колонке; "
                    "называется так же, как AirPlay). Вызывай на «включи блютуз», «выключи блютус», «хочу подключить телефон по блютузу».",
     "parameters": {"type": "OBJECT", "properties": {"action": {
         "type": "STRING", "enum": ["on", "off"], "description": "on - включить Bluetooth, off - выключить"}},
         "required": ["action"]}},
    {"name": "set_volume",
     "description": "Громкость колонки. Либо точный уровень в процентах, либо шаг больше/меньше.",
     "parameters": {"type": "OBJECT", "properties": {
         "level": {"type": "INTEGER", "description": "точный уровень 0-100"},
         "change": {"type": "INTEGER", "description": "изменить на столько процентов, минус - тише"}}}},
]}]


# Commands that finish the exchange: the speaker does not listen for a follow-up after them.
FINAL_TOOLS = {"home_control", "run_scenario", "set_timer", "set_alarm", "set_reminder", "list_reminders", "cancel_reminder", "spotify_like", "clock_equalizer", "get_weather", "spotify_play", "music_control", "airplay_control", "bluetooth_control", "set_volume"}
TOOL_DONE = {"v": False}
state_services = {}      # lostOS's user services of the current connection
# The clock faces that look good on this display (the others - Surprised, Wink, Big Smile, Love, Robot, UwU, UwU thin,
# >w< - were drawn for stations with a better screen: not used for the reactions).
FACES = ["Smiley", "Sad", "Neutral", "Angry", "Sleepy", "Dead", "Cool", "UwU happy", "OwO"]
PENDING_DISPLAY = {}     # text to show on the clock once the answer has been spoken (the eyes would cover it)


# What a voice that is not in the list of known people may not do (when there are known people at all): it would change
# things that belong to them. Playing, pausing, volume, weather and chatting stay open to everybody.
RESTRICTED_TOOLS = {"spotify_like": "менять любимые треки", "forget_voice": "стирать голоса",
                    "clock_equalizer": "менять настройки дисплея"}


TIME_TOOLS = {"set_timer": "На сколько поставить таймер?", "set_alarm": "На какое время поставить будильник?",
              "set_reminder": "Когда напомнить?"}
# Words that carry a time or a duration: "через ...", "завтра", "в семь", "на десять минут", "утром", digits...
TIME_HINT = re.compile(
    r"\d|через|завтра|сегодня|послезавтра|\b(?:ноль|один|одна|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|"
    r"двенадцать|тринадцать|четырнадцать|пятнадцать|двадцать|тридцать|сорок|пятьдесят|шестьдесят|полтора|полтора|полчаса|полдень|полночь|"
    r"час\w*|минут\w*|секунд\w*|утр\w*|вечер\w*|ноч\w*|дн[её]м|обед\w*|недел\w*|понедельник\w*|вторник\w*|сред[ауы]\w*|четверг\w*|"
    r"пятниц\w*|суббот\w*|воскресень\w*|ежедневно|кажд\w+|будн\w*|выходн\w*|сейчас|сразу|скоро)\b", re.I)


def has_time_hint(text: str) -> bool:
    return bool(TIME_HINT.search(text or ""))


def tools_off() -> set:
    """The commands the owner switched off (wizard / Settings): the model neither sees them nor may run them."""
    return set(SETTINGS.get("tools_off", []))


STYLES = {"short": "Отвечай очень коротко: одна-две фразы, без вступлений.", "detailed": "Отвечай подробно, когда вопрос того требует: можно несколько предложений."}


def journal(text: str, tg=None):
    """A line in the personal journal of the person whose voice or hand it was (his logs page); a voice nobody knows belongs to the owner of the server."""
    if ACCOUNTS is None or not text:
        return
    ACCOUNTS.log(tg or LAST.get("tg") or household_owner(), "event", text[:190])


async def run_tool(name: str, args: dict, trusted: bool = False) -> str:
    res = await _run_tool(name, args, trusted)
    if not trusted and ACCOUNTS is not None and not name.startswith("_"):           # what she did on the speaker at the request of a voice
        import setup_wizard
        a = ", ".join(str(v) for v in (args or {}).values() if isinstance(v, (str, int, float)) and str(v))[:80]
        journal(f"команда «{setup_wizard.TITLES.get(name, name)}»" + (f": {a}" if a else ""))
    return res


async def _run_tool(name: str, args: dict, trusted: bool = False) -> str:
    if name in tools_off() and not trusted:
        log(f"{name}: switched off by the owner")
        return "эта команда отключена владельцем: ничего не делай, коротко скажи, что она выключена"
    if THINK["cancel"] and not trusted:
        log(f"{name}: not carried out, the owner cancelled")
        return "отменено владельцем: ничего не делай и ничего не говори, ответь РОВНО [IGNORE]"
    if name in TIME_TOOLS and not trusted and not has_time_hint(LAST.get("text", "")):
        # she tends to invent "in a minute": without a time in what was said she asks instead
        log(f"{name}: no time in {LAST.get('text', '')!r}, she asks")
        return f"время не названо: НЕ ставь ничего и не выдумывай время, спроси человека «{TIME_TOOLS[name]}» и дождись ответа"
    if name in RESTRICTED_TOOLS and not trusted and VOICES.people and WHO["name"] is None:
        log(f"refused for an unknown voice: {name}")
        return (f"отказано: незнакомый голос не может {RESTRICTED_TOOLS[name]}. Вежливо скажи это и предложи, что можно "
                "(музыка, громкость, погода), без нравоучений")
    if name in FINAL_TOOLS:
        TOOL_DONE["v"] = True
    fn = ACTIONS.get(name)
    if fn is None and PLUGINS and name in {d["name"] for _, d in PLUGINS.plugin_tools()}:       # a command that a plugin gave to the assistant
        if PLUGINS.is_restricted_tool(name) and not trusted and VOICES.people and WHO["name"] is None:
            log(f"refused for an unknown voice: {name}")
            return "отказано: незнакомый голос не может этого. Вежливо скажи и предложи, что можно (музыка, громкость, погода)"
        try:
            return await PLUGINS.run_plugin_tool(name, args)
        except Exception as e:
            log(f"plugin tool {name}{args}: {e!r}")
            return f"ошибка: {e}"
    if fn is None:
        return "инструмент недоступен: колонка не подключена"
    try:
        return await fn(**args)
    except Exception as e:
        log(f"tool {name}{args}: {e!r}")
        return f"ошибка: {e}"


async def ask_gemini(key: str) -> str:
    """Gemini, and once more after a moment when Google says "high demand" (503 comes at once, so the wait is short). A timeout is not retried: it already took the time."""
    try:
        return await _ask_gemini(key)
    except Exception as e:
        if "503" not in str(e) and "high demand" not in str(e):
            raise
        log("gemini: 503 high demand, one more try in 1.5 s")
        await asyncio.sleep(1.5)
        return await _ask_gemini(key)


async def _ask_gemini(key: str) -> str:
    import aiohttp
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                for m in HISTORY]
    last = None
    async with aiohttp.ClientSession() as s:
        chosen = ai_settings.model_of(SETTINGS, "gemini")                           # his own choice in the panel goes first, the usual ones are the fallback
        order = [chosen] + [x for x in GEMINI_MODELS if x != chosen] if chosen else sorted(GEMINI_MODELS, key=lambda m: m != GEMINI_BEST["m"])    # else the model that answered last time goes first
        for model in order:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            msgs = list(contents)
            try:
                for _ in range(3):                    # model -> tool -> model ... at most three rounds
                    body = {"systemInstruction": {"parts": [{"text": system_prompt()}]}, "contents": msgs,
                            "generationConfig": {"maxOutputTokens": int(ai_settings.cfg(SETTINGS)["max_tokens"]), "temperature": float(ai_settings.cfg(SETTINGS)["temperature"])}}
                    if ACTIONS and gemini_tools():
                        body["tools"] = gemini_tools()
                    async with s.post(url, params={"key": key}, json=body,
                                      timeout=aiohttp.ClientTimeout(total=8)) as r:   # a stuck model must not hold the answer for long (the one that answered last goes first)
                        data = await r.json()
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}: {str(data.get('error', {}).get('message', data))[:120]}")
                    parts = data["candidates"][0]["content"].get("parts", [])
                    calls = [p["functionCall"] for p in parts if "functionCall" in p]
                    if not calls:
                        text = "".join(p.get("text", "") for p in parts).strip()
                        if text:
                            GEMINI_BEST["m"] = model
                            if SETTINGS.get("gemini_best") != model:                  # remembered across restarts: the first answer after a start must not wait for a model that hangs
                                SETTINGS["gemini_best"] = model
                                save_settings()
                            return text
                        raise RuntimeError("empty answer")
                    msgs.append({"role": "model", "parts": parts})
                    results = []
                    for c in calls:
                        log(f"tool call: {c['name']}({c.get('args', {})})")
                        results.append({"functionResponse": {"name": c["name"], "response": {
                            "result": await run_tool(c["name"], c.get("args", {}))}}})
                    msgs.append({"role": "user", "parts": results})
                raise RuntimeError("too many tool rounds")
            except Exception as e:                    # try the next model
                last = e
                log(f"gemini {model}: {e!r}")
                journal(f"нейросеть Gemini ({model}): сбой, {e.__class__.__name__}")
    raise last


def builtin_tool_names() -> set:
    return {d["name"] for group in TOOLS for d in group["functionDeclarations"]}


def tools_neutral() -> list:
    """All commands of the assistant (built in and those that plugins added) as plain JSON-schema (lower-case types):
    this is the hub every brain works with, the built-in model and the brain plugins alike."""
    def lower(node):
        if isinstance(node, dict):
            return {k: (str(v).lower() if k == "type" and isinstance(v, str) else lower(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [lower(x) for x in node]
        return node
    off = tools_off()
    own = [lower(d) for group in TOOLS for d in group["functionDeclarations"] if d["name"] in ACTIONS and d["name"] not in off]
    added = [{k: d[k] for k in ("name", "description", "parameters")} for _, d in (PLUGINS.plugin_tools() if PLUGINS else []) if d["name"] not in off]
    return own + added


def gemini_tools() -> list:
    """The hub in Gemini's dialect: the built-in declarations plus the commands of the plugins (types in capitals)."""
    def upper(node):
        if isinstance(node, dict):
            return {k: (str(v).upper() if k == "type" and isinstance(v, str) else upper(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [upper(x) for x in node]
        return node
    off = tools_off()
    added = [upper({k: d[k] for k in ("name", "description", "parameters")}) for _, d in (PLUGINS.plugin_tools() if PLUGINS else []) if d["name"] not in off]
    decl = [d for d in TOOLS[0]["functionDeclarations"] if d["name"] not in off] + added
    return [{"functionDeclarations": decl}] if decl else []


async def ask_brain_plugin(brain, text: str, note: str, who: str) -> str:
    """The brain plugin (brain.replace) answers instead of the built-in model; it may run any command through tools.call meanwhile."""
    payload = {"text": text, "note": note, "who": who, "system": system_prompt(), "history": [dict(m) for m in HISTORY],
               "tools": tools_neutral()}
    res = await PLUGINS.ask(brain, "brain.respond", payload, 40)
    if isinstance(res, dict):
        res = res.get("reply")
    return str(res or "").strip()


async def ask_claude(key: str) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=key)
    c = ai_settings.cfg(SETTINGS)
    msg = await client.messages.create(model=ai_settings.model_of(SETTINGS, "claude", os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")),
                                       max_tokens=int(c["max_tokens"]), temperature=float(c["temperature"]), system=system_prompt(), messages=HISTORY)
    return "".join(b.text for b in msg.content if b.type == "text").strip()


async def ask_provider(provider: str) -> str:
    if provider == "gemini":
        return await ask_gemini(os.environ["GEMINI_API_KEY"])
    if provider == "claude":
        return await ask_claude(os.environ["ANTHROPIC_API_KEY"])
    return await brains.ask(sys.modules[__name__], provider)


async def english_query(q: str) -> str:
    """A search phrase in English (one cheap model call): for the queries a search engine answers with nothing."""
    import aiohttp
    key = os.environ.get("GEMINI_API_KEY", "") if llm_on() else ""
    if not key:
        return ""
    body = {"contents": [{"role": "user", "parts": [{"text": "Переведи поисковый запрос на английский, верни ТОЛЬКО запрос без кавычек: " + q}]}],
            "generationConfig": {"maxOutputTokens": 60, "temperature": 0}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        for model in GEMINI_MODELS:
            try:
                async with s.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  params={"key": key}, json=body) as r:
                    d = await r.json()
                    if r.status == 200:
                        return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
            except Exception:
                continue
    return ""


def barge_feed(data: bytes, on_barge):
    """Silero on the microphone stream while she talks: ~0.5 s of confident speech = somebody talks over her."""
    if not BARGE["on"]:
        return
    BARGE["buf"].extend(data)
    win = 512 * 2
    model = vad_model()
    while len(BARGE["buf"]) - BARGE["pos"] >= win:
        seg = np.frombuffer(BARGE["buf"][BARGE["pos"]:BARGE["pos"] + win], dtype=np.int16).astype(np.float32) / 32768.0
        BARGE["pos"] += win
        if time.time() - BARGE["t0"] < BARGE_DEAD_S:
            BARGE["run"] = 0
            continue
        p = float(model(as_tensor(seg), 16000).item())
        BARGE["run"] = BARGE["run"] + 1 if p > BARGE_PROB else 0
        # With a conversation running only the voice she talks with may interrupt her: a second of speech is enough for a
        # voice print. Other voices (people nearby, the TV) do not cut her short.
        check = SESSION["emb"] is not None and time.time() - SESSION["t"] < SESSION_S
        if BARGE["run"] >= (BARGE_VERIFY_RUN if check else BARGE_MIN_RUN):
            if check:
                end = BARGE["pos"]
                emb = VOICES.embed(bytes(BARGE["buf"][max(0, end - (BARGE["run"] + 6) * win):end]))
                same = float(np.dot(emb, SESSION["emb"])) if emb is not None else 1.0
                log(f"barge-in check: the voice is {'the same' if same >= BARGE_SAME_T else 'another one'} (similarity {same:.2f})")
                if same < BARGE_SAME_T:
                    BARGE["run"] = 0
                    continue
            BARGE["on"] = False
            on_barge()
            return
    if BARGE["pos"] > 128000:                          # do not let the buffer grow (the last two seconds stay for the voice print)
        cut = BARGE["pos"] - 64000
        del BARGE["buf"][:cut]
        BARGE["pos"] -= cut


THINK = {"on": False, "cancel": False, "buf": bytearray(), "pos": 0, "speech": bytearray(), "voiced": 0, "quiet": 0, "checking": False}
THINK_MIN_VOICED = 10      # windows of 32 ms of speech before a phrase is worth a look
THINK_END_QUIET = 14       # ~0.45 s of silence closes it
THINK_MAX_BYTES = 16000 * 2 * 8


def think_reset():
    THINK.update(on=True, cancel=False, pos=0, voiced=0, quiet=0, checking=False)
    THINK["buf"].clear()
    THINK["speech"].clear()
    vad_model().reset_states()


async def think_check(pcm: bytes):
    try:
        text = await asyncio.get_running_loop().run_in_executor(None, transcribe, GLOBALS["stt"], pcm)
        log(f"heard while she was thinking: {text!r}")
        if text and THINK["on"] and IGNORE_CMD_RE.search(text):
            THINK["cancel"] = True
            log("cancelled by the owner while she was thinking")
    except Exception as e:
        log(f"think check: {e!r}")
    finally:
        THINK["checking"] = False


def think_feed(data: bytes):
    """She is thinking and the microphone is open: a shout "игнорируй / заткнись, не отвечай" cancels the request."""
    if THINK["cancel"]:
        return
    THINK["buf"].extend(data)
    win = 512 * 2
    model = vad_model()
    while len(THINK["buf"]) - THINK["pos"] >= win:
        raw = bytes(THINK["buf"][THINK["pos"]:THINK["pos"] + win])
        THINK["pos"] += win
        p = float(model(as_tensor(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0), 16000).item())
        if p > 0.6:
            THINK["voiced"] += 1
            THINK["quiet"] = 0
            THINK["speech"].extend(raw)
        elif THINK["voiced"]:
            THINK["quiet"] += 1
            THINK["speech"].extend(raw)
            if THINK["quiet"] >= THINK_END_QUIET:
                if THINK["voiced"] >= THINK_MIN_VOICED and not THINK["checking"]:
                    THINK["checking"] = True
                    asyncio.get_running_loop().create_task(think_check(bytes(THINK["speech"])))
                THINK.update(voiced=0, quiet=0)
                THINK["speech"].clear()
        if len(THINK["speech"]) > THINK_MAX_BYTES:               # long talk, not a shout: forget it
            THINK.update(voiced=0, quiet=0)
            THINK["speech"].clear()
    if THINK["pos"] > 128000:
        cut = THINK["pos"] - 64000
        del THINK["buf"][:cut]
        THINK["pos"] -= cut


def said_so_far() -> str:
    """Roughly how much of the answer was spoken when she was cut short (by time: the speech is even)."""
    reply, secs = BARGE["reply"], max(BARGE["seconds"] - 0.8, 0.5)
    frac = min(1.0, max(0.0, (time.time() - BARGE["t0"] - 0.25) / secs))
    cut = reply[: int(len(reply) * frac)]
    return cut.rsplit(" ", 1)[0] if " " in cut and frac < 1.0 else cut


def rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def clean_context(text: str) -> str:
    """What was said before "Алиса": no phantoms, without the wake word at its end."""
    t = (text or "").strip()
    if not t or is_phantom(t):
        return ""
    t = re.sub(r"[\s,.!?:;—–-]*алис\w*[\s,.!?]*$", "", t, flags=re.I).strip()
    return t if len(re.findall(r"\w", t)) >= 3 else ""


def is_ignore(reply: str) -> bool:
    return reply.strip().upper().startswith(("[IGNORE", "IGNORE"))


async def think(text: str, pre: str = "", who_note: str = "", cont: bool = False) -> str:
    global _last_turn
    await refresh_context()
    stock = llm_on()                                         # the stock brain is a plugin that can be switched off
    provider = brains.pick(SETTINGS.get("brain", "")) if stock else ""      # the model that answers: the owner's choice, else the first that has a key
    brain = PLUGINS.brain() if PLUGINS else None             # a plugin with brain.replace thinks instead of Gemini
    if not stock and not brain:
        journal("нейросеть выключена, ответа нет")
        return "Мозг ассистента выключен. Включите его в плагинах или поставьте другой."
    if not provider and not brain:
        journal("нейросеть: нет ни одного ключа, колонка только повторяет сказанное")
        return f"Вы сказали: {text}"
    now = time.time()
    if now - _last_turn > HISTORY_IDLE_S:
        HISTORY.clear()
    _last_turn = now
    cut = ""
    if INTERRUPTED["t"] and time.time() - INTERRUPTED["t"] < 25:
        cut = (f"[Тебя только что перебили на полуслове. Ты говорила: «{INTERRUPTED['reply']}». Успела сказать примерно до: "
               f"«…{INTERRUPTED['said'][-60:]}». Человек перебил тебя и говорит то, что ниже. Не повторяй уже сказанное: ответь на "
               "его слова; если он просто просит замолчать, коротко согласись.]\n")
        INTERRUPTED["t"] = 0.0
    cont_note = ("[Эту фразу ты услышала БЕЗ слова «Алиса»: это продолжение диалога или перебивание, а рядом могут говорить люди между собой, по телефону, в игре или телевизор. Отвечай ТОЛЬКО если фраза ЯВНО обращена к тебе: ответ на твой вопрос, продолжение темы, которую ты только что обсуждала с этим человеком, просьба или вопрос тебе. ВАЖНО: если твоя последняя реплика была вопросом или предложением, то короткое «да», «нет», «не надо», «давай», «хорошо», «спасибо» сразу после неё это ответ ТЕБЕ, в том числе отказ: ответь одной короткой фразой («Хорошо.», «Окей.», «Поняла.», «Обращайся.») или выполни просьбу. Это НИКОГДА не [IGNORE]. Если человек говорит с кем-то другим, сам с собой, комментирует происходящее, ругается, рассуждает вслух или рассказывает историю без вопроса к тебе, ответь РОВНО [IGNORE]. При любом сомнении выбирай [IGNORE].]\n") if cont else ""
    note = cut + cont_note + (f"[{who_note}]\n" if who_note else "") + (f"[Незадолго до обращения рядом говорили (возможно, не тебе): «{pre}»]\n" if pre else "")
    HISTORY.append({"role": "user", "content": note + text})
    del HISTORY[:-12]
    try:
        reply = ""
        if brain:
            try:
                reply = await ask_brain_plugin(brain, text, note, WHO["name"] or "")
            except Exception as e:                  # a broken brain must not leave the speaker silent: the built-in model answers
                log(f"brain plugin {brain.id}: {e!r}")
                journal(f"нейросеть: плагин «{brain.m['name']}» не ответил, отвечает встроенная")
                notify("warn", f"Мозг «{brain.m['name']}» не отвечает", repr(e)[:120], cooldown=300)
        if not reply:
            if not provider:
                raise RuntimeError("empty answer")
            t_llm = time.time()
            try:
                reply = await ask_provider(provider)
            except Exception as first:                  # the chosen service is down: another one that has a key answers instead
                reply = ""
                for other in brains.ORDER:
                    if other == provider or not brains.key_of(other):
                        continue
                    try:
                        reply = await ask_provider(other)
                        log(f"{provider} failed ({first!r}); {other} answered instead")
                        journal(f"нейросеть {brains.PROVIDERS[provider]['title']} не ответила, ответила {brains.PROVIDERS[other]['title']}")
                        break
                    except Exception as e2:
                        log(f"{other}: {e2!r}")
                if not reply:
                    raise first
            journal(f"нейросеть {brains.PROVIDERS.get(provider, {}).get('title', provider)}: ответила за {time.time() - t_llm:.1f} с")
        elif brain:
            journal(f"нейросеть: ответил плагин «{brain.m['name']}»")
        if not reply:
            raise RuntimeError("empty answer")
    except Exception as e:                      # network / key / quota problems must not leave the speaker silent
        log(f"llm error: {e!r}")
        journal(f"нейросеть {brains.PROVIDERS.get(provider, {}).get('title', provider or '')}: не ответила, {str(e)[:100]}")
        notify("warn", "Нейросеть не отвечает", str(e)[:120], cooldown=300)
        HISTORY.pop()
        return "Не получилось связаться с нейросетью."
    if is_ignore(reply):                       # not meant for her: it leaves no trace in the conversation
        del HISTORY[-1:]
        return reply
    HISTORY.append({"role": "assistant", "content": reply})
    return reply


# ---------------------------------------------------------------- the dialog with the speaker
_VAD_MODEL = None


def vad_model():
    global _VAD_MODEL
    if _VAD_MODEL is None:
        if TENANT:
            import engine_client
            _VAD_MODEL = engine_client.RemoteVad(ENGINE)
        else:
            from silero_vad import load_silero_vad
            _VAD_MODEL = load_silero_vad()
    return _VAD_MODEL


REM = Reminders()
from scenarios import Scenarios
SCEN = Scenarios()
NOTIFS: list = []                  # the panel's notification centre (newest last): alarms, timers, scenarios, services that stopped answering
_NOTIF = {"seq": 0, "cool": {}}


def parse_now_source(text: str) -> str:
    """The source in the speaker's "Now playing" text ("Artist — Title · AirPlay · пауза")."""
    for part in (text or "").split(" · ")[1:]:
        if part in ("Spotify", "AirPlay", "Bluetooth"):
            return part
    return ""


def notify(kind: str, title: str, text: str = "", cooldown: float = 0.0):
    """kind: alarm | timer | reminder | scenario | warn. With a cooldown the same title is not repeated for that many seconds."""
    now = time.time()
    if cooldown and now - _NOTIF["cool"].get(title, 0) < cooldown:
        return
    _NOTIF["cool"][title] = now
    _NOTIF["seq"] += 1
    NOTIFS.append({"id": _NOTIF["seq"], "t": now, "kind": kind, "title": title, "text": text})
    del NOTIFS[:-60]
    try:
        import push                                  # the same event goes to the phones that subscribed (never blocks)
        push.send_async(title, text, tag=kind, log=log)
    except Exception:
        pass


SLEEP = {"task": None, "until": 0.0, "minutes": 0, "smooth": True}     # the sleep fade of the music (a scenario action or the panel's button)
HA = HomeAssistant()
RINGING = {"alarm": None}          # the alarm that is ringing now (to ring again if nobody stops it)
GLOBALS = {"tts": None, "base_url": ""}
VOICES = Voices()
VOICES.on_change = lambda: globals().update(_PROMPT_IDS=None)   # the name hints for Whisper follow the known people
SESSION = {"emb": None, "name": None, "t": 0.0, "turns": 0, "offered": False, "cont": 0}      # whom the running conversation is with (the voice that woke her)
WHO = {"name": None, "emb": None}                 # who said the phrase being answered (for the tools)
ENROLL = {"name": None, "embs": [], "need": 4, "owner": 0, "off": 1}        # "remember my voice": the next phrases are samples
ENROLL_PROMPTS = ["Сегодня отличная погода, давай включим что-нибудь весёлое",
                  "Расскажи мне что-нибудь интересное про космос и про море",
                  "Поставь будильник на семь утра и выключи свет в комнате"]   # varied sounds, ~3 s each
CONT_UNTIL = {"t": 0.0}      # until then a new dialog without "Алиса" is a follow-up / an interruption of her answer, not a new request
CONT_MAX = 4                 # so many such turns in a row, then she stops listening on her own (a question of hers keeps it open)
SESSION_S = 90                                        # a conversation is "running" this long after the last phrase

# ---- barge-in: somebody talks over her answer, she stops and listens (no "Алиса" needed)
SETTINGS_FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or os.path.dirname(os.path.abspath(__file__)), "settings.json")
SETTINGS = {"barge_in": True}
try:
    SETTINGS.update(json.load(open(SETTINGS_FILE, encoding="utf-8")))
except (OSError, ValueError):
    pass


def save_settings():
    json.dump(SETTINGS, open(SETTINGS_FILE, "w", encoding="utf-8"))
    shared.mark()


def reload_shared():
    """Another station of the house changed settings, scenarios, reminders or widgets: read them again (the settings in place, the others hold their own lists)."""
    new = json.load(open(SETTINGS_FILE, encoding="utf-8"))
    SETTINGS.clear()
    SETTINGS.update(new)
    for part in (REM, SCEN, globals().get("HOME_TILES")):
        try:
            if part is not None:
                part.reload()
        except (OSError, ValueError) as e:
            log(f"shared: {part.__class__.__name__}: {e!r}")


import weather as _weather
_weather.LOCATION = lambda: SETTINGS.get("weather")                 # the city that the owner chose in the panel


def city_timezone() -> str:
    """The IANA time zone of the owner's city ("Europe/Moscow"), "" until a city is chosen."""
    return (SETTINGS.get("weather") or {}).get("tz", "") or ""


def apply_timezone():
    """A server on the internet runs in UTC: alarms, reminders and the clock of the speaker follow the owner's city instead (Linux; the home PC has its own zone)."""
    tz = city_timezone()
    if tz and hasattr(time, "tzset") and re.fullmatch(r"[A-Za-z_]+(/[A-Za-z_+\-0-9]+){0,2}", tz):
        os.environ["TZ"] = tz
        time.tzset()
        log(f"time zone: {tz}")


async def timezone_changed():
    """The city changed: this process takes the new zone and the speaker asks again for the time (it gets the zone of the new connection)."""
    apply_timezone()
    cli = PANEL.get("cli")
    if cli is not None:
        asyncio.get_running_loop().create_task(cli.disconnect())


apply_timezone()

HEALTH = {"t": 0.0, "items": []}


async def health_check(force: bool = False) -> list:
    """One quick probe of every service the assistant lives on (the panel's «Состояние служб»); cached for 20 s."""
    if not force and time.time() - HEALTH["t"] < 20:
        return HEALTH["items"]
    import aiohttp
    loop = asyncio.get_running_loop()

    async def run(name, fn):
        t0 = time.time()
        try:
            ok, detail = await asyncio.wait_for(fn(), 14)
        except Exception as e:
            ok, detail = False, (str(e) or type(e).__name__)[:110]
        return {"name": name, "ok": ok, "detail": detail, "ms": int((time.time() - t0) * 1000)}

    async def speaker():
        return PANEL["cli"] is not None, "на связи" if PANEL["cli"] is not None else "нет соединения с колонкой"

    async def ya_tts():
        from alice_tts import synth
        pcm = await loop.run_in_executor(None, lambda: synth("проверка", 24000, alice_voice(), timeout=8))
        return pcm is not None and len(pcm) > 2000, "голос Алисы отвечает" if pcm is not None else "облако не отдало звук"

    async def ya_asr():
        from yandex_asr import recognize
        noise = (np.random.randn(16000) * 600).astype(np.int16).tobytes()
        res = await loop.run_in_executor(None, lambda: recognize(noise, 8.0))
        return res is not None, "распознавание отвечает" if res is not None else "облако не ответило"

    async def gemini():
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            return False, "ключ GEMINI_API_KEY не задан"
        model = GEMINI_BEST["m"] or GEMINI_MODELS[0]
        async with aiohttp.ClientSession() as s:
            async with s.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", params={"key": key},
                              json={"contents": [{"role": "user", "parts": [{"text": "ответь одним словом: ок"}]}], "generationConfig": {"maxOutputTokens": 8}},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                if r.status == 200:
                    return True, f"модель {model}"
                return False, str(data.get("error", {}).get("message", data))[:110]

    async def ha():
        if not HA.configured:
            return False, "HA_URL / HA_TOKEN не заданы в .env"
        await HA.refresh(force=True)
        return (not HA.error), (f"{len(HA.entities)} устройств" if not HA.error else HA.error)

    async def spotify():
        if not SPOT.available:
            return False, "Spotify не подключён"
        st = await asyncio.wait_for(SPOT.state(), 6)
        return True, "играет на колонке" if (st or {}).get("is_playing") else "отвечает"

    items = await asyncio.gather(run("Колонка", speaker), run("Облако Яндекса: озвучка", ya_tts), run("Облако Яндекса: распознавание", ya_asr),
                                 run("Gemini", gemini), run("Home Assistant", ha), run("Spotify", spotify))
    HEALTH.update(t=time.time(), items=items)
    return items

ENGINES = {}                       # {"stt": LazyStt, "tts": LazyTts} once the server runs
ALICE_VOICES = {"shitova": "Алиса", "oksana": "Оксана", "jane": "Джейн", "zahar": "Захар"}


def engines_state() -> dict:
    return {"stt": stt_engine(), "tts": tts_engine(), "voice": alice_voice(), "voices": ALICE_VOICES, "alice_api": alice_api_on(),
            "plugins": {"stt": PLUGINS.engine_plugins("speech.stt") if PLUGINS else [], "tts": PLUGINS.engine_plugins("speech.tts") if PLUGINS else []},
            "whisper": {"installed": whisper_ok(), **engines.STATE["whisper"], "model": WHISPER_MODEL, "blocked": engines.blocked()},
            "silero": {"installed": silero_ok(), **engines.STATE["silero"], "blocked": engines.blocked()}}


def engine_set(kind: str, value: str) -> str:
    """"ok" or the reason why not: a local engine cannot be chosen before it is installed."""
    if kind in ("stt", "tts") and value.startswith("plugin:"):
        if not (PLUGINS and PLUGINS.engine_plugin(value, "speech." + kind)):
            return "плагин не включён или ему не выданы права"
        SETTINGS[kind + "_engine"] = value
    elif kind == "stt" and value in ("yandex", "whisper"):
        if value == "whisper" and not whisper_ok():
            return "Whisper не установлен: сначала нажми «Установить»"
        SETTINGS["stt_engine"] = value
    elif kind == "tts" and value in ("alice", "silero"):
        if value == "silero" and not silero_ok():
            return "Silero не установлен: сначала нажми «Установить»"
        SETTINGS["tts_engine"] = value
    elif kind == "voice" and value in ALICE_VOICES:
        SETTINGS["alice_voice"] = value
    else:
        return "неизвестный выбор"
    save_settings()
    st = ENGINES.get("stt")
    if kind == "stt" and value == "whisper" and st is not None:       # load the model now: the first phrase does not wait for it
        threading.Thread(target=st.get, daemon=True).start()
    log(f"engine {kind} -> {value}")
    return "ok"


def engine_install(name: str) -> str:
    return engines.install(name, WHISPER_MODEL, SILERO_PATH, log)


BARGE = {"on": False, "t0": 0.0, "seconds": 0.0, "reply": "", "buf": bytearray(), "run": 0, "pos": 0}
INTERRUPTED = {"reply": "", "said": "", "t": 0.0}      # what she was saying when she was cut short
BARGE_MIN_RUN = 16                                     # consecutive speech windows of 32 ms (~0.5 s) to call it an interruption
BARGE_VERIFY_RUN = 34                                  # ~1.1 s of speech: long enough to check whose voice it is before she is cut short
BARGE_SAME_T = float(os.environ.get("BARGE_SAME_THRESHOLD", "0.22"))   # a second of speech over her own voice scores lower than a phrase: measured 0.36 for the owner, 0.04-0.18 for others
BARGE_PROB = 0.8
BARGE_DEAD_S = 0.9                                     # the start of her own answer: echo, the player is starting

PRE_BYTES = 16000 * 5 * 2      # lostOS sends exactly 5 s (s16le, 16 kHz) of context before every dialog


class Dialog:
    """One utterance: collects audio, decides when the user stopped talking (Silero VAD)."""

    def __init__(self):
        if TENANT:
            from engine_client import VADIterator
        else:
            from silero_vad import VADIterator
        self.audio = bytearray()
        self.pre = bytearray()             # the 5 s lostOS sends first: what was said up to the wake word
        self.pre_left = PRE_BYTES
        self.t0 = time.time()
        self.spoke = False
        self.pos = 0                       # bytes already analysed
        self.done = asyncio.Event()
        self.vad = VADIterator(vad_model(), threshold=0.5, sampling_rate=16000,
                               min_silence_duration_ms=int(SILENCE_END_S * 1000), speech_pad_ms=100)
        self.vad.reset_states()

    def feed(self, data: bytes):
        if self.pre_left > 0:               # the context in front of the request is kept apart from the request
            take = min(len(data), self.pre_left)
            self.pre.extend(data[:take])
            self.pre_left -= take
            data = data[take:]
            if not data:
                return
        self.audio.extend(data)
        win = 512 * 2                      # Silero wants exactly 512 samples @ 16 kHz
        while len(self.audio) - self.pos >= win:
            seg = np.frombuffer(self.audio[self.pos:self.pos + win], dtype=np.int16).astype(np.float32) / 32768.0
            self.pos += win
            ev = self.vad(as_tensor(seg))
            if ev and "start" in ev and not self.spoke:
                self.spoke = True
                log("speech started")
            if ev and "end" in ev and self.spoke:
                log(f"speech ended after {time.time() - self.t0:.1f}s")
                self.done.set()
        now = time.time()
        if not self.spoke and now - self.t0 > NO_SPEECH_S:
            self.done.set()
        elif now - self.t0 > MAX_S:
            self.done.set()


STOP_RE = re.compile(r"(стоп|хватит|отмена|достаточно|спасибо|благодарю|пока|до свидания|ничего не надо)", re.I)


def wants_more(text: str) -> bool:
    """False for 'спасибо', 'хватит' ...: the dialog ends instead of waiting for another phrase."""
    return not (STOP_RE.search(text) and len(text.split()) <= 5)


# "turn AirPlay off/on" is handled without the language model: it is instant, never misses a tool call, and the
# speech recognizer garbles the unusual word "AirPlay", so the rule is deliberately loose (verb stem + any name).
AIR_NAMES = (r"(эйр\s?плей|аир\s?плей|air\s?play|эйр|поток\w* звук\w*|поток\b|трансляци\w*|"
             r"музык\w* с (айфон|телефон|айпад|мак)\w*|звук\w* с (айфон|телефон|айпад)\w*|при[её]м музык\w*)")
OFF_VERBS = r"(выкл|викл|выруб|отключ|отруб|останов|прекрат|убери)\w*"
ON_VERBS = r"(вкл|влюч|включ|запуст|подключ|активир)\w*"
AIR_OFF_RE = re.compile(f"{OFF_VERBS}.*{AIR_NAMES}|{AIR_NAMES}.*{OFF_VERBS}", re.I)
AIR_ON_RE = re.compile(f"{ON_VERBS}.*{AIR_NAMES}|{AIR_NAMES}.*{ON_VERBS}", re.I)
AIR_REPLIES = {"off": ["Выключила.", "Готово, отключила.", "Всё, AirPlay выключен."],
               "on": ["Включаю.", "Включила, подключайся с телефона.", "Готово, жду музыку."]}


# What Whisper "hears" in silence and noise: credits of subtitles it was trained on. Never a real request.
PHANTOMS = ("субтитр", "редактор", "корректор", "продолжение следует", "спасибо за просмотр", "спасибо за внимание",
            "подписывайтесь", "подписывайся", "ставьте лайк", "до новых встреч", "до свидания", "dimatorzok", "amara.org",
            "семкин", "егорова", "поддержите канал", "перевод", "озвучка", "звучит музыка", "музыкальная заставка",
            "аплодисменты", "смех в зале", "всем пока", "субтитры сделал", "субтитры создавал")


def is_phantom(text: str) -> bool:
    t = text.casefold().strip()
    if not re.search(r"[a-zа-яё0-9]", t):
        return True
    words = re.findall(r"\w+", t)
    if len(words) >= 5 and len(set(words)) * 5 <= len(words):    # "Морри. Морри. Морри. ...": Whisper loops a word of its own prompt on noise
        return True
    if any(p in t for p in PHANTOMS):
        # a real request that merely contains such a word is long and has a verb; the phantoms are short credits
        return len(t.split()) <= 8
    return False


BT_NAMES = r"(блю\s?т[уо][зсф]\w*|блю\s?тус\w*|bluetooth|blue\s?tooth)"
BT_OFF_RE = re.compile(f"{OFF_VERBS}.*{BT_NAMES}|{BT_NAMES}.*{OFF_VERBS}", re.I)
BT_ON_RE = re.compile(f"{ON_VERBS}.*{BT_NAMES}|{BT_NAMES}.*{ON_VERBS}", re.I)
BT_REPLIES = {"off": ["Выключила.", "Готово, блютуз выключен."], "on": ["Включаю.", "Готово, можно подключаться."]}


def bt_intent(text: str):
    if BT_OFF_RE.search(text):
        return "off"
    if BT_ON_RE.search(text):
        return "on"
    return None


# "игнорируй" / "заткнись, не отвечай, не делай": the speaker hears a video or the TV say "Алиса, включи музыку" and the owner
# shouts at it. That cancels the request: nothing is carried out, nothing is answered, and the music that this request has just
# started is paused again. While she thinks the microphone stays open for exactly this (think_feed below).
IGNORE_CMD_RE = re.compile(r"игнор\w*|не\s+реагир\w*|не\s+отвечай|не\s+делай|заткни\w*", re.I)
ACTED = {"music_t": 0.0}      # when she last started music by a request (what "не делай" can still undo)
UNDO_S = 25


async def undo_recent():
    """The music that the cancelled request has just started is paused again."""
    if time.time() - ACTED["music_t"] < UNDO_S:
        ACTED["music_t"] = 0.0
        log("cancelled: the music she has just started is paused")
        await run_tool("music_control", {"action": "pause"}, trusted=True)


def air_intent(text: str):
    if AIR_OFF_RE.search(text):
        return "off"
    if AIR_ON_RE.search(text):
        return "on"
    return None


async def serve_dialog(cli, d: Dialog, stt, tts, base_url: str, face):
    send = cli.send_voice_assistant_event
    PENDING_AIR.clear()
    PENDING_BT.clear()
    TOOL_DONE["v"] = False
    send(Ev.VOICE_ASSISTANT_RUN_START, {"url": ""})
    send(Ev.VOICE_ASSISTANT_STT_START, None)
    await face("listen", 12)                       # the breathing line on the display
    try:
        await asyncio.wait_for(d.done.wait(), timeout=MAX_S + 2)
    except asyncio.TimeoutError:
        pass
    send(Ev.VOICE_ASSISTANT_STT_VAD_END, None)
    if d.spoke:
        await face("think", 25)                   # the sweeping highlight while it thinks

    async def quiet_end(why: str):
        log(why)
        LAST["outcome"] = why
        await face("off", 0)
        send(Ev.VOICE_ASSISTANT_RUN_END, None)      # no error flash: silence after a reply is normal

    if not d.spoke:
        return await quiet_end("no speech heard")
    t0 = time.time()
    loop = asyncio.get_running_loop()
    pre_audio = bytes(d.pre)
    pre_job = None
    if len(pre_audio) >= PRE_BYTES and rms(pre_audio) > 200:
        pre_job = loop.run_in_executor(None, transcribe, stt, pre_audio)
    text = await loop.run_in_executor(None, transcribe, stt, bytes(d.audio))
    pre_text = ""
    if pre_job is not None:
        pre_text = clean_context(await pre_job)
    log(f"heard ({time.time() - t0:.1f}s): {text!r}" + (f"  [before the wake word: {pre_text!r}]" if pre_text else ""))
    keep_suspect(bytes(d.audio), text)
    LAST.update(t=time.strftime("%H:%M:%S"), text=text, pre=pre_text, who="", score=0.0, outcome="…")
    if not text:
        return await quiet_end("empty transcript")
    if is_phantom(text):
        return await quiet_end(f"ignored a Whisper phantom: {text!r}")
    if IGNORE_CMD_RE.search(text):                                # "игнорируй, не делай": nothing of this phrase is carried out
        log(f"cancelled by the owner: {text!r}")
        await undo_recent()
        try:
            if state_services.get("clock_show_face"):             # silent: only a sleeping face for a moment
                await cli.execute_service(state_services["clock_show_face"], {"face": "Sleepy", "seconds": 2})
        except Exception as e:
            log(f"cancel face: {e!r}")
        return await quiet_end("cancelled by the owner")
    # ---- who is speaking: one voice per conversation, and the known people by name
    wake_start = rms(pre_audio) > 1                        # silence-only context = a button or a continued conversation
    cont = (not wake_start) and d.t0 < CONT_UNTIL["t"]          # a follow-up or an interruption, spoken without "Алиса"
    SESSION["cont"] = SESSION.get("cont", 0) + 1 if cont else 0
    emb = await loop.run_in_executor(None, VOICES.embed, bytes(d.audio))
    emb_t = await loop.run_in_executor(None, VOICES.embed, bytes(d.audio), True) if emb is not None else None   # the speech alone
    who, score = None, 0.0
    if emb is not None:
        if VOICES.people:
            who, score = VOICES.identify(emb)
            if emb_t is not None:                                  # the better of the two views counts
                who_t, score_t = VOICES.identify(emb_t)
                log(f"voice scores: whole {score:.2f}, speech only {score_t:.2f}")
                if score_t > score:
                    who, score = who_t, score_t
        now = time.time()
        if SESSION["emb"] is not None and not wake_start and now - SESSION["t"] < SESSION_S:
            same = float(np.dot(emb, SESSION["emb"]))
            if emb_t is not None:
                same = max(same, float(np.dot(emb_t, SESSION["emb"])))
            if same < SAME_T:
                log(f"another voice (similarity {same:.2f} < {SAME_T}), ignored: {text!r}")
                return await quiet_end("a different voice than the one she talks with")
            mix = SESSION["emb"] * 0.7 + emb * 0.3
            SESSION["emb"] = mix / np.linalg.norm(mix)
            if not who and SESSION["name"]:                       # the same voice as the one that was recognised at the start
                who = SESSION["name"]
        else:
            SESSION.update(emb=emb, name=who, turns=0, offered=False)
        SESSION["turns"] += 1
        SESSION["t"] = now
        SESSION["name"] = who or SESSION["name"]
        log(f"voice: {who or 'unknown'} ({score:.2f})")
    WHO.update(name=who, emb=emb)
    LAST.update(who=who or ("unknown" if emb is not None else ""), score=round(float(score), 2), tg=(VOICES.people.get(who) or {}).get("owner") if who else None)
    if ENROLL_PENDING["name"] and emb is not None and not ENROLL["name"] and wake_start:    # started from the panel
        ENROLL.update(name=ENROLL_PENDING["name"], embs=[emb], need=4, owner=ENROLL_PENDING.get("owner", 0), off=1)
        ENROLL_PENDING["name"] = None
        enroll_first = True
    else:
        enroll_first = False
    send(Ev.VOICE_ASSISTANT_STT_END, {"text": text})
    send(Ev.VOICE_ASSISTANT_INTENT_START, None)
    bt_i = bt_intent(text)
    intent = None if bt_i else air_intent(text)
    taken = None                                            # a plugin with assistant.intercept may answer the phrase itself
    if PLUGINS and not enroll_first and not ENROLL["name"]:
        taken = await PLUGINS.intercept(text, who or "")
    if taken is not None:
        reply = taken or "Хорошо."
    elif enroll_first:
        reply = f"Хорошо, {ENROLL['name']}, запоминаю. Повтори за мной: {ENROLL_PROMPTS[0]}"
    elif ENROLL["name"] and emb is not None:               # "remember my voice": this phrase is a sample, no chat
        if len(d.audio) < 16000 * 2 * 1.8:                 # (the 0.9 s of silence at its end counts too)
            reply = "Слишком коротко, скажи фразу целиком."
        else:
            ENROLL["embs"].append(emb)
            ENROLL_STATUS.update(state="sample", step=len(ENROLL["embs"]), t=time.time())
            if len(ENROLL["embs"]) >= ENROLL["need"]:
                VOICES.add(ENROLL["name"], ENROLL["embs"], ENROLL.get("owner", 0))
                reply = f"Готово, запомнила твой голос, {ENROLL['name']}"
                log(f"voice enrolled: {ENROLL['name']} ({len(ENROLL['embs'])} samples)")
                ENROLL_STATUS.update(state="done", t=time.time())
                ENROLL_STATUS["end"] = True                   # the dialog ends after "Готово": no follow-up is awaited
                ENROLL.update(name=None, embs=[])
            else:
                reply = "Хорошо. Теперь скажи: " + ENROLL_PROMPTS[len(ENROLL["embs"]) - ENROLL.get("off", 1)]
    elif bt_i:
        log(f"quick intent: bluetooth {bt_i}")
        res = await run_tool("bluetooth_control", {"action": bt_i})
        reply = ("Он уже " + ("включён." if bt_i == "on" else "выключен.")) if "уже" in str(res) else random.choice(BT_REPLIES[bt_i])
    elif intent:
        log(f"quick intent: airplay {intent}")
        await run_tool("airplay_control", {"action": intent})
        reply = random.choice(AIR_REPLIES[intent])
    else:
        who_note = ""
        if VOICES.people:
            if who:
                who_note = f"Сейчас говорит: {who}."
                note = VOICES.people[who].get("note", "")
                if note:
                    who_note += f" Что ты знаешь о нём/ней: {note}"
            elif SESSION["turns"] >= 4 and not SESSION["offered"]:    # a long talk: once, softly
                SESSION["offered"] = True
                who_note = ("Говорит незнакомый тебе человек. Диалог с ним уже долгий: ОДИН раз, к месту и мягко, можешь предложить "
                            "запомнить его голос (не настаивай, если откажется).")
            else:
                who_note = ("Говорит незнакомый тебе человек. Не предлагай запомнить голос и не спрашивай имя, пока он сам не "
                            "попросит; просто общайся.")
        think_reset()
        text = SCEN.unmask(text)
        try:
            reply = await think(text, pre_text, who_note, cont)
        finally:
            THINK["on"] = False
        if THINK["cancel"]:                                       # shouted at while she was thinking
            await undo_recent()
            return await quiet_end("cancelled by the owner while she was thinking")
    if is_ignore(reply):
        log(f"not addressed to her, ignored: {text!r}")
        return await quiet_end("speech about her, not to her")
    mood = None
    mm = re.match(r"\s*\[\s*([A-Za-z>][A-Za-z> ]{1,14})\s*\]\s*", reply)       # "[Smiley] Привет": the face of the reaction
    if mm:
        mood = next((f for f in FACES if f.lower() == mm.group(1).strip().lower()), None)
        reply = reply[mm.end():]
    LAST["outcome"] = "ответила"
    log(f"reply: {reply!r}" + (f" (face {mood})" if mood else ""))
    DIALOG.append({"t": time.strftime("%H:%M:%S"), "heard": text, "reply": reply, "tg": LAST.get("tg")})
    journal(f"услышала: {text[:80]} → {reply[:80]}")           # the journal of the person whose voice it was (the logs page shows everybody only his own)
    more = wants_more(text) and (not TOOL_DONE["v"] or reply.rstrip().endswith("?"))     # a question of hers is always answered
    if ENROLL["name"]:
        more = True                                           # she waits for the next phrase of the training
    elif ENROLL_STATUS.pop("end", False):
        more = False
    capped = SESSION["cont"] >= CONT_MAX and not reply.rstrip().endswith("?") and not ENROLL["name"]
    if more and capped:
        more = False
        log(f"{SESSION['cont']} turns in a row without the wake word: she stops listening for a follow-up")
    send(Ev.VOICE_ASSISTANT_INTENT_END,
         {"conversation_response": reply, "continue_conversation": "1" if more else "0"})
    send(Ev.VOICE_ASSISTANT_TTS_START, {"text": reply})
    t0 = time.time()
    chime = PENDING_CHIME.pop("kind", None)
    wav = await asyncio.get_running_loop().run_in_executor(None, synthesize, tts, reply, chime)
    name = uuid.uuid4().hex[:10]
    tts_files[name] = wav
    seconds = len(wav) / (TTS_RATE * 2)
    log(f"tts {seconds:.1f}s of speech in {time.time() - t0:.1f}s; follow-up {'yes' if more else 'no'}")
    send(Ev.VOICE_ASSISTANT_TTS_END, {"url": f"{base_url}/tts/{name}.wav"})
    if more or (SETTINGS["barge_in"] and not capped):    # what is heard now, without "Алиса", is a continuation
        CONT_UNTIL["t"] = time.time() + seconds + NO_SPEECH_S + 3
    if SETTINGS["barge_in"] and state_services.get("assistant") and seconds > 1.5 and not capped:
        BARGE.update(on=True, t0=time.time(), seconds=seconds, reply=reply, run=0, pos=0)
        BARGE["buf"].clear()
        vad_model().reset_states()

        async def barge_off():
            await asyncio.sleep(seconds + 0.6)
            BARGE["on"] = False

        asyncio.get_running_loop().create_task(barge_off())
    air = PENDING_AIR.pop("cmd", None)
    if air:
        async def later():
            await asyncio.sleep(seconds + 0.8)
            log(f"airplay {air} (after the answer)")
            await run_tool("_airplay_now", {"action": air})

        asyncio.get_running_loop().create_task(later())
    btc = PENDING_BT.pop("cmd", None)
    if btc:
        async def bt_later():
            await asyncio.sleep(seconds + 0.8)
            log(f"bluetooth {btc} (after the answer)")
            await run_tool("_bluetooth_now", {"action": btc})

        asyncio.get_running_loop().create_task(bt_later())
    shown = PENDING_DISPLAY.pop("text", None)
    extra = int(PENDING_DISPLAY.pop("extra", 0))                # the text stays a little after she has finished
    if shown and state_services.get("clock_show_text"):     # weather / alarm time: the text instead of the eyes
        try:
            await cli.execute_service(state_services["clock_show_text"], {"text": shown, "seconds": max(2, int(seconds + 0.6)) + extra})
            CLOCKTXT.update(text=str(shown), until=time.time() + max(2, int(seconds + 0.6)) + extra)
        except Exception as e:
            log(f"display text: {e!r}")
    elif mood and state_services.get("clock_show_face"):     # the emotion instead of the eyes: while she talks and a bit after
        try:
            await cli.execute_service(state_services["clock_show_face"], {"face": mood, "seconds": int(seconds) + 4})
        except Exception as e:
            log(f"face: {e!r}")
    else:
        await face("speak:" + envelope_hex(wav, ANIM_LEAD_MS), int(seconds + 1.5) + 1)     # eyes while it talks
    send(Ev.VOICE_ASSISTANT_RUN_END, None)

async def run_speaker(stt, tts, base_url):
    if LINK:
        log("no station is linked to this server yet" if not LINK.active() else "a station is linked")
    while True:
        if LINK:                                              # wait for a confirmed station and use the loopback ports of its link
            conn = await LINK.wait_active()
            SPK.update(host="127.0.0.1", api=conn.local[6053], airplay=conn.local[7000], cover=conn.local[49153])
            log(f"station {conn.id} is on the link")
        key = (LINK.api_key_of(conn.id) if LINK else "") or PULSAR_KEY
        cli = APIClient(SPK["host"], SPK["api"], None, noise_psk=key or None, timezone=city_timezone() or None)   # the key appears when Home Assistant adopts the speaker
        try:
            await cli.connect(login=True)
            info = await cli.device_info()
            log(f"connected to {info.name} ({info.model})")
            state = {"d": None}
            services = (await cli.list_entities_services())[1]
            svc = {x.name: x for x in services}
            state_services.clear()
            state_services.update(svc)
            log("lostOS services: " + ", ".join(svc))
            old_faces = {"listen": "Neutral", "think": "Neutral", "off": "None"}

            async def face(program: str, seconds: int):
                """Display animation: lostOS's clock_anim (smooth) or, on a stock build, a static face."""
                step = program.split(":")[0]
                if step in ("listen", "think", "speak"):
                    PHASE.update(p=step, until=time.time() + max(1, seconds))
                elif step == "off":
                    PHASE.update(p="idle", until=0.0)
                try:
                    if "clock_anim" in svc:
                        await cli.execute_service(svc["clock_anim"], {"program": program, "seconds": seconds})
                    elif "clock_show_face" in svc:
                        name = old_faces.get(program.split(":")[0], "Blink")
                        await cli.execute_service(svc["clock_show_face"], {"face": name, "seconds": seconds})
                except Exception as e:
                    log(f"display {program[:12]}: {e!r}")
            media = next((e for e in (await cli.list_entities_services())[0] if type(e).__name__ == "MediaPlayerInfo"), None)

            async def tool_airplay(action: str = "") -> str:
                if "airplay" not in svc:
                    return "эта сборка lostOS не умеет управлять AirPlay"
                if action == "on" and PLUGINS and not PLUGINS.plugin_on("ru.lostos.airplay"):
                    return "AirPlay выключен в плагинах: его можно включить на странице плагина AirPlay 2 в панели"
                if action in ("on", "off"):
                    PENDING_CHIME["kind"] = action
                    PENDING_AIR["cmd"] = action
                    return "AirPlay " + ("включится" if action == "on" else "выключится") + " сразу после ответа"
                await cli.execute_service(svc["airplay"], {"command": action})
                return {"on": "AirPlay включён", "off": "AirPlay выключен"}.get(action, f"команда {action} отправлена")

            async def tool_bluetooth(action: str = "") -> str:
                if "assistant" not in svc:
                    return "эта сборка lostOS не умеет управлять Bluetooth"
                if action in ("on", "off"):
                    if BT["on"] is not None and BT["on"] == (action == "on"):    # e.g. lostOS's own spotter already did it
                        return "Bluetooth уже " + ("включён" if action == "on" else "выключен")
                    PENDING_BT["cmd"] = action
                    return "Bluetooth " + ("включится" if action == "on" else "выключится") + " сразу после ответа"
                return "непонятная команда"

            async def bluetooth_now(action: str = "") -> str:
                await cli.execute_service(svc["assistant"], {"command": "bluetooth " + action})
                return "ok"

            volume = {"level": 0.4}

            async def tool_volume(level: int = None, change: int = None) -> str:
                if media is None:
                    return "громкость недоступна"
                cur = volume["level"]
                new = (level / 100) if level is not None else cur + (change or 0) / 100
                new = max(0.0, min(1.0, new))
                cli.media_player_command(media.key, volume=new)
                volume["level"] = new
                return f"громкость {round(new * 100)} процентов"

            PANEL["cli"] = cli
            PANEL["selects"] = {e.object_id: {"key": e.key, "name": e.name, "options": list(e.options)}
                                for e in (await cli.list_entities_services())[0] if type(e).__name__ == "SelectInfo"}
            PANEL["switches"] = {e.object_id: {"key": e.key, "name": e.name}
                                 for e in (await cli.list_entities_services())[0] if type(e).__name__ == "SwitchInfo" and e.object_id in ("volume_sound", "lyrics", "cover_lookup", "alice_button")}
            AIR_NOW["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                   if type(e).__name__ == "TextSensorInfo" and e.object_id == "airplay_now_playing"), None)

            DEVPLUG["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                   if type(e).__name__ == "TextSensorInfo" and e.object_id == "plugins_state"), None)
            SYSSTAT["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                   if type(e).__name__ == "TextSensorInfo" and e.object_id == "system_stats"), None)
            DISPLAY["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                   if type(e).__name__ == "TextSensorInfo" and e.object_id == "display_frame"), None)
            DISPLAY.update(f="", t=0.0)
            EQ["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                              if type(e).__name__ == "TextSensorInfo" and e.object_id == "eq_state"), None)
            MEDIA["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                 if type(e).__name__ == "TextSensorInfo" and e.object_id == "music_json"), None)
            PLAYBACK["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                    if type(e).__name__ == "TextSensorInfo" and e.object_id == "playback_position"), None)
            NOWP["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                 if type(e).__name__ == "TextSensorInfo" and e.object_id == "now_playing"), None)
            SPOT_NOW["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                    if type(e).__name__ == "TextSensorInfo" and e.object_id == "spotify_now_playing"), None)
            SPOT_COVER["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                      if type(e).__name__ == "TextSensorInfo" and e.object_id == "spotify_cover"), None)

            BT["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                              if type(e).__name__ == "TextSensorInfo" and e.object_id == "bluetooth_status"), None)
            if "assistant" in svc:
                await cli.execute_service(svc["assistant"], {"command": "hello"})       # "the assistant server is here" (the boot show waits for it)
                await cli.execute_service(svc["assistant"], {"command": "barge on" if SETTINGS["barge_in"] else "barge off"})
            WIFI["key"] = next((e.key for e in (await cli.list_entities_services())[0]
                                if type(e).__name__ == "TextSensorInfo" and e.object_id == "wifi_status"), None)

            def on_state(st):
                on_event_state(st)
                if type(st).__name__ == "TextSensorState" and WIFI["key"] is not None and st.key == WIFI["key"]:
                    WIFI["text"] = st.state or ""
                if type(st).__name__ == "TextSensorState" and SPOT_NOW["key"] is not None and st.key == SPOT_NOW["key"]:
                    SPOT_NOW["text"] = st.state or ""
                if type(st).__name__ == "TextSensorState" and SPOT_COVER["key"] is not None and st.key == SPOT_COVER["key"]:
                    # "cover url<TAB>spotify:track:ID<TAB>length ms<TAB>position ms" (position at the moment of the last event)
                    cover, uri, ms, pos = ((st.state or "").split("\t") + ["", "", "", ""])[:4]
                    SPOT_COVER.update(text=cover, uri=uri, ms=int(ms) if ms.isdigit() else 0, pos=int(pos) if pos.isdigit() else 0, t=time.time())
                if type(st).__name__ == "TextSensorState" and NOWP["key"] is not None and st.key == NOWP["key"]:
                    NOWP["text"] = st.state or ""
                if type(st).__name__ == "TextSensorState" and DEVPLUG["key"] is not None and st.key == DEVPLUG["key"]:
                    for ln in (st.state or "").splitlines():      # "id<TAB>state<TAB>detail"
                        f = ln.split("\t")
                        if len(f) >= 2 and getattr(PANEL.get("plugins"), "device", None) is not None:
                            PANEL["plugins"].device[f[0]] = (f[1] + (": " + f[2] if len(f) > 2 and f[2] else ""))
                if type(st).__name__ == "TextSensorState" and DISPLAY["key"] is not None and st.key == DISPLAY["key"]:
                    DISPLAY.update(f=st.state or "", t=time.time())
                    for q in list(DISPLAY["subs"]):                 # the panels that watch the display get every frame at once
                        if q.full():
                            q.get_nowait()
                        q.put_nowait(DISPLAY["f"])
                if type(st).__name__ == "TextSensorState" and SYSSTAT["key"] is not None and st.key == SYSSTAT["key"]:
                    d, texts = {}, {}
                    for part in (st.state or "").split(";"):         # "cpu=23;temp=48.5;mem_used=...;disk_free=...;name=...;model=...;code=...;ver=...;fw=..."
                        k, _, v = part.partition("=")
                        try:
                            d[k] = float(v)
                        except ValueError:
                            texts[k] = v
                    SYSSTAT.update(d=d, s=texts, t=time.time())
                    own = texts.get("name", "").strip()              # the Spotify Connect name of THIS speaker: every station of a house plays to itself, not to the first one
                    if own and not os.environ.get("SPEAKER_NAME") and own.casefold() != SPOT.device_name.casefold():
                        log(f"spotify: this speaker is '{own}' in Spotify Connect")
                        SPOT.device_name, SPOT._device_id = own, None
                if type(st).__name__ == "TextSensorState" and EQ["key"] is not None and st.key == EQ["key"]:
                    try:
                        on, pre, gains = (st.state or "").split(";")
                        g = [float(x) for x in gains.split(",")]
                        if len(g) == 10:
                            EQ.update(on=on == "1", pre=float(pre), gains=g, known=True)
                    except ValueError:
                        pass
                if type(st).__name__ == "TextSensorState" and MEDIA["key"] is not None and st.key == MEDIA["key"]:
                    media_update(st.state or "")
                if type(st).__name__ == "TextSensorState" and PLAYBACK["key"] is not None and st.key == PLAYBACK["key"]:
                    f = (st.state or "").split("\t")
                    if len(f) == 3 and f[1].isdigit() and f[2].isdigit():
                        PLAYBACK.update(src=f[0], pos=int(f[1]), dur=int(f[2]), t=time.time())
                if type(st).__name__ == "TextSensorState" and AIR_NOW["key"] is not None and st.key == AIR_NOW["key"]:
                    AIR_NOW["text"] = st.state or ""
                if type(st).__name__ == "TextSensorState" and BT["key"] is not None and st.key == BT["key"]:
                    f = (st.state or "").split("\t")
                    BT.update(on=(f[0] == "on") if f and f[0] else None, connected=len(f) > 1 and f[1] == "1",
                              playing=len(f) > 2 and f[2] == "1", now=f[3] if len(f) > 3 else "")
                if type(st).__name__ in ("MediaPlayerState", "MediaPlayerEntityState"):
                    volume["level"] = st.volume
                    PANEL["volume"] = st.volume
                if type(st).__name__ in ("SelectState", "SwitchState"):
                    PANEL["state"][st.key] = st.state

            cli.subscribe_states(on_state)

            async def airplay_now(action: str = "") -> str:
                await cli.execute_service(svc["airplay"], {"command": action})
                return "ok"

            async def tool_music(action: str = "") -> str:
                """Pause / play / skip whatever is playing: Spotify when it plays on this speaker, else AirPlay / Bluetooth."""
                if MEDIA["t"] and "assistant" in svc and action in ("play", "pause", "next", "previous"):   # the speaker's MediaHub knows what plays
                    await cli.execute_service(svc["assistant"], {"command": "media " + action})
                    return f"{(MEDIA['data'].get('source') or 'музыка')}: {action}"
                if BT["playing"] and "airplay" in svc:             # lostOS routes the command to the phone on Bluetooth
                    await cli.execute_service(svc["airplay"], {"command": action})
                    return f"Bluetooth: {action}"
                if SPOT.available:
                    try:
                        if action in ("pause", "next", "previous") and await SPOT.is_playing_here():
                            await SPOT.control(action)
                            return f"Spotify: {action}"
                        if action == "play":
                            st = await SPOT.state()
                            dev = (st or {}).get("device") or {}
                            other = bool(st and st.get("is_playing")) and dev.get("name", "").casefold() != SPOT.device_name.casefold()
                            if not other and not (st and st.get("is_playing")):   # paused or idle on this speaker: resume Spotify
                                await SPOT.control("play")
                                return "Spotify: продолжаю"
                    except SpotifyError as e:
                        log(f"spotify: {e}")
                if "airplay" in svc:
                    await cli.execute_service(svc["airplay"], {"command": action})
                    return f"AirPlay: {action}"
                return "нечем управлять"

            async def tool_equalizer(mode: str = "", style: str = "") -> str:
                sels = {e.object_id: e for e in (await cli.list_entities_services())[0] if type(e).__name__ == "SelectInfo"}
                done = []
                for oid, want, names in (("clock_equalizer", mode, {"always": "Always", "track_change": "On track change", "off": "Off"}),
                                         ("clock_equalizer_style", style, {"wave": "Wave", "bars": "Bars"})):
                    if want and oid in sels and want in names:
                        cli.select_command(sels[oid].key, names[want])
                        done.append(names[want])
                return "эквалайзер: " + ", ".join(done) if done else "ничего не изменила"

            async def tool_home(entity_ids=None, service: str = "", brightness_pct=None, color: str = "", color_temp_kelvin=None,
                                position=None, temperature=None, hvac_mode: str = "", volume=None, other_room_ok: bool = False) -> str:
                if not HA.configured:
                    return "Home Assistant не подключён (в .env нет HA_URL и HA_TOKEN)"
                if not await HA.refresh():
                    return f"Home Assistant недоступен: {HA.error}"
                ids = [e for e in (entity_ids or []) if e in HA.entities]
                if not ids:
                    return "таких устройств в доме не нашла; назови устройство точнее"
                if HA.room and not other_room_ok:                  # the speaker's own room only, unless the person said otherwise
                    other = [HA.entities[e]["name"] + " (" + (HA.room_of(e) or "без комнаты") + ")" for e in ids if HA.room_of(e) not in (HA.room, "")]
                    if other:
                        return ("отказано: это устройства другой комнаты: " + ", ".join(other) +
                                ". Спроси, можно ли управлять ими, или пусть назовёт комнату сам")
                data = {}
                if brightness_pct is not None:
                    data["brightness_pct"] = int(brightness_pct)
                if color:
                    c = color.strip().lower()
                    parts = [x for x in c.replace(" ", "").split(",") if x]
                    if len(parts) == 3 and all(x.isdigit() for x in parts):
                        data["rgb_color"] = [int(x) for x in parts]
                    else:
                        data["color_name"] = c
                if color_temp_kelvin:
                    data["color_temp_kelvin"] = int(color_temp_kelvin)
                if position is not None:
                    data["position"] = int(position)
                if temperature is not None:
                    data["temperature"] = float(temperature)
                if hvac_mode:
                    data["hvac_mode"] = hvac_mode
                if volume is not None:
                    data["volume_level"] = float(volume)
                res = await HA.call(ids, service, data)
                if res.startswith("готово") and state_services.get("assistant"):     # the jingle at the very moment HA said "done"
                    kind = {"turn_on": "home_on", "turn_off": "home_off"}.get(service, "home_change")
                    await cli.execute_service(state_services["assistant"], {"command": "jingle " + kind})
                log(f"home: {service} {ids} {data} -> {res}")
                return res

            async def push_alarm(it: dict):
                """Hand the alarm to the speaker: it rings it by itself, also when this server is down."""
                svc_a = state_services.get("assistant")
                if not svc_a or it.get("snooze"):
                    return
                occ = REM.occurrences(it)
                if not occ:
                    return
                label = re.sub(r"\s+", " ", it.get("label") or "").strip()[:40]
                try:      # "~z<minutes>": how long the speaker waits before it rings again when nobody switched it off (0 = never)
                    await cli.execute_service(svc_a, {"command": "alarm_set %s %s ~z%d %s" % (
                        it["id"], ",".join(str(int(x)) for x in occ), int(it.get("snooze_min", 5)), label)})
                except Exception as e:
                    log(f"alarm hand-over: {e!r}")

            async def push_reminder(it: dict):
                """Prepare the spoken phrase now and hand the reminder to the speaker: it says it by itself, also when this
                server is down; the phrase is deleted from the speaker after it was said."""
                svc_a = state_services.get("assistant")
                if not svc_a:
                    return
                occ = REM.occurrences(it)
                if not occ:
                    return
                path = os.path.join(REM_DIR, it["id"] + ".wav")
                if not os.path.exists(path):
                    wav = await asyncio.get_running_loop().run_in_executor(
                        None, synthesize, GLOBALS["tts"], "Вы просили напомнить: " + it["text"] + ".", "on")
                    os.makedirs(REM_DIR, exist_ok=True)
                    with open(path, "wb") as f:
                        f.write(wav)
                label = re.sub(r"\s+", " ", it.get("text") or "").strip()[:40]
                try:
                    await cli.execute_service(svc_a, {"command": "reminder_set %s %s %s %s" % (
                        it["id"], ",".join(str(int(x)) for x in occ), GLOBALS["base_url"] + "/rem/" + it["id"] + ".wav", label)})
                except Exception as e:
                    log(f"reminder hand-over: {e!r}")

            def drop_reminder_file(rid: str):
                try:
                    os.remove(os.path.join(REM_DIR, rid + ".wav"))
                except OSError:
                    pass

            async def sync_alarm_led():
                svc_a = state_services.get("assistant")
                if svc_a:
                    try:
                        has = any(i["kind"] == "alarm" and i.get("enabled", True) for i in REM.items)
                        await cli.execute_service(svc_a, {"command": "alarm on" if has else "alarm off"})
                    except Exception as e:
                        log(f"alarm led: {e!r}")

            async def tool_timer(minutes=None, seconds=None, label: str = "") -> str:
                total = int((minutes or 0) * 60 + (seconds or 0))
                if total < 1:
                    return "не поняла, на сколько поставить таймер"
                it = REM.add_timer(total, label or "")
                cli.send_voice_assistant_timer_event(TE.VOICE_ASSISTANT_TIMER_STARTED, it["id"], it.get("label") or None, total, total, True)
                return f"таймер поставлен на {total // 60} мин {total % 60} с"

            async def tool_alarm(time: str = "", date: str = "", repeat: str = "once", label: str = "", snooze_minutes: int = 5, replace_id: str = "") -> str:
                enabled = True
                if replace_id:                                # the panel edits an alarm: the old one goes, its on/off state stays
                    old = next((i for i in REM.items if i["id"] == replace_id and i["kind"] == "alarm"), None)
                    if old:
                        enabled = old.get("enabled", True)
                        REM.items.remove(old)
                        REM.save()
                        if state_services.get("assistant"):
                            await cli.execute_service(state_services["assistant"], {"command": "alarm_del " + replace_id})
                try:
                    it = REM.add_alarm(time, date or "", repeat or "once", label or "", snooze_minutes, enabled)
                except (ValueError, KeyError):
                    return "не поняла время будильника"
                await push_alarm(it)
                await sync_alarm_led()
                import datetime
                # the alarm time covers the clock while she confirms it (fades in and out, the "alarm set" LED lights with it)
                PENDING_DISPLAY["text"] = datetime.datetime.fromtimestamp(it["at"]).strftime("%H:%M") + "\u00b0"
                PENDING_DISPLAY["extra"] = 3
                return "будильник поставлен: " + REM.describe(it)

            async def alarm_toggle(id: str = "", on: bool = True) -> str:
                it = REM.set_enabled(id, bool(on))
                if not it:
                    return "нет такого будильника"
                svc_a = state_services.get("assistant")
                if svc_a and not on:
                    await cli.execute_service(svc_a, {"command": "alarm_del " + id})
                elif on:
                    await push_alarm(it)
                await sync_alarm_led()
                return "ok"

            async def speak(text: str):
                """Say a phrase on the speaker out of a dialog (an announcement, like Home Assistant's): the same voice as the answers."""
                if media is None or not text.strip():
                    return
                wav = await asyncio.get_running_loop().run_in_executor(None, synthesize, GLOBALS["tts"], text, None)
                name = uuid.uuid4().hex[:10]
                tts_files[name] = wav
                cli.media_player_command(media.key, media_url=f"{GLOBALS['base_url']}/tts/{name}.wav", announcement=True)
                await asyncio.sleep(len(wav) / (TTS_RATE * 2))

            async def start_enroll(name: str, owner: int) -> str:
                """The panel asked to remember a voice: the music stops, she greets, gives the first phrase and listens without "Алиса"; silence ends it with an error for the panel."""
                try:
                    await run_tool("music_control", {"action": "pause"}, trusted=True)
                except Exception as e:
                    log(f"enroll: could not pause the music: {e!r}")
                ENROLL.update(name=name, embs=[], need=3, owner=owner, off=0)
                ENROLL_STATUS.update(state="waiting", name=name, step=0, need=3, error="", t=time.time())
                journal(f"обучение голосу: {name}", owner)
                text = f"Привет, я очень хочу познакомиться с тобой. Повтори за мной: {ENROLL_PROMPTS[0]}"
                wav = await asyncio.get_running_loop().run_in_executor(None, synthesize, GLOBALS["tts"], text, None)
                tag = uuid.uuid4().hex[:10]
                tts_files[tag] = wav
                try:
                    await cli.send_voice_assistant_announcement_await_response(f"{GLOBALS['base_url']}/tts/{tag}.wav", 60, text, "", True)
                except Exception as e:
                    log(f"enroll: announcement failed: {e!r}")
                    ENROLL.update(name=None, embs=[])
                    ENROLL_STATUS.update(state="error", error="Колонка не ответила, попробуйте ещё раз", t=time.time())
                    return "error"
                ENROLL_STATUS["t"] = time.time()
                asyncio.get_running_loop().create_task(enroll_watch(name))
                return "ok"

            async def enroll_watch(name: str):
                """No phrase for 25 seconds (after the question or after the last sample): the training is dropped and the panel is told."""
                while ENROLL["name"] == name:
                    await asyncio.sleep(1)
                    if time.time() - ENROLL_STATUS["t"] > 25:
                        ENROLL.update(name=None, embs=[])
                        ENROLL_STATUS.update(state="error", error="Не услышала ответ, нажмите ещё раз и повторите фразу", t=time.time())
                        log("enroll: no answer, dropped")
                        return

            SPK_CALLS["start_enroll"] = start_enroll

            async def sleep_fade(minutes: int, smooth: bool = True):
                """The music gets quieter for `minutes` and stops (smooth), or plays on and stops at the end (not smooth); the volume comes back."""
                start = volume["level"]
                total = minutes * 60
                t0 = time.time()
                SLEEP.update(until=t0 + total, minutes=minutes, smooth=smooth)
                done = False
                try:
                    if smooth:
                        last = None
                        while time.time() - t0 < total:
                            lvl = max(1, round(100 * start * (1 - (time.time() - t0) / total)))
                            if lvl != last:
                                await tool_volume(level=lvl)
                                last = lvl
                            await asyncio.sleep(max(3.0, total / 80))
                    else:
                        await asyncio.sleep(total)
                    done = True
                finally:
                    if done:
                        await run_tool("music_control", {"action": "pause"}, trusted=True)
                    await tool_volume(level=round(start * 100))          # the volume is not left low (also when the fade was cancelled)
                    SLEEP.update(until=0.0, task=None)

            def sleep_start(minutes: int, smooth: bool = True) -> str:
                if SLEEP["task"]:
                    SLEEP["task"].cancel()
                SLEEP["task"] = asyncio.get_running_loop().create_task(sleep_fade(int(minutes), bool(smooth)))
                return "ok"

            async def sleep_start_tool(minutes: int = 30, smooth: bool = True) -> str:
                return sleep_start(minutes, smooth)

            async def sleep_cancel() -> str:
                if SLEEP["task"]:
                    SLEEP["task"].cancel()
                return "ok"

            async def run_scenario_obj(sc: dict, source: str = "panel"):
                log(f"scenario «{sc['name']}» ({source}): {len(sc['actions'])} actions")
                for a in sc["actions"]:
                    try:
                        t = a["type"]
                        if t == "light":
                            data = {k: v for k, v in (("brightness_pct", a.get("brightness_pct")), ("rgb_color", a.get("rgb"))) if v}
                            res = await HA.call([a["entity"]], "turn_on" if a.get("on", True) else "turn_off", data if a.get("on", True) else {})
                        elif t == "ha":
                            res = await HA.call([a["entity"]], a.get("service", "turn_on"), {})
                        elif t == "say":
                            await speak(a["text"])
                            res = "ok"
                        elif t == "music":
                            res = await run_tool("music_control", {"action": a["action"]}, trusted=True)
                        elif t == "volume":
                            res = await run_tool("set_volume", {"level": a["level"]}, trusted=True)
                        elif t == "fade":
                            await sleep_fade(a["minutes"], a.get("smooth", True))
                            res = "ok"
                        elif t == "wait":
                            await asyncio.sleep(a["seconds"])
                            res = "ok"
                        else:
                            res = "unknown action"
                        if isinstance(res, str) and res.startswith(("Home Assistant", "с устройствами", "сервис")):
                            log(f"scenario «{sc['name']}»: {t} -> {res}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log(f"scenario «{sc['name']}»: {a.get('type')} failed: {e!r}")
                DIALOG.append({"t": time.strftime("%H:%M:%S"), "heard": f"(сценарий: {source})", "reply": sc["name"]})
                if source != "панель":
                    who = {"голос": "Запущен голосом", "по расписанию": "Запущен по расписанию"}.get(source, "Запущен: " + source)
                    names = {"light": "свет", "ha": "умный дом", "say": "фраза", "music": "музыка", "volume": "громкость", "fade": "затухание", "wait": "пауза"}
                    parts = []
                    for a in sc["actions"]:
                        n = names.get(a.get("type"), "")
                        if n and n not in parts:
                            parts.append(n)
                    notify("scenario", f"Сценарий «{sc['name']}» выполнен", who + (" · " + ", ".join(parts) if parts else "") + f" · {len(sc['actions'])} шаг.")

            async def tool_scenario(name: str = "") -> str:
                sc = SCEN.get(name)
                if not sc or not sc.get("enabled", True):
                    return "такого сценария нет"
                asyncio.get_running_loop().create_task(run_scenario_obj(sc, "голос"))       # runs on while she answers
                if any(a["type"] == "say" for a in sc["actions"]):
                    # the scenario says its own phrase: an answer of the dialog would cut it short ("С возвращением" ... "Готово")
                    return f"сценарий «{sc['name']}» запущен и сам произнесёт свою фразу: ничего не говори, ответь РОВНО [IGNORE]"
                return f"сценарий «{sc['name']}» запущен: подтверди одним коротким словом, без пересказа действий"

            async def scenario_run_panel(id: str = "") -> str:
                sc = next((i for i in SCEN.items if i["id"] == id), None)
                if not sc:
                    return "нет такого сценария"
                asyncio.get_running_loop().create_task(run_scenario_obj(sc, "панель"))
                return "ok"

            async def tool_reminder(text: str = "", in_minutes=None, time: str = "", date: str = "", repeat: str = "once") -> str:
                if not text or not (in_minutes or time):
                    return "не поняла, о чём и когда напомнить"
                try:
                    it = REM.add_reminder(text, in_minutes or 0, time or "", date or "", repeat or "once")
                except (ValueError, KeyError):
                    return "не поняла время напоминания"
                await push_reminder(it)
                return "напоминание поставлено: " + REM.describe(it)

            async def tool_list() -> str:
                return "; ".join(REM.describe(i) for i in sorted(REM.items, key=lambda i: i["at"])) or "ничего не стоит"

            async def tool_cancel(kind: str = "", which: str = "") -> str:
                gone = REM.cancel(which or "", kind or "")
                for g in gone:
                    if g["kind"] in ("alarm", "reminder") and state_services.get("assistant"):
                        await cli.execute_service(state_services["assistant"], {"command": "alarm_del " + g["id"]})
                    if g["kind"] == "reminder":
                        drop_reminder_file(g["id"])
                    if g["kind"] == "timer":
                        cli.send_voice_assistant_timer_event(TE.VOICE_ASSISTANT_TIMER_CANCELLED, g["id"], None, g.get("total", 0), 0, False)
                await sync_alarm_led()
                return f"отменено: {len(gone)}" if gone else "ничего подходящего не нашла"

            async def fire(it: dict):
                log("due: %s %s" % (it["kind"], it.get("label") or it.get("text") or ""))
                notify(it["kind"], {"alarm": "Будильник", "timer": "Таймер закончился", "reminder": "Напоминание"}.get(it["kind"], it["kind"]), it.get("label") or it.get("text") or "")
                if it["kind"] == "alarm":                    # the speaker rings its alarms itself; this only keeps the lists in step
                    if it.get("repeat", "once") != "once":
                        await push_alarm(it)                 # the coming weeks again
                    DIALOG.append({"t": time.strftime("%H:%M:%S"), "heard": "(по расписанию)", "reply": "будильник " + (it.get("label") or "")})
                    await sync_alarm_led()
                    return
                if it["kind"] in ("timer", "alarm"):
                    name = ("alarm:" + (it.get("label") or "")) if it["kind"] == "alarm" else (it.get("label") or None)
                    if it["kind"] == "alarm":
                        RINGING["alarm"] = it
                    cli.send_voice_assistant_timer_event(TE.VOICE_ASSISTANT_TIMER_FINISHED, it["id"], name,
                                                         it.get("total", 0), 0, False)
                else:                                         # the speaker says the prepared phrase itself; this keeps the files in step
                    if it.get("repeat", "once") != "once":
                        await push_reminder(it)
                    else:
                        drop_reminder_file(it["id"])
                DIALOG.append({"t": time.strftime("%H:%M:%S"), "heard": "(по расписанию)",
                               "reply": it.get("text") or it.get("label") or it["kind"]})
                await sync_alarm_led()

            async def scheduler():
                # on (re)connect the timers the speaker lost are started again; then once a second
                svc_a = state_services.get("assistant")
                if svc_a:                                    # the speaker's alarm list is made equal to this one
                    await cli.execute_service(svc_a, {"command": "alarm_clear"})
                    for it in list(REM.items):
                        if it["kind"] == "alarm" and not it.get("snooze"):
                            await push_alarm(it)
                        elif it["kind"] == "reminder":
                            await push_reminder(it)
                    await cli.execute_service(svc_a, {"command": "alarm_gc"})
                for it in REM.items:
                    if it["kind"] == "timer":
                        left = max(1, int(it["at"] - time.time()))
                        cli.send_voice_assistant_timer_event(TE.VOICE_ASSISTANT_TIMER_STARTED, it["id"], it.get("label") or None, it["total"], left, True)
                await sync_alarm_led()
                while True:
                    await asyncio.sleep(1)
                    for it in REM.pop_due():
                        try:
                            await fire(it)
                        except Exception as e:
                            log(f"due item failed: {e!r}")
                    import datetime as _dt
                    for sc in SCEN.due(_dt.datetime.now()):                 # scenarios on a schedule
                        asyncio.get_running_loop().create_task(run_scenario_obj(sc, "по расписанию"))

            async def tool_search(query: str = "") -> str:
                q = (query or "").strip()
                if not q:
                    return "пустой запрос"
                try:
                    res = await websearch.search(q)
                    if res.startswith("ничего не нашла"):          # some phrases give nothing: try the English wording
                        en = await english_query(q)
                        if en and en.lower() != q.lower():
                            log(f"search: nothing for {q!r}, trying {en!r}")
                            res = await websearch.search(en)
                except Exception as e:
                    log(f"search: {e!r}")
                    return f"поиск не сработал: {e}"
                log(f"search {q!r}: {len(res)} chars")
                return res + "\n\n(Ответь по найденному своими словами, коротко и по делу; цифры и названия бери из текста.)"

            async def tool_enroll(name: str = "") -> str:
                name = (name or "").strip()
                if not name:
                    return "не услышала имя"
                if WHO["emb"] is None:
                    return "голос не разобрала, пусть скажет подольше"
                ENROLL.update(name=name, embs=[WHO["emb"]], need=4, owner=household_owner(), off=1)
                return (f"запоминаю голос {name}. Скажи дословно: «Хорошо, {name}, запоминаю. Повтори за мной: "
                        f"{ENROLL_PROMPTS[0]}» (дальше я сама назову остальные фразы)")

            async def tool_remember(note: str = "") -> str:
                if not WHO["name"]:
                    return "этот голос мне незнаком, сначала надо запомнить голос"
                VOICES.note(WHO["name"], note)
                return f"запомнила про {WHO['name']}"

            async def tool_forget(name: str = "") -> str:
                name = (name or WHO["name"] or "").strip()
                if name not in VOICES.people:
                    return "такого голоса я не знаю"
                VOICES.delete(name)
                return f"забыла голос {name}"

            async def tool_like(action: str = "like") -> str:
                src = parse_now_source(NOWP["text"])
                if src in ("AirPlay", "Bluetooth"):
                    return f"нельзя: сейчас играет {src}, а лайкать можно только музыку из Spotify. Скажи об этом коротко."
                try:
                    if SPOT_COVER.get("uri") and src == "Spotify":      # the speaker knows the track: no Web API guessing
                        await SPOT.like_uri(SPOT_COVER["uri"], action != "unlike")
                        what = (NOWP["text"].split(" · ")[0] or "трек")
                    else:
                        what = await SPOT.like(action != "unlike")
                except SpotifyError as e:
                    log(f"spotify like: {e}")
                    return f"не получилось: {e}" + (" (нужно заново дать доступ в Spotify)" if "scope" in str(e).lower() else "")
                return (f"добавила в любимые: {what}" if action != "unlike" else f"убрала лайк: {what}")

            async def tool_weather() -> str:
                try:
                    w = await get_weather()
                except Exception as e:
                    log(f"weather: {e!r}")
                    return f"не получилось узнать погоду: {e}"
                PENDING_DISPLAY["text"] = w["display"]
                return ("Скажи пользователю дословно эту фразу, можешь после неё добавить ОДНУ короткую реплику в своём стиле "
                        f"(совет по одежде, зонту и т.п.): «{w['phrase']}»")

            async def tool_spotify_play(query: str = "", kind: str = "any", variants=None) -> str:
                if not SPOT.available:
                    return "Spotify не подключён к ассистенту"
                try:
                    what = await SPOT.play(query, kind, list(variants or []))
                except SpotifyError as e:
                    log(f"spotify play: {e}")
                    return f"не получилось: {e}"
                ACTED["music_t"] = time.time()
                return f"включила {what}"

            async def local_command(name: str):
                """Voice commands heard without the wake word, forwarded by lostOS when no AirPlay session is active."""
                act = {"cmd_pause": "pause", "cmd_stop": "pause", "cmd_play": "play", "cmd_next": "next",
                       "cmd_previous": "previous", "swipe_next": "next", "swipe_previous": "previous"}.get(name)
                if name in ("ring_stopped", "ring_timeout"):            # the speaker rings its alarm again by itself after a timeout
                    log(f"alarm: {name}")
                    return
                if name == "spotify_pause":                    # an AirPlay sender took over
                    try:
                        await SPOT.control("pause")
                    except Exception as e:
                        log(f"spotify pause: {e}")
                    return
                if name in ("cmd_play_fresh", "cmd_spotify_fresh"):    # paused for 10+ minutes: ordinary music, not the old track
                    log(f"local command {name} -> {await tool_spotify_play(chr(0)[:0], 'library')}")
                    return
                if name == "cmd_spotify":                      # double tap on the centre button
                    try:
                        await SPOT.control("play")
                        res = "Spotify: play"
                    except Exception as e:
                        log(f"spotify play: {e}")
                        res = await tool_spotify_play("", "library")
                    log(f"local command {name} -> {res}")
                elif act:
                    # lostOS fires these only when no AirPlay session is active, so they are always about Spotify
                    try:
                        await SPOT.control(act)
                        log(f"local command {name} -> Spotify: {act}")
                    except Exception as e:
                        log(f"local command {name}: {e}")

            def on_event_state(st):
                if type(st).__name__ in ("Event", "EventState"):
                    log(f"button event {getattr(st, 'event_type', None)!r}")
                if type(st).__name__ in ("Event", "EventState") and (getattr(st, "event_type", "") or "").startswith(("cmd_", "swipe_", "spotify_", "ring_")):
                    asyncio.get_running_loop().create_task(local_command(st.event_type))

            ACTIONS.clear()
            ACTIONS.update({"airplay_control": tool_airplay, "bluetooth_control": tool_bluetooth, "_bluetooth_now": bluetooth_now, "set_volume": tool_volume, "_airplay_now": airplay_now,
                            "music_control": tool_music, "spotify_play": tool_spotify_play,
                            "get_weather": tool_weather, "clock_equalizer": tool_equalizer, "spotify_like": tool_like,
                            "search_web": tool_search, "set_timer": tool_timer, "set_alarm": tool_alarm, "_alarm_toggle": alarm_toggle, "run_scenario": tool_scenario, "_scenario_run": scenario_run_panel,
                            "_sleep_start": sleep_start_tool, "_sleep_cancel": sleep_cancel,
                            "set_reminder": tool_reminder, "list_reminders": tool_list, "cancel_reminder": tool_cancel,
                            "home_control": tool_home, "enroll_voice": tool_enroll, "remember_about_speaker": tool_remember, "forget_voice": tool_forget})
            async def on_start(conversation_id, flags, settings, wake_word_phrase):
                log(f"wake word {wake_word_phrase!r}")
                d = Dialog()
                state["d"] = d
                asyncio.get_running_loop().create_task(serve_dialog(cli, d, stt, tts, base_url, face))
                return 0

            def on_barge():
                said = said_so_far()
                INTERRUPTED.update(reply=BARGE["reply"], said=said, t=time.time())
                log(f"barge-in: she is interrupted after ~{said[-40:]!r}")
                asyncio.get_running_loop().create_task(
                    cli.execute_service(svc["assistant"], {"command": "interrupt"}))

            async def on_audio(data: bytes, end_of_speech=None):
                if state["d"] and not state["d"].done.is_set():
                    state["d"].feed(data)
                elif BARGE["on"]:
                    barge_feed(data, on_barge)
                elif THINK["on"]:
                    think_feed(data)

            async def on_stop(abort: bool):
                if state["d"]:
                    state["d"].done.set()

            listen = _envvar("LOSTOS_NO_VOICE") != "1"    # only ONE server may hold the voice of a speaker: the others (a PC at home next to the VDS) only show the panel
            if listen:
                cli.subscribe_voice_assistant(handle_start=on_start, handle_stop=on_stop, handle_audio=on_audio)
                if "assistant" in svc:       # lostOS forgets the server when the voice subscription moves: say it again
                    await asyncio.sleep(0.3)
                    await cli.execute_service(svc["assistant"], {"command": "hello"})
                sched_task = asyncio.get_running_loop().create_task(scheduler())
                log("listening for the wake word (say 'Алиса')")
            else:
                log("LOSTOS_NO_VOICE=1: the voice, the alarms and the timers are served by another server; this one only shows the panel")
            beat = 0
            while True:
                await asyncio.sleep(5)
                await cli.device_info()        # keep-alive; raises when the speaker goes away
                beat += 1
                if listen and beat % 2 == 0 and "assistant" in svc:
                    await cli.execute_service(svc["assistant"], {"command": "hello"})    # "I am here": lostOS goes local when it stops
        except Exception as e:
            log(f"speaker connection lost: {e!r}; retrying in 5 s")
            try:
                await cli.disconnect()
            except Exception:
                pass
            await asyncio.sleep(5)


async def setup_tenant(app, tts):
    """What a tenant has instead of the login, the accounts and the station link: the gateway vouches for every request, the link to the speaker is asked from the gateway."""
    global LINK, ACCOUNTS
    import hmac
    import accounts
    import vault
    import spotify_login
    import homeassistant
    import setup_wizard
    import tenant_link
    ACCOUNTS = accounts.Accounts(os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or os.path.dirname(os.path.abspath(__file__)), "accounts.db"))     # only the journal and the voice prints of this household
    VOICES.attach(accounts.VoiceStore(ACCOUNTS))
    app["accounts"] = ACCOUNTS

    @web.middleware
    async def gate(req: web.Request, handler):
        if not TENANT_KEY or not hmac.compare_digest(req.headers.get("X-Lostos-Key", ""), TENANT_KEY):
            return web.Response(status=403)
        tg = req.headers.get("X-Lostos-Tg")
        if tg is not None:                                               # a person the gateway has logged in; the public requests of the speaker (answers, reminders) have none
            req["session"] = {"tg_id": int(tg or 0), "role": req.headers.get("X-Lostos-Role", "member"), "hash": ""}
        return await handler(req)

    app.middlewares.insert(0, gate)
    VAULT = vault.Vault(log)
    vault.install_api(app, VAULT)
    LINK = tenant_link.RemoteLink(os.environ.get("LOSTOS_GATEWAY", "http://127.0.0.1:8765"), TENANT_ID, TENANT_KEY, VAULT, log, STATION_ID)
    homeassistant.FORWARD = lambda: LINK.forward_url("ha")
    setup_wizard.install(app, sys.modules[__name__], VAULT, PUBLIC_URL)
    spotify_login.install(app, VAULT, PUBLIC_URL, SPOT, log)

    async def internal_status(req):
        return web.json_response({"online": PANEL["cli"] is not None, "now": NOWP["text"], "tenant": TENANT_ID, "station": STATION_ID})

    async def internal_media(req):
        import media
        b = await req.json()
        try:
            return web.json_response({"ok": True, "result": await media.command(sys.modules[__name__], str(b.get("cmd", "")), b.get("args") or {})})
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    app.router.add_get("/internal/status", internal_status)
    app.router.add_post("/internal/media", internal_media)
    asyncio.get_running_loop().create_task(LINK.run())
    log(f"tenant {TENANT_ID}: the link to the speaker is asked from the gateway")


async def main():
    stt, tts = LazyStt(), LazyTts()
    ENGINES.update(stt=stt, tts=tts)
    for name, kind, ok in (("Whisper", "stt", whisper_ok), ("Silero", "tts", silero_ok)):    # the local engines are optional: the server starts without them
        if (stt_engine() if kind == "stt" else tts_engine()) in ("whisper", "silero") and not ok():
            log(f"{name} is chosen but not installed: the choice is reset to the cloud, install it in the panel")
            SETTINGS[kind + "_engine"] = "yandex" if kind == "stt" else "alice"
            save_settings()
        elif not ok():
            log(f"{name} is not installed (optional, the panel can install it)")

    def warm_up():
        # the first GPU call compiles kernels (~15 s on a fresh card): do it before the first user phrase
        t0 = time.time()
        noise = (np.random.randn(16000 * 2) * 800).astype(np.int16).tobytes()
        if stt_engine() == "whisper":
            transcribe(stt, noise)
        synthesize(tts, "Привет, я готова.")
        vad_model()
        VOICES.load()
        log(f"warm-up done in {time.time() - t0:.1f}s")

    if not LITE:
        await asyncio.get_running_loop().run_in_executor(None, warm_up)
    asyncio.get_running_loop().create_task(music_refresher())
    asyncio.get_running_loop().create_task(shared.watch(reload_shared))
    GEMINI_BEST["m"] = SETTINGS.get("gemini_best") or GEMINI_BEST["m"]

    # which of our addresses does the speaker see?
    if TENANT:
        my_ip, base_url = "127.0.0.1", f"{PUBLIC_URL}/t/{TENANT_ID}" + (f"/s/{STATION_ID}" if STATION_ID else "")     # the speaker fetches its answers from here; the gateway forwards /t/<id>[/s/<station>]/... to this process
    elif LITE:
        my_ip, base_url = "127.0.0.1", PUBLIC_URL or f"http://127.0.0.1:{HTTP_PORT}"
    else:
        if not SPEAKER:
            log("не задан адрес колонки: python voice_server.py 192.168.x.y (или переменная LOSTOS_SPEAKER)")
            sys.exit(2)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((SPEAKER, 6053))
        my_ip = s.getsockname()[0]
        s.close()
        base_url = f"http://{my_ip}:{HTTP_PORT}"

    app = web.Application()

    async def get_tts(req):
        wav = tts_files.get(req.match_info["name"])
        return web.Response(body=wav, content_type="audio/wav") if wav else web.Response(status=404)

    async def get_rem(req):
        rid = req.match_info["id"]
        path = os.path.join(REM_DIR, rid + ".wav")
        if not re.fullmatch(r"[0-9a-f]{8}", rid) or not os.path.exists(path):
            return web.Response(status=404)
        return web.FileResponse(path)

    app.router.add_get("/tts/{name}.wav", get_tts)
    app.router.add_get("/rem/{id}.wav", get_rem)
    import panel
    panel.register(app, sys.modules[__name__])
    bot = None
    if TENANT:
        await setup_tenant(app, tts)
    elif os.environ.get("LOSTOS_AUTH") == "1":
        import auth, accounts, account_api, tgbot
        people = accounts.Accounts()
        global ACCOUNTS
        ACCOUNTS = people
        VOICES.attach(accounts.VoiceStore(people))                           # the voice prints live in the database, each with the person it belongs to
        token = _envvar("LOSTOS_TG_TOKEN")
        bot_ctx = BotCtx()
        if token:
            bot = tgbot.Bot(token, people, log, bot_ctx, os.environ.get("LOSTOS_TG_API", tgbot.API), PUBLIC_URL)
        auth.install(app, PUBLIC_URL, log, people, bot)
        import telegram_oidc
        telegram_oidc.install(app, people, bot, PUBLIC_URL, log)
        account_api.install(app, bot)
    else:
        import account_api, setup_wizard
        account_api.install_local(app, _envvar("LOSTOS_REMOTE_URL"))
        setup_wizard.install_unavailable(app)
    brains.SETTINGS = lambda: SETTINGS
    ai_settings.install(app, sys.modules[__name__])                      # the settings of the assistant's brain: every kind of server has them
    if LITE and not TENANT:
        global LINK
        import link, vault
        VAULT = vault.Vault(log)
        vault.install_api(app, VAULT)
        LINK = link.install(app, log, VAULT)
        app["locked"] = lambda s: not any(x["confirmed"] for x in LINK.listing())                  # the panel works only after the first speaker is confirmed
        LINK.say = lambda text: asyncio.get_running_loop().run_in_executor(None, synthesize, tts, text)       # the spoken line when a station must be pressed
        if bot:
            bot_ctx.link = LINK
            LINK.listeners.append(bot.on_link_event)
        import homeassistant, spotify_login
        homeassistant.FORWARD = lambda: LINK.forward_url("ha")

        def preload_voices():                                                  # the one shared voice model of the server, loaded once in the background
            ok = VOICES.load()
            log("voice recognition is ready (one model for everybody)" if ok else "voice recognition is not installed on this server")
        asyncio.get_running_loop().run_in_executor(None, preload_voices)
        import setup_wizard
        setup_wizard.install(app, sys.modules[__name__], VAULT, PUBLIC_URL)
        spotify_login.install(app, VAULT, PUBLIC_URL, SPOT, log)
    runner = web.AppRunner(app)
    await runner.setup()
    if bot:
        asyncio.create_task(bot.run())
    if LITE:        # behind Caddy (or the gateway): plain HTTP on loopback, no own certificate, no port sniffing
        await web.TCPSite(runner, os.environ.get("LOSTOS_BIND", "127.0.0.1"), HTTP_PORT).start()          # LOSTOS_BIND=0.0.0.0: a home server that speakers on the LAN reach by http
        log(f"{'tenant ' + str(TENANT_ID) if TENANT else 'lite server'}: http://127.0.0.1:{HTTP_PORT} behind the proxy, public address {base_url}")
        GLOBALS.update(tts=tts, stt=stt, base_url=base_url)
        await run_speaker(stt, tts, base_url)
        return
    INNER_HTTP = HTTP_PORT - 1                 # the plain server listens inside (loopback); the public port below tells TLS from plain HTTP by the first byte
    await web.TCPSite(runner, "127.0.0.1", INNER_HTTP).start()
    if HTTPS_PORT:
        try:
            import ssl, certs
            crt, key, _ca = certs.ensure(set(certs.lan_ips()) | {my_ip})
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(crt, key)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_alpn_protocols(["http/1.1"])                 # say it plainly: some TLS scanners of antiviruses choke on a server that names no protocol
            await web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=ctx).start()
            HTTPS.update(on=True, port=HTTPS_PORT)
            log(f"panel over TLS: https://{my_ip}:{HTTPS_PORT}/ (the CA to install on the phone: {base_url}/lostos-ca.crt)")
        except Exception as e:
            log(f"https is off: {e!r}")
    async def sniff_front(reader, writer):
        """The public port: a browser that goes to https://host:8765 (or upgrades http to https by itself) gets TLS, everything else plain HTTP."""
        w2 = None
        try:
            first = await asyncio.wait_for(reader.read(1), 15)
            if not first:
                writer.close()
                return
            tls = first[0] == 0x16 and HTTPS["on"]
            r2, w2 = await asyncio.open_connection("127.0.0.1", HTTPS_PORT if tls else INNER_HTTP)
            w2.write(first)

            async def pipe(src, dst):
                try:
                    while True:
                        data = await src.read(65536)
                        if not data:
                            break
                        dst.write(data)
                        await dst.drain()
                except Exception:
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:
                        pass
            await asyncio.gather(pipe(reader, w2), pipe(r2, writer))
        except Exception:
            for w in (writer, w2):
                try:
                    if w:
                        w.close()
                except Exception:
                    pass

    await asyncio.start_server(sniff_front, "0.0.0.0", HTTP_PORT)
    log(f"serving answers at {base_url}/tts/...")
    GLOBALS.update(tts=tts, stt=stt, base_url=base_url)
    await run_speaker(stt, tts, base_url)


if __name__ == "__main__":
    asyncio.run(main())
