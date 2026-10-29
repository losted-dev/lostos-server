"""Short reactions right after her answer ("да", "не знаю", "ага") are for her (voice_server.backchannel_reply). Run from the server folder."""
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


for text in ("Да.", "Не знаю.", "Ага.", "Угу", "Ну да", "Не знаю, наверное", "Хорошо.", "Нет.", "Не надо.", "Ладно, спасибо", "Может быть.", "А не знаю", "Да нет"):
    check(f"a reaction: {text!r}", v.backchannel_reply(text) is not None, text)
for text in ("Включи свет на кухне", "Да включи уже музыку погромче", "Не знаю, какая сегодня погода в Москве", "Расскажи про фотосинтез", "Он сказал да и ушёл", "Привет, как дела у тебя сегодня"):
    check(f"not a reaction: {text!r}", v.backchannel_reply(text) is None, text)
check("'не знаю' is answered kindly", any(v.backchannel_reply("Не знаю.") in group for group in (v.BACKCHANNEL_REPLIES["dk"],)))
check("'нет' is answered as a refusal", v.backchannel_reply("Не хочу.") in v.BACKCHANNEL_REPLIES["no"])
for text, more in (("Не знаю пока.", True), ("Пока нет.", True), ("Да не знаю", True), ("Пока.", False), ("Спасибо, пока!", False), ("Хватит.", False)):
    check(f"the dialog goes on={more}: {text!r}", v.wants_more(text) is more, text)
# A wake word that fired late (Station 1): the request is in the context before it.
for text, want in (("Алиса, привет.", ("", "привет")), ("Ну вот. Алиса, включи музыку!", ("Ну вот.", "включи музыку")),
                   ("Мы говорили про погоду. Алиса.", ("Мы говорили про погоду.", "")), ("Алиса, вк", ("", "")),
                   ("Яндекс, сколько времени?", ("", "сколько времени")), ("Просто разговор без имени", ("Просто разговор без имени", "")),
                   ("Алиса сказала, что Алиса, привет", ("Алиса сказала, что", "привет"))):
    check(f"late wake word: {text!r}", v.split_late_wake(text) == want, v.split_late_wake(text))
print("ALL PASS" if OK else "SOME FAILED")
sys.exit(0 if OK else 1)
