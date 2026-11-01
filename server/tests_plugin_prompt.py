"""What the running plugins tell the assistant's model about their commands (manifest "prompt", plugins.prompt_hints, voice_server.system_prompt).
Run from the server folder."""
import os
import sys

os.environ["LOSTOS_MODE"] = "tenant"
os.environ["LOSTOS_LITE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_server as v

OK = True


def check(name, cond, extra=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    OK = OK and bool(cond)


class Fake:
    def __init__(self, hints):
        self.hints = hints

    def prompt_hints(self):
        return self.hints


HINT = "Интернет-радио: вызывай play_radio, а НЕ spotify_play"
v.PLUGINS = None
check("no plugins: no hint", HINT not in v.system_prompt())
v.PLUGINS = Fake([])
check("no plugin with a hint: none", HINT not in v.system_prompt())
v.PLUGINS = Fake([HINT])
p = v.system_prompt()
check("a running plugin's hint is in the prompt", HINT in p)
check("the usual prompt is still there", "Spotify" in p and len(p) > 5000, len(p))


class Broken:
    def prompt_hints(self):
        raise RuntimeError("boom")


v.PLUGINS = Broken()
check("a failing plugin manager does not break the prompt", HINT not in v.system_prompt() and len(v.system_prompt()) > 5000)
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
