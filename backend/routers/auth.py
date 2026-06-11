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
    verify_password,
)

# Alias for readability inside login()
_now_iso = _now

router = APIRouter(prefix="/api", tags=["auth"])
logger = logging.getLogger(__name__)


class GoogleAuthIn(BaseModel):
    code: str
    redirect_uri: str
    slug: str


@router.post("/auth/google")
async def google_auth(payload: GoogleAuthIn):
    import os
    import requests
    import jwt
    from pydantic import BaseModel

    token_url = "https://oauth2.googleapis.com/token"
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "mock-client-id")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "mock-client-secret")

    token_data = {
        "code": payload.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": payload.redirect_uri,
        "grant_type": "authorization_code"
    }

    try:
        r = requests.post(token_url, data=token_data)
        if r.status_code != 200:
            logger.error(f"Google Token Exchange error: {r.text}")
            raise HTTPException(status_code=400, detail="Failed to exchange Google OAuth code")
        res_data = r.json()
    except Exception as e:
        logger.error(f"Failed to post to Google token url: {e}")
        raise HTTPException(status_code=400, detail="Failed to exchange Google OAuth code")

    id_token = res_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No id_token in Google response")

    try:
        id_info = jwt.decode(id_token, options={"verify_signature": False})
    except Exception as e:
        logger.error(f"Failed to decode id_token: {e}")
        raise HTTPException(status_code=400, detail="Failed to parse user profile from Google")

    email = id_info.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email address")

    email = email.lower().strip()
    google_id = id_info.get("sub")
    name = id_info.get("name") or ""
    picture = id_info.get("picture") or ""

    talent = await db.talents.find_one({"email": email})
    if not talent:
        return {
            "existing": False,
            "email": email,
            "google_id": google_id,
            "name": name,
            "picture": picture
        }

    project = await db.projects.find_one({"slug": payload.slug})
    if not project:
        name_parts = talent.get("name", "").split(" ", 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        return {
            "existing": True,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "location": talent.get("location", ""),
            "phone": talent.get("phone", ""),
            "height": talent.get("height", ""),
            "dob": talent.get("dob", ""),
            "gender": talent.get("gender", ""),
            "ethnicity": talent.get("ethnicity", ""),
            "bio": talent.get("bio", ""),
            "instagram_handle": talent.get("instagram_handle", ""),
            "instagram_followers": talent.get("instagram_followers", ""),
            "skills": talent.get("skills", []),
            "work_links": talent.get("work_links", []),
        }

    submission = await db.submissions.find_one({"project_id": project["id"], "talent_email": email})
    if submission:
        token = make_token({"role": "submitter", "sid": submission["id"], "slug": payload.slug}, days=3)
        return {
            "existing": True,
            "email": email,
            "token": token,
            "submission_id": submission["id"],
            "status": submission.get("status", "draft")
        }
    else:
        name_parts = talent.get("name", "").split(" ", 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        return {
            "existing": True,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "location": talent.get("location", ""),
            "phone": talent.get("phone", ""),
            "height": talent.get("height", ""),
            "dob": talent.get("dob", ""),
            "gender": talent.get("gender", ""),
            "ethnicity": talent.get("ethnicity", ""),
            "bio": talent.get("bio", ""),
            "instagram_handle": talent.get("instagram_handle", ""),
            "instagram_followers": talent.get("instagram_followers", ""),
            "skills": talent.get("skills", []),
            "work_links": talent.get("work_links", []),
        }



@router.post("/auth/login", response_model=TokenOut)
async def login(payload: LoginIn):
    user = await db.users.find_one({"email": payload.email.lower()})
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
