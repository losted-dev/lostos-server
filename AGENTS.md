# AGENTS.md: lostOS server

Instructions for an AI coding agent (and a quick map for a person) working in this repository or setting a server up for its owner.

## What this is

The server of lostOS, the firmware of Yandex Stations. Python 3.11+, aiohttp, no framework. The speaker connects OUT to the server (`/link/ws`, `server/link.py`); the server talks to the speaker's own API through ports that it opens on its loopback.
The panel (a web app) is in `server/web/`. Plugins are signed packages: the registry is the repository `lostos-plugins`, the speaker firmware is in the repository `lostos`.

## Set up a server for the owner

Follow `docs/server-setup.md` step by step. Facts an agent must not guess:

- The speaker needs an **HTTPS address with a real certificate**. Ask the owner for the domain (or the tunnel); do not use a bare IP.
- Keys of services never go to `.env`, to the repository or to the chat: they are entered in the panel and stored in the encrypted vault (`server/vault.py`).
- Never ask for, print or store the owner's passwords, tokens or private keys. A one-time login link is printed by the server itself.
- The one-time login link and `data/` are private. `.env` and `data/` are never committed.

## Run

```
cd server
python -m venv ../venv && ../venv/bin/pip install -r requirements.txt
python -u voice_server.py            # reads server/.env
```

Many people on one machine: `python gateway.py` with a shared engine (`engine_service.py`), see `docs/lostos-architecture.md`.

## Tests

Each `server/tests_*.py` is a program that prints PASS / FAIL lines and `ALL PASS` at the end. Run them with `python -I server/tests_<name>.py` from the repository root. The gateway suite is `tests_gateway.py`. A change to the panel or the server needs the matching suite green before it is done.
The panel has no build step: edit `server/web/*`, raise the version in `server/web/sw.js` (`CACHE`), reload with Ctrl+F5.

## Rules of the code

- Texts of the interface are Russian and end without a final period; the status words are "В сети" / "Не в сети".
- Everything inside `server/web/app.js` stays inside its `(() => { ... })();` wrapper.
- Plugins are signed with Ed25519 (`tools/lostos-sign.py`); the lists of developers and servers are signed with the keys of the team and checked by the speaker and the installer. Do not weaken a check to make a test pass.
- The firmware is not signed: its protection is the protected tags of the repository `lostos`.
- Do not put the address of any real server, a key or a token into the repository: the secret scanner of `tools/prepare-publish.py` refuses it.

## Layout

- `server/voice_server.py`: the process of one household; `gateway.py`: the door of the many-people mode; `link.py`: the speaker link; `panel.py`, `account_api.py`, `setup_wizard.py`: the HTTP API of the panel.
- `server/plugins/system/`: the built-in plugins (signed). `server/web/`: the panel. `tools/`: signing, staging, systemd units.
- `docs/`: `server-setup.md`, `plugins.md`, `lostos-architecture.md`, `models.md`.
