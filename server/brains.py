"""The models the assistant can think with. Gemini and Claude are built into voice_server.py; DeepSeek and ChatGPT speak the same "chat completions" dialect,
so one function serves both (and any other service with that API: change the base address)

The owner picks one in the first-run wizard (Settings > First setup) or leaves it to the server: the first provider that has a key, in the order below.
Keys live in the vault (vault.py) and are put into the environment of the process while it is open.

ask(m, provider) answers the conversation in m.HISTORY with the commands of the assistant (tools) for up to three rounds, like ask_gemini.
ping(provider) checks a key with one small request (the wizard does it before it says "works")"""
import json
import os

import aiohttp

ORDER = ("gemini", "claude", "deepseek", "openai", "custom")
SETTINGS = lambda: {}                  # the settings of the person (voice_server gives them): the address and the model of the custom service live there, its key in the vault
PROVIDERS = {
    "gemini": {"title": "Gemini", "env": "GEMINI_API_KEY", "hint": "Google AI Studio даёт бесплатный ключ. Быстрая модель flash-lite, хорошо работает с командами колонки",
               "url": "https://aistudio.google.com/apikey"},
    "claude": {"title": "Claude", "env": "ANTHROPIC_API_KEY", "hint": "Ключ на console.anthropic.com, платно. Отвечает аккуратно, но пока без команд колонки: только разговор",
               "url": "https://console.anthropic.com/settings/keys"},
    "deepseek": {"title": "DeepSeek", "env": "DEEPSEEK_API_KEY", "base": "https://api.deepseek.com", "model_env": "DEEPSEEK_MODEL", "model": "deepseek-chat",
                 "hint": "Ключ на platform.deepseek.com, очень дёшево. Понимает команды колонки", "url": "https://platform.deepseek.com/api_keys"},
    "openai": {"title": "ChatGPT", "env": "OPENAI_API_KEY", "base": "https://api.openai.com/v1", "model_env": "OPENAI_MODEL", "model": "gpt-4.1-mini",
               "hint": "Ключ на platform.openai.com, платно (нужно пополнить счёт). Понимает команды колонки", "url": "https://platform.openai.com/api-keys"},
    "custom": {"title": "Свой сервис", "env": "CUSTOM_API_KEY", "base": "", "model": "",
               "hint": "Любой сервис с OpenAI-совместимым API (купленный ключ, прокси, свой сервер): адрес, ключ и название модели", "url": ""},
}


def custom() -> dict:
    """The address and the model of the custom service, as the person set them in Panel > LLM > Настройки ИИ."""
    c = (SETTINGS().get("ai") or {}).get("custom") or {}
    return {"base": str(c.get("base", "")).strip().rstrip("/"), "model": str(c.get("model", "")).strip()}


def base_of(provider: str) -> str:
    return custom()["base"] if provider == "custom" else PROVIDERS[provider]["base"].rstrip("/")


def model_for(provider: str) -> str:
    """The model a request goes to: the choice in the panel, else the default of the service."""
    import ai_settings
    p = PROVIDERS[provider]
    if provider == "custom":
        return custom()["model"]
    return ai_settings.model_of(SETTINGS(), provider) or os.environ.get(p.get("model_env", ""), "") or p["model"]


def key_of(provider: str) -> str:
    """The key of the service; the custom one counts only when its address and model are set too (without them there is nobody to ask)."""
    p = PROVIDERS.get(provider)
    if provider == "custom" and not (custom()["base"] and custom()["model"]):
        return ""
    return (os.environ.get(p["env"], "") if p else "").strip()


def pick(selected: str = "") -> str:
    """The provider that answers: the chosen one if it has a key, else the first with a key, else ""."""
    for name in ([selected] if selected in PROVIDERS else []) + list(ORDER):
        if key_of(name):
            return name
    return ""


def listing(selected: str = "", with_custom: bool = False) -> list:
    """The services the panel offers. The custom one only where its address and model can be typed (the page of the brain settings and the first setup)."""
    chosen = pick(selected)
    return [{"id": n, "title": p["title"], "hint": p["hint"], "url": p["url"], "env": p["env"], "has_key": bool(key_of(n)), "selected": n == chosen,
             "model": custom()["model"] if n == "custom" else os.environ.get(p.get("model_env", ""), "") or p.get("model", "")}
            for n, p in PROVIDERS.items() if with_custom or n != "custom"]


def _tools_openai(tools: list) -> list:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            for t in tools]


async def ask(m, provider: str) -> str:
    """The answer of an OpenAI-compatible service to the conversation of the assistant, with the commands it may run. Raises on any failure."""
    p = PROVIDERS[provider]
    base = base_of(provider)
    import ai_settings
    settings = getattr(m, "SETTINGS", {})
    aic = ai_settings.cfg(settings)
    model = model_for(provider)
    if not base or not model:
        raise RuntimeError(f"{p['title']}: не задан адрес или модель")
    msgs = [{"role": "system", "content": m.system_prompt()}] + [{"role": h["role"], "content": h["content"]} for h in m.HISTORY]
    tools = _tools_openai(m.tools_neutral()) if m.ACTIONS else []
    async with aiohttp.ClientSession() as s:
        for _ in range(3):                                           # model -> command -> model ... at most three rounds
            body = {"model": model, "messages": msgs, "max_tokens": int(aic["max_tokens"]), "temperature": float(aic["temperature"])}
            if tools:
                body["tools"] = tools
            async with s.post(base + "/chat/completions", json=body, headers={"Authorization": "Bearer " + key_of(provider)},
                              timeout=aiohttp.ClientTimeout(total=25)) as r:
                data = await r.json(content_type=None)
                if r.status in (400, 422) and tools:                  # a proxy that does not know the commands (tools): talk without them rather than not at all
                    m.log(f"{p['title']}: HTTP {r.status} with tools, trying without")
                    tools = []
                    body.pop("tools", None)
                    async with s.post(base + "/chat/completions", json=body, headers={"Authorization": "Bearer " + key_of(provider)},
                                      timeout=aiohttp.ClientTimeout(total=25)) as r2:
                        data = await r2.json(content_type=None)
                        if r2.status != 200:
                            raise RuntimeError(f"{p['title']} HTTP {r2.status}: {str((data.get('error') or {}).get('message', data))[:140]}")
                        r = r2
                if r.status != 200:
                    raise RuntimeError(f"{p['title']} HTTP {r.status}: {str((data.get('error') or {}).get('message', data))[:140]}")
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                if text:
                    return text
                raise RuntimeError("empty answer")
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                m.log(f"tool call: {fn.get('name')}({args})")
                result = await m.run_tool(fn.get("name", ""), args)
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": str(result)})
    raise RuntimeError("too many tool rounds")


async def ping(provider: str) -> str:
    """"" when the key works, else a short reason (a rejected key, no money on the account, no network)"""
    p = PROVIDERS.get(provider)
    key = key_of(provider)
    if not p or not key:
        return "ключ не сохранён"
    try:
        async with aiohttp.ClientSession() as s:
            if provider == "gemini":
                url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent"
                async with s.post(url, params={"key": key}, json={"contents": [{"role": "user", "parts": [{"text": "Скажи: ок"}]}], "generationConfig": {"maxOutputTokens": 8}},
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
                    return "" if r.status == 200 else str((data.get("error") or {}).get("message", data))[:140]
            if provider == "claude":
                async with s.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                                  json={"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": 8, "messages": [{"role": "user", "content": "Скажи: ок"}]},
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
                    return "" if r.status == 200 else str((data.get("error") or {}).get("message", data))[:140]
            model = model_for(provider)
            if not base_of(provider) or not model:
                return "не задан адрес или модель"
            async with s.post(base_of(provider) + "/chat/completions", headers={"Authorization": "Bearer " + key},
                              json={"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "Скажи: ок"}]}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
                return "" if r.status == 200 else str((data.get("error") or {}).get("message", data))[:140]
    except (aiohttp.ClientError, ValueError, TimeoutError) as e:
        return f"нет связи с сервисом: {e.__class__.__name__}"
