from pathlib import Path

import pytest

from verifelis.backends import Message
from verifelis.orchestrator import Orchestrator
from verifelis.sandbox import Sandbox
from verifelis.tools import ToolBox


class FakeBackend:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools):
        self.calls.append(list(messages))
        return self.replies.pop(0)


async def test_empty_reply_nudged(tmp_path: Path):
    backend = FakeBackend([
        Message(role="assistant", content="", reasoning="hmm let me think"),
        Message(role="assistant", content="The answer is 7."),
        Message(role="assistant", content="[]"),
    ])
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    orch = Orchestrator(backend, box, reviewer="black")
    result = await orch.run("q")
    assert result.answer == "The answer is 7."
    nudge = backend.calls[1][-1]
    assert nudge.role == "user" and "no content" in nudge.content


async def test_persistent_empty_reply_gives_up(tmp_path: Path):
    backend = FakeBackend([
        Message(role="assistant", content=""),
        Message(role="assistant", content=""),
        Message(role="assistant", content=""),
        Message(role="assistant", content="[]"),
    ])
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    orch = Orchestrator(backend, box, reviewer="black")
    result = await orch.run("q")
    assert result.answer == ""
    assert len(backend.calls) == 4  # 3 white attempts + 1 review, no infinite loop
