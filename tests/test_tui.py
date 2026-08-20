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


async def test_markdown_rendered_in_chat(workdir):
    backend = FakeBackend([
        Message(role="assistant", content="The **key value** is `42`."),
        Message(role="assistant", content="[]"),
    ])
    app = VerifelisApp(backend, workdir, reviewer="black")
    async with app.run_test() as pilot:
        app.query_one("Input").value = "q"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if not app.busy:
                break
        lines = app.query_one("#chat").lines
        text = "\n".join(str(s) for s in lines)
        # Markdown rendered: bold markers consumed, styled span present.
        assert "key value" in text
        assert "**key value**" not in text
        # Lines are Strips of styled Segments; "key value" carries bold.
        assert any(
            seg.text.strip() == "key value" and "bold" in str(seg.style)
            for line in lines
            for seg in line._segments
        )


async def test_reviewer_toggle(workdir):
    app = VerifelisApp(FakeBackend([]), workdir, reviewer="black")
    async with app.run_test() as pilot:
        assert app.reviewer == "black"
        await pilot.press("ctrl+r")
        assert app.reviewer == "calico"
        assert app.query_one("#black-panel", CatPanel).agent == "calico"


# -- text selection --

async def test_drag_select_copies_to_clipboard(workdir, monkeypatch):
    import subprocess
    from verifelis.tui import SelectableLog
    copied = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: copied.append(k.get("input")))
    app = VerifelisApp(FakeBackend([]), workdir, reviewer="black")
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat", SelectableLog)
        log.clear()
        log.write("alpha beta gamma")
        log.write("delta epsilon")
        await pilot.pause()
        gx, gy = log.gutter.top_left  # offsets are region-relative; skip the border
        await pilot.mouse_down(log, offset=(gx, gy))
        await pilot.hover(log, offset=(gx + 4, gy + 1))
        await pilot.mouse_up(log, offset=(gx + 4, gy + 1))
        await pilot.pause()
        assert app.screen.get_selected_text() == "alpha beta gamma\ndelta"
        assert app.clipboard == "alpha beta gamma\ndelta"
        if copied:  # darwin: pbcopy fallback
            assert copied == [b"alpha beta gamma\ndelta"]


async def test_selection_highlight_aligns_with_wide_chars(workdir):
    from rich.cells import cell_len
    from verifelis.tui import SelectableLog
    app = VerifelisApp(FakeBackend([]), workdir, reviewer="black")
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat", SelectableLog)
        log.clear()
        log.write("🐈 cat 猫 end")
        await pilot.pause()
        gx, gy = log.gutter.top_left
        await pilot.mouse_down(log, offset=(gx, gy))
        target = cell_len("🐈 cat 猫 en") - 1  # cell column of last selected char
        await pilot.hover(log, offset=(gx + target, gy))
        await pilot.mouse_up(log, offset=(gx + target, gy))
        await pilot.pause()
        assert app.screen.get_selected_text() == "🐈 cat 猫 en"
        sel_style = app.screen.get_component_rich_style("screen--selection")
        strip = log.render_line(0)
        highlighted = sum(seg.cell_length for seg in strip if seg.style and seg.style.bgcolor == sel_style.bgcolor)
        assert highlighted == cell_len("🐈 cat 猫 en")


async def test_drag_extends_monotonically(workdir):
    """Regression: offsets stamped before crop misplaced segments right of the highlight."""
    from verifelis.tui import SelectableLog
    app = VerifelisApp(FakeBackend([]), workdir, reviewer="black")
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat", SelectableLog)
        log.clear()
        log.write("backend: fake · reviewer: BlackCat")
        await pilot.pause()
        gx, gy = log.gutter.top_left
        await pilot.mouse_down(log, offset=(gx + 10, gy))
        for x in range(10, 30, 4):
            await pilot.hover(log, offset=(gx + x, gy))
            await pilot.pause()
            sel = app.screen.selections[log]
            assert (sel.start.x, sel.end.x) == (10, x + 1)
