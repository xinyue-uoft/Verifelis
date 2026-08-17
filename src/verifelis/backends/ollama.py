"""Ollama backend via /api/chat (non-streaming).

Reasoning arrives either in message.thinking (models with think support)
or inline <think> tags; both are normalized into Message.reasoning.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import Message, ToolCallRequest, strip_think_tags, to_openai_chat


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str, host: str = "http://localhost:11434") -> None:
        self.model = model
        self.host = host.rstrip("/")

    async def chat(self, messages: list[Message], tools: list[dict[str, Any]]) -> Message:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_chat(messages, args_as_object=True),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{self.host}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        msg = data.get("message", {})
        content = msg.get("content", "") or ""
        reasoning = msg.get("thinking", "") or ""
        content, tag_reasoning = strip_think_tags(content)
        if tag_reasoning:
            reasoning = (reasoning + "\n" + tag_reasoning).strip()
        tool_calls: list[ToolCallRequest] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(
                ToolCallRequest(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args)
            )
        return Message(role="assistant", content=content, reasoning=reasoning, tool_calls=tool_calls)
