"""Talentgram Portfolio Engine — FastAPI bootstrap.

All route logic lives in `routers/`. Shared primitives (config, DB, security,
storage, utils, constants, models, visibility filters) live in `core.py`.
"""
import os

from fastapi import APIRouter, FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from core import init_storage, mongo_client, seed_admin
from routers import applications, auth, links, projects, submissions, talents, users

app = FastAPI(title="Talentgram Portfolio Engine")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth headers to reduce XSS/clickjacking blast radius.

    Notes:
    - CSP is deliberately permissive for the API (no HTML rendered here). The
      React SPA is served by its own host; this middleware hardens direct
      backend responses (JSON + media streams).
    - `frame-ancestors 'none'` blocks clickjacking of any JSON response.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'",
        )
        return response


# Health / meta
_meta = APIRouter(prefix="/api")


@_meta.get("/")
async def root():
    return {"app": "talentgram", "ok": True}


# Register routers
app.include_router(_meta)
app.include_router(auth.router)
app.include_router(talents.router)
app.include_router(links.router)
app.include_router(projects.router)
app.include_router(submissions.router)
app.include_router(applications.router)
app.include_router(users.router)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await seed_admin()
    init_storage()


@app.on_event("shutdown")
async def on_shutdown():
    mongo_client.close()
