"""One plugin in several versions in the index: the panel shows and installs the newest only (registry.Registry.view / newest). Run from the server folder."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


r = object.__new__(registry.Registry)
r.url, r.error, r.devs_error = "https://example.invalid", "", ""
r.developers_source = lambda: "index"
r.index = {"plugins": [
    {"id": "a.radio", "name": "Radio", "version": "1.0.0", "file": "plugins/a.radio-1.0.0.lstplugin"},
    {"id": "a.other", "name": "Other", "version": "2.0.0", "file": "plugins/a.other-2.0.0.lstplugin"},
    {"id": "a.radio", "name": "Radio", "version": "1.0.1", "file": "plugins/a.radio-1.0.1.lstplugin"},
    {"id": "a.radio", "name": "Radio", "version": "1.0.10", "file": "plugins/a.radio-1.0.10.lstplugin"},
]}
newest = {p["id"]: p["version"] for p in r.newest()}
check("the newest version of every plugin", newest == {"a.radio": "1.0.10", "a.other": "2.0.0"}, newest)
v = r.view({})
check("the list has every plugin once", sorted(p["id"] for p in v["plugins"]) == ["a.other", "a.radio"], v["plugins"])
check("an installed old version gets an update offer", [p for p in r.view({"a.radio": "1.0.0"})["plugins"] if p["id"] == "a.radio"][0]["update"] is True)
check("the newest installed: no update", [p for p in r.view({"a.radio": "1.0.10"})["plugins"] if p["id"] == "a.radio"][0]["update"] is False)
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
