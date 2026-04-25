"""Talentgram Portfolio Engine — FastAPI bootstrap.

All route logic lives in `routers/`. Shared primitives (config, DB, security,
storage, utils, constants, models, visibility filters) live in `core.py`.
"""
import asyncio
import logging
import os

from fastapi import APIRouter, FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from core import db, get_object, init_storage, mongo_client, seed_admin
from drive_backup import attach_db, drive_enabled, retry_pending_uploads, start_drive_worker
from notifications import ensure_indexes as ensure_notifications_indexes
from routers import (
    applications,
    auth,
    drive_admin,
    feedback,
    links,
    notifications as notifications_router,
    password,
    projects,
    submissions,
    talents,
    users,
)

app = FastAPI(title="Talentgram Portfolio Engine")
logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth headers to reduce XSS/clickjacking blast radius.

    Notes:
    - CSP is deliberately permissive for the API (no HTML rendered here). The
      React SPA is served by its own host; this middleware hardens direct
      backend responses (JSON + media streams).
    - `frame-ancestors 'none'` blocks clickjacking of any JSON response.
    """

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
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'",
        )
        return response


# Health / meta
_meta = APIRouter(prefix="/api")


@_meta.get("/")
async def root():
    return {"app": "talentgram", "ok": True}


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
app.include_router(feedback.router)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await seed_admin()
    init_storage()
    await ensure_notifications_indexes(db)
    # client_states (M5 client viewing-intelligence) — fast lookup per (link, viewer).
    await db.client_states.create_index(
        [("link_id", 1), ("viewer_email", 1)], unique=True
    )
    # feedback — talent-facing query needs a fast (submission_id, status, visibility) lookup.
    await db.feedback.create_index([("submission_id", 1), ("status", 1)])
    await db.feedback.create_index([("project_id", 1), ("status", 1)])
    await db.feedback.create_index([("created_at", -1)])
    if drive_enabled():
        logger.info("Google Drive backup ENABLED — starting retry worker")
        attach_db(db)
        start_drive_worker()
        asyncio.create_task(_drive_retry_loop())
    else:
        logger.info("Google Drive backup DISABLED (env vars not set)")


async def _drive_retry_loop():
    """Periodic background task — retries any failed Drive uploads every
    5 minutes. In-process, single-instance — safe given current deployment
    topology. If the queue is empty the call is essentially a no-op."""
    await asyncio.sleep(60)  # let the app finish booting before first sweep
    while True:
        try:
            await retry_pending_uploads(db, get_object)
        except Exception as e:
            logger.warning("drive retry loop error: %s", e)
        await asyncio.sleep(5 * 60)


@app.on_event("shutdown")
async def on_shutdown():
    mongo_client.close()
