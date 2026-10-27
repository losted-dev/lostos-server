"""Latin -> Russian letters for the TTS (Silero reads only Cyrillic): "Metallica" -> "металлика".
Rule based and approximate, a fallback: the assistant is also told to write foreign names the way they sound."""
import re

_MULTI = [("tion", "шн"), ("sion", "жн"), ("igh", "айт"), ("sch", "ск"), ("tch", "ч"), ("dge", "дж"), ("ough", "ау"),
          ("sh", "ш"), ("ch", "ч"), ("th", "з"), ("ph", "ф"), ("ck", "к"), ("qu", "кв"), ("wh", "в"), ("kn", "н"),
          ("wr", "р"), ("oo", "у"), ("ee", "и"), ("ea", "и"), ("ou", "ау"), ("ow", "оу"), ("ai", "эй"), ("ay", "эй"),
          ("oy", "ой"), ("oi", "ой"), ("ng", "нг"), ("gh", ""), ("ie", "и"), ("ey", "и"), ("aw", "о"), ("au", "о")]
_ONE = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "э", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж", "k": "к",
        "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "а", "v": "в",
        "w": "в", "x": "кс", "y": "и", "z": "з"}


def _word(w: str) -> str:
    s = w.lower().replace("'", "").replace("’", "")
    if len(s) > 3 and s.endswith("e") and s[-2] not in "aeiouy":      # silent final e: "like", "love"
        s = s[:-1]
    out, i = [], 0
    while i < len(s):
        for lat, ru in _MULTI:
            if s.startswith(lat, i):
                out.append(ru)
                i += len(lat)
                break
        else:
            ch = s[i]
            if ch == "c" and i + 1 < len(s) and s[i + 1] in "eiy":
                out.append("с")
            elif ch == "g" and i + 1 < len(s) and s[i + 1] in "ey" and i > 0:
                out.append("дж")
            else:
                out.append(_ONE.get(ch, ""))
            i += 1
    return "".join(out)


def latin_to_ru(text: str) -> str:
    return re.sub(r"[A-Za-z][A-Za-z'’]*", lambda m: _word(m.group()), text)
