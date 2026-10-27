"""The accounts of a lostOS server that is open to the internet: who may come in, with which role, and the sessions they hold.

One SQLite file (data/accounts.db, mode 0600). A person is a Telegram user (tg_id); roles:
  owner   the one who owns the server: keys, stations, plugins, invitations, everybody's sessions
  member  a person the owner invited: the player, the sound, the home page, no keys and no stations
The owner is created by the one-time link of the server (a session without a Telegram id yet), and then binds a Telegram account to it.
Nothing secret is stored in clear: sessions and one-time tokens are kept as SHA-256 hashes, so a stolen file logs nobody in.

Tokens (table `tokens`) are short-lived and used once:
  login   a browser asks to come in; the bot confirms it (number matching, see tgbot.py)
  bind    the owner session asks to tie its Telegram account to the server
  invite  the owner invites somebody
  oidc    the state of a login through Telegram OpenID Connect (telegram_oidc.py)
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("LOSTOS_DATA", HERE)
SESSION_DAYS = 180
TTL = {"login": 300, "bind": 600, "invite": 86400, "oidc": 600, "reg": 7 * 86400}     # reg: the link of the operator that lets a new person in (a week, once)
MAX_OPEN_TOKENS = 50                                # an open server must not fill its disk with login requests


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class VoiceStore:
    """The voice prints of the people (voices.py) in the database of the accounts: one row per voice, with the Telegram id of the person it belongs to."""

    def __init__(self, accounts: "Accounts"):
        self.acc = accounts
        accounts._x("CREATE TABLE IF NOT EXISTS voiceprints (name TEXT PRIMARY KEY, owner_tg INTEGER, data TEXT)")

    def load(self) -> dict:
        out = {}
        for r in self.acc._q("SELECT name, owner_tg, data FROM voiceprints"):
            try:
                p = json.loads(r["data"])
            except ValueError:
                continue
            p["owner"] = r["owner_tg"]
            out[r["name"]] = p
        return out

    def save(self, people: dict):
        with self.acc.lock:
            db = self.acc.db
            db.execute("BEGIN")
            try:
                db.execute("DELETE FROM voiceprints WHERE name NOT IN (%s)" % ",".join("?" * len(people)), list(people)) if people else db.execute("DELETE FROM voiceprints")
                for name, p in people.items():
                    data = {k: v for k, v in p.items() if k != "owner"}
                    db.execute("INSERT INTO voiceprints VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET owner_tg=?, data=?", (name, int(p.get("owner", 0) or 0), json.dumps(data, ensure_ascii=False),
                                                                                                                               int(p.get("owner", 0) or 0), json.dumps(data, ensure_ascii=False)))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise


class Accounts:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(DATA, "accounts.db")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS users (tg_id INTEGER PRIMARY KEY, name TEXT, username TEXT, role TEXT NOT NULL, added INTEGER, last_login INTEGER);
          CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, created INTEGER, last INTEGER, agent TEXT, ip TEXT);
          CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, created INTEGER, expires INTEGER, state TEXT, tg_id INTEGER,
                                             code TEXT, agent TEXT, ip TEXT, by_tg INTEGER, session_tg INTEGER);
          CREATE TABLE IF NOT EXISTS events (ts INTEGER, tg_id INTEGER, kind TEXT, text TEXT);
        """)
        for col in ("photo TEXT",                                                # the picture of the Telegram profile (an https address)
                    "tenant INTEGER",                                            # the household (the Telegram id of its owner) a person belongs to: his own server process
                    "admin INTEGER DEFAULT 0"):                                  # the operator of the server (approves new people, sets the memory)
            try:
                self.db.execute("ALTER TABLE users ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (tg_id INTEGER PRIMARY KEY, name TEXT, username TEXT, ts INTEGER, state TEXT)")   # people who ask for access
        self.db.execute("UPDATE users SET admin=1 WHERE role='owner' AND NOT EXISTS (SELECT 1 FROM users WHERE admin=1) AND tg_id=(SELECT MIN(tg_id) FROM users WHERE role='owner')")
        self.db.execute("UPDATE users SET tenant=tg_id WHERE role='owner' AND tenant IS NULL")
        self.db.execute("UPDATE users SET tenant=(SELECT tg_id FROM users WHERE admin=1 LIMIT 1) WHERE role='member' AND tenant IS NULL")

    # ---- helpers
    def _q(self, sql: str, *args) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _x(self, sql: str, *args) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    def log(self, tg_id: int, kind: str, text: str = ""):
        self._x("INSERT INTO events VALUES (?,?,?,?)", int(time.time()), tg_id, kind, text[:200])
        self._x("DELETE FROM events WHERE ts < ?", int(time.time()) - 90 * 86400)

    # ---- people
    def user(self, tg_id: int):
        r = self._q("SELECT * FROM users WHERE tg_id=?", tg_id)
        return r[0] if r else None

    def admin(self):
        """The operator of the server (the first owner): the one who approves people and is told about the server itself."""
        r = self._q("SELECT * FROM users WHERE admin=1 ORDER BY added LIMIT 1")
        return r[0] if r else None

    def owner(self):
        return self.admin()                          # the code that says "the owner of the server" means the operator

    def admins(self) -> list:
        return self._q("SELECT * FROM users WHERE admin=1")

    def tenant_of(self, tg_id: int) -> int:
        """The household (the id of its owner) a person works in; the one-time-link session without a Telegram id (0) works in the operator's."""
        u = self.user(tg_id) if tg_id else None
        if u:
            return u.get("tenant") or (tg_id if u["role"] == "owner" else (self.admin() or {}).get("tg_id", 0))
        a = self.admin()
        return a["tg_id"] if a else 0

    def tenants(self) -> list:
        """The households: their owners."""
        return self._q("SELECT * FROM users WHERE role='owner' ORDER BY added")

    def users(self, tenant: int = None) -> list:
        if tenant is None:
            return self._q("SELECT * FROM users ORDER BY role='owner' DESC, added")
        return self._q("SELECT * FROM users WHERE tenant=? ORDER BY role='owner' DESC, added", tenant)

    # ---- people who ask for access (registration with the approval of the operator)
    def request_access(self, tg_id: int, name: str, username: str) -> str:
        """-> "new" (the request is made now), "pending" (made before), "denied" (the operator said no)."""
        r = self._q("SELECT state FROM requests WHERE tg_id=?", tg_id)
        if r:
            return "pending" if r[0]["state"] == "pending" else "denied" if r[0]["state"] == "denied" else "pending"
        self._x("INSERT INTO requests VALUES (?,?,?,?,?)", tg_id, name, username, int(time.time()), "pending")
        return "new"

    def pending_requests(self) -> list:
        return self._q("SELECT * FROM requests WHERE state='pending' ORDER BY ts")

    def admit(self, tg_id: int, name: str, username: str, how: str = "доступ открыт"):
        """A new person becomes the owner of a household of his own (his own server process, keys and settings)."""
        self._x("INSERT INTO users(tg_id,name,username,role,added,last_login,tenant,admin) VALUES (?,?,?,?,?,0,?,0) ON CONFLICT(tg_id) DO UPDATE SET role='owner', tenant=?",
                tg_id, name, username, "owner", int(time.time()), tg_id, tg_id)
        self._x("UPDATE requests SET state='approved' WHERE tg_id=?", tg_id)
        self.log(tg_id, "owner", how)

    def decide(self, tg_id: int, approve: bool):
        """The operator decides: an approved person becomes the owner of a household of his own."""
        r = self._q("SELECT * FROM requests WHERE tg_id=?", tg_id)
        if not r or r[0]["state"] != "pending":
            return None
        self._x("UPDATE requests SET state=? WHERE tg_id=?", "approved" if approve else "denied", tg_id)
        if approve:
            self.admit(tg_id, r[0]["name"], r[0]["username"])
        return r[0]

    # ---- the links of the operator that let a new person in (registration "by link")
    def reg_links(self) -> list:
        """the open links, newest first (the token itself is shown once, when it is made)"""
        return self._q("SELECT hash, created, expires FROM tokens WHERE kind='reg' AND state='wait' AND expires>? ORDER BY created DESC", int(time.time()))

    def revoke_reg_link(self, prefix: str) -> bool:
        if len(prefix) < 8 or not prefix.isalnum():
            return False
        n = len(self.reg_links())
        self._x("DELETE FROM tokens WHERE kind='reg' AND hash LIKE ?", prefix + "%")
        return len(self.reg_links()) < n

    def events_text(self, tg_id: int, n: int = 80) -> list:
        """The person's own journal as lines of text, oldest first (what the logs page shows to a member)."""
        rows = self._q("SELECT ts, kind, text FROM events WHERE tg_id=? ORDER BY ts DESC LIMIT ?", tg_id, n)
        return [time.strftime("%d.%m %H:%M", time.localtime(r["ts"])) + "  " + (r["text"] or r["kind"]) for r in reversed(rows)]

    def bind_owner(self, tg_id: int, name: str, username: str, photo: str = ""):
        """The Telegram account that owns the server from now on (the owner may move it to another account: sessions of the old one go away)"""
        old = self.owner()
        if old and old["tg_id"] != tg_id:
            self._x("DELETE FROM users WHERE tg_id=?", old["tg_id"])
            self._x("DELETE FROM sessions WHERE tg_id=?", old["tg_id"])
        self._x("INSERT INTO users(tg_id,name,username,role,added,last_login,photo,tenant,admin) VALUES (?,?,?,?,?,0,?,?,1) ON CONFLICT(tg_id) DO UPDATE SET name=?, username=?, role='owner', photo=?, tenant=?, admin=1",
                tg_id, name, username, "owner", int(time.time()), photo, tg_id, name, username, photo, tg_id)
        self._x("UPDATE sessions SET tg_id=? WHERE tg_id=0", tg_id)          # the session that came in with the one-time link is now this person's
        self.log(tg_id, "owner", "Telegram привязан к серверу")

    def touch(self, tg_id: int, name: str, username: str, photo: str = ""):
        """The name, the @username and the picture a person has in Telegram now."""
        self._x("UPDATE users SET name=?, username=?, photo=CASE WHEN ?<>'' THEN ? ELSE photo END WHERE tg_id=?", name, username, photo, photo, tg_id)

    def add_member(self, tg_id: int, name: str, username: str, tenant: int = None) -> bool:
        """A person the owner of a household invited: he works in that household (its keys, its speaker)."""
        if self.user(tg_id):
            return False
        if tenant is None:
            tenant = (self.admin() or {}).get("tg_id", 0)
        self._x("INSERT INTO users(tg_id,name,username,role,added,last_login,tenant) VALUES (?,?,?,?,?,0,?)", tg_id, name, username, "member", int(time.time()), tenant)
        self.log(tg_id, "member", "принял приглашение")
        return True

    def remove_user(self, tg_id: int, tenant: int = None) -> bool:
        u = self.user(tg_id)
        if not u or u["role"] == "owner" or (tenant is not None and u.get("tenant") != tenant):
            return False
        self._x("DELETE FROM users WHERE tg_id=?", tg_id)
        self._x("DELETE FROM sessions WHERE tg_id=?", tg_id)
        self.log(tg_id, "removed", "убран владельцем")
        return True

    def role_of(self, tg_id: int) -> str:
        """"owner" | "member" | "" (not allowed). tg_id 0 is the owner who has not tied a Telegram account yet."""
        if tg_id == 0:
            return "" if self.owner() else "owner"
        u = self.user(tg_id)
        return u["role"] if u else ""

    # ---- sessions
    def new_session(self, tg_id: int, agent: str = "", ip: str = "") -> str:
        raw = secrets.token_urlsafe(32)
        now = int(time.time())
        self._x("DELETE FROM sessions WHERE created < ?", now - SESSION_DAYS * 86400)
        self._x("INSERT INTO sessions VALUES (?,?,?,?,?,?)", digest(raw), tg_id, now, now, agent[:120], ip[:45])
        if tg_id:
            self._x("UPDATE users SET last_login=? WHERE tg_id=?", now, tg_id)
        return raw

    def session(self, raw: str):
        """-> {"tg_id", "role", "hash"} for a valid session, None otherwise."""
        if not raw:
            return None
        h = digest(raw)
        r = self._q("SELECT * FROM sessions WHERE hash=?", h)
        if not r or r[0]["created"] + SESSION_DAYS * 86400 < time.time():
            return None
        role = self.role_of(r[0]["tg_id"])
        if not role:                                                             # the person was removed
            return None
        if time.time() - (r[0]["last"] or 0) > 3600:
            self._x("UPDATE sessions SET last=? WHERE hash=?", int(time.time()), h)
        u = self.user(r[0]["tg_id"]) if r[0]["tg_id"] else None
        return {"tg_id": r[0]["tg_id"], "role": role, "hash": h, "tenant": self.tenant_of(r[0]["tg_id"]), "admin": bool(u and u.get("admin")) or (not u and role == "owner")}

    def sessions_of(self, tg_id: int = None, tenant: int = None) -> list:
        """The sessions of one person, or of a whole household (its owner sees the sessions of everybody in it, and nothing of other households)."""
        if tenant is not None:
            rows = self._q("SELECT hash,tg_id,created,last,agent,ip FROM sessions WHERE tg_id IN (SELECT tg_id FROM users WHERE tenant=?)" + (" OR tg_id=0" if tenant == self.tenant_of(0) else "") + " ORDER BY last DESC", tenant)
        else:
            rows = self._q("SELECT hash,tg_id,created,last,agent,ip FROM sessions" + (" WHERE tg_id=?" if tg_id is not None else "") + " ORDER BY last DESC",
                           *([tg_id] if tg_id is not None else []))
        for r in rows:
            r["id"] = r.pop("hash")[:12]
        return rows

    def revoke(self, prefix: str, tg_id: int = None, tenant: int = None) -> bool:
        """End a session by the first 12 characters of its hash (the owner of a household any of his household, anybody their own)"""
        if len(prefix) != 12 or not prefix.isalnum():
            return False
        if tenant is not None:
            return self._x("DELETE FROM sessions WHERE hash LIKE ? AND (tg_id IN (SELECT tg_id FROM users WHERE tenant=?)" + (" OR tg_id=0" if tenant == self.tenant_of(0) else "") + ")", prefix + "%", tenant) > 0
        return self._x("DELETE FROM sessions WHERE hash LIKE ? AND (? IS NULL OR tg_id=?)", prefix + "%", tg_id, tg_id) > 0

    def revoke_all(self, tg_id: int, keep_hash: str = "") -> int:
        return self._x("DELETE FROM sessions WHERE tg_id=? AND hash<>?", tg_id, keep_hash)

    def drop_session(self, raw: str):
        self._x("DELETE FROM sessions WHERE hash=?", digest(raw))

    # ---- one-time tokens
    def new_token(self, kind: str, agent: str = "", ip: str = "", by_tg: int = 0, code: str = ""):
        """-> the raw token (shown once), or None when too many are open."""
        now = int(time.time())
        self._x("DELETE FROM tokens WHERE expires < ?", now)
        if self._q("SELECT COUNT(*) AS n FROM tokens")[0]["n"] >= MAX_OPEN_TOKENS:
            return None
        raw = secrets.token_urlsafe(24)
        self._x("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,?,?,?)", digest(raw), kind, now, now + TTL[kind], "wait", 0, code, agent[:120], ip[:45], by_tg, 0)
        return raw

    def token(self, raw: str, kind: str):
        r = self._q("SELECT * FROM tokens WHERE hash=? AND kind=? AND expires>?", digest(raw), kind, int(time.time()))
        return r[0] if r else None

    def token_by_id(self, prefix: str, kind: str):
        """The bot's buttons carry the first 16 characters of the hash, never the token itself."""
        if len(prefix) != 16 or not prefix.isalnum():
            return None
        r = self._q("SELECT * FROM tokens WHERE hash LIKE ? AND kind=? AND expires>?", prefix + "%", kind, int(time.time()))
        return r[0] if r else None

    def set_token(self, h: str, state: str, tg_id: int = 0):
        self._x("UPDATE tokens SET state=?, tg_id=? WHERE hash=?", state, tg_id, h)

    def use_token(self, raw: str):
        self._x("DELETE FROM tokens WHERE hash=?", digest(raw))

    def use_token_by_hash(self, h: str):
        self._x("DELETE FROM tokens WHERE hash=?", h)

    def import_old(self, path: str):
        """The sessions of the first version (auth.json: one-time link and sessions without a Telegram id) move here once."""
        try:
            old = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return
        n = 0
        if old.get("sessions"):
            for h, s in old["sessions"].items():
                n += self._x("INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?)", h, 0, s.get("created", 0), s.get("last", 0), s.get("agent", ""), "")
        if n:
            old["sessions"] = {}
            tmp = path + ".tmp"
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(old, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
