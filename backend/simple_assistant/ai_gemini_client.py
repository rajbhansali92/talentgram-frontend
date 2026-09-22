"""Simple Assistant's Gemini provider — thin re-export shim.

Extraction (2026-09-22): the actual client (structured-output config,
thinking-budget fix, error handling — see its own module docstring for the
full history) now lives at `ai/gemini_client.py`, a genuinely shared,
provider-neutral module sibling to `ai/client.py` (the pre-existing shared
Anthropic adapter), because a second feature (the casting-pipeline Gemini
Command Interpreter, agents/modules/casting_command_interpreter.py) needed
the identical capability and importing it under this module's old
"Simple-Assistant-only" name would have been confusing cross-feature
coupling.

This module is kept, unchanged in its public surface, purely so every
existing Simple Assistant call site (`simple_assistant/ai_response.py`'s
provider-selection import) keeps working byte-for-byte with zero edits:

    is_configured() -> bool
    async def call_tool_json(*, system, user, tool_name, tool_description,
                              input_schema, max_tokens=4000, model=None) -> dict
    DEFAULT_MODEL
    LLMError, LLMUnavailable
"""
from __future__ import annotations

from ai.gemini_client import (  # noqa: F401
    DEFAULT_MODEL,
    LLMError,
    LLMUnavailable,
    call_tool_json,
    is_configured,
)
