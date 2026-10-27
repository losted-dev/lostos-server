"""Several stations in one household: the vault keyring (every station opens the same vault with its own key), reading a vault of the old format, the sharing of changes between
the processes, the owner of an alarm. Run:  python -I server/tests_stations.py"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="lostos-st-")
os.environ["LOSTOS_DATA"] = TMP
os.environ["LOSTOS_TENANT_DIR"] = TMP
os.environ["LOSTOS_STATION"] = "aaaa1111aaaa1111aaaa"
os.environ["LOSTOS_PRIMARY"] = "1"
OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


import vault as vmod
import shared

A, B = "aaaa1111aaaa1111aaaa", "bbbb2222bbbb2222bbbb"
ka, kb = os.urandom(32), os.urandom(32)

v1 = vmod.Vault(lambda *a: None)
check("the first station creates the vault", v1.unlock(ka, A) and not v1.locked)
v1.set("GEMINI_API_KEY", "secret-one")
v2 = vmod.Vault(lambda *a: None)
check("a second station has no key to it yet", v2.unlock(kb, B) is False and v2.locked)
v1.wrap_for_peers({B: kb})
check("after the first one adds its key the second opens the same vault", v2.unlock(kb, B) and v2.get("GEMINI_API_KEY") == "secret-one")
v2.set("OPENAI_API_KEY", "secret-two")
v1.refresh()
check("what the second saves, the first sees", v1.get("OPENAI_API_KEY") == "secret-two")
raw = open(vmod.FILE, "rb").read()
check("the file holds no plain text", b"secret-one" not in raw and b"secret-two" not in raw)
v3 = vmod.Vault(lambda *a: None)
check("a wrong key does not open it", v3.unlock(os.urandom(32), A) is False)
v1.lock()
v2b = vmod.Vault(lambda *a: None)
check("the second station opens it alone when the first is away", v2b.unlock(kb, B) and v2b.get("GEMINI_API_KEY") == "secret-one")
v2.lock(); v2b.lock()

# the old format: the key of the only station encrypts the data itself
os.remove(vmod.FILE)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
nonce = os.urandom(12)
open(vmod.FILE, "w").write(json.dumps({"v": 1, "nonce": base64.b64encode(nonce).decode(),
                                       "data": base64.b64encode(AESGCM(ka).encrypt(nonce, json.dumps({"GEMINI_API_KEY": "old"}).encode(), b"lostos-vault-v1")).decode()}))
v4 = vmod.Vault(lambda *a: None)
check("a vault of the old format opens and is converted", v4.unlock(ka, A) and v4.get("GEMINI_API_KEY") == "old" and json.load(open(vmod.FILE))["v"] == 2)
v4.lock()

# alarms: who rings
check("an alarm of this station rings here", shared.owns({"station": A}))
check("an alarm of another station does not", not shared.owns({"station": B}))
check("an alarm without an owner rings at the main station", shared.owns({}))
child = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import shared; print(shared.owns({}), shared.owns({'station': 'x'}), shared.fires())" % HERE],
                       env=dict(os.environ, LOSTOS_PRIMARY="0"), capture_output=True, text=True).stdout.split()
check("a station that is not the main one leaves the ownerless alarms alone", child == ["False", "False", "False"], child)
child = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import shared; print(shared.owns({}), shared.fires())" % HERE],
                       env=dict(os.environ, LOSTOS_STATION=""), capture_output=True, text=True).stdout.split()
check("a process without a station rings nothing", child == ["False", "False"], child)

shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
