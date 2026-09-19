"""Recorded model turns.

The free tier allows 20 generate_content requests per day per model
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, quotaValue 20, observed
2026-09-19). One agent run costs 2–6 of them, so a handful of demo rehearsals
exhausts the day.

`CachedLLM` records each real turn against a hash of what the model was asked,
and replays it when the same question comes round again. A replayed demo runs
the real loop shape — same tools, same order, same wording — with zero requests,
which is also what CLAUDE.md 2.4 asks for: the demo must not depend on the live
world. Replayed turns are labelled, never passed off as fresh.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.agent.llm import LLM, LLMTurn, ToolCall, ToolDeclaration


def turn_key(system: str, history: list[dict[str, Any]], tools: list[ToolDeclaration]) -> str:
    """Stable hash of the question. `raw` is excluded: it carries provider-side
    signatures that differ between runs but say nothing about the question."""
    shape = {
        "system": system,
        "tools": sorted(tool.name for tool in tools),
        "history": [
            {
                "role": item.get("role"),
                "text": item.get("text", ""),
                "name": item.get("name"),
                "response": item.get("response"),
                "tool_calls": [
                    {"name": c.name, "args": c.args} for c in item.get("tool_calls", []) or []
                ],
            }
            for item in history
        ],
    }
    blob = json.dumps(shape, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class CachedLLM:
    """Wraps a live LLM with an on-disk cassette.

    modes:
      cache  — replay a hit, otherwise call the inner LLM and record it (default)
      live   — always call, never read or write the cassette
      replay — cassette only; a miss returns an error so the caller falls back
    """

    def __init__(self, inner: LLM, path: Path, mode: str = "cache") -> None:
        self.inner = inner
        self.path = path
        self.mode = mode
        self.hits = 0
        self.misses = 0
        self._entries: dict[str, dict[str, Any]] = {}
        self.recorded_model: str | None = None
        if mode in {"cache", "replay"}:
            self._load()

    @property
    def available(self) -> bool:
        if self.mode == "replay":
            return bool(self._entries)
        return bool(getattr(self.inner, "available", False))

    @property
    def model_id(self) -> str:
        """In replay the recording model is what actually spoke, so name it.

        The UI must never imply a live call that did not happen
        (demo-and-acceptance.md lists that as a failure condition).
        """
        if self.mode == "replay" and self.recorded_model:
            return self.recorded_model
        return getattr(self.inner, "model_id", "") or "cassette"

    @property
    def replaying(self) -> bool:
        return self.mode == "replay"

    @property
    def reason(self) -> str | None:
        if self.mode == "replay" and not self._entries:
            return f"沒有錄音檔 {self.path.name}"
        return getattr(self.inner, "reason", None)

    # -- storage -------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self._entries = {entry["key"]: entry for entry in raw.get("turns", [])}
        self.recorded_model = raw.get("model") or None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "note": (
                "Recorded Gemini turns. Replayed in the demo so it costs no quota "
                "and reproduces exactly. Delete a turn to re-record it."
            ),
            # The model that actually produced these turns, which is what the
            # UI must name when replaying them.
            "model": self.recorded_model or getattr(self.inner, "model_id", "") or "unknown",
            "turns": list(self._entries.values()),
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- LLM ----------------------------------------------------------------
    def turn(self, system, history, tools) -> LLMTurn:  # noqa: ANN001
        key = turn_key(system, history, tools)

        if self.mode != "live":
            entry = self._entries.get(key)
            if entry is not None and self.mode == "cache" and entry.get("tool_calls"):
                # A recorded tool turn without its thought_signature cannot be
                # continued live: Gemini 3 rejects the history with 400. Treat it
                # as a miss so it is re-recorded complete.
                if not entry.get("raw"):
                    entry = None
            if entry is not None:
                self.hits += 1
                return LLMTurn(
                    text=entry.get("text", ""),
                    tool_calls=[
                        ToolCall(name=c["name"], args=c.get("args", {}))
                        for c in entry.get("tool_calls", [])
                    ],
                    raw=entry.get("raw"),
                    model_id=entry.get("model_id", self.model_id),
                    input_tokens=0,
                    output_tokens=0,
                )
            if self.mode == "replay":
                self.misses += 1
                return LLMTurn(error=f"錄音檔沒有這一步（{key}）")

        self.misses += 1
        turn = self.inner.turn(system, history, tools)
        if self.mode == "cache" and not turn.error:
            self._entries[key] = {
                "key": key,
                "text": turn.text,
                "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls],
                # The provider's own turn, including the thought_signature that
                # Gemini 3 demands back verbatim when the run continues live.
                "raw": (
                    turn.raw.model_dump(mode="json")
                    if hasattr(turn.raw, "model_dump")
                    else turn.raw
                ),
                "model_id": turn.model_id,
            }
            self.recorded_model = turn.model_id or self.recorded_model
            self._save()
        return turn

    def extract(self, system, text, schema) -> dict[str, Any] | None:  # noqa: ANN001
        """Document reading is cached the same way, keyed by the document."""
        key = "extract:" + turn_key(system, [{"role": "user", "text": text}], [])

        if self.mode != "live":
            entry = self._entries.get(key)
            if entry is not None:
                self.hits += 1
                return entry.get("result")
            if self.mode == "replay":
                self.misses += 1
                return None

        self.misses += 1
        result = self.inner.extract(system, text, schema)
        if self.mode == "cache" and result is not None:
            self._entries[key] = {"key": key, "result": result, "model_id": self.model_id}
            self.recorded_model = self.recorded_model or getattr(self.inner, "model_id", None)
            self._save()
        return result
