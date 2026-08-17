"""Read-only tool layer.

Every tool goes through the Sandbox. No tool writes, deletes, or runs
arbitrary commands. Pipelines are fixed argv templates where only <file>
is substitutable; the file must pass the sandbox first.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import Sandbox, SandboxViolation

MAX_READ_BYTES = 256 * 1024
MAX_GREP_MATCHES = 200
MAX_LIST_ENTRIES = 500
PIPELINE_TIMEOUT_S = 300


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    result: str = ""
    error: bool = False

    def digest(self, limit: int = 400) -> str:
        r = self.result if len(self.result) <= limit else self.result[:limit] + "…"
        status = "ERROR" if self.error else "ok"
        return f"{self.name}({self.args}) [{status}] -> {r}"

    def excerpt(self, budget: int = 4096) -> str:
        """Head+tail excerpt with an explicit truncation marker.

        The marker tells a reviewer that unseen content exists and must be
        read directly rather than assumed unverified.
        """
        status = "ERROR" if self.error else "ok"
        header = f"{self.name}({self.args}) [{status}] ->\n"
        if len(self.result) <= budget:
            return header + self.result
        head = self.result[: (budget * 2) // 3]
        tail = self.result[-(budget // 3) :]
        omitted = len(self.result) - len(head) - len(tail)
        return (
            header + head
            + f"\n…[truncated: {omitted} bytes omitted — read the source yourself to verify]…\n"
            + tail
        )


@dataclass
class Pipeline:
    """Whitelisted external command. argv items may contain '<file>'."""

    name: str
    argv: list[str]
    description: str

    def build(self, file: Path) -> list[str]:
        return [a.replace("<file>", str(file)) for a in self.argv]


def validate_pipeline(name: str, spec: dict) -> Pipeline:
    """Validate a user-configured pipeline entry.

    Contract: fixed argv of strings, exactly one element containing the
    <file> placeholder. Nothing else is substitutable.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"invalid pipeline name: {name!r}")
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError(f"{name}: argv must be a non-empty list of strings")
    if sum("<file>" in a for a in argv) != 1:
        raise ValueError(f"{name}: argv must contain the <file> placeholder exactly once")
    desc = spec.get("description", "")
    if not isinstance(desc, str):
        raise ValueError(f"{name}: description must be a string")
    return Pipeline(name=name, argv=list(argv), description=desc or f"user pipeline {name}")


def load_pipelines(config: dict) -> tuple[dict[str, Pipeline], list[str]]:
    """Defaults plus validated user entries from config["pipelines"].

    Returns (active, notes). Structurally invalid entries and entries
    whose binary is absent become notes instead of tools, so the model
    is never offered a pipeline that cannot run.
    """
    active = default_pipelines()
    notes: list[str] = []
    for name, spec in (config.get("pipelines") or {}).items():
        try:
            p = validate_pipeline(name, spec if isinstance(spec, dict) else {})
        except ValueError as e:
            notes.append(f"pipeline config rejected: {e}")
            continue
        if shutil.which(p.argv[0]) is None:
            notes.append(f"pipeline '{name}' inactive: binary '{p.argv[0]}' not found")
            continue
        active[name] = p
    return active, notes


def default_pipelines() -> dict[str, Pipeline]:
    out: dict[str, Pipeline] = {}
    if shutil.which("pdftotext"):
        out["pdftotext"] = Pipeline(
            name="pdftotext",
            argv=["pdftotext", "-layout", "<file>", "-"],
            description="Extract text from a PDF (stdout).",
        )
    if shutil.which("mineru"):
        out["mineru"] = Pipeline(
            name="mineru",
            argv=["mineru", "-p", "<file>", "-o", "/tmp/verifelis-mineru"],
            description="Run minerU OCR on a PDF.",
        )
    return out


@dataclass
class ToolBox:
    sandbox: Sandbox
    pipelines: dict[str, Pipeline] = field(default_factory=default_pipelines)
    log: list[ToolCall] = field(default_factory=list)

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI-style function specs for the LLM."""
        specs = [
            _spec(
                "list_dir",
                "List entries of a directory (read-only).",
                {"path": {"type": "string", "description": "Directory path."}},
                ["path"],
            ),
            _spec(
                "read_file",
                "Read a text file. Optional line range.",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-based, optional."},
                    "end_line": {"type": "integer", "description": "inclusive, optional."},
                },
                ["path"],
            ),
            _spec(
                "grep",
                "Search files under a directory for a regex. Returns file:line matches.",
                {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory or file to search."},
                    "glob": {"type": "string", "description": "Filename glob filter, optional."},
                },
                ["pattern", "path"],
            ),
            _spec(
                "stat",
                "File metadata: size, type, mtime.",
                {"path": {"type": "string"}},
                ["path"],
            ),
        ]
        if self.pipelines:
            names = ", ".join(self.pipelines)
            specs.append(
                _spec(
                    "run_pipeline",
                    f"Run a whitelisted read-only pipeline on a file. Available: {names}.",
                    {
                        "pipeline": {"type": "string", "enum": list(self.pipelines)},
                        "file": {"type": "string"},
                    },
                    ["pipeline", "file"],
                )
            )
        return specs

    def call(self, name: str, args: dict[str, Any]) -> ToolCall:
        tc = ToolCall(name=name, args=dict(args))
        try:
            handler = {
                "list_dir": self._list_dir,
                "read_file": self._read_file,
                "grep": self._grep,
                "stat": self._stat,
                "run_pipeline": self._run_pipeline,
            }.get(name)
            if handler is None:
                raise SandboxViolation(f"unknown tool: {name}")
            tc.result = handler(**args)
        except SandboxViolation as e:
            tc.error = True
            tc.result = f"DENIED: {e}"
        except (TypeError, ValueError, OSError, re.error) as e:
            tc.error = True
            tc.result = f"ERROR: {e}"
        self.log.append(tc)
        return tc

    def _list_dir(self, path: str) -> str:
        p = self.sandbox.resolve(path)
        if not p.is_dir():
            raise ValueError(f"not a directory: {p}")
        entries = self.sandbox.filter_visible(sorted(p.iterdir()))
        lines = []
        for e in entries[:MAX_LIST_ENTRIES]:
            kind = "dir " if e.is_dir() else "file"
            size = e.stat().st_size if e.is_file() else ""
            lines.append(f"{kind} {e.name} {size}")
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"… {len(entries) - MAX_LIST_ENTRIES} more entries")
        return "\n".join(lines) or "(empty)"

    def _read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        p = self.sandbox.resolve(path)
        if not p.is_file():
            raise ValueError(f"not a file: {p}")
        data = p.read_bytes()[: MAX_READ_BYTES + 1]
        truncated = len(data) > MAX_READ_BYTES
        text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start_line is not None or end_line is not None:
            s = max(1, start_line or 1)
            e = min(len(lines), end_line or len(lines))
            body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(s, e + 1))
        else:
            body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))
        if truncated:
            body += "\n… (file truncated at 256KB)"
        return body

    def _grep(self, pattern: str, path: str, glob: str | None = None) -> str:
        p = self.sandbox.resolve(path)
        rx = re.compile(pattern)
        files: list[Path]
        if p.is_file():
            files = [p]
        else:
            files = [f for f in p.rglob(glob or "*") if f.is_file()]
            files = self.sandbox.filter_visible(files)
        matches: list[str] = []
        for f in files:
            try:
                text = f.read_bytes()[:MAX_READ_BYTES].decode("utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(matches) >= MAX_GREP_MATCHES:
                        matches.append("… (match limit reached)")
                        return "\n".join(matches)
        return "\n".join(matches) or "(no matches)"

    def _stat(self, path: str) -> str:
        p = self.sandbox.resolve(path)
        st = p.stat()
        kind = "dir" if p.is_dir() else "file"
        return f"{p}: {kind}, {st.st_size} bytes, mtime={int(st.st_mtime)}"

    def _run_pipeline(self, pipeline: str, file: str) -> str:
        if pipeline not in self.pipelines:
            raise SandboxViolation(f"pipeline not whitelisted: {pipeline}")
        p = self.sandbox.resolve(file)
        if not p.is_file():
            raise ValueError(f"not a file: {p}")
        argv = self.pipelines[pipeline].build(p)
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=PIPELINE_TIMEOUT_S
        )
        out = proc.stdout[:MAX_READ_BYTES]
        if proc.returncode != 0:
            raise ValueError(f"pipeline exited {proc.returncode}: {proc.stderr[:2000]}")
        return out or "(no output)"


def _spec(name: str, description: str, props: dict, required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }
