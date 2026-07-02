#!/usr/bin/env python3
"""READ-ONLY production media-storage audit.

Classifies every media record by where it physically lives (Cloudinary / Cloudflare
Stream / R2) to determine the real production state vs. the intended architecture.

SAFETY — this script is strictly read-only:
  * DB: only .find() reads. No insert/update/delete.
  * Cloudinary: HEAD request to the delivery URL (or read-only Admin API).
  * Stream: GET /stream/{uid} (read-only).
  * R2: head_object (read-only). Never put/delete.
There is intentionally NO code path that writes, uploads, deletes, or migrates.

USAGE (run against a prod-readonly connection string):
    MONGO_URL=... DB_NAME=talentgram \
    CLOUDINARY_CLOUD_NAME=... CLOUDINARY_API_KEY=... CLOUDINARY_API_SECRET=... \
    CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_STREAM_API_TOKEN=... \
    R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_ENDPOINT_URL=... R2_BUCKET_NAME=... \
    python backend/scripts/audit_media_storage.py --limit 0 --out media_audit.jsonl

Flags:
    --limit N        cap records scanned per collection (0 = all)
    --probe / --no-probe   actually hit Cloudinary/Stream/R2 to confirm existence
                           (default: --probe). --no-probe classifies by DB fields only.
    --out FILE       write per-record results as JSONL (ids/keys only, no PII)
"""
import argparse
import asyncio
import json
import os
import sys

VIDEO_CATEGORIES = {"intro_video", "take", "take_1", "take_2", "take_3", "video", "portfolio_video"}
IMAGE_CATEGORIES = {"image", "indian", "western", "portfolio", "headshot", "additional_portfolio"}


def _config_report():
    def state(name, disabled_when_false=False):
        v = os.environ.get(name, "")
        if not v:
            return "MISSING"
        if disabled_when_false and v.strip().lower() in ("false", "0", "no", ""):
            return "DISABLED"
        return "CONFIGURED"
    return {
        "ENABLE_R2_MEDIA_PIPELINE": state("ENABLE_R2_MEDIA_PIPELINE", disabled_when_false=True),
        "VIDEO_PROVIDER": os.environ.get("VIDEO_PROVIDER", "(unset → defaults to 'stream')"),
        "DIRECT_UPLOAD_ENABLED": state("DIRECT_UPLOAD_ENABLED", disabled_when_false=True),
        "DIRECT_VIDEO_UPLOAD": state("DIRECT_VIDEO_UPLOAD", disabled_when_false=True),
        "CLOUDFLARE_ACCOUNT_ID": state("CLOUDFLARE_ACCOUNT_ID"),
        "CLOUDFLARE_STREAM_API_TOKEN": state("CLOUDFLARE_STREAM_API_TOKEN"),
        "CLOUDFLARE_STREAM_CUSTOMER_CODE": state("CLOUDFLARE_STREAM_CUSTOMER_CODE"),
        "R2_ACCESS_KEY_ID": state("R2_ACCESS_KEY_ID"),
        "R2_SECRET_ACCESS_KEY": state("R2_SECRET_ACCESS_KEY"),
        "R2_ENDPOINT_URL": state("R2_ENDPOINT_URL"),
        "R2_BUCKET_NAME": state("R2_BUCKET_NAME"),
        "CLOUDINARY_CLOUD_NAME": state("CLOUDINARY_CLOUD_NAME"),
    }


def _derive_r2_key(scope, parent_id, category, public_id):
    if not (scope and parent_id and category and public_id):
        return None
    leaf = public_id.split("/")[-1]
    return f"raw-uploads/{scope}s/{parent_id}/{category}/{leaf}.mp4"


async def _probe_cloudinary(client, cloud, public_id):
    # HEAD the canonical Cloudinary video delivery URL (read-only, no Admin API limits).
    if not (cloud and public_id) or public_id.startswith("raw-uploads/"):
        return False
    url = f"https://res.cloudinary.com/{cloud}/video/upload/{public_id}.mp4"
    try:
        r = await client.head(url, follow_redirects=True, timeout=15.0)
        return r.status_code == 200
    except Exception:
        return False


async def _probe_stream(client, account_id, token, stream_uid):
    if not (account_id and token and stream_uid):
        return False
    try:
        r = await client.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/{stream_uid}",
            headers={"Authorization": f"Bearer {token}"}, timeout=15.0,
        )
        return r.status_code == 200
    except Exception:
        return False


def _probe_r2(s3, bucket, key):
    if not (s3 and bucket and key):
        return False
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _bucket_label(cl, st, r2):
    on = [n for n, b in (("Cloudinary", cl), ("Stream", st), ("R2", r2)) if b]
    return " + ".join(on) if on else "Unknown (none found)"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--probe", dest="probe", action="store_true", default=True)
    ap.add_argument("--no-probe", dest="probe", action="store_false")
    ap.add_argument("--out", default="media_audit.jsonl")
    args = ap.parse_args()

    cfg = _config_report()
    print("=== PART 3 — Production configuration (no secrets) ===")
    for k, v in cfg.items():
        print(f"  {k:34s} {v}")
    print()

    from motor.motor_asyncio import AsyncIOMotorClient
    import httpx
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]

    cloud = os.environ.get("CLOUDINARY_CLOUD_NAME")
    cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    cf_tok = os.environ.get("CLOUDFLARE_STREAM_API_TOKEN")
    s3 = None
    if args.probe and os.environ.get("R2_ENDPOINT_URL"):
        try:
            import boto3
            s3 = boto3.client(
                "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
            )
        except Exception as e:
            print(f"  (R2 client unavailable: {e})")

    video_totals, image_flags = {}, {"multi_provider": 0, "total": 0}
    out = open(args.out, "w")
    http = httpx.AsyncClient()
    try:
        for coll in ("submissions", "applications", "talents"):
            cursor = db[coll].find({"media": {"$exists": True, "$ne": []}}, {"_id": 0, "id": 1, "media": 1})
            scanned = 0
            async for doc in cursor:
                scanned_parent = doc.get("id")
                for m in doc.get("media") or []:
                    cat = m.get("category")
                    pid = m.get("public_id")
                    url = m.get("url") or ""
                    provider = m.get("provider")
                    stream_uid = m.get("stream_uid")
                    is_video = (cat in VIDEO_CATEGORIES) or (m.get("resource_type") == "video")
                    if is_video:
                        scope = "submission" if coll == "submissions" else ("application" if coll == "applications" else (m.get("scope") or ("submission" if m.get("submission_id") else "application" if m.get("application_id") else None)))
                        parent = scanned_parent if coll in ("submissions", "applications") else (m.get("submission_id") or m.get("application_id"))
                        r2_key = _derive_r2_key(scope, parent, cat, pid)
                        if args.probe:
                            cl = await _probe_cloudinary(http, cloud, pid)
                            st = await _probe_stream(http, cf_acct, cf_tok, stream_uid)
                            r2 = _probe_r2(s3, os.environ.get("R2_BUCKET_NAME"), r2_key)
                            label = _bucket_label(cl, st, r2)
                        else:
                            # DB-field-only classification (no network)
                            cl = ("res.cloudinary.com" in url) or (provider == "cloudinary") or (bool(pid) and not (pid or "").startswith("raw-uploads/"))
                            st = bool(stream_uid) or ("cloudflarestream.com" in url) or (provider == "stream")
                            r2 = bool(r2_key) and (pid or "").startswith("raw-uploads/")
                            label = _bucket_label(cl, st, r2) + " (DB-fields only)"
                        video_totals[label] = video_totals.get(label, 0) + 1
                        out.write(json.dumps({
                            "coll": coll, "parent_id": parent, "media_id": m.get("id"),
                            "category": cat, "provider": provider, "has_stream_uid": bool(stream_uid),
                            "public_id_prefix": (pid or "")[:20], "url_host": url.split("/")[2] if "//" in url else "",
                            "derived_r2_key": r2_key, "cloudinary": cl, "stream": st, "r2": r2, "classification": label,
                        }) + "\n")
                    elif cat in IMAGE_CATEGORIES or m.get("resource_type") == "image":
                        image_flags["total"] += 1
                        # Images should be Cloudinary-only: flag stream/r2 traces.
                        if stream_uid or "cloudflarestream.com" in url or (pid or "").startswith("raw-uploads/"):
                            image_flags["multi_provider"] += 1
                            out.write(json.dumps({"coll": coll, "media_id": m.get("id"), "category": cat,
                                                  "FLAG": "image-has-nonCloudinary-trace", "url_host": url.split("/")[2] if "//" in url else ""}) + "\n")
                scanned += 1
                if args.limit and scanned >= args.limit:
                    break
    finally:
        await http.aclose()
        out.close()

    print("=== PART 1 — Video classification totals ===")
    for label, n in sorted(video_totals.items(), key=lambda x: -x[1]):
        print(f"  {n:6d}  {label}")
    print(f"\n=== PART 2 — Images (expected Cloudinary-only) ===")
    print(f"  total images scanned: {image_flags['total']}")
    print(f"  images with non-Cloudinary trace (FLAGGED): {image_flags['multi_provider']}")
    print(f"\nPer-record detail written to: {args.out}")
    print("Probe mode:", "LIVE (Cloudinary/Stream/R2 existence checked)" if args.probe else "DB-FIELDS ONLY")


if __name__ == "__main__":
    asyncio.run(main())
