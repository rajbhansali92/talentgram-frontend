"""Talentgram Portfolio Engine — FastAPI bootstrap.

All route logic lives in `routers/`. Shared primitives (config, DB, security,
storage, utils, constants, models, visibility filters) live in `core.py`.
"""
import os

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from core import init_storage, mongo_client, seed_admin
from routers import applications, auth, links, projects, submissions, talents

app = FastAPI(title="Talentgram Portfolio Engine")

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
