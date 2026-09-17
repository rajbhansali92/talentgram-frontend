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

Entry point: ``simple_assistant.router.router`` — registered once in
``server.py`` exactly like every other router.
"""
from __future__ import annotations

import os

__all__ = ["is_enabled"]


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
