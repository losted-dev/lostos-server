"""lostOS plugin SDK: a few lines to talk to the server (standard library only).

    from lostos_sdk import call, on, run

    @on("media")                       # a track started or stopped (needs events.subscribe and media.read)
    def changed(now):
        call("tile.set", title="Играет", value=now.get("title", "")[:12])

    call("events.subscribe")
    run()                              # keeps the plugin alive and delivers the events

    @handle("assistant.heard")         # a phrase was said to the assistant (needs assistant.intercept)
    def heard(p):                      # p = {"text": ..., "who": ...}; return {"claim": True, "reply": "..."} to answer it yourself
        return {"claim": "тест" in p["text"].lower(), "reply": "Это ответил плагин."}

    @handle("brain.respond")           # you are the assistant's brain (needs brain.replace), see docs/plugins.md
    def think(p):                      # p = {"text", "note", "system", "history", "who", "tools"}; return the text to say
        return "Ответ"

    # a command for the assistant (needs assistant.tools): every brain sees it, the built-in Gemini too
    call("tools.register", name="coin_flip", description="Подбросить монетку", parameters={"type": "object", "properties": {}})
    @handle("tool.call")               # p = {"name", "args"}; return the result as text for the model
    def tool(p):
        return "орёл"

    @handle("stt.transcribe")          # you are the recognizer (needs speech.stt): p = {"pcm_b64", "rate": 16000, "language"}, 16-bit mono
    def stt(p):
        return {"text": "что услышал"}

    @handle("tts.synthesize")          # you are the voice (needs speech.tts): return {"wav_b64": ...} or {"pcm_b64": ..., "rate": 24000}
    def tts(p):
        return {"pcm_b64": "...", "rate": 24000}

call(method, **params) returns the result or raises PluginError (for example when the permission is missing)."""
import itertools
import json
import queue
import sys
import threading

_ids = itertools.count(1)
_waiting: dict = {}
_events: "queue.Queue" = queue.Queue()
_handlers: dict = {}
_questions: dict = {}
_lock = threading.Lock()


class PluginError(Exception):
    pass


def _reader():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "event" in msg or "call" in msg:
            _events.put(msg)
        elif msg.get("id") in _waiting:
            slot = _waiting[msg["id"]]
            slot["msg"] = msg
            slot["ev"].set()
    _events.put(None)                                       # the server closed the pipe: the plugin ends


threading.Thread(target=_reader, daemon=True).start()


def call(method: str, timeout: float = 15.0, **params):
    rid = next(_ids)
    slot = {"ev": threading.Event(), "msg": None}
    _waiting[rid] = slot
    with _lock:
        sys.stdout.write(json.dumps({"id": rid, "method": method, "params": params}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    if not slot["ev"].wait(timeout):
        _waiting.pop(rid, None)
        raise PluginError("no answer from the server")
    _waiting.pop(rid, None)
    msg = slot["msg"]
    if "error" in msg:
        raise PluginError(msg["error"])
    return msg.get("result")


def on(event: str):
    def deco(fn):
        _handlers[event] = fn
        return fn
    return deco


def handle(method: str):
    """A question from the server that must be answered (needs assistant.intercept or brain.replace): the function gets the
    parameters as a dict and returns the answer (anything JSON). Questions are answered in threads, so a slow brain does not block events."""
    def deco(fn):
        _questions[method] = fn
        return fn
    return deco


def _answer(msg: dict):
    fn = _questions.get(msg.get("method"))
    try:
        if fn is None:
            raise PluginError("no handler for " + str(msg.get("method")))
        reply = {"reply_to": msg["call"], "result": fn(msg.get("params") or {})}
    except Exception as e:
        reply = {"reply_to": msg["call"], "error": repr(e)}
    with _lock:
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def run():
    while True:
        msg = _events.get()
        if msg is None:
            return
        if "call" in msg:
            threading.Thread(target=_answer, args=(msg,), daemon=True).start()
            continue
        fn = _handlers.get(msg["event"])
        if fn:
            try:
                fn(msg.get("data"))
            except Exception as e:                          # one bad handler must not end the plugin
                try:
                    call("log", text=f"handler error: {e!r}")
                except PluginError:
                    pass
