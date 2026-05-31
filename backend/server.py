"""Talentgram Portfolio Engine — FastAPI bootstrap."""

import asyncio
import logging
from datetime import datetime, timezone
import os

from fastapi import APIRouter, FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from core import db, mongo_client, seed_admin, update_talent_cover_cache
from drive_backup import attach_db, drive_enabled, start_drive_worker
from notifications import ensure_indexes as ensure_notifications_indexes
from routers import (
    applications,
    auth,
    casting_pipeline,
    drive_admin,
    feedback,
    links,
    marketing as marketing_router,
    notifications as notifications_router,
    password,
    projects,
    submissions,
    talents,
    users,
    workflow,
    portal,
)

app = FastAPI(title="Talentgram Portfolio Engine")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# SecurityHeadersMiddleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        # Skip docs completely
        if request.url.path.startswith("/docs") or request.url.path.startswith("/openapi"):
            return response

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )

        return response


# Health / meta
_meta = APIRouter(prefix="/api")


@_meta.get("/")
async def root():
    return {"app": "talentgram", "ok": True}


@app.get("/health")
async def health():
    """Dedicated Railway health-check endpoint — no auth, no middleware cost."""
    return {"status": "ok"}


# Register routers
app.include_router(_meta)
app.include_router(auth.router)
app.include_router(talents.router)
app.include_router(links.router)
app.include_router(projects.router)
app.include_router(submissions.router)
app.include_router(applications.router)
app.include_router(users.router)
app.include_router(password.router)
app.include_router(drive_admin.router)
app.include_router(notifications_router.router)
app.include_router(marketing_router.router)
app.include_router(feedback.router)
app.include_router(casting_pipeline.router)
app.include_router(workflow.router)
app.include_router(portal.router)


# Middleware — order matters: last registered = outermost (first to run).
# SecurityHeadersMiddleware is registered first so it wraps the full response.
app.add_middleware(SecurityHeadersMiddleware)

# CORS — env-var driven with Vercel preview regex fallback.
# CORS_ORIGINS: comma-separated explicit origins (e.g. your production domain).
# CORS_ORIGINS_REGEX: regex covering dynamic preview URLs. Defaults to all
# talentgram-frontend Vercel preview deployments. Set to "disabled" to turn off.
cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

cors_origins_regex = os.environ.get(
    "CORS_ORIGINS_REGEX",
    r"https://talentgram-frontend-.*\.vercel\.app",
).strip()

if cors_origins_regex in ("", "disabled", "None", "none"):
    cors_origins_regex = None

logger.info("Active CORS origins: %s", cors_origins)
if cors_origins_regex:
    logger.info("Active CORS origin regex: %s", cors_origins_regex)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=cors_origins,
    allow_origin_regex=cors_origins_regex,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# Startup
async def run_media_duplicate_cleanup_migration() -> None:
    """Group media by public_id. Keep oldest record, delete newer duplicates.

    This migration is idempotent and runs as a no-op on subsequent boots.
    Preserve: cover_media_id, cover_url.
    Save run report in db.migration_reports collection.
    """
    logger.info("== STARTING MEDIA DUPLICATE CLEANUP MIGRATION ==")
    scanned = 0
    report_records = []

    async for talent in db.talents.find({"media": {"$exists": True, "$ne": []}}):
        tid = talent.get("id")
        media = talent.get("media") or []
        scanned += 1

        # Group by public_id
        groups = {}
        for m in media:
            pid = m.get("public_id")
            if not pid:
                continue
            groups.setdefault(pid, []).append(m)

        to_delete_ids = set()
        affected_pids = []

        for pid, items in groups.items():
            if len(items) > 1:
                # Group has duplicates!
                # Map each item to its index in the original media list to keep the oldest
                indexed_items = [(idx, item) for idx, item in enumerate(media) if item.get("public_id") == pid]
                indexed_items.sort(key=lambda x: x[0])

                surviving_idx, surviving_item = indexed_items[0]
                duplicates = indexed_items[1:]

                for idx, duplicate_item in duplicates:
                    to_delete_ids.add(duplicate_item.get("id"))

                affected_pids.append(pid)

        if to_delete_ids:
            # Keep only items not in to_delete_ids
            new_media = [m for m in media if m.get("id") not in to_delete_ids]

            # Preserve cover: if the deleted item was cover_media_id,
            # update cover_media_id to point to the surviving oldest record sharing that public_id.
            cover_id = talent.get("cover_media_id")
            new_cover_id = cover_id

            if cover_id in to_delete_ids:
                deleted_cover_item = next((m for m in media if m.get("id") == cover_id), None)
                if deleted_cover_item:
                    pid = deleted_cover_item.get("public_id")
                    surviving_item = next((m for m in new_media if m.get("public_id") == pid), None)
                    if surviving_item:
                        new_cover_id = surviving_item.get("id")
                    else:
                        new_cover_id = None

            # Update the talent doc in DB
            await db.talents.update_one(
                {"id": tid},
                {
                    "$set": {
                        "media": new_media,
                        "cover_media_id": new_cover_id
                    }
                }
            )

            # Recalculate cover cache
            await update_talent_cover_cache(tid)

            report_records.append({
                "talent_id": tid,
                "duplicate_count_removed": len(to_delete_ids),
                "public_ids_affected": affected_pids
            })
            logger.info("DEDUPLICATED TALENT %s: removed %d duplicates of %s", tid, len(to_delete_ids), affected_pids)

    # Save the migration report in the database
    await db.migration_reports.insert_one({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "report": report_records,
        "scanned": scanned
    })
    logger.info("== MEDIA DUPLICATE CLEANUP MIGRATION COMPLETED ==")


@app.on_event("startup")
async def on_startup():
    try:
        logger.info("Starting Talentgram backend...")

        await run_media_duplicate_cleanup_migration()

        await seed_admin()
        logger.info("Admin seed complete")

        await ensure_notifications_indexes(db)
        logger.info("Notification indexes ready")

        await db.client_states.create_index(
            [("link_id", 1), ("viewer_email", 1)], unique=True
        )

        await db.feedback.create_index([("submission_id", 1), ("status", 1)])
        await db.feedback.create_index([("project_id", 1), ("status", 1)])
        await db.feedback.create_index([("created_at", -1)])

        # Marketing Hub / CRM indexes
        await db.clients.create_index([("last_contacted_date", -1)])
        try:
            await db.clients.create_index([("name", "text"), ("company_name", "text"), ("tags", "text")])
        except Exception as _e:
            logger.warning("clients text index: %s", _e)

        # Casting pipeline: enforce one card per (project, talent).
        # Backs up the application-level duplicate guard in `add_to_pipeline` and
        # the auto-create paths in `sync_pipeline_from_submission` /
        # `ensure_pipeline_from_finalized_submission`. Idempotent on re-boot.
        try:
            await db.casting_pipeline.create_index(
                [("project_id", 1), ("talent_id", 1)],
                unique=True,
                name="pipeline_project_talent_unique",
            )
        except Exception as _e:
            logger.warning("casting_pipeline unique index: %s", _e)

        # Workflow indexes
        try:
            await db.workflow_tasks.create_index([("assignee_id", 1), ("status", 1)])
            await db.workflow_tasks.create_index([("creator_id", 1)])
            await db.workflow_scouts.create_index([("status", 1), ("created_at", -1)])
            await db.workflow_notifications.create_index([("user_id", 1), ("read_at", 1)])
        except Exception as _e:
            logger.warning("workflow indexes: %s", _e)

        logger.info("Mongo indexes ready")

        if drive_enabled():
            logger.info("Google Drive backup ENABLED — starting retry worker")
            attach_db(db)
            start_drive_worker()
            # `_drive_retry_loop` (which polled for failed Emergent-OS uploads) is
            # disabled in v37m. With Cloudinary as primary storage, every successful
            # POST has already returned a public CDN URL; there's no concept of
            # "pending" data sitting on Emergent OS waiting to be backed up.
        else:
            logger.info("Google Drive backup DISABLED")

        logger.info("Backend startup completed successfully")

    except Exception as e:
        logger.exception("CRITICAL STARTUP FAILURE: %s", e)
        raise


# Shutdown
@app.on_event("shutdown")
async def on_shutdown():
    mongo_client.close()