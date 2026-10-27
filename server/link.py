"""Station link: how a speaker that stands at home (behind a router) reaches a lostOS server on the internet.

The speaker (or a "bridge" program on a computer in the same home network) opens ONE outgoing WebSocket to /link/ws of the server
and proves who it is with a key pair it made itself (Ed25519). The server then opens a few ports on its own loopback, and every
connection to one of them is carried through the WebSocket to the same port of the speaker, so the server code talks to
127.0.0.1:<port> as if the speaker was next to it (API 6053, AirPlay state 7000, cover 49153). The ESPHome API inside is encrypted
end to end with the speaker's own key (Noise), the link carries only ciphertext.

Protocol (text frames are JSON, binary frames are  <stream id: 4 bytes big endian><bytes>):
  server  -> {"t": "challenge", "nonce": "<hex>"}
  station -> {"t": "hello", "ver": 1, "fresh": 1, "pub": "<base64 of the 32 byte public key>", "sig": "<base64 Ed25519 of nonce+b'lostos-link-v1'>", "name": "..."}
  server  -> {"t": "welcome", "id": "<station id>", "state": "pending" | "confirmed"}        (id = first 20 hex digits of SHA-256 of the public key)
  server  -> {"t": "open", "sid": N, "port": P}    station -> {"t": "opened", "sid": N} | {"t": "fail", "sid": N}
  both    -> {"t": "close", "sid": N}
  server  -> {"t": "state", "state": "confirmed"}  (the owner confirmed the station in the panel)
  station -> {"t": "vaultkey", "key": "<base64 32 bytes>"}   (after confirmation: opens the encrypted vault of the server, kept in memory only, see vault.py)
  station -> {"t": "forward", "name": "ha"}       (after confirmation: the station offers a host of the home network that the OWNER wrote into its link.conf;
                                                   the server opens a loopback port for it, the address itself never leaves the speaker)
  server  -> {"t": "open", "sid": N, "forward": "ha"}   (instead of "port")
  station -> {"t": "apikey", "key": "<base64>"}   (after confirmation: the key of the speaker's API; it goes into the vault, the panel never shows it)
  server  -> {"t": "press", "nonce": "<hex>", "seconds": 60, "code": "AAA3", "wav": "<base64 wav of the spoken line>"}   (the owner confirmed: now somebody must press the
                                                   Alice button of the speaker; the speaker shows the code, says the line and waits)
  station -> {"t": "pressed", "nonce": "<hex>"} | {"t": "press_timeout", "nonce": "<hex>"}
  station -> {"t": "wake", "station": "<name>", "word": "alisa", "busy": 0|1, "level": "-31.5"}   (a confirmed station heard the wake word: its claim goes to the OTHER stations of the
             same household, which cannot hear its UDP broadcast when they are on another network; the one that hears best answers, see arbiter.cpp)
  server  -> {"t": "wake", ...the same, from another station of the household}   server -> {"t": "peers", "n": N}   (how many other stations of the household are on line)
  server  -> {"t": "say", "wav": "<base64 wav>"}   (a new station that nobody confirmed yet: the speaker says GREETING once)
  A station that says "ver": 2 in its hello can do this; an older one is confirmed at once (the owner sees "без кнопки" in the panel)
A station that is new stays "pending": no ports are opened for it until the owner confirms it in the panel. The id cannot be forged: it is a hash
of the key, and every later connection must sign a fresh challenge with that key."""
import asyncio
import base64
import hashlib
import json
import os
import re
import time

from aiohttp import WSMsgType, web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("LOSTOS_DATA", HERE)
FILE = os.path.join(DATA, "stations.json")
PORTS = (6053, 7000, 49153)               # the ports of the speaker the server may reach through the link
MAX_PENDING = 20
PRESS_SECONDS = 60
GREETING = "Подключение успешно, ожидаю привязки к вашему аккаунту"    # said once when a new station reaches the server and nobody has confirmed it yet
CONNECTED = "Подключение успешно"                                        # said by a bound station after its boot show (the daemon has just started and the server is reached); the words of GREETING without the second half
PRESS_LINE = "Нажмите кнопку Алисы, чтобы подтвердить подключение"
PENDING_HOURS = 24


def hmac_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


def station_id(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()[:20]


class Conn:
    """One online station: its WebSocket, the loopback listeners and the streams that run through them."""

    def __init__(self, ws: web.WebSocketResponse, sid: str, name: str):
        self.ws, self.id, self.name = ws, sid, name
        self.listeners: dict = {}               # speaker port -> asyncio server
        self.local: dict = {}                   # speaker port -> port on this machine's loopback
        self.forward_addrs: dict = {}           # the same names -> "192.168.1.5:8123", as the owner wrote them into link.conf (informational)
        self.forwards: dict = {}                # name of a forward the station offers ("ha") -> port on this machine's loopback
        self.streams: dict = {}                 # stream id -> {"w": writer, "opened": Event, "ok": bool}
        self.next_sid = 1
        self.pending_api_key = ""               # the Noise key of the station's API, kept until the vault is open
        self.vault_key = ""                     # base64 key that opens the vault of the person (kept in memory only, handed to his tenant process)
        self.api_key = ""                       # base64 Noise key of the speaker's API (the same way)
        self.ver = 1                            # the protocol version the station speaks
        self.press = None                       # {"nonce", "until", "by"} while somebody has to press the button of the speaker


class Link:
    def __init__(self, log=print, vault=None):
        self.log = log
        self.vault = vault                      # the encrypted vault (vault.py): the station opens it with its key
        self.stations: dict = {}
        self.online: dict = {}                  # station id -> Conn
        self.changed = asyncio.Event()          # set whenever a station comes or goes
        self.listeners: list = []               # callables (kind, station id, name, extra): "pending" "press" "confirmed" "press_failed" "online" "offline" "removed"
        self.say = None                         # async (text) -> wav bytes: the voice of the spoken line (set by the server)
        self.tenant_of = lambda tg: tg          # tg of a person -> tg of the tenant (the owner of the household) he belongs to; a gateway sets it
        self.legacy_tenant = 0                  # the tenant of the stations that were connected before there were tenants (the first owner)
        self._load()

    # ---- the list of stations (id -> {"pub", "name", "confirmed", "added", "last_seen"})
    def _load(self):
        try:
            self.stations = json.load(open(FILE, encoding="utf-8"))
        except (OSError, ValueError):
            self.stations = {}

    def _save(self):
        os.makedirs(DATA, exist_ok=True)
        tmp = FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.stations, f, ensure_ascii=False, indent=1)
        os.replace(tmp, FILE)

    def tenant_of_station(self, r: dict) -> int:
        return r.get("tenant") or self.legacy_tenant

    def tenant_online(self, tenant: int) -> list:
        """The ids of the household's stations that are on line now, in the order they came."""
        return [sid for sid in self.online if self.stations.get(sid, {}).get("confirmed") and self.tenant_of_station(self.stations[sid]) == tenant]

    def default_station(self, tenant: int) -> str:
        """The station the panel talks to when the person has not chosen one: the first one on line, else the first confirmed, else none."""
        on = self.tenant_online(tenant)
        if on:
            return on[0]
        mine = [sid for sid, r in self.stations.items() if r.get("confirmed") and self.tenant_of_station(r) == tenant]
        return mine[0] if mine else ""

    def owns(self, tenant: int, sid: str) -> bool:
        r = self.stations.get(sid)
        return bool(r and r.get("confirmed") and self.tenant_of_station(r) == tenant)

    def tenant_view(self, tenant: int, station: str = None) -> dict:
        """What the process of a household's station is told: THAT station (its loopback ports, the keys it needs), the list of the household's stations, and the vault keys of its
        other stations that are on line (the process that has the vault open gives each of them access). station=None: the first one on line (the lock check and old callers);
        station="": the process of a house that has no station yet gets none."""
        rows = [x for x in self.listing(raw=True) if self.tenant_of_station(self.stations[x["id"]]) == tenant and x["confirmed"]]
        on = self.tenant_online(tenant)
        sid = on[0] if (station is None and on) else station
        if sid and sid in on:
            c = self.online[sid]
            peers = {o: self.online[o].vault_key for o in on if o != sid and self.online[o].vault_key}
            return {"station": {"id": sid, "name": c.name, "ports": c.local, "forwards": c.forwards, "forward_addrs": c.forward_addrs}, "stations": rows, "vault_key": c.vault_key, "api_key": c.api_key,
                    "peer_keys": peers, "online": on}
        return {"station": None, "stations": rows, "online": on}

    def has_station(self, tenant: int) -> bool:
        """Has this person (household) a connected-once, confirmed station at all (online or not)? Without one the panel stays locked."""
        return any(r.get("confirmed") and self.tenant_of_station(r) == tenant for r in self.stations.values())

    def visible_to(self, r: dict, tg: int, role: str, tenant=None) -> bool:
        """A station is seen by the person it belongs to (the owner of a household sees all the confirmed ones of HIS household, nobody sees another household's). A station that
        waits is seen ONLY by the person who is connecting it right now (he typed its identifier): nobody else learns that it exists."""
        if r.get("confirmed"):
            if tenant is not None and self.tenant_of_station(r) != tenant:
                return False
            return role == "owner" or r.get("owner_tg") == tg
        return r.get("claimant") == tg and r.get("claimant_until", 0) > time.time()

    def listing(self, tg: int = 0, role: str = "owner", raw: bool = False, tenant=None) -> list:
        out = []
        for sid, r in self.stations.items():
            if not raw and not self.visible_to(r, tg, role, tenant):
                continue
            c = self.online.get(sid)
            out.append({"id": sid, "name": r.get("name", ""), "confirmed": r.get("confirmed", False), "online": sid in self.online,
                        "added": r.get("added"), "last_seen": r.get("last_seen"), "code": sid[:4].upper(),
                        "press": int(c.press["until"] - time.time()) if c and c.press and c.press["until"] > time.time() else 0,
                        "button": bool(c and c.ver >= 2), "owner_tg": r.get("owner_tg", 0)})
        return out

    async def claim(self, ident: str, tg: int) -> str:
        """A person types the identifier that his speaker shows (the installer prints it): the station becomes his after the button of the speaker is pressed.
        -> "press" | "ok" | "offline" | "none" (no such station waiting) | "busy" (another person is connecting it)."""
        sid = "".join(ch for ch in ident.lower() if ch in "0123456789abcdef")
        r = self.stations.get(sid)
        if len(sid) != 20 or not r or r.get("confirmed"):
            return "none"
        if r.get("claimant") not in (None, tg) and r.get("claimant_until", 0) > time.time():
            return "busy"
        r["claimant"], r["claimant_until"] = tg, time.time() + PRESS_SECONDS + 90
        self._save()
        res = await self.confirm(sid, by=tg)
        if res in ("none", "offline"):
            r.pop("claimant", None)
        return res

    def emit(self, kind: str, sid: str, name: str = "", extra: str = ""):
        for fn in list(self.listeners):
            try:
                fn(kind, sid, name, extra)
            except Exception as e:
                self.log(f"link: listener failed: {e!r}")

    # ---- the station that the server works with (the first confirmed one that is online)
    def api_key_of(self, sid: str) -> str:
        return self.vault.get(f"station:{sid}:api_key") if self.vault is not None else ""

    def active(self):
        for sid, c in self.online.items():
            if self.stations.get(sid, {}).get("confirmed"):
                return c
        return None

    async def wait_active(self) -> Conn:
        while True:
            c = self.active()
            if c:
                return c
            self.changed.clear()
            await self.changed.wait()

    # ---- owner's actions
    async def confirm(self, sid: str, by: int = 0) -> str:
        """The owner agrees. -> "ok" (the station is confirmed), "press" (it waits for its button to be pressed), "offline" or "none"."""
        r = self.stations.get(sid)
        if not r:
            return "none"
        if r.get("confirmed"):
            return "ok"
        c = self.online.get(sid)
        if c is None:
            return "offline"                                          # a station that is not here cannot be pressed
        if c.ver >= 2:
            if c.press and c.press["until"] > time.time():
                return "press"
            nonce = os.urandom(8).hex()
            c.press = {"nonce": nonce, "until": time.time() + PRESS_SECONDS, "by": by}
            wav = b""
            if self.say:
                try:
                    wav = await self.say(PRESS_LINE)
                except Exception as e:
                    self.log(f"link: the spoken line failed: {e!r}")
            await c.ws.send_json({"t": "press", "nonce": nonce, "seconds": PRESS_SECONDS, "code": sid[:4].upper(), "wav": base64.b64encode(wav).decode() if wav else ""})
            asyncio.get_event_loop().call_later(PRESS_SECONDS + 5, lambda: self._press_expired(c, nonce))
            self.log(f"link: station {sid} must be pressed (the owner confirmed it)")
            self.emit("press", sid, r.get("name", ""), str(by))
            return "press"
        await self._confirmed(sid, r)                                  # an old firmware without the button: confirmed at once
        return "ok"

    async def _greet(self, c: "Conn", text: str = GREETING):
        """The speaker tells that it has reached the server: a new one that it waits to be tied to an account (GREETING), a bound one just that it is connected (CONNECTED).
        The speaker plays it after its boot show."""
        if not self.say:
            return
        try:
            wav = await self.say(text)
            if wav and not c.ws.closed:
                await c.ws.send_json({"t": "say", "wav": base64.b64encode(wav).decode()})
        except Exception as e:
            self.log(f"link: the greeting failed: {e!r}")

    def _press_expired(self, c: "Conn", nonce: str):
        if c.press and c.press["nonce"] == nonce:
            c.press = None
            self.log(f"link: station {c.id}: the button was not pressed")
            rec = self.stations.get(c.id) or {}
            who = rec.pop("claimant", 0)
            self.emit("press_failed", c.id, c.name, str(who or 0))

    async def _confirmed(self, sid: str, r: dict):
        r["confirmed"] = True
        r["owner_tg"] = r.pop("claimant", 0) or r.get("owner_tg", 0)           # the station belongs to the person who connected it (0: to the owner of the server)
        r["tenant"] = self.tenant_of(r["owner_tg"]) if r["owner_tg"] else self.legacy_tenant     # and works for his household: the server process of the owner of it
        r.pop("claimant_until", None)
        r.pop("pending_until", None)
        self._save()
        c = self.online.get(sid)
        if c:
            c.press = None
            await self._open_listeners(c)
            await c.ws.send_json({"t": "state", "state": "confirmed"})
            self.changed.set()
            await self._tell_peers(self.tenant_of_station(r))
        self.emit("confirmed", sid, r.get("name", ""), str(r["owner_tg"]))

    async def remove(self, sid: str) -> bool:
        if sid not in self.stations:
            return False
        c = self.online.get(sid)
        self.stations.pop(sid)
        self._save()
        if c:
            await c.ws.close()
        return True

    # ---- loopback listeners: a connection to one of them is carried to the same port of the speaker
    async def _open_listeners(self, c: Conn):
        for port in PORTS:
            if port in c.listeners:
                continue

            async def handler(reader, writer, port=port):
                await self._carry(c, port, reader, writer)

            srv = await asyncio.start_server(handler, "127.0.0.1", 0)
            c.listeners[port] = srv
            c.local[port] = srv.sockets[0].getsockname()[1]

    async def _open_forward(self, c: Conn, name: str, addr: str = ""):
        if not name.isalnum() or len(name) > 16:
            return
        if len(addr) <= 64 and re.fullmatch(r"[0-9.]+:[0-9]{1,5}", addr):     # the home address the owner wrote into link.conf: only to show it in the wizard
            c.forward_addrs[name] = addr
        if name in c.forwards:
            return

        async def handler(reader, writer):
            await self._carry(c, name, reader, writer)

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        c.listeners["fw:" + name] = srv
        c.forwards[name] = srv.sockets[0].getsockname()[1]
        self.log(f"link: station {c.id} forwards '{name}' to loopback port {c.forwards[name]}")
        self.changed.set()

    def forward_addr(self, name: str) -> str:
        c = self.active()
        return c.forward_addrs.get(name, "") if c else ""

    def forward_url(self, name: str) -> str:
        """http://127.0.0.1:<port> of a forward of the active station, "" when there is none."""
        c = self.active()
        return f"http://127.0.0.1:{c.forwards[name]}" if c and name in c.forwards else ""

    async def _carry(self, c: Conn, port, reader, writer):
        sid = c.next_sid
        c.next_sid += 1
        st = {"w": writer, "opened": asyncio.Event(), "ok": False}
        c.streams[sid] = st
        try:
            await c.ws.send_json({"t": "open", "sid": sid, "forward": port} if isinstance(port, str) else {"t": "open", "sid": sid, "port": port})
            try:
                await asyncio.wait_for(st["opened"].wait(), 8)
            except asyncio.TimeoutError:
                return
            if not st["ok"]:
                return
            while True:
                data = await reader.read(32768)
                if not data:
                    break
                await c.ws.send_bytes(sid.to_bytes(4, "big") + data)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            if c.streams.pop(sid, None) is not None and not c.ws.closed:
                try:
                    await c.ws.send_json({"t": "close", "sid": sid})
                except Exception:
                    pass
            try:
                writer.close()
            except Exception:
                pass

    def _drop_streams(self, c: Conn):
        for st in c.streams.values():
            st["opened"].set()
            try:
                st["w"].close()
            except Exception:
                pass
        c.streams.clear()

    # ---- the WebSocket of a station
    async def _tell_peers(self, tenant: int):
        """Every station of the household learns how many others are on line (with a neighbour the wake word decision waits for it a little longer)."""
        ids = self.tenant_online(tenant)
        for sid in ids:
            c = self.online.get(sid)
            if c is not None:
                try:
                    await c.ws.send_json({"t": "peers", "n": len(ids) - 1})
                except Exception:
                    pass

    async def _relay_wake(self, sid: str, m: dict):
        """A claim for the wake word goes to the household's other stations, and only to them."""
        rec = self.stations.get(sid, {})
        if not rec.get("confirmed"):
            return
        claim = {"t": "wake", "station": str(m.get("station", ""))[:31], "word": str(m.get("word", ""))[:15], "busy": 1 if str(m.get("busy")) in ("1", "True", "true") else 0, "level": str(m.get("level", ""))[:8]}
        for other in self.tenant_online(self.tenant_of_station(rec)):
            c = self.online.get(other)
            if other != sid and c is not None:
                try:
                    await c.ws.send_json(claim)
                except Exception:
                    pass

    async def ws_handler(self, req: web.Request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(req)
        nonce = os.urandom(16)
        await ws.send_json({"t": "challenge", "nonce": nonce.hex()})
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 15)
            pub = base64.b64decode(hello["pub"])
            Ed25519PublicKey.from_public_bytes(pub).verify(base64.b64decode(hello["sig"]), nonce + b"lostos-link-v1")
        except (asyncio.TimeoutError, KeyError, ValueError, TypeError, InvalidSignature):
            await ws.close(code=4001, message=b"auth")
            return ws
        sid = station_id(pub)
        name = str(hello.get("name", ""))[:60]
        rec = self.stations.get(sid)
        if rec is None:
            pending = [k for k, v in self.stations.items() if not v.get("confirmed")]
            for k in pending:                                   # old pending ones go away
                if self.stations[k].get("pending_until", 0) < time.time():
                    self.stations.pop(k)
            if len([1 for v in self.stations.values() if not v.get("confirmed")]) >= MAX_PENDING:
                await ws.close(code=4002, message=b"too many pending stations")
                return ws
            rec = self.stations[sid] = {"pub": hello["pub"], "name": name, "confirmed": False, "added": int(time.time()),
                                        "pending_until": time.time() + PENDING_HOURS * 3600}
            self.log(f"link: new station {sid} ({name}) waits until a person types its identifier")
        elif rec["pub"] != hello["pub"]:
            await ws.close(code=4003, message=b"key mismatch")
            return ws
        rec["last_seen"] = int(time.time())
        rec["name"] = name or rec.get("name", "")
        self._save()
        old = self.online.get(sid)
        if old:                                                  # the same station again: the new connection wins
            await old.ws.close()
        c = Conn(ws, sid, rec["name"])
        try:
            c.ver = int(hello.get("ver", 1))
        except (TypeError, ValueError):
            c.ver = 1
        self.online[sid] = c
        await ws.send_json({"t": "welcome", "id": sid, "state": "confirmed" if rec["confirmed"] else "pending"})
        fresh = bool(hello.get("fresh"))                                          # the daemon has just started (the boot show is about to play), not a reconnect of the link
        if not rec["confirmed"] and c.ver >= 2 and (not rec.get("greeted") or fresh):
            rec["greeted"] = True                                                 # a new station is greeted at its first connection and after every start, never at a reconnect
            self._save()
            asyncio.get_event_loop().create_task(self._greet(c))
        elif rec["confirmed"] and c.ver >= 2 and fresh:
            asyncio.get_event_loop().create_task(self._greet(c, CONNECTED))
        if rec["confirmed"]:
            await self._open_listeners(c)
            self.log(f"link: station {sid} is online (loopback ports {c.local})")
            self.emit("online", sid, rec["name"])
            await self._tell_peers(self.tenant_of_station(rec))
        self.changed.set()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY and len(msg.data) > 4:
                    st = c.streams.get(int.from_bytes(msg.data[:4], "big"))
                    if st and st["ok"]:
                        st["w"].write(msg.data[4:])
                        await st["w"].drain()
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    st = c.streams.get(m.get("sid"))
                    if m.get("t") == "wake":
                        await self._relay_wake(sid, m)
                        continue
                    if m.get("t") in ("pressed", "press_timeout"):          # the button of the speaker (only inside the window the owner opened)
                        pr = c.press
                        if pr and hmac_eq(str(m.get("nonce", "")), pr["nonce"]) and pr["until"] > time.time():
                            if m["t"] == "pressed" and not self.stations.get(sid, {}).get("confirmed"):
                                await self._confirmed(sid, self.stations[sid])
                            else:
                                c.press = None
                                self.emit("press_failed", sid, c.name, str(self.stations.get(sid, {}).pop("claimant", 0) or 0))
                        continue
                    if m.get("t") == "forward":                       # a place in the owner's home network that the station offers (Home Assistant); only from a confirmed station
                        if self.stations.get(sid, {}).get("confirmed"):
                            await self._open_forward(c, str(m.get("name", "")), str(m.get("addr", "")))
                        continue
                    if m.get("t") in ("vaultkey", "apikey"):        # only a CONFIRMED station may open the vault or hand over its API key
                        if not self.stations.get(sid, {}).get("confirmed"):
                            continue
                        key = str(m.get("key", ""))
                        try:
                            raw = base64.b64decode(key, validate=True)
                        except ValueError:
                            continue
                        if m["t"] == "vaultkey" and len(raw) == 32:
                            c.vault_key = key                      # a gateway hands it to the tenant process of the person; here it stays in memory
                        elif m["t"] == "apikey" and len(key) == 44:
                            c.api_key = key
                        if self.vault is None:
                            self.changed.set()
                            continue
                        if m["t"] == "vaultkey" and self.vault.unlock(raw, sid):
                            if c.pending_api_key:
                                self.vault.set(f"station:{sid}:api_key", c.pending_api_key)
                                c.pending_api_key = ""
                            old = self.stations[sid].pop("api_key", "")       # an older version kept it in plain text: it moves into the vault
                            if old:
                                self.vault.set(f"station:{sid}:api_key", old)
                                self._save()
                            self.changed.set()
                        elif m["t"] == "apikey" and len(key) == 44:
                            if self.vault is not None and not self.vault.locked:
                                self.vault.set(f"station:{sid}:api_key", key)
                            else:
                                c.pending_api_key = key
                        continue
                    if m.get("t") == "opened" and st:
                        st["ok"] = True
                        st["opened"].set()
                    elif m.get("t") == "fail" and st:
                        st["opened"].set()
                    elif m.get("t") == "close" and st:
                        c.streams.pop(m["sid"], None)
                        st["w"].close()
        except (ConnectionError, ValueError, KeyError):
            pass
        finally:
            self._drop_streams(c)
            for srv in c.listeners.values():
                srv.close()
            if self.online.get(sid) is c:
                self.online.pop(sid)
                self.log(f"link: station {sid} went offline")
                if self.stations.get(sid, {}).get("confirmed"):
                    self.emit("offline", sid, c.name, str(self.stations[sid].get("owner_tg", 0)))
                    await self._tell_peers(self.tenant_of_station(self.stations[sid]))
                if self.vault is not None and not self.active():       # nobody holds the key now: the vault closes, the keys leave the memory
                    self.vault.lock()
                    self.log("vault: closed (no station is connected)")
            self.changed.set()
        return ws


def install(app: web.Application, log=print, vault=None) -> Link:
    link = Link(log, vault)
    app.router.add_get("/link/ws", link.ws_handler)

    def who(req):
        s = req.get("session") or {}
        return s.get("tg_id", 0), s.get("role", "owner")

    def household(req):
        return (req.get("session") or {}).get("tenant")

    async def stations(req):
        tg, role = who(req)
        return web.json_response({"stations": link.listing(tg, role, tenant=household(req))})

    async def claim(req):
        """POST {"id": "<identifier of the speaker>"}: this account connects that station (the speaker asks for its button to be pressed)."""
        from auth import _limited
        tg, role = who(req)
        if _limited(f"claim:{tg}", 8):
            return web.json_response({"ok": False, "error": "слишком много попыток, подождите минуту"}, status=429)
        try:
            ident = str((await req.json()).get("id", ""))
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        res = await link.claim(ident, tg)
        text = {"none": "такой колонки нет в ожидании: проверьте идентификатор и что колонка включена", "busy": "эту колонку уже подключает другой человек",
                "offline": "колонка сейчас не в сети"}.get(res, "")
        return web.json_response({"ok": res in ("ok", "press"), "state": res, "error": text}, status=200 if res in ("ok", "press") else 404 if res == "none" else 409)

    async def remove(req):
        tg, role = who(req)
        r = link.stations.get(req.match_info["sid"])
        if not r or not link.visible_to(r, tg, role, household(req)):
            return web.json_response({"ok": False}, status=404)
        ok = await link.remove(req.match_info["sid"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    app.router.add_get("/api/v1/stations", stations)
    app.router.add_post("/api/v1/stations/claim", claim)
    app.router.add_delete("/api/v1/stations/{sid}", remove)
    return link
