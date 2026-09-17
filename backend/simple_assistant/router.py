"""Simple Assistant HTTP surface.

  GET  /overview            read-only situational awareness (Phase 1)
  POST /command             understand → resolve → preview (Phases 2–4A)
  POST /command/confirm     execute an HMAC-signed plan:
                              - confirm_plan  → controlled pipeline mutation (Phase 3)
                              - confirm_comm  → hand off to the existing WhatsApp
                                                Engine job queue (Phase 4A)
  POST /command/comm-status reconcile the worker's real per-job delivery outcome

All routes require the same ``current_team_or_admin`` JWT dependency every
other admin-plane endpoint uses, and are gated behind
``SIMPLE_ASSISTANT_ENABLED`` (off by default → 404). Destination + media
for a send are ALWAYS re-derived server-side from the signed plan — never
taken from the client payload.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from core import current_team_or_admin
from simple_assistant import is_enabled
from simple_assistant.comm_execute import comm_status, confirm_and_send
from simple_assistant.commands import _verified_pending, run_command
from simple_assistant.execute import confirm_and_execute
from simple_assistant.service import build_overview
from simple_assistant.submission_execute import confirm_and_attach, confirm_and_ingest

# incoming-audition file transport is a plain admin-authed multipart POST that
# delegates to the canonical submission upload service — NOT a second upload
# implementation. Cap the multipart body at the same ceiling the submission
# video upload enforces.
from core import MAX_SUBMISSION_VIDEO_BYTES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple-assistant", tags=["Simple Assistant"])


def _require_enabled() -> None:
    if not is_enabled():
        raise HTTPException(status_code=404, detail="Not found")


class FileMeta(BaseModel):
    filename: Optional[str] = Field(default=None, max_length=512)
    size: Optional[int] = Field(default=None, ge=0)
    content_type: Optional[str] = Field(default=None, max_length=255)
    sha256: Optional[str] = Field(default=None, max_length=128)


class CommandIn(BaseModel):
    message: str = Field(default="", max_length=8000)
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    context: Optional[Dict[str, Any]] = None
    # metadata ONLY for a file the user has attached in the Assistant UI —
    # name/size/type/hash. The bytes never travel on /command; they arrive
    # once, at /command/upload, only after the user confirms the preview.
    file_meta: Optional[FileMeta] = None


class ConfirmIn(BaseModel):
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    context: Optional[Dict[str, Any]] = None
    # Phase 9 — the (optionally edited) final AI reply text. Re-validated
    # server-side against the freshly-rebuilt verified context; the browser
    # can never bypass validation.
    final_text: Optional[str] = Field(default=None, max_length=2000)


class CommStatusIn(BaseModel):
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    plan_id: str = Field(..., max_length=128)
    batch_ids: List[str] = Field(default_factory=list)
    status_token: str = Field(..., max_length=256)


@router.get("/overview")
async def overview(
    _: None = Depends(_require_enabled),
    user: dict = Depends(current_team_or_admin),
):
    try:
        return await build_overview(user)
    except Exception:
        logger.exception("simple-assistant overview build failed")
        raise HTTPException(status_code=502, detail="Could not assemble overview")


@router.post("/command")
async def command(
    payload: CommandIn,
    _: None = Depends(_require_enabled),
    user: dict = Depends(current_team_or_admin),
):
    """Understand → resolve → preview. Never sends or mutates."""
    try:
        return await run_command(
            message=payload.message,
            conversation_id=payload.conversation_id,
            context=payload.context,
            user=user,
            file_meta=payload.file_meta.dict() if payload.file_meta else None,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("simple-assistant command failed")
        raise HTTPException(status_code=502, detail="Could not process that command")


@router.post("/command/confirm")
async def command_confirm(
    payload: ConfirmIn,
    _: None = Depends(_require_enabled),
    user: dict = Depends(current_team_or_admin),
):
    """Execute the approved, HMAC-signed plan. Dispatches on the signed
    plan's own kind — a pipeline plan goes to the Phase-3 executor, a
    communication plan is re-resolved server-side and handed to the
    existing WhatsApp Engine. Only explicitly enabled action types run."""
    kind = (_verified_pending(payload.context) or {}).get("kind")
    try:
        if kind == "confirm_comm":
            return await confirm_and_send(
                conversation_id=payload.conversation_id, context=payload.context, user=user,
            )
        if kind == "confirm_submission":
            return await confirm_and_attach(
                conversation_id=payload.conversation_id, context=payload.context, user=user,
            )
        if kind == "confirm_upload":
            raise HTTPException(
                status_code=400,
                detail="This plan uploads a file — send it to /command/upload with the audition video attached.",
            )
        if kind == "confirm_ai_response":
            from simple_assistant.ai_response_execute import confirm_and_send_ai
            return await confirm_and_send_ai(
                conversation_id=payload.conversation_id, context=payload.context,
                user=user, final_text=payload.final_text,
            )
        return await confirm_and_execute(
            conversation_id=payload.conversation_id, context=payload.context, user=user,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("simple-assistant confirm failed")
        raise HTTPException(status_code=502, detail="Could not complete that action")


@router.post("/command/comm-status")
async def command_comm_status(
    payload: CommStatusIn,
    _: None = Depends(_require_enabled),
    user: dict = Depends(current_team_or_admin),
):
    """Reconcile the worker's real per-job delivery outcome. `status_token`
    is the HMAC issued by the confirm response — a client cannot query
    arbitrary batch ids."""
    return await comm_status(
        plan_id=payload.plan_id, batch_ids=payload.batch_ids, status_token=payload.status_token,
    )


@router.post("/command/upload")
async def command_upload(
    context: str = Form(...),
    conversation_id: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    _: None = Depends(_require_enabled),
    user: dict = Depends(current_team_or_admin),
):
    """Execute a signed INGEST plan. The audition file's bytes arrive here
    (multipart) exactly once, only after the user confirmed the preview.
    ``context`` is the JSON-encoded HMAC-signed plan the /command response
    returned — the destination talent/project/submission and the pinned
    SHA-256 are re-verified server-side; the client payload is not trusted.
    """
    try:
        ctx = json.loads(context) if context else None
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed plan context")
    if not isinstance(ctx, dict):
        raise HTTPException(status_code=400, detail="Malformed plan context")

    # Reject an oversized body before reading it — same ceiling as the
    # canonical submission video upload.
    if (file.size or 0) > MAX_SUBMISSION_VIDEO_BYTES:
        cap = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"Video is too large. Max {cap} MB.")

    try:
        return await confirm_and_ingest(
            conversation_id=conversation_id, context=ctx, user=user, file=file,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("simple-assistant audition upload failed")
        raise HTTPException(status_code=502, detail="Could not complete that upload")


@router.get("/health")
async def health(_: None = Depends(_require_enabled)):
    return {"status": "ok", "module": "simple_assistant"}
