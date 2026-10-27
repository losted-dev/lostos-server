"""Checks the built-in (system) plugins: the packages are valid, the owner can switch only the ones that allow it, and the services
that other code asks about (service_on) follow the switches. No speaker, no microphone.
Run: python tests_system.py   (uses its own state file)"""
import asyncio
import os
import sys
import tempfile

import plugins

failed = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


class Cli:
    """The connection to the speaker: remembers what was told to it."""
    def __init__(self):
        self.sent = []

    async def execute_service(self, svc, data):
        self.sent.append(data["command"])


class Stub:
    def __init__(self):
        self.said = []
        self.cli = Cli()
        self.PANEL = {"cli": self.cli}
        self.state_services = {"assistant": object()}

    def log(self, text):
        pass

    def notify(self, *a, **k):
        pass

    async def alice_api_tts(self, text):
        self.said.append(text)
        return {"pcm_b64": "AAAA", "rate": 48000}

    async def alice_api_stt(self, params):
        return {"text": "привет"}


async def main():
    plugins.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
    srv = Stub()
    mgr = plugins.Manager(srv)
    mgr.scan()
    srv.PLUGINS = mgr
    ids = {"ru.lostos.llm", "ru.lostos.alice-api", "ru.lostos.airplay", "ru.lostos.spotify"}
    check("the four packages are read", ids <= set(mgr.items))
    check("no package was refused", not [b for b in mgr.bad if b["folder"].startswith("ru.lostos.") and b["folder"].split(".")[-1] in ("llm", "alice-api", "airplay", "spotify")])
    check("all of them are system plugins", all(mgr.items[i].m["system"] for i in ids))

    check("on by default: llm", mgr.service_on("llm"))
    check("on by default: alice-api", mgr.service_on("alice-api"))
    check("the llm is switchable and alice-api too", mgr.items["ru.lostos.llm"].m["switchable"] and mgr.items["ru.lostos.alice-api"].m["switchable"])
    air = mgr.items["ru.lostos.airplay"]
    check("AirPlay is on while the speaker says nothing", mgr.enabled(air))
    check("AirPlay switches off: the speaker is told", await mgr.set_enabled("ru.lostos.airplay", False) and srv.cli.sent == ["plugin disable ru.lostos.airplay"] and not mgr.enabled(air))
    mgr.device["ru.lostos.airplay"] = "off"
    check("... and the page says it is off", not mgr.page("ru.lostos.airplay")["enabled"] and not mgr.page("ru.lostos.airplay")["running"])
    check("AirPlay switches on: the speaker is told", await mgr.set_enabled("ru.lostos.airplay", True) and srv.cli.sent[-1] == "plugin enable ru.lostos.airplay" and mgr.enabled(air))
    check("Spotify switches off too", await mgr.set_enabled("ru.lostos.spotify", False) and srv.cli.sent[-1] == "plugin disable ru.lostos.spotify")
    await mgr.set_enabled("ru.lostos.spotify", True)
    srv.PANEL = {}
    check("with no speaker connected the switch is refused", not await mgr.set_enabled("ru.lostos.airplay", False) and mgr.enabled(air))
    srv.PANEL = {"cli": srv.cli}

    check("the llm switches off", await mgr.set_enabled("ru.lostos.llm", False) and not mgr.service_on("llm"))
    check("... and the other service is not touched", mgr.service_on("alice-api"))
    check("the llm switches on", await mgr.set_enabled("ru.lostos.llm", True) and mgr.service_on("llm"))
    check("a system plugin cannot be removed", not await mgr.remove("ru.lostos.llm"))

    page = mgr.page("ru.lostos.llm")
    check("the page of the llm says what it uses", [q["id"] for q in page["requires"]] == ["ru.lostos.alice-api"])
    page = mgr.page("ru.lostos.alice-api")
    check("the page of alice-api says who uses it", [q["id"] for q in page["used_by"]] == ["ru.lostos.llm"])
    check("the readme comes with the package", "Alice API" in page["readme"])
    check("the logo is served", mgr.asset_path("ru.lostos.alice-api", "logo.png") is not None)
    check("the speaker plugin has no program to download", mgr.page("ru.lostos.airplay")["device_permissions"] and mgr.items["ru.lostos.airplay"].m["device"]["binary"] is None)
    mgr.device["ru.lostos.airplay"] = "running"
    check("the state of a speaker plugin comes from the speaker", mgr.is_running(mgr.items["ru.lostos.airplay"]) and mgr.page("ru.lostos.airplay")["device_state"] == "работает")

    # alice-api for the plugins that have the permission alice.api
    probe = plugins.Plugin({"id": "ru.test.probe", "name": "probe", "permissions": ["alice.api"], "system": False, "builtin": None, "switchable": False,
                            "device": None, "requires": [], "side": "server", "tile": None}, ".", "user")
    mgr.items[probe.id] = probe
    mgr.state[probe.id] = {"enabled": True, "granted": ["alice.api"]}
    res = await mgr._call(probe, "alice.tts", {"text": "Привет"})
    check("alice.tts works for a plugin with the permission", res.get("rate") == 48000 and srv.said == ["Привет"])
    check("alice.stt works too", (await mgr._call(probe, "alice.stt", {"pcm_b64": "AAAA"}))["text"] == "привет")
    await mgr.set_enabled("ru.lostos.alice-api", False)
    try:
        await mgr._call(probe, "alice.tts", {"text": "Привет"})
        check("alice.tts is refused while alice-api is off", False)
    except RuntimeError:
        check("alice.tts is refused while alice-api is off", True)
    await mgr.set_enabled("ru.lostos.alice-api", True)
    mgr.state[probe.id]["granted"] = []
    try:
        await mgr._call(probe, "alice.tts", {"text": "Привет"})
        check("alice.tts is refused without the permission", False)
    except PermissionError:
        check("alice.tts is refused without the permission", True)

    # tiles: they belong to plugins, a plugin that is off has none, and the edit mode remembers whose they were
    import tiles as home_tiles
    layout = home_tiles.HomeTiles(os.path.join(tempfile.mkdtemp(), "tiles.json"))
    mgr.device["ru.lostos.airplay"] = "running"
    live = mgr.tiles()
    check("the tile of AirPlay and Spotify come from the plugins", {t["id"] for t in live} >= {"ru.lostos.airplay", "ru.lostos.spotify"} and all(t["shelf"] == "sources" for t in live if t["id"].startswith("ru.lostos.")))
    view = layout.view(live, mgr.tile_gone_reason)
    check("nothing is missing at first", view["ghosts"] == [])
    await mgr.set_enabled("ru.lostos.airplay", False)
    check("a plugin that is off shows no tile", "ru.lostos.airplay" not in {t["id"] for t in mgr.tiles()})
    view = layout.view(mgr.tiles(), mgr.tile_gone_reason)
    ghost = next((g for g in view["ghosts"] if g["id"] == "ru.lostos.airplay"), None)
    check("... the edit mode still knows whose tile it was", ghost is not None and ghost["plugin"] == "AirPlay 2" and ghost["reason"] == "плагин отключён")
    check("the owner puts a tile away and orders the rest", layout.set_layout(["ru.lostos.spotify"], {"sources": ["ru.lostos.spotify", "ru.lostos.airplay"]}))
    await mgr.set_enabled("ru.lostos.airplay", True)
    mgr.device["ru.lostos.airplay"] = "running"
    again = layout.view(mgr.tiles(), mgr.tile_gone_reason)
    check("the layout is kept", [t["id"] for t in again["tiles"] if t["shelf"] == "sources"] == ["ru.lostos.spotify", "ru.lostos.airplay"] and next(t for t in again["tiles"] if t["id"] == "ru.lostos.spotify")["hidden"])
    check("a removed plugin says it is removed", mgr.tile_gone_reason("ru.nobody.here") == "плагин удалён")
    system = [{"id": "sys.now", "plugin_id": "lostos", "plugin": "lostOS", "shelf": "now", "title": "Сейчас играет", "icon": "music", "static": True},
              {"id": "sys.dialogs", "plugin_id": "lostos", "plugin": "lostOS", "shelf": "page", "title": "Что она слышала", "icon": "note", "static": True}]
    layout.set_layout(["sys.dialogs"], {"now": ["sys.now"]})
    shown = layout.view(system + mgr.tiles(), mgr.tile_gone_reason)
    check("the parts of lostOS itself can be put away like any tile", next(t for t in shown["tiles"] if t["id"] == "sys.dialogs")["hidden"] and shown["ghosts"] == [])
    stat = [{"id": "sys.cpu", "plugin_id": "lostos", "plugin": "lostOS", "shelf": "now", "title": "Процессор", "icon": "cpu", "off_by_default": True}]
    first = layout.view(stat, mgr.tile_gone_reason)
    check("a system widget that waits for the owner is hidden when it first appears", first["tiles"][0]["hidden"])
    layout.set_layout([], {})
    check("... and stays on once the owner turned it on", not layout.view(stat, mgr.tile_gone_reason)["tiles"][0]["hidden"])
    check("a place holder can be forgotten", layout.forget("ru.lostos.airplay") and "ru.lostos.airplay" not in layout.seen)

    ok, err = plugins._valid_manifest({"id": "ru.evil.pkg", "version": "1.0.0", "name": "x", "author": "y", "builtin": "llm"}, "user", ".")
    check("a user package cannot claim to be built in", ok is None and "builtin" in err)


asyncio.run(main())
print("\n" + ("ALL OK" if not failed else "FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
