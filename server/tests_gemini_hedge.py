"""The Gemini request is not waited for alone: when the first model is silent the next one gets the same question (voice_server._gemini_hedged). Run from the server folder."""
import asyncio
import os
import sys
import time

os.environ["LOSTOS_MODE"] = "tenant"
os.environ["LOSTOS_LITE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_server as v

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


def fake(script):
    """script: model -> (delay, result or an exception); counts the calls"""
    calls = []

    async def post(s, model, key, body):
        calls.append(model)
        delay, res = script[model]
        await asyncio.sleep(delay)
        if isinstance(res, Exception):
            raise res
        return model, res
    return post, calls


async def main():
    v.GEMINI_HEDGE_S = 0.3
    v.log = lambda *a: None
    v.journal = lambda *a: None
    # 1. the first model answers at once: the second is never asked
    v._gemini_post, calls = fake({"a": (0.0, {"ok": "a"}), "b": (0.0, {"ok": "b"})})
    t = time.time()
    m, d = await v._gemini_hedged(None, ["a", "b"], "k", {})
    check("a quick first model answers alone", m == "a" and calls == ["a"], calls)
    # 2. the first model is silent: the second is asked after the hedge time and wins, the silent one is cancelled
    v._gemini_post, calls = fake({"a": (5.0, {"ok": "a"}), "b": (0.0, {"ok": "b"})})
    t = time.time()
    m, d = await v._gemini_hedged(None, ["a", "b"], "k", {})
    took = time.time() - t
    check("a silent first model: the second answers after the hedge time, not after the timeout", m == "b" and 0.25 < took < 1.0, (m, took))
    # 3. the first model fails at once: the second is asked at once
    v._gemini_post, calls = fake({"a": (0.0, RuntimeError("HTTP 429")), "b": (0.1, {"ok": "b"})})
    t = time.time()
    m, d = await v._gemini_hedged(None, ["a", "b"], "k", {})
    check("a failed first model: the second is asked at once", m == "b" and time.time() - t < 0.28, (m, time.time() - t))
    # 4. both fail: the error comes out (the caller then tries another service)
    v._gemini_post, calls = fake({"a": (0.0, RuntimeError("HTTP 500")), "b": (0.0, RuntimeError("HTTP 503"))})
    try:
        await v._gemini_hedged(None, ["a", "b"], "k", {})
        check("both fail: an error", False)
    except RuntimeError as e:
        check("both fail: an error, no hang", "HTTP" in str(e), e)
    # 5. the slow first model finishes first although the second was started: the first good answer wins
    v._gemini_post, calls = fake({"a": (0.5, {"ok": "a"}), "b": (2.0, {"ok": "b"})})
    m, d = await v._gemini_hedged(None, ["a", "b"], "k", {})
    check("the first good answer wins even when the second was already started", m == "a", m)
    # 6. no task is left running
    await asyncio.sleep(0.1)
    left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    check("nothing is left running", not left, left)
    # 7. the services that failed lately go last
    v.brains.key_of = lambda p: "k"
    v.BRAIN_DOWN.clear()
    check("order without failures: the chosen one first", v.brain_order("gemini")[0] == "gemini")
    v.BRAIN_DOWN["gemini"] = time.time() + 60
    order = v.brain_order("gemini")
    check("a service that failed lately goes last", order[-1] == "gemini" and order[0] != "gemini", order)

    # 8. a slow chosen service hands over to the next one after BRAIN_SOFT_S
    v.BRAIN_DOWN.clear()
    v.BRAIN_SOFT_S = 0.3
    v.brains.ORDER = ("custom", "gemini")
    v.brains.key_of = lambda p: "k"

    async def slow_then_fast(who):
        if who == "custom":
            await asyncio.sleep(5)
            return "slow answer"
        return "fast answer"
    v.ask_provider = slow_then_fast
    t = time.time()
    reply, who = await v.ask_with_fallback("custom")
    check("a service that is too slow is replaced by the next one", reply == "fast answer" and who == "gemini" and time.time() - t < 1.0, (reply, who, time.time() - t))
    check("... and goes to the end of the queue for a while", v.brain_order("custom")[-1] == "custom", v.brain_order("custom"))
    # 9. a slow service that has already run a command is waited for (a replacement would run it again)
    v.BRAIN_DOWN.clear()

    async def slow_with_tool(who):
        if who == "custom":
            await asyncio.sleep(0.1)
            v.TURN_TOOLS["n"] += 1
            await asyncio.sleep(0.6)
            return "done with the command"
        return "must not be used"
    v.ask_provider = slow_with_tool
    reply, who = await v.ask_with_fallback("custom")
    check("a service that already ran a command is not replaced", (reply, who) == ("done with the command", "custom"), (reply, who))
    # 10. a failing service: the next answers at once; the last one failing raises
    v.BRAIN_DOWN.clear()

    async def broken(who):
        if who == "custom":
            raise RuntimeError("HTTP 500")
        return "backup"
    v.ask_provider = broken
    t = time.time()
    reply, who = await v.ask_with_fallback("custom")
    check("a failed service: the next answers at once", (reply, who) == ("backup", "gemini") and time.time() - t < 0.2, (reply, who))
    v.BRAIN_DOWN.clear()

    async def all_broken(who):
        raise RuntimeError("HTTP 500 " + who)
    v.ask_provider = all_broken
    try:
        await v.ask_with_fallback("custom")
        check("every service failed: an error", False)
    except RuntimeError as e:
        check("every service failed: an error", "HTTP 500" in str(e), e)

    # 11. the journal tells what really happened: an error, a skip, a slow one
    lines = []
    v.journal = lambda text, *a: lines.append(text)
    v.BRAIN_DOWN.clear()
    v.ask_provider = broken
    await v.ask_with_fallback("custom")
    check("the journal says why a service failed", any("ошибка, HTTP 500" in x for x in lines), lines)
    lines.clear()
    v.ask_provider = slow_then_fast
    v.BRAIN_DOWN["custom"] = time.time() + 100          # failed a moment ago: it is not asked at all
    await v.ask_with_fallback("custom")
    check("a skipped service is called skipped, not 'did not answer'", any("пропущена" in x for x in lines) and not any("не ответила" in x for x in lines), lines)
    lines.clear()
    v.BRAIN_DOWN.clear()
    await v.ask_with_fallback("custom")
    check("a slow service is called slow", any("нет ответа за" in x for x in lines), lines)

asyncio.run(main())
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
