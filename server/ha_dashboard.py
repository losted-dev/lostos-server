"""The "Колонка" dashboard in Home Assistant: the speaker's media player (cover, title, artist, the controls) and its volume.
Usage: python ha_dashboard.py [apply | delete]
The media player is the DLNA one that lostOS announces (media_player.iandeks_stantsiia_mini_3): it shows what AirPlay, Spotify and
Bluetooth play. The dashboard is a separate one in the sidebar, nothing else in Home Assistant is touched."""
import asyncio, os, sys
import aiohttp

env = {}
for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8"):
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.strip().split("=", 1)
        env[k] = v
URL, TOKEN = env["HA_URL"].rstrip("/"), env["HA_TOKEN"]

MP = "media_player.iandeks_stantsiia_mini_3"
PATH = "kolonka-music"
CONFIG = {
    "title": "Колонка",
    "views": [{
        "title": "Колонка", "path": "kolonka", "icon": "mdi:speaker",
        "cards": [{
            "type": "vertical-stack",
            "cards": [
                {"type": "media-control", "entity": MP},
                {"type": "tile", "entity": MP, "name": "Громкость", "features": [{"type": "media-player-volume-slider"}]},
            ],
        }],
    }],
}


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "apply"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.ws_connect(URL + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            if (await ws.receive_json()).get("type") != "auth_ok":                  # not an assert: python -OO strips asserts together with the await inside
                raise RuntimeError("Home Assistant did not accept the token")
            n = 0

            async def call(t, **kw):
                nonlocal n
                n += 1
                await ws.send_json({"id": n, "type": t, **kw})
                while True:
                    m = await ws.receive_json()
                    if m.get("id") == n:
                        return m

            lst = (await call("lovelace/dashboards/list")).get("result") or []
            have = next((d for d in lst if d.get("url_path") == PATH), None)
            if mode == "delete":
                if have:
                    print("delete:", (await call("lovelace/dashboards/delete", dashboard_id=have["id"])).get("success"))
                return
            if not have:
                m = await call("lovelace/dashboards/create", url_path=PATH, title="Колонка", icon="mdi:speaker",
                               show_in_sidebar=True, require_admin=False, mode="storage")
                print("create dashboard:", m.get("success"), m.get("error"))
            m = await call("lovelace/config/save", url_path=PATH, config=CONFIG)
            print("save card:", m.get("success"), m.get("error"))
            m = await call("lovelace/config", url_path=PATH, force=True)
            print("read back:", [c.get("type") for c in m["result"]["views"][0]["cards"][0]["cards"]])


asyncio.run(main())
