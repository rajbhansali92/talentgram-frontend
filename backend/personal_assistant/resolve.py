"""Personal Assistant — entity resolution against the real database (server-side, small queries only).

Reuses the existing casting-pipeline NLU matchers (`resolve_project_by_name`, `resolve_against_candidates`,
`match_stage_phrase`, the tiered template matcher) — nothing here re-implements name matching. The only
new logic is how the candidate pools are built so we never load the whole roster:

  talents   a name search narrowed in MongoDB by the first letters of each typed word (typo tolerant), capped,
            then ranked by the NLU's fuzzy tiers; ties come back as options with photo + identifying details
  projects  the (small) non-deleted project list, matched span by span so "Amazon Now - girl 1 and girl 2 and
            Pepsi" is read as two projects, not three
  templates the existing `whatsapp_templates` collection via the generic tiered matcher
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from agents import name_match
from agents.modules import casting_pipeline_nlu as nlu
from core import active_only, db
from routers.roster import _thumb_url, _roster_images

MAX_OPTIONS = 8
_CANDIDATE_CAP = 200


# ---------------------------------------------------------------------------------------------------- talents
def _thumb(t: dict) -> Optional[str]:
    """Small image for a card: the cover (or first roster image) as a sanctioned thumbnail; else the stored cover scalars."""
    media = _roster_images(t)
    flat = [m for cat in ("indian", "western", "portfolio") for m in media[cat]]
    pick = next((m for m in flat if m.get("id") == t.get("cover_media_id")), None) or (flat[0] if flat else None)
    if pick:
        try:
            return _thumb_url(pick)
        except Exception:
            return pick.get("url")
    return t.get("cover_thumbnail_url") or t.get("cover_url")


def _loc(t: dict) -> str:
    loc = t.get("location")
    if isinstance(loc, list):
        return "; ".join(f"{x.get('city') or ''}{', ' + x['country'] if x.get('country') else ''}".strip(", ") for x in loc[:2] if isinstance(x, dict))
    return str(loc or "")


async def talent_cards(ids: List[str]) -> Dict[str, dict]:
    """Display info for specific talents (batched, one query). Never the whole roster."""
    if not ids:
        return {}
    docs = await db.talents.find(
        {"id": {"$in": ids}},
        {"_id": 0, "id": 1, "name": 1, "age": 1, "height": 1, "location": 1, "gender": 1, "instagram_handle": 1,
         "cover_media_id": 1, "cover_url": 1, "cover_thumbnail_url": 1, "status": 1,
         "media": {"$slice": 12}},
    ).to_list(len(ids))
    out = {}
    for d in docs:
        sub = " · ".join(x for x in [f"{d['age']}y" if d.get("age") else "", d.get("height") or "", _loc(d),
                                     ("@" + d["instagram_handle"].lstrip("@")) if d.get("instagram_handle") else ""] if x)
        out[d["id"]] = {"id": d["id"], "label": d.get("name") or "Unnamed", "sublabel": sub, "thumb": _thumb(d),
                        "status": d.get("status")}
    return out


async def _talent_pool(phrase: str) -> List[nlu.Candidate]:
    words = [w for w in re.findall(r"[A-Za-z]{2,}", phrase or "")][:4]
    if not words:
        return []
    ors = [{"name": {"$regex": re.escape(w[: max(2, min(3, len(w)))]), "$options": "i"}} for w in words]
    query = active_only({"$or": ors}, exclude_archived=True)
    query["status"] = {"$nin": ["ARCHIVED", "merged", "MERGED", "DRAFT"]}
    docs = await db.talents.find(query, {"_id": 0, "id": 1, "name": 1}).limit(_CANDIDATE_CAP).to_list(_CANDIDATE_CAP)
    return [nlu.Candidate(id=d["id"], label=d.get("name") or "") for d in docs if d.get("name")]


async def resolve_talent(phrase: str) -> dict:
    """→ {"state": "resolved", "id", "label"} | {"state": "ambiguous", "options": [...]} | {"state": "none"}"""
    phrase = (phrase or "").strip()
    if not phrase or phrase.isdigit():
        return {"state": "none"}
    pool = await _talent_pool(phrase)
    if not pool:
        return {"state": "none"}
    res = nlu.resolve_against_candidates(nlu.SelectorResult(ok=True, name_query=phrase), pool)
    if res.ok and len(res.talent_ids) == 1:
        return {"state": "resolved", "id": res.talent_ids[0], "label": res.talent_labels[0]}
    if res.ambiguous_candidates:
        ids = [c.id for c in res.ambiguous_candidates][:MAX_OPTIONS]
        cards = await talent_cards(ids)
        return {"state": "ambiguous", "options": [cards[i] for i in ids if i in cards]}
    return {"state": "none"}


# --------------------------------------------------------------------------------------------------- projects
async def load_projects() -> List[dict]:
    docs = await db.projects.find(active_only({"status": {"$ne": "deleted"}}), {"_id": 0, "id": 1, "brand_name": 1, "status": 1, "character": 1}).to_list(3000)
    return [{"id": d["id"], "label": d.get("brand_name") or "", "status": d.get("status") or "ongoing", "character": d.get("character") or ""} for d in docs if d.get("brand_name")]


def _opt(p: dict) -> dict:
    return {"id": p["id"], "label": p["label"], "sublabel": " · ".join(x for x in [p.get("status", "").title(), p.get("character") or ""] if x), "thumb": None}


def _strong(span: str, label: str) -> bool:
    n, l = nlu._normalize_project_label(span), nlu._normalize_project_label(label)
    if not n:
        return False
    if n in l:
        return True
    return set(n.split()) <= set(l.split())


def _strong_matches(span: str, projects: List[dict]) -> List[dict]:
    return [p for p in projects if _strong(span, p["label"])]


def _pick_by_status(matches: List[dict], prefer_active: bool) -> List[dict]:
    if prefer_active and len(matches) > 1:
        active = [m for m in matches if m["status"] == "ongoing"]
        if len(active) == 1:
            return active
    return matches


def _pieces(text: str) -> List[Tuple[int, int]]:
    """Character spans of the text split on commas / '&' / 'and' (project names may themselves contain 'and')."""
    spans, start = [], 0
    for m in re.finditer(r"\s*(?:,|&|\band\b)\s*", text, flags=re.I):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


async def resolve_projects(text: str, *, prefer_active: bool = True, projects: Optional[List[dict]] = None) -> List[dict]:
    """→ one entry per project mentioned: {"phrase", "state": resolved|ambiguous|none, "id"?, "label"?, "options"?}"""
    projects = projects if projects is not None else await load_projects()
    text = (text or "").strip()
    spans = _pieces(text)
    out: List[dict] = []
    i = 0
    while i < len(spans):
        taken = False
        for j in range(len(spans), i, -1):                  # longest run of pieces first
            span = text[spans[i][0]: spans[j - 1][1]]
            strong = _pick_by_status(_strong_matches(span, projects), prefer_active)
            if len(strong) == 1 and (j - i > 1 or True):
                out.append({"phrase": span, "state": "resolved", "id": strong[0]["id"], "label": strong[0]["label"]})
                i, taken = j, True
                break
            if len(strong) > 1 and j - i >= 1:
                out.append({"phrase": span, "state": "ambiguous", "options": [_opt(p) for p in strong[:MAX_OPTIONS]]})
                i, taken = j, True
                break
        if taken:
            continue
        span = text[spans[i][0]: spans[i][1]]                # nothing strong: typo-tolerant NLU match for this one piece
        m = nlu.resolve_project_by_name(span, [{"id": p["id"], "label": p["label"]} for p in projects])
        by_id = {p["id"]: p for p in projects}
        if m.project:
            out.append({"phrase": span, "state": "resolved", "id": m.project["id"], "label": m.project["label"]})
        elif m.ambiguous or m.suggestions:
            opts = [_opt(by_id[c["id"]]) for c in (m.ambiguous or m.suggestions) if c["id"] in by_id][:MAX_OPTIONS]
            out.append({"phrase": span, "state": "ambiguous" if opts else "none", "options": opts})
        else:
            out.append({"phrase": span, "state": "none"})
        i += 1
    return out


async def recent_project_options(limit: int = MAX_OPTIONS) -> List[dict]:
    docs = await db.projects.find(active_only({"status": "ongoing"}), {"_id": 0, "id": 1, "brand_name": 1, "status": 1, "character": 1}).sort("created_at", -1).to_list(limit)
    return [_opt({"id": d["id"], "label": d.get("brand_name") or "", "status": d.get("status") or "ongoing", "character": d.get("character") or ""}) for d in docs]


# -------------------------------------------------------------------------------------------------- templates
async def load_templates() -> List[dict]:
    return await db.whatsapp_templates.find({}, {"_id": 0, "id": 1, "name": 1, "slug": 1}).to_list(200)


async def resolve_template(phrase: Optional[str], templates: Optional[List[dict]] = None) -> dict:
    templates = templates if templates is not None else await load_templates()
    usable = [t for t in templates if t.get("slug") != "custom"]          # the free-text template is the assistant's share channel, not a pick
    if not phrase:
        return {"state": "none", "options": template_options(usable)}
    m = name_match.tiered_name_match(phrase, usable, lambda t: t.get("name") or "", id_fn=lambda t: t["id"], what="template")
    if m.item is not None:
        return {"state": "resolved", "id": m.item["id"], "label": m.item["name"]}
    if m.ambiguous:
        return {"state": "ambiguous", "options": [{"id": a["id"], "label": a["label"], "sublabel": "", "thumb": None} for a in m.ambiguous]}
    return {"state": "none", "options": template_options(usable)}


def template_options(templates: List[dict], limit: int = 12) -> List[dict]:
    ordered = sorted(templates, key=lambda t: (0 if t.get("slug") == "casting_call" else 1, (t.get("name") or "").lower()))
    return [{"id": t["id"], "label": t.get("name") or "Untitled", "sublabel": "", "thumb": None} for t in ordered[:limit]]
