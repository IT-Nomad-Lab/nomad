#!/usr/bin/env python3
"""Load the Google OAuth client and refresh token into the vault (secret/google/local).

Reads the client from ~/nomad/.env and the token file written by mcp-gateway/google_consent.py,
uses the root token on this host. Prints no secret. After a good write, the token file can be
deleted (--delete-source).
"""
import json, os, sys, urllib.request

D = os.path.expanduser("~/.config/nomad/openbao")
TOKEN_FILE = os.path.expanduser("~/.config/nomad/google-broker/consent.json")
ADDR = os.environ.get("BAO_ADDR", "http://127.0.0.1:8205")


def env(name):
    for line in open(os.path.expanduser("~/nomad/.env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit(f"{name} missing")


tok = json.load(open(TOKEN_FILE))
if not tok.get("refresh_token"):
    sys.exit("no refresh_token in " + TOKEN_FILE)
data = {"client_id": env("GOOGLE_MCP_LOCAL_CLIENT_ID"), "client_secret": env("GOOGLE_MCP_LOCAL_CLIENT_SECRET"),
        "refresh_token": tok["refresh_token"], "scope": tok.get("scope", "")}
req = urllib.request.Request(f"{ADDR}/v1/secret/data/google/local", json.dumps({"data": data}).encode(),
                             {"X-Vault-Token": open(f"{D}/root.token").read().strip(), "Content-Type": "application/json"},
                             method="POST")
r = json.load(urllib.request.urlopen(req))
print("stored secret/google/local version", r["data"]["version"], "scopes", len(data["scope"].split()))
if "--delete-source" in sys.argv:
    os.remove(TOKEN_FILE)
    print("deleted", TOKEN_FILE)
