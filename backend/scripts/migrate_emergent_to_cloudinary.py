"""Migrate legacy Emergent Object Storage media → Cloudinary.

Scans every collection that holds `media[]` arrays (talents, submissions,
applications) and, for each entry that has a `storage_path` but no `url`
yet, downloads the bytes from Emergent storage and re-uploads them to
Cloudinary preserving the folder hierarchy
(`talentgram/{collection}/{owner_id}/{media_id}`). The resulting Cloudinary
`secure_url` and `public_id` are written back into the same media item.

Behaviour:
  - SAFE / additive: never deletes `storage_path` or any other field.
  - Skips items already carrying a non-empty `url`.
  - Retries each item up to 2 times before logging FAILED.
  - Prints SUCCESS / SKIPPED / FAILED lines and a final summary.

Run from /app:

    cd /app/backend && python -m scripts.migrate_emergent_to_cloudinary

DRY RUN:
    cd /app/backend && python -m scripts.migrate_emergent_to_cloudinary --dry-run
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

# Cloudinary free-tier image upload limit is 10 MB. We pre-resize anything
# larger using Pillow to a 4K-wide JPEG (still ample for portfolio work)
# so ingestion never hits 413/BadRequest.
CLOUDINARY_IMAGE_LIMIT = 10 * 1024 * 1024
PRE_RESIZE_MAX_WIDTH = 3840

# Load env from /app/backend/.env so this works regardless of CWD.
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# Import after env load so `core` picks up Cloudinary creds.
sys.path.insert(0, str(ROOT_DIR))
from core import APP_NAME, cloudinary_upload, db, logger  # noqa: E402

EMERGENT_KEY = os.environ.get("EMERGENT_LLM_KEY")
STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
DRY_RUN = "--dry-run" in sys.argv

# Lazy-init session-scoped storage key.
_storage_key: Optional[str] = None


def _init_storage_key() -> Optional[str]:
    global _storage_key
    if _storage_key:
        return _storage_key
    if not EMERGENT_KEY:
        return None
    try:
        resp = requests.post(
            f"{STORAGE_URL}/init",
            json={"emergent_key": EMERGENT_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        _storage_key = resp.json()["storage_key"]
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
        return None
    return _storage_key


def _maybe_shrink_image(data: bytes, content_type: Optional[str]) -> bytes:
    """If `data` is an image larger than Cloudinary's image limit, downscale it
    to PRE_RESIZE_MAX_WIDTH JPEG. Otherwise return the bytes unchanged."""
    if len(data) <= CLOUDINARY_IMAGE_LIMIT:
        return data
    if not (content_type or "").startswith("image/"):
        return data
    try:
        import io as _io
        from PIL import Image
        img = Image.open(_io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if w > PRE_RESIZE_MAX_WIDTH:
            new_h = int(h * (PRE_RESIZE_MAX_WIDTH / float(w)))
            img = img.resize((PRE_RESIZE_MAX_WIDTH, new_h), Image.LANCZOS)
        # Try progressively lower quality until under the limit.
        for quality in (90, 82, 75, 65):
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            out = buf.getvalue()
            if len(out) <= CLOUDINARY_IMAGE_LIMIT:
                logger.info(
                    f"Pre-resize: {len(data)} → {len(out)} bytes (q={quality}, w={img.size[0]})"
                )
                return out
        return out  # last attempt — best effort
    except Exception as e:
        logger.warning(f"Pre-resize skipped: {e}")
        return data


def emergent_get(path: str) -> Tuple[bytes, str]:
    """Fetch raw bytes + content-type from legacy Emergent storage."""
    key = _init_storage_key()
    if not key:
        raise RuntimeError("Emergent storage unavailable (no key)")
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=180,
    )
    if resp.status_code == 404:
        raise FileNotFoundError(path)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


def _resource_type_for(category: Optional[str], content_type: Optional[str]) -> str:
    if category in {"intro_video", "video", "take", "take_1", "take_2", "take_3", "video_file"}:
        return "video"
    if category == "audio":
        return "video"  # Cloudinary serves audio under the video resource type
    if category == "script":
        return "raw"
    if (content_type or "").startswith("video/"):
        return "video"
    if (content_type or "").startswith("audio/"):
        return "video"
    if (content_type or "").startswith("application/"):
        return "raw"
    return "image"


# Each spec describes how to walk a Mongo collection's media[] arrays.
# `folder_for(doc)` returns the Cloudinary folder path; `kind` is the
# scope label used in logs.
COLLECTIONS = [
    {
        "kind": "talents",
        "field": "media",
        "folder_for": lambda doc: f"{APP_NAME}/talents/{doc.get('id')}",
    },
    {
        "kind": "submissions",
        "field": "media",
        "folder_for": lambda doc: f"{APP_NAME}/submissions/{doc.get('id')}",
    },
    {
        "kind": "applications",
        "field": "media",
        "folder_for": lambda doc: f"{APP_NAME}/applications/{doc.get('id')}",
    },
    # Project audition materials live under projects.materials[]
    {
        "kind": "projects",
        "field": "materials",
        "folder_for": lambda doc: f"{APP_NAME}/projects/{doc.get('id')}",
    },
    # Moderated client→talent feedback (voice notes only carry a path)
    {
        "kind": "feedback",
        "field": None,  # single-doc, special-cased below
        "folder_for": lambda doc: f"{APP_NAME}/feedback/{doc.get('project_id') or 'misc'}/{doc.get('talent_id') or 'misc'}",
    },
]


async def _migrate_media_item(coll_name: str, doc_id: str, field: str, m: dict, folder: str) -> Tuple[str, str]:
    """Migrate a single media item. Returns (status, detail).
    status ∈ {SUCCESS, SKIPPED, FAILED}.
    """
    if m.get("url"):
        return ("SKIPPED", "already-migrated")
    storage_path = m.get("storage_path")
    if not storage_path:
        return ("SKIPPED", "no-storage-path")

    media_id = m.get("id") or storage_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    last_err: Optional[str] = None
    for attempt in (1, 2, 3):
        try:
            data, ctype = emergent_get(storage_path)
            if DRY_RUN:
                return ("SUCCESS", f"dry-run bytes={len(data)} ct={ctype}")
            rt = _resource_type_for(m.get("category"), ctype or m.get("content_type"))
            if rt == "image":
                data = _maybe_shrink_image(data, ctype or m.get("content_type"))
            result = cloudinary_upload(
                data,
                folder=folder,
                public_id=str(media_id),
                resource_type=rt,
                content_type=ctype or m.get("content_type"),
            )
            # Additive update: set url/public_id/resource_type only.
            await db[coll_name].update_one(
                {"id": doc_id, f"{field}.id": m.get("id")},
                {"$set": {
                    f"{field}.$.url": result["url"],
                    f"{field}.$.public_id": result["public_id"],
                    f"{field}.$.resource_type": result["resource_type"],
                }},
            )
            return ("SUCCESS", result["url"])
        except FileNotFoundError:
            return ("FAILED", "source-missing-on-emergent")
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.6 * attempt)
    return ("FAILED", last_err or "unknown-error")


async def _migrate_feedback_doc(doc: dict) -> Tuple[str, str]:
    """Feedback docs don't have a `media[]` array — they store a single
    `content_url`/`content_path` (legacy) or `content_url`/`content_public_id`
    (post-migration). Migrate the legacy path if present."""
    if doc.get("content_url"):
        return ("SKIPPED", "already-migrated")
    storage_path = doc.get("content_path") or doc.get("storage_path")
    if not storage_path:
        return ("SKIPPED", "no-storage-path")
    folder = f"{APP_NAME}/feedback/{doc.get('project_id') or 'misc'}/{doc.get('talent_id') or 'misc'}"
    media_id = doc.get("id") or storage_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    last_err: Optional[str] = None
    for attempt in (1, 2, 3):
        try:
            data, ctype = emergent_get(storage_path)
            if DRY_RUN:
                return ("SUCCESS", f"dry-run bytes={len(data)} ct={ctype}")
            rt = _resource_type_for(doc.get("type"), ctype)
            result = cloudinary_upload(
                data,
                folder=folder,
                public_id=str(media_id),
                resource_type=rt,
                content_type=ctype,
            )
            await db.feedback.update_one(
                {"id": doc.get("id")},
                {"$set": {
                    "content_url": result["url"],
                    "content_public_id": result["public_id"],
                    "content_resource_type": result["resource_type"],
                }},
            )
            return ("SUCCESS", result["url"])
        except FileNotFoundError:
            return ("FAILED", "source-missing-on-emergent")
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.6 * attempt)
    return ("FAILED", last_err or "unknown-error")


async def main() -> None:
    print(f"== Cloudinary migration {'(DRY RUN)' if DRY_RUN else ''} ==")
    if not EMERGENT_KEY:
        print("ERROR: EMERGENT_LLM_KEY missing from .env — cannot fetch from Emergent storage.")
        return
    if not _init_storage_key():
        print("ERROR: Emergent storage init failed (network or invalid key).")
        return

    total = migrated = skipped = failed = 0

    for spec in COLLECTIONS:
        coll = spec["kind"]
        field = spec["field"]
        folder_for = spec["folder_for"]

        if coll == "feedback":
            cursor = db.feedback.find({"type": "voice"}, {"_id": 0})
            async for doc in cursor:
                total += 1
                status, detail = await _migrate_feedback_doc(doc)
                tag = f"[feedback/{doc.get('id')}]"
                if status == "SUCCESS":
                    migrated += 1
                    print(f"SUCCESS: {tag} → {detail}")
                elif status == "SKIPPED":
                    skipped += 1
                    print(f"SKIPPED: {tag} → {detail}")
                else:
                    failed += 1
                    print(f"FAILED:  {tag} → {detail}")
            continue

        cursor = db[coll].find({field: {"$exists": True, "$ne": []}}, {"_id": 0})
        async for doc in cursor:
            doc_id = doc.get("id")
            for m in (doc.get(field) or []):
                total += 1
                folder = folder_for(doc)
                status, detail = await _migrate_media_item(coll, doc_id, field, m, folder)
                tag = f"[{coll}/{doc_id}/{m.get('id')}]"
                if status == "SUCCESS":
                    migrated += 1
                    print(f"SUCCESS: {tag} → {detail}")
                elif status == "SKIPPED":
                    skipped += 1
                    print(f"SKIPPED: {tag} → {detail}")
                else:
                    failed += 1
                    print(f"FAILED:  {tag} → {detail}")

    print()
    print("================ SUMMARY ================")
    print(f"Total scanned: {total}")
    print(f"Migrated:      {migrated}")
    print(f"Skipped:       {skipped}")
    print(f"Failed:        {failed}")
    print(f"Mode:          {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print("=========================================")


if __name__ == "__main__":
    asyncio.run(main())
