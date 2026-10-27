"""Tests of the model providers (DeepSeek / ChatGPT dialect) and of the first-run wizard API, against fake services (no network).
Run from the project folder:  venv\\Scripts\\python.exe -I server\\tests_wizard.py"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp
from aiohttp import web

import brains
import setup_wizard

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


async def main():
    seen = []

    async def completions(req):
        body = await req.json()
        seen.append((req.headers.get("Authorization"), body))
        if req.headers.get("Authorization") != "Bearer GOOD":
            return web.json_response({"error": {"message": "Incorrect API key"}}, status=401)
        msgs = body["messages"]
        if not any(x["role"] == "tool" for x in msgs) and body.get("tools"):
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call1", "type": "function", "function": {"name": "set_volume", "arguments": "{\"level\": 40}"}}]}}]})
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Готово, громкость сорок."}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
    brains.PROVIDERS["openai"]["base"] = base
    brains.PROVIDERS["deepseek"]["base"] = base

    # ---- which model answers
    for p in brains.PROVIDERS.values():
        os.environ.pop(p["env"], None)
    check("no keys, nobody answers", brains.pick() == "")
    os.environ["DEEPSEEK_API_KEY"] = "GOOD"
    check("the only key answers", brains.pick() == "deepseek")
    os.environ["GEMINI_API_KEY"] = "g"
    check("the first provider with a key answers by default", brains.pick() == "gemini")
    check("the owner's choice wins", brains.pick("deepseek") == "deepseek")
    check("a choice without a key is ignored", brains.pick("openai") == "gemini")
    lst = brains.listing("deepseek")
    check("the list says which have keys and which is chosen", [p["id"] for p in lst if p["has_key"]] == ["gemini", "deepseek"] and [p["id"] for p in lst if p["selected"]] == ["deepseek"])

    # ---- the conversation with commands
    ran = []

    async def run_tool(name, args, trusted=False):
        ran.append((name, args))
        return "ok"

    m = types.SimpleNamespace(system_prompt=lambda: "Ты колонка.", HISTORY=[{"role": "user", "content": "громкость сорок"}], ACTIONS={"set_volume": 1},
                              tools_neutral=lambda: [{"name": "set_volume", "description": "громкость", "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}}}],
                              run_tool=run_tool, log=lambda *a: None)
    text = await brains.ask(m, "deepseek")
    check("the model's command is run and its answer comes back", text == "Готово, громкость сорок." and ran == [("set_volume", {"level": 40})], (text, ran))
    first = seen[0][1]
    check("the system prompt, the history and the commands are sent", first["messages"][0]["role"] == "system" and first["messages"][1]["content"] == "громкость сорок"
          and first["tools"][0]["function"]["name"] == "set_volume" and first["model"] == "deepseek-chat")
    check("the second round carries the result of the command", any(x["role"] == "tool" and x["tool_call_id"] == "call1" for x in seen[1][1]["messages"]))
    os.environ["DEEPSEEK_API_KEY"] = "BAD"
    try:
        await brains.ask(m, "deepseek")
        check("a rejected key raises", False)
    except RuntimeError as e:
        check("a rejected key raises with the reason", "Incorrect API key" in str(e), e)
    check("ping says the key is bad", "Incorrect" in await brains.ping("deepseek"))
    os.environ["DEEPSEEK_API_KEY"] = "GOOD"
    check("ping says nothing when the key works", await brains.ping("deepseek") == "")
    os.environ.pop("OPENAI_API_KEY", None)
    check("ping without a key says so", "не сохранён" in await brains.ping("openai"))

    # ---- a custom OpenAI-compatible service: the address and the model are the person's, the key is a secret
    os.environ.pop("CUSTOM_API_KEY", None)
    brains.SETTINGS = lambda: {}
    check("a custom service without an address does not count", brains.key_of("custom") == "" and "custom" not in [p["id"] for p in brains.listing()])
    brains.SETTINGS = lambda: {"ai": {"custom": {"base": base + "/", "model": "my-model"}}}
    os.environ["CUSTOM_API_KEY"] = "GOOD"
    check("with an address, a model and a key it is ready and listed for the settings page", brains.key_of("custom") == "GOOD" and "custom" in [p["id"] for p in brains.listing(with_custom=True)])
    check("it is not offered in the first setup", "custom" not in [p["id"] for p in brains.listing()])
    seen.clear()
    text = await brains.ask(m, "custom")
    check("the request goes to the person's address with his model", text == "Готово, громкость сорок." and seen[0][1]["model"] == "my-model", seen[:1])
    check("its ping works", await brains.ping("custom") == "")
    os.environ["CUSTOM_API_KEY"] = "BAD"
    check("a rejected key of the custom service says why", "Incorrect" in await brains.ping("custom"))
    os.environ.pop("CUSTOM_API_KEY", None)
    brains.SETTINGS = lambda: {}

    # ---- the wizard API
    settings = {}
    saved = []
    tools_decl = [{"name": "search_web", "description": "Поиск в интернете. Подробнее."}, {"name": "set_volume", "description": "Громкость."}]
    off_of = lambda: set(settings.get("tools_off", []))
    vault = types.SimpleNamespace(locked=False, get=lambda k, d="": "")
    link = types.SimpleNamespace(listing=lambda tg=0, role="owner": [{"id": "aaa3", "name": "Мини", "confirmed": True, "online": True, "press": 0, "code": "AAA3", "button": True}])
    tz_calls = []

    async def timezone_changed():
        tz_calls.append(1)

    mod = types.SimpleNamespace(timezone_changed=timezone_changed, SETTINGS=settings, save_settings=lambda: saved.append(dict(settings)), tools_off=off_of, TOOLS=[{"functionDeclarations": tools_decl}], PLUGINS=None,
                                HA=types.SimpleNamespace(configured=False, error=""), VOICES=types.SimpleNamespace(people={"Аня": 1}), LINK=link)
    app2 = web.Application()
    setup_wizard.install(app2, mod, vault, "https://example.test")
    r2 = web.AppRunner(app2)
    await r2.setup()
    s2 = web.TCPSite(r2, "127.0.0.1", 0)
    await s2.start()
    b2 = f"http://127.0.0.1:{s2._server.sockets[0].getsockname()[1]}"
    async with aiohttp.ClientSession() as http:
        async def get():
            async with http.get(b2 + "/api/v1/setup") as r:
                return await r.json()

        async def post(path, body):
            async with http.post(b2 + path, json=body) as r:
                return r.status, await r.json()

        d = await get()
        check("the state lists the station, the models, the commands", d["available"] and d["station"]["confirmed"] and len(d["brain"]["providers"]) == 4
              and [c["name"] for c in d["commands"]] == ["search_web", "set_volume"] and d["search"]["on"] is True and d["voices"] == 1 and d["done"] is False)
        check("the Spotify callback of this server is shown", d["spotify"]["redirect"] == "https://example.test/spotify/callback")
        st, _ = await post("/api/v1/setup", {"tool": "search_web", "on": False})
        d = await get()
        check("a command can be switched off", st == 200 and d["search"]["on"] is False and settings["tools_off"] == ["search_web"] and saved)
        st, _ = await post("/api/v1/setup", {"tool": "search_web", "on": True})
        check("and on again", (await get())["search"]["on"] is True and settings["tools_off"] == [])
        st, _ = await post("/api/v1/setup", {"tool": "nonsense", "on": False})
        check("an unknown command is refused", st == 404)
        st, _ = await post("/api/v1/setup", {"brain": "openai"})
        check("the model is chosen", st == 200 and settings["brain"] == "openai")
        st, _ = await post("/api/v1/setup", {"brain": "nope"})
        check("an unknown model is refused", st == 400)
        st, _ = await post("/api/v1/setup", {"style": "short"})
        check("the answer style is saved", st == 200 and (await get())["style"] == "short")
        st, _ = await post("/api/v1/setup", {"style": "rude"})
        check("a bad style is refused", st == 400)

        async def fake_city(name):
            if name == "Нигде":
                raise ValueError("нет")
            return {"name": "Краснодар, Россия", "lat": 45.04, "lon": 38.98, "tz": "Europe/Moscow"}

        setup_wizard.find_city = fake_city
        st, j = await post("/api/v1/setup", {"city": "Краснодар"})
        check("the city is looked up and saved", st == 200 and settings["weather"]["lat"] == 45.04 and j["city"]["name"].startswith("Краснодар"))
        check("the time zone of the city goes to the clock of the server and the speaker", settings["weather"]["tz"] == "Europe/Moscow" and tz_calls == [1])
        st, _ = await post("/api/v1/setup", {"city": "Нигде"})
        check("an unknown city is refused", st == 404)
        st, _ = await post("/api/v1/setup", {"done": True})
        check("the wizard can be finished", st == 200 and (await get())["done"] is True)
        os.environ["GEMINI_API_KEY"] = "g"
        st, j = await post("/api/v1/setup/test", {"provider": "deepseek"})
        check("the key test answers", st == 200 and j["ok"] is True, j)
        st, j = await post("/api/v1/setup/test", {"provider": "zzz"})
        check("a test of an unknown model is refused", st == 400)

    await r2.cleanup()
    await runner.cleanup()
    print("ALL PASS" if OK else "SOME FAILED")
    sys.exit(0 if OK else 1)


asyncio.run(main())
