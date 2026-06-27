from fastapi import APIRouter, Request, HTTPException, Header
import logging
import json
import os
import hmac
import hashlib
from datetime import datetime, timezone
from core import db, _now

logger = logging.getLogger("talentgram")
router = APIRouter()

def verify_cloudflare_signature(body_bytes: bytes, webhook_signature: str, secret: str) -> bool:
    if not webhook_signature or not secret:
        return False
    try:
        parts = {p.split("=")[0]: p.split("=")[1] for p in webhook_signature.split(",")}
        timestamp = parts.get("time")
        sig1 = parts.get("sig1")
        if not timestamp or not sig1:
            return False
        
        # Cloudflare computes signature as: hmac_sha256(timestamp + "." + body_bytes, secret)
        to_sign = f"{timestamp}.".encode("utf-8") + body_bytes
        computed = hmac.new(secret.encode("utf-8"), to_sign, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, sig1)
    except Exception as e:
        logger.error(f"[Cloudflare Stream Webhook] Signature parsing failed: {e}")
        return False

@router.post("/public/webhooks/cloudflare-stream")
async def cloudflare_stream_webhook(
    request: Request,
    webhook_signature: str = Header(None, alias="Webhook-Signature")
):
    secret = os.environ.get("CLOUDFLARE_STREAM_WEBHOOK_SECRET")
    body_bytes = await request.body()
    
    # 1. Verify webhook signature if secret is configured
    if secret:
        if not verify_cloudflare_signature(body_bytes, webhook_signature, secret):
            logger.warning("[Cloudflare Stream Webhook] Invalid signature received")
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        logger.warning("[Cloudflare Stream Webhook] Webhook secret not configured in env, bypassing signature check")

    # 2. Parse body JSON
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"[Cloudflare Stream Webhook] Failed to decode body JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    uid = payload.get("uid")
    status_info = payload.get("status") or {}
    state = status_info.get("state") # e.g. "ready", "processing", "error"
    meta = payload.get("meta") or {}
    
    media_id = meta.get("media_id")
    parent_id = meta.get("parent_id")
    scope = meta.get("scope")
    category = meta.get("category")
    
    if not media_id or not parent_id or not scope:
        logger.info(f"[Cloudflare Stream Webhook] Non-Talentgram event or missing metadata: uid={uid}")
        return {"status": "ignored"}

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "default")
    playback_url = f"https://customer-{account_id}.cloudflarestream.com/{uid}/manifest/video.m3u8"
    thumbnail_url = f"https://customer-{account_id}.cloudflarestream.com/{uid}/thumbnails/thumbnail.jpg"

    # 3. Handle processing outcome
    if state == "ready":
        duration = payload.get("duration") or 0.0
        bytes_size = payload.get("size") or 0
        
        update_fields = {
            "media.$.url": playback_url,
            "media.$.thumbnail_url": thumbnail_url,
            "media.$.poster_url": thumbnail_url,
            "media.$.duration": duration,
            "media.$.size": bytes_size,
            "media.$.status": "completed",
            "media.$.stream_uid": uid,
            "media.$.provider": "stream"
        }
        
        if scope == "submission":
            res = await db.submissions.update_one(
                {"id": parent_id, "media.id": media_id},
                {"$set": update_fields}
            )
            logger.info(f"[Cloudflare Stream Webhook] Marked submission={parent_id} media={media_id} as completed")
        elif scope == "application":
            res = await db.applications.update_one(
                {"id": parent_id, "media.id": media_id},
                {"$set": update_fields}
            )
            # Sync application media to global talent
            try:
                updated_doc = await db.applications.find_one({"id": parent_id})
                if updated_doc:
                    updated_media = next((m for m in updated_doc.get("media", []) if m.get("id") == media_id), None)
                    if updated_media:
                        from core import sync_media_to_global_talent
                        await sync_media_to_global_talent(updated_doc, updated_media)
            except Exception as e:
                logger.error(f"[Cloudflare Stream Webhook] Syncing application media to global talent failed: {e}")
            logger.info(f"[Cloudflare Stream Webhook] Marked application={parent_id} media={media_id} as completed")

    elif state in {"error", "failed"}:
        err_msg = status_info.get("errorReason") or "Cloudflare transcode failed"
        update_fields = {
            "media.$.status": "failed",
            "media.$.failed_at": datetime.now(timezone.utc),
            "media.$.failure_reason": err_msg
        }
        if scope == "submission":
            await db.submissions.update_one({"id": parent_id, "media.id": media_id}, {"$set": update_fields})
        elif scope == "application":
            await db.applications.update_one({"id": parent_id, "media.id": media_id}, {"$set": update_fields})
        logger.warning(f"[Cloudflare Stream Webhook] Video uid={uid} failed processing: {err_msg}")

    return {"status": "ok"}
