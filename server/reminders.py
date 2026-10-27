"""Timers, alarms and reminders: stored in reminders.json, checked once a second by the server (the speaker only rings /
shows the countdown, the schedule itself lives on the PC and survives restarts of both)."""
import datetime as dt
import json
import os
import time
import uuid

import shared

FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or os.path.dirname(os.path.abspath(__file__)), "reminders.json")
WEEKDAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


class Reminders:
    def __init__(self):
        self.items: list = []
        try:
            self.items = json.load(open(FILE, encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def save(self):
        json.dump(self.items, open(FILE, "w", encoding="utf-8"), ensure_ascii=False)
        shared.mark()

    def reload(self):
        """Another station of the house changed the file."""
        self.items[:] = json.load(open(FILE, encoding="utf-8"))

    # ---- creating
    def add_timer(self, seconds: int, label: str = "") -> dict:
        it = {"id": uuid.uuid4().hex[:8], "kind": "timer", "label": label, "total": int(seconds), "at": time.time() + int(seconds)}
        return self._put(it)

    def add_alarm(self, hhmm: str, date: str = "", repeat: str = "once", label: str = "", snooze_min: int = 5, enabled: bool = True) -> dict:
        it = {"id": uuid.uuid4().hex[:8], "kind": "alarm", "label": label, "time": hhmm, "repeat": repeat or "once",
              "snooze_min": max(0, min(60, int(snooze_min))), "enabled": bool(enabled)}
        it["at"] = self._next_at(it, date)
        return self._put(it)

    def set_enabled(self, rid: str, on: bool):
        """An alarm switched off stays in the list but does not ring; switched on it rings at its next moment."""
        for it in self.items:
            if it["id"] == rid and it["kind"] == "alarm":
                it["enabled"] = bool(on)
                if on:
                    it["at"] = self._next_at(it)
                self.save()
                return it
        return None

    def add_reminder(self, text: str, in_minutes: float = 0, hhmm: str = "", date: str = "", repeat: str = "once") -> dict:
        it = {"id": uuid.uuid4().hex[:8], "kind": "reminder", "text": text, "repeat": repeat or "once", "label": text[:40]}
        if in_minutes:
            it["at"] = time.time() + float(in_minutes) * 60
        else:
            it["time"] = hhmm
            it["at"] = self._next_at(it, date)
        return self._put(it)

    def add_snooze(self, alarm: dict, seconds: int = 300) -> dict:
        """The alarm rang its five minutes and nobody switched it off: it rings again."""
        it = {"id": uuid.uuid4().hex[:8], "kind": "alarm", "label": alarm.get("label", ""), "time": alarm.get("time", ""),
              "repeat": "once", "snooze": True, "tries": alarm.get("tries", 0) + 1, "at": time.time() + seconds}
        return self._put(it)

    def cancel_snooze(self):
        gone = [i for i in self.items if i.get("snooze")]
        for g in gone:
            self.items.remove(g)
        if gone:
            self.save()

    def _put(self, it: dict) -> dict:
        if shared.STATION:
            it.setdefault("station", shared.STATION)                          # it rings at the speaker that was asked (or whose panel it was made in)
        self.items.append(it)
        self.save()
        return it

    # ---- schedule maths
    @staticmethod
    def _allowed(repeat: str, day: dt.date) -> bool:
        if repeat in ("", "once", "daily"):
            return True
        if repeat == "weekdays":
            return day.weekday() < 5
        if repeat == "weekends":
            return day.weekday() >= 5
        if repeat.startswith("days:"):
            return day.weekday() in {WEEKDAYS[d] for d in repeat[5:].split(",") if d in WEEKDAYS}
        return True

    def _next_at(self, it: dict, date: str = "") -> float:
        h, m = (int(x) for x in it["time"].split(":"))
        now = dt.datetime.now()
        if date:                                             # an explicit day: only that day
            d = dt.date.fromisoformat(date)
            return dt.datetime.combine(d, dt.time(h, m)).timestamp()
        for add in range(0, 9):
            cand = dt.datetime.combine(now.date() + dt.timedelta(days=add), dt.time(h, m))
            if cand > now and self._allowed(it.get("repeat", "once"), cand.date()):
                return cand.timestamp()
        return time.time() + 86400

    # ---- what is due
    def pop_due(self) -> list:
        now = time.time()
        due = [it for it in self.items if it["at"] <= now and shared.owns(it)]
        for it in list(due):
            if it["kind"] == "alarm" and not it.get("enabled", True):           # switched off: does not ring, waits for its next day
                it["at"] = self._next_at(it)
                due.remove(it)
            elif it["kind"] != "timer" and it.get("repeat", "once") != "once":    # repeating: the next day
                it["at"] = self._next_at(it)
            else:
                self.items.remove(it)
        if due:
            self.save()
        return due

    def occurrences(self, it: dict, days: int = 40) -> list:
        """The coming moments an alarm rings (a repeating one: the next weeks): the speaker rings them on its own."""
        if it["kind"] == "alarm" and not it.get("enabled", True):
            return []
        if it.get("repeat", "once") in ("", "once"):
            return [it["at"]] if it["at"] > time.time() else []
        h, m = (int(x) for x in it["time"].split(":"))
        now = dt.datetime.now()
        out = []
        for add in range(0, days):
            cand = dt.datetime.combine(now.date() + dt.timedelta(days=add), dt.time(h, m))
            if cand > now and self._allowed(it.get("repeat", "once"), cand.date()):
                out.append(cand.timestamp())
        return out

    def cancel(self, which: str = "", kind: str = "") -> list:
        w = (which or "").strip().lower()
        gone = [it for it in self.items
                if (not kind or it["kind"] == kind) and (not w or w in ("все", "всё", "all") or w in (it.get("label", "") + it.get("text", "")).lower() or w == it["id"])]
        for it in gone:
            self.items.remove(it)
        if gone:
            self.save()
        return gone

    def describe(self, it: dict) -> str:
        when = dt.datetime.fromtimestamp(it["at"])
        if it["kind"] == "timer":
            left = max(0, int(it["at"] - time.time()))
            return f"таймер{' «' + it['label'] + '»' if it.get('label') else ''}: осталось {left // 60} мин {left % 60} с"
        repeat = it.get("repeat", "once")
        if repeat.startswith("days:"):
            names = {"пн": "Пн", "вт": "Вт", "ср": "Ср", "чт": "Чт", "пт": "Пт", "сб": "Сб", "вс": "Вс"}
            rep = ", " + ", ".join(names.get(d, d) for d in repeat[5:].split(","))
        else:
            rep = {"once": "", "daily": ", каждый день", "weekdays": ", по будням", "weekends": ", по выходным"}.get(repeat, ", " + repeat)
        what = ("повтор будильника" if it.get("snooze") else "будильник") if it["kind"] == "alarm" else "напоминание «" + it.get("text", "") + "»"
        off = " (выключен)" if it["kind"] == "alarm" and not it.get("enabled", True) else ""
        left = max(0, int(it["at"] - time.time())) // 60
        eta = f" (до него {left // 1440} д {left % 1440 // 60} ч {left % 60} мин)" if left >= 1440 else f" (до него {left // 60} ч {left % 60} мин)"
        return f"{what} на {when:%d.%m %H:%M}{rep}{off}{eta}"