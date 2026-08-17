# Verifelis

A read-only agent for verified scientific PDF and information retrieval, in a TUI.

Two cats work every question: a **white cat** 🐈 reads and reports; a **black cat** 🐈‍⬛ audits the white cat's claims — including its chain of thought and its actual tool log — and files comments the white cat must address. A **calico cat** 🐱 may replace the black cat (`ctrl+r` in the TUI, or `--reviewer calico`): it is more thorough and re-executes every one of the white cat's operations itself before judging.

## Principles

- **Strictly read-only.** Tools: `list_dir`, `read_file`, `grep`, `stat`, and whitelisted pipelines only (fixed argv templates where only the target file is substitutable — e.g. `pdftotext`, `mineru` when installed). No writes, no arbitrary shell.
- **Secrets are invisible.** `.env*`, SSH keys, `*.pem`, credentials, keychains, `.ssh/`, `.aws/` etc. are blocked at the sandbox layer — including through symlinks — and filtered out of directory listings and grep results.
- **Confinement with a human gate.** All access is confined to the working directory. A path outside it triggers an approval modal; approval is per directory, denial is the default (and automatic in headless mode).
- **Everything verified.** Claims must trace to a tool result actually obtained. The reviewer sees the full transcript plus the ground-truth tool log (head+tail excerpts with explicit truncation markers) and holds the same read-only tools, so it spot-checks doubted claims against the sources itself before filing: unverified claims, operations claimed but never performed, citations that don't match sources, and inconsistencies. The white cat must then address every comment.

## Install & run

```sh
uv sync
uv run verifelis [workdir]                 # TUI
uv run verifelis . --once "question"       # headless one-shot (events on stderr)
uv run verifelis . --backend ollama --model qwen3.5:9b-q4_K_M --reviewer calico
```

Register as a global tool (puts `verifelis` on PATH; run it from inside any work directory, which becomes the read-only sandbox root):

```sh
uv tool install --editable /path/to/Verifelis
cd ~/papers/some-project && verifelis      # workdir defaults to .
```

## In-TUI commands

Type `/` to open the command menu (arrow keys navigate, tab/enter complete, esc closes):

- `/login [deepseek <key> | openai [token]]` — store API keys persistently (`~/.config/verifelis/credentials.json`, mode 0600); bare `/login` shows what's stored; bare `/login openai` runs the browser OAuth flow.
- `/model [name]` — bare `/model` lists available models pulled live from every reachable/authenticated backend (ollama tags, deepseek and openai `/models`). `/model <name>` switches model *and* backend by catalog lookup; names present on several backends are disambiguated as `<backend>-<name>` (e.g. `ollama-deepseek-chat`). `/model <backend> <model>` stays explicit. A failed switch keeps the current backend.
- `/reviewer [black|calico]` — switch the reviewer cat (a short introduction of the newcomer is shown).
- `/help`, `/exit` (see you next time meow).

Answers render as markdown (bold, code, lists) in the chat pane.

Type `@` to reference workspace documents: a sandbox-filtered file menu opens (secrets never listed), and selected `@path` tokens are expanded into a reference section the white cat is instructed to read.

## Backends

| Backend  | Format               | Auth                                   |
|----------|----------------------|----------------------------------------|
| ollama   | ollama /api/chat     | none (local); used for testing         |
| deepseek | chat completions     | API key: `DEEPSEEK_API_KEY`            |
| openai   | Responses API        | `OPENAI_API_KEY`, or OAuth: `verifelis login openai` |

The OpenAI OAuth flow is PKCE (S256) against `auth.openai.com` with endpoints and parameters taken from the open-source `openai/codex` CLI; `verifelis login openai --paste-token` stores a pasted key/token instead. API-key auth is the tested path; ChatGPT-plan OAuth tokens may not be accepted by `api.openai.com` for all account types.

Persistent config: `~/.config/verifelis/config.json`, e.g. `{"backend": "ollama", "model": "qwen3.5:9b-q4_K_M"}`. CLI flags override it.

## Tests

```sh
uv run pytest                 # unit + TUI smoke tests; live ollama tests
                              # auto-skip when ollama/qwen3.5:9b-q4_K_M is absent
```

The sandbox suite covers symlink escapes, path traversal, secret-pattern blocking (never overridable, even with approval), and grep/listing leak prevention. The verification loop is tested against a deterministic scripted backend, plus a live end-to-end run on ollama.
