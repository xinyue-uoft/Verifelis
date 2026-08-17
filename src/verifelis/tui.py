"""Verifelis TUI.

Left: conversation and answers. Right: live status of each cat plus the
review panel. Approval modal gates sandbox expansion. The orchestrator
runs in a thread worker with its own event loop; events cross into the
UI thread via call_from_thread.

Input extras: "/" opens a filtering command menu; "@" opens a workspace
file menu whose selections become document references in the question.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from rich.markdown import Markdown
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, RichLog, Static

from .backends import Backend, fetch_model_catalog, make_backend, resolve_model
from .orchestrator import Event, Orchestrator
from .sandbox import Sandbox
from .tools import ToolBox

AGENT_ICON = {"white": "🐈", "black": "🐈‍⬛", "calico": "🐱", "system": "🐾"}
AGENT_NAME = {"white": "WhiteCat", "black": "BlackCat", "calico": "CalicoCat"}
REVIEWER_INTRO = {
    "black": "🐈‍⬛ BlackCat — audits the transcript against the ground-truth tool log "
             "and spot-checks doubted claims with its own read tools.",
    "calico": "🐱 CalicoCat — the thorough one: re-executes every operation WhiteCat "
              "performed, compares results, then audits with its own eyes.",
}

# (name, usage, description)
COMMANDS = [
    ("/login", "/login [deepseek <key> | openai [token]]", "store API keys / OAuth login"),
    ("/model", "/model [backend] [model]", "show or switch backend/model"),
    ("/reviewer", "/reviewer [black|calico]", "switch reviewer cat"),
    ("/help", "/help", "list commands"),
    ("/exit", "/exit", "leave gracefully"),
]

MAX_INDEX_FILES = 1000
MENU_KEYS = ("up", "down", "tab", "enter", "escape")


class ApprovalModal(ModalScreen[bool]):
    """Human gate: allow or deny sandbox expansion to a path."""

    CSS = """
    ApprovalModal { align: center middle; }
    #dialog { width: 70; height: auto; border: thick $warning; padding: 1 2;
              background: $surface; }
    #buttons { height: auto; align: center middle; }
    Button { margin: 1 2; }
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("🐾 The cat paws at a door outside its room…")
            yield Static(f"Requested path:\n  {self.path}\n\nAllow read access to this directory?")
            with Horizontal(id="buttons"):
                yield Button("Allow", variant="warning", id="allow")
                yield Button("Deny", variant="primary", id="deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")


class CatPanel(Static):
    """Live status of one agent."""

    def __init__(self, agent: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.agent = agent
        self.current = "idle"

    def set_status(self, text: str) -> None:
        self.current = text
        icon = AGENT_ICON.get(self.agent, "🐾")
        name = AGENT_NAME.get(self.agent, self.agent)
        self.update(f"{icon} [b]{name}[/b]\n{text}")


class PromptInput(Input):
    """Routes menu-navigation keys to the app while the menu is open."""

    async def on_key(self, event: events.Key) -> None:
        app = self.app
        if isinstance(app, VerifelisApp) and app.menu_mode and event.key in MENU_KEYS:
            event.prevent_default()
            event.stop()
            app.menu_key(event.key)


class VerifelisApp(App):
    TITLE = "Verifelis"
    SUB_TITLE = "two cats, one verified answer"

    CSS = """
    #main { height: 1fr; }
    #left { width: 2fr; }
    #right { width: 1fr; border-left: solid $primary; }
    #chat { height: 1fr; border: round $primary; }
    #review { height: 1fr; border: round $secondary; }
    CatPanel { height: auto; min-height: 4; border: round $accent; padding: 0 1;
               margin: 0 0 1 0; }
    #white-panel { border: round white; }
    #black-panel { border: round #666666; }
    #menu { height: auto; max-height: 9; border: round $warning; display: none; }
    Input { dock: bottom; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+r", "toggle_reviewer", "BlackCat/CalicoCat"),
    ]

    def __init__(
        self,
        backend: Backend,
        workdir: Path,
        reviewer: str = "black",
        config: dict | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.workdir = workdir
        self.reviewer = reviewer
        self.config = dict(config or {"backend": backend.name})
        self.busy = False
        self.menu_mode = ""  # "" | "command" | "file"
        self.menu_items: list[str] = []
        self._file_index: list[str] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield RichLog(id="chat", wrap=True, markup=True)
                yield OptionList(id="menu")
            with Vertical(id="right"):
                yield CatPanel("white", id="white-panel")
                yield CatPanel("black", id="black-panel")
                yield RichLog(id="review", wrap=True, markup=True)
        yield PromptInput(placeholder="Ask about the documents… ( / commands · @ files )")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#white-panel", CatPanel).set_status("idle — grooming whiskers")
        self._set_reviewer_panel()
        chat = self.query_one("#chat", RichLog)
        chat.write(f"[b]🐾 Verifelis[/b] — workdir: {self.workdir}")
        chat.write(f"backend: {self.backend.name} · reviewer: {AGENT_NAME[self.reviewer]}")
        chat.write("Read-only. Secrets blocked. Everything verified. Type / for commands.\n")

    def _chat(self) -> RichLog:
        return self.query_one("#chat", RichLog)

    def _chat_write(self, text: str) -> None:
        """UI-thread target for call_from_thread; workers must not query the DOM."""
        self._chat().write(text)

    def _set_reviewer_panel(self) -> None:
        panel = self.query_one("#black-panel", CatPanel)
        panel.agent = self.reviewer
        panel.set_status("idle — watching from the shelf")

    def action_toggle_reviewer(self) -> None:
        if self.busy:
            return
        self._set_reviewer(("calico" if self.reviewer == "black" else "black"))

    def _set_reviewer(self, reviewer: str) -> None:
        self.reviewer = reviewer
        self._set_reviewer_panel()
        chat = self._chat()
        chat.write(f"[i]reviewer switched to {AGENT_NAME[self.reviewer]}[/i]")
        chat.write(f"[dim]{REVIEWER_INTRO[self.reviewer]}[/dim]")

    # -- workspace file index (sandbox-filtered, lazy) --

    def file_index(self) -> list[str]:
        if self._file_index is None:
            sandbox = Sandbox(self.workdir)
            out: list[str] = []
            for p in sorted(self.workdir.rglob("*")):
                if len(out) >= MAX_INDEX_FILES:
                    break
                if any(part.startswith(".") for part in p.relative_to(self.workdir).parts[:-1]):
                    continue
                if p.is_file() and not sandbox.is_secret(p.resolve()):
                    out.append(str(p.relative_to(self.workdir)))
            self._file_index = out
        return self._file_index

    # -- suggestion menu --

    def on_input_changed(self, event: Input.Changed) -> None:
        value = event.value
        menu = self.query_one("#menu", OptionList)
        if value.startswith("/") and " " not in value:
            items = [f"{usage}  —  {desc}" for name, usage, desc in COMMANDS
                     if name.startswith(value)]
            self.menu_items = [name for name, _, _ in COMMANDS if name.startswith(value)]
            self._show_menu("command" if items else "", menu, items)
        else:
            token = value.rsplit(" ", 1)[-1]
            if token.startswith("@"):
                needle = token[1:].lower()
                files = [f for f in self.file_index() if needle in f.lower()][:8]
                self.menu_items = files
                self._show_menu("file" if files else "", menu, files)
            else:
                self._show_menu("", menu, [])

    def _show_menu(self, mode: str, menu: OptionList, items: list[str]) -> None:
        self.menu_mode = mode
        menu.clear_options()
        if mode:
            menu.add_options(items)
            menu.highlighted = 0
            menu.styles.display = "block"
        else:
            menu.styles.display = "none"

    def menu_key(self, key: str) -> None:
        menu = self.query_one("#menu", OptionList)
        if key == "escape":
            self._show_menu("", menu, [])
            return
        if key == "up":
            menu.highlighted = max(0, (menu.highlighted or 0) - 1)
            return
        if key == "down":
            menu.highlighted = min(menu.option_count - 1, (menu.highlighted or 0) + 1)
            return
        # tab / enter: accept selection
        idx = menu.highlighted or 0
        if idx >= len(self.menu_items):
            return
        chosen = self.menu_items[idx]
        inp = self.query_one(PromptInput)
        if self.menu_mode == "command":
            inp.value = chosen + " "
        else:
            head = inp.value.rsplit(" ", 1)[0] if " " in inp.value else ""
            inp.value = (head + " " if head else "") + "@" + chosen + " "
        inp.cursor_position = len(inp.value)
        self._show_menu("", menu, [])

    # -- input submission --

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if text.startswith("/"):
            self.dispatch_command(text)
            return
        if self.busy:
            self._chat().write("[i]a session is already running — patience, the cats are busy[/i]")
            return
        question, refs = self._expand_refs(text)
        self.busy = True
        self._chat().write(f"\n[b cyan]you:[/b cyan] {text}")
        if refs:
            self._chat().write(f"[dim]referenced: {', '.join(refs)}[/dim]")
        self.run_session(question)

    def _expand_refs(self, text: str) -> tuple[str, list[str]]:
        """Resolve @tokens against the index; append a reference section."""
        index = set(self.file_index())
        refs = [t[1:] for t in text.split() if t.startswith("@") and t[1:] in index]
        if refs:
            listing = "\n".join(f"- {r}" for r in refs)
            text += (
                "\n\n[Referenced documents] (paths relative to the working directory; "
                f"read them with your tools before answering)\n{listing}"
            )
        return text, refs

    # -- slash commands --

    def dispatch_command(self, text: str) -> None:
        parts = text.split()
        cmd, args = parts[0], parts[1:]
        handler = {
            "/help": self._cmd_help,
            "/exit": self._cmd_exit,
            "/reviewer": self._cmd_reviewer,
            "/model": self._cmd_model,
            "/login": self._cmd_login,
        }.get(cmd)
        if handler is None:
            self._chat().write(f"[red]unknown command: {cmd}[/red] — try /help")
            return
        handler(args)

    def _cmd_help(self, args: list[str]) -> None:
        chat = self._chat()
        chat.write("[b]commands:[/b]")
        for _, usage, desc in COMMANDS:
            chat.write(f"  {usage}  —  {desc}")

    def _cmd_exit(self, args: list[str]) -> None:
        if self.busy:
            self._chat().write("[i]finishing the current call, then leaving…[/i]")
        self.exit(message="see you next time meow 🐾")

    def _cmd_reviewer(self, args: list[str]) -> None:
        if self.busy:
            self._chat().write("[i]cannot switch reviewer mid-session[/i]")
            return
        if args and args[0] in ("black", "calico"):
            self._set_reviewer(args[0])
        elif not args:
            self.action_toggle_reviewer()
        else:
            self._chat().write("[red]usage: /reviewer [black|calico][/red]")

    def _cmd_model(self, args: list[str]) -> None:
        chat = self._chat()
        if self.busy:
            chat.write("[i]cannot switch model mid-session[/i]")
            return
        if len(args) == 2 and args[0] in ("ollama", "deepseek", "openai"):
            self._switch_backend(args[0], args[1])
            return
        if len(args) > 1:
            chat.write("[red]usage: /model [<model> | <backend> <model>][/red]")
            return
        # No args (list) or bare model name: needs the live catalog.
        chat.write("[dim]fetching available models…[/dim]")
        self._model_worker(args)

    @work(thread=True)
    def _model_worker(self, args: list[str]) -> None:
        catalog = fetch_model_catalog(self.config)
        self.call_from_thread(self._model_apply, catalog, args)

    def _model_apply(self, catalog: dict[str, list[str]], args: list[str]) -> None:
        chat = self._chat()
        if not args:
            chat.write(
                f"backend: [b]{self.config.get('backend')}[/b] · "
                f"model: [b]{self.config.get('model', '(default)')}[/b]"
            )
            if not catalog:
                chat.write("[dim]no backend reachable/authenticated for listing[/dim]")
                return
            counts: dict[str, int] = {}
            for models in catalog.values():
                for m in models:
                    counts[m] = counts.get(m, 0) + 1
            for b, models in catalog.items():
                shown = [f"{b}-{m}" if counts[m] > 1 else m for m in models]
                chat.write(f"[b]{b}[/b]: " + (", ".join(shown) or "(none)"))
            chat.write("[dim]switch with /model <name>[/dim]")
            return
        try:
            backend_name, model = resolve_model(
                catalog, args[0], self.config.get("backend", "ollama")
            )
        except ValueError as e:
            chat.write(f"[red]{e}[/red]")
            return
        self._switch_backend(backend_name, model)

    def _switch_backend(self, backend_name: str, model: str) -> None:
        chat = self._chat()
        new = dict(self.config)
        new["backend"] = backend_name
        new["model"] = model
        try:
            self.backend = make_backend(new)
        except (ValueError, KeyError) as e:
            chat.write(f"[red]switch failed: {e}[/red] — keeping {self.config.get('backend')}")
            return
        self.config = new
        chat.write(f"[green]switched to {backend_name} · {model}[/green]")

    def _cmd_login(self, args: list[str]) -> None:
        from . import credentials
        from .backends import oauth

        chat = self._chat()
        if not args:
            ds = credentials.get("deepseek")
            oa = oauth.load_tokens()
            chat.write(f"deepseek: {credentials.mask(ds) if ds else '[dim]not stored[/dim]'}")
            chat.write(f"openai:   {'token stored' if oa else '[dim]not stored[/dim]'}")
            return
        if args[0] == "deepseek":
            if len(args) != 2:
                chat.write("[red]usage: /login deepseek <api-key>[/red]")
                return
            credentials.store("deepseek", args[1])
            chat.write(f"[green]deepseek key stored ({credentials.mask(args[1])})[/green]")
            return
        if args[0] == "openai":
            if len(args) == 2:
                oauth.login_paste_token(args[1])
                chat.write("[green]openai token stored[/green]")
            else:
                chat.write("opening browser for OAuth… (or use /login openai <token>)")
                self._oauth_login()
            return
        chat.write("[red]usage: /login [deepseek <key> | openai [token]][/red]")

    @work(thread=True)
    def _oauth_login(self) -> None:
        from .backends import oauth

        try:
            oauth.login_interactive()
            self.call_from_thread(self._chat_write, "[green]openai login complete[/green]")
        except Exception as e:
            self.call_from_thread(
                self._chat_write,
                f"[red]OAuth failed: {e}[/red] — try /login openai <token>",
            )

    # -- approval gate (called from worker thread) --

    def _gate(self, path: Path) -> bool:
        done = threading.Event()
        result: dict[str, bool] = {}

        def dismissed(value: bool | None) -> None:
            result["v"] = bool(value)
            done.set()

        self.call_from_thread(self.push_screen, ApprovalModal(path), dismissed)
        done.wait(timeout=600)
        return result.get("v", False)

    # -- orchestrator worker --

    @work(thread=True, exclusive=True)
    def run_session(self, question: str) -> None:
        sandbox = Sandbox(self.workdir, approval_gate=self._gate)
        toolbox = ToolBox(sandbox=sandbox)
        orch = Orchestrator(
            self.backend,
            toolbox,
            reviewer=self.reviewer,
            on_event=lambda e: self.call_from_thread(self._on_orch_event, e),
        )
        try:
            asyncio.run(orch.run(question))
        except Exception as e:  # surface backend/network errors in UI
            self.call_from_thread(
                self._on_orch_event, Event("error", "system", f"{type(e).__name__}: {e}")
            )
        finally:
            self.call_from_thread(self._session_done)

    def _session_done(self) -> None:
        self.busy = False
        self.query_one("#white-panel", CatPanel).set_status("idle — grooming whiskers")
        self.query_one("#black-panel", CatPanel).set_status("idle — watching from the shelf")

    def _on_orch_event(self, e: Event) -> None:
        chat = self._chat()
        review = self.query_one("#review", RichLog)
        icon = AGENT_ICON.get(e.agent, "🐾")
        if e.kind == "status":
            panel_id = "#white-panel" if e.agent == "white" else "#black-panel"
            self.query_one(panel_id, CatPanel).set_status(e.text)
        elif e.kind == "phase":
            chat.write(f"[b magenta]— {e.text} —[/b magenta]")
        elif e.kind == "tool":
            target = review if e.agent in ("black", "calico") else chat
            target.write(f"[dim]{icon} {e.text}[/dim]")
        elif e.kind == "reasoning":
            preview = e.text if len(e.text) < 300 else e.text[:300] + "…"
            (review if e.agent in ("black", "calico") else chat).write(
                f"[dim italic]{icon} thinks: {preview}[/dim italic]"
            )
        elif e.kind == "comment":
            review.write(f"[yellow]{icon} {e.text}[/yellow]")
        elif e.kind == "message":
            chat.write(f"{icon} [b]{AGENT_NAME.get(e.agent, e.agent)}:[/b]")
            chat.write(Markdown(e.text))
        elif e.kind == "done":
            chat.write(f"[b green]✓ {e.text}[/b green]")
        elif e.kind == "error":
            chat.write(f"[b red]✗ {e.text}[/b red]")
