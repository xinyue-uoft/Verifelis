"""OpenAI OAuth (PKCE authorization-code flow).

Endpoints and parameters verified against the open-source openai/codex CLI
(codex-rs/login): auth.openai.com, S256, localhost callback on port 1455.
Tokens are stored in ~/.config/verifelis/openai_auth.json (mode 0600).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
PORT = 1455
REDIRECT_URI = f"http://localhost:{PORT}/auth/callback"
SCOPES = "openid profile email offline_access"

AUTH_FILE = Path.home() / ".config" / "verifelis" / "openai_auth.json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def load_tokens() -> dict | None:
    if AUTH_FILE.exists():
        return json.loads(AUTH_FILE.read_text())
    return None


def save_tokens(tokens: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(tokens, indent=2))
    os.chmod(AUTH_FILE, 0o600)


def get_access_token() -> str | None:
    """Return a valid access token, refreshing if expired."""
    tokens = load_tokens()
    if not tokens:
        return None
    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens.get("access_token")
    if tokens.get("refresh_token"):
        try:
            return _refresh(tokens)
        except httpx.HTTPError:
            return None
    return tokens.get("access_token")


def _refresh(tokens: dict) -> str:
    resp = httpx.post(
        f"{ISSUER}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    tokens.update(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", tokens["refresh_token"]),
        expires_at=time.time() + data.get("expires_in", 3600),
    )
    save_tokens(tokens)
    return tokens["access_token"]


def login_interactive(timeout_s: int = 300) -> str:
    """Run the browser PKCE flow. Blocks until callback or timeout."""
    verifier, challenge = _pkce()
    state = _b64url(secrets.token_bytes(24))
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = f"{ISSUER}/oauth/authorize?{urllib.parse.urlencode(params)}"

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result["code"] = q.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Verifelis: login complete. You may close this tab.</h3>")
            done.set()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    webbrowser.open(url)
    print(f"If the browser did not open, visit:\n{url}")
    try:
        if not done.wait(timeout=timeout_s):
            raise TimeoutError("OAuth callback not received")
    finally:
        server.shutdown()
    code = result.get("code")
    if not code:
        raise RuntimeError("no authorization code in callback")
    resp = httpx.post(
        f"{ISSUER}/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "id_token": data.get("id_token", ""),
        "expires_at": time.time() + data.get("expires_in", 3600),
    }
    save_tokens(tokens)
    return tokens["access_token"]


def login_paste_token(token: str) -> str:
    """Fallback: store a user-pasted API key or access token."""
    save_tokens({"access_token": token.strip(), "expires_at": time.time() + 10 * 365 * 86400})
    return token.strip()
