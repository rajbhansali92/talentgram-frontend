"""Google Drive secondary backup layer.

Design contract (locked by product owner 2026-04):

  - PRIMARY storage is Emergent Object Storage. Drive uploads NEVER block,
    NEVER fail the primary write, and NEVER touch latency-critical code paths.
  - Files are uploaded by a service account into a folder the user shared
    with that SA. Files therefore consume the user's personal Drive quota,
    not Google's free SA-tier (which is zero).
  - Folder structure (created lazily, idempotent by name + parent):
        Talentgram/
          Projects/
            {brand_name}/
              {submission_id}/
                intro/
                takes/
                images/
  - File names preserve the talent-supplied label verbatim (only filesystem-
    illegal characters are stripped). NO renaming after upload (per spec).
  - Files stay PRIVATE. Sharing happens implicitly through whoever the
    parent folder is shared with.
  - On any failure: log + write an entry into `drive_upload_failures`
    so a periodic retry task can pick it up later. The user-facing upload
    succeeds regardless.

Concurrency model:
  Uploads are serialised through a single-worker asyncio.Queue. The
  googleapiclient HTTP layer isn't thread-safe (we observed SSL
  decryption errors under concurrent use), so one-worker + FIFO is the
  safe + simple choice. Queue is in-memory; if the process restarts, any
  pending items are re-picked-up by the periodic retry sweep reading from
  `drive_upload_failures`.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SA_KEY_PATH = os.environ.get("GOOGLE_DRIVE_SA_KEY_PATH")
PARENT_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_PARENT_FOLDER_ID")
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
SCOPES = ["https://www.googleapis.com/auth/drive"]

ROOT_FOLDER_NAME = "Talentgram"
PROJECTS_FOLDER_NAME = "Projects"
MAX_NAME_LEN = 100
ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Caches: folder name + parent_id -> Drive folder id. Drive folder IDs are
# stable so we cache aggressively. Thread-safe (serialised worker).
_folder_cache: Dict[tuple, str] = {}
_folder_lock = threading.Lock()
_service = None
_service_mode: Optional[str] = None  # 'user_oauth' | 'service_account'
_service_lock = threading.Lock()
_db_for_oauth = None  # Set on startup; used for refresh-token persistence
_main_loop: Optional[asyncio.AbstractEventLoop] = None  # Set on startup so worker threads can schedule mongo reads

# Serialised upload queue — max 1 worker (googleapiclient HTTP not thread-safe)
_upload_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None


def drive_enabled() -> bool:
    """Drive backup is opt-in: only runs if a parent folder is configured AND
    we have at least one usable auth path (user OAuth via stored refresh
    token OR service account JSON). Lets the app boot cleanly without Drive
    credentials."""
    if not PARENT_FOLDER_ID:
        return False
    if SA_KEY_PATH and os.path.exists(SA_KEY_PATH):
        return True
    if OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET:
        return True
    return False


def oauth_configured() -> bool:
    """Are the OAuth Client credentials present? (does NOT mean a user has
    consented yet — that requires `_load_user_credentials` to find a
    refresh token in Mongo)."""
    return bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_REDIRECT_URI)


def attach_db(db) -> None:
    """Server bootstrap calls this so the OAuth token-refresh path can
    persist refreshed access tokens back to Mongo. Also captures the main
    asyncio loop ref so worker threads can schedule mongo reads."""
    global _db_for_oauth, _main_loop
    _db_for_oauth = db
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = None


def _build_user_credentials(doc: dict) -> UserCredentials:
    return UserCredentials(
        token=doc.get("access_token"),
        refresh_token=doc.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        scopes=doc.get("scopes") or SCOPES,
    )


def _refresh_user_credentials_sync(creds: UserCredentials) -> UserCredentials:
    """Refresh an expired access token using the long-lived refresh token.
    Persists the new access_token + expiry back to Mongo (best-effort)."""
    creds.refresh(GoogleAuthRequest())
    if _db_for_oauth is not None and _main_loop is not None:
        async def _persist():
            await _db_for_oauth.drive_oauth.update_one(
                {"_id": "primary"},
                {"$set": {
                    "access_token": creds.token,
                    "expiry": creds.expiry.isoformat() if creds.expiry else None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
        try:
            asyncio.run_coroutine_threadsafe(_persist(), _main_loop)
        except Exception as e:
            logger.debug("OAuth refresh persist skipped: %s", e)
    return creds


def _get_service():
    """Lazy-build the Drive service client.

    Strategy:
      1. If a `drive_oauth` doc exists with a refresh_token → user OAuth.
         (User-owned drive, real quota, the path that ACTUALLY uploads files.)
      2. Otherwise fall back to service account.
         (Service accounts can create folders but cannot upload files
         outside of a Shared Drive — see Google's storage-quota docs.)
    Cached for the process lifetime; cleared via `clear_drive_service()`.
    """
    global _service, _service_mode
    if _service is not None:
        return _service
    with _service_lock:
        if _service is not None:
            return _service

        # Try user OAuth first.
        oauth_doc = None
        if oauth_configured() and _db_for_oauth is not None and _main_loop is not None:
            # Worker threads can't await Motor coroutines; schedule the read
            # on the main asyncio loop and block until it returns. We wrap
            # the find_one in a tiny coroutine so `run_coroutine_threadsafe`
            # gets an awaitable (Motor returns a non-awaitable Future-like).
            async def _lookup():
                return await _db_for_oauth.drive_oauth.find_one(
                    {"_id": "primary"}, {"_id": 0}
                )
            try:
                fut = asyncio.run_coroutine_threadsafe(_lookup(), _main_loop)
                oauth_doc = fut.result(timeout=10)
            except Exception as e:
                logger.warning("OAuth doc lookup failed: %s", e)

        if oauth_doc and oauth_doc.get("refresh_token"):
            creds = _build_user_credentials(oauth_doc)
            if not creds.valid:
                try:
                    creds = _refresh_user_credentials_sync(creds)
                except Exception as e:
                    logger.warning("OAuth token refresh failed, falling back to SA: %s", e)
                    creds = None
            if creds:
                _service = build("drive", "v3", credentials=creds, cache_discovery=False)
                _service_mode = "user_oauth"
                logger.info(
                    "Google Drive client initialised (user OAuth as %s)",
                    oauth_doc.get("connected_email", "unknown"),
                )
                return _service

        # Fallback: service account. Folders work; files generally do NOT
        # without a Shared Drive — but we still allow folder lookups.
        if SA_KEY_PATH and os.path.exists(SA_KEY_PATH):
            sa_creds = service_account.Credentials.from_service_account_file(
                SA_KEY_PATH, scopes=SCOPES
            )
            _service = build("drive", "v3", credentials=sa_creds, cache_discovery=False)
            _service_mode = "service_account"
            logger.info("Google Drive client initialised (service account fallback)")
            return _service

        raise RuntimeError("No usable Drive credentials available")


def clear_drive_service() -> None:
    """Force the next call to `_get_service` to rebuild — used after the
    user (re-)connects OAuth so we don't keep using stale SA credentials."""
    global _service, _service_mode, _folder_cache
    with _service_lock:
        _service = None
        _service_mode = None
    with _folder_lock:
        _folder_cache.clear()


# --------------------------------------------------------------------------
# Filename sanitisation (preserves label as much as possible)
# --------------------------------------------------------------------------
def sanitize_filename(label: str, fallback: str = "file") -> str:
    """Strip only filesystem-illegal characters and excess whitespace.

    Preserves the talent-supplied label verbatim where possible — does NOT
    convert to generic names (e.g. take_1) per product spec.
    """
    if not label:
        return fallback
    cleaned = ILLEGAL_CHARS.sub("", label)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    if len(cleaned) > MAX_NAME_LEN:
        cleaned = cleaned[:MAX_NAME_LEN].rstrip(" .")
    return cleaned


def _ext_from_filename(filename: Optional[str], fallback: str = "bin") -> str:
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 6 and ext.isalnum():
            return ext
    return fallback


def _folder_for_category(category: str) -> Optional[str]:
    if category == "intro_video":
        return "intro"
    if category == "take" or category in {"take_1", "take_2", "take_3"}:
        return "takes"
    if category == "image":
        return "images"
    return None


def _build_filename(media: dict, sub: dict) -> str:
    """Compute the desired Drive filename for a media item.

    - intro_video → 'intro.mp4' (or actual extension)
    - take → '<label>.<ext>' (label preserved exactly minus illegal chars)
    - image → 'image_{n}.<ext>' (n = index of this image in the submission)
    """
    cat = media.get("category")
    orig = media.get("original_filename") or ""
    if cat == "intro_video":
        ext = _ext_from_filename(orig, fallback="mp4")
        return f"intro.{ext}"
    if cat == "take" or cat in {"take_1", "take_2", "take_3"}:
        label = (media.get("label") or "").strip()
        if not label:
            if cat == "take_1":
                label = "Take 1"
            elif cat == "take_2":
                label = "Take 2"
            elif cat == "take_3":
                label = "Take 3"
            else:
                label = "Take"
        ext = _ext_from_filename(orig, fallback="mp4")
        return f"{sanitize_filename(label)}.{ext}"
    if cat == "image":
        # Determine 1-based index across all images in this submission.
        idx = 1
        for m in sub.get("media", []) or []:
            if m.get("category") == "image":
                if m.get("id") == media.get("id"):
                    break
                idx += 1
        ext = _ext_from_filename(orig, fallback="jpg")
        return f"image_{idx}.{ext}"
    return sanitize_filename(orig or "file")


# --------------------------------------------------------------------------
# Folder management — idempotent get-or-create
# --------------------------------------------------------------------------
def _find_folder(svc, name: str, parent_id: str) -> Optional[str]:
    safe = name.replace("'", "\\'")
    q = (
        f"name = '{safe}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"'{parent_id}' in parents and trashed = false"
    )
    res = svc.files().list(
        q=q,
        fields="files(id,name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(svc, name: str, parent_id: str) -> str:
    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    res = svc.files().create(
        body=body, fields="id", supportsAllDrives=True
    ).execute()
    return res["id"]


def _ensure_folder(svc, name: str, parent_id: str) -> str:
    key = (name, parent_id)
    cached = _folder_cache.get(key)
    if cached:
        return cached
    with _folder_lock:
        cached = _folder_cache.get(key)
        if cached:
            return cached
        fid = _find_folder(svc, name, parent_id) or _create_folder(svc, name, parent_id)
        _folder_cache[key] = fid
        return fid


def _ensure_path(svc, parts: list[str]) -> str:
    """Create or find a nested path under PARENT_FOLDER_ID. Returns the
    leaf folder id. `parts` is the chain to descend, e.g.
    ['Talentgram','Projects','BrandX','sub-uuid','takes']."""
    parent = PARENT_FOLDER_ID
    for name in parts:
        parent = _ensure_folder(svc, name, parent)
    return parent


# --------------------------------------------------------------------------
# Public submission folder URL
# --------------------------------------------------------------------------
def submission_folder_url(brand_name: str, submission_id: str) -> Optional[str]:
    """Return a clickable Drive URL for the submission's root folder.

    Best-effort — looks up (creates if missing) the path so admins can land
    even if no media has been uploaded yet."""
    if not drive_enabled():
        return None
    try:
        svc = _get_service()
        leaf = _ensure_path(
            svc,
            [ROOT_FOLDER_NAME, PROJECTS_FOLDER_NAME, sanitize_filename(brand_name), submission_id],
        )
        return f"https://drive.google.com/drive/folders/{leaf}"
    except Exception as e:
        logger.warning("submission_folder_url failed: %s", e)
        return None


# --------------------------------------------------------------------------
# Upload core
# --------------------------------------------------------------------------
def _upload_to_drive(
    media: dict, sub: dict, brand_name: str, data: bytes
) -> Dict[str, Any]:
    """Synchronous upload — runs inside an asyncio thread executor."""
    svc = _get_service()
    cat_folder = _folder_for_category(media.get("category"))
    if not cat_folder:
        raise ValueError(f"Unsupported category {media.get('category')}")
    parent_id = _ensure_path(
        svc,
        [
            ROOT_FOLDER_NAME,
            PROJECTS_FOLDER_NAME,
            sanitize_filename(brand_name),
            sub["id"],
            cat_folder,
        ],
    )
    filename = _build_filename(media, sub)
    mime = media.get("content_type") or "application/octet-stream"
    body = {"name": filename, "parents": [parent_id]}
    upload = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    res = svc.files().create(
        body=body,
        media_body=upload,
        fields="id, webViewLink, webContentLink, name",
        supportsAllDrives=True,
    ).execute()
    return {
        "google_drive_id": res["id"],
        "google_drive_url": res.get("webViewLink"),
        "google_drive_filename": res.get("name", filename),
        "google_drive_folder_id": parent_id,
    }


# --------------------------------------------------------------------------
# Async dispatch + retry queue
# --------------------------------------------------------------------------
async def _do_upload_async(
    db, media: dict, sub: dict, brand_name: str, data: bytes
) -> None:
    if not drive_enabled():
        return
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, _upload_to_drive, media, sub, brand_name, data
        )
        # Patch the media doc inline (works for submissions AND applications
        # since both store media as $push'd subdocuments keyed by media.id).
        coll = db.submissions if media.get("scope") == "submission" else db.applications
        await coll.update_one(
            {"id": sub["id"], "media.id": media["id"]},
            {"$set": {
                "media.$.google_drive_id": result["google_drive_id"],
                "media.$.google_drive_url": result["google_drive_url"],
                "media.$.google_drive_filename": result["google_drive_filename"],
                "media.$.google_drive_folder_id": result["google_drive_folder_id"],
            }},
        )
        logger.info(
            "Drive upload OK media=%s sub=%s name=%s",
            media.get("id"), sub.get("id"), result["google_drive_filename"],
        )
        # Best-effort cleanup of any prior failure row.
        await db.drive_upload_failures.delete_many({"media_id": media["id"]})
    except (HttpError, Exception) as e:
        msg = str(e)
        # `storageQuotaExceeded` is a hard, non-transient error — service
        # account has no Drive quota of its own. Mark such failures as
        # terminal so the retry loop doesn't spin on them forever.
        terminal = "storageQuotaExceeded" in msg or "do not have storage quota" in msg
        logger.warning(
            "Drive upload %s media=%s sub=%s: %s",
            "TERMINAL" if terminal else "FAILED",
            media.get("id"), sub.get("id"), msg[:300],
        )
        await db.drive_upload_failures.update_one(
            {"media_id": media["id"]},
            {"$set": {
                "media_id": media["id"],
                "submission_id": sub["id"],
                "scope": media.get("scope") or "submission",
                "brand_name": brand_name,
                "last_error": msg[:500],
                # `data` field intentionally NOT persisted — re-fetch from
                # primary storage on retry to keep the queue lightweight.
                "storage_path": media.get("storage_path"),
                "terminal": terminal,
                # If terminal, set retry_count to a high value so the cron
                # skips it. If not, keep at 0 for next sweep to retry.
                "retry_count": 99 if terminal else 0,
            },
            "$inc": {"_attempted": 1}},
            upsert=True,
        )


def enqueue_drive_upload(db, media: dict, sub: dict, brand_name: str, data: bytes) -> None:
    """Fire-and-forget Drive upload. Safe to call from request handlers —
    pushes the job onto the FIFO queue so a single worker processes them
    serially (googleapiclient HTTP client is not thread-safe)."""
    if not drive_enabled():
        return
    global _upload_queue
    if _upload_queue is None:
        # Worker hasn't started yet — skip and let the retry sweep pick it up.
        logger.debug("enqueue_drive_upload: worker not running, skipping")
        return
    try:
        _upload_queue.put_nowait({
            "db": db,
            "media": media,
            "sub": sub,
            "brand_name": brand_name,
            "data": data,
        })
    except asyncio.QueueFull:
        logger.warning("Drive upload queue full — dropping media=%s", media.get("id"))


async def _worker_loop():
    """Serialised Drive upload worker. Keeps one in-flight upload at a time.
    Exceptions inside the task are fully contained — worker never dies."""
    assert _upload_queue is not None
    logger.info("Drive upload worker started")
    while True:
        job = await _upload_queue.get()
        try:
            await _do_upload_async(
                job["db"], job["media"], job["sub"], job["brand_name"], job["data"],
            )
        except Exception as e:
            logger.exception("Drive worker error: %s", e)
        finally:
            _upload_queue.task_done()


def start_drive_worker():
    """Spawn the single upload worker task. Safe to call multiple times —
    only starts once."""
    global _upload_queue, _worker_task
    if not drive_enabled():
        return
    if _worker_task and not _worker_task.done():
        return
    _upload_queue = asyncio.Queue(maxsize=500)
    _worker_task = asyncio.create_task(_worker_loop())


# --------------------------------------------------------------------------
# Retry pending failures (scheduled from server.py startup)
# --------------------------------------------------------------------------
async def retry_pending_uploads(db, get_object) -> None:
    """Walk through failure queue and retry each one via the single-worker
    queue. Skips terminal failures (e.g. SA quota errors) which are marked
    retry_count=99 at failure time. `get_object` is `core.get_object` —
    passed in to avoid a circular import."""
    if not drive_enabled():
        return
    pending = await db.drive_upload_failures.find(
        {"retry_count": {"$lt": 5}, "terminal": {"$ne": True}}, {"_id": 0}
    ).limit(20).to_list(20)
    for row in pending:
        coll = db.submissions if row.get("scope") == "submission" else db.applications
        sub = await coll.find_one({"id": row["submission_id"]})
        if not sub:
            await db.drive_upload_failures.delete_one({"media_id": row["media_id"]})
            continue
        media = next((m for m in (sub.get("media") or []) if m.get("id") == row["media_id"]), None)
        if not media or media.get("google_drive_url"):
            await db.drive_upload_failures.delete_one({"media_id": row["media_id"]})
            continue
        try:
            data, _ = get_object(media["storage_path"])
        except Exception as e:
            logger.warning("Drive retry: cannot re-fetch primary file %s: %s", media.get("id"), e)
            await db.drive_upload_failures.update_one(
                {"media_id": row["media_id"]},
                {"$inc": {"retry_count": 1}, "$set": {"last_error": f"refetch: {e}"[:500]}},
            )
            continue
        # Bump retry counter BEFORE enqueue so a persistent failure eventually exits the loop.
        await db.drive_upload_failures.update_one(
            {"media_id": row["media_id"]}, {"$inc": {"retry_count": 1}},
        )
        enqueue_drive_upload(db, media, sub, row["brand_name"], data)
