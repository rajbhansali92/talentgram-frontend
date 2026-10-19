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

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import time

from agents import name_match
from agents.modules import casting_pipeline_nlu as nlu
from core import active_only, db
from personal_assistant import phonetic
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
    """Candidates for a typed name, narrowed in MongoDB so the whole roster is never loaded. A spelling mistake can hit
    the start of a word ("Vidushi" vs "Viidushi"), so each word contributes BOTH its first and its last letters."""
    words = [w for w in re.findall(r"[A-Za-z]{2,}", phrase or "")][:4]
    if not words:
        return []
    ors = []
    for w in words:
        ors.append({"name": {"$regex": re.escape(w[: max(2, min(3, len(w)))]), "$options": "i"}})
        if len(w) >= 5:
            ors.append({"name": {"$regex": re.escape(w[-3:]), "$options": "i"}})
    query = active_only({"$or": ors}, exclude_archived=True)
    query["status"] = {"$nin": ["ARCHIVED", "merged", "MERGED", "DRAFT"]}
    docs = await db.talents.find(query, {"_id": 0, "id": 1, "name": 1}).limit(_CANDIDATE_CAP).to_list(_CANDIDATE_CAP)
    return [nlu.Candidate(id=d["id"], label=d.get("name") or "") for d in docs if d.get("name")]


# How a typed name is judged against a stored name. EVERY word the user typed must be accounted for (a stored name that
# merely shares one word — another "Tiwari" — is never offered), and a candidate is classed by how it matched:
#   exact    the same name, ignoring case / spacing / punctuation
#   partial  every typed word starts a stored word, or the typed text sits inside the name ("Angela" → "Angela Kumar")
#   close    only reachable through a spelling correction ("Vidushi" → "Viidushi") — NEVER chosen without a tap
_WORD_MIN = 0.75
_MATCH_RANK = {"exact": 0, "partial": 1, "prefix": 2, "close": 2, "sounds_like": 2}


def _word_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) <= 2 or len(b) <= 2:
        return 0.0                                  # a 1-2 letter "match" is noise, not evidence
    if len(a) >= 3 and b.startswith(a):
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def _word_sim_ext(a: str, b: str) -> Tuple[float, bool]:
    """→ (similarity, reached_only_by_sound). Spelling similarity first; if the words merely SOUND alike (speech-recognition
    style errors) the phonetic score counts too, but is capped below an exact / prefix match and flagged so the caller can label it."""
    s = _word_sim(a, b)
    if s >= _WORD_MIN:
        return s, False
    p = phonetic.similarity(a, b)
    if p >= phonetic.PHON_MIN:
        return min(p, 0.92), True
    return s, False


def score_name(query: str, label: str) -> Optional[Tuple[str, float]]:
    """→ (kind, score) when `label` is a plausible match for `query`, else None. Pure (no I/O).
    kind: exact | partial (typed words start stored words) | close (needs a spelling correction) | sounds_like (needs a phonetic one)."""
    q, l = nlu._normalize_name(query), nlu._normalize_name(label)
    qt, lt = q.split(), l.split()
    if not qt or not lt:
        return None
    if q == l:
        return "exact", 1.0
    per, by_sound = [], False
    for t in qt:
        best, snd = 0.0, False
        for x in lt:
            v, vs = _word_sim_ext(t, x)
            if v > best:
                best, snd = v, vs
        per.append(best)
        by_sound = by_sound or snd
    if min(per) < _WORD_MIN:
        return None                                  # at least one typed word matches nothing in this name
    cover = sum(per) / len(per)
    whole = difflib.SequenceMatcher(None, q, l).ratio()
    extra = max(0, len(lt) - len(qt))
    # "partial" (may be chosen without a tap) needs every typed word to BE a whole stored word ("Angela" → "Angela Kumar"). A typed word that
    # is only the START of a longer one ("Sahi" → "Sahil", "Disha" → "Dishant") could just as well be a different person: that is a card.
    whole_words = all(t in lt for t in qt)
    prefix = q in l or all(any(x.startswith(t) for x in lt) for t in qt)
    kind = "partial" if whole_words else ("prefix" if prefix else ("sounds_like" if by_sound else "close"))
    return kind, round(cover - 0.03 * extra + 0.1 * whole, 4)


def rank_talents(query: str, candidates: List[nlu.Candidate]) -> List[Tuple[nlu.Candidate, str, float]]:
    scored = []
    for c in candidates:
        r = score_name(query, c.label)
        if r:
            scored.append((c, r[0], r[1]))
    scored.sort(key=lambda x: (_MATCH_RANK[x[1]], -x[2], x[0].label.lower()))
    return scored


# ---------------------------------------------------------------------------------------------- roster name index
# Speech errors often change the START of a name ("Disha" → "Doha"), which the old prefix-narrowed database query could never
# reach. So a small in-memory index of live talents' ID + NAME ONLY (nothing else: no phone, e-mail, profile fields) is kept for
# recall. It is rebuilt at most once a minute, lives only in this process, is only read by routes that already require an
# allowed assistant user, is never returned to the browser as a list and never logged. It is used ONLY to find candidates; the
# decision always re-reads the live records (see resolve_talent), so a stale entry can never become a chosen person.
_ROSTER_TTL = 60.0
_roster: Dict[str, Any] = {"at": 0.0, "names": [], "index": {}}
_roster_lock: Optional[asyncio.Lock] = None
_BLOCKED = ("ARCHIVED", "MERGED", "DRAFT")


def invalidate_roster() -> None:
    _roster["at"] = 0.0


async def _load_roster() -> Dict[str, Any]:
    global _roster_lock
    if _roster_lock is None:
        _roster_lock = asyncio.Lock()
    if time.monotonic() - _roster["at"] < _ROSTER_TTL and _roster["names"]:
        return _roster
    async with _roster_lock:
        if time.monotonic() - _roster["at"] < _ROSTER_TTL and _roster["names"]:
            return _roster
        query = active_only({}, exclude_archived=True)
        query["status"] = {"$nin": ["ARCHIVED", "merged", "MERGED", "DRAFT"]}
        docs = await db.talents.find(query, {"_id": 0, "id": 1, "name": 1}).to_list(20000)
        names = [nlu.Candidate(id=d["id"], label=d["name"]) for d in docs if d.get("id") and d.get("name")]
        index: Dict[str, List[int]] = {}
        for i, c in enumerate(names):
            for tok in set(nlu._normalize_name(c.label).split()):
                index.setdefault(tok, []).append(i)
        _roster.update({"at": time.monotonic(), "names": names, "index": index})
        return _roster


async def _roster_candidates(phrase: str, cap: int = 60) -> List[nlu.Candidate]:
    """Names in which EVERY typed word is accounted for by spelling or sound — found through the token index, in memory, once per
    phrase (nothing is queried per word)."""
    qt = nlu._normalize_name(phrase).split()[:4]
    if not qt:
        return []
    r = await _load_roster()
    sets: List[set] = []
    for t in qt:
        hit: set = set()
        for tok, idxs in r["index"].items():
            if _word_sim_ext(t, tok)[0] >= _WORD_MIN:
                hit.update(idxs)
        if not hit:
            return []
        sets.append(hit)
    common = set.intersection(*sets) if sets else set()
    return [r["names"][i] for i in sorted(common)[:cap]]


async def _live_talents(ids: List[str]) -> Dict[str, dict]:
    """The LIVE records behind candidate ids (one query): drops anything archived / merged / deleted since the index was built."""
    if not ids:
        return {}
    docs = await db.talents.find(active_only({"id": {"$in": ids}}, exclude_archived=True), {"_id": 0, "id": 1, "name": 1, "status": 1}).to_list(len(ids))
    return {d["id"]: d for d in docs if d.get("name") and str(d.get("status") or "").upper() not in _BLOCKED}


async def _pipeline_context(ids: List[str], project_ids: List[str]) -> Dict[str, dict]:
    """Which of these candidates are already in the named project(s)' pipeline → {talent_id: {"project", "stage"}} (one query)."""
    if not ids or not project_ids:
        return {}
    rows = await db.casting_pipeline.find({"project_id": {"$in": project_ids}, "talent_id": {"$in": ids}}, {"_id": 0, "talent_id": 1, "project_id": 1, "stage": 1}).to_list(500)
    if not rows:
        return {}
    names = {d["id"]: d.get("brand_name") or "" for d in await db.projects.find({"id": {"$in": project_ids}}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(len(project_ids))}
    return {r["talent_id"]: {"project": names.get(r["project_id"]) or "", "stage": str(r.get("stage") or "").replace("_", " ").title()} for r in rows}


async def resolve_talent(phrase: str, project_ids: Optional[List[str]] = None) -> dict:
    """→ {"state": "resolved", "id", "label"} | {"state": "ambiguous", "options": [...]} | {"state": "none"}

    Chosen automatically ONLY for an exact name, or a partial name that exactly one talent fits ("Angela"). Anything that
    needed a spelling or sound correction — or fits several people — comes back as real talent cards to tap (best match first and
    flagged), so a misheard or mistyped name can never silently become the wrong person. `project_ids` (the project(s) named in the
    same clause) is CONTEXT only: it orders and annotates the cards ("already in <project>'s pipeline") and never selects anyone."""
    phrase = (phrase or "").strip()
    if not phrase or phrase.isdigit():
        return {"state": "none"}
    direct = await _talent_pool(phrase)                        # fresh database recall (new / renamed talents)
    seen = {c.id for c in direct}
    pool = direct + [c for c in await _roster_candidates(phrase) if c.id not in seen]
    ranked = rank_talents(phrase, pool)
    if not ranked:
        return {"state": "none"}
    live = await _live_talents([c.id for c, _k, _s in ranked[: MAX_OPTIONS * 3]])
    ranked = [(c, k, sc) for c, k, sc in ranked[: MAX_OPTIONS * 3] if c.id in live and live[c.id]["name"] == c.label] + [r for r in ranked[MAX_OPTIONS * 3:] if r[0].id in live]
    if not ranked:
        return {"state": "none"}
    exact = [r for r in ranked if r[1] == "exact"]
    partial = [r for r in ranked if r[1] == "partial"]
    if len(exact) == 1:
        return {"state": "resolved", "id": exact[0][0].id, "label": exact[0][0].label}
    if not exact and len(partial) == 1:
        return {"state": "resolved", "id": partial[0][0].id, "label": partial[0][0].label}
    shown = (exact or partial or ranked)[:MAX_OPTIONS]      # duplicate names → those; else partial fits; else spelling / sound guesses
    if not (exact or partial):
        shown = shown[:5]                                    # guesses only: a short list, not a wall of lookalikes
    ids = [c.id for c, _k, _s in shown]
    ctx = await _pipeline_context(ids, project_ids or [])
    if ctx:                                                  # people already in the named project's pipeline first (stable within a kind)
        shown = sorted(shown, key=lambda r: (_MATCH_RANK[r[1]], 0 if r[0].id in ctx else 1, -r[2]))
        ids = [c.id for c, _k, _s in shown]
    cards = await talent_cards(ids)
    kinds = {c.id: (k, sc) for c, k, sc in shown}
    options = []
    for i, tid in enumerate(ids):
        if tid not in cards:
            continue
        opt = dict(cards[tid])
        kind, score = kinds[tid]
        opt["match"] = kind
        if tid in ctx:
            where = f"In {ctx[tid]['project']}'s pipeline" + (f" · {ctx[tid]['stage']}" if ctx[tid]["stage"] else "")
            opt["sublabel"] = " · ".join(x for x in [where, opt.get("sublabel")] if x)
            opt["in_project"] = True
        lead = score - kinds[ids[1]][1] if len(ids) > 1 and ids[1] in kinds else 1.0
        if i == 0 and kind in ("partial", "prefix", "close") and score >= 0.85 and (len(ids) == 1 or lead >= 0.05):
            opt["best"] = True                       # proposed, never assumed: the card still has to be tapped; two near-equals → none is flagged
        elif kind == "exact" and tid in ctx and sum(1 for x in ids if x in ctx) == 1:
            opt["best"] = True                       # two talents with the SAME name: the one already in this project's pipeline is proposed
        # a "sounds like" card is never flagged: nothing but the sound links it to what was said
        options.append(opt)
    return {"state": "ambiguous", "options": options} if options else {"state": "none"}


# --------------------------------------------------------------------------------------------------- projects
async def load_projects() -> List[dict]:
    docs = await db.projects.find(active_only({"status": {"$ne": "deleted"}}), {"_id": 0, "id": 1, "brand_name": 1, "status": 1, "character": 1}).to_list(3000)
    return [{"id": d["id"], "label": d.get("brand_name") or "", "status": d.get("status") or "ongoing", "character": d.get("character") or ""} for d in docs if d.get("brand_name")]


def _opt(p: dict, best: bool = False) -> dict:
    o = {"id": p["id"], "label": p["label"], "sublabel": " · ".join(x for x in [p.get("status", "").title(), p.get("character") or ""] if x),
         "thumb": None, "status": p.get("status") or "ongoing"}
    if best:
        o["best"] = True
    return o


_NUMWORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _pn(text: str) -> str:
    """Project text as compared: the usual normalisation plus spoken numbers as digits on BOTH sides ("Film Two" = "Film 2"; speech engines
    write numbers either way). "One" in "Embassy One" becomes 1 on the stored name too, so exact names still match exactly."""
    n = nlu._normalize_project_label(text)
    return " ".join(_NUMWORDS.get(w, w) for w in n.split())


def _strong(span: str, label: str) -> bool:
    n, l = _pn(span), _pn(label)
    if not n:
        return False
    if n in l:
        return True
    if set(n.split()) <= set(l.split()):
        return True
    cn = n.replace(" ", "")
    return len(cn) >= 5 and cn in l.replace(" ", "")       # speech splits / joins words differently: "Toba Made" ≈ "Tobamade"


def _strong_matches(span: str, projects: List[dict]) -> List[dict]:
    return [p for p in projects if _strong(span, p["label"])]


def _exact_matches(span: str, projects: List[dict]) -> List[dict]:
    n = _pn(span)
    return [p for p in projects if n and _pn(p["label"]) == n]


def _sound_matches(span: str, projects: List[dict]) -> List[dict]:
    """Projects in which EVERY spoken word is accounted for by spelling or sound, and at least one only by sound ("Cebamed" ~ "Sebamed").
    Offered as cards — a sound-alike project is never chosen, and never supplies a WhatsApp group by itself."""
    words = [w for w in _pn(span).split() if len(w) >= 3 or w.isdigit()]
    if not words:
        return []
    out = []
    for p in projects:
        lt = _pn(p["label"]).split()
        by_sound, ok = False, True
        for w in words:
            best, snd = 0.0, False
            for x in lt:
                v, vs = _word_sim_ext(w, x)
                if w == x:
                    v, vs = 1.0, False
                if v > best:
                    best, snd = v, vs
            if best < _WORD_MIN:
                ok = False
                break
            by_sound = by_sound or snd
        if ok and by_sound:
            out.append(p)
    return _ordered(out)[:5]


_LIVE_PROJECT = ("ongoing", "upcoming")        # statuses a casting call / add can really target ("upcoming" is not used in the data today)


def _pick_by_status(matches: List[dict], prefer_active: bool) -> List[dict]:
    """Several PRECISE fits and exactly one of them is ongoing → that one (a closed project can't take adds, and a casting
    call is meant for the live job). Only ever applied to precise word matches, never to a spelling correction."""
    if prefer_active and len(matches) > 1:
        active = [m for m in matches if m["status"] == "ongoing"]
        if len(active) == 1:
            return active
    return matches


def _ordered(matches: List[dict]) -> List[dict]:
    """Ongoing projects first (they are the ones a casting call / add can actually target), then by name."""
    return sorted(matches, key=lambda m: (0 if m.get("status") == "ongoing" else 1, (m.get("label") or "").lower()))


def _options(matches: List[dict]) -> List[dict]:
    """Cards for several candidate projects. The single ongoing one is PROPOSED (flagged), never chosen for the user — two
    similarly named projects can belong to different casting groups."""
    ordered = _ordered(matches)[:MAX_OPTIONS]
    ongoing = [m for m in ordered if m.get("status") == "ongoing"]
    return [_opt(m, best=bool(ongoing) and len(ongoing) == 1 and m is ongoing[0]) for m in ordered]


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
    """→ one entry per project mentioned: {"phrase", "state": resolved|ambiguous|none, "id"?, "label"?, "options"?}

    A project is chosen automatically only when the words typed ARE that project's name (same words, any case / hyphens),
    or are a precise part of exactly one name. Several fits — or anything that needed a spelling correction — come back
    as cards to tap, so a message can never go to the wrong casting group because a name was spoken a little off.
    (`prefer_active`: when exactly one precise fit is ongoing it wins over closed ones; ongoing projects are listed first.)"""
    projects = projects if projects is not None else await load_projects()
    text = (text or "").strip()
    spans = _pieces(text)
    out: List[dict] = []
    i = 0
    while i < len(spans):
        taken = False
        for j in range(len(spans), i, -1):                  # longest run of pieces first
            span = text[spans[i][0]: spans[j - 1][1]]
            exact = _exact_matches(span, projects)
            if len(exact) == 1 and exact[0].get("status") not in _LIVE_PROJECT:
                # the words ARE a closed project's name, but a live project's name also contains them ("… Film" vs "… Film 2"):
                # a completed project is never chosen silently when a live one is a plausible intent — show both, live first
                live = [m for m in _strong_matches(span, projects) if m.get("status") in _LIVE_PROJECT]
                if live:
                    out.append({"phrase": span, "state": "ambiguous", "options": _options(exact + live)})
                    i, taken = j, True
                    break
            strong = exact or _pick_by_status(_strong_matches(span, projects), prefer_active)
            if len(exact) == 1 or (not exact and len(strong) == 1):
                out.append({"phrase": span, "state": "resolved", "id": strong[0]["id"], "label": strong[0]["label"]})
                i, taken = j, True
                break
            if len(strong) > 1:
                out.append({"phrase": span, "state": "ambiguous", "options": _options(strong)})
                i, taken = j, True
                break
        if taken:
            continue
        span = text[spans[i][0]: spans[i][1]]                # nothing precise: typo-tolerant match for this one piece — always a card to tap
        labels = [{"id": p["id"], "label": p["label"]} for p in projects]
        m = nlu.resolve_project_by_name(span, labels)
        by_id = {p["id"]: p for p in projects}
        snd = _sound_matches(span, projects)
        if m.project and m.project["id"] in by_id:
            try:
                precise = {projects[k]["id"] for k in nlu._project_token_subset_matches(span, [p["label"] for p in projects])}
            except Exception:
                precise = set()
            if m.project["id"] in precise:
                out.append({"phrase": span, "state": "resolved", "id": m.project["id"], "label": m.project["label"]})
            else:
                rest = [by_id[c["id"]] for c in (m.suggestions or []) if c["id"] in by_id and c["id"] != m.project["id"]]
                opts = [_opt(by_id[m.project["id"]], best=True)] + [_opt(r) for r in rest[: MAX_OPTIONS - 1]]
                opts += [_opt(p) for p in snd if p["id"] not in {o["id"] for o in opts}][: MAX_OPTIONS - len(opts)]
                out.append({"phrase": span, "state": "ambiguous", "options": opts})
        elif m.ambiguous or m.suggestions:
            opts = [_opt(by_id[c["id"]]) for c in (m.ambiguous or m.suggestions) if c["id"] in by_id][:MAX_OPTIONS]
            opts += [_opt(p) for p in snd if p["id"] not in {o["id"] for o in opts}][: MAX_OPTIONS - len(opts)]
            out.append({"phrase": span, "state": "ambiguous" if opts else "none", "options": opts})
        elif snd:
            out.append({"phrase": span, "state": "ambiguous", "options": [_opt(p) for p in snd]})
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
    if re.sub(r"[^a-z ]", "", phrase.lower()).strip() in ("casting", "casting call", "casting calls", "the casting", "the casting call"):
        cc = next((t for t in usable if t.get("slug") == "casting_call"), None)        # "send the casting" can only mean the Casting Call
        if cc is not None:
            return {"state": "resolved", "id": cc["id"], "label": cc["name"]}
    m = name_match.tiered_name_match(phrase, usable, lambda t: t.get("name") or "", id_fn=lambda t: t["id"], what="template")
    if m.item is not None:
        return {"state": "resolved", "id": m.item["id"], "label": m.item["name"]}
    if m.ambiguous:
        return {"state": "ambiguous", "options": [{"id": a["id"], "label": a["label"], "sublabel": "", "thumb": None} for a in m.ambiguous]}
    return {"state": "none", "options": template_options(usable)}


def template_options(templates: List[dict], limit: int = 12) -> List[dict]:
    ordered = sorted(templates, key=lambda t: (0 if t.get("slug") == "casting_call" else 1, (t.get("name") or "").lower()))
    return [{"id": t["id"], "label": t.get("name") or "Untitled", "sublabel": "", "thumb": None} for t in ordered[:limit]]


# ------------------------------------------------------------------------------------------------ recipients
def mask_phone(phone: str) -> str:
    """"+919876543210" → "+91 ••••• 43210" — enough to recognise a number, not enough to copy it."""
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) < 6:
        return "no number on file"
    return ("+" if (phone or "").startswith("+") else "") + digits[:-5][:2] + " ••••• " + digits[-5:] if len(digits) > 7 else "••••" + digits[-4:]


def _contact_option(c: dict, extra: str = "") -> dict:
    kind = (c.get("contact_type") or "").replace("_", " ").title()
    sub = " · ".join(x for x in [kind, c.get("company") or "", mask_phone(c.get("phone") or "")] if x)
    return {"id": f"crm:{c['id']}", "label": c["name"], "sublabel": sub, "thumb": None}


async def _crm_contacts(contact_type: Optional[str] = None) -> List[dict]:
    q: Dict[str, Any] = {"archived": {"$ne": True}, "deleted": {"$ne": True}}
    if contact_type:
        q["contact_type"] = contact_type
    docs = await db.clients.find(q, {"name": 1, "phone_number": 1, "contact_type": 1, "company_name": 1}).to_list(3000)
    return [{"id": str(d["_id"]), "name": d.get("name") or "", "phone": d.get("phone_number") or "", "contact_type": d.get("contact_type"),
             "company": d.get("company_name") or ""} for d in docs if (d.get("name") or "").strip()]


async def resolve_recipient(rec: dict) -> dict:
    """Existing WhatsApp recipient sources ONLY — a typed number, a saved CRM contact, a saved contact list, a saved
    group list (the same sources the WhatsApp agent's SHARE command resolves from). A role ("the casting director")
    is never guessed: it lists the CRM contacts of that type to pick from. → {"state", "kind", "id", "label", "display", "options"}"""
    kind = (rec or {}).get("kind")
    if kind == "number":
        phone = rec.get("value") or ""
        return {"state": "resolved", "kind": "number", "label": mask_phone(phone), "display": f"WhatsApp number {mask_phone(phone)}", "phone": phone}
    if kind == "project_group":
        return {"state": "resolved", "kind": "project_group", "label": "project group", "display": "the project's WhatsApp casting group"}
    if kind == "role":
        contacts = await _crm_contacts(rec["value"])
        title = (rec.get("text") or rec["value"].replace("_", " ")).title()
        if not contacts:
            return {"state": "none", "kind": "role", "prompt": f"I have no saved {title} contacts. Give me a WhatsApp number, or save them in the CRM first.", "options": []}
        return {"state": "ambiguous", "kind": "role", "prompt": f"Which {title}? (saved CRM contacts)", "options": [_contact_option(c) for c in contacts[:MAX_OPTIONS]]}
    if kind == "named":
        q = (rec.get("value") or "").strip()
        from agents.modules.whatsapp_campaign_agent import _fetch_contact_lists, _fetch_group_lists
        contacts = await _crm_contacts()
        by_id = {c["id"]: c for c in contacts}
        # The OLD behaviour (contact → list → group, first category with an answer wins) is kept exactly. Projects (a project's saved
        # casting group) are added on top: a precise project hit that conflicts with another category, or a project-only name, is
        # never guessed — the user taps which one they meant. Spelling-corrected project guesses only appear when nothing else matched.
        orig: Optional[dict] = None
        cat_hits: List[dict] = []          # every category with a precise answer {"option","recipient"}
        m = name_match.tiered_name_match(q, contacts, lambda c: c["name"], id_fn=lambda c: c["id"], what="contact")
        if m.item is not None:
            c = m.item
            r = {"state": "resolved", "kind": "crm", "id": c["id"], "label": c["name"], "display": f"{c['name']} ({mask_phone(c['phone'])}) — saved CRM contact", "phone_present": bool(c["phone"])}
            orig = orig or r
            cat_hits.append({"option": _contact_option(c), "recipient": r})
        elif m.ambiguous:
            orig = orig or {"state": "ambiguous", "kind": "named", "prompt": f'Which contact did you mean by "{q}"?', "options": [_contact_option(by_id[a["id"]]) for a in m.ambiguous if a["id"] in by_id]}
        for fetch, key, label in ((_fetch_contact_lists, "list", "saved contact list"), (_fetch_group_lists, "group", "saved group list")):
            items = await fetch()
            lm = name_match.tiered_name_match(q, items, lambda it: it.get("name") or "", id_fn=lambda it: it["id"], what=label)
            if lm.item is not None:
                r = {"state": "resolved", "kind": key, "id": lm.item["id"], "label": lm.item["name"], "display": f"{lm.item['name']} — {label}"}
                orig = orig or r
                cat_hits.append({"option": {"id": f"{key}:{lm.item['id']}", "label": lm.item["name"], "sublabel": label.title(), "thumb": None}, "recipient": r})
            elif lm.ambiguous:
                orig = orig or {"state": "ambiguous", "kind": "named", "prompt": f'Which {label} did you mean?', "options": [{"id": f"{key}:{a['id']}", "label": a["label"], "sublabel": label.title(), "thumb": None} for a in lm.ambiguous]}
        proj_q = re.sub(r"(?i)^\s*(?:the\s+)?|\s*\b(?:whatsapp\s+|casting\s+)?(?:group|project)\s*$", "", q).strip() or q
        proj_hits: List[dict] = []
        proj_opts: List[dict] = []
        for e in await resolve_projects(proj_q, prefer_active=False):
            if e["state"] == "resolved":
                pr = await recipient_from_option(f"project:{e['id']}")
                if pr:
                    proj_hits.append({"option": _project_recipient_option(pr), "recipient": pr})
            elif e["state"] == "ambiguous":
                proj_opts += [{**o, "id": f"project:{o['id']}", "sublabel": " · ".join(x for x in ["Project · casting group", o.get("sublabel") or ""] if x)} for o in e.get("options") or []]
        if not proj_hits and (orig is not None):
            return orig                                           # no precise project hit: exactly the previous behaviour
        everything = cat_hits + proj_hits
        if len(everything) == 1 and not proj_opts:
            return everything[0]["recipient"]
        options = [h["option"] for h in everything] + (proj_opts if not everything else [])
        if not options and orig is None:
            return {"state": "none", "kind": "named", "prompt": f"I couldn't find a saved contact, list, group or project called “{q}”. Give me a WhatsApp number, or save them in the CRM first.", "options": []}
        if not options:
            return orig
        seen, uniq = set(), []
        for o in options:
            if o["id"] not in seen:
                seen.add(o["id"]); uniq.append(o)
        return {"state": "ambiguous", "kind": "named", "prompt": f'Which did you mean by "{q}"? A project\'s casting group, or a saved contact/list/group?' if len(everything) > 1 else f'Which project\'s casting group did you mean by "{q}"?', "options": uniq[:MAX_OPTIONS]}
    return {"state": "none", "kind": "unspecified", "prompt": "Who should I send it to? Give me a WhatsApp number, a saved contact's name, or say “the project group”.", "options": []}


def _project_recipient_option(pr: dict) -> dict:
    return {"id": f"project:{pr['project_id']}", "label": pr["label"], "sublabel": f"Project · {str(pr.get('project_status') or 'ongoing').title()} · casting group", "thumb": None}


async def recipient_from_option(option_id: str) -> Optional[dict]:
    """A picked option id ("crm:<id>" / "list:<id>" / "group:<id>") → the recipient entry (re-read from the database)."""
    from bson import ObjectId
    kind, _, rid = (option_id or "").partition(":")
    if kind == "crm":
        try:
            d = await db.clients.find_one({"_id": ObjectId(rid), "archived": {"$ne": True}, "deleted": {"$ne": True}}, {"name": 1, "phone_number": 1})
        except Exception:
            return None
        if not d:
            return None
        return {"state": "resolved", "kind": "crm", "id": rid, "label": d.get("name") or "", "display": f"{d.get('name')} ({mask_phone(d.get('phone_number') or '')}) — saved CRM contact", "phone_present": bool(d.get("phone_number"))}
    if kind == "project":
        d = await db.projects.find_one({"id": rid}, {"_id": 0, "id": 1, "brand_name": 1, "status": 1, "whatsapp_casting_group_name": 1})
        if not d or d.get("status") == "deleted" or not (d.get("brand_name") or "").strip():
            return None
        brand = d["brand_name"]
        return {"state": "resolved", "kind": "project_group", "project_id": d["id"], "label": brand, "project_status": d.get("status") or "ongoing",
                "display": f"the WhatsApp casting group of {brand}"}
    if kind in ("list", "group"):
        coll = db.whatsapp_contact_lists if kind == "list" else db.whatsapp_group_lists
        d = await coll.find_one({"id": rid, "deleted": {"$ne": True}}, {"_id": 0, "id": 1, "name": 1})
        if not d:
            return None
        label = "saved contact list" if kind == "list" else "saved group list"
        return {"state": "resolved", "kind": kind, "id": rid, "label": d.get("name") or "", "display": f"{d.get('name')} — {label}"}
    return None


# --------------------------------------------------------------------------------------- group destination checks
_GROUP_NOT_FOUND = "not found in chat list"


async def group_evidence(group: str) -> Dict[str, dict]:
    """What the job history says about each registered worker and ONE group chat (by name). There is no registry of which groups a
    worker's WhatsApp account belongs to — the worker finds the chat by name when it sends — so the only evidence is what already
    happened: a later successful send means it can reach it; a latest failure "not found in chat list" means it could not.
      → {worker_id: {"state": "verified" | "failed_before" | "unknown", "label", "when"}}"""
    out: Dict[str, dict] = {}
    group = (group or "").strip()
    for w in await db.whatsapp_workers.find({}, {"_id": 0, "id": 1, "label": 1}).to_list(50):
        wid = w["id"]
        wf = {"worker_id": wid} if wid != "default" else {"$or": [{"worker_id": "default"}, {"worker_id": None}, {"worker_id": {"$exists": False}}]}
        rows = await db.whatsapp_jobs.find({"destination_type": "group", "destination": group, **wf},
                                           {"_id": 0, "status": 1, "error_message": 1, "created_at": 1}).sort("created_at", -1).limit(15).to_list(15)
        state, when = "unknown", None
        for r in rows:
            if r.get("status") in ("sent", "sent_unverified"):
                state, when = "verified", r.get("created_at")
                break
            if r.get("status") == "failed" and _GROUP_NOT_FOUND in (r.get("error_message") or ""):
                state, when = "failed_before", r.get("created_at")
                break
        out[wid] = {"state": state, "label": w.get("label") or wid, "when": (when or "")[:10] or None}
    return out


async def group_collisions(group: str, project_id: Optional[str]) -> Dict[str, int]:
    """How many OTHER projects are configured with this same group chat (one chat is routinely reused by several projects). The
    destination chat is the same either way; it is shown so the user knows the message lands in a shared group."""
    group = (group or "").strip()
    if not group:
        return {"others": 0, "ongoing": 0}
    rows = await db.projects.find({"whatsapp_casting_group_name": group, "id": {"$ne": project_id}, "status": {"$ne": "deleted"}}, {"_id": 0, "status": 1}).to_list(200)
    return {"others": len(rows), "ongoing": sum(1 for r in rows if (r.get("status") or "ongoing") == "ongoing")}


# ---------------------------------------------------------------------------------------- link previews
async def peek_media_link(tid: str) -> dict:
    """What the talent's media link WOULD be, without writing anything (resolve_media_link mints a token on first
    use — a write — so previews use this and only the confirmed send mints)."""
    from routers.talent_media import is_media_download_enabled, media_link_url
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "id": 1, "status": 1, "media_download_enabled": 1, "media_download_token": 1})
    if t is None:          # NOT `if not t`: a real talent with none of the optional fields projects to {} (falsy)
        return {"state": "missing"}
    if not is_media_download_enabled(t) or str(t.get("status") or "").lower() in ("archived", "merged"):
        return {"state": "off"}
    if t.get("media_download_token"):
        return {"state": "ready", "url": media_link_url(t["media_download_token"])}
    return {"state": "will_create"}
