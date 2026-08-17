"""Verifelis TUI.

Left: conversation and answers. Right: live status of each cat plus the
review panel. Approval modal gates sandbox expansion. The orchestrator
runs in a thread worker with its own event loop; events cross into the
UI thread via call_from_thread.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from .backends import Backend
from .orchestrator import Event, Orchestrator
from .sandbox import Sandbox
from .tools import ToolBox

AGENT_ICON = {"white": "🐈", "black": "🐈‍⬛", "calico": "🐱", "system": "🐾"}
AGENT_NAME = {"white": "WhiteCat", "black": "BlackCat", "calico": "CalicoCat"}


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
    Input { dock: bottom; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+r", "toggle_reviewer", "BlackCat/CalicoCat"),
    ]

    def __init__(
        self, backend: Backend, workdir: Path, reviewer: str = "black"
    ) -> None:
        super().__init__()
        self.backend = backend
        self.workdir = workdir
        self.reviewer = reviewer
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield RichLog(id="chat", wrap=True, markup=True)
            with Vertical(id="right"):
                yield CatPanel("white", id="white-panel")
                yield CatPanel("black", id="black-panel")
                yield RichLog(id="review", wrap=True, markup=True)
        yield Input(placeholder="Ask about the documents… (Enter to send)")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#white-panel", CatPanel).set_status("idle — grooming whiskers")
        self._set_reviewer_panel()
        chat = self.query_one("#chat", RichLog)
        chat.write(f"[b]🐾 Verifelis[/b] — workdir: {self.workdir}")
        chat.write(f"backend: {self.backend.name} · reviewer: {AGENT_NAME[self.reviewer]}")
        chat.write("Read-only. Secrets blocked. Everything verified.\n")

    def _set_reviewer_panel(self) -> None:
        panel = self.query_one("#black-panel", CatPanel)
        panel.agent = self.reviewer
        panel.set_status("idle — watching from the shelf")

    def action_toggle_reviewer(self) -> None:
        if self.busy:
            return
        self.reviewer = "calico" if self.reviewer == "black" else "black"
        self._set_reviewer_panel()
        self.query_one("#chat", RichLog).write(
            f"[i]reviewer switched to {AGENT_NAME[self.reviewer]}[/i]"
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question or self.busy:
            return
        event.input.value = ""
        self.busy = True
        self.query_one("#chat", RichLog).write(f"\n[b cyan]you:[/b cyan] {question}")
        self.run_session(question)

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
        chat = self.query_one("#chat", RichLog)
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
            chat.write(f"{icon} [b]{AGENT_NAME.get(e.agent, e.agent)}:[/b] {e.text}")
        elif e.kind == "done":
            chat.write(f"[b green]✓ {e.text}[/b green]")
        elif e.kind == "error":
            chat.write(f"[b red]✗ {e.text}[/b red]")
