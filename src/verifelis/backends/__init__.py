"""Backend abstraction.

A backend turns (messages, tool specs) into one assistant Message.
`reasoning` carries chain of thought; the reviewer depends on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_id: str = ""


class Backend(Protocol):
    name: str

    async def chat(self, messages: list[Message], tools: list[dict[str, Any]]) -> Message: ...


def strip_think_tags(content: str) -> tuple[str, str]:
    """Extract <think>...</think> blocks. Returns (content, reasoning)."""
    reasoning_parts: list[str] = []
    out = content
    while "<think>" in out:
        start = out.index("<think>")
        end = out.find("</think>", start)
        if end == -1:
            reasoning_parts.append(out[start + 7 :])
            out = out[:start]
            break
        reasoning_parts.append(out[start + 7 : end])
        out = out[:start] + out[end + 8 :]
    return out.strip(), "\n".join(p.strip() for p in reasoning_parts)


def to_openai_chat(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert to OpenAI chat-completions wire format (ollama/deepseek)."""
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            wire.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        else:
            wire.append({"role": m.role, "content": m.content})
    return wire


def make_backend(config: dict[str, Any]) -> Backend:
    kind = config.get("backend", "ollama")
    if kind == "ollama":
        from .ollama import OllamaBackend

        return OllamaBackend(
            model=config.get("model", "qwen3:4b"),
            host=config.get("ollama_host", "http://localhost:11434"),
        )
    if kind == "deepseek":
        from .deepseek import DeepSeekBackend

        return DeepSeekBackend(model=config.get("model", "deepseek-chat"), api_key=config.get("api_key", ""))
    if kind == "openai":
        from .openai_responses import OpenAIResponsesBackend

        return OpenAIResponsesBackend(model=config.get("model", "gpt-5.2"))
    raise ValueError(f"unknown backend: {kind}")
