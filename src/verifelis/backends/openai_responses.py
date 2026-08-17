"""OpenAI backend using the Responses API format.

Auth resolution order: OPENAI_API_KEY env, then stored OAuth token
(see oauth.py). Reasoning summaries land in Message.reasoning.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from . import Message, ToolCallRequest
from . import oauth

API_BASE = "https://api.openai.com/v1"


class OpenAIResponsesBackend:
    name = "openai"

    def __init__(self, model: str = "gpt-5.2", api_base: str = API_BASE) -> None:
        self.model = model
        self.api_base = api_base.rstrip("/")

    def _token(self) -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            return key
        token = oauth.get_access_token()
        if token:
            return token
        raise ValueError(
            "OpenAI auth missing: set OPENAI_API_KEY or run `verifelis login openai`"
        )

    def _to_input(self, messages: list[Message]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                items.append(
                    {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content}
                )
            elif m.role == "assistant" and m.tool_calls:
                if m.content:
                    items.append({"role": "assistant", "content": m.content})
                for tc in m.tool_calls:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        }
                    )
            else:
                items.append({"role": m.role, "content": m.content})
        return items

    def _to_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Responses API flattens function specs: name at top level.
        out = []
        for t in tools:
            fn = t.get("function", t)
            out.append(
                {
                    "type": "function",
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        return out

    async def chat(self, messages: list[Message], tools: list[dict[str, Any]]) -> Message:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self._to_input(messages),
            "reasoning": {"summary": "auto"},
        }
        if tools:
            payload["tools"] = self._to_tools(tools)
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{self.api_base}/responses",
                json=payload,
                headers={"Authorization": f"Bearer {self._token()}"},
            )
            resp.raise_for_status()
            data = resp.json()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for item in data.get("output", []):
            kind = item.get("type")
            if kind == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        content_parts.append(c.get("text", ""))
            elif kind == "reasoning":
                for s in item.get("summary", []):
                    reasoning_parts.append(s.get("text", ""))
            elif kind == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCallRequest(
                        id=item.get("call_id", ""), name=item.get("name", ""), arguments=args
                    )
                )
        return Message(
            role="assistant",
            content="\n".join(content_parts),
            reasoning="\n".join(reasoning_parts),
            tool_calls=tool_calls,
        )
