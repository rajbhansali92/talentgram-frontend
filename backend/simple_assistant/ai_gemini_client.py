"""Gemini provider migration — an isolated, Simple-Assistant-only LLM client.

Google GenAI Migration (2026-09-15). Simple Assistant's preferred provider is
now Google Gemini instead of Anthropic. This module is a SEPARATE, additive
adapter — it does NOT touch `backend/ai/client.py`, which remains exactly as
it was and is still used, unmodified, by AI Scout (routers/ai_scout.py,
ai/scout.py) and AI Casting Desk (routers/casting_desk.py,
ai/casting_requirement.py) — both frozen/live features that must not be
affected by this migration in any way.

Mirrors `ai.client`'s call contract EXACTLY (same function name/signature,
same two exception classes — imported from `ai.client`, not redefined, so
every existing `except ai_client.LLMUnavailable` / `except
ai_client.LLMError` in simple_assistant/ai_response.py keeps working
unchanged regardless of which provider module `ai_client` currently refers
to) so `ai_response.py` needs only a one-line provider-selection change, not
a rewrite:

    is_configured() -> bool
    async def call_tool_json(*, system, user, tool_name, tool_description,
                              input_schema, max_tokens=4000, model=None) -> dict

SDK: the official `google-genai` package (already a pinned dependency,
google-genai==1.71.0 in requirements.txt — pre-existing, unused until now).
`google-generativeai` is NOT used: Google's own README marks it end-of-life
("All support ... has ended ... switch to `google.genai`").

Uses Gemini's NATIVE structured-output support (`response_mime_type=
"application/json"` + `response_json_schema=<the same JSON Schema
ai_response.py already builds for Anthropic's tool input_schema>`) — no tool-
calling shim, no second schema dialect. `tool_name` / `tool_description` are
accepted (for signature parity with ai.client.call_tool_json) but unused:
Gemini's structured-output mode has no concept of a named tool.

Env:
  GEMINI_API_KEY   required — the provider credential (server-side only,
                   never sent to the frontend, never logged, never audited)
  SA_AI_PROVIDER   "gemini" (default) | "anthropic" — see ai_response.py
  SA_AI_MODEL      optional — model id override; provider-aware default
                   applied in ai_response.py when unset

Does NOT set its own request timeout: the caller (ai_response.generate_draft)
already wraps the single call in `asyncio.wait_for(..., timeout=
SA_AI_TIMEOUT_SEC)`, exactly as it does for Anthropic — one bound, not two.
No retry loop here beyond whatever the SDK does internally for transient
errors on its own (no application-level retry is added).

Compatibility fix (2026-09-15, live-validated against the real API):
  * `gemini-2.5-flash` returns a real HTTP 404 for this account ("no longer
    available to new users") despite appearing in `models.list()` — listing
    and per-account generateContent authorization are evidently separate
    checks. DEFAULT_MODEL was moved to `gemini-3.6-flash`, confirmed
    reachable AND (with the fix below) able to produce a valid grounded
    draft, via a live smoke test against the real API.
  * Root cause of the "LLM returned non-JSON output" failure, established
    from live `usage_metadata` (NOT assumed): every current Flash/Pro model
    in this account's catalog reports `thinking: true`, and with no explicit
    `thinking_config` Gemini defaults to an automatic thinking budget that
    can consume some/all of `max_output_tokens` on hidden reasoning before
    any visible answer text — leaving a truncated or empty JSON string.
    Confirmed directly: the identical request with `thinking_config=
    ThinkingConfig(thinking_budget=0)` (a documented SDK setting — the
    field's own description states "0 is DISABLED") produced
    `thoughts_token_count=None`, `finish_reason=STOP`, and a complete,
    valid, schema-conformant, correctly-grounded JSON response. No markdown
    fencing was ever observed in any live response inspected — no fence-
    stripping was added; `json.loads` stays a strict parser.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

# Re-exported, NOT redefined — see module docstring. `ai_response.py` catches
# these by whatever name `ai_client` (its provider-selected module alias)
# exposes them under, so both provider modules must be the SAME classes.
from ai.client import LLMError, LLMUnavailable  # noqa: F401

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.6-flash"


def is_configured() -> bool:
    """True when a call would at least be attempted (key present)."""
    return bool(os.environ.get("GEMINI_API_KEY"))


def _client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    try:
        from google import genai  # lazy — keeps module import light for tests/tools
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("the 'google-genai' package is not installed") from exc
    return genai.Client(api_key=api_key)


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
    """Single-shot structured generation via Gemini's native JSON-schema mode.

    `tool_name` / `tool_description` are accepted for signature parity with
    ai.client.call_tool_json (Anthropic's tool-calling contract) but are not
    used — Gemini's response_json_schema mode has no "tool" concept; the
    schema alone constrains the output shape.
    """
    client = _client()
    mdl = model or DEFAULT_MODEL
    try:
        from google.genai import errors, types
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("the 'google-genai' package is not installed") from exc

    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        response_json_schema=input_schema,
        # Live-established fix (see module docstring): without this, Gemini's
        # automatic thinking budget can consume max_output_tokens before any
        # visible JSON is written, producing a truncated/empty response. "0"
        # is the SDK's own documented value for "disabled". A model that
        # rejects thinking_budget=0 (none observed so far) still fails
        # cleanly via the existing ClientError/ServerError handling below.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    try:
        resp = await client.aio.models.generate_content(model=mdl, contents=user, config=config)
    except errors.ClientError as exc:
        # 401/403 → operator/config problem (bad or missing key), matching
        # ai.client's own 401/403 → LLMUnavailable split. Never includes the
        # key itself — `.message` is the provider's own error body text.
        if getattr(exc, "code", None) in (401, 403):
            raise LLMUnavailable(f"LLM auth failed ({exc.code})") from exc
        detail = (getattr(exc, "message", None) or str(exc))[:300]
        logger.warning("Gemini call failed (%s): %s", getattr(exc, "code", "?"), detail)
        raise LLMError(f"LLM call failed ({getattr(exc, 'code', '?')}): {detail}".rstrip(": ")) from exc
    except errors.ServerError as exc:
        detail = (getattr(exc, "message", None) or str(exc))[:300]
        raise LLMError(f"LLM server error ({getattr(exc, 'code', '?')}): {detail}".rstrip(": ")) from exc
    except errors.APIError as exc:  # pragma: no cover — any other SDK-raised API error
        raise LLMError(f"LLM error: {exc}") from exc

    # Safe (no prompt/response content, no credentials) diagnostic metadata —
    # helps distinguish "hit the output-token ceiling" from other failure
    # shapes without ever logging/storing the raw text itself.
    candidates = getattr(resp, "candidates", None) or []
    finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None

    text = resp.text
    if not text:
        # Blocked / empty response (safety filter, no candidates, etc.) — a
        # clean failure, never a silent empty draft.
        feedback = getattr(resp, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None) if feedback else None
        raise LLMError(
            f"Gemini returned no usable content (block_reason={block_reason}, "
            f"finish_reason={finish_reason})"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned non-JSON output (finish_reason={finish_reason})") from exc
    if not isinstance(data, dict):
        raise LLMError(f"LLM output was not a JSON object (finish_reason={finish_reason})")
    return data
