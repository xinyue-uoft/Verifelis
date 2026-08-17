"""v1.0.1 features: credentials store, slash commands, menu, @ references."""

import stat
from pathlib import Path

import pytest

from verifelis import credentials
from verifelis.backends import Message
from verifelis.tui import VerifelisApp, PromptInput


@pytest.fixture(autouse=True)
def cred_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "cred_file", lambda: tmp_path / "creds.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return tmp_path


class FakeBackend:
    name = "fake"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools):
        self.calls.append(list(messages))
        return self.replies.pop(0)


@pytest.fixture
def workdir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("ws")
    (d / "doc.txt").write_text("fact\n")
    (d / "paper_notes.md").write_text("notes\n")
    (d / ".env").write_text("KEY=x\n")
    return d


def make_app(workdir, replies=()):
    return VerifelisApp(FakeBackend(replies), workdir, reviewer="black")


# -- credentials --

def test_credentials_roundtrip_and_perms(cred_tmp):
    credentials.store("deepseek", "  sk-test-123  ")
    assert credentials.get("deepseek") == "sk-test-123"
    mode = stat.S_IMODE((cred_tmp / "creds.json").stat().st_mode)
    assert mode == 0o600


def test_deepseek_uses_stored_credential(cred_tmp):
    from verifelis.backends.deepseek import DeepSeekBackend

    with pytest.raises(ValueError):
        DeepSeekBackend()
    credentials.store("deepseek", "sk-stored")
    assert DeepSeekBackend().api_key == "sk-stored"


# -- slash menu --

async def test_slash_opens_and_filters_menu(workdir):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        await pilot.press("/")
        assert app.menu_mode == "command"
        from verifelis.tui import COMMANDS
        assert len(app.menu_items) == len(COMMANDS)
        await pilot.press("m", "o")
        assert app.menu_items == ["/model"]
        await pilot.press("enter")  # accept completion
        assert app.query_one(PromptInput).value == "/model "
        assert app.menu_mode == ""


async def test_exit_command(workdir):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/exi"
        await pilot.press("t")   # menu open on "/exit"
        await pilot.press("enter")  # completion -> "/exit "
        await pilot.press("enter")  # execute
        await pilot.pause()
    assert not app.is_running


async def test_model_switch_ollama(workdir):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/model ollama qwen3:4b"
        await pilot.press("enter")
        assert app.backend.name == "ollama"
        assert app.backend.model == "qwen3:4b"
        assert app.config["model"] == "qwen3:4b"


async def test_model_switch_failure_keeps_backend(workdir):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        old = app.backend
        inp = app.query_one(PromptInput)
        inp.value = "/model deepseek deepseek-chat"  # no key stored
        await pilot.press("enter")
        assert app.backend is old
        chat_lines = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "switch failed" in chat_lines


async def test_login_deepseek_persists(workdir, cred_tmp):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/login deepseek sk-via-tui"
        await pilot.press("enter")
    assert credentials.get("deepseek") == "sk-via-tui"


async def test_reviewer_command(workdir):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/reviewer calico"
        await pilot.press("enter")
        assert app.reviewer == "calico"


# -- @ references --

async def test_at_menu_lists_workspace_files_not_secrets(workdir):
    app = make_app(workdir)
    async with app.run_test() as pilot:
        await pilot.press("@")
        assert app.menu_mode == "file"
        assert "doc.txt" in app.menu_items
        assert ".env" not in app.menu_items
        await pilot.press("p", "a", "p")  # filter
        assert app.menu_items == ["paper_notes.md"]
        await pilot.press("enter")
        assert app.query_one(PromptInput).value == "@paper_notes.md "


async def test_at_reference_expanded_in_question(workdir):
    backend = FakeBackend([
        Message(role="assistant", content="done"),
        Message(role="assistant", content="[]"),
    ])
    app = VerifelisApp(backend, workdir, reviewer="black")
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "summarize @doc.txt please"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if not app.busy:
                break
        user_msg = backend.calls[0][1].content
        assert "[Referenced documents]" in user_msg
        assert "- doc.txt" in user_msg
