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
PIPELINE_TIMEOUT_S = 600
MAX_OUTLINE_LINES = 50

# Derived artifacts (pipeline outputs) live here; each output dir is
# auto-added to the sandbox after a successful run.
DERIVED_BASE = Path("/tmp/verifelis-derived")


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
    """Whitelisted external command with fixed argv.

    Placeholders: '<file>' (required, the sandboxed input) and optionally
    '<outdir>' (a derived-output directory). With '<outdir>', the tool
    result is an index of the produced files, not stdout, and the output
    dir becomes readable through the sandbox.
    """

    name: str
    argv: list[str]
    description: str

    @property
    def has_outdir(self) -> bool:
        return any("<outdir>" in a for a in self.argv)

    def outdir_for(self, file: Path) -> Path:
        return DERIVED_BASE / self.name / file.stem

    def build(self, file: Path, outdir: Path | None = None) -> list[str]:
        out = []
        for a in self.argv:
            a = a.replace("<file>", str(file))
            if outdir is not None:
                a = a.replace("<outdir>", str(outdir))
            out.append(a)
        return out


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
    if sum("<outdir>" in a for a in argv) > 1:
        raise ValueError(f"{name}: argv may contain the <outdir> placeholder at most once")
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
            argv=["mineru", "-p", "<file>", "-o", "<outdir>"],
            description="Run minerU OCR on a PDF; returns an index of the "
                        "extracted markdown to read incrementally.",
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
        # Streamed so line ranges deep in large files stay reachable;
        # only the returned range counts against the byte budget.
        s = max(1, start_line or 1)
        out: list[str] = []
        budget = MAX_READ_BYTES
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh, 1):
                if i < s:
                    continue
                if end_line is not None and i > end_line:
                    break
                ln = ln.rstrip("\n")
                budget -= len(ln) + 1
                if budget < 0:
                    out.append(f"… (output truncated at 256KB; continue from line {i})")
                    break
                out.append(f"{i}: {ln}")
        return "\n".join(out) or "(empty range)"

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
        pl = self.pipelines[pipeline]
        p = self.sandbox.resolve(file)
        if not p.is_file():
            raise ValueError(f"not a file: {p}")
        if not pl.has_outdir:
            proc = subprocess.run(
                pl.build(p), capture_output=True, text=True, timeout=PIPELINE_TIMEOUT_S
            )
            if proc.returncode != 0:
                raise ValueError(f"pipeline exited {proc.returncode}: {proc.stderr[:2000]}")
            return proc.stdout[:MAX_READ_BYTES] or "(no output)"
        # Derived-output pipeline: run (or reuse cache), whitelist the
        # output dir, return an index instead of the content.
        outdir = pl.outdir_for(p)
        cached = self._derived_fresh(outdir, p)
        if not cached:
            outdir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                pl.build(p, outdir), capture_output=True, text=True,
                timeout=PIPELINE_TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise ValueError(f"pipeline exited {proc.returncode}: {proc.stderr[:2000]}")
        self.sandbox.extra_roots.add(outdir.resolve())
        return self._derived_index(pl.name, outdir, cached)

    @staticmethod
    def _derived_fresh(outdir: Path, source: Path) -> bool:
        """True if a previous output exists and is newer than the source."""
        if not outdir.is_dir():
            return False
        files = [f for f in outdir.rglob("*") if f.is_file()]
        if not files:
            return False
        return max(f.stat().st_mtime for f in files) >= source.stat().st_mtime

    @staticmethod
    def _derived_index(name: str, outdir: Path, cached: bool) -> str:
        files = sorted(f for f in outdir.rglob("*") if f.is_file())
        if not files:
            raise ValueError(f"pipeline '{name}' produced no files in {outdir}")
        lines = [
            f"pipeline '{name}' {'reused cached output' if cached else 'completed'}.",
            f"output dir (now readable): {outdir}",
            "files:",
        ]
        lines += [f"  {f.relative_to(outdir)} ({f.stat().st_size} bytes)" for f in files[:50]]
        if len(files) > 50:
            lines.append(f"  … {len(files) - 50} more files")
        mds = [f for f in files if f.suffix == ".md"]
        if mds:
            main = max(mds, key=lambda f: f.stat().st_size)
            with main.open("r", encoding="utf-8", errors="replace") as fh:
                n_lines = 0
                outline: list[str] = []
                for ln in fh:
                    n_lines += 1
                    if ln.startswith("#") and len(outline) < MAX_OUTLINE_LINES:
                        outline.append(f"  L{n_lines}: {ln.strip()[:120]}")
            lines.append(f"main document: {main} ({n_lines} lines)")
            if outline:
                lines.append("outline (heading lines):")
                lines += outline
        lines.append(
            "Do NOT read everything at once: use grep(pattern, path) and "
            "read_file(path, start_line, end_line) on the paths above."
        )
        return "\n".join(lines)


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
