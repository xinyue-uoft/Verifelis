"""Verification loop: WhiteCat works, BlackCat/CalicoCat reviews,
WhiteCat addresses comments.

All progress is emitted as Events so the TUI can show what each cat
is doing at any moment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .backends import Backend, Message
from .tools import ToolBox, ToolCall

MAX_TOOL_ITERATIONS = 20

WHITE_SYSTEM = """You are WhiteCat, a careful read-only research agent for scientific \
documents. You may ONLY observe: list directories, read files, grep, stat, and run \
whitelisted extraction pipelines. You cannot write files or run other commands.

Rules:
- Every factual claim in your answer must be grounded in a tool result you actually \
obtained in this conversation. Cite the source as file:line or (pipeline output).
- If you could not verify something, say so explicitly.
- Never invent file contents. Never claim to have performed an operation you did not perform.
- Answer concisely when the evidence is in."""

REVIEW_INSTRUCTIONS = """You are {name}, a strict verification reviewer. You are given \
WhiteCat's full transcript: its chain of thought, its actual tool call log (ground \
truth of what was really executed), and its final answer.

Audit the final answer against the tool log. Flag:
1. unverified_claim - a claim not supported by any tool result actually obtained
2. claimed_not_performed - an operation WhiteCat claims to have done that does not \
appear in the tool log
3. ungrounded_source - a citation that does not match the cited file/line content
4. inconsistency - answer contradicts tool results or its own reasoning

Respond with ONLY a JSON array (no prose). Each element:
{{"type": "<one of the four above>", "claim": "<the problematic text>", "comment": "<why it fails and what would fix it>"}}
Return [] if the answer is fully verified."""

CALICO_EXTRA = """
You are the thorough variant: before judging, you re-executed WhiteCat's operations \
yourself. Your replay results are included below; treat them as ground truth. If a \
replay result differs from what WhiteCat reported, that is an inconsistency."""

REVISION_PROMPT = """A reviewer ({reviewer}) audited your answer and filed {n} comment(s):

{comments}

Address every comment: verify what was unverified (use your tools), remove or correct \
what cannot be verified, and produce a corrected final answer. State explicitly which \
comments you fixed and which you reject (with grounds)."""


@dataclass
class Event:
    kind: str  # phase | status | reasoning | tool | message | comment | done | error
    agent: str  # white | black | calico | system
    text: str
    data: Any = None


EventSink = Callable[[Event], None]


@dataclass
class ReviewComment:
    type: str
    claim: str
    comment: str


@dataclass
class SessionResult:
    answer: str
    comments: list[ReviewComment]
    revised_answer: str = ""
    white_transcript: list[Message] = field(default_factory=list)
    tool_log: list[ToolCall] = field(default_factory=list)


def parse_review(content: str) -> list[ReviewComment]:
    """Extract the first JSON array from reviewer output."""
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[ReviewComment] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(
                ReviewComment(
                    type=str(item.get("type", "other")),
                    claim=str(item.get("claim", "")),
                    comment=str(item.get("comment", "")),
                )
            )
    return out


class Orchestrator:
    def __init__(
        self,
        backend: Backend,
        toolbox: ToolBox,
        reviewer: str = "black",  # black | calico
        on_event: EventSink | None = None,
        reviewer_backend: Backend | None = None,
    ) -> None:
        self.backend = backend
        self.reviewer_backend = reviewer_backend or backend
        self.toolbox = toolbox
        self.reviewer = reviewer
        self.on_event = on_event or (lambda e: None)

    def _emit(self, kind: str, agent: str, text: str, data: Any = None) -> None:
        self.on_event(Event(kind=kind, agent=agent, text=text, data=data))

    async def _agent_loop(
        self, agent: str, messages: list[Message], use_tools: bool = True
    ) -> Message:
        """Standard tool loop. Mutates `messages` in place."""
        tools = self.toolbox.specs() if use_tools else []
        for _ in range(MAX_TOOL_ITERATIONS):
            self._emit("status", agent, "thinking…")
            reply = await self.backend.chat(messages, tools)
            messages.append(reply)
            if reply.reasoning:
                self._emit("reasoning", agent, reply.reasoning)
            if not reply.tool_calls:
                return reply
            for tc in reply.tool_calls:
                self._emit("status", agent, f"running {tc.name}({_short(tc.arguments)})")
                result = self.toolbox.call(tc.name, tc.arguments)
                self._emit("tool", agent, result.digest(), data=result)
                messages.append(
                    Message(role="tool", content=result.result, tool_call_id=tc.id)
                )
        self._emit("error", agent, "tool iteration limit reached")
        return messages[-1] if messages[-1].role == "assistant" else Message(
            role="assistant", content="(iteration limit reached without final answer)"
        )

    def _dossier(self, transcript: list[Message], tool_log: list[ToolCall]) -> str:
        parts = ["=== WhiteCat transcript ==="]
        for m in transcript:
            if m.role == "assistant":
                if m.reasoning:
                    parts.append(f"[chain of thought]\n{m.reasoning}")
                if m.tool_calls:
                    for tc in m.tool_calls:
                        parts.append(f"[requested tool] {tc.name}({_short(tc.arguments)})")
                if m.content:
                    parts.append(f"[assistant said]\n{m.content}")
        parts.append("\n=== Actual tool log (ground truth) ===")
        if tool_log:
            parts.extend(tc.digest(limit=600) for tc in tool_log)
        else:
            parts.append("(no tools were executed)")
        return "\n".join(parts)

    async def _replay(self, tool_log: list[ToolCall]) -> str:
        """CalicoCat re-executes every WhiteCat operation.

        Uses a separate ToolBox over the same sandbox so replay calls do
        not pollute WhiteCat's tool log.
        """
        replay_box = ToolBox(sandbox=self.toolbox.sandbox, pipelines=self.toolbox.pipelines)
        parts = ["=== CalicoCat replay results ==="]
        for tc in tool_log:
            self._emit("status", "calico", f"replaying {tc.name}({_short(tc.args)})")
            redo = replay_box.call(tc.name, tc.args)
            match = "MATCH" if redo.result == tc.result else "DIFFERS"
            self._emit("tool", "calico", f"replay {tc.name}: {match}", data=redo)
            parts.append(f"{redo.digest(limit=600)}\n  vs WhiteCat: {match}")
        if not tool_log:
            parts.append("(nothing to replay)")
        return "\n".join(parts)

    async def run(self, question: str) -> SessionResult:
        # Phase 1: WhiteCat works.
        self._emit("phase", "system", "WhiteCat is working")
        transcript: list[Message] = [
            Message(role="system", content=WHITE_SYSTEM),
            Message(role="user", content=question),
        ]
        log_start = len(self.toolbox.log)
        final = await self._agent_loop("white", transcript)
        tool_log = self.toolbox.log[log_start:]
        self._emit("message", "white", final.content)

        # Phase 2: review.
        reviewer_name = "CalicoCat" if self.reviewer == "calico" else "BlackCat"
        self._emit("phase", "system", f"{reviewer_name} is reviewing")
        dossier = self._dossier(transcript, tool_log)
        instructions = REVIEW_INSTRUCTIONS.format(name=reviewer_name)
        if self.reviewer == "calico":
            instructions += CALICO_EXTRA
            dossier += "\n\n" + await self._replay(tool_log)
        review_messages = [
            Message(role="system", content=instructions),
            Message(role="user", content=dossier + f"\n\n=== Original question ===\n{question}"),
        ]
        agent_tag = self.reviewer
        self._emit("status", agent_tag, "auditing transcript…")
        review_reply = await self.reviewer_backend.chat(review_messages, [])
        if review_reply.reasoning:
            self._emit("reasoning", agent_tag, review_reply.reasoning)
        comments = parse_review(review_reply.content)
        for c in comments:
            self._emit("comment", agent_tag, f"[{c.type}] {c.claim}: {c.comment}", data=c)
        self._emit(
            "phase", "system", f"{reviewer_name} filed {len(comments)} comment(s)"
        )

        result = SessionResult(
            answer=final.content,
            comments=comments,
            white_transcript=transcript,
            tool_log=tool_log,
        )
        if not comments:
            self._emit("done", "system", "verified: no comments")
            result.revised_answer = final.content
            return result

        # Phase 3: WhiteCat addresses comments.
        self._emit("phase", "system", "WhiteCat is addressing comments")
        comment_text = "\n".join(
            f"{i}. [{c.type}] {c.claim}\n   -> {c.comment}" for i, c in enumerate(comments, 1)
        )
        transcript.append(
            Message(
                role="user",
                content=REVISION_PROMPT.format(
                    reviewer=reviewer_name, n=len(comments), comments=comment_text
                ),
            )
        )
        revised = await self._agent_loop("white", transcript)
        result.revised_answer = revised.content
        result.tool_log = self.toolbox.log[log_start:]
        self._emit("message", "white", revised.content)
        self._emit("done", "system", "revision complete")
        return result


def _short(args: dict[str, Any], limit: int = 120) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"
