"""DeepSeek backend. OpenAI chat-completions wire format, API-key auth.

deepseek-reasoner returns chain of thought in reasoning_content.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from . import Message, ToolCallRequest, to_openai_chat

API_BASE = "https://api.deepseek.com"


class DeepSeekBackend:
    name = "deepseek"

    def __init__(self, model: str = "deepseek-chat", api_key: str = "") -> None:
        from .. import credentials

        self.model = model
        # Resolution order: explicit arg > env > stored credential.
        self.api_key = (
            api_key or os.environ.get("DEEPSEEK_API_KEY", "") or credentials.get("deepseek")
        )
        if not self.api_key:
            raise ValueError(
                "DeepSeek requires an API key (DEEPSEEK_API_KEY, config api_key, "
                "or `verifelis login deepseek`)"
            )

    async def chat(self, messages: list[Message], tools: list[dict[str, Any]]) -> Message:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_chat(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{API_BASE}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls: list[ToolCallRequest] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCallRequest(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args)
            )
        return Message(
            role="assistant",
            content=msg.get("content") or "",
            reasoning=msg.get("reasoning_content") or "",
            tool_calls=tool_calls,
        )
