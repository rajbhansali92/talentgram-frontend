"""Roster / Comp-card links — a fourth Generated Link type ("Roster / PDF").

This extends the EXISTING Generated Links system, it is not a parallel one:
a roster is an ordinary `db.links` document with `link_type: "roster"` plus a
`roster` sub-document, so link history, views / unique viewers (the existing
`identify` flow), the public/private toggle, copy, delete, duplicate and
bulk-delete all keep working untouched.

  roster = {
    subtitle,
    talents: [{talent_id, media_ids: [ordered selected media ids], dims: {media_id: [w, h]}}],
  }

Images are never copied: the roster stores talent id + media id + order (+ the
pixel size of each selected image, probed once at save time so the layout
engine can respect aspect ratios). At render time each reference is resolved
against the live talent profile; a media item that was deleted afterwards is
skipped, never fatal. The intro video is always the talent's CURRENT intro
video. Names are shown "First L" via talent_media.short_talent_name (the one
canonical formatter).

Web roster and PDF are both driven by the same layout plan
(services/roster_layout.py) so they match.
"""
import asyncio
import io
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

from core import (
    DEFAULT_VISIBILITY,
    LinkIn,
    LinkOut,
    _now,
    _slugify,
    current_admin,
    current_team_or_admin,
    db,
    decode_token,
    decode_viewer,
    enrich_talent,
    normalize_instagram_handle,
)
from routers.links import _extract_stream_uid, _format_location, _require_active_link
from routers.talent_media import short_talent_name
from services import roster_fields as F
from services import roster_layout as L
from services import roster_pdf

logger = logging.getLogger("talentgram")
router = APIRouter(prefix="/api", tags=["roster"])

ROSTER_CATEGORIES = ("indian", "western", "portfolio")
CATEGORY_LABELS = {"indian": "Indian Look Images", "western": "Western Look Images", "portfolio": "Additional Portfolio"}
MAX_TALENTS = 60
MAX_IMAGES_PER_TALENT = 12
MAX_TOTAL_IMAGES = 400
DEFAULT_PER_CATEGORY = 2
LINKS_BASE_URL = os.environ.get("TALENT_MEDIA_BASE_URL", "https://links.talentgramagency.com").rstrip("/")
_BLOCKED = {"archived", "merged"}


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------
def _visible(m: dict) -> bool:
    return bool(m.get("url")) and m.get("client_visible") is not False and m.get("internal_only") is not True


def _roster_images(talent: dict) -> Dict[str, List[dict]]:
    media = [m for m in (talent.get("media") or []) if _visible(m)]
    return {c: [m for m in media if m.get("category") == c] for c in ROSTER_CATEGORIES}


def _intro_video(talent: dict) -> Optional[dict]:
    for m in talent.get("media") or []:
        if _visible(m) and m.get("category") == "video":
            return m
    return None


def video_watch_url(m: dict) -> str:
    """A URL a person can actually open to watch the video: Cloudflare Stream
    HLS manifests (not playable in a desktop browser tab) map to the Stream
    watch page; anything else is the stored URL, unchanged."""
    url = m.get("url") or ""
    uid = _extract_stream_uid(url) if "cloudflarestream.com" in url else None
    if uid:
        host = url.split("/" + uid, 1)[0]
        return f"{host}/{uid}/watch"
    return url


def default_selection(talent: dict) -> List[str]:
    """Curated starting point for a comp card: the cover image first (if it is
    one of the roster categories), then up to DEFAULT_PER_CATEGORY per category
    (Indian, Western, Portfolio) in profile order. The admin has full control."""
    groups = _roster_images(talent)
    chosen: List[str] = []
    cover = talent.get("cover_media_id")
    for cat in ROSTER_CATEGORIES:
        for m in groups[cat]:
            if m.get("id") == cover:
                chosen.append(m["id"])
    for cat in ROSTER_CATEGORIES:
        taken = sum(1 for m in groups[cat] if m["id"] in chosen)
        for m in groups[cat]:
            if taken >= DEFAULT_PER_CATEGORY:
                break
            if m["id"] not in chosen:
                chosen.append(m["id"])
                taken += 1
    return chosen


async def probe_image_dims(client: httpx.AsyncClient, url: str) -> Optional[Tuple[int, int]]:
    """Display size (EXIF-orientation aware) of an image from just its first
    bytes — no full download. Tries a small range first (enough for almost every
    JPEG/PNG/WebP header), then a larger one for files with big EXIF/ICC blocks.
    None if it can't be determined."""
    for size in (98304, 524288):
        try:
            r = await client.get(url, headers={"Range": f"bytes=0-{size - 1}"})
            if r.status_code not in (200, 206):
                return None
            im = Image.open(io.BytesIO(r.content))
            w, h = im.size
            try:
                if im.getexif().get(274, 1) in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:
                pass
            if w and h:
                return int(w), int(h)
        except Exception:
            continue
    return None


@router.get("/roster/fields")
async def roster_fields_registry(admin: dict = Depends(current_team_or_admin)):
    """The field groups/defaults the roster builder offers (single source of truth)."""
    return F.registry()


# ---------------------------------------------------------------------------
# Admin: media options for the image-selection step
# ---------------------------------------------------------------------------
class MediaOptionsIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)


@router.post("/roster/media-options")
async def roster_media_options(payload: MediaOptionsIn, admin: dict = Depends(current_team_or_admin)):
    ids = list(dict.fromkeys([i for i in payload.talent_ids if i]))[:MAX_TALENTS]
    docs = await db.talents.find({"id": {"$in": ids}}, {"_id": 0, "id": 1, "name": 1, "media": 1, "cover_media_id": 1, "status": 1}).to_list(len(ids) or 1)
    by_id = {d["id"]: d for d in docs}
    out = []
    for tid in ids:
        t = by_id.get(tid)
        if not t:
            continue
        groups = _roster_images(t)
        out.append({
            "talent_id": tid,
            "name": t.get("name"),
            "short_name": short_talent_name(t.get("name")),
            "cover_media_id": t.get("cover_media_id"),
            "has_video": _intro_video(t) is not None,
            "default_media_ids": default_selection(t),
            "groups": [
                {"key": c, "label": CATEGORY_LABELS[c],
                 "items": [{"id": m["id"], "url": m["url"]} for m in groups[c]]}
                for c in ROSTER_CATEGORIES
            ],
        })
    return {"talents": out, "limits": {"max_talents": MAX_TALENTS, "max_images_per_talent": MAX_IMAGES_PER_TALENT,
                                       "max_total_images": MAX_TOTAL_IMAGES}}


# ---------------------------------------------------------------------------
# Create / update (called from links.create_link / update_link)
# ---------------------------------------------------------------------------
async def prepare_roster(raw: Optional[dict], existing: Optional[dict] = None) -> Tuple[List[str], dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("talents"), list) or not raw["talents"]:
        raise HTTPException(400, "Select at least one talent for the roster")
    entries = []
    seen = set()
    for e in raw["talents"]:
        tid = (e or {}).get("talent_id")
        if tid and tid not in seen:
            seen.add(tid)
            entries.append({"talent_id": tid, "media_ids": [m for m in dict.fromkeys((e or {}).get("media_ids") or []) if m]})
    if not entries:
        raise HTTPException(400, "Select at least one talent for the roster")
    if len(entries) > MAX_TALENTS:
        raise HTTPException(400, f"A roster can include at most {MAX_TALENTS} talents")

    docs = await db.talents.find({"id": {"$in": [e["talent_id"] for e in entries]}},
                                 {"_id": 0, "id": 1, "name": 1, "media": 1, "status": 1}).to_list(len(entries))
    by_id = {d["id"]: d for d in docs}
    prev_dims: Dict[str, Dict[str, list]] = {}
    for pt in ((existing or {}).get("roster") or {}).get("talents") or []:
        prev_dims[pt.get("talent_id")] = pt.get("dims") or {}

    total_images = 0
    to_probe: List[Tuple[int, str, str]] = []
    out_talents: List[dict] = []
    for idx, e in enumerate(entries):
        t = by_id.get(e["talent_id"])
        if not t or str(t.get("status") or "").lower() in _BLOCKED:
            raise HTTPException(400, "One of the selected talents no longer exists or is archived")
        allowed = {m["id"]: m for cat in ROSTER_CATEGORIES for m in _roster_images(t)[cat]}
        bad = [m for m in e["media_ids"] if m not in allowed]
        if bad:
            raise HTTPException(400, f"A selected image no longer exists on {short_talent_name(t.get('name'))}'s profile")
        if len(e["media_ids"]) > MAX_IMAGES_PER_TALENT:
            raise HTTPException(400, f"Select at most {MAX_IMAGES_PER_TALENT} images per talent")
        total_images += len(e["media_ids"])
        dims: Dict[str, list] = {}
        for mid in e["media_ids"]:
            known = (prev_dims.get(e["talent_id"]) or {}).get(mid)
            if known:
                dims[mid] = known
            else:
                to_probe.append((idx, mid, allowed[mid]["url"]))
        out_talents.append({"talent_id": e["talent_id"], "media_ids": e["media_ids"], "dims": dims})
    if total_images > MAX_TOTAL_IMAGES:
        raise HTTPException(400, f"A roster can include at most {MAX_TOTAL_IMAGES} images in total")

    if to_probe:
        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            async def one(item):
                async with sem:
                    return item, await probe_image_dims(client, item[2])
            for (idx, mid, _u), d in await asyncio.gather(*[one(i) for i in to_probe]):
                if d:
                    out_talents[idx]["dims"][mid] = [d[0], d[1]]
    subtitle = (raw.get("subtitle") or "").strip() or None
    # Field visibility is roster-level config (never touches a talent). A save that
    # sends no `fields` keeps an existing roster's stored config; a brand-new roster
    # (or one that never had any) gets the standard defaults stored explicitly.
    if isinstance(raw.get("fields"), dict):
        fields = F.normalize_fields(raw["fields"])
    elif F.has_stored_fields((existing or {}).get("roster")):
        fields = F.normalize_fields(existing["roster"]["fields"])
    else:
        fields = F.normalize_fields(None)
    return [e["talent_id"] for e in entries], {"subtitle": subtitle, "fields": fields, "talents": out_talents,
                                               "updated_at": _now()}


def _warm_pdf(link: dict) -> None:
    """Build the PDF in the background so the first Download is instant."""
    async def _go():
        try:
            resolved = await resolve_roster(link)
            if resolved["talents"]:
                await roster_pdf.get_or_build_pdf(roster_pdf.content_key(resolved), resolved)
        except Exception:
            logger.exception("roster pdf warm-up failed (non-fatal)")
    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        pass


async def create_roster_link(payload: LinkIn, admin: dict) -> dict:
    if not (payload.title or "").strip():
        raise HTTPException(400, "Roster name is required")
    talent_ids, roster = await prepare_roster(payload.roster)
    now_iso = _now()
    doc = {
        "id": str(uuid.uuid4()),
        "slug": _slugify(payload.title),
        "title": payload.title.strip(),
        "brand_name": None,
        "link_type": "roster",
        "roster": roster,
        "talent_ids": talent_ids,
        "submission_ids": [],
        "subject_added_at": {tid: now_iso for tid in talent_ids},
        "talent_field_visibility": {},
        "auto_pull": False,
        "auto_project_id": None,
        "visibility": {**DEFAULT_VISIBILITY},
        "is_public": payload.is_public,
        "password": None,
        "notes": payload.notes,
        "client_budget_override": None,
        "created_at": now_iso,
        "created_by": admin["id"],
    }
    await db.links.insert_one(doc)
    doc.pop("_id", None)
    doc["view_count"] = 0
    doc["unique_viewers"] = 0
    _warm_pdf(doc)
    return doc


async def update_roster_link(lid: str, payload: LinkIn, admin: dict, existing: dict) -> dict:
    if payload.roster is None:
        # Settings-only update (e.g. the Generated Links "public link" toggle,
        # which round-trips the generic LinkIn shape): never touch the roster.
        upd: Dict[str, Any] = {"is_public": payload.is_public, "notes": payload.notes}
        if (payload.title or "").strip():
            upd["title"] = payload.title.strip()
    else:
        talent_ids, roster = await prepare_roster(payload.roster, existing)
        prev_added = existing.get("subject_added_at") or {}
        now_iso = _now()
        upd = {
            "title": (payload.title or existing.get("title") or "").strip(),
            "roster": roster, "talent_ids": talent_ids, "submission_ids": [],
            "subject_added_at": {t: prev_added.get(t, now_iso) for t in talent_ids},
            "is_public": payload.is_public, "notes": payload.notes,
        }
    await db.links.update_one({"id": lid}, {"$set": upd})
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    if link is None:
        raise HTTPException(404, "Link not found")
    link["view_count"] = await db.link_views.count_documents({"link_id": lid})
    link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": lid}))
    if payload.roster is not None:
        _warm_pdf(link)
    return link


# ---------------------------------------------------------------------------
# Resolve a roster against live talent data (web view + PDF input)
# ---------------------------------------------------------------------------
def _info_items(t: dict, fields: Dict[str, bool]) -> List[dict]:
    t = enrich_talent(dict(t)) or {}
    return F.info_items(t, fields, _format_location, normalize_instagram_handle)


async def resolve_roster(link: dict) -> Dict[str, Any]:
    roster = link.get("roster") or {}
    fields = F.normalize_fields(roster.get("fields"))  # old rosters (no stored config) -> defaults
    entries = roster.get("talents") or []
    docs = await db.talents.find(
        {"id": {"$in": [e["talent_id"] for e in entries]}},
        F.projection(),
    ).to_list(len(entries) or 1)
    by_id = {d["id"]: d for d in docs}
    talents: List[dict] = []
    for e in entries:
        t = by_id.get(e["talent_id"])
        if not t or str(t.get("status") or "").lower() in _BLOCKED:
            continue  # talent gone/archived: omit gracefully
        allowed = {m["id"]: (cat, m) for cat in ROSTER_CATEGORIES for m in _roster_images(t)[cat]}
        images = []
        for mid in e.get("media_ids") or []:
            hit = allowed.get(mid)
            if not hit:
                continue  # deleted/hidden since the roster was made
            cat, m = hit
            d = (e.get("dims") or {}).get(mid)
            images.append({"id": mid, "url": m["url"], "category": cat,
                           "ratio": L.safe_ratio(d[0], d[1]) if d else L.DEFAULT_RATIO})
        video = _intro_video(t) if fields.get("intro_video") else None
        talents.append({
            # Name OFF: a neutral numbered label keeps nav / index usable without naming the talent.
            "name": short_talent_name(t.get("name")) if fields.get("name") else f"Talent {len(talents) + 1:02d}",
            "info": _info_items(t, fields),
            "video_url": video_watch_url(video) if video else None,
            "images": images,
        })
    return {"title": link.get("title") or "Talentgram Roster", "subtitle": roster.get("subtitle"),
            "fields": fields, "talents": talents}


def web_view(resolved: Dict[str, Any]) -> Dict[str, Any]:
    talents = []
    for i, t in enumerate(resolved["talents"]):
        plan = L.plan_talent_pages([(im["id"], im["ratio"]) for im in t["images"]], t["info"], bool(t["video_url"]), t["name"])
        talents.append({
            "key": f"t{i + 1}", "index": i + 1, "name": t["name"], "info": t["info"],
            "video": {"url": t["video_url"]} if t["video_url"] else None,
            "images": t["images"], "pages": [p.as_dict() for p in plan],
        })
    return {"title": resolved["title"], "subtitle": resolved["subtitle"], "fields": resolved["fields"],
            "page": {"w": L.PAGE_W, "h": L.PAGE_H, "margin": L.MARGIN},
            "talents": talents, "pdf_available": any(t["images"] for t in resolved["talents"])}


def _pdf_filename(title: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9 _.\-]", "", title or "").strip() or "Talentgram Roster"
    return f"{safe}.pdf"


async def _pdf_response(link: dict) -> FileResponse:
    resolved = await resolve_roster(link)
    if not resolved["talents"]:
        raise HTTPException(404, "This roster has no available talents")
    key = roster_pdf.content_key(resolved)
    try:
        path = await roster_pdf.get_or_build_pdf(key, resolved)
    except Exception:
        logger.exception("roster pdf build failed for link %s", link.get("id"))
        raise HTTPException(500, "Unable to generate the PDF. Please try again in a moment.")
    return FileResponse(path, media_type="application/pdf", filename=_pdf_filename(link.get("title")),
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------
@router.get("/public/links/{slug}/meta")
async def public_link_meta(slug: str):
    """Tiny unauthenticated probe so the public page can pick the right
    viewer (roster vs. the classic client view). Reveals the link TYPE only;
    the title/subtitle are returned for roster links alone (their identify
    screen shows them) — never for classic links."""
    link = await db.links.find_one({"slug": slug}, {"_id": 0, "link_type": 1, "title": 1, "roster": 1})
    if not link:
        raise HTTPException(404, "Link not found")
    is_roster = link.get("link_type") == "roster"
    return {"link_type": link.get("link_type") or "talent",
            "title": link.get("title") if is_roster else None,
            "subtitle": (link.get("roster") or {}).get("subtitle") if is_roster else None}


async def _roster_link_for_viewer(slug: str, authorization: Optional[str]) -> dict:
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link or link.get("link_type") != "roster":
        raise HTTPException(404, "Link not found")
    _require_active_link(link, allow_roster=True)
    return link


@router.get("/public/links/{slug}/roster")
async def public_roster(slug: str, authorization: Optional[str] = Header(None)):
    link = await _roster_link_for_viewer(slug, authorization)
    return web_view(await resolve_roster(link))


@router.get("/public/links/{slug}/roster/pdf")
async def public_roster_pdf(slug: str, authorization: Optional[str] = Header(None), token: Optional[str] = None):
    auth = authorization or (f"Bearer {token}" if token else None)
    link = await _roster_link_for_viewer(slug, auth)
    viewer = decode_viewer(auth) or {}
    try:
        await db.link_downloads.insert_one({
            "id": str(uuid.uuid4()), "link_id": link["id"], "slug": slug,
            "viewer_email": viewer.get("email"), "viewer_name": viewer.get("name"),
            "talent_id": None, "media_id": "roster:pdf", "session_id": None, "created_at": _now(),
        })
    except Exception:
        logger.exception("failed to log roster pdf download (non-fatal)")
    return await _pdf_response(link)


@router.get("/links/{lid}/roster/pdf")
async def admin_roster_pdf(lid: str, admin: dict = Depends(current_team_or_admin)):
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    if not link or link.get("link_type") != "roster":
        raise HTTPException(404, "Roster not found")
    return await _pdf_response(link)
