"""Home Assistant for the assistant (REST API): the list of devices with their rooms, and service calls.
.env: HA_URL=http://192.168.1.10:8123  HA_TOKEN=<a long-lived access token: HA profile -> Security -> Create token>
SPEAKER_ROOM=<the name of the area the Station stands in, as in HA, e.g. Зал>"""
import json
import time

import aiohttp

from spotify_client import _env

DOMAINS = ("light", "switch", "fan", "cover", "climate", "media_player", "scene", "script", "input_boolean", "humidifier", "vacuum")
# what the voice may call (nothing like locks, alarm panels or arbitrary services)
SERVICES = {"turn_on", "turn_off", "toggle", "open_cover", "close_cover", "stop_cover", "set_cover_position",
            "set_temperature", "set_hvac_mode", "volume_set", "media_play", "media_pause", "media_next_track",
            "media_previous_track", "set_percentage", "start", "return_to_base"}
TEMPLATE = ("{% for s in states if s.domain in " + json.dumps(list(DOMAINS)) + " %}"
            "{{ s.entity_id }}|{{ s.name }}|{{ area_name(s.entity_id) or '' }}|{{ s.state }}\n{% endfor %}")


# the devices of the panel's "smart home" page: state + what each can do (as JSON, rooms from HA's areas)
CONTROL = ("light", "switch", "fan", "cover", "input_boolean", "climate")
TEMPLATE_DEV = ("{% set ns = namespace(l=[]) %}{% for s in states if s.domain in " + json.dumps(list(CONTROL)) + " %}{% set a = s.attributes %}"
                "{% set ns.l = ns.l + [{'id': s.entity_id, 'name': s.name, 'area': area_name(s.entity_id) or '', 'state': s.state, 'bri': a.get('brightness'),"
                " 'rgb': a.get('rgb_color'), 'modes': a.get('supported_color_modes'), 'kelvin': a.get('color_temp_kelvin'), 'kmin': a.get('min_color_temp_kelvin'),"
                " 'kmax': a.get('max_color_temp_kelvin'), 'pos': a.get('current_position'), 'temp': a.get('temperature'), 'cur': a.get('current_temperature')}] %}"
                "{% endfor %}{{ ns.l | tojson }}")


FORWARD = None      # a lite server sets a function that returns the loopback address of the speaker's forward "ha" (link.py), or ""


class HomeAssistant:
    def __init__(self):
        self._dev_cache: list = []
        self._dev_at = 0.0
        self.entities: dict = {}            # entity_id -> {name, area, state}
        self.updated = 0.0
        self.error = ""

    @property
    def url(self) -> str:
        """A server on a VDS reaches the home Home Assistant through the speaker (the link's forward "ha"); at home the address is in the settings."""
        return ((FORWARD() if FORWARD else "") or _env("HA_URL") or "").rstrip("/")

    @property
    def token(self) -> str:
        return _env("HA_TOKEN")

    @property
    def room(self) -> str:
        return _env("SPEAKER_ROOM")

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _headers(self):
        return {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}

    async def refresh(self, force: bool = False) -> bool:
        if not self.configured:
            return False
        if not force and time.time() - self.updated < 60:
            return True
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(self.url + "/api/template", headers=self._headers(), json={"template": TEMPLATE}) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    text = await r.text()
        except Exception as e:
            self.error = str(e)
            return False
        ents = {}
        skip = [x.strip().lower() for x in (_env("HA_IGNORE") or "stantsiia,remote_access").split(",") if x.strip()]   # the Station's own entities and noise
        for line in text.splitlines():
            f = line.split("|")
            if len(f) >= 4 and not any(x in f[0].lower() for x in skip):
                ents[f[0]] = {"name": f[1], "area": f[2], "state": f[3]}
        self.entities, self.updated, self.error = ents, time.time(), ""
        return True

    async def devices(self, max_age: float = 2.0) -> list:
        """Lights, switches, fans, covers, climate with their rooms, states and abilities (cached for a couple of seconds)."""
        if not self.configured:
            return []
        if time.time() - self._dev_at < max_age:
            return self._dev_cache
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(self.url + "/api/template", headers=self._headers(), json={"template": TEMPLATE_DEV}) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    items = json.loads(await r.text())
        except Exception as e:
            self.error = str(e)
            return self._dev_cache
        skip = [x.strip().lower() for x in (_env("HA_IGNORE") or "stantsiia,remote_access").split(",") if x.strip()]
        out = []
        for d in items:
            if any(x in d["id"].lower() for x in skip):
                continue
            modes = d.get("modes") or []
            d["color"] = any(m in modes for m in ("rgb", "rgbw", "rgbww", "hs", "xy"))
            d["ct"] = "color_temp" in modes
            d["dim"] = d["id"].startswith("light.") and any(m != "onoff" for m in modes) or (d["id"].startswith("light.") and not modes)
            d["bri_pct"] = round(100 * d["bri"] / 255) if d.get("bri") is not None else None
            out.append(d)
        self._dev_cache, self._dev_at, self.error = out, time.time(), ""
        return out

    async def scenes(self) -> list:
        """Scenes and scripts (what a scenario can start)."""
        if not self.configured:
            return []
        tpl = ("{% for s in states if s.domain in ['scene','script'] %}{{ s.entity_id }}|{{ s.name }}|{{ area_name(s.entity_id) or '' }}\n{% endfor %}")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(self.url + "/api/template", headers=self._headers(), json={"template": tpl}) as r:
                    text = await r.text() if r.status == 200 else ""
        except Exception:
            return []
        return [{"id": f[0], "name": f[1], "area": f[2]} for f in (ln.split("|") for ln in text.splitlines()) if len(f) >= 3]
    def context(self, limit: int = 3500) -> str:
        """The devices by room, for the language model (kept short)."""
        if not self.entities:
            return ""
        rooms: dict = {}
        for eid, e in self.entities.items():
            if eid.startswith(("script.", "scene.")) and not e["area"]:
                key = "без комнаты"
            else:
                key = e["area"] or "без комнаты"
            rooms.setdefault(key, []).append(f"{e['name']} [{eid}] {e['state']}")
        out = []
        for room in sorted(rooms, key=lambda r: (r != self.room, r)):
            out.append(f"{room}{' (комната колонки)' if room == self.room else ''}: " + "; ".join(rooms[room]))
        return "\n".join(out)[:limit]

    def room_of(self, eid: str) -> str:
        return self.entities.get(eid, {}).get("area", "")

    async def call(self, entity_ids: list, service: str, data: dict) -> str:
        if service not in SERVICES:
            return f"сервис {service} не разрешён"
        if not self.configured:
            return "Home Assistant не подключён"
        by_domain: dict = {}
        for eid in entity_ids:
            if "." not in eid:
                return f"непонятное устройство {eid}"
            dom = eid.split(".")[0]
            if dom not in DOMAINS:
                return f"с устройствами типа {dom} я не работаю"
            by_domain.setdefault(dom, []).append(eid)
        done = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            for dom, ids in by_domain.items():
                body = dict(data, entity_id=ids)
                if dom != "light":                             # light-only fields
                    for k in ("brightness_pct", "color_name", "rgb_color", "color_temp_kelvin"):
                        body.pop(k, None)
                async with s.post(f"{self.url}/api/services/{dom}/{service}", headers=self._headers(), json=body) as r:
                    if r.status != 200:
                        return f"Home Assistant ответил ошибкой {r.status}: {(await r.text())[:100]}"
                done += ids
        self.updated = 0.0                                       # states changed: reread soon
        self._dev_at = 0.0
        return "готово: " + ", ".join(self.entities.get(i, {}).get("name", i) for i in done)