"""TUI smoke tests with a scripted backend (Textual headless pilot)."""

from pathlib import Path

import pytest

from verifelis.backends import Message
from verifelis.tui import VerifelisApp, CatPanel


class FakeBackend:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    async def chat(self, messages, tools):
        return self.replies.pop(0)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "doc.txt").write_text("fact: water boils at 100 C\n")
    return tmp_path


async def test_app_boots_and_answers(workdir):
    backend = FakeBackend([
        Message(role="assistant", content="Water boils at 100 C (doc.txt:1)."),
        Message(role="assistant", content="[]"),
    ])
    app = VerifelisApp(backend, workdir, reviewer="black")
    async with app.run_test() as pilot:
        inp = app.query_one("Input")
        inp.value = "boiling point?"
        await pilot.press("enter")
        # Worker runs in a thread; poll until session completes.
        for _ in range(100):
            await pilot.pause(0.05)
            if not app.busy:
                break
        assert not app.busy
        chat_lines = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "100 C" in chat_lines
        assert "verified" in chat_lines


async def test_approval_modal_deny(workdir, tmp_path_factory):
    """Out-of-root tool call raises the modal; Deny yields DENIED result."""
    from verifelis.backends import ToolCallRequest
    from verifelis.tui import ApprovalModal

    outside = tmp_path_factory.mktemp("outside")
    (outside / "x.txt").write_text("outside data")
    backend = FakeBackend([
        Message(role="assistant",
                tool_calls=[ToolCallRequest("c1", "read_file", {"path": str(outside / "x.txt")})]),
        Message(role="assistant", content="Could not access the file."),
        Message(role="assistant", content="[]"),
    ])
    app = VerifelisApp(backend, workdir, reviewer="black")
    async with app.run_test() as pilot:
        app.query_one("Input").value = "read the outside file"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app.screen, ApprovalModal):
                break
        assert isinstance(app.screen, ApprovalModal)
        await pilot.click("#deny")
        for _ in range(100):
            await pilot.pause(0.05)
            if not app.busy:
                break
        assert not app.busy
        chat_lines = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "DENIED" in chat_lines


async def test_reviewer_toggle(workdir):
    app = VerifelisApp(FakeBackend([]), workdir, reviewer="black")
    async with app.run_test() as pilot:
        assert app.reviewer == "black"
        await pilot.press("ctrl+r")
        assert app.reviewer == "calico"
        assert app.query_one("#black-panel", CatPanel).agent == "calico"
