"""Auth, file upload, file serving."""
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from core import (
    APP_NAME,
    LoginIn,
    TokenOut,
    _now,
    current_admin,
    current_team_or_admin,
    current_user,
    db,
    get_object,
    make_token,
    put_object,
    stream_object,
    verify_password,
)

# Alias for readability inside login()
_now_iso = _now

router = APIRouter(prefix="/api", tags=["auth"])


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
    })
    # Track last_login (best-effort)
    try:
        await db.users.update_one({"id": user["id"]}, {"$set": {"last_login": _now_iso()}})
    except Exception:
        pass
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
    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/uploads/{admin['id']}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")
    return {
        "path": result["path"],
        "size": result.get("size", len(data)),
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
    }


@router.get("/files/{path:path}")
async def download_file(path: str, request: Request):
    """Stream a file from Emergent Object Store.

    - Supports HTTP Range (required for video seeking).
    - Never buffers the full file in RAM — yields 256 KB chunks.
    - Files are referenced by UUID-paths; intentionally public for client portfolio viewing.
    """
    range_header = request.headers.get("range") or request.headers.get("Range")
    iterator, headers, status = stream_object(path, range_header=range_header)
    headers.setdefault("Cache-Control", "public, max-age=86400")
    return StreamingResponse(
        iterator,
        status_code=status,
        media_type=headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )
