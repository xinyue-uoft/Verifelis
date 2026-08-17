"""v1.0.2: reviewer must see real evidence — excerpts + own read tools.

Regression for the false-flag bug: with 600-char digests, BlackCat could
not see content WhiteCat cited from deep in a file and wrongly filed
unverified_claim comments.
"""

from pathlib import Path

import pytest

from verifelis.backends import Message, ToolCallRequest
from verifelis.orchestrator import Orchestrator
from verifelis.sandbox import Sandbox
from verifelis.tools import ToolBox, ToolCall


class FakeBackend:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools):
        self.calls.append(list(messages))
        return self.replies.pop(0)


def msg(content="", reasoning="", tool_calls=None):
    return Message(role="assistant", content=content, reasoning=reasoning,
                   tool_calls=tool_calls or [])


def test_excerpt_short_result_full():
    tc = ToolCall(name="read_file", args={"path": "a"}, result="short result")
    assert "short result" in tc.excerpt()
    assert "truncated" not in tc.excerpt()


def test_excerpt_long_result_head_tail_marker():
    body = "H" * 5000 + "MIDDLE" + "T" * 5000
    tc = ToolCall(name="read_file", args={"path": "a"}, result=body)
    ex = tc.excerpt(budget=4096)
    assert ex.startswith("read_file")
    assert "HHHH" in ex and "TTTT" in ex
    assert "read the source yourself to verify" in ex
    omitted = len(body) - (4096 * 2) // 3 - 4096 // 3
    assert f"{omitted} bytes omitted" in ex


async def test_dossier_contains_deep_content(tmp_path: Path):
    """Fact at char ~900 (beyond the old 600-char digest) reaches the reviewer."""
    filler = ("x" * 76 + "\n") * 11  # ~850 chars of noise
    (tmp_path / "long.txt").write_text(filler + "boiling point = 373 K\n")
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    backend = FakeBackend([
        msg(tool_calls=[ToolCallRequest("c1", "read_file", {"path": "long.txt"})]),
        msg(content="Boiling point is 373 K (long.txt:12)."),
        msg(content="[]"),
    ])
    orch = Orchestrator(backend, box, reviewer="black")
    await orch.run("boiling point?")
    dossier = backend.calls[2][1].content
    assert "boiling point = 373 K" in dossier


async def test_reviewer_can_use_tools_without_polluting_white_log(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("value = 7\n")
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    backend = FakeBackend([
        msg(content="The value is 7 (notes.txt:1)."),  # white: no tools used
        # reviewer spot-checks the citation itself, then files nothing
        msg(tool_calls=[ToolCallRequest("r1", "read_file", {"path": "notes.txt"})]),
        msg(content="[]"),
    ])
    events = []
    orch = Orchestrator(backend, box, reviewer="black", on_event=events.append)
    result = await orch.run("value?")
    assert result.comments == []
    # White's log stays empty; reviewer's read happened and was emitted.
    assert result.tool_log == []
    reviewer_tools = [e for e in events if e.agent == "black" and e.kind == "tool"]
    assert len(reviewer_tools) == 1
    assert "value = 7" in reviewer_tools[0].data.result
    # The reviewer's tool result went back into its own conversation.
    tool_msgs = [m for m in backend.calls[2] if m.role == "tool"]
    assert len(tool_msgs) == 1 and "value = 7" in tool_msgs[0].content


async def test_review_instructions_mention_tools_and_truncation(tmp_path: Path):
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    backend = FakeBackend([
        msg(content="nothing to report"),
        msg(content="[]"),
    ])
    orch = Orchestrator(backend, box, reviewer="black")
    await orch.run("q")
    system = backend.calls[1][0].content
    assert "truncation is NOT evidence" in system
    assert "verify the claim yourself" in system
