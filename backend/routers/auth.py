"""Auth, file upload, file serving."""
import logging
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

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
