"""CLI entry point.

verifelis [workdir] [--backend ollama|deepseek|openai] [--model M]
          [--reviewer black|calico] [--once QUESTION]
verifelis login openai [--paste-token]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "verifelis" / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            print(f"warning: invalid config at {CONFIG_FILE}", file=sys.stderr)
    return {}


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["login"]:
        return _login(argv[1:])

    parser = argparse.ArgumentParser(prog="verifelis", description="Two cats, one verified answer.")
    parser.add_argument("workdir", nargs="?", default=".", help="Read-only working directory.")
    parser.add_argument("--backend", choices=["ollama", "deepseek", "openai"])
    parser.add_argument("--model")
    parser.add_argument("--reviewer", choices=["black", "calico"], default="black")
    parser.add_argument("--once", metavar="QUESTION", help="Headless: answer one question, print, exit.")
    args = parser.parse_args(argv)

    config = load_config()
    if args.backend:
        config["backend"] = args.backend
    if args.model:
        config["model"] = args.model

    from .backends import make_backend

    backend = make_backend(config)
    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        parser.error(f"not a directory: {workdir}")

    if args.once:
        return _headless(backend, workdir, args.reviewer, args.once)

    from .tui import VerifelisApp

    VerifelisApp(backend, workdir, reviewer=args.reviewer, config=config).run()


def _headless(backend, workdir: Path, reviewer: str, question: str) -> None:
    """One question, events to stderr, final answer to stdout."""
    from .orchestrator import Orchestrator
    from .sandbox import Sandbox
    from .tools import ToolBox

    def on_event(e) -> None:
        print(f"[{e.agent}:{e.kind}] {e.text[:200]}", file=sys.stderr)

    toolbox = ToolBox(sandbox=Sandbox(workdir))  # headless: expansion always denied
    orch = Orchestrator(backend, toolbox, reviewer=reviewer, on_event=on_event)
    result = asyncio.run(orch.run(question))
    print(result.revised_answer or result.answer)
    print(f"\n-- {len(result.comments)} review comment(s), "
          f"{len(result.tool_log)} tool call(s)", file=sys.stderr)


def _login(argv: list[str]) -> None:
    if argv[:1] == ["deepseek"]:
        from . import credentials

        key = input("Paste DeepSeek API key: ")
        credentials.store("deepseek", key)
        print(f"stored ({credentials.mask(key.strip())}).")
        return
    if argv[:1] != ["openai"]:
        print("usage: verifelis login {openai [--paste-token] | deepseek}", file=sys.stderr)
        sys.exit(2)
    from .backends import oauth

    if "--paste-token" in argv:
        token = input("Paste OpenAI API key or access token: ")
        oauth.login_paste_token(token)
        print("token stored.")
        return
    try:
        oauth.login_interactive()
        print("login complete.")
    except Exception as e:
        print(f"OAuth flow failed ({e}); falling back to token paste.", file=sys.stderr)
        token = input("Paste OpenAI API key or access token: ")
        oauth.login_paste_token(token)
        print("token stored.")


if __name__ == "__main__":
    main()
