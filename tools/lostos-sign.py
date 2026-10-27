#!/usr/bin/env python3
"""Keys, signatures and the registry index of lostOS plugins (the format is in server/plugin_sign.py).

  lostos-sign.py keygen <file.pem>                          a new key pair: the private key goes to the file, the public one is printed (keep the file secret and backed up)
  lostos-sign.py sign <package.lstplugin> <key.pem> <publisher>   signs the package in place (adds lostos-signature.json)
  lostos-sign.py sign-folder <folder> <key.pem> <publisher> signs a plugin that is not packed (the built-in ones in server/plugins/system)
  lostos-sign.py check <package.lstplugin>                  what the signature of a package says
  lostos-sign.py catalog <catalog.json> <team-key.pem>      signs the catalog of speaker models that the installer reads (catalog.json.sig)
  lostos-sign.py index <registry-folder> <team-key.pem>     builds index.json and index.json.sig from registry-folder/plugins/*.lstplugin and registry-folder/developers.json

The folder of a registry (the repository lostos-plugins):
  developers.json      [{"id": "demo", "name": "Demo", "key": "<public key>"}]   the developers whose signatures are trusted
  revoked.json         ["<fingerprint>", ...]                                    keys that are no longer trusted
  plugins/<id>-<version>.lstplugin                                               signed packages
  index.json, index.json.sig                                                     made by this tool, signed by the team key"""
import datetime
import hashlib
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.environ.get("LOSTOS_SERVER", ""), os.path.join(os.environ.get("LOSTOS_SERVER", "/nonexistent"), "server"), os.path.join(HERE, "..", "server"),
             os.path.join(HERE, "..", "_server", "server"), HERE):   # the project, a checkout of lostos-server (CI), or next to this file
    if cand and os.path.isfile(os.path.join(cand, "plugin_sign.py")):
        sys.path.insert(0, os.path.abspath(cand))
        break
import plugin_sign as ps


def manifest_of(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    names = [i.filename for i in z.infolist() if not i.is_dir()]
    prefix = ""
    if "manifest.json" not in names:
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        prefix = next(iter(tops)) + "/" if len(tops) == 1 else ""
    return json.loads(z.read(prefix + "manifest.json").decode("utf-8"))


def main(argv) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "keygen" and len(argv) == 2:
        if os.path.exists(argv[1]):
            sys.exit("the file exists: " + argv[1])
        pem, pub = ps.new_keypair()
        with open(argv[1], "w") as f:
            f.write(pem)
        try:
            os.chmod(argv[1], 0o600)
        except OSError:
            pass
        print("public key:", pub)
        print("fingerprint:", ps.fingerprint(pub))
        return 0
    if cmd == "sign" and len(argv) == 4:
        data = open(argv[1], "rb").read()
        out = ps.sign_package(data, ps.load_private(open(argv[2]).read()), argv[3])
        open(argv[1], "wb").write(out)
        print("signed:", argv[1], ps.check_package(out))
        return 0
    if cmd == "sign-folder" and len(argv) == 4:
        ps.sign_folder(argv[1], ps.load_private(open(argv[2]).read()), argv[3])
        print("signed folder:", argv[1], ps.check_folder(argv[1]))
        return 0
    if cmd == "check" and len(argv) == 2:
        c = ps.check_package(open(argv[1], "rb").read())
        print(c, "-> trust:", ps.trust_state(c, ps.trusted_keys()))
        return 0
    if cmd == "index" and len(argv) == 3:
        root = argv[1]
        devs = json.load(open(os.path.join(root, "developers.json"), encoding="utf-8")) if os.path.exists(os.path.join(root, "developers.json")) else []
        revoked = json.load(open(os.path.join(root, "revoked.json"), encoding="utf-8")) if os.path.exists(os.path.join(root, "revoked.json")) else []
        keys = ps.trusted_keys(devs, revoked)
        plugins = []
        for name in sorted(os.listdir(os.path.join(root, "plugins"))):
            if not name.endswith(".lstplugin"):
                continue
            data = open(os.path.join(root, "plugins", name), "rb").read()
            st = ps.trust_state(ps.check_package(data), keys, revoked)
            if st["state"] not in ("trusted",):
                sys.exit(f"{name}: not signed by a trusted key ({st['state']}), the index is not built")
            m = manifest_of(data)
            plugins.append({"id": m["id"], "name": m.get("name", m["id"]), "version": str(m.get("version", "")), "about": str(m.get("about") or m.get("description") or "")[:200],
                            "publisher": st["publisher"], "fingerprint": st["fingerprint"], "file": "plugins/" + name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                            "min_firmware": m.get("min_firmware", "")})
        index = {"v": 1, "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "developers": devs, "revoked": revoked, "plugins": plugins}
        raw = json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8")
        open(os.path.join(root, "index.json"), "wb").write(raw)
        open(os.path.join(root, "index.json.sig"), "w").write(ps.sign_index(raw, ps.load_private(open(argv[2]).read())))
        print(f"index.json: {len(plugins)} plugins, signed")
        return 0
    if cmd == "catalog" and len(argv) == 3:
        doc = json.loads(open(argv[1], "rb").read().decode("utf-8"))
        for m in doc["models"]:                                       # the checksum of every profile file next to the catalog goes into it before it is signed
            f = os.path.join(os.path.dirname(os.path.abspath(argv[1])), m.get("profile", ""))
            if m.get("profile") and os.path.exists(f):
                m["sha256"] = hashlib.sha256(open(f, "rb").read()).hexdigest()
        raw = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
        open(argv[1], "wb").write(raw)
        open(argv[1] + ".sig", "w").write(ps.sign_blob(ps.MODELS_TAG, raw, ps.load_private(open(argv[2]).read())))
        print("catalog signed:", argv[1] + ".sig")
        return 0
    print(__doc__)
    return 0 if not cmd else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
