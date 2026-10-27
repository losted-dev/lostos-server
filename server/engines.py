"""The optional local engines (Whisper for speech recognition, Silero for speech): are they installed, and install them from the panel.
The server starts without them; the panel lets one be chosen only after it is installed. LOSTOS_MISSING=whisper,silero pretends they are not (for tests)."""
import importlib.util
import os
import subprocess
import sys
import threading

SILERO_URL = "https://models.silero.ai/models/tts/ru/v5_ru.pt"
STATE = {"whisper": {"busy": False, "text": ""}, "silero": {"busy": False, "text": ""}}


def _pretend_missing(name: str) -> bool:
    return name in os.environ.get("LOSTOS_MISSING", "")


def whisper_installed(model: str) -> bool:
    if _pretend_missing("whisper") or importlib.util.find_spec("transformers") is None:
        return False
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(model, local_files_only=True)
        return True
    except Exception:
        return False


def silero_installed(path: str) -> bool:
    return not _pretend_missing("silero") and os.path.exists(path) and importlib.util.find_spec("num2words") is not None


def _pip(*pkgs):
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *pkgs], check=True, capture_output=True, timeout=1800)


def blocked() -> bool:
    """LOSTOS_NO_LOCAL_ENGINES=1 in the environment of the server (the VDS): Whisper and Silero are never downloaded or installed here."""
    return os.environ.get("LOSTOS_NO_LOCAL_ENGINES") == "1"


def install(name: str, whisper_model: str, silero_path: str, log) -> str:
    """Starts the installation in a thread and returns at once; the progress is in STATE[name]['text']."""
    st = STATE.get(name)
    if st is None:
        return "неизвестный движок"
    if blocked():
        return "на этом сервере локальные движки запрещены настройкой"
    if st["busy"]:
        return "уже устанавливается"

    def work():
        st["busy"] = True
        try:
            if name == "whisper":
                if importlib.util.find_spec("transformers") is None:
                    st["text"] = "Ставлю библиотеку transformers…"
                    _pip("transformers", "accelerate")
                st["text"] = "Скачиваю модель Whisper (около 1,6 ГБ)…"
                from huggingface_hub import snapshot_download
                snapshot_download(whisper_model)
            else:
                if importlib.util.find_spec("num2words") is None:
                    st["text"] = "Ставлю библиотеку num2words…"
                    _pip("num2words")
                st["text"] = "Скачиваю модель Silero (около 100 МБ)…"
                import torch
                os.makedirs(os.path.dirname(silero_path), exist_ok=True)
                torch.hub.download_url_to_file(SILERO_URL, silero_path)
            st["text"] = ""
            log(f"{name} installed")
        except Exception as e:
            st["text"] = f"Не получилось: {str(e)[:120]}"
            log(f"{name} install failed: {e!r}")
        finally:
            st["busy"] = False

    st["text"] = "Начинаю…"
    threading.Thread(target=work, daemon=True).start()
    return "ok"
