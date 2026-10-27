"""Which lostOS server this is: the CENTER (the one of the lostOS team) or a HOST (anybody else who runs the published server for his own people).

The center keeps what only the team may touch: the key that signs the list of verified developers and the list of known and banned servers (plugapi.losted.dev), the registry of plugins.
Their sections of the admin panel and their commands exist there only. A host gets the part that is about his own server: the people, the registration, the stations, the limits, the memory.

How it is told: the word "center" in the file DATA/edition (or in the environment variable LOSTOS_EDITION). Without it the server is a host. The file is read at every call: a switch needs no restart."""
import os

CENTER_ONLY = "этот раздел есть только на центральном сервере lostOS"


def edition(data_dir: str = None) -> str:
    word = os.environ.get("LOSTOS_EDITION", "").strip().lower()
    if not word:
        try:
            word = open(os.path.join(data_dir or os.environ.get("LOSTOS_DATA") or ".", "edition"), encoding="utf-8").read().strip().lower()
        except OSError:
            word = ""
    return "center" if word == "center" else "host"
