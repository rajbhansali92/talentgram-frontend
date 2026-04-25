"""Google Drive user-OAuth bootstrap.

One-time admin consent flow used to bypass the service-account zero-quota
limitation. Once the admin consents, we store the refresh_token in Mongo
and Drive uploads stream into the user's personal Drive (counting against
their free 15 GB quota).

Endpoints:
  GET  /api/admin/drive/oauth/start    — admin-only; returns Google consent URL
  GET  /api/admin/drive/oauth/callback — Google redirects here; stores refresh token
  GET  /api/admin/drive/status         — admin-only; UI uses to show "Connected as ..."
  POST /api/admin/drive/oauth/disconnect — admin-only; nukes the stored creds
  POST /api/admin/drive/retry          — admin-only; clears terminal flags so the
                                         retry sweep re-uploads previously-failed media
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from core import current_user, db, require_role
from drive_backup import (
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_REDIRECT_URI,
    SCOPES,
    clear_drive_service,
    drive_enabled,
    oauth_configured,
)

router = APIRouter(prefix="/api/admin/drive", tags=["drive-admin"])
logger = logging.getLogger(__name__)


def _flow() -> Flow:
    if not oauth_configured():
        raise HTTPException(503, "Google OAuth not configured on the server")
    return Flow.from_client_config(
        {
            "web": {
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [OAUTH_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )


@router.get("/oauth/start")
async def oauth_start(_: dict = Depends(require_role("admin"))):
    """Return the consent URL the admin must visit. `prompt=consent` forces
    a fresh refresh_token even if the user previously authorised."""
    flow = _flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    # Persist the state for CSRF protection — single value is enough for this
    # single-admin flow; if multi-admin needed, key by user.id.
    await db.drive_oauth_state.update_one(
        {"_id": "primary"},
        {"$set": {"state": state, "created_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"authorization_url": authorization_url}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Google redirects the admin's browser here after consent. We do NOT
    require a JWT here (the redirect comes from Google, not the SPA) — CSRF
    protection comes from validating the `state` value against the one we
    stored in `oauth_start`."""
    if error:
        return HTMLResponse(_html_message(False, f"Google returned an error: {error}"), status_code=400)
    if not code or not state:
        return HTMLResponse(_html_message(False, "Missing code/state"), status_code=400)
    stored = await db.drive_oauth_state.find_one({"_id": "primary"})
    if not stored or stored.get("state") != state:
        return HTMLResponse(_html_message(False, "Invalid state — please retry"), status_code=400)

    flow = _flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        return HTMLResponse(_html_message(False, f"Token exchange failed: {e}"), status_code=400)

    creds: UserCredentials = flow.credentials
    if not creds.refresh_token:
        return HTMLResponse(
            _html_message(
                False,
                "No refresh token returned by Google. Please revoke the app at "
                "myaccount.google.com/permissions and retry — we need offline access.",
            ),
            status_code=400,
        )

    # Look up the connected email so admins can see which account is linked.
    connected_email = "unknown"
    try:
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = svc.about().get(fields="user(emailAddress,displayName)").execute()
        connected_email = about.get("user", {}).get("emailAddress") or connected_email
    except Exception as e:
        logger.warning("Drive about() lookup failed: %s", e)

    await db.drive_oauth.update_one(
        {"_id": "primary"},
        {"$set": {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "scopes": creds.scopes,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "connected_email": connected_email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    await db.drive_oauth_state.delete_one({"_id": "primary"})

    # New credentials in place — drop cached service so next upload picks them up.
    clear_drive_service()

    # Auto-clear terminal failures so the retry sweep re-uploads previous failures.
    res = await db.drive_upload_failures.update_many(
        {"terminal": True},
        {"$set": {"terminal": False, "retry_count": 0}},
    )
    logger.info(
        "Drive OAuth connected as %s, requeued %d previously-terminal media",
        connected_email, res.modified_count,
    )

    return HTMLResponse(_html_message(True, f"Connected as {connected_email}"))


@router.get("/status")
async def drive_status(_: dict = Depends(require_role("admin"))):
    """Status JSON the admin UI uses to render the Connect / Connected pill."""
    enabled = drive_enabled()
    oauth_doc = await db.drive_oauth.find_one(
        {"_id": "primary"}, {"_id": 0, "access_token": 0, "refresh_token": 0}
    )
    pending = await db.drive_upload_failures.count_documents({"terminal": {"$ne": True}})
    terminal = await db.drive_upload_failures.count_documents({"terminal": True})
    return {
        "enabled": enabled,
        "oauth_configured": oauth_configured(),
        "connected": bool(oauth_doc and oauth_doc.get("connected_email")),
        "connected_email": (oauth_doc or {}).get("connected_email"),
        "updated_at": (oauth_doc or {}).get("updated_at"),
        "pending_retries": pending,
        "terminal_failures": terminal,
        "parent_folder_id": os.environ.get("GOOGLE_DRIVE_PARENT_FOLDER_ID"),
    }


@router.post("/oauth/disconnect")
async def disconnect(_: dict = Depends(require_role("admin"))):
    await db.drive_oauth.delete_one({"_id": "primary"})
    clear_drive_service()
    return {"ok": True}


@router.post("/retry")
async def retry_now(_: dict = Depends(require_role("admin"))):
    """Manual trigger — clears the `terminal` flag on every queued failure so
    the next sweep retries them."""
    res = await db.drive_upload_failures.update_many(
        {},
        {"$set": {"terminal": False, "retry_count": 0}},
    )
    return {"ok": True, "requeued": res.modified_count}


def _html_message(success: bool, body: str) -> str:
    """Tiny, brand-matched landing page so the admin lands somewhere sensible
    after the Google redirect (they don't end up on a raw JSON blob)."""
    color = "#0F0" if success else "#FF3B30"
    title = "Drive Connected" if success else "Connection Failed"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{margin:0;background:#050505;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}}
.card{{max-width:480px;text-align:center}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};margin-bottom:24px;box-shadow:0 0 30px {color}}}
h1{{font-size:32px;font-weight:300;letter-spacing:-0.02em;margin:0 0 12px}}
p{{color:rgba(255,255,255,.7);font-size:14px;line-height:1.6;margin:0 0 32px}}
a{{display:inline-block;background:#fff;color:#000;padding:12px 24px;border-radius:2px;text-decoration:none;font-size:13px;letter-spacing:0.05em}}
</style></head>
<body><div class="card">
<div class="dot"></div>
<h1>{title}</h1>
<p>{body}</p>
<a href="/admin">Back to admin</a>
</div></body></html>"""
