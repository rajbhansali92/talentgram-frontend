from fastapi import APIRouter, Request, HTTPException, Header
import logging
import uuid
import json
from datetime import datetime, timezone
import cloudinary.utils
from core import db, video_poster_url, _now

logger = logging.getLogger("talentgram")
router = APIRouter()

@router.post("/public/webhooks/cloudinary")
async def cloudinary_webhook(
    request: Request,
    x_cld_signature: str = Header(None),
    x_cld_timestamp: str = Header(None)
):
    """
    HTTP POST Webhook handler for Cloudinary eager transformation callbacks.
    Validates Cloudinary signature and updates MongoDB media status.
    """
    # 1. Signature Verification
    if not x_cld_signature or not x_cld_timestamp:
        logger.warning("Cloudinary Webhook: missing X-Cld-Signature or X-Cld-Timestamp header")
        raise HTTPException(status_code=401, detail="Invalid Cloudinary signature")

    try:
        timestamp_int = int(x_cld_timestamp)
    except (ValueError, TypeError):
        logger.warning(f"Cloudinary Webhook: invalid X-Cld-Timestamp format: {x_cld_timestamp}")
        raise HTTPException(status_code=401, detail="Invalid Cloudinary signature")

    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    is_valid = False
    try:
        is_valid = cloudinary.utils.verify_notification_signature(
            body_str,
            timestamp_int,
            x_cld_signature
        )
    except Exception as e:
        logger.error(f"Cloudinary Webhook: signature validation check threw an exception: {e}")

    if not is_valid:
        logger.warning("Cloudinary Webhook: signature validation failed")
        raise HTTPException(status_code=401, detail="Invalid Cloudinary signature")

    # 2. JSON Ingestion
    try:
        payload = json.loads(body_str)
    except Exception as e:
        logger.error(f"Cloudinary Webhook: failed to parse JSON payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 3. Detect Failure Conditions
    is_failed = False
    failure_reason = "Unknown Cloudinary transcode failure"

    if payload.get("status") == "failed":
        is_failed = True
        failure_reason = payload.get("status_description") or "Cloudinary upload failed status"

    if "error" in payload:
        is_failed = True
        failure_reason = payload["error"].get("message") or "Cloudinary general error"

    eager_results = payload.get("eager") or []
    for eager_res in eager_results:
        if eager_res.get("status") == "failed":
            is_failed = True
            failure_reason = eager_res.get("error", {}).get("message") or "Eager transformation failed"
            break

    # Extract tags mapping to MongoDB documents
    tags_list = payload.get("tags") or []
    tags = {}
    for t in tags_list:
        if "=" in t:
            k, v = t.split("=", 1)
            tags[k] = v

    media_id = tags.get("media_id")
    scope = tags.get("scope")
    parent_id = tags.get("parent_id")
    category = tags.get("category")
    label = tags.get("label")

    if not media_id or not scope or not parent_id:
        logger.warning(f"Cloudinary Webhook: missing mapping tags in payload. Tags: {tags_list}")
        return {"status": "ignored_missing_tags"}

    cloudinary_public_id = payload.get("public_id")
    secure_url = None
    for result in eager_results:
        if result.get("secure_url") and result.get("secure_url").endswith(".mp4"):
            secure_url = result.get("secure_url")
            break

    if not secure_url:
        secure_url = payload.get("secure_url") or payload.get("url")

    bytes_size = payload.get("bytes") or 0
    duration = payload.get("duration")

    # 4. State Modification
    if scope == "submission":
        if is_failed:
            logger.warning(f"Cloudinary Webhook: reporting failure for submission={parent_id}, media_id={media_id}: {failure_reason}")
            res = await db.submissions.update_one(
                {"id": parent_id, "media.id": media_id},
                {"$set": {
                    "media.$.status": "failed",
                    "media.$.failed_at": datetime.now(timezone.utc),
                    "media.$.failure_reason": failure_reason,
                }}
            )
            try:
                await db.asset_metadata.update_one(
                    {"submission_id": parent_id, "category": category},
                    {"$set": {"upload_status": "failed", "updated_at": datetime.now(timezone.utc)}},
                )
            except Exception as e:
                logger.warning(f"Cloudinary Webhook: failed asset_metadata write: {e}")
        else:
            res = await db.submissions.update_one(
                {"id": parent_id, "media.id": media_id},
                {"$set": {
                    "media.$.url": secure_url,
                    "media.$.thumbnail_url": video_poster_url(cloudinary_public_id),
                    "media.$.poster_url": video_poster_url(cloudinary_public_id),
                    "media.$.duration": duration,
                    "media.$.size": bytes_size,
                    "media.$.status": "completed",
                }}
            )
            if res.modified_count == 0:
                logger.warning(f"Cloudinary Webhook: could not find matching media_id={media_id} in submission={parent_id}")
            else:
                logger.info(f"Cloudinary Webhook: updated media record for submission={parent_id}")

            try:
                await db.asset_metadata.update_one(
                    {"submission_id": parent_id, "category": category},
                    {"$set": {"upload_status": "completed", "updated_at": datetime.now(timezone.utc)}},
                )
            except Exception as e:
                logger.warning(f"Cloudinary Webhook: asset_metadata update failed: {e}")

            if category == "intro_video":
                # Find and clean up previous intro videos deferred from replacement
                sub_doc = await db.submissions.find_one(
                    {"id": parent_id}, {"media": 1, "status": 1, "submitted_at": 1}
                )
                if sub_doc and "media" in sub_doc:
                    prev_items = [m for m in sub_doc.get("media", []) if m.get("category") == "intro_video" and m.get("id") != media_id]
                    if prev_items:
                        # Issue 2: only mirror removals into the global profile
                        # while the submission is still ORIGINAL. Replacing an
                        # intro video on an already-submitted submission is a
                        # resubmission/edit and must not mutate the global
                        # profile. `submitted_at` is set on first finalize and
                        # never cleared, so it robustly identifies edits across
                        # all workflows.
                        already_submitted = bool(sub_doc.get("submitted_at")) or \
                            sub_doc.get("status") in ("submitted", "updated")
                        await db.submissions.update_one(
                            {"id": parent_id},
                            {"$pull": {"media": {"id": {"$in": [pi["id"] for pi in prev_items]}}}}
                        )
                        for pi in prev_items:
                            from core import cleanup_media_storage, remove_synced_media_from_global_talent
                            op_id = tags.get("operation_id") or str(uuid.uuid4())
                            await cleanup_media_storage(pi, scope="submission", parent_id=parent_id, operation_id=op_id)
                            if not already_submitted:
                                await remove_synced_media_from_global_talent(sub_doc, pi["id"])

    elif scope == "application":
        if is_failed:
            logger.warning(f"Cloudinary Webhook: reporting failure for application={parent_id}, media_id={media_id}: {failure_reason}")
            res = await db.applications.update_one(
                {"id": parent_id, "media.id": media_id},
                {"$set": {
                    "media.$.status": "failed",
                    "media.$.failed_at": datetime.now(timezone.utc),
                    "media.$.failure_reason": failure_reason,
                }}
            )
            try:
                await db.asset_metadata.update_one(
                    {"application_id": parent_id, "category": category},
                    {"$set": {"upload_status": "failed", "updated_at": datetime.now(timezone.utc)}},
                )
            except Exception as e:
                logger.warning(f"Cloudinary Webhook: failed asset_metadata write: {e}")
        else:
            res = await db.applications.update_one(
                {"id": parent_id, "media.id": media_id},
                {"$set": {
                    "media.$.url": secure_url,
                    "media.$.thumbnail_url": video_poster_url(cloudinary_public_id),
                    "media.$.poster_url": video_poster_url(cloudinary_public_id),
                    "media.$.duration": duration,
                    "media.$.size": bytes_size,
                    "media.$.status": "completed",
                }}
            )
            if res.modified_count == 0:
                logger.warning(f"Cloudinary Webhook: could not find matching media_id={media_id} in application={parent_id}")
            else:
                logger.info(f"Cloudinary Webhook: updated media record for application={parent_id}")

            try:
                await db.asset_metadata.update_one(
                    {"application_id": parent_id, "category": category},
                    {"$set": {"upload_status": "completed", "updated_at": datetime.now(timezone.utc)}},
                )
            except Exception as e:
                logger.warning(f"Cloudinary Webhook: asset_metadata update failed: {e}")

            try:
                updated_doc = await db.applications.find_one({"id": parent_id})
                if updated_doc:
                    updated_media = None
                    for m in updated_doc.get("media", []):
                        if m.get("id") == media_id:
                            updated_media = m
                            break
                    if updated_media:
                        from core import sync_media_to_global_talent
                        await sync_media_to_global_talent(updated_doc, updated_media)
                        logger.info(f"Cloudinary Webhook: mirrored media_id={media_id} to global talent")
            except Exception as e:
                logger.error(f"Cloudinary Webhook: sync to global talent failed: {e}", exc_info=True)

    return {"status": "ok"}
