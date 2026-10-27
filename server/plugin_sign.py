"""Signatures of plugin packages (.lstplugin) and of the registry index. Ed25519, nothing but the standard `cryptography` that the server has anyway.

A package is a zip. The signature sits in it as `lostos-signature.json` (next to manifest.json):
    {"v": 1, "publisher": "losted-dev", "key": "<public key, base64>", "digest": "<hex>", "sig": "<signature, base64>"}
digest = SHA-256 of the list "path NUL sha256-of-the-file LF" of every file of the package EXCEPT the signature, sorted by path: it does not depend on how the zip
is packed (order, compression, dates), only on what is inside. sig = Ed25519 over b"lostos-plugin-v1\\n" + digest.

Whom to trust (trust_state):
  "trusted"  the key is the key of the lostOS team (trusted_keys.json, shipped with the server) or a developer key that the signed registry lists and has not revoked;
  "known"    the signature is valid but nobody we know owns the key: the plugin is genuine, but the author is a stranger (still in the sandbox, with a warning in the panel);
  "none"     no signature (a manual file as before);
  "bad"      a signature that does not match the content: the package is changed after signing, it is refused.

The registry index (index.json) is signed in the same way by a team key: `index.json.sig` = base64 Ed25519 over b"lostos-registry-v1\\n" + the bytes of index.json."""
import base64
import hashlib
import io
import json
import os
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

HERE = os.path.dirname(os.path.abspath(__file__))
TRUST_FILE = os.path.join(HERE, "trusted_keys.json")          # the public keys of the team (tests point it to their own)
SIG_FILE = "lostos-signature.json"
PLUGIN_TAG = b"lostos-plugin-v1\n"
INDEX_TAG = b"lostos-registry-v1\n"
MODELS_TAG = b"lostos-models-v1\n"                       # the signed catalog of speaker models that the installer reads


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def fingerprint(pub_b64: str) -> str:
    """A short name of a public key to show and to revoke by: the first 16 hex digits of its SHA-256."""
    return hashlib.sha256(base64.b64decode(pub_b64)).hexdigest()[:16]


def new_keypair() -> tuple:
    """-> (private key as PEM text, public key base64)"""
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pem, b64(pub)


def load_private(pem: str) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(pem.encode(), password=None)


def public_of(priv: Ed25519PrivateKey) -> str:
    return b64(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def _files(z: zipfile.ZipFile) -> dict:
    """path -> content of the package; a package that sits in one top folder is read as if the folder were not there (the same rule as the installer has)."""
    names = [i.filename for i in z.infolist() if not i.is_dir()]
    prefix = ""
    if "manifest.json" not in names:
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(tops) == 1 and (next(iter(tops)) + "/manifest.json") in names:
            prefix = next(iter(tops)) + "/"
    return {n[len(prefix):]: z.read(n) for n in names if n.startswith(prefix)}


def digest_of(files: dict) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in files if p != SIG_FILE):
        h.update(path.encode("utf-8") + b"\0" + hashlib.sha256(files[path]).hexdigest().encode() + b"\n")
    return h.hexdigest()


def files_table(files: dict) -> dict:
    """path -> SHA-256 of every file the digest covers. It goes into the signature file (last), so that a device that holds only some of the files (the speaker: the program and the manifest)
    can recompute the digest from the table, check the signature and then check its own files against the table."""
    return {p: hashlib.sha256(files[p]).hexdigest() for p in sorted(files) if p != SIG_FILE}


def sign_package(data: bytes, priv: Ed25519PrivateKey, publisher: str) -> bytes:
    """The same package with lostos-signature.json added (an older signature is replaced)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    files = _files(z)
    digest = digest_of(files)
    sig = {"v": 1, "publisher": publisher, "key": public_of(priv), "digest": digest, "sig": b64(priv.sign(PLUGIN_TAG + digest.encode())), "files": files_table(files)}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
        for path in sorted(files):
            if path != SIG_FILE:
                w.writestr(path, files[path])
        w.writestr(SIG_FILE, json.dumps(sig, ensure_ascii=False, indent=1))
    return out.getvalue()


def check_package(data: bytes) -> dict:
    """What the signature of a package says, without any trust yet: {"state": "none"|"bad"|"valid", "publisher", "key", "fingerprint"}"""
    try:
        files = _files(zipfile.ZipFile(io.BytesIO(data)))
    except zipfile.BadZipFile:
        return {"state": "none"}
    raw = files.get(SIG_FILE)
    if raw is None:
        return {"state": "none"}
    try:
        s = json.loads(raw.decode("utf-8"))
        pub = base64.b64decode(s["key"], validate=True)
        if s.get("v") != 1 or len(pub) != 32 or s["digest"] != digest_of(files):
            raise ValueError("digest")
        Ed25519PublicKey.from_public_bytes(pub).verify(base64.b64decode(s["sig"], validate=True), PLUGIN_TAG + s["digest"].encode())
    except (ValueError, KeyError, TypeError, InvalidSignature):
        return {"state": "bad"}
    return {"state": "valid", "publisher": str(s.get("publisher", ""))[:60], "key": s["key"], "fingerprint": fingerprint(s["key"])}


def trusted_keys(extra: dict = None, revoked=()) -> dict:
    """fingerprint -> {"publisher", "key", "team": bool}: the keys of the team (trusted_keys.json) and the developers of the signed registry, minus the revoked ones."""
    out = {}
    try:
        for k in json.load(open(TRUST_FILE, encoding="utf-8")).get("team", []):
            if k.get("scope"):                                 # a key with a scope (the release robot "index", the list of developers "developers") signs that one file, never a package
                continue
            out[fingerprint(k["key"])] = {"publisher": k.get("publisher", "losted.dev"), "key": k["key"], "team": True}
    except (OSError, ValueError, KeyError):
        pass
    for d in (extra or {}).values() if isinstance(extra, dict) else (extra or []):
        try:
            out.setdefault(fingerprint(d["key"]), {"publisher": d.get("name") or d.get("id", ""), "key": d["key"], "team": False})
        except (KeyError, ValueError, TypeError):
            pass
    for f in revoked:
        out.pop(f, None)
    return out


def trust_state(check: dict, keys: dict, revoked=()) -> dict:
    """check_package() + the keys we trust -> {"state": "trusted"|"known"|"none"|"bad"|"revoked", "publisher", "fingerprint"}"""
    if check["state"] != "valid":
        return {"state": check["state"]}
    fp = check["fingerprint"]
    if fp in revoked:
        return {"state": "revoked", "publisher": check["publisher"], "fingerprint": fp}
    k = keys.get(fp)
    if k:
        return {"state": "trusted", "publisher": k["publisher"] if k["team"] else check["publisher"], "fingerprint": fp, "team": k["team"]}
    return {"state": "known", "publisher": check["publisher"], "fingerprint": fp}


def sign_index(raw: bytes, priv: Ed25519PrivateKey) -> str:
    return b64(priv.sign(INDEX_TAG + raw))


def verify_index(raw: bytes, sig_b64: str, team_keys: list) -> bool:
    """Is index.json signed by one of the team keys (a list of public keys, base64)?"""
    try:
        sig = base64.b64decode(sig_b64.strip(), validate=True)
    except ValueError:
        return False
    for k in team_keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, INDEX_TAG + raw)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def team_public_keys(scope: str = "index") -> list:
    """The keys that may sign a file of the given purpose: the keys of the team without a scope (they sign anything) and the keys with just this scope.
    "index" (the index of the registry, the catalog of models) or "developers" (the list of verified developers on plugapi)."""
    try:
        return [k["key"] for k in json.load(open(TRUST_FILE, encoding="utf-8")).get("team", []) if k.get("scope") in (None, "", scope)]
    except (OSError, ValueError, KeyError):
        return []


DEVS_TAG = b"lostos-developers-v1" + bytes([10])          # the list of verified developers (plugapi.losted.dev)


def sign_developers(raw: bytes, priv: Ed25519PrivateKey) -> str:
    return sign_blob(DEVS_TAG, raw, priv)


def verify_developers(raw: bytes, sig_b64: str, keys: list = None) -> bool:
    return verify_blob(DEVS_TAG, raw, sig_b64, keys if keys is not None else team_public_keys("developers"))


SERVERS_TAG = b"lostos-servers-v1" + bytes([10])        # the list of known servers (plugapi.losted.dev/servers.json): shown by the installer, never trusted by a speaker


def sign_servers(raw: bytes, priv: Ed25519PrivateKey) -> str:
    return sign_blob(SERVERS_TAG, raw, priv)


def verify_servers(raw: bytes, sig_b64: str, keys: list = None) -> bool:
    return verify_blob(SERVERS_TAG, raw, sig_b64, keys if keys is not None else team_public_keys("developers"))


def sign_blob(tag: bytes, raw: bytes, priv: Ed25519PrivateKey) -> str:
    """A signature of any file under the name (tag) of its purpose, so that a signature of one kind of file is never taken for another kind."""
    return b64(priv.sign(tag + raw))


def verify_blob(tag: bytes, raw: bytes, sig_b64: str, team_keys: list) -> bool:
    try:
        sig = base64.b64decode(sig_b64.strip(), validate=True)
    except ValueError:
        return False
    for k in team_keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, tag + raw)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def _folder_files(folder: str) -> dict:
    files = {}
    for dp, dn, fn in os.walk(folder):
        dn[:] = [d for d in dn if d not in ("__pycache__", "data", ".git")]
        for f in fn:
            p = os.path.join(dp, f)
            files[os.path.relpath(p, folder).replace(os.sep, "/")] = open(p, "rb").read()
    return files


def sign_folder(folder: str, priv: Ed25519PrivateKey, publisher: str):
    """Writes lostos-signature.json into a folder of a plugin that is not packed (the built-in ones): the same digest as for a package."""
    files = _folder_files(folder)
    files.pop(SIG_FILE, None)
    digest = digest_of(files)
    sig = {"v": 1, "publisher": publisher, "key": public_of(priv), "digest": digest, "sig": b64(priv.sign(PLUGIN_TAG + digest.encode())), "files": files_table(files)}
    with open(os.path.join(folder, SIG_FILE), "w", encoding="utf-8", newline="\n") as f:
        json.dump(sig, f, ensure_ascii=False, indent=1)
        f.write("\n")


def check_folder(folder: str) -> dict:
    """The same answer as check_package, for a folder."""
    try:
        files = _folder_files(folder)
    except OSError:
        return {"state": "none"}
    raw = files.get(SIG_FILE)
    if raw is None:
        return {"state": "none"}
    try:
        s = json.loads(raw.decode("utf-8"))
        pub = base64.b64decode(s["key"], validate=True)
        if s.get("v") != 1 or len(pub) != 32 or s["digest"] != digest_of(files):
            raise ValueError("digest")
        Ed25519PublicKey.from_public_bytes(pub).verify(base64.b64decode(s["sig"], validate=True), PLUGIN_TAG + s["digest"].encode())
    except (ValueError, KeyError, TypeError, InvalidSignature):
        return {"state": "bad"}
    return {"state": "valid", "publisher": str(s.get("publisher", ""))[:60], "key": s["key"], "fingerprint": fingerprint(s["key"])}
