"""Minimal LLM client — the ONLY place Talentgram talks to an LLM provider.

One provider (Anthropic), one call shape (a single forced-tool call that
returns a validated JSON object). No streaming, no agent loop, no memory,
no retry framework beyond the SDK's own. ~1 function of real surface area.

Env:
  ANTHROPIC_API_KEY            required — the provider credential (server-side only)
  CASTING_DESK_MODEL           optional — model id (default: claude-opus-5)
  CASTING_DESK_LLM_TIMEOUT_SEC optional — per-call wall clock (default: 90)

Callers catch:
  LLMUnavailable — no API key configured / SDK missing (operator problem)
  LLMError       — the provider call failed or returned something unusable
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("CASTING_DESK_MODEL", "claude-opus-5")
_TIMEOUT_SEC = float(os.environ.get("CASTING_DESK_LLM_TIMEOUT_SEC", "90"))


class LLMUnavailable(RuntimeError):
    """LLM cannot be reached at all — missing key or missing SDK. Distinct
    from LLMError so the API layer can return 503 (retry later / call ops)
    vs 502 (the model misbehaved on this input)."""


class LLMError(RuntimeError):
    """The provider call ran but failed, timed out, or produced output that
    could not be turned into the requested JSON object."""


def is_configured() -> bool:
    """True when a call would at least be attempted (key present)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    try:
        import anthropic  # lazy — keeps module import light for tests/tools
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("the 'anthropic' package is not installed") from exc
    return anthropic.AsyncAnthropic(api_key=api_key, timeout=_TIMEOUT_SEC, max_retries=2)


async def call_tool_json(
    *,
    system: str,
    user: str,
    tool_name: str,
    tool_description: str,
    input_schema: Dict[str, Any],
    max_tokens: int = 4000,
    model: str | None = None,
) -> Dict[str, Any]:
    """Single-shot structured extraction.

    Sends ``system`` + ``user`` and forces the model to answer by calling
    one tool whose ``input_schema`` is ``input_schema``. Returns the tool
    input dict — guaranteed to be a JSON object (schema validity is still
    the caller's job; the model is strongly steered but the API only
    guarantees well-formed JSON of the right shape when ``strict`` holds).
    """
    client = _client()
    mdl = model or DEFAULT_MODEL
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("the 'anthropic' package is not installed") from exc

    try:
        resp = await client.messages.create(
            model=mdl,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": tool_description,
                    "strict": True,
                    "input_schema": input_schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
    except anthropic.APIStatusError as exc:
        # 401/403 → treat as an operator/config problem, not a bad-input problem.
        if exc.status_code in (401, 403):
            raise LLMUnavailable(f"LLM auth failed ({exc.status_code})") from exc
        logger.warning("casting-desk LLM call failed: %s", exc)
        raise LLMError(f"LLM call failed ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"LLM connection error: {exc}") from exc
    except anthropic.AnthropicError as exc:
        raise LLMError(f"LLM error: {exc}") from exc

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            data = block.input
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise LLMError("LLM returned non-JSON tool input") from exc
            if not isinstance(data, dict):
                raise LLMError("LLM tool input was not a JSON object")
            return data

    raise LLMError(f"LLM did not call '{tool_name}' (stop_reason={resp.stop_reason})")
