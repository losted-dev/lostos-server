"""MediaHub (server side): one view of what plays and one set of commands for every source (Spotify, AirPlay, Bluetooth, ...)
The panel, Home Assistant and plugins use /api/v1/media; the sources themselves stay behind this module.
Capabilities are what a source can do, so a client never has to know which source plays (the heart is only for Spotify, etc.)"""

SOURCES = {
    "spotify":   {"name": "Spotify",   "caps": ["play", "pause", "next", "previous", "volume", "like", "cover", "seek"]},
    "airplay":   {"name": "AirPlay",   "caps": ["play", "pause", "next", "previous", "volume", "cover"]},
    "bluetooth": {"name": "Bluetooth", "caps": ["play", "pause", "next", "previous", "volume"]},
}
_ID = {"Spotify": "spotify", "AirPlay": "airplay", "Bluetooth": "bluetooth"}


def view(now: dict, volume, airplay_on: bool, bt: dict) -> dict:
    """The unified state. `now` is the panel's aggregated "what plays" (empty when nothing)"""
    sid = _ID.get(now.get("source", ""), "")
    caps = list(SOURCES[sid]["caps"]) if sid else []
    return {
        "source": sid or None,
        "playing": bool(now.get("playing")),
        "title": now.get("title", ""), "artist": now.get("artist", ""), "cover": now.get("art", ""),
        "progress": now.get("progress", 0), "duration": now.get("duration", 0), "liked": now.get("liked"),
        "volume": volume, "capabilities": caps,
        "sources": [{"id": k, "name": v["name"], "active": k == sid, "capabilities": v["caps"],
                     "enabled": (airplay_on if k == "airplay" else bt.get("on") if k == "bluetooth" else True)} for k, v in SOURCES.items()],
    }


async def command(m, cmd: str, args: dict) -> str:
    """cmd: play | pause | toggle | next | previous | volume (level 0-100) | like | unlike. Raises ValueError for the unknown or unsupported."""
    if cmd in ("next", "previous", "play", "pause", "playpause"):
        svc = m.state_services.get("assistant")
        if svc and m.MEDIA["t"]:                          # the speaker's own media command: no Web API, no server-side guessing
            await m.PANEL["cli"].execute_service(svc, {"command": "media " + cmd})
            return "ok"
        return await m.run_tool("music_control", {"action": cmd})
    if cmd == "volume":
        return await m.run_tool("set_volume", {"level": int(args.get("level", 30))})
    if cmd == "seek":                                   # args: ms (position in the track); Spotify only for now
        svc = m.state_services.get("assistant")
        if not svc:
            raise ValueError("колонка не на связи")
        await m.PANEL["cli"].execute_service(svc, {"command": f"spotify_seek {max(0, int(args.get('ms', 0)))}"})
        return "ok"
    if cmd in ("like", "unlike"):
        return await m.run_tool("spotify_like", {"action": cmd}, trusted=True)
    raise ValueError("unknown command: " + cmd)


EQ_BANDS_HZ = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


async def set_eq(m, on: bool, preamp: float, gains: list) -> str:
    """The equalizer of the speaker: ten bands (+-15 dB) and a preamp. The speaker keeps it in its eq.conf; the ALSA plugin reads it."""
    if len(gains) != 10:
        raise ValueError("нужно десять значений")
    svc = m.state_services.get("assistant")
    if not svc:
        raise ValueError("колонка не на связи")
    g = ",".join(f"{max(-15.0, min(15.0, x)):.1f}" for x in gains)
    await m.PANEL["cli"].execute_service(svc, {"command": f"eq {1 if on else 0} {max(-15.0, min(15.0, preamp)):.1f} {g}"})
    return "ok"
