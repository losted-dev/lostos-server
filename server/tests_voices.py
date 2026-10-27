"""Tests of the voice prints in the database: every print belongs to a person, each person sees only his own, the owner sees all.
Run from the project folder: python -I server/tests_voices.py"""
import os
import sys
import tempfile

DATA = tempfile.mkdtemp(prefix="lostos-voices-")
os.environ["LOSTOS_DATA"] = DATA
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import accounts
import voices

OK = True


def check(name, cond):
    global OK
    print(("PASS " if cond else "FAIL ") + name)
    OK = OK and bool(cond)


def vec(seed):
    r = np.random.default_rng(seed).normal(size=192).astype(np.float32)
    return r / np.linalg.norm(r)


# a print made before the database existed (the old file) moves into it once
v = voices.Voices()
v.add("Старый", [vec(1), vec(1)])
check("without a database the prints go to the file", os.path.exists(os.path.join(DATA, "voices.json")))
people = accounts.Accounts()
store = accounts.VoiceStore(people)
v.attach(store)
check("the old file moved into the database", "Старый" in store.load())

v.add("Аня", [vec(2), vec(2)], owner=100)
v.add("Боря", [vec(3), vec(3)], owner=200)
check("the owner sees all the voices", sorted(v.visible(100, "owner")) == ["Аня", "Боря", "Старый"])
check("a member sees only his own", v.visible(200, "member") == ["Боря"])
check("a stranger sees nothing", v.visible(300, "member") == [])

v2 = voices.Voices()
v2.attach(store)
check("a restart finds everything in the database with the owners", sorted(v2.people) == ["Аня", "Боря", "Старый"] and v2.people["Аня"]["owner"] == 100)
name, score = v2.identify(vec(2))
check("a voice is recognised by the shared service whoever it belongs to", name == "Аня" and score > 0.99)
name, _ = v2.identify(vec(3))
check("another person's voice too", name == "Боря")
v2.note("Аня", "любит джаз")
v2.rename("Боря", "Борис")
v2.delete("Старый")
v3 = voices.Voices()
v3.attach(store)
check("notes, renames and deletes are saved", sorted(v3.people) == ["Аня", "Борис"] and v3.people["Аня"]["note"] == "любит джаз" and v3.people["Борис"]["owner"] == 200)
v3.add("Аня", [vec(2)], owner=999)
check("more samples of a voice do not change its owner", v3.people["Аня"]["owner"] == 100)
import panel

owner_req = {"session": {"tg_id": 100, "role": "owner"}}
member_req = {"session": {"tg_id": 200, "role": "member"}}
check("without accounts everybody sees everything", panel.mine_item({}, {"tg": 100}) and panel.mine_item({}, {"tg": None}))
check("a member sees his own dialogs only", panel.mine_item(member_req, {"tg": 200}) and not panel.mine_item(member_req, {"tg": 100}) and not panel.mine_item(member_req, {"tg": None}))
check("the owner sees his own and those of unknown voices, not the members'", panel.mine_item(owner_req, {"tg": 100}) and panel.mine_item(owner_req, {"tg": None}) and not panel.mine_item(owner_req, {"tg": 200}))
people.log(200, "dialog", "привет → здравствуй")
people.log(100, "login", "по ссылке")
app = type("A", (), {"get": lambda self, k, d=None: people if k == "accounts" else d})()
member_req2 = {"session": {"tg_id": 200, "role": "member"}}
class R(dict):
    app = app
m = type("M", (), {"LOG_RING": ["служебная строка"]})()
lines_member = panel.own_log(R(member_req2), m)
lines_owner = panel.own_log(R({"session": {"tg_id": 100, "role": "owner"}}), m)
check("a member's log has only his own events", len(lines_member) == 1 and "здравствуй" in lines_member[0])
check("the owner's log has only his events too: no server journal, nothing of others", any("по ссылке" in l for l in lines_owner) and "служебная строка" not in lines_owner and not any("здравствуй" in l for l in lines_owner))
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
