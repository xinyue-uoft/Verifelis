"""v1.0.5: /new clears the slate and refreshes the workspace index."""

from pathlib import Path

import pytest

from verifelis.tui import VerifelisApp, PromptInput


class FakeBackend:
    name = "fake"

    async def chat(self, messages, tools):
        raise AssertionError("not used")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "doc.txt").write_text("x\n")
    return tmp_path


async def test_new_clears_chat_and_refreshes_index(workdir):
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/help "
        await pilot.press("enter")
        assert "commands:" in "\n".join(str(s) for s in app.query_one("#chat").lines)
        # Index cached before the new file exists.
        assert "late.txt" not in app.file_index()
        (workdir / "late.txt").write_text("y\n")
        inp.value = "/new "
        await pilot.press("enter")
        text = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "fresh page" in text
        assert "commands:" not in text          # old content gone
        assert "late.txt" in app.file_index()   # index rescanned
        assert not app.busy


async def test_new_refused_while_busy(workdir):
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        app.busy = True
        inp = app.query_one(PromptInput)
        inp.value = "/new "
        await pilot.press("enter")
        text = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "still running" in text
        assert "fresh page" not in text
