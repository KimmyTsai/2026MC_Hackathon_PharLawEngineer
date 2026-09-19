"""LLM boundary.

The orchestrator talks to `LLM`, not to google-genai. That keeps the agent loop
testable without a key or a network call, and keeps the SDK's shapes in one file.

Verified against the live API on 2026-09-19 (see docs/campuspulse-status.md):
- `gemini-3-flash-preview` — 1.70s median, function calling works.
- `gemini-3.8-flash` — 503 "high demand", too unreliable for a demo.
- `gemini-3.1-pro-preview` — hard 429 on this key: the free tier has no Pro
  quota, so reasoning also runs on Flash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class LLMTurn:
    """One model turn: either tool calls, or final text, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The provider's own turn object, kept opaque. Gemini 3 rejects a history
    # whose functionCall parts were rebuilt: they must carry back the
    # thought_signature they arrived with, so we replay this verbatim.
    raw: Any = None
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolDeclaration:
    """A tool as the model sees it. `parameters` is a JSON-schema-ish dict."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLM(Protocol):
    available: bool
    model_id: str

    def turn(
        self,
        system: str,
        history: list[dict[str, Any]],
        tools: list[ToolDeclaration],
    ) -> LLMTurn: ...

    def extract(self, system: str, text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        """Structured reading of one document. None means the model gave nothing
        usable, and the caller must fall back rather than invent a value."""
        ...


class NullLLM:
    """No key configured. The orchestrator falls back to deterministic planning."""

    available = False
    model_id = ""

    def __init__(self, reason: str = "GEMINI_API_KEY 未設定") -> None:
        self.reason = reason

    def turn(self, system, history, tools) -> LLMTurn:  # noqa: ANN001
        return LLMTurn(error=self.reason)

    def extract(self, system, text, schema) -> None:  # noqa: ANN001
        return None


class GeminiLLM:
    """google-genai adapter. Automatic function calling is disabled on purpose:
    CLAUDE.md 7 requires the tool loop to be visible in the agent log."""

    available = True

    def __init__(
        self,
        api_key: str,
        model_id: str,
        temperature: float = 0.0,
        quota_retries: int = 0,
        max_wait_seconds: float = 90.0,
    ) -> None:
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.model_id = model_id
        self.temperature = temperature
        # Free tier is rate limited per minute as well as per day (5 RPM on
        # gemini-3.5-flash, observed 2026-09-19). Waiting is right for the
        # recording script and wrong inside a web request, so it is off by
        # default and scripts/record_cassette.py turns it on.
        self.quota_retries = quota_retries
        self.max_wait_seconds = max_wait_seconds

    # -- conversion ----------------------------------------------------------
    def _to_sdk_tools(self, tools: list[ToolDeclaration]):
        from google.genai import types

        def schema(node: dict[str, Any]):
            kind = node.get("type", "string").upper()
            if kind == "OBJECT":
                return types.Schema(
                    type=types.Type.OBJECT,
                    properties={k: schema(v) for k, v in node.get("properties", {}).items()},
                    required=node.get("required", []),
                    description=node.get("description"),
                )
            if kind == "ARRAY":
                return types.Schema(
                    type=types.Type.ARRAY,
                    items=schema(node.get("items", {"type": "string"})),
                    description=node.get("description"),
                )
            return types.Schema(type=getattr(types.Type, kind), description=node.get("description"))

        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=schema(tool.parameters),
            )
            for tool in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    def _to_sdk_contents(self, history: list[dict[str, Any]]):
        """History items: {"role": "user"|"model", "text": ...},
        {"role": "model", "raw": <provider Content>} or
        {"role": "tool", "name": ..., "response": {...}}."""
        from google.genai import types

        contents = []
        for item in history:
            role = item["role"]
            if item.get("raw") is not None:
                # Replay the model's own turn unchanged: rebuilding a
                # functionCall part drops its thought_signature and Gemini 3
                # answers 400 INVALID_ARGUMENT. A cassette stores the turn as a
                # plain dict, so revive it into a Content first.
                raw = item["raw"]
                contents.append(
                    types.Content.model_validate(raw) if isinstance(raw, dict) else raw
                )
            elif role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=item["name"], response=item["response"]
                            )
                        ],
                    )
                )
            elif role == "model" and item.get("tool_calls"):
                contents.append(
                    types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(name=call.name, args=call.args)
                            for call in item["tool_calls"]
                        ],
                    )
                )
            else:
                contents.append(
                    types.Content(
                        role="model" if role == "model" else "user",
                        parts=[types.Part.from_text(text=item.get("text", ""))],
                    )
                )
        return contents

    # -- one turn ------------------------------------------------------------
    def turn(self, system, history, tools) -> LLMTurn:  # noqa: ANN001
        from google.genai import types

        config = types.GenerateContentConfig(
            tools=self._to_sdk_tools(tools) if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=self.temperature,
            system_instruction=system,
        )
        contents = self._to_sdk_contents(history)
        try:
            response = self._call_with_retry(contents, config)
        except Exception as exc:  # noqa: BLE001 - a provider failure must degrade, not crash
            return LLMTurn(model_id=self.model_id, error=f"{type(exc).__name__}: {exc}")

        calls: list[ToolCall] = []
        texts: list[str] = []
        raw = None
        for candidate in response.candidates or []:
            if raw is None and candidate.content is not None:
                raw = candidate.content
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.function_call:
                    calls.append(
                        ToolCall(name=part.function_call.name, args=dict(part.function_call.args or {}))
                    )
                elif part.text:
                    texts.append(part.text)

        usage = response.usage_metadata
        return LLMTurn(
            text="\n".join(texts).strip(),
            tool_calls=calls,
            raw=raw,
            model_id=self.model_id,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        )


    # -- quota pacing --------------------------------------------------------
    @staticmethod
    def _retry_delay_seconds(message: str) -> float | None:
        """How long to wait before retrying, or None when waiting cannot help.

        Three cases seen on the free tier: a per-minute quota (wait the server's
        retryDelay), a per-day quota (waiting is pointless), and 503 "high
        demand" on the newer models (transient, short wait)."""
        import re

        if "503" in message or "UNAVAILABLE" in message:
            # "This model is currently experiencing high demand" — transient.
            return 15.0
        if "RESOURCE_EXHAUSTED" not in message and "429" not in message:
            return None
        if "PerDay" in message and "PerMinute" not in message:
            return None  # a daily quota will not free up by waiting
        match = re.search(r"retryDelay['\"]?[:=]\s*['\"]?(\d+(?:\.\d+)?)s", message)
        return float(match.group(1)) + 1.0 if match else 20.0

    def _call_with_retry(self, contents, config):  # noqa: ANN001
        import time

        attempts = self.quota_retries + 1
        waited = 0.0
        for attempt in range(1, attempts + 1):
            try:
                return self._client.models.generate_content(
                    model=self.model_id, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001
                if attempt == attempts:
                    raise
                delay = self._retry_delay_seconds(str(exc))
                if delay is None or waited + delay > self.max_wait_seconds:
                    raise
                print(f"    額度限制，等 {delay:.0f}s 後重試（{self.model_id}）", flush=True)
                time.sleep(delay)
                waited += delay
        raise RuntimeError("unreachable")

    # -- structured extraction ------------------------------------------------
    def extract(self, system, text, schema) -> dict[str, Any] | None:  # noqa: ANN001
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=0,
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        try:
            response = self._client.models.generate_content(
                model=self.model_id, contents=text, config=config
            )
        except Exception:  # noqa: BLE001 - caller falls back
            return None
        import json

        raw = (response.text or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


def build_llm(settings: Settings) -> LLM:
    """Flash for everything: Pro has no quota on the free tier (see module docstring).

    Wrapped in a cassette unless AGENT_MODE=live, because the free tier allows
    only 20 requests per day per model.
    """
    from app.agent.cassette import CachedLLM, fingerprint
    from app.agent.prompts import SYSTEM_PROMPT

    mode = (settings.agent_mode or "cache").lower()
    if mode == "off":
        return NullLLM("AGENT_MODE=off，改用確定性規劃")
    if not settings.has_gemini and mode != "replay":
        return NullLLM()

    inner: LLM = (
        GeminiLLM(
            api_key=settings.gemini_api_key or "",
            model_id=settings.reasoning_model(),
            quota_retries=settings.quota_retries,
        )
        if settings.has_gemini
        else NullLLM()
    )
    if mode == "live":
        return inner

    from app.agent.tools import declarations

    return CachedLLM(
        inner,
        settings.cassette_path,
        mode=mode,
        expected_fingerprint=fingerprint(settings.scenario_path, SYSTEM_PROMPT, declarations()),
    )
