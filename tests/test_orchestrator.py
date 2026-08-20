from pathlib import Path

import pytest

from verifelis.backends import Message, ToolCallRequest
from verifelis.orchestrator import Orchestrator, parse_review
from verifelis.sandbox import Sandbox
from verifelis.tools import ToolBox


class FakeBackend:
    """Returns scripted replies in order."""

    name = "fake"

    def __init__(self, replies: list[Message]) -> None:
        self.replies = list(replies)
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools):
        self.calls.append(list(messages))
        return self.replies.pop(0)


@pytest.fixture
def box(tmp_path: Path) -> ToolBox:
    (tmp_path / "notes.txt").write_text("the melting point is 42 C\n")
    return ToolBox(sandbox=Sandbox(tmp_path), pipelines={})


def msg(content="", reasoning="", tool_calls=None):
    return Message(role="assistant", content=content, reasoning=reasoning,
                   tool_calls=tool_calls or [])


async def test_clean_run_no_comments(box):
    backend = FakeBackend([
        msg(tool_calls=[ToolCallRequest("c1", "read_file", {"path": "notes.txt"})],
            reasoning="need to read the file"),
        msg(content="Melting point is 42 C (notes.txt:1)."),
        msg(content="[]"),  # reviewer: clean
    ])
    events = []
    orch = Orchestrator(backend, box, reviewer="black", on_event=events.append)
    result = await orch.run("What is the melting point?")
    assert result.answer.startswith("Melting point")
    assert result.comments == []
    assert result.revised_answer == result.answer
    assert len(result.tool_log) == 1
    assert result.tool_log[0].name == "read_file"
    kinds = [e.kind for e in events]
    assert "phase" in kinds and "done" in kinds


async def test_review_comments_trigger_revision(box):
    backend = FakeBackend([
        msg(content="The melting point is 99 C."),  # white: unverified claim, no tools
        msg(content='[{"type": "unverified_claim", "claim": "99 C", '
                    '"comment": "no tool result supports this"}]'),
        msg(tool_calls=[ToolCallRequest("c1", "read_file", {"path": "notes.txt"})]),
        msg(content="Corrected: melting point is 42 C (notes.txt:1)."),
    ])
    events = []
    orch = Orchestrator(backend, box, reviewer="black", on_event=events.append)
    result = await orch.run("Melting point?")
    assert len(result.comments) == 1
    assert result.comments[0].type == "unverified_claim"
    assert "42 C" in result.revised_answer
    # Revision round actually executed the tool.
    assert any(t.name == "read_file" for t in result.tool_log)
    # Revision prompt contains the comment.
    revision_call = backend.calls[2]
    assert any("no tool result supports this" in m.content for m in revision_call)


async def test_reviewer_sees_reasoning_and_tool_log(box):
    backend = FakeBackend([
        msg(tool_calls=[ToolCallRequest("c1", "read_file", {"path": "notes.txt"})],
            reasoning="I will check notes.txt first"),
        msg(content="42 C.", reasoning="the file says 42"),
        msg(content="[]"),
    ])
    orch = Orchestrator(backend, box, reviewer="black")
    await orch.run("Melting point?")
    dossier = backend.calls[2][1].content
    assert "I will check notes.txt first" in dossier
    assert "Actual tool log" in dossier
    assert "read_file" in dossier
    assert "chain of thought" in dossier


async def test_calico_replays_operations(box):
    backend = FakeBackend([
        msg(tool_calls=[ToolCallRequest("c1", "read_file", {"path": "notes.txt"})]),
        msg(content="42 C (notes.txt:1)."),
        msg(content="[]"),
    ])
    events = []
    orch = Orchestrator(backend, box, reviewer="calico", on_event=events.append)
    result = await orch.run("Melting point?")
    dossier = backend.calls[2][1].content
    assert "CalicoCat replay results" in dossier
    assert "MATCH" in dossier
    # Replay does not pollute WhiteCat's tool log.
    assert len(result.tool_log) == 1
    replay_events = [e for e in events if e.agent == "calico" and e.kind == "tool"]
    assert len(replay_events) == 1


async def test_separate_reviewer_backend(box):
    white = FakeBackend([msg(content="No files checked, answer is 7.")])
    black = FakeBackend([msg(content="[]")])
    orch = Orchestrator(white, box, reviewer="black", reviewer_backend=black)
    await orch.run("q")
    assert len(white.calls) == 1
    assert len(black.calls) == 1


def test_parse_review_robust():
    assert parse_review("[]") == []
    assert parse_review("no json here") == []
    assert parse_review("garbage [ not json ] end") == []
    got = parse_review('Here are my findings:\n[{"type": "unverified_claim", '
                       '"claim": "x", "comment": "y"}]\nDone.')
    assert len(got) == 1
    assert got[0].claim == "x"


async def test_max_iterations_respected(box):
    tc = lambda i: msg(tool_calls=[ToolCallRequest(f"c{i}", "read_file", {"path": "notes.txt"})])
    backend = FakeBackend([tc(1), tc(2), tc(3)])
    events = []
    orch = Orchestrator(backend, box, reviewer="black", on_event=events.append, max_iterations=2)
    reply = await orch._agent_loop("WhiteCat", [Message(role="user", content="q")])
    assert len(backend.calls) == 2
    assert any(e.kind == "error" and "iteration limit" in e.text for e in events)
