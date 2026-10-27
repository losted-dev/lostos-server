# Running a lostOS server (Windows, macOS, Linux)

A small server for yourself and your family: one Python process, no GPU, no big models to download. The speech goes through the cloud (the Yandex Alice engines the speaker already uses), the answers come from the
language model you choose. The only heavy dependency is `torch` (the CPU build, 100-250 MB on disk) for the voice activity detector `silero-vad`, whose model comes inside the pip package. It is light on the machine: it runs on a VDS with 1 CPU and 2 GB. Many people on one machine (a gateway with a process per person) is a different mode, see `lostos-architecture.md`.

What is verified: this mode runs on Linux (a VDS with 1 CPU and 2 GB) and its tests pass on Windows. macOS is not tried by the authors: the code has nothing system-specific, but nobody has run it there yet.

## The easy way: the installer does it

In the installer (`lostos-install`) choose "Server on this computer". It finds Python 3.11+, downloads this repository, makes a venv, installs the requirements, writes `.env` (HTTPS with a persistent private CA under `data/certs`), sets the start at login (Startup folder on Windows, a `launchd` agent on macOS, a user `systemd` unit on Linux), starts the server and links the speaker to it. The first install takes a few minutes (torch). The server lives in `%LOCALAPPDATA%\lostos-server` (Windows) or `~/.lostos-server`. Terminal form: `lostos-install --local-server <speaker ip>`; `--server-src <folder or zip>` takes the code from disk instead of GitHub.

The rest of this page is the same thing by hand.

## What you need

- Python 3.11 or newer (3.14 is what the authors' server runs).
- **An address that the speaker reaches.** Two kinds:
  - from the internet: HTTPS with a real certificate (a domain of your own behind Caddy is the simplest, or a tunnel service that gives you an HTTPS name);
  - inside your home network: use the local-server installer. It creates an HTTPS certificate for the computer’s IP and provisions the public CA to the speaker over ADB. Import `data/certs/ca.crt` into your browser trust store before opening the setup link. Never copy `ca.key` or `server.key` to another device.

## Install

```bash
git clone https://github.com/losted-dev/lostos-server
cd lostos-server/server
python -m venv ../venv
../venv/bin/pip install -r requirements.txt          # Windows: ..\venv\Scripts\pip install -r requirements.txt
```

Create the file `server/.env` (the server reads it itself, on every system; it is in `.gitignore`):

```
LOSTOS_LITE=1
LOSTOS_AUTH=1
LOSTOS_PUBLIC_URL=https://your.domain
TTS_PORT=8765
LOSTOS_DATA=/full/path/to/data
LOSTOS_NO_LOCAL_ENGINES=1
```

`LOSTOS_DATA` is an existing folder, an absolute path (Windows: `C:/lostos-data`). Write no comments after a value on the same line.

For local HTTPS the installer sets `LOSTOS_LOCAL_TLS=1`, `LOSTOS_PUBLIC_URL=https://<LAN-IP>:8765` and `LOSTOS_BIND=0.0.0.0`. HTTP listeners are permitted only on loopback behind a TLS proxy. Existing local HTTP installations must be restarted and their speaker reconfigured with the updated installer; old firmware must also be updated. Previously exposed keys and sessions should be rotated if the LAN was compromised.

Optional lines: `LOSTOS_TG_TOKEN` (the Telegram bot for login and notifications), `LOSTOS_TG_CLIENT_ID`, `LOSTOS_TG_CLIENT_SECRET` (login through Telegram). Without them you enter with a one-time link.
Keys of services (Gemini, Spotify, Home Assistant token) do NOT go to this file: they go to the panel, where they are stored encrypted, and the key to them lives on the speaker.

Run:

```bash
../venv/bin/python -u voice_server.py                # Windows: ..\venv\Scripts\python -u voice_server.py
```

The server listens on `127.0.0.1:8765` only. Put an HTTPS proxy in front of it. With Caddy the whole config is:

```
your.domain {
	reverse_proxy 127.0.0.1:8765 {
		flush_interval -1
	}
}
```

The first start prints a one-time login link (24 hours, works once) and writes it to `data/setup-link.txt`. A new one: `python auth.py new-link https://your.domain`.

## Connect the speaker

Run the installer (`lostos-install`, the repository `lostos`, the release page), choose "your server" and enter `https://your.domain`. The installer prints the identifier of the speaker.
In the panel: Settings, Account, Stations, "Connect a speaker", the identifier, then press the Alice button on the speaker when it says so.

## Start with the system

- Linux: `tools/systemd/lostos.service` is the unit of the many-people mode; for one server process copy it and replace `gateway.py` with `voice_server.py`.
- Windows: Task Scheduler, "At log on", the action is the `python.exe` of the venv with the arguments `-u voice_server.py`, the start folder is `server`.
- macOS: a `launchd` agent with the same command.

## Updating

`git pull`, `pip install -r requirements.txt`, restart. The data (`data/`) and `.env` are not in the repository and stay.

## If something does not work

- The speaker does not appear in Stations: open `https://your.domain/api/v1/hello` in a browser. If it does not answer, the problem is the proxy or the certificate, not lostOS.
- The panel says that it is locked: that is normal until the first speaker is confirmed.
- Logs: the console of the process (or the journal of the service).

## Server plugin isolation

User server plugins require Linux, bubblewrap (`bwrap`) and working user namespaces. Install bubblewrap with the distribution package manager before enabling plugins. Windows/macOS and Linux without a working sandbox refuse user plugin execution; built-in services still work. Plugins receive a minimal environment, their package and Python dependencies read-only, the SDK, and only their own data directory writable. Files of the server and other households are not mounted. Network access remains shared for cloud integrations, including access to local services: these permissions are not a network destination policy. Keep loopback services authenticated.

The installer and server TLS changes must ship together. Test speech playback and device plugin installation on the actual speaker model before release.
