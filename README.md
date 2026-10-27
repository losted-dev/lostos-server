# lostOS Server

The server of lostOS: the voice assistant of a Yandex Station that runs lostOS (see the repository `lostos`). It listens through the speaker, understands the phrase (the cloud of Yandex or a local model),
asks a language model of your choice (Gemini, Claude, DeepSeek, ChatGPT or any OpenAI-compatible service), runs the commands (music, timers, scenarios, Home Assistant) and speaks back.
It also serves the panel (a web app for the phone and the computer) and keeps your keys encrypted: the key to them lives on the speaker.

Two ways to run it:

- **A small server of your own** (Windows, macOS, Linux): one process, no GPU, cloud speech. Step by step: `docs/server-setup.md`. An AI agent can do it for you: it reads `AGENTS.md`.
- **At home with local models**: `python voice_server.py <speaker ip>` on a computer in the same network (needs torch and the models, see `docs/models.md`).
- **On the internet for many people** (a VDS): a gateway (`gateway.py`) with a server process of its own for every person, one shared engine, an admin panel, registration by approval of the operator,
  memory limit per person. See `docs/` and `tools/systemd/`. This mode is for friends and small groups, it is not a public service.

Plugins live in the repository `lostos-plugins` (a signed registry; the server checks the signature before it installs anything).

Tests: `python -I server/tests_*.py` (see each file for what it needs).

License: MIT.
