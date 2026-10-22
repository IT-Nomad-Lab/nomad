#!/usr/bin/env python3
"""One-time Google consent with offline access (#1289, PRD docs/prd/secrets-vault-google-broker.md).

Prints the authorization URL, listens on 127.0.0.1:8767 for the redirect, exchanges the code and
writes the token response to a 0600 file. Never prints a token. The client comes from ~/nomad/.env
(GOOGLE_MCP_LOCAL_CLIENT_ID / _SECRET).

  google_consent.py [--out PATH]
"""
import base64, hashlib, http.server, json, os, secrets, sys, urllib.parse, urllib.request

ENV = os.path.expanduser("~/nomad/.env")
PORT = 8767
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
]


def env(name):
    for line in open(ENV):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit(f"{name} missing in {ENV}")


def main():
    out = os.path.expanduser("~/.config/nomad/google-broker/consent.json")
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    cid, csec = env("GOOGLE_MCP_LOCAL_CLIENT_ID"), env("GOOGLE_MCP_LOCAL_CLIENT_SECRET")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and q.get("code"):
                got["code"] = q["code"][0]
                msg = b"Consent received. You can close this tab."
            else:
                got["error"] = q.get("error", ["bad state"])[0]
                msg = b"Consent failed. See the terminal."
            self.send_response(200); self.end_headers(); self.wfile.write(msg)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    srv.timeout = 900
    print("Open this URL in the browser signed in as the operator:\n" + url, flush=True)
    while not got:
        srv.handle_request()
        if srv.timeout and not got and False:
            break
    if "code" not in got:
        sys.exit("consent failed: " + got.get("error", "?"))
    body = urllib.parse.urlencode({
        "code": got["code"], "client_id": cid, "client_secret": csec, "redirect_uri": REDIRECT,
        "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    try:
        tok = json.load(urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", body)))
    except urllib.error.HTTPError as e:
        sys.exit("token exchange failed: " + e.read().decode()[:300])
    os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    print("saved", out, "| refresh_token:", bool(tok.get("refresh_token")), "| expires_in:", tok.get("expires_in"),
          "| scopes:", len(tok.get("scope", "").split()))


if __name__ == "__main__":
    main()
