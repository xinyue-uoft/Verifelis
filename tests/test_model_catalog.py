"""v1.0.2: model catalog resolution and catalog-driven /model switching."""

from pathlib import Path

import pytest

from verifelis.backends import resolve_model
from verifelis import tui as tui_mod
from verifelis.tui import VerifelisApp, PromptInput

CATALOG = {
    "ollama": ["qwen3:4b", "deepseek-chat"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
}


def test_resolve_unique_name_switches_backend():
    assert resolve_model(CATALOG, "deepseek-reasoner", "ollama") == (
        "deepseek", "deepseek-reasoner"
    )


def test_resolve_duplicate_raises_with_prefixed_options():
    with pytest.raises(ValueError) as e:
        resolve_model(CATALOG, "deepseek-chat", "ollama")
    assert "ollama-deepseek-chat" in str(e.value)
    assert "deepseek-deepseek-chat" in str(e.value)


def test_resolve_prefixed_form():
    assert resolve_model(CATALOG, "ollama-deepseek-chat", "deepseek") == (
        "ollama", "deepseek-chat"
    )
    assert resolve_model(CATALOG, "deepseek-deepseek-chat", "ollama") == (
        "deepseek", "deepseek-chat"
    )


def test_resolve_unknown_stays_on_current_backend():
    assert resolve_model(CATALOG, "mystery:7b", "ollama") == ("ollama", "mystery:7b")
    assert resolve_model({}, "anything", "openai") == ("openai", "anything")


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.replies = []

    async def chat(self, messages, tools):
        return self.replies.pop(0)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "doc.txt").write_text("x\n")
    return tmp_path


async def test_bare_model_name_switches_backend_via_catalog(workdir, monkeypatch):
    monkeypatch.setattr(tui_mod, "fetch_model_catalog", lambda config: CATALOG)
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/model qwen3:4b"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if app.config.get("model") == "qwen3:4b":
                break
        assert app.config["backend"] == "ollama"
        assert app.backend.name == "ollama"


async def test_duplicate_name_reports_prefixed_options(workdir, monkeypatch):
    monkeypatch.setattr(tui_mod, "fetch_model_catalog", lambda config: CATALOG)
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        old = app.backend
        inp = app.query_one(PromptInput)
        inp.value = "/model deepseek-chat"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            text = "\n".join(str(s) for s in app.query_one("#chat").lines)
            if "several backends" in text:
                break
        assert "ollama-deepseek-chat" in text
        assert app.backend is old


async def test_model_listing_marks_duplicates(workdir, monkeypatch):
    monkeypatch.setattr(tui_mod, "fetch_model_catalog", lambda config: CATALOG)
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/model "  # trailing space: menu closed, enter submits
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            text = "\n".join(str(s) for s in app.query_one("#chat").lines)
            if "deepseek-reasoner" in text:
                break
        assert "ollama-deepseek-chat" in text      # duplicate → prefixed
        assert "deepseek-deepseek-chat" in text
        assert "deepseek-reasoner" in text          # unique → bare


async def test_reviewer_intro_shown_on_switch(workdir):
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        text = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "CalicoCat" in text
        assert "re-executes every operation" in text


async def test_exit_flavour_message(workdir):
    app = VerifelisApp(FakeBackend(), workdir, reviewer="black")
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/exit "
        await pilot.press("enter")
        await pilot.pause()
        # Read before teardown renders and clears the exit renderables.
        assert any("see you next time meow" in str(r) for r in app._exit_renderables)
    assert not app.is_running
