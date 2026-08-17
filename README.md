# Verifelis

A read-only agent for verified scientific PDF and information retrieval, in a TUI.

Two cats work every question: a **white cat** reads and reports; a **black cat** audits the white cat's claims — including its chain of thought and its actual tool log — and files comments the white cat must address. A **calico cat** may replace the black cat for thorough reviews: it re-executes every operation itself.

## Principles

- **Strictly read-only.** Tools: `list_dir`, `read_file`, `grep`, `stat`, and whitelisted pipelines (fixed argv, only the target file substitutable). No writes, no arbitrary shell.
- **Secrets are invisible.** `.env`, SSH keys, credentials, keychains are blocked at the sandbox layer and filtered out of listings and search results.
- **Confinement with a human gate.** Access outside the working directory requires interactive approval, per directory.
- **Everything verified.** Claims must trace to a tool result. The reviewer flags unverified claims and operations claimed but never performed.

## Install

```sh
uv sync
uv run verifelis [workdir]
```

## Backends

| Backend  | Auth              | Notes                          |
|----------|-------------------|--------------------------------|
| ollama   | none (local)      | default; used for testing      |
| deepseek | API key           | `DEEPSEEK_API_KEY`             |
| openai   | OAuth (PKCE) or token paste | Responses API format |

Configure in `~/.config/verifelis/config.json` or via `--backend`.
