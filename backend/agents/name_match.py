"""Generic tiered fuzzy name matcher — exact -> normalized-exact ->
substring -> character-level fuzzy. Extracted from
whatsapp_campaign_agent.py's original resolve_template_by_name (itself
already documented as "mirrors resolve_project_by_name's tier shape"), so
that shape now lives in exactly one place instead of being re-typed for
every new "match free text against a small named collection" need.

Deliberately NOT used for casting_pipeline_nlu.py's resolve_project_by_name
or talent matching — those have accumulated several project/talent-specific
hotfixes (filler-word stripping, plural folding, per-domain cutoffs) that
don't generalize cleanly, and refactoring already-hardened, heavily-tested
production matchers for the sake of DRY-ing this one shared shape isn't
worth the regression risk. This module is for NEW small-collection matchers
(templates, saved contact lists, saved group lists) that would otherwise
each hand-roll their own copy of the same four tiers.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = _PUNCT_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class GenericMatch:
    item: Optional[object] = None
    ambiguous: Optional[List[Dict[str, str]]] = None
    error: Optional[str] = None


def tiered_name_match(
    query: str,
    items: List[T],
    label_fn: Callable[[T], str],
    *,
    id_fn: Optional[Callable[[T], str]] = None,
    what: str = "match",
    fuzzy_cutoff: float = 0.6,
    autocorrect_cutoff: float = 0.8,
    ambiguity_margin: float = 0.05,
) -> GenericMatch:
    """Resolve `query` against `items` by label, escalating tiers only when
    the previous one found nothing unique. `label_fn` extracts the display
    label from one item; optional `id_fn` also attaches an "id" key to each
    `ambiguous` entry (omitted if not given). Returns the matched item
    itself (not a dict) on success, so callers can read whatever fields
    they need off it."""
    q_raw = (query or "").strip()
    if not q_raw or not items:
        return GenericMatch(error=f'I couldn\'t find a {what} matching "{query}".')

    labels = [label_fn(it) for it in items]
    q_lower = q_raw.lower()

    def _as_dicts(idxs: List[int]) -> List[Dict[str, str]]:
        if id_fn:
            return [{"id": id_fn(items[i]), "label": labels[i]} for i in idxs]
        return [{"label": labels[i]} for i in idxs]

    # Tier 1: exact, case-insensitive.
    exact = [i for i, l in enumerate(labels) if l.strip().lower() == q_lower]
    if len(exact) == 1:
        return GenericMatch(item=items[exact[0]])
    if len(exact) > 1:
        return GenericMatch(ambiguous=_as_dicts(exact[:8]))

    # Tier 2: normalized exact (case/punctuation/whitespace-insensitive).
    q_norm = normalize(q_raw)
    norm_labels = [normalize(l) for l in labels]
    nexact = [i for i, l in enumerate(norm_labels) if q_norm and l == q_norm]
    if len(nexact) == 1:
        return GenericMatch(item=items[nexact[0]])
    if len(nexact) > 1:
        return GenericMatch(ambiguous=_as_dicts(nexact[:8]))

    # Tier 3: normalized substring (either direction).
    contains = [i for i, l in enumerate(norm_labels) if q_norm and (q_norm in l or l in q_norm)]
    if len(contains) == 1:
        return GenericMatch(item=items[contains[0]])
    if len(contains) > 1:
        return GenericMatch(ambiguous=_as_dicts(contains[:8]))

    # Tier 4: character-level fuzzy, confident-and-unambiguous auto-resolve.
    scored = sorted(
        ((i, difflib.SequenceMatcher(None, q_norm, norm_labels[i]).ratio()) for i in range(len(labels))),
        key=lambda pair: pair[1], reverse=True,
    )
    scored = [(i, s) for i, s in scored if s >= fuzzy_cutoff]
    if not scored:
        return GenericMatch(error=f'I couldn\'t find a {what} matching "{query}".')
    top_i, top_s = scored[0]
    if len(scored) == 1:
        return GenericMatch(item=items[top_i])
    second_s = scored[1][1]
    if top_s >= autocorrect_cutoff and (top_s - second_s) > ambiguity_margin:
        return GenericMatch(item=items[top_i])
    close_enough = [i for i, s in scored if (top_s - s) <= ambiguity_margin][:8]
    return GenericMatch(ambiguous=_as_dicts(close_enough))
