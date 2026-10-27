"""One-time Spotify login (Authorization Code with PKCE, no client secret): opens the browser, you sign in and
allow access, the tokens are saved to spotify_token.json next to this file.

    python spotify_auth.py
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(os.environ.get("LOSTOS_TENANT_DIR") or HERE, "spotify_token.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing user-top-read user-read-recently-played user-library-read user-library-modify playlist-read-private playlist-read-collaborative"


def client_id() -> str:
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("SPOTIFY_CLIENT_ID="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("SPOTIFY_CLIENT_ID is not set in .env")


def main():
    cid = client_id()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/callback"):
                got.update({k: v[0] for k, v in q.items()})
                body = ("<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:3em'>"
                        "<h2>Р“РѕС‚РѕРІРѕ, РјРѕР¶РЅРѕ Р·Р°РєСЂС‹С‚СЊ РІРєР»Р°РґРєСѓ.</h2></body></html>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8888), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge})
    print("Opening the browser. Sign in to Spotify and click 'Agree'.", flush=True)
    print(url, flush=True)
    webbrowser.open(url)

    t0 = time.time()
    while "code" not in got and "error" not in got and time.time() - t0 < 300:
        time.sleep(0.3)
    srv.shutdown()
    if "error" in got or "code" not in got:
        sys.exit(f"login failed: {got.get('error', 'timeout')}")
    if got.get("state") != state:
        sys.exit("login failed: state mismatch")

    data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": got["code"],
                                   "redirect_uri": REDIRECT, "client_id": cid, "code_verifier": verifier}).encode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.load(urllib.request.urlopen(req, timeout=20))
    tok["obtained_at"] = int(time.time())
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f)
    print("OK: tokens saved to spotify_token.json (scopes:", tok.get("scope"), ")")


if __name__ == "__main__":
    main()

