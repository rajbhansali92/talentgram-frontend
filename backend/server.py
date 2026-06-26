"""Talentgram Portfolio Engine — FastAPI bootstrap."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
import os

from fastapi import APIRouter, FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from core import db, mongo_client, seed_admin, update_talent_cover_cache, validate_talent_fields_classification
from drive_backup import attach_db, drive_enabled, start_drive_worker
from notifications import ensure_indexes as ensure_notifications_indexes
from routers import (
    applications,
    auth,
    casting_pipeline,
    cloudinary_admin,
    drive_admin,
    feedback,
    links,
    marketing as marketing_router,
    notifications as notifications_router,
    password,
    portal,
    projects,
    submissions,
    talents,
    users,
    webhooks,
    whatsapp,
    workflow,
)
import scout_capture

_docs_url = None if os.environ.get("DISABLE_DOCS", "true").lower() in ("1", "true", "yes") else "/docs"
_redoc_url = None if os.environ.get("DISABLE_DOCS", "true").lower() in ("1", "true", "yes") else "/redoc"
app = FastAPI(title="Talentgram Portfolio Engine", docs_url=_docs_url, redoc_url=_redoc_url)

class JsonFormatter(logging.Formatter):
    def format(self, record):
        import json
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.root.handlers = [handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# SecurityHeadersMiddleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")

        return response


# Health / meta
_meta = APIRouter(prefix="/api")


@_meta.get("/")
async def root():
    return {"app": "talentgram", "ok": True}


@app.get("/health")
async def health():
    """Dedicated Railway health-check endpoint — no auth, no middleware cost.

    Includes AI Scout Capture OCR readiness so ops can see whether EasyOCR has
    finished warming. `status` stays "ok" regardless — OCR warmup is best-effort
    and must not fail the Railway health probe.
    """
    return {"status": "ok", "ocr": scout_capture.ocr_readiness()}


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
app.include_router(cloudinary_admin.router)
app.include_router(whatsapp.router)
app.include_router(webhooks.router)


# CORS — env-var driven with Vercel preview regex fallback.
# CORS_ORIGINS: comma-separated explicit origins (e.g. your production domain).
# CORS_ORIGINS_REGEX: regex covering dynamic preview URLs. Defaults to all
# talentgram-frontend Vercel preview deployments. Set to "disabled" to turn off.
cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

# Ensure the main production frontend origins are explicitly allowed by default
default_origins = [
    "https://talentgramagency.com",
    "https://www.talentgramagency.com",
    "https://apply.talentgramagency.com",
    "https://submit.talentgramagency.com",
    "https://review.talentgramagency.com",
    "https://links.talentgramagency.com"
]
for origin in default_origins:
    if origin not in cors_origins:
        cors_origins.append(origin)

cors_origins_regex = os.environ.get(
    "CORS_ORIGINS_REGEX",
    r"https://talentgram-frontend-.*\.vercel\.app|https://.*\.talentgramagency\.com",
).strip()

if cors_origins_regex in ("", "disabled", "None", "none"):
    cors_origins_regex = None

logger.info("Active CORS origins: %s", cors_origins)
if cors_origins_regex:
    logger.info("Active CORS origin regex: %s", cors_origins_regex)

# Register security headers first
app.add_middleware(SecurityHeadersMiddleware)

# Add CORS Middleware next so it becomes the outer-most middleware (last added = first to run on request)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=cors_origins,
    allow_origin_regex=cors_origins_regex,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
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


async def run_draft_talent_migration():
    logger.info("== STARTING DRAFT TALENT CLEANUP & MIGRATION CHECK ==")
    cursor = db.talents.find({})
    talents = await cursor.to_list(length=10000)
    migrated_count = 0
    
    for t in talents:
        email = t.get("email")
        if not email:
            continue
            
        sub = await db.submissions.find_one({
            "talent_email": email,
            "status": {"$in": ["submitted", "updated", "shortlisted", "selected", "rejected"]}
        })
        app = await db.applications.find_one({
            "talent_email": email,
            "status": {"$in": ["submitted", "updated", "shortlisted", "selected", "rejected"]}
        })
        
        if not sub and not app:
            draft_id = t.get("id")
            existing_draft = await db.submission_drafts.find_one({"email": email})
            if not existing_draft:
                new_draft = {
                    "draft_id": draft_id,
                    "project_id": "apply",
                    "email": email,
                    "google_id": t.get("google_id"),
                    "draft_status": "draft",
                    "form_data": {
                        "first_name": t.get("name", "").split(" ")[0] if t.get("name") else "",
                        "last_name": " ".join(t.get("name", "").split(" ")[1:]) if t.get("name") else "",
                        "email": email,
                        "phone": t.get("phone", ""),
                        "location": t.get("location", []),
                        "dob": t.get("dob", ""),
                        "gender": t.get("gender", ""),
                        "skills": t.get("skills", []),
                        "work_links": t.get("work_links", []),
                        "bio": t.get("bio", ""),
                    },
                    "created_at": t.get("created_at") or datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
                await db.submission_drafts.insert_one(new_draft)
            
            await db.talents.delete_one({"id": t["id"]})
            migrated_count += 1
            logger.info("Migrated unsubmitted draft talent %s to submission_drafts", email)
            
    res = await db.talents.update_many(
        {"status": {"$exists": False}},
        {"$set": {"status": "SUBMITTED"}}
    )
    logger.info("Updated %d existing talents to status='SUBMITTED'", res.modified_count)
    logger.info("== DRAFT TALENT CLEANUP & MIGRATION COMPLETED (migrated %d) ==", migrated_count)


async def run_draft_expiration_and_backfill():
    logger.info("== STARTING DRAFT EXPIRATION & SUBMISSION METRICS BACKFILL ===")
    
    limit_date = datetime.now(timezone.utc) - timedelta(days=30)
    limit_date_iso = limit_date.isoformat()
    
    drafts_query = {
        "$or": [
            {"updated_at": {"$lt": limit_date_iso}},
            {"updated_at": {"$lt": limit_date}},
            {"created_at": {"$lt": limit_date_iso}},
            {"created_at": {"$lt": limit_date}}
        ]
    }
    res_drafts = await db.submission_drafts.delete_many(drafts_query)
    logger.info("Expired/Deleted %d submission drafts older than 30 days", res_drafts.deleted_count)
    
    submissions_query = {
        "status": "draft",
        "$or": [
            {"updated_at": {"$lt": limit_date_iso}},
            {"updated_at": {"$lt": limit_date}},
            {"created_at": {"$lt": limit_date_iso}},
            {"created_at": {"$lt": limit_date}}
        ]
    }
    res_subs = await db.submissions.delete_many(submissions_query)
    logger.info("Expired/Deleted %d draft submissions older than 30 days", res_subs.deleted_count)
    
    cursor = db.talents.find({})
    talents = await cursor.to_list(length=10000)
    backfilled_count = 0
    
    for t in talents:
        email = t.get("email")
        if not email:
            email = (t.get("source") or {}).get("talent_email")
            
        if not email:
            continue
            
        sub_cursor = db.submissions.find({
            "talent_email": email.lower().strip(),
            "status": {"$ne": "draft"}
        }).sort("submitted_at", 1)
        subs = await sub_cursor.to_list(length=1000)
        
        if not subs:
            if t.get("total_submissions") == 0:
                continue
            await db.talents.update_one(
                {"id": t["id"]},
                {"$set": {
                    "first_submission_at": None,
                    "last_submission_at": None,
                    "total_submissions": 0
                }}
            )
            backfilled_count += 1
            continue
            
        submitted_dates = []
        for s in subs:
            dt = s.get("submitted_at") or s.get("created_at")
            if dt:
                submitted_dates.append(dt)
                
        if submitted_dates:
            first_sub = min(submitted_dates)
            last_sub = max(submitted_dates)
        else:
            first_sub = None
            last_sub = None
            
        await db.talents.update_one(
            {"id": t["id"]},
            {"$set": {
                "first_submission_at": first_sub,
                "last_submission_at": last_sub,
                "total_submissions": len(subs)
            }}
        )
        backfilled_count += 1
        
    logger.info("Recalculated/Backfilled submission metrics for %d talents", backfilled_count)
    logger.info("== DRAFT EXPIRATION & SUBMISSION METRICS BACKFILL COMPLETED ==")


@app.on_event("startup")
async def on_startup():
    try:
        logger.info("Starting Talentgram backend...")
        validate_talent_fields_classification()

        await run_media_duplicate_cleanup_migration()
        await run_draft_talent_migration()
        await run_draft_expiration_and_backfill()

        await seed_admin()
        logger.info("Admin seed complete")

        await ensure_notifications_indexes(db)
        logger.info("Notification indexes ready")

        await db.client_states.create_index(
            [("link_id", 1), ("viewer_email", 1)], unique=True
        )

        # OTP login indexes
        try:
            await db.otp_codes.create_index([("email", 1), ("used", 1)])
            await db.otp_codes.create_index("expires_at", expireAfterSeconds=0)
            await db.otp_audit_logs.create_index([("email", 1), ("timestamp", -1)])
            await db.otp_audit_logs.create_index([("ip_address", 1), ("timestamp", -1)])
            logger.info("OTP verification indexes ready")
        except Exception as _e:
            logger.warning("OTP indexes creation failed: %s", _e)

        await db.feedback.create_index([("submission_id", 1), ("status", 1)])
        await db.feedback.create_index([("project_id", 1), ("status", 1)])
        await db.feedback.create_index([("created_at", -1)])

        # Marketing Hub / CRM indexes
        await db.clients.create_index([("last_contacted_date", -1)])
        # WhatsApp CRM targeting (Slice 2): filter by contact_type / tags at scale.
        await db.clients.create_index([("contact_type", 1)])
        await db.clients.create_index([("tags", 1)])
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
            # AI Scout Capture — dedup lookups + audit trail
            await db.workflow_scouts.create_index([("instagram_username", 1)])
            await db.workflow_scouts.create_index([("phone", 1)])
            await db.scout_capture_audit.create_index([("created_at", -1)])
            await db.scout_capture_audit.create_index([("user_id", 1), ("created_at", -1)])
        except Exception as _e:
            logger.warning("workflow indexes: %s", _e)

        logger.info("Mongo indexes ready")

        # WhatsApp Engine — indexes, default templates, config defaults
        try:
            await whatsapp.ensure_whatsapp_ready()
        except Exception as _e:
            logger.warning("WhatsApp Engine startup init failed (non-fatal): %s", _e)

        # AI Scout Capture — warm EasyOCR in the background so the first user
        # request doesn't pay model download/load latency. Non-blocking (boot +
        # Railway health stay fast) and non-fatal (capture still lazy-loads).
        try:
            asyncio.create_task(scout_capture.warmup())
        except Exception as _e:
            logger.warning("OCR warmup scheduling failed (non-fatal): %s", _e)

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