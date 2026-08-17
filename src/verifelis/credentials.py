"""Persistent API-key store: ~/.config/verifelis/credentials.json (0600).

Paths are computed at call time so tests can monkeypatch cred_file().
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def cred_file() -> Path:
    return Path.home() / ".config" / "verifelis" / "credentials.json"


def load_all() -> dict[str, str]:
    f = cred_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def get(name: str) -> str:
    return load_all().get(name, "")


def store(name: str, value: str) -> None:
    f = cred_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    creds = load_all()
    creds[name] = value.strip()
    f.write_text(json.dumps(creds, indent=2))
    os.chmod(f, 0o600)


def mask(value: str) -> str:
    return value[:6] + "…" + value[-4:] if len(value) > 12 else "***"
