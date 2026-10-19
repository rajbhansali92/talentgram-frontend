"""Personal Assistant — planning and executing one `update` task (ONE talent; "Angela and Priya's locations" becomes two independent tasks).

Task params:
  talents  [one resolved entry]
  updates  {"fields": [{"key","label","raw","state":"ready|needs_value|invalid","value","display","note","before","before_display","error"}],
            "media":  [{"category","label","op":"add|replace","existing","files":[{"key","name","size","type","state":"pending|uploading|uploaded|failed","error","media_id"}]}],
            "unsupported": [text], "base_updated_at": <talent.updated_at at plan time>}

Fields are written through the EXISTING PUT /talents/{id} (routers.talents.update_talent: validation, height_inches, profile_audits) and media through the
EXISTING POST /talents/{id}/media (routers.talents.add_media: Cloudinary upload, ownership, introduction-video replacement). Nothing is written
before approval; a file is uploaded only after approval, one manifest entry at a time, and a result is reported only after it is read back.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from core import db
from personal_assistant import store as S
from personal_assistant import update_items as U

SUPPORTED_TEXT = ("I can update: age, height, location, WhatsApp group name, Instagram handle and followers, and the introduction video, "
                  "Indian look pictures, Western look pictures and additional portfolio. For anything else, use the talent's profile page.")


def new_params(items: List[dict]) -> Dict[str, Any]:
    fields, media, unsupported = [], [], []
    for it in items:
        if it["kind"] == "field":
            fields.append({"key": it["key"], "label": U.FIELDS[it["key"]], "raw": it.get("raw"), "state": "needs_value" if it.get("raw") in (None, "") else "ready"})
        elif it["kind"] == "media":
            if not any(m["category"] == it["key"] for m in media):
                media.append({"category": it["key"], "label": U.MEDIA[it["key"]], "files": []})
        else:
            unsupported.append(it.get("said") or "that")
    return {"fields": fields, "media": media, "unsupported": unsupported}


def slots(task: dict) -> List[dict]:
    u = (task["params"].get("updates") or {})
    out: List[dict] = []
    for f in u.get("fields") or []:
        if f["state"] in ("needs_value", "invalid"):
            out.append({"key": f"value:{f['key']}", "kind": "value", "phrase": f.get("raw") or "", "options": [],
                        "prompt": f.get("error") or f"What should {f['label']} be?"})
    for m in u.get("media") or []:
        if not m.get("files"):
            video = m["category"] == "video"
            out.append({"key": f"files:{m['category']}", "kind": "files", "phrase": "", "options": [], "category": m["category"], "multiple": not video,
                        "accept": ".mp4,.mov,video/mp4,video/quicktime" if video else ".jpg,.jpeg,.png,.webp,.heic,.heif,image/*",
                        "max_mb": (U.MAX_VIDEO_BYTES if video else U.MAX_IMAGE_BYTES) // (1024 * 1024),
                        "prompt": f"Choose the {'video' if video else 'picture(s)'} for {m['label']}."})
    return out


def all_unsupported(task: dict) -> bool:
    u = task["params"].get("updates") or {}
    return not (u.get("fields") or u.get("media"))


_GENERIC_PICTURES = {"picture", "pictures", "photo", "photos", "image", "images"}


def unsupported_message(task: dict) -> str:
    """Why nothing can be done. A bare "pictures / photos" names no category — ask which one rather than guess between three."""
    said = [str(x).lower().strip() for x in (task["params"].get("updates") or {}).get("unsupported") or []]
    if said and all(x in _GENERIC_PICTURES for x in said):
        return ("Which pictures? Say the category: Indian look pictures, Western look pictures or additional portfolio "
                "(for example “update Kashvi's Western pictures”). I won't guess between them.")
    return "I can't update that from here. " + SUPPORTED_TEXT


def apply_value(task: dict, key: str, text: str) -> bool:
    for f in task["params"]["updates"]["fields"]:
        if f["key"] == key:
            f["raw"], f["state"], f["error"] = text, "ready", None
            return True
    return False


def apply_files(task: dict, category: str, files: List[dict]) -> Optional[str]:
    """Attach the file MANIFEST (names / sizes / types the browser holds) to a media slot. → error text or None."""
    for m in task["params"]["updates"]["media"]:
        if m["category"] != category:
            continue
        if not files:
            return "Choose at least one file."
        if category == "video" and len(files) != 1:
            return "The introduction video is a single file — choose one."
        if len(files) > 20:
            return "Please choose at most 20 files at a time."
        seen, out = set(), []
        for f in files:
            name, size, ctype = str(f.get("name") or "")[:200], int(f.get("size") or 0), str(f.get("type") or "")
            bad = U.check_file(category, name, size, ctype)
            if bad:
                return bad
            key = f"{name}|{size}"
            if key in seen:
                return f"{name} was chosen twice."
            seen.add(key)
            out.append({"key": key, "name": name, "size": size, "type": ctype, "state": "pending", "error": None, "media_id": None})
        m["files"] = out
        return None
    return "That isn't one of the pictures or videos requested."


async def prepare(task: dict) -> None:
    """With the talent resolved and every value present: read the CURRENT profile (read-only) and fill in before / after / add-or-replace, and
    validate each value. Sets `error` on the task when nothing supported is left."""
    p = task["params"]
    u = p["updates"]
    tid = (p.get("talents") or [{}])[0].get("id")
    doc = await db.talents.find_one({"id": tid}, {"_id": 0, "name": 1, "age": 1, "dob": 1, "height": 1, "location": 1, "whatsapp_group_name": 1,
                                                  "instagram_handle": 1, "instagram_followers": 1, "media": 1, "updated_at": 1}) or {}
    u["base_updated_at"] = doc.get("updated_at")
    for f in u["fields"]:
        if f["state"] == "needs_value":
            continue
        try:
            value, display, note = U.parse_value(f["key"], f.get("raw"))
            f.update({"value": value, "display": display, "note": note, "state": "ready", "error": None})
        except U.BadValue as e:
            f.update({"state": "invalid", "error": str(e), "value": None, "display": None})
            continue
        cur = doc.get(f["key"])
        f["before"] = cur
        f["before_display"] = U.location_text(cur) if f["key"] == "location" and cur else (("@" + str(cur)) if f["key"] == "instagram_handle" and cur else (str(cur) if cur not in (None, "", []) else "—"))
        f["blocked"] = None
        if f["key"] == "age" and doc.get("dob"):
            f["blocked"] = "This profile has a date of birth, and age is calculated from it — change the date of birth on the profile page instead."
        if f["key"] == "age" and cur == f.get("value"):
            f["note"] = (f.get("note") or "") or "Already this value"
    for m in u["media"]:
        existing = [x for x in (doc.get("media") or []) if x.get("category") == m["category"]]
        m["existing"] = len(existing)
        m["existing_names"] = [x.get("original_filename") or "" for x in existing][:3]
        m["op"] = "replace" if (m["category"] == "video" and existing) else "add"


def describe(task: dict) -> List[str]:
    p = task["params"]
    u = p["updates"]
    lines: List[str] = [f"Talent: {(p.get('talents') or [{}])[0].get('label') or '?'}"]
    for f in u["fields"]:
        if f.get("blocked"):
            lines.append(f"{f['label']}: NOT changed — {f['blocked']}")
            continue
        change = f"{f.get('before_display', '—')} → {f.get('display')}"
        lines.append(f"{f['label']}: {change}" + (f" ({f['note']})" if f.get("note") else ""))
    for m in u["media"]:
        names = ", ".join(x["name"] for x in m["files"][:5]) + (f" +{len(m['files']) - 5} more" if len(m["files"]) > 5 else "")
        if m["op"] == "replace":
            cur = m["existing_names"][0] if m.get("existing_names") and m["existing_names"][0] else "the current video"
            lines.append(f"{m['label']}: REPLACES {cur} with {names}")
        else:
            lines.append(f"{m['label']}: adds {len(m['files'])} file{'s' if len(m['files']) != 1 else ''} ({names}) to {m['existing']} existing — nothing is removed")
    for text in u.get("unsupported") or []:
        lines.append(f"“{text}” can't be changed from here — it will be left as it is. {SUPPORTED_TEXT}")
    return lines


def title(task: dict) -> str:
    u = (task["params"].get("updates") or {})
    who = ((task["params"].get("talents") or [{}])[0].get("label") or (task["params"].get("talents") or [{}])[0].get("phrase") or "…")
    what = [f["label"].lower() for f in u.get("fields") or []] + [m["label"].lower() for m in u.get("media") or []]
    return f"Update {who}: {', '.join(what) or '…'}"


def fingerprint_part(task: dict) -> dict:
    u = (task["params"].get("updates") or {})
    return {
        "f": [[f["key"], str(f.get("value")), str(f.get("before")), bool(f.get("blocked"))] for f in u.get("fields") or []],
        "m": [[m["category"], m.get("op"), m.get("existing"), [x["key"] for x in m.get("files") or []]] for m in u.get("media") or []],
    }


# ------------------------------------------------------------------------------------------------------------ execution
logger = logging.getLogger(__name__)


def _same(key: str, a: Any, b: Any) -> bool:
    if key == "location":
        norm = lambda v: [(str(x.get("city") or "").strip().lower(), str(x.get("country") or "").strip().lower()) for x in (v or []) if isinstance(x, dict)]
        return norm(a) == norm(b)
    return (a if a not in ("",) else None) == (b if b not in ("",) else None)


def _out(talent: str, item: str, status: str, reason: Optional[str] = None, detail: Optional[str] = None) -> dict:
    return {"name": f"{talent} — {item}", "status": status, "reason": reason, "detail": detail}


async def _write_fields(user: dict, doc: dict, todo: List[dict]) -> None:
    """The EXISTING admin update (validation, height_inches, profile_audits). It rewrites the whole form, so the payload is the stored profile
    plus the requested values — never a partial payload that would blank other fields."""
    from core import TalentIn, TalentUpdateIn
    from routers.talents import update_talent
    data = {k: doc[k] for k in TalentIn.model_fields if k in doc and doc[k] is not None}
    for f in todo:
        data[f["key"]] = f["value"]
    await update_talent(doc["id"], TalentUpdateIn(**data), admin=user)


async def run(user: dict, task: dict) -> dict:
    """Approved → apply the FIELD changes now (read back to verify), and open the media manifest for uploads. → the dict run_task expects."""
    p = task["params"]
    u = p["updates"]
    ent = (p.get("talents") or [{}])[0]
    tid, who = ent.get("id"), ent.get("label") or "the talent"
    outcomes: List[dict] = []
    doc = await db.talents.find_one({"id": tid})
    if not doc:
        return {"status": S.FAILED, "result": None, "error": f"{who} no longer exists.", "outcomes": []}
    todo: List[dict] = []
    for f in u["fields"]:
        if f.get("blocked"):
            outcomes.append(_out(who, f["label"], "skipped", f["blocked"]))
        elif f.get("state") != "ready":
            outcomes.append(_out(who, f["label"], "failed", "No usable value was given."))
        elif f["key"] == "whatsapp_group_name" and user.get("role") != "admin":
            outcomes.append(_out(who, f["label"], "failed", "Only an admin can change the WhatsApp group name."))
        elif not _same(f["key"], doc.get(f["key"]), f.get("before")):
            cur = doc.get(f["key"])
            now_txt = U.location_text(cur) if f["key"] == "location" and cur else (str(cur) if cur not in (None, "", []) else "empty")
            outcomes.append(_out(who, f["label"], "failed", f"It changed since you reviewed this (now {now_txt}), so I did not overwrite it. Ask me again."))
        else:
            todo.append(f)
    if todo:
        try:
            await _write_fields(user, doc, todo)
            after = await db.talents.find_one({"id": tid}) or {}
        except HTTPException as exc:
            after = None
            for f in todo:
                outcomes.append(_out(who, f["label"], "failed", exc.detail if isinstance(exc.detail, str) else "The profile could not be saved."))
        except Exception as exc:  # noqa: BLE001  (a stored value the model rejects, a database error …) — nothing is reported as saved
            after = None
            logger.warning("assistant update %s failed to save: %s", task["id"], type(exc).__name__)
            for f in todo:
                outcomes.append(_out(who, f["label"], "failed", "The profile could not be saved (a stored value on this profile didn't validate) — nothing was changed."))
        if after is not None:
            for f in todo:
                ok = _same(f["key"], after.get(f["key"]), f["value"])
                outcomes.append(_out(who, f["label"], "updated" if ok else "failed", None if ok else "It was not saved (the profile still has the old value).", f["display"] if ok else None))
    pending = sum(1 for m in u["media"] for x in m["files"] if x["state"] in ("pending", "uploading"))
    return _settled(outcomes + _file_outcomes(who, u), u, pending, field_outcomes=outcomes)


def _file_outcomes(task_talent: str, u: dict) -> List[dict]:
    out = []
    for m in u["media"]:
        for x in m["files"]:
            st = {"uploaded": "uploaded", "failed": "failed", "uploading": "pending", "pending": "pending"}[x["state"]]
            out.append(_out(task_talent, m["label"], st, x.get("error"), x["name"]))
    return out


def _settled(outcomes: List[dict], u: dict, pending: int, field_outcomes: List[dict]) -> dict:
    ok = sum(1 for o in field_outcomes if o["status"] == "updated") + sum(1 for m in u["media"] for x in m["files"] if x["state"] == "uploaded")
    bad = sum(1 for o in field_outcomes if o["status"] in ("failed", "skipped")) + sum(1 for m in u["media"] for x in m["files"] if x["state"] == "failed")
    if pending:
        status = S.RUNNING
    elif ok and not bad:
        status = S.COMPLETED
    elif ok:
        status = S.PARTIAL
    else:
        status = S.FAILED
    return {"status": status, "result": {"ok": ok, "failed": bad, "pending": pending}, "error": None if status != S.FAILED else "Nothing was changed.",
            "outcomes": outcomes, "field_outcomes": field_outcomes}


def settle(task: dict) -> dict:
    """Re-derive status / outcomes from the PERSISTED field outcomes + the per-file states (never from what the browser claims)."""
    u = task["params"]["updates"]
    who = (task["params"].get("talents") or [{}])[0].get("label") or "the talent"
    fo = task.get("field_outcomes") or []
    pending = sum(1 for m in u["media"] for x in m["files"] if x["state"] in ("pending", "uploading"))
    res = _settled(fo + _file_outcomes(who, u), u, pending, field_outcomes=fo)
    return res


async def upload_one(user: dict, task: dict, category: str, key: str, upload) -> dict:
    """One approved file → the existing add_media. Everything is checked against the APPROVED manifest; the talent and category come from the
    plan, never from the request. → the settled patch for the task."""
    owner = user["id"]
    u = task["params"]["updates"]
    m = next((x for x in u["media"] if x["category"] == category), None)
    entry = next((x for x in (m or {}).get("files", []) if x["key"] == key), None)
    if not m or not entry:
        raise HTTPException(400, "That file is not part of the approved plan.")
    size = getattr(upload, "size", None)
    if size is None:
        await upload.seek(0, 2)
        size = await upload.tell()
        await upload.seek(0)
    if size != entry["size"] or (upload.filename or "") != entry["name"]:
        raise HTTPException(400, "The file does not match what was approved (name or size differs).")
    bad = U.check_file(category, entry["name"], size, upload.content_type or entry["type"])
    if bad:
        raise HTTPException(400, bad)
    ent = (task["params"].get("talents") or [{}])[0]
    tid = ent.get("id")
    claimed = await S.claim_upload(owner, task["id"], category, key)
    if not claimed:
        raise HTTPException(409, "This file is already uploading or uploaded.")
    reason: Optional[str] = None
    media_id: Optional[str] = None
    try:
        head = await upload.read(32)
        await upload.seek(0)
        doc = await db.talents.find_one({"id": tid}, {"_id": 0, "id": 1, "status": 1, "media.id": 1, "media.category": 1})
        if not doc or str(doc.get("status") or "").upper() in ("ARCHIVED", "MERGED"):
            reason = "The talent is no longer available."
        elif not U.sniff(category, head):
            reason = "The file's contents don't match its type."
        else:
            before = {x.get("id") for x in doc.get("media") or []}
            from routers.talents import add_media
            await add_media(tid, category, upload, admin=user)
            after = await db.talents.find_one({"id": tid}, {"_id": 0, "media": 1}) or {}
            fresh = [x for x in after.get("media") or [] if x.get("id") not in before and x.get("category") == category and x.get("original_filename") == entry["name"]]
            if fresh:
                media_id = fresh[-1]["id"]
            else:
                reason = "The upload finished but the file was not found on the profile, so it is not reported as done."
    except HTTPException as exc:
        reason = exc.detail if isinstance(exc.detail, str) else "The upload was rejected."
    except Exception as exc:  # noqa: BLE001
        logger.warning("assistant media upload %s failed: %s", task["id"], type(exc).__name__)
        reason = "The upload failed — nothing was added to the profile."
    return await S.finish_upload(owner, task["id"], category, key, media_id, reason)
