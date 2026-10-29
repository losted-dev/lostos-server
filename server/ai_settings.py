"""The settings of the assistant's brain (Panel > LLM > Настройки ИИ): which model of which service thinks, how it answers, what it is told about the person's life.

  GET  /api/v1/ai   -> {"provider", "models": {provider: {"list": [{id, title, note}], "chosen": id}}, "temperature", "max_tokens", "style", "persona", "persona_default", "contexts": [{id, title, about, on}], "preview": "<what is sent>"}
  POST /api/v1/ai   {"provider": "gemini"} {"model": {"provider": "gemini", "id": "gemini-flash-latest"}} {"temperature": 0.5} {"max_tokens": 400} {"style": "short"}
                    {"persona": "text"} (empty = the standard one) {"context": {"id": "home", "on": false}}
Everything is kept in settings.json of the person (SETTINGS["ai"]); the keys of the services are in the vault, not here."""
from aiohttp import web

import brains

CONTEXTS = [
    ("time", "Дата и время", "день, время, часть суток: она знает, который час"),
    ("playing", "Что играет", "Spotify, AirPlay и Bluetooth на колонке: что сейчас звучит и играет ли"),
    ("reminders", "Будильники и таймеры", "что поставлено и сколько осталось"),
    ("scenarios", "Сценарии", "твои сценарии, чтобы она запускала их по похожей фразе"),
    ("home", "Умный дом", "устройства Home Assistant по комнатам и их состояние"),
    ("music", "Моя музыка", "любимые исполнители и треки из Spotify для точного написания названий"),
]
CTX_IDS = [c[0] for c in CONTEXTS]
# The models a person usually wants: a short note about speed and price, no numbers that go stale. "Своя" is any name the service accepts.
CATALOG = {
    "gemini": [("gemini-flash-lite-latest", "Flash Lite", "быстро, почти бесплатно (по умолчанию)"), ("gemini-3.1-flash-lite", "3.1 Flash Lite", "быстро, дёшево"),
               ("gemini-flash-latest", "Flash", "умнее, медленнее"), ("gemini-pro-latest", "Pro", "самая умная, медленно и дороже")],
    "claude": [("claude-haiku-5-5", "Haiku 5.5", "быстро, дёшево"), ("claude-sonnet-5-5", "Sonnet 5.5", "умнее, дороже (по умолчанию)"), ("claude-opus-5-5", "Opus 5.5", "самая умная, дорого")],
    "deepseek": [("deepseek-chat", "DeepSeek Chat", "очень дёшево (по умолчанию)"), ("deepseek-reasoner", "DeepSeek Reasoner", "думает дольше, дёшево")],
    "openai": [("gpt-4.1-mini", "GPT-4.1 mini", "быстро, дёшево (по умолчанию)"), ("gpt-4.1", "GPT-4.1", "умнее, дороже"), ("gpt-4o-mini", "GPT-4o mini", "быстро, дёшево")],
}
DEFAULTS = {"temperature": 0.7, "max_tokens": 700, "persona": "", "model": {}, "off": []}


def set_custom(ai: dict, cb) -> str:
    """Puts the address and the model of the custom service into the "ai" settings; -> "" or what is wrong (the first setup uses it too)."""
    cb = cb if isinstance(cb, dict) else {}
    base = str(cb.get("base", "")).strip().rstrip("/")[:300]
    if base and not (base.startswith("https://") or base.startswith("http://")):
        return "адрес должен начинаться с https://"
    ai["custom"] = {"base": base, "model": str(cb.get("model", "")).strip()[:120]}
    return ""


def cfg(settings: dict) -> dict:
    """The effective settings (what is saved, with the defaults for the rest)."""
    c = dict(DEFAULTS)
    c.update(settings.get("ai") or {})
    return c


def ctx_on(settings: dict, name: str) -> bool:
    return name not in cfg(settings)["off"]


def model_of(settings: dict, provider: str, default: str = "") -> str:
    return (cfg(settings)["model"] or {}).get(provider) or default


def install(app: web.Application, m):
    S = m.SETTINGS

    async def get(req):
        c = cfg(S)
        sel = S.get("brain", "")
        chosen = brains.pick(sel)
        providers = []
        brains.SETTINGS = lambda: S
        for p in brains.listing(sel, with_custom=True):
            pid = p["id"]
            default = {"gemini": "gemini-flash-lite-latest", "claude": "claude-sonnet-5-5"}.get(pid) or p["model"]
            providers.append({**p, "default": default, "chosen": (c["model"] or {}).get(pid, ""),
                              "list": [{"id": i, "title": t, "note": n} for i, t, n in CATALOG.get(pid, [])]})
        return web.json_response({
            "provider": chosen, "providers": providers, "custom": brains.custom(), "temperature": c["temperature"], "max_tokens": c["max_tokens"], "style": S.get("answer_style", "normal") or "normal",
            "persona": c["persona"], "persona_default": m.default_persona(),
            "contexts": [{"id": i, "title": t, "about": a, "on": i not in c["off"], "available": i != "home" or bool(m.HA.configured)} for i, t, a in CONTEXTS],
            "preview": m.system_prompt()[:6000],
        })

    async def post(req):
        try:
            b = await req.json()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        c = dict(S.get("ai") or {})
        if "provider" in b:
            if b["provider"] not in brains.PROVIDERS:
                return web.json_response({"ok": False, "error": "такого сервиса нет"}, status=400)
            S["brain"] = b["provider"]
        if "custom" in b:                                                       # the address and the model of the custom service (its key is a secret of the vault)
            err = set_custom(c, b["custom"])
            if err:
                return web.json_response({"ok": False, "error": err}, status=400)
        if "model" in b:
            mb = b["model"] or {}
            if mb.get("provider") not in brains.PROVIDERS:
                return web.json_response({"ok": False, "error": "такого сервиса нет"}, status=400)
            mid = str(mb.get("id", "")).strip()[:80]
            if mid and not all(ch.isalnum() or ch in "-._:/" for ch in mid):
                return web.json_response({"ok": False, "error": "в названии модели только латиница, цифры и - . _ : /"}, status=400)
            models = dict(c.get("model") or {})
            if mid:
                models[mb["provider"]] = mid
            else:
                models.pop(mb["provider"], None)                              # empty = the default model of the service
            c["model"] = models
            m.GEMINI_BEST["m"] = None
        for key, lo, hi in (("temperature", 0.0, 1.5), ("max_tokens", 100, 2000)):
            if key in b:
                try:
                    v = float(b[key])
                except (TypeError, ValueError):
                    return web.json_response({"ok": False}, status=400)
                c[key] = round(min(hi, max(lo, v)), 2) if key == "temperature" else int(min(hi, max(lo, v)))
        if "style" in b:
            if b["style"] not in ("short", "normal", "detailed"):
                return web.json_response({"ok": False}, status=400)
            S["answer_style"] = b["style"]
        if "persona" in b:
            c["persona"] = str(b["persona"]).strip()[:6000]
        if "context" in b:
            cx = b["context"] or {}
            if cx.get("id") not in CTX_IDS:
                return web.json_response({"ok": False, "error": "такого контекста нет"}, status=404)
            off = set(c.get("off") or [])
            (off.discard if cx.get("on") else off.add)(cx["id"])
            c["off"] = sorted(off)
        S["ai"] = c
        m.save_settings()
        await m.refresh_context()                                             # the preview shows the new state at once
        return await get(req)

    app.router.add_get("/api/v1/ai", get)
    app.router.add_post("/api/v1/ai", post)
