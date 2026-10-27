"""Yandex Weather for the assistant: reads the public page of the configured city (WEATHER_CITY in .env, a
yandex.ru/pogoda slug such as "krasnodar") and builds the short scripted phrase Alice-style:
"Сейчас 20 градусов, ясно" / "Сейчас 12 градусов, скоро начнётся дождь"."""
import re
import time

import aiohttp

from spotify_client import _env

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
_cache = {"t": 0.0, "data": None}
LOCATION = None                  # a callable -> {"name", "lat", "lon"} or None: the city that the owner chose in the panel (it wins over WEATHER_CITY of .env)
_WORDS = {"clear": "ясно", "partly": "переменная облачность", "cloud": "пасмурно", "fog": "туман", "rain": "дождь", "snow": "снег", "storm": "гроза"}


def reset():
    _cache.update(t=0.0, data=None)
    _fc.update(t=0.0, data=None)


def _grad(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return "градусов"
    return {1: "градус", 2: "градуса", 3: "градуса", 4: "градуса"}.get(n % 10, "градусов")


async def weather() -> dict:
    """{'temp': int, 'condition': str, 'note': str, 'phrase': str, 'display': str}; cached for ten minutes."""
    if _cache["data"] and time.time() - _cache["t"] < 600:
        return _cache["data"]
    if LOCATION and LOCATION():                                     # the city of the panel: the phrase comes from the forecast (Yandex pages need their own slug of every city)
        f = await forecast()
        temp, word = f["temp"], _WORDS.get(f["kind"], "облачно")
        data = {"temp": temp, "condition": word, "note": "", "phrase": f"Сейчас {'минус ' if temp < 0 else ''}{abs(temp)} {_grad(temp)}, {word}", "display": f"{temp}°"}
        _cache.update(t=time.time(), data=data)
        return data
    city = _env("WEATHER_CITY") or "krasnodar"
    url = _env("WEATHER_URL") or f"https://yandex.ru/pogoda/{city}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru"}) as r:
            html = await r.text()
    m = re.search(r"погода сейчас:\s*(?P<cond>[^.<]+)\.[\u200c\s]*(?P<warn>[^<]*?),?\s*Температура воздуха\s*"
                  r"(?P<sign>[+\-\u2212\u2013]?)\s*(?P<n>\d+)", html)
    if not m:
        raise RuntimeError("не разобрала страницу Яндекс Погоды")
    temp = int(m["n"]) * (-1 if m["sign"] in ("-", "\u2212", "\u2013") else 1)
    cond = m["cond"].strip().lower()
    warn = re.sub(r"^сегодня\s+", "", m["warn"].strip().rstrip(",").strip(), flags=re.I).lower()
    dry = (not warn) or "не ожидается" in warn
    tail = cond if dry else warn
    word = "минус " if temp < 0 else ("плюс " if temp > 0 and False else "")
    phrase = f"Сейчас {word}{abs(temp)} {_grad(temp)}, {tail}"
    data = {"temp": temp, "condition": cond, "note": "" if dry else warn, "phrase": phrase,
            "display": f"{temp}°"}
    _cache.update(t=time.time(), data=data)
    return data


# ---- the picture for the weather widget of the home page: the city, the weather now and the next three parts of the day (Open-Meteo, no key)
_wmo = {0: "clear", 1: "clear", 2: "partly", 3: "cloud", 45: "fog", 48: "fog", 51: "rain", 53: "rain", 55: "rain", 56: "rain", 57: "rain",
        61: "rain", 63: "rain", 65: "rain", 66: "rain", 67: "rain", 71: "snow", 73: "snow", 75: "snow", 77: "snow", 80: "rain", 81: "rain",
        82: "rain", 85: "snow", 86: "snow", 95: "storm", 96: "storm", 99: "storm"}
_PARTS = [("Ночь", 3), ("Утро", 9), ("День", 15), ("Вечер", 21)]
_geo = {}
_fc = {"t": 0.0, "data": None}


def _kind(code: int) -> str:
    return _wmo.get(int(code), "cloud")


async def _where(session) -> tuple:
    """(city name, latitude, longitude): WEATHER_LAT / WEATHER_LON when set, else the place that Open-Meteo finds for WEATHER_CITY."""
    loc = LOCATION() if LOCATION else None
    if loc:
        return loc["name"], float(loc["lat"]), float(loc["lon"])
    lat, lon = _env("WEATHER_LAT"), _env("WEATHER_LON")
    slug = (_env("WEATHER_CITY") or "krasnodar").strip()
    if lat and lon:
        return slug.replace("-", " ").title(), float(lat), float(lon)
    if slug not in _geo:
        async with session.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": slug.replace("-", " "), "count": 1, "language": "ru", "format": "json"}) as r:
            res = (await r.json()).get("results") or []
        if not res:
            raise RuntimeError("город не найден: " + slug)
        _geo[slug] = (res[0]["name"], res[0]["latitude"], res[0]["longitude"])
    return _geo[slug]


async def forecast() -> dict:
    """{'city', 'temp', 'kind', 'night', 'parts': [{'name', 'kind', 'night', 'temp'} x3]}; kind: clear partly cloud fog rain snow storm. Cached ten minutes."""
    import datetime
    if _fc["data"] and time.time() - _fc["t"] < 600:
        return _fc["data"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        city, lat, lon = await _where(s)
        async with s.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code,is_day", "hourly": "temperature_2m,weather_code,is_day",
                "forecast_days": 3, "timezone": "auto"}) as r:
            d = await r.json()
    cur, hr = d["current"], d["hourly"]
    now = datetime.datetime.fromisoformat(cur["time"])
    at = {datetime.datetime.fromisoformat(t): i for i, t in enumerate(hr["time"])}
    cands = []
    for day in range(3):
        for name, hour in _PARTS:
            when = datetime.datetime.combine(now.date() + datetime.timedelta(days=day), datetime.time(hour))
            if when > now and when in at:
                cands.append((when, name, at[when]))
    cands.sort()
    parts = [{"name": n, "kind": _kind(hr["weather_code"][i]), "night": not hr["is_day"][i], "temp": round(hr["temperature_2m"][i])} for _, n, i in cands[:3]]
    data = {"city": city, "temp": round(cur["temperature_2m"]), "kind": _kind(cur["weather_code"]), "night": not cur["is_day"], "parts": parts}
    _fc.update(t=time.time(), data=data)
    return data
