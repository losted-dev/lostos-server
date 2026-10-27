"""What the processes of one household's stations share. Every station of a household has a process of its own (its own speaker, player, dialog), but the household's
settings, scenarios, reminders, widgets and keys are ONE set of files in its folder. A process that changes one of them calls mark(); the others notice within a few seconds
(watch) and read the files again. Alarms, timers and reminders belong to the station that made them (the field "station"); the ones without an owner (made before there
were several stations) ring at the main station of the household.

Outside a household process (a home server, a single-person server) LOSTOS_TENANT_DIR is empty and everything here does nothing special: fires() is always true."""
import asyncio
import glob
import os

DIR = os.environ.get("LOSTOS_TENANT_DIR") or ""
STATION = os.environ.get("LOSTOS_STATION", "")
PRIMARY = os.environ.get("LOSTOS_PRIMARY", "1") != "0"
_ME = os.path.join(DIR, f".sync.{STATION or os.getpid()}") if DIR else ""
_seen = 0


def mark():
    """I changed a shared file: the other stations of the house read it again."""
    if not _ME:
        return
    try:
        with open(_ME, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def fires() -> bool:
    """May this process ring what has no owner (scenarios on a schedule, old reminders)? Only the main station of a household may; a process without a station never."""
    return not DIR or (bool(STATION) and PRIMARY)


def owns(item: dict) -> bool:
    """Does this reminder, timer or alarm ring here?"""
    if not DIR:
        return True
    owner = item.get("station", "")
    return owner == STATION if owner else fires()


def _others() -> int:
    best = 0
    for p in glob.glob(os.path.join(DIR, ".sync.*")):
        if p != _ME:
            try:
                best = max(best, os.stat(p).st_mtime_ns)
            except OSError:
                pass
    return best


async def watch(reload, every: float = 2.0):
    """reload() is called when another process of the household has marked a change."""
    global _seen
    if not DIR:
        return
    _seen = _others()
    while True:
        await asyncio.sleep(every)
        m = _others()
        if m > _seen:
            _seen = m
            try:
                reload()
            except Exception as e:                                           # a half written file: the next mark reads it again
                print(f"shared: reload: {e!r}", flush=True)
