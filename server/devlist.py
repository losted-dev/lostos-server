"""The list of verified developers that plugapi.losted.dev serves: the state, the signing and the two files for the web server.

It lives on the server (the operator works with it through `lostos-admin developers ...`). The key that signs the list is made here (`init`), stays here (mode 0600) and is trusted by the
servers for this one file only (the scope "developers" in trusted_keys.json): it can not sign an index or a package.

State (DATA/plugapi/):  developers.src.json (who is verified, what is revoked, the serial number), developers.pem (the key).
Output (PLUGAPI_SITE, default /var/www/plugapi):  developers.json and developers.json.sig, which Caddy serves.
The list is valid for VALID_DAYS and is signed again whenever something changes and by a daily timer (`refresh` signs again when fewer than REFRESH_BELOW days are left),
so that it never runs out and a server that is kept from the newest one still stops trusting it within two weeks."""
import datetime
import hashlib
import json
import os
import urllib.parse

import plugin_sign as ps

VALID_DAYS = 14
REFRESH_BELOW = 12                      # days left: the daily timer signs again


def server_host(address: str) -> str:
    """The host of a server as the installer asks it: lower case, without the scheme, the port, the path and a dot at the end ("https://OS.Example.org:443/x" -> "os.example.org")."""
    a = address.strip()
    a = a if "://" in a else "https://" + a
    return (urllib.parse.urlsplit(a).hostname or "").lower().rstrip(".")


def host_hash(address: str) -> str:
    """What the list of servers holds instead of the address: the SHA-256 of the host (the list is public; the installer hashes what a person types and compares)."""
    return hashlib.sha256(server_host(address).encode("utf-8")).hexdigest()


class DevList:
    def __init__(self, data_dir: str, site_dir: str = None):
        self.dir = os.path.join(data_dir, "plugapi")
        self.src = os.path.join(self.dir, "developers.src.json")
        self.key = os.path.join(self.dir, "developers.pem")
        self.site = site_dir or os.environ.get("PLUGAPI_SITE", "/var/www/plugapi")

    def state(self) -> dict:
        try:
            return json.load(open(self.src, encoding="utf-8"))
        except (OSError, ValueError):
            return {"serial": 0, "developers": [], "revoked": [], "expires": ""}

    def _save(self, d: dict):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.src + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, self.src)

    def init(self) -> str:
        """A key for the list. -> its public part (put it into trusted_keys.json with the scope "developers")."""
        if os.path.exists(self.key):
            raise ValueError("the key exists already: " + self.key)
        os.makedirs(self.dir, exist_ok=True)
        pem, pub = ps.new_keypair()
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        return pub

    def public(self) -> str:
        return ps.public_of(ps.load_private(open(self.key).read()))

    @staticmethod
    def _revoked_fps(d: dict) -> list:
        return [r["fingerprint"] if isinstance(r, dict) else r for r in d.get("revoked", [])]

    def add(self, did: str, name: str, key: str) -> str:
        fp = ps.fingerprint(key)                                      # raises on a key that is not base64 of 32 bytes
        d = self.state()
        if not did or not name.strip() or len(did) > 40 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in did):
            raise ValueError("id: латиница в нижнем регистре, цифры, точка, дефис")
        if any(x["id"] == did or x["key"] == key for x in d["developers"]):
            raise ValueError("такой id или такой ключ уже в списке")
        if fp in self._revoked_fps(d):
            raise ValueError("этот ключ отозван")
        d["developers"].append({"id": did, "name": name.strip(), "key": key})
        self._save(d)
        self.build()
        return fp

    def remove(self, did: str):
        d = self.state()
        n = len(d["developers"])
        d["developers"] = [x for x in d["developers"] if x["id"] != did]
        if len(d["developers"]) == n:
            raise ValueError("такого разработчика нет")
        self._save(d)
        self.build()

    def revoke(self, fp: str, why: str = ""):
        if len(fp) != 16 or any(c not in "0123456789abcdef" for c in fp):
            raise ValueError("отпечаток: 16 шестнадцатеричных цифр")
        d = self.state()
        if fp not in self._revoked_fps(d):
            d.setdefault("revoked", []).append({"fingerprint": fp, "why": why[:80]})
        d["developers"] = [x for x in d["developers"] if ps.fingerprint(x["key"]) != fp]
        self._save(d)
        self.build()

    def add_server(self, address: str, name: str) -> str:
        """A server that the installer knows (it does not warn about it). -> the hash that is published."""
        host = server_host(address)
        if not host or "." not in host and host != "localhost" or not name.strip():
            raise ValueError("нужен адрес сервера и название")
        h = hashlib.sha256(host.encode("utf-8")).hexdigest()
        d = self.state()
        servers = d.setdefault("servers", [])
        if any(x["h"] == h for x in servers):
            raise ValueError("этот сервер уже в списке")
        servers.append({"h": h, "name": name.strip()[:60]})
        self._save(d)
        self.build()
        return h

    def remove_server(self, ident: str):
        """by the name or by the beginning of the hash"""
        d = self.state()
        n = len(d.get("servers", []))
        d["servers"] = [x for x in d.get("servers", []) if x["name"] != ident and not (len(ident) >= 8 and x["h"].startswith(ident.lower()))]
        if len(d["servers"]) == n:
            raise ValueError("такого сервера нет в списке")
        self._save(d)
        self.build()

    def ban_server(self, address: str, why: str) -> str:
        """A server that the installer refuses (a hard stop, unlike an unknown server that only gets a warning): the hash of its host and the reason that the person is told."""
        host = server_host(address)
        if not host or "." not in host and host != "localhost" or not why.strip():
            raise ValueError("нужны адрес сервера и причина")
        h = hashlib.sha256(host.encode("utf-8")).hexdigest()
        d = self.state()
        banned = d.setdefault("banned", [])
        if any(x["h"] == h for x in banned):
            raise ValueError("этот сервер уже заблокирован")
        d["servers"] = [x for x in d.get("servers", []) if x["h"] != h]          # a server is never known and banned at once
        banned.append({"h": h, "why": why.strip()[:120]})
        self._save(d)
        self.build()
        return h

    def unban_server(self, ident: str):
        """by the beginning of the hash (8 digits or more) or by the beginning of the reason"""
        d = self.state()
        n = len(d.get("banned", []))
        d["banned"] = [x for x in d.get("banned", []) if not (len(ident) >= 8 and x["h"].startswith(ident.lower())) and not (ident and x["why"].lower().startswith(ident.lower()))]
        if len(d["banned"]) == n:
            raise ValueError("такого сервера нет в списке заблокированных")
        self._save(d)
        self.build()

    def view(self) -> dict:
        """what the admin panel shows"""
        d = self.state()
        return {"serial": d.get("serial", 0), "days_left": round(self.days_left(), 1), "expires": d.get("expires", ""),
                "developers": [{"id": x["id"], "name": x["name"], "fingerprint": ps.fingerprint(x["key"])} for x in d.get("developers", [])],
                "revoked": [{"fingerprint": r["fingerprint"] if isinstance(r, dict) else r, "why": r.get("why", "") if isinstance(r, dict) else ""} for r in d.get("revoked", [])],
                "servers": [{"h": x["h"], "name": x["name"]} for x in d.get("servers", [])],
                "banned": [{"h": x["h"], "why": x["why"]} for x in d.get("banned", [])],
                "key": os.path.exists(self.key)}

    def days_left(self) -> float:
        try:
            end = datetime.datetime.fromisoformat(self.state()["expires"].replace("Z", "+00:00"))
            return (end - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400
        except (KeyError, ValueError):
            return -1.0

    def build(self) -> dict:
        """Signs the list again (serial + 1) and writes the two files for the web server."""
        d = self.state()
        now = datetime.datetime.now(datetime.timezone.utc)
        d["serial"] = int(d.get("serial", 0)) + 1
        d["expires"] = (now + datetime.timedelta(days=VALID_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {"v": 1, "serial": d["serial"], "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "expires": d["expires"],
               "developers": d["developers"], "revoked": self._revoked_fps(d)}
        raw = (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        sig = ps.sign_developers(raw, ps.load_private(open(self.key).read()))
        os.makedirs(self.site, exist_ok=True)
        servers = (json.dumps({"v": 1, "serial": d["serial"], "updated": doc["updated"], "expires": d["expires"], "servers": d.get("servers", []), "banned": d.get("banned", [])}, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        ssig = ps.sign_servers(servers, ps.load_private(open(self.key).read()))
        for name, data, mode in (("developers.json", raw, "wb"), ("developers.json.sig", (sig + "\n").encode(), "wb"),
                                 ("servers.json", servers, "wb"), ("servers.json.sig", (ssig + "\n").encode(), "wb")):
            tmp = os.path.join(self.site, name + ".tmp")
            with open(tmp, mode) as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, os.path.join(self.site, name))        # the web server never sees half a file
        self._save(d)
        return doc

    def refresh(self) -> bool:
        """For the daily timer: signs again when the list runs low. True when it did."""
        if self.days_left() < REFRESH_BELOW:
            self.build()
            return True
        return False
