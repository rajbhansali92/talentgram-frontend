"""Auth, file upload, file serving."""
import logging
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from core import (
    APP_NAME,
    LoginIn,
    TokenOut,
    _now,
    cloudinary_upload,
    current_admin,
    current_team_or_admin,
    current_user,
    db,
    make_token,
    mint_portal_token,
    verify_password,
    _resolve_cover_url,
    compute_age,
    normalize_email,
)

# Alias for readability inside login()
_now_iso = _now

router = APIRouter(prefix="/api", tags=["auth"])
logger = logging.getLogger(__name__)


async def _grant_portal_session(talent: dict) -> str:
    """Mint a portal session token for an ownership-verified talent and persist
    it on the record so the session is revocable. Returns the token."""
    email = talent.get("email") or talent.get("normalized_email")
    token = mint_portal_token(email)
    await db.talents.update_one(
        {"id": talent["id"]},
        {"$set": {"portal_access_token": token}},
    )
    return token

# Prefill/Auth IP sliding window storage bucket
_PREFILL_BUCKET = {}


class GoogleAuthIn(BaseModel):
    code: str
    redirect_uri: str
    slug: str


@router.post("/auth/google")
async def google_auth(payload: GoogleAuthIn, request: Request):
    import os
    import requests
    import jwt
    import time
    from pydantic import BaseModel

    # Priority 5: Implement conservative rate limiting to prevent auth endpoint abuse (max 15 requests per minute per IP)
    ip = get_client_ip(request)
    now_ts = time.time()
    ip_bucket = _PREFILL_BUCKET.setdefault(ip, [])
    # Filter timestamps within last 60 seconds
    ip_bucket[:] = [ts for ts in ip_bucket if now_ts - ts < 60.0]
    if len(ip_bucket) >= 15:
        raise HTTPException(status_code=429, detail="Too many authentication attempts. Please try again in a minute.")
    ip_bucket.append(now_ts)

    token_url = "https://oauth2.googleapis.com/token"
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "mock-client-id")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "mock-client-secret")

    token_data = {
        "code": payload.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": payload.redirect_uri,
        "grant_type": "authorization_code"
    }

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(token_url, data=token_data, timeout=10.0)
        if r.status_code != 200:
            logger.error(f"Google Token Exchange error: {r.text}")
            raise HTTPException(status_code=400, detail="Failed to exchange Google OAuth code")
        res_data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to post to Google token url: {e}")
        raise HTTPException(status_code=400, detail="Failed to exchange Google OAuth code")

    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests

    id_token_val = res_data.get("id_token")
    if not id_token_val:
        raise HTTPException(status_code=400, detail="No id_token in Google response")

    try:
        id_info = id_token.verify_oauth2_token(
            id_token_val,
            google_requests.Request(),
            client_id
        )
    except Exception as e:
        logger.error(f"Google Token Verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid Google token signature")

    email = normalize_email(id_info.get("email"))
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email address")

    google_id = id_info.get("sub")
    name = id_info.get("name") or ""
    picture = id_info.get("picture") or ""

    if payload.slug == "apply":
        application = await db.applications.find_one({"talent_email": email})
        if application:
            talent = await db.talents.find_one({
                "$or": [
                    {"normalized_email": email},
                    {"email": email}
                ]
            })
            token = make_token({"role": "submitter", "sid": application["id"], "kind": "application"}, days=7)
            await db.applications.update_one({"id": application["id"]}, {"$set": {"access_token": token}})
            return {
                "existing": True,
                "email": email,
                "token": token,
                "application_id": application["id"],
                "status": application.get("status", "draft"),
                "talent": (await _get_talent_profile_response(talent)) if talent else None,
                "portal_token": (await _grant_portal_session(talent)) if talent else None,
            }

    talent = await db.talents.find_one({
        "$or": [
            {"normalized_email": email},
            {"email": email}
        ]
    })
    if not talent:
        # STEP 3: Store a temporary submission draft instead of creating a Talent record.
        draft = await db.submission_drafts.find_one({"email": email, "project_id": payload.slug or "apply"})
        if not draft:
            draft_id = str(uuid.uuid4())
            new_draft = {
                "draft_id": draft_id,
                "project_id": payload.slug or "apply",
                "email": email,
                "google_id": google_id,
                "draft_status": "draft",
                "form_data": {},
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            await db.submission_drafts.insert_one(new_draft)

        return {
            "existing": False,
            "email": email,
            "google_id": google_id,
            "name": name,
            "picture": picture
        }

    portal_token = await _grant_portal_session(talent)

    project = await db.projects.find_one({"slug": payload.slug})
    if not project:
        profile_data = await _get_talent_profile_response(talent)
        return {
            "existing": True,
            "portal_token": portal_token,
            **profile_data
        }

    submission = await db.submissions.find_one({"project_id": project["id"], "talent_email": email})
    if submission:
        token = make_token({"role": "submitter", "sid": submission["id"], "slug": payload.slug}, days=3)
        await db.submissions.update_one({"id": submission["id"]}, {"$set": {"access_token": token}})
        return {
            "existing": True,
            "email": email,
            "token": token,
            "submission_id": submission["id"],
            "status": submission.get("status", "draft"),
            "talent": await _get_talent_profile_response(talent),
            "portal_token": portal_token,
        }
    else:
        profile_data = await _get_talent_profile_response(talent)
        return {
            "existing": True,
            "portal_token": portal_token,
            **profile_data
        }



@router.post("/auth/login", response_model=TokenOut)
async def login(payload: LoginIn):
    email = normalize_email(payload.email)
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("status") == "disabled":
        raise HTTPException(status_code=403, detail="Account disabled")
    if user.get("status") == "invited":
        raise HTTPException(status_code=403, detail="Account not activated — complete signup first")
    role = user.get("role") or "team"
    token = make_token({
        "email": user["email"],
        "role": role,
        "id": user["id"],
        "tv": int(user.get("token_version") or 0),
    })
    # Track last_login (best-effort)
    try:
        await db.users.update_one({"id": user["id"]}, {"$set": {"last_login": _now_iso()}})
    except Exception as e:
        # Don't block login on this — but surface the error so a Mongo
        # outage doesn't fail silently.
        logger.warning(f"last_login write failed for {user.get('email')}: {e}")
    return {
        "token": token,
        "admin": {
            "email": user["email"],
            "name": user.get("name"),
            "id": user["id"],
            "role": role,
            "status": user.get("status", "active"),
        },
    }


@router.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return user


@router.get("/debug/user-role")
async def debug_user_role(user: dict = Depends(current_user)):
    """Diagnostic endpoint — returns the currently authenticated user's role.

    Useful for verifying that the frontend is sending the right JWT and that
    the backend agrees on the user's role. Intentionally minimal; does NOT
    leak password hashes or invite tokens.
    """
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "role": user.get("role"),
        "status": user.get("status"),
        "is_admin": user.get("role") == "admin",
    }


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    admin: dict = Depends(current_team_or_admin),
):
    """Upload a generic admin file to Cloudinary."""
    media_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/uploads/{admin['id']}"
    data = await file.read()
    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=media_id,
        resource_type="auto",
        content_type=file.content_type,
    )
    return {
        "url": result["url"],
        "public_id": result["public_id"],
        "resource_type": result["resource_type"],
        "size": result.get("bytes") or len(data),
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
    }


# --------------------------------------------------------------------------
# Email OTP Authentication
# --------------------------------------------------------------------------
import os
import random
import hashlib
import httpx
import boto3
from datetime import datetime, timezone, timedelta

class OtpSendIn(BaseModel):
    email: str

class OtpVerifyIn(BaseModel):
    email: str
    otp: str
    slug: str

def get_client_ip(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "127.0.0.1")
    )

RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Talentgram Agency <team@talentgramagency.com>")
print(f"Using Resend sender: {RESEND_FROM_EMAIL}")

async def send_otp_email(email: str, otp: str) -> bool:
    subject = "Your Talentgram Verification Code"
    
    # Branded plain text version
    text_content = (
        f"Hello,\n\n"
        f"Your Talentgram verification code is:\n\n"
        f"{otp}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you did not request this verification code, please ignore this email.\n\n"
        f"Instagram:\n"
        f"https://www.instagram.com/talentgram.agency/\n\n"
        f"Regards,\n"
        f"Talentgram Agency\n\n"
        f"https://www.talentgramagency.com"
    )

    # Modern, center-aligned, mobile-responsive HTML version
    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Talentgram Verification Code</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    .wrapper {{
      width: 100%;
      table-layout: fixed;
      background-color: #f8fafc;
      padding: 40px 0;
    }}
    .container {{
      max-width: 600px;
      margin: 0 auto;
      background-color: #ffffff;
      border-radius: 12px;
      box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);
      border: 1px solid #e2e8f0;
      overflow: hidden;
    }}
    .header {{
      background-color: #0b192c;
      padding: 24px;
      text-align: center;
    }}
    .header-text {{
      color: #ffffff;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.5px;
      margin: 0;
    }}
    .content {{
      padding: 40px;
      color: #334155;
      line-height: 1.6;
    }}
    .greeting {{
      font-size: 16px;
      margin-bottom: 16px;
    }}
    .body-text {{
      font-size: 16px;
      margin-bottom: 32px;
    }}
    .otp-box {{
      background-color: #f1f5f9;
      border-radius: 8px;
      padding: 24px;
      text-align: center;
      margin-bottom: 32px;
      border: 1px dashed #cbd5e1;
    }}
    .otp-code {{
      font-size: 42px;
      font-weight: 800;
      letter-spacing: 6px;
      color: #0b192c;
      margin: 0;
      font-family: monospace, Courier, monospace;
    }}
    .disclaimer {{
      font-size: 14px;
      color: #64748b;
      margin-bottom: 24px;
    }}
    .instagram-link {{
      font-size: 15px;
      color: #0b192c;
      text-decoration: none;
      font-weight: 600;
      display: inline-block;
      margin-bottom: 8px;
    }}
    .footer {{
      border-top: 1px solid #e2e8f0;
      padding: 32px 40px;
      background-color: #fafafa;
      text-align: center;
      color: #64748b;
      font-size: 14px;
    }}
    .footer-title {{
      font-weight: 700;
      color: #334155;
      margin: 0 0 4px 0;
    }}
    .footer-sub {{
      margin: 0 0 16px 0;
    }}
    .footer-link {{
      color: #64748b;
      text-decoration: underline;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="container">
      <div class="header">
        <h1 class="header-text">Talentgram Agency</h1>
      </div>
      <div class="content">
        <p class="greeting">Hello,</p>
        <p class="body-text">Your Talentgram verification code is:</p>
        <div class="otp-box">
          <h2 class="otp-code">{otp}</h2>
        </div>
        <p class="body-text" style="margin-top: 32px;">This code expires in 10 minutes.</p>
        <p class="disclaimer">If you did not request this verification code, please ignore this email.</p>
        <p style="margin: 0;">
          <a href="https://www.instagram.com/talentgram.agency/" class="instagram-link">Follow us on Instagram</a>
        </p>
      </div>
      <div class="footer">
        <p class="footer-title">Talentgram Agency</p>
        <p class="footer-sub">Global Talent Management & Casting</p>
        <a href="https://www.talentgramagency.com" class="footer-link">www.talentgramagency.com</a>
      </div>
    </div>
  </div>
</body>
</html>"""

    # 1. Resend
    resend_key = os.environ.get("RESEND_API_KEY")
    if resend_key and resend_key != "dummy":
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "from": RESEND_FROM_EMAIL,
                        "to": email,
                        "reply_to": "team@talentgramagency.com",
                        "subject": subject,
                        "html": html_content,
                        "text": text_content
                    },
                    timeout=10.0
                )
                if res.status_code in (200, 201):
                    logger.info(f"OTP sent to {email} via Resend")
                    return True
                else:
                    logger.error(f"Resend failed ({res.status_code}): {res.text}")
        except Exception as e:
            logger.error(f"Resend error: {e}")

    # 2. SendGrid
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if sendgrid_key and sendgrid_key != "dummy":
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {sendgrid_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "personalizations": [{"to": [{"email": email}]}],
                        "from": {"email": "team@talentgramagency.com", "name": "Talentgram Agency"},
                        "reply_to": {"email": "team@talentgramagency.com", "name": "Talentgram Agency"},
                        "subject": subject,
                        "content": [
                            {"type": "text/plain", "value": text_content},
                            {"type": "text/html", "value": html_content}
                        ]
                    },
                    timeout=10.0
                )
                if res.status_code in (200, 201, 202):
                    logger.info(f"OTP sent to {email} via SendGrid")
                    return True
                logger.error(f"SendGrid failed: {res.text}")
        except Exception as e:
            logger.error(f"SendGrid error: {e}")

    # 3. AWS SES
    try:
        if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_DEFAULT_REGION"):
            ses = boto3.client('ses', region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            res = ses.send_email(
                Source="Talentgram Agency <team@talentgramagency.com>",
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": subject},
                    "Body": {
                        "Text": {"Data": text_content},
                        "Html": {"Data": html_content}
                    }
                },
                ReplyToAddresses=["team@talentgramagency.com"]
            )
            if res.get("MessageId"):
                logger.info(f"OTP sent to {email} via AWS SES")
                return True
    except Exception as e:
        logger.error(f"AWS SES error: {e}")

    # Dev/Test Mock
    if os.environ.get("MONGO_URL") == "mongodb://localhost:27017" or os.environ.get("DB_NAME") == "test":
        logger.info(f"[DEV MOCK] Sent OTP email to {email} with code {otp}")
        return True

    return False

async def _get_talent_profile_response(talent: dict) -> dict:
    name_parts = talent.get("name", "").split(" ", 1)
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    
    from routers.submissions import deduplicate_media
    import datetime
    
    email = talent.get("email", "").strip().lower()
    
    # Image prefill: fetch existing image, indian, western look images from master profile
    prefill_images = []
    for m in (talent.get("media") or []):
        category = m.get("category")
        if category == "portfolio":
            category = "image"
        resource_type = m.get("resource_type") or "image"
        is_image = resource_type == "image" or (category not in {"video", "intro_video"} and not (m.get("content_type") or "").startswith("video/"))
        if is_image and m.get("url"):
            prefill_images.append({
                "id": m.get("id"),
                "category": category or "image",
                "url": m.get("url"),
                "public_id": m.get("public_id"),
                "resource_type": "image",
                "content_type": m.get("content_type") or "image/jpeg",
                "original_filename": m.get("original_filename"),
                "size": m.get("size") or 0,
                "created_at": m.get("created_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

    # Intro video prefill: priority 1: db.talents.media
    latest_intro = None
    for m in (talent.get("media") or []):
        if m.get("category") in {"video", "intro_video"} and m.get("url"):
            latest_intro = {
                "id": m.get("id"),
                "category": "intro_video",
                "url": m.get("url"),
                "public_id": m.get("public_id"),
                "resource_type": m.get("resource_type") or "video",
                "content_type": m.get("content_type") or "video/mp4",
                "original_filename": m.get("original_filename"),
                "size": m.get("size") or 0,
                "created_at": m.get("created_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
    return {
        "email": talent.get("email"),
        "first_name": first_name,
        "last_name": last_name,
        "location": talent.get("location", ""),
        "phone": talent.get("phone", ""),
        "height": talent.get("height", ""),
        "dob": talent.get("dob", ""),
        "age": talent.get("age") if talent.get("age") is not None else (compute_age(talent.get("dob")) if talent.get("dob") else None),
        "gender": talent.get("gender", ""),
        "ethnicity": talent.get("ethnicity", ""),
        "bio": talent.get("bio", ""),
        "instagram_handle": talent.get("instagram_handle", ""),
        "instagram_followers": talent.get("instagram_followers", ""),
        "skills": talent.get("skills", []),
        "work_links": talent.get("work_links", []),
        "image_url": _resolve_cover_url(talent),
        "prefill_media": deduplicate_media(prefill_images + ([latest_intro] if latest_intro else [])),
    }

@router.post("/auth/otp/send")
async def send_otp(payload: OtpSendIn, request: Request):
    email = normalize_email(payload.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    ip = get_client_ip(request)
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    # Rate Limiting: max 5 resend/send requests per hour per email/IP
    email_sends = await db.otp_audit_logs.count_documents({
        "email": email,
        "action": {"$in": ["sent", "resent"]},
        "timestamp": {"$gte": one_hour_ago.isoformat()}
    })
    ip_sends = await db.otp_audit_logs.count_documents({
        "ip_address": ip,
        "action": {"$in": ["sent", "resent"]},
        "timestamp": {"$gte": one_hour_ago.isoformat()}
    })

    if email_sends >= 5 or ip_sends >= 5:
        raise HTTPException(
            status_code=429,
            detail="Too many verification requests. Please try again in an hour."
        )

    # Invalidate previous unused OTP codes for this email
    await db.otp_codes.update_many(
        {"email": email, "used": False},
        {"$set": {"used": True}}
    )

    # Generate 6-digit numeric OTP
    otp = f"{random.randint(100000, 999999)}"
    hashed_otp = hashlib.sha256(otp.encode()).hexdigest()

    expires_at = now + timedelta(minutes=10)

    await db.otp_codes.insert_one({
        "email": email,
        "hashed_otp": hashed_otp,
        "expires_at": expires_at,
        "attempts": 0,
        "used": False,
        "ip_address": ip,
        "created_at": now
    })

    has_previous = await db.otp_audit_logs.find_one({"email": email})
    action = "resent" if has_previous else "sent"

    success = await send_otp_email(email, otp)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send verification email. Please try again.")

    await db.otp_audit_logs.insert_one({
        "email": email,
        "action": action,
        "timestamp": now.isoformat(),
        "ip_address": ip
    })

    return {"message": "Verification code sent successfully."}

@router.post("/auth/otp/verify")
async def verify_otp(payload: OtpVerifyIn, request: Request):
    email = normalize_email(payload.email)
    otp = payload.otp.strip()
    slug = payload.slug.strip()

    if not email or not otp:
        raise HTTPException(status_code=400, detail="Email and verification code are required.")

    ip = get_client_ip(request)
    now = datetime.now(timezone.utc)

    otp_record = await db.otp_codes.find_one(
        {"email": email, "used": False},
        sort=[("created_at", -1)]
    )

    if not otp_record:
        await db.otp_audit_logs.insert_one({
            "email": email,
            "action": "failed",
            "timestamp": now.isoformat(),
            "ip_address": ip
        })
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    expires_at = otp_record["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now > expires_at:
        await db.otp_codes.update_one({"_id": otp_record["_id"]}, {"$set": {"used": True}})
        await db.otp_audit_logs.insert_one({
            "email": email,
            "action": "expired",
            "timestamp": now.isoformat(),
            "ip_address": ip
        })
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    if otp_record.get("attempts", 0) >= 5:
        await db.otp_codes.update_one({"_id": otp_record["_id"]}, {"$set": {"used": True}})
        await db.otp_audit_logs.insert_one({
            "email": email,
            "action": "failed",
            "timestamp": now.isoformat(),
            "ip_address": ip
        })
        raise HTTPException(status_code=400, detail="Too many failed attempts. Please request a new code.")

    hashed_input = hashlib.sha256(otp.encode()).hexdigest()
    if hashed_input != otp_record["hashed_otp"]:
        await db.otp_codes.update_one(
            {"_id": otp_record["_id"]},
            {"$inc": {"attempts": 1}}
        )
        await db.otp_audit_logs.insert_one({
            "email": email,
            "action": "failed",
            "timestamp": now.isoformat(),
            "ip_address": ip
        })
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    await db.otp_codes.update_one({"_id": otp_record["_id"]}, {"$set": {"used": True}})

    await db.otp_audit_logs.insert_one({
        "email": email,
        "action": "verified",
        "timestamp": now.isoformat(),
        "ip_address": ip
    })

    if slug == "apply":
        application = await db.applications.find_one({"talent_email": email})
        talent = await db.talents.find_one({
            "$or": [
                {"normalized_email": email},
                {"email": email}
            ]
        })
        if application:
            token = make_token({"role": "submitter", "sid": application["id"], "kind": "application"}, days=7)
            await db.applications.update_one({"id": application["id"]}, {"$set": {"access_token": token}})
            return {
                "existing": True,
                "email": email,
                "token": token,
                "application_id": application["id"],
                "status": application.get("status", "draft"),
                "talent": (await _get_talent_profile_response(talent)) if talent else None,
                "portal_token": (await _grant_portal_session(talent)) if talent else None,
            }
        elif talent:
            return {
                "existing": True,
                "email": email,
                "talent": await _get_talent_profile_response(talent),
                "portal_token": await _grant_portal_session(talent),
            }
    else:
        project = await db.projects.find_one({"slug": slug})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        submission = await db.submissions.find_one({"project_id": project["id"], "talent_email": email})
        
        if submission:
            token = make_token({"role": "submitter", "sid": submission["id"], "slug": slug}, days=30)
            await db.submissions.update_one({"id": submission["id"]}, {"$set": {"access_token": token}})
            talent = await db.talents.find_one({
                "$or": [
                    {"normalized_email": email},
                    {"email": email}
                ]
            })
            return {
                "existing": True,
                "email": email,
                "token": token,
                "submission_id": submission["id"],
                "status": submission.get("status", "draft"),
                "talent": (await _get_talent_profile_response(talent)) if talent else None,
                "portal_token": (await _grant_portal_session(talent)) if talent else None,
            }

    talent = await db.talents.find_one({
        "$or": [
            {"normalized_email": email},
            {"email": email}
        ]
    })
    if talent:
        return {
            "existing": True,
            "email": email,
            "talent": await _get_talent_profile_response(talent),
            "portal_token": await _grant_portal_session(talent),
        }

    # STEP 3: Store a temporary submission draft instead of creating a Talent record.
    draft = await db.submission_drafts.find_one({"email": email, "project_id": slug or "apply"})
    if not draft:
        draft_id = str(uuid.uuid4())
        new_draft = {
            "draft_id": draft_id,
            "project_id": slug or "apply",
            "email": email,
            "google_id": None,
            "draft_status": "draft",
            "form_data": {},
            "created_at": now.isoformat(),
            "updated_at": now.isoformat()
        }
        await db.submission_drafts.insert_one(new_draft)

    return {
        "existing": False,
        "email": email,
        "message": "Verification successful. Draft submission initialized."
    }
