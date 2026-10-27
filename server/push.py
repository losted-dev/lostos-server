"""Web Push to the phones that installed the panel (iOS 16.4+ home-screen app, Android Chrome, desktop browsers).
VAPID keys are made once (server\\certs\\vapid_private.pem); the subscriptions live in push_subs.json; sending runs in a thread."""
import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.environ.get("LOSTOS_PUSH_KEY") or os.path.join(HERE, "certs", "vapid_private.pem")      # one key of the server for all (a gateway gives its path)
SUBS_FILE = os.environ.get("LOSTOS_PUSH_FILE") or os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "push_subs.json")
CLAIMS = {"sub": "mailto:lostos-admin@example.com"}          # a made-up contact: the push services only want a well-formed one
_lock = threading.Lock()


def _vapid():
    from py_vapid import Vapid
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    if os.path.exists(KEY_FILE):
        return Vapid.from_file(KEY_FILE)
    v = Vapid()
    v.generate_keys()
    v.save_key(KEY_FILE)
    return v


def public_key() -> str:
    """The application server key the browser needs to subscribe (base64url, uncompressed P-256 point)."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid.utils import b64urlencode
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def _load() -> list:
    try:
        return json.load(open(SUBS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(subs: list):
    json.dump(subs, open(SUBS_FILE, "w", encoding="utf-8"), ensure_ascii=False)


def subscribe(sub: dict, agent: str = "") -> bool:
    if not (isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://") and isinstance(sub.get("keys"), dict)):
        return False
    with _lock:
        subs = [s for s in _load() if s["sub"]["endpoint"] != sub["endpoint"]]
        subs.append({"sub": sub, "agent": agent[:80], "t": time.time()})
        _save(subs)
    return True


def unsubscribe(endpoint: str):
    with _lock:
        _save([s for s in _load() if s["sub"]["endpoint"] != endpoint])


def count() -> int:
    return len(_load())


def _send(title: str, body: str, tag: str, url: str, log):
    from pywebpush import WebPushException, webpush
    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url}, ensure_ascii=False)
    dead = []
    for s in _load():
        try:
            r = webpush(subscription_info=s["sub"], data=payload, vapid_private_key=KEY_FILE, vapid_claims=dict(CLAIMS), ttl=3600)
            log(f"push sent to {s.get('agent', '?')[:30]}: {getattr(r, 'status_code', r)}")
        except WebPushException as e:
            code = getattr(e.response, "status_code", 0)
            if code in (404, 410):                       # the phone dropped its subscription (the app was removed)
                dead.append(s["sub"]["endpoint"])
            log(f"push to {s.get('agent', '?')[:30]}: {code or e!r} {getattr(e.response, 'text', '')[:120]}")
        except Exception as e:
            log(f"push failed: {e!r}")
    for ep in dead:
        unsubscribe(ep)


def send_async(title: str, body: str = "", tag: str = "lostos", url: str = "/", log=print):
    """Does nothing without subscriptions; never blocks the caller."""
    if not os.path.exists(SUBS_FILE) or not _load():
        return
    threading.Thread(target=_send, args=(title, body, tag, url, log), daemon=True).start()
