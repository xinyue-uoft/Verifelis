"""Read-only filesystem sandbox.

Confines all access to a root directory. Blocks secret files by pattern.
Expansion beyond the root requires an approval callback (human gate).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Basename patterns always blocked, case-insensitive.
SECRET_BASENAME_PATTERNS = [
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*_key",
    "*_key.*",
    "*apikey*",
    "*api_key*",
    "credentials",
    "credentials.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.keychain",
    "*.keychain-db",
    "secrets.*",
    "*.secret",
    "*.secrets",
    ".htpasswd",
    "shadow",
]

# Directory names blocked anywhere in the resolved path.
SECRET_DIR_NAMES = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config/gcloud",
}


class SandboxViolation(Exception):
    """Access denied by sandbox policy."""


class SecretBlocked(SandboxViolation):
    """Path matches a secret pattern. Never overridable."""


class OutsideRoot(SandboxViolation):
    """Path resolves outside allowed roots and was not approved."""


# Callback: (path) -> bool. True approves expansion for that path's directory.
ApprovalGate = Callable[[Path], bool]


def _deny_all(path: Path) -> bool:
    return False


@dataclass
class Sandbox:
    root: Path
    approval_gate: ApprovalGate = field(default=_deny_all)
    extra_roots: set[Path] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    def is_secret(self, resolved: Path) -> bool:
        name = resolved.name.lower()
        for pat in SECRET_BASENAME_PATTERNS:
            if fnmatch.fnmatch(name, pat):
                return True
        parts = [p.lower() for p in resolved.parts]
        for d in SECRET_DIR_NAMES:
            if "/" in d:
                sub = d.lower().split("/")
                for i in range(len(parts) - len(sub) + 1):
                    if parts[i : i + len(sub)] == sub:
                        return True
            elif d.lower() in parts:
                return True
        return False

    def _in_roots(self, resolved: Path) -> bool:
        for root in (self.root, *self.extra_roots):
            if resolved == root or resolved.is_relative_to(root):
                return True
        return False

    def resolve(self, raw: str | Path) -> Path:
        """Validate a path for read access. Returns the resolved path.

        Order matters: secret check runs on the resolved path first and is
        never overridable; the approval gate only handles confinement.
        """
        p = Path(raw)
        if not p.is_absolute():
            p = self.root / p
        # strict=False: nonexistent paths still resolve symlinked parents
        resolved = p.resolve()
        if self.is_secret(resolved):
            raise SecretBlocked(f"blocked secret path: {resolved}")
        if self._in_roots(resolved):
            return resolved
        # Human gate for expansion outside roots.
        if self.approval_gate(resolved):
            new_root = resolved if resolved.is_dir() else resolved.parent
            self.extra_roots.add(new_root)
            return resolved
        raise OutsideRoot(f"path outside sandbox root, expansion denied: {resolved}")

    def filter_visible(self, paths: list[Path]) -> list[Path]:
        """Drop secret entries from listings/search results."""
        return [p for p in paths if not self.is_secret(p.resolve())]
