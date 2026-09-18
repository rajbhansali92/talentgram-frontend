"""Simple Assistant — isolated, additive module.

Phase 2 (read-only Command Centre). This package owns exactly one HTTP
surface: ``GET /api/simple-assistant/overview`` — a read-only aggregation
over data that ALREADY lives in Projects + Casting Pipeline + Submissions.

Design rules (see docs/SIMPLE_ASSISTANT_STEP1_AUDIT.md):
  - Additive only. Nothing in here mutates any collection or calls any
    write path. Every DB access is a ``find``/``aggregate``.
  - Reuses existing helpers rather than duplicating business logic:
      * ``routers.casting_pipeline.get_stage_counts`` / ``_normalise_stage``
        / ``PIPELINE_STAGE_ORDER`` — the canonical pipeline vocabulary.
      * ``core.enrich_talent`` / ``compute_age`` / ``video_poster_url`` /
        ``active_only`` — the same talent/media enrichment the rest of the
        app uses.
  - No new collection, no index, no migration, no background worker.
  - No LLM, no WhatsApp, no Cloudinary write.
  - Gated behind ``SIMPLE_ASSISTANT_ENABLED`` (default OFF; set to
    ``true``/``1``/``yes``/``on`` to expose the surface — any other value,
    including unset, 404s every route in the module).
  - Gated *again*, independently, behind ``SA_EXECUTION_ENABLED`` (default
    OFF) for the five functions that can mutate, send, or upload — see
    ``is_execution_enabled()`` below. ``SIMPLE_ASSISTANT_ENABLED=true`` with
    ``SA_EXECUTION_ENABLED`` left off exposes the read-only overview and
    command-preview surface while every confirm/upload attempt is blocked
    server-side, before any canonical mutating/sending/Cloudinary function
    is reached.

Entry point: ``simple_assistant.router.router`` — registered once in
``server.py`` exactly like every other router.
"""
from __future__ import annotations

import os

__all__ = ["is_enabled", "is_execution_enabled", "EXECUTION_DISABLED_MESSAGE"]

# Single source of truth for the "execution disabled" wording, so every one
# of the five gated functions returns an identical, recognisable message
# rather than five hand-copied strings that could drift.
EXECUTION_DISABLED_MESSAGE = (
    "Simple Assistant execution is currently disabled. "
    "This action was not performed — nothing was sent, uploaded, or changed."
)


def is_enabled() -> bool:
    """Feature flag — OFF by default. Simple Assistant is only exposed when
    ``SIMPLE_ASSISTANT_ENABLED`` is explicitly set to a truthy value
    (``true``/``1``/``yes``/``on``). Any other value (including unset) 404s
    every route in the module."""
    return os.environ.get("SIMPLE_ASSISTANT_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_execution_enabled() -> bool:
    """Independent execution kill-switch — OFF by default, separate from
    ``is_enabled()`` above.

    ``SIMPLE_ASSISTANT_ENABLED`` alone exposes the whole router, including
    the confirm/upload endpoints that reach a real mutation, WhatsApp send,
    or Cloudinary upload. It does NOT by itself make those endpoints safe —
    ``/overview`` and ``/command`` are structurally read-only (they only
    touch the database through ``readonly_db.RDB``, which raises on any
    write call), but ``/command/confirm`` and ``/command/upload`` dispatch
    straight into ``execute.confirm_and_execute``,
    ``comm_execute.confirm_and_send``,
    ``submission_execute.confirm_and_attach``/``confirm_and_ingest``, and
    ``ai_response_execute.confirm_and_send_ai`` — the five functions that
    are the ONLY paths in this module that call a canonical mutating,
    sending, or Cloudinary-uploading function.

    ``SA_EXECUTION_ENABLED`` gates exactly those five functions (checked as
    the first thing each one does, so it cannot be bypassed by calling them
    directly rather than through a route). Read-only preview/overview
    behavior is completely unaffected by this flag either way.

    Independent of, and unaffected by, ``SA_AI_RESPONSE_ENABLED`` (AI draft
    generation) and ``SA_INBOUND_CAPTURE_ENABLED`` (the separate
    worker-webhook inbound-capture hook) — neither of those flags is
    touched by this one."""
    return os.environ.get("SA_EXECUTION_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
