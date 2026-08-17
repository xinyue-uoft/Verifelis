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


def to_openai_chat(messages: list[Message], args_as_object: bool = False) -> list[dict[str, Any]]:
    """Convert to chat-completions wire format.

    args_as_object=True for ollama: it requires tool_call arguments as a
    JSON object and non-null assistant content; OpenAI/DeepSeek require a
    JSON-encoded string.
    """
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            wire.append(
                {
                    "role": "assistant",
                    "content": m.content if args_as_object else (m.content or None),
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": tc.arguments
                                if args_as_object
                                else json.dumps(tc.arguments),
                            },
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        else:
            wire.append({"role": m.role, "content": m.content})
    return wire


def fetch_model_catalog(config: dict[str, Any]) -> dict[str, list[str]]:
    """Available models per backend, for backends whose auth is present.

    ollama: GET /api/tags. deepseek/openai: OpenAI-style GET /models
    (deepseek endpoint per api-docs.deepseek.com "Lists Models").
    Unreachable or unauthenticated backends are simply omitted.
    """
    import os

    import httpx

    from .. import credentials
    from . import oauth

    catalog: dict[str, list[str]] = {}
    host = config.get("ollama_host", "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{host}/api/tags", timeout=5)
        r.raise_for_status()
        catalog["ollama"] = sorted(m["name"] for m in r.json().get("models", []))
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    ds_key = config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or credentials.get("deepseek")
    if ds_key:
        try:
            r = httpx.get(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {ds_key}"},
                timeout=10,
            )
            r.raise_for_status()
            catalog["deepseek"] = sorted(m["id"] for m in r.json().get("data", []))
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    oa_token = os.environ.get("OPENAI_API_KEY") or oauth.get_access_token()
    if oa_token:
        try:
            r = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {oa_token}"},
                timeout=10,
            )
            r.raise_for_status()
            catalog["openai"] = sorted(m["id"] for m in r.json().get("data", []))
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    return catalog


def resolve_model(
    catalog: dict[str, list[str]], name: str, current_backend: str
) -> tuple[str, str]:
    """Resolve a bare or backend-prefixed model name to (backend, model).

    Prefixed form ("deepseek-deepseek-chat") always wins. A bare name found
    in exactly one backend switches to it; duplicates raise with the
    prefixed candidates; an unknown name stays on the current backend.
    """
    for b, models in catalog.items():
        prefix = b + "-"
        if name.startswith(prefix) and name[len(prefix):] in models:
            return b, name[len(prefix):]
    owners = [b for b, models in catalog.items() if name in models]
    if len(owners) == 1:
        return owners[0], name
    if len(owners) > 1:
        options = ", ".join(f"{b}-{name}" for b in owners)
        raise ValueError(f"'{name}' exists on several backends — use one of: {options}")
    return current_backend, name


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
