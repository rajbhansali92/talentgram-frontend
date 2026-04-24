"""Auth, file upload, file serving."""
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from core import (
    APP_NAME,
    LoginIn,
    TokenOut,
    current_admin,
    db,
    get_object,
    make_token,
    put_object,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", response_model=TokenOut)
async def login(payload: LoginIn):
    user = await db.admins.find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = make_token({"email": user["email"], "role": "admin", "id": user["id"]})
    return {
        "token": token,
        "admin": {"email": user["email"], "name": user.get("name"), "id": user["id"]},
    }


@router.get("/auth/me")
async def me(admin: dict = Depends(current_admin)):
    return admin


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
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
async def download_file(path: str):
    """Files are referenced by UUID paths. Public by design for client portfolio viewing."""
    data, content_type = get_object(path)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
