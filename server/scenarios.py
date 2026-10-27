"""Scenarios: named sets of actions (lights, Home Assistant scenes, speech, music, volume, a sleep fade), started from the panel, by a
phrase (the language model maps what was said to the name), or on a schedule. Stored in scenarios.json; run by the voice server."""
import datetime as dt
import json
import os
import re
import uuid

import shared

FILE = os.environ.get("LOSTOS_SCEN_FILE") or os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or os.path.dirname(os.path.abspath(__file__)), "scenarios.json")
ICONS = ("home", "moon", "sun", "film", "party", "away", "work", "star")
MUSIC = ("play", "pause", "next", "previous")


def _s(v, n=120) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def clean_action(a: dict):
    """One action checked and reduced to the known fields (None when it is not usable)."""
    t = a.get("type")
    if t == "light" and "." in _s(a.get("entity")):
        out = {"type": "light", "entity": _s(a["entity"], 80), "on": bool(a.get("on", True))}
        if a.get("brightness_pct") is not None:
            out["brightness_pct"] = max(1, min(100, int(a["brightness_pct"])))
        if a.get("rgb"):
            out["rgb"] = [max(0, min(255, int(x))) for x in a["rgb"][:3]]
        return out
    if t == "ha" and "." in _s(a.get("entity")):
        return {"type": "ha", "entity": _s(a["entity"], 80), "service": "turn_off" if a.get("service") == "turn_off" else "turn_on"}
    if t == "say" and _s(a.get("text")):
        return {"type": "say", "text": _s(a["text"], 300)}
    if t == "music" and a.get("action") in MUSIC:
        return {"type": "music", "action": a["action"]}
    if t == "volume":
        return {"type": "volume", "level": max(0, min(100, int(a.get("level", 30))))}
    if t == "fade":
        return {"type": "fade", "minutes": max(1, min(180, int(a.get("minutes", 30)))), "smooth": bool(a.get("smooth", True))}
    if t == "wait":
        return {"type": "wait", "seconds": max(1, min(600, int(a.get("seconds", 5))))}
    return None


class Scenarios:
    def __init__(self):
        self.items: list = []
        self.fired: dict = {}
        try:
            self.items = json.load(open(FILE, encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def save(self):
        json.dump(self.items, open(FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        shared.mark()

    def reload(self):
        self.items[:] = json.load(open(FILE, encoding="utf-8"))

    def upsert(self, d: dict) -> dict:
        sc = {"id": _s(d.get("id"), 12) or uuid.uuid4().hex[:8], "name": _s(d.get("name"), 40) or "Сценарий",
              "icon": d.get("icon") if d.get("icon") in ICONS else "star", "enabled": bool(d.get("enabled", True)),
              "phrases": [p for p in (_s(x, 60) for x in (d.get("phrases") or [])[:8]) if p],
              "schedule": None, "actions": [a for a in (clean_action(x) for x in (d.get("actions") or [])[:30] if isinstance(x, dict)) if a]}
        s = d.get("schedule")
        if isinstance(s, dict) and re.fullmatch(r"\d{1,2}:\d{2}", _s(s.get("time"), 5)):
            h, m = (int(x) for x in s["time"].split(":"))
            if h < 24 and m < 60:
                sc["schedule"] = {"time": f"{h:02d}:{m:02d}", "days": sorted({int(x) for x in s.get("days", range(7)) if 0 <= int(x) <= 6}) or list(range(7))}
        for i, old in enumerate(self.items):
            if old["id"] == sc["id"]:
                self.items[i] = sc
                break
        else:
            self.items.append(sc)
        self.save()
        return sc

    def delete(self, sid: str):
        self.items = [i for i in self.items if i["id"] != sid]
        self.save()

    def get(self, key: str):
        k = (key or "").strip().lower()
        return next((i for i in self.items if i["id"] == key or i["name"].lower() == k or k in [p.lower() for p in i["phrases"]]), None) or \
            next((i for i in self.items if k and (k in i["name"].lower() or any(k in p.lower() for p in i["phrases"]))), None)

    def unmask(self, text: str) -> str:
        """The cloud recogniser stars out rude words ("Я н*****"): when that fits a scenario phrase, give the phrase back."""
        import re
        t = (text or "").strip().rstrip(".!?").lower()
        if "*" not in t:
            return text
        pat = re.compile("^" + re.escape(t).replace(r"\*", ".").replace(r"\ ", " ") + "$")
        for sc in self.items:
            if sc.get("enabled", True):
                for p in sc["phrases"]:
                    if len(p) == len(t) and pat.match(p.lower()):
                        return p
        return text

    def due(self, now: dt.datetime) -> list:
        """The scenarios whose time has come (each at most once in its minute)."""
        stamp = now.strftime("%Y%m%d%H%M")
        out = []
        if not shared.fires():                                                  # only the main station of the house starts the scheduled ones
            return out
        for sc in self.items:
            s = sc.get("schedule")
            if sc.get("enabled", True) and s and s["time"] == now.strftime("%H:%M") and now.weekday() in s["days"] and self.fired.get(sc["id"]) != stamp:
                self.fired[sc["id"]] = stamp
                out.append(sc)
        return out

    def describe(self) -> str:
        """For the language model: what the user can trigger by speaking."""
        lines = []
        for sc in self.items:
            if sc.get("enabled", True):
                lines.append(f"«{sc['name']}»" + (f" (фразы: {', '.join(sc['phrases'])})" if sc["phrases"] else ""))
        return "; ".join(lines)
