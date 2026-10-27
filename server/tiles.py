"""The widgets (tiles) of the home page.

A tile belongs to a plugin: it exists while the plugin is on and is gone when the plugin is off or removed. This module keeps what the
plugins cannot: which tiles the owner hid, in which order the owner put them, and which tiles used to be on the page. The last one is
for the edit mode of the panel: a tile that is gone is shown there as a place holder that says whose tile it was and why it is not there.
"""
import json
import os

import shared

MAX_IDS = 200


class HomeTiles:
    def __init__(self, path: str):
        self.path = path
        self.seen: dict = {}                    # tile id -> what it looked like: {plugin_id, plugin, shelf, title, icon, accent, tint}
        self.hidden: list = []                  # tile ids the owner put away
        self.order: dict = {}                   # shelf -> [tile ids]
        try:
            d = json.load(open(path, encoding="utf-8"))
            self.seen, self.hidden, self.order = d.get("seen", {}), d.get("hidden", []), d.get("order", {})
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            json.dump({"seen": self.seen, "hidden": self.hidden, "order": self.order}, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            shared.mark()
        except OSError:
            pass

    def reload(self):
        d = json.load(open(self.path, encoding="utf-8"))
        self.seen, self.hidden, self.order = d.get("seen", {}), d.get("hidden", []), d.get("order", {})

    def view(self, live: list, why) -> dict:
        """live: the tiles that exist now (dicts with id, plugin_id, plugin, shelf, title, icon, ...). why(plugin_id) -> the reason a tile of this
        plugin is not there. -> {"tiles": ordered, each with "hidden"; "ghosts": the tiles that used to be and are not now}."""
        changed = False
        for t in live:
            if t.get("off_by_default") and t["id"] not in self.seen and t["id"] not in self.hidden:   # a system widget that waits for the owner to turn it on
                self.hidden.append(t["id"])
                changed = True
            rec = {k: str(t.get(k, "")) for k in ("plugin_id", "plugin", "shelf", "title", "icon", "accent", "tint")}
            if self.seen.get(t["id"]) != rec:
                self.seen[t["id"]] = rec
                changed = True
        if changed:
            self._save()

        def place(t):
            ids = self.order.get(t["shelf"], [])
            return ids.index(t["id"]) if t["id"] in ids else len(ids)       # a new tile goes to the end

        tiles = sorted(live, key=place)
        for t in tiles:
            t["hidden"] = t["id"] in self.hidden
        here = {t["id"] for t in live}
        ghosts = [{"id": i, **rec, "reason": why(rec["plugin_id"])} for i, rec in self.seen.items() if i not in here]
        return {"tiles": tiles, "ghosts": ghosts}

    def set_layout(self, hidden, order) -> bool:
        ok = (isinstance(hidden, list) and isinstance(order, dict) and len(hidden) <= MAX_IDS and all(isinstance(x, str) and len(x) < 100 for x in hidden)
              and all(isinstance(k, str) and isinstance(v, list) and len(v) <= MAX_IDS and all(isinstance(x, str) and len(x) < 100 for x in v) for k, v in order.items()))
        if not ok:
            return False
        self.hidden, self.order = list(dict.fromkeys(hidden)), {k: list(dict.fromkeys(v)) for k, v in order.items()}
        self._save()
        return True

    def forget(self, tid: str) -> bool:
        """The owner does not want the place holder of a tile that is gone any more."""
        if tid not in self.seen:
            return False
        self.seen.pop(tid)
        self.hidden = [i for i in self.hidden if i != tid]
        self.order = {k: [i for i in v if i != tid] for k, v in self.order.items()}
        self._save()
        return True
