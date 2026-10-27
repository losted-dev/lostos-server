"""The link of a TENANT process to its speaker, as the gateway sees it (gateway.py owns the WebSocket of the speaker; the tenant only asks).

A tenant is the server of ONE person: it has his keys, settings, scenarios and the voice of his speaker. The speaker's WebSocket ends at the gateway, which opens loopback ports for the
station (API 6053, AirPlay 7000, covers 49153, forwards such as Home Assistant). This object asks the gateway for them every two seconds and looks like link.Link for the rest of the
server code: active(), wait_active(), api_key_of(), listing(), forward_url(), stations.

The gateway also hands over, over the same internal channel and only while the speaker is connected, the key that opens the vault of the person and the key of the speaker's API."""
import asyncio
import base64
import json
import os
import time

import aiohttp


class Conn:
    def __init__(self, d: dict):
        self.id = d["id"]
        self.name = d.get("name", "")
        self.local = {int(k): v for k, v in d.get("ports", {}).items()}
        self.forwards = dict(d.get("forwards", {}))
        self.forward_addrs = dict(d.get("forward_addrs", {}))


class RemoteLink:
    def __init__(self, gateway: str, tenant: int, key: str, vault=None, log=print, station: str = ""):
        self.gateway, self.tenant, self.key, self.vault, self.log = gateway.rstrip("/"), tenant, key, vault, log
        self.station = station                    # the station this process serves ("" = a house that has none yet)
        self.online: list = []
        self.conn = None
        self.rows: list = []
        self.stations: dict = {}
        self.changed = asyncio.Event()
        self.last_ok = 0.0
        self._api_key = ""
        self._opened_for = None

    # ---- the interface the server code expects
    def active(self):
        return self.conn

    async def wait_active(self) -> Conn:
        while True:
            if self.conn:
                return self.conn
            self.changed.clear()
            await self.changed.wait()

    def api_key_of(self, sid: str) -> str:
        return (self.vault.get(f"station:{sid}:api_key") if self.vault is not None else "") or self._api_key

    def listing(self, tg: int = 0, role: str = "owner", raw: bool = False) -> list:
        return list(self.rows)

    def forward_addr(self, name: str) -> str:
        c = self.conn
        return c.forward_addrs.get(name, "") if c else ""

    def forward_url(self, name: str) -> str:
        c = self.conn
        return f"http://127.0.0.1:{c.forwards[name]}" if c and name in c.forwards else ""

    def visible_to(self, r, tg, role) -> bool:
        return True

    # ---- the asking
    async def _fetch(self, http: aiohttp.ClientSession) -> dict:
        async with http.get(f"{self.gateway}/internal/station", params={"tenant": str(self.tenant), "station": self.station}, headers={"X-Lostos-Key": self.key}, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                raise RuntimeError(f"gateway {r.status}")
            return await r.json()

    def _apply(self, d: dict):
        st = d.get("station")
        self.online = d.get("online", [])
        self.rows = d.get("stations", [])
        self.stations = {r["id"]: r for r in self.rows}
        new = Conn(st) if st else None
        old = self.conn
        self.conn = new
        vk = d.get("vault_key")
        if new and vk and self.vault is not None:
            try:
                if self.vault.unlock(base64.b64decode(vk), new.id):
                    ak = d.get("api_key")
                    if ak and not self.vault.locked:
                        self.vault.set(f"station:{new.id}:api_key", ak)
                    self._api_key = ak or self._api_key
                    peers = {sid: base64.b64decode(k) for sid, k in (d.get("peer_keys") or {}).items()}
                    if peers:
                        self.vault.wrap_for_peers(peers)                    # the other stations of the house open the same vault
                    self.vault.refresh()                                    # and what another station's process saved is visible here
            except Exception as e:
                self.log(f"vault: {e!r}")
        elif not new and self.vault is not None and not self.vault.locked:
            self.vault.lock()                                              # the speaker went away: the keys leave the memory, as always
            self.log("vault: closed (the station is not connected)")
        if (old is None) != (new is None) or (old and new and old.id != new.id):
            self.changed.set()

    async def run(self):
        """Forever: ask the gateway, apply the answer. Errors only mean "the gateway is restarting"."""
        seen = time.time()
        async with aiohttp.ClientSession() as http:
            while True:
                try:
                    self._apply(await self._fetch(http))
                    self.last_ok = seen = time.time()
                except Exception as e:
                    if time.time() - seen > 120:                            # the gateway is gone for good: a tenant has no life without it (and must not stay as an orphan)
                        self.log("gateway is gone, stopping")
                        os._exit(0)
                    if time.time() - self.last_ok > 30:
                        self.log(f"gateway is not answering: {e!r}")
                        self.last_ok = time.time()
                await asyncio.sleep(2)
