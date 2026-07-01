import os
import logging
import httpx
import hmac
import hashlib
from datetime import datetime, timezone
import cloudinary.uploader
import cloudinary.utils
import cloudinary.api

logger = logging.getLogger("talentgram")

class VideoProvider:
    async def create_processing_job(
        self,
        parent_id: str,
        media_id: str,
        category: str,
        scope: str,
        r2_url: str,
        folder: str,
        public_id: str,
        label: str = None,
        eager_transformation: str = None,
        operation_id: str = None,
    ) -> dict:
        raise NotImplementedError()

    async def handle_webhook(self, payload: dict, signature: str = None, timestamp: str = None) -> dict:
        raise NotImplementedError()


class CloudinaryProvider(VideoProvider):
    async def create_processing_job(
        self,
        parent_id: str,
        media_id: str,
        category: str,
        scope: str,
        r2_url: str,
        folder: str,
        public_id: str,
        label: str = None,
        eager_transformation: str = None,
        operation_id: str = None,
    ) -> dict:
        backend_url = os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/")
        if not backend_url:
            r_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
            if r_domain:
                backend_url = f"https://{r_domain}"
        webhook_url = f"{backend_url}/public/webhooks/cloudinary" if backend_url else None

        options = {
            "folder": folder,
            "public_id": public_id,
            "resource_type": "video",
            "overwrite": True,
        }
        if eager_transformation:
            options["eager"] = eager_transformation
            options["eager_async"] = True
        if webhook_url:
            options["notification_url"] = webhook_url

        tags = [
            f"media_id={media_id}",
            f"scope={scope}",
            f"parent_id={parent_id}",
            f"category={category}",
        ]
        if label:
            tags.append(f"label={label}")
        if operation_id:
            tags.append(f"operation_id={operation_id}")
        options["tags"] = ",".join(tags)

        try:
            logger.info(f"[CloudinaryProvider] Triggering fetch for media_id={media_id} | OpID: {operation_id}")
            res = cloudinary.uploader.upload(r2_url, **options)
            return {"ok": True, "provider_data": res}
        except Exception as e:
            logger.error(f"[CloudinaryProvider] Failed to enqueue transcode: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}


class CloudflareStreamProvider(VideoProvider):
    async def create_processing_job(
        self,
        parent_id: str,
        media_id: str,
        category: str,
        scope: str,
        r2_url: str,
        folder: str,
        public_id: str,
        label: str = None,
        eager_transformation: str = None,
        operation_id: str = None,
    ) -> dict:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        api_token = os.environ.get("CLOUDFLARE_STREAM_API_TOKEN")
        
        if not account_id or not api_token:
            err_msg = "CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_STREAM_API_TOKEN is missing"
            logger.error(f"[CloudflareStreamProvider] {err_msg}")
            return {"ok": False, "error": err_msg}

        copy_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/copy"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        
        # Build signed GET URL from R2 for Copy API
        try:
            # We generate a presigned R2 URL valid for 2 hours (comfortably long enough for Stream to fetch it)
            # Find the path key from public_id or R2 structure
            r2_key = f"raw-uploads/{scope}s/{parent_id}/{category}/{public_id.split('/')[-1]}.mp4"
            from core import generate_r2_presigned_url
            r2_signed_url = generate_r2_presigned_url(r2_key, "GET", expiry=7200)
        except Exception as e:
            logger.error(f"[CloudflareStreamProvider] Failed to sign R2 url for {public_id}: {e}")
            r2_signed_url = r2_url

        payload = {
            "url": r2_signed_url,
            "meta": {
                "media_id": media_id,
                "parent_id": parent_id,
                "scope": scope,
                "category": category,
                "label": label or "",
                "operation_id": operation_id or ""
            }
        }

        try:
            logger.info(f"[CloudflareStreamProvider] Triggering copy API for media_id={media_id} | OpID: {operation_id}")
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(copy_url, json=payload, headers=headers)
                if res.status_code >= 200 and res.status_code < 300:
                    data = res.json()
                    return {"ok": True, "provider_data": data.get("result", {})}
                else:
                    logger.error(f"[CloudflareStreamProvider] Copy API returned status {res.status_code}: {res.text}")
                    return {"ok": False, "error": f"Cloudflare Copy API status {res.status_code}"}
        except Exception as e:
            logger.error(f"[CloudflareStreamProvider] Network error calling Copy API: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}


def get_video_provider() -> VideoProvider:
    # Architecture mandate: video → Cloudflare Stream. Default to "stream" so a
    # missing/unset VIDEO_PROVIDER can never SILENTLY route audition video to
    # Cloudinary (which violates the storage architecture and fails loudly in logs
    # if Stream creds are absent, rather than quietly mis-storing video). Set
    # VIDEO_PROVIDER=cloudinary explicitly only for legacy/opt-out.
    provider_name = os.environ.get("VIDEO_PROVIDER", "stream").strip().lower()
    if provider_name == "stream":
        return CloudflareStreamProvider()
    return CloudinaryProvider()
