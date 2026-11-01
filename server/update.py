"""Update check: compares VERSION with the latest GitHub release of LOSTOS_REPO ("owner/name") and keeps its changelog for the panel.
Nothing is installed by itself: the panel only tells that a new version exists (and what is in it)."""
import json
import os
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
INFO = {"checked": 0.0, "latest": "", "notes": "", "url": "", "error": ""}


def version() -> str:
    try:
        return open(os.path.join(HERE, "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        return "0"


def _key(v: str):
    return tuple(int(x) for x in v.lstrip("v").split("-")[0].split(".") if x.isdigit())


def newer() -> bool:
    return bool(INFO["latest"]) and _key(INFO["latest"]) > _key(version())


async def check(repo: str, force: bool = False) -> dict:
    """At most once in six hours (GitHub's anonymous limit is small)."""
    if not repo or (not force and time.time() - INFO["checked"] < 6 * 3600):
        return INFO
    INFO["checked"] = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers={"User-Agent": "lostOS"}) as ses:
            async with ses.get(f"https://api.github.com/repos/{repo}/releases/latest") as r:
                if r.status != 200:
                    INFO["error"] = f"GitHub: {r.status}"
                    return INFO
                j = await r.json()
        INFO.update(latest=j.get("tag_name", ""), notes=(j.get("body") or "")[:4000], url=j.get("html_url", ""), error="")
    except Exception as e:
        INFO["error"] = str(e)[:100]
    return INFO


FW_REPO = os.environ.get("LOSTOS_FW_REPO", "losted-dev/lostos")       # where the speakers take lostOS from (controller.cpp kUpdateRepo)
_FW: dict = {}                                                        # tag -> {"notes", "sizes": {asset: bytes}, "url", "t"}


async def speaker_release(tag: str) -> dict:
    """What the panel shows about a lostOS release a speaker offers: its notes and the size of each daemon. One request per tag
    (a failure is retried after ten minutes); the speaker itself reads only the redirect of releases/latest."""
    c = _FW.get(tag)
    if c and (c["ok"] or time.time() - c["t"] < 600):
        return c
    c = {"ok": False, "notes": "", "sizes": {}, "url": "", "t": time.time()}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers={"User-Agent": "lostOS"}) as ses:
            async with ses.get(f"https://api.github.com/repos/{FW_REPO}/releases/tags/{tag}") as r:
                if r.status == 200:
                    j = await r.json()
                    c.update(ok=True, notes=(j.get("body") or "")[:4000], url=j.get("html_url", ""),
                             sizes={a.get("name", ""): int(a.get("size") or 0) for a in j.get("assets") or []})
    except Exception:
        pass
    _FW[tag] = c
    return c
