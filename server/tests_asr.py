"""The choice between the final text of the cloud recognizer and its partial results (a phrase that the final answer cut short). No network. Run: python -I server/tests_asr.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yandex_asr as y

OK = True


def check(name, got, want):
    global OK
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" {got!r} != {want!r}"))
    OK = OK and ok


check("a final cut to one word gets the words the partial results had", y.longest("Включи.", ["", "включи о", "включи онда андрей", "включи"]), "Включи онда андрей")
check("a complete final stays", y.longest("Включи Ван.", ["включи", "включи ван"]), "Включи Ван.")
check("a partial that does not start with the final's words is not used", y.longest("Включи свет.", ["включи", "выключи свет"]), "Включи свет.")
check("an empty final falls back to the best partial", y.longest("", ["включи", "включи свет"]), "Включи свет")
check("nothing at all stays empty", y.longest("", []), "")
check("the same words with other punctuation keep the final", y.longest("Алиса, включи.", ["алиса включи"]), "Алиса, включи.")
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
