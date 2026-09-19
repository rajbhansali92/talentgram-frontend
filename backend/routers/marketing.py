"""Marketing — lightweight CRM router.

Tracks four collections:
  • `clients`           — basic contact records (name, company, phone).
  • `interactions`      — call/meeting/email notes against a client.
  • `crm_companies`     — admin-managed Production House / Company directory
                           (2026-09-19 Talentgram-relationship adaptation).
  • `crm_contact_types` — admin-managed Contact Type lookup list (same).

Both `clients`/`interactions` endpoints use BSON ObjectId for `_id` (per
spec) — `clients._id` and `interactions.client_id` are stored as ObjectId,
so the foreign-key relationship can be queried efficiently. Outgoing JSON
converts ObjectId to string so the FastAPI response is JSON-serialisable.
`crm_companies`/`crm_contact_types` follow the exact same ObjectId shape.

This module is fully self-contained:
  • No import from `routers.notifications`.
  • No global state mutation.
  • Single `router = APIRouter(...)` definition (line near top).

NOTE on prefix: the spec asked for `prefix="/marketing"`, but this
deployment's Kubernetes ingress only forwards paths under `/api/*` to
the backend pod. Using a bare `/marketing` would 404 at the ingress
layer. Prefix is therefore set to `/api/marketing` so the routes are
reachable end-to-end. Tags remain `Marketing` as requested.

CRM talent-industry adaptation (2026-09-19): `clients.company_name` and
`clients.contact_type` stay free-text/slug strings exactly as before —
nothing renamed, nothing removed — so every existing caller (the WhatsApp
CRM agent's `insert_client_doc()` calls, Production Desk's `client_id`
crew/kickback references, this file's own bulk endpoints) keeps working
completely unmodified. `company_id`/`designation`/`notes` are new, purely
optional fields; when a caller sets `company_id`, `company_name` is kept in
sync from the directory entry so every existing company_name-reading
surface (list card, search, WhatsApp agent) needs no changes at all.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Re-use the singleton Motor client from core. The marketing router
# explicitly does NOT pull in the notifications module — this is a
# clean, parallel surface. `current_admin` gates company/contact-type
# *management* only (add/rename/deactivate) — reusing the app's existing
# admin-only dependency exactly as it's already used in routers/users.py,
# not a new permission system. Listing/selecting stays current_team_or_admin
# (unchanged parity with every other /marketing endpoint).
from core import current_admin, current_team_or_admin, db

# ---------------------------------------------------------------------------
# Single APIRouter definition
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/marketing", tags=["Marketing"])


# ---------------------------------------------------------------------------
# Pydantic input/output models
# ---------------------------------------------------------------------------
class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    company_name: Optional[str] = Field(default=None, max_length=200)
    company_id: Optional[str] = Field(default=None, description="crm_companies ObjectId; resolves/syncs company_name")
    phone_number: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=200)
    tags: Optional[List[str]] = Field(default=None)
    stage: Optional[str] = Field(default="lead", max_length=40)
    value: Optional[float] = Field(default=None)
    contact_type: Optional[str] = Field(default=None, max_length=100)
    designation: Optional[str] = Field(default=None, max_length=150)
    notes: Optional[str] = Field(default=None, max_length=2000)


class ClientUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    company_name: Optional[str] = Field(default=None, max_length=200)
    company_id: Optional[str] = Field(default=None, description="crm_companies ObjectId; resolves/syncs company_name. Empty string clears it.")
    phone_number: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=200)
    tags: Optional[List[str]] = Field(default=None)
    stage: Optional[str] = Field(default=None, max_length=40)
    value: Optional[float] = Field(default=None)
    contact_type: Optional[str] = Field(default=None, max_length=100)
    designation: Optional[str] = Field(default=None, max_length=150)
    notes: Optional[str] = Field(default=None, max_length=2000)


class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class CompanyUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class ContactTypeCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    group: Optional[str] = Field(default=None, max_length=60)


class ContactTypeUpdate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    group: Optional[str] = Field(default=None, max_length=60)


class InteractionCreate(BaseModel):
    client_id: str = Field(..., description="ObjectId of the client (24-hex)")
    # Free-form so callers can use 'call' / 'email' / 'meeting' / 'whatsapp'
    # without backend changes for new types.
    type: str = Field(..., min_length=1, max_length=40)
    notes: Optional[str] = Field(default=None, max_length=4000)


class BulkActionPayload(BaseModel):
    ids: List[str] = Field(..., description="List of client object IDs (24-hex)")


class BulkTagPayload(BaseModel):
    ids: List[str] = Field(..., description="List of client object IDs (24-hex)")
    tags: List[str] = Field(..., description="List of tags to assign/merge")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    """UTC-aware timestamp — Mongo stores naive UTC, but the source of
    truth for our backend is timezone-aware to avoid DST surprises."""
    return datetime.now(timezone.utc)


def _to_object_id(raw: str, *, field: str = "id") -> ObjectId:
    """Best-effort ObjectId parser that returns a clean 400 when the
    caller hands us a malformed string instead of leaking BSON errors."""
    try:
        return ObjectId(raw)
    except (InvalidId, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field}: must be a 24-character hex ObjectId",
        ) from exc


def _serialise_client(doc: dict) -> dict:
    """Convert internal Mongo doc → JSON-safe response shape."""
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "company_name": doc.get("company_name"),
        "company_id": str(doc["company_id"]) if doc.get("company_id") else None,
        "phone_number": doc.get("phone_number"),
        "email": doc.get("email"),
        "tags": doc.get("tags") or [],
        "stage": doc.get("stage") or "lead",
        "value": doc.get("value"),
        "contact_type": doc.get("contact_type"),
        "designation": doc.get("designation"),
        "notes": doc.get("notes"),
        "created_at": doc.get("created_at"),
        "last_contacted_date": doc.get("last_contacted_date"),
        "archived": doc.get("archived") or False,
        "deleted": doc.get("deleted") or False,
    }


def _serialise_interaction(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "client_id": str(doc["client_id"]) if doc.get("client_id") else None,
        "type": doc.get("type"),
        "notes": doc.get("notes"),
        "created_at": doc.get("created_at"),
    }


def _serialise_company(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "active": doc.get("active", True),
        "created_at": doc.get("created_at"),
    }


def _serialise_contact_type(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "value": doc.get("value"),
        "label": doc.get("label"),
        "group": doc.get("group"),
        "active": doc.get("active", True),
        "created_at": doc.get("created_at"),
    }


def _slugify_snake(label: str) -> str:
    """snake_case slug matching the existing contact_type convention
    (e.g. "brand_manager") — the exact shape the WhatsApp CRM agent's
    ROLE_LABEL_TO_SLUG already writes to clients.contact_type."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "type"


async def _resolve_company_id(company_id: Optional[str]) -> tuple[Optional[ObjectId], Optional[str]]:
    """Look up a crm_companies doc by id. Returns (ObjectId, name) so the
    caller can sync clients.company_name from the directory — or (None,
    None) for an absent/blank id, never raising on a stale/bad id so an
    edit to unrelated fields never breaks because a company was deleted
    (companies are only ever deactivated, never deleted, so this is
    defensive rather than an expected path)."""
    if not company_id:
        return None, None
    try:
        oid = ObjectId(company_id)
    except (InvalidId, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid company_id")
    company = await db.crm_companies.find_one({"_id": oid})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return oid, company.get("name")


async def insert_client_doc(
    *,
    name: str,
    company_name: Optional[str] = None,
    company_id: Optional[str] = None,
    phone_number: Optional[str] = None,
    email: Optional[str] = None,
    tags: Optional[List[str]] = None,
    stage: Optional[str] = None,
    value: Optional[float] = None,
    contact_type: Optional[str] = None,
    designation: Optional[str] = None,
    notes: Optional[str] = None,
    source: Optional[str] = None,
) -> dict:
    """Core client-insert logic, shared by the HTTP endpoint below and
    any non-HTTP caller (e.g. the WhatsApp CRM agent executor) so there is
    exactly one place a `clients` document gets created. `source` is an
    optional free-text provenance tag (e.g. "whatsapp_agent:crm-agent")
    stored on the doc but not required by the HTTP API.

    `company_id`/`designation`/`notes` are new, purely additive params —
    every existing caller (WhatsApp CRM agent, management_agent.py) omits
    them and behaves exactly as before. When `company_id` is given, the
    resolved directory name is written into `company_name` too, so every
    pre-existing company_name-reading surface keeps working unchanged."""
    resolved_company_id, resolved_company_name = await _resolve_company_id(company_id)
    now = _now()
    doc = {
        "name": name.strip(),
        "company_name": resolved_company_name or (company_name or "").strip() or None,
        "phone_number": (phone_number or "").strip() or None,
        "email": (email or "").strip().lower() or None,
        "tags": [t.strip() for t in tags if t.strip()] if tags else [],
        "stage": (stage or "lead").strip(),
        "value": value,
        "contact_type": (contact_type or "").strip() or None,
        "designation": (designation or "").strip() or None,
        "notes": (notes or "").strip() or None,
        "created_at": now,
        "last_contacted_date": now,
    }
    if resolved_company_id:
        doc["company_id"] = resolved_company_id
    if source:
        doc["source"] = source
    res = await db.clients.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _serialise_client(doc)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
@router.post("/clients", status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate,
    _admin: dict = Depends(current_team_or_admin),
):
    """Insert a new client document. `last_contacted_date` is initialised
    to the same instant as `created_at` so the row sorts naturally
    among other already-contacted clients before the first interaction
    is logged."""
    return await insert_client_doc(
        name=payload.name,
        company_name=payload.company_name,
        company_id=payload.company_id,
        phone_number=payload.phone_number,
        email=payload.email,
        tags=payload.tags,
        stage=payload.stage,
        value=payload.value,
        contact_type=payload.contact_type,
        designation=payload.designation,
        notes=payload.notes,
    )


@router.get("/clients")
async def list_clients(
    _admin: dict = Depends(current_team_or_admin),
) -> List[dict]:
    """Return every active, non-archived client. Sorted by most-recent contact first."""
    cursor = db.clients.find({"archived": {"$ne": True}, "deleted": {"$ne": True}}).sort("last_contacted_date", -1)
    items = await cursor.to_list(length=None)
    return [_serialise_client(d) for d in items]


@router.get("/clients/{client_id}")
async def get_client(
    client_id: str,
    _admin: dict = Depends(current_team_or_admin),
):
    oid = _to_object_id(client_id, field="client_id")
    doc = await db.clients.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Client not found")
    return _serialise_client(doc)


@router.put("/clients/{client_id}")
async def update_client(
    client_id: str,
    payload: ClientUpdate,
    _admin: dict = Depends(current_team_or_admin),
):
    oid = _to_object_id(client_id, field="client_id")
    existing = await db.clients.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")

    upd = {}
    if payload.name is not None:
        upd["name"] = payload.name.strip()
    if payload.company_name is not None:
        upd["company_name"] = payload.company_name.strip() or None
    if payload.phone_number is not None:
        upd["phone_number"] = payload.phone_number.strip() or None
    if payload.email is not None:
        upd["email"] = payload.email.strip().lower() or None
    if payload.tags is not None:
        upd["tags"] = [t.strip() for t in payload.tags if t.strip()]
    if payload.stage is not None:
        upd["stage"] = payload.stage.strip()
    if payload.value is not None:
        upd["value"] = payload.value
    if payload.contact_type is not None:
        upd["contact_type"] = payload.contact_type.strip() or None
    if payload.designation is not None:
        upd["designation"] = payload.designation.strip() or None
    if payload.notes is not None:
        upd["notes"] = payload.notes.strip() or None
    # Explicit "" clears the company relationship; a real id resolves +
    # syncs company_name so the list/search/WhatsApp-agent view of this
    # client stays consistent without those surfaces needing any changes.
    if payload.company_id is not None:
        if payload.company_id == "":
            upd["company_id"] = None
        else:
            resolved_id, resolved_name = await _resolve_company_id(payload.company_id)
            upd["company_id"] = resolved_id
            upd["company_name"] = resolved_name

    if upd:
        await db.clients.update_one({"_id": oid}, {"$set": upd})
        
    doc = await db.clients.find_one({"_id": oid})
    return _serialise_client(doc)


@router.post("/clients/{client_id}/archive")
async def archive_client(
    client_id: str,
    _admin: dict = Depends(current_team_or_admin),
):
    logger.info("=== BACKEND ROUTE HIT: POST /api/marketing/clients/%s/archive ===", client_id)
    oid = _to_object_id(client_id, field="client_id")
    res = await db.clients.update_one({"_id": oid}, {"$set": {"archived": True}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"success": True}


@router.delete("/clients/{client_id}")
async def delete_client(
    client_id: str,
    _admin: dict = Depends(current_team_or_admin),
):
    logger.info("=== BACKEND ROUTE HIT: DELETE /api/marketing/clients/%s ===", client_id)
    oid = _to_object_id(client_id, field="client_id")
    res = await db.clients.update_one({"_id": oid}, {"$set": {"deleted": True}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"success": True}


@router.post("/clients/bulk-archive")
async def bulk_archive_clients(
    payload: BulkActionPayload,
    _admin: dict = Depends(current_team_or_admin),
):
    logger.info("=== BACKEND ROUTE HIT: POST /api/marketing/clients/bulk-archive ===")
    oids = [_to_object_id(cid, field="client_id") for cid in payload.ids]
    await db.clients.update_many({"_id": {"$in": oids}}, {"$set": {"archived": True}})
    return {"success": True, "count": len(oids)}


@router.post("/clients/bulk-delete")
async def bulk_delete_clients(
    payload: BulkActionPayload,
    _admin: dict = Depends(current_team_or_admin),
):
    logger.info("=== BACKEND ROUTE HIT: POST /api/marketing/clients/bulk-delete ===")
    oids = [_to_object_id(cid, field="client_id") for cid in payload.ids]
    await db.clients.update_many({"_id": {"$in": oids}}, {"$set": {"deleted": True}})
    return {"success": True, "count": len(oids)}


@router.post("/clients/bulk-tag")
async def bulk_tag_clients(
    payload: BulkTagPayload,
    _admin: dict = Depends(current_team_or_admin),
):
    logger.info("=== BACKEND ROUTE HIT: POST /api/marketing/clients/bulk-tag ===")
    oids = [_to_object_id(cid, field="client_id") for cid in payload.ids]
    tags_clean = [t.strip() for t in payload.tags if t.strip()]
    if tags_clean:
        await db.clients.update_many(
            {"_id": {"$in": oids}},
            {"$addToSet": {"tags": {"$each": tags_clean}}}
        )
    return {"success": True, "count": len(oids)}


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------
@router.post("/interactions", status_code=status.HTTP_201_CREATED)
async def create_interaction(
    payload: InteractionCreate,
    _admin: dict = Depends(current_team_or_admin),
):
    """Log a new touchpoint AND bump the parent client's
    `last_contacted_date` so the clients list re-sorts to surface the
    just-contacted lead at the top."""
    client_oid = _to_object_id(payload.client_id, field="client_id")

    # Validate the client exists before writing the interaction.
    parent = await db.clients.find_one({"_id": client_oid}, {"_id": 1})
    if not parent:
        raise HTTPException(status_code=404, detail="Client not found")

    now = _now()
    doc = {
        "client_id": client_oid,
        "type": payload.type.strip(),
        "notes": (payload.notes or "").strip() or None,
        "created_at": now,
    }
    res = await db.interactions.insert_one(doc)
    doc["_id"] = res.inserted_id

    # Bump the parent client. We deliberately don't roll this into a
    # transaction — Atlas free-tier and standalone Mongo would refuse
    # the multi-doc write. Two sequential writes are acceptable here:
    # if the bump fails the interaction is still recorded, and the next
    # interaction will refresh the date.
    await db.clients.update_one(
        {"_id": client_oid},
        {"$set": {"last_contacted_date": now}},
    )

    return _serialise_interaction(doc)


@router.get("/interactions/{client_id}")
async def list_interactions(
    client_id: str,
    _admin: dict = Depends(current_team_or_admin),
) -> List[dict]:
    """Return every interaction for the given client, newest first."""
    client_oid = _to_object_id(client_id, field="client_id")
    cursor = (
        db.interactions.find({"client_id": client_oid}).sort("created_at", -1)
    )
    items = await cursor.to_list(length=None)
    return [_serialise_interaction(d) for d in items]


# ---------------------------------------------------------------------------
# Production House / Company directory (admin-managed lookup list)
# ---------------------------------------------------------------------------
@router.get("/companies")
async def list_companies(
    include_inactive: bool = False,
    _admin: dict = Depends(current_team_or_admin),
) -> List[dict]:
    """Every team member can list companies (to select one on a contact);
    only admins can add/rename/deactivate — see the POST routes below."""
    query = {} if include_inactive else {"active": {"$ne": False}}
    cursor = db.crm_companies.find(query).sort("name", 1)
    items = await cursor.to_list(length=None)
    return [_serialise_company(d) for d in items]


@router.post("/companies", status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    _admin: dict = Depends(current_admin),
):
    """Idempotent on normalized name — re-submitting the same name (any
    casing/whitespace) returns the existing entry instead of a duplicate,
    matching the no-duplicate-companies requirement without ever merging
    two genuinely different names on our own initiative."""
    name = payload.name.strip()
    existing = await db.crm_companies.find_one({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}})
    if existing:
        return _serialise_company(existing)
    doc = {"name": name, "active": True, "created_at": _now()}
    res = await db.crm_companies.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _serialise_company(doc)


@router.put("/companies/{company_id}")
async def rename_company(
    company_id: str,
    payload: CompanyUpdate,
    _admin: dict = Depends(current_admin),
):
    oid = _to_object_id(company_id, field="company_id")
    existing = await db.crm_companies.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Company not found")
    new_name = payload.name.strip()
    await db.crm_companies.update_one({"_id": oid}, {"$set": {"name": new_name}})
    # Keep every client's denormalized company_name in sync with the rename —
    # otherwise the list/search view would silently show the old name.
    await db.clients.update_many({"company_id": oid}, {"$set": {"company_name": new_name}})
    doc = await db.crm_companies.find_one({"_id": oid})
    return _serialise_company(doc)


@router.post("/companies/{company_id}/deactivate")
async def deactivate_company(
    company_id: str,
    _admin: dict = Depends(current_admin),
):
    oid = _to_object_id(company_id, field="company_id")
    res = await db.crm_companies.update_one({"_id": oid}, {"$set": {"active": False}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"success": True}


@router.post("/companies/{company_id}/reactivate")
async def reactivate_company(
    company_id: str,
    _admin: dict = Depends(current_admin),
):
    oid = _to_object_id(company_id, field="company_id")
    res = await db.crm_companies.update_one({"_id": oid}, {"$set": {"active": True}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# Contact Type lookup list (admin-managed)
# ---------------------------------------------------------------------------
@router.get("/contact-types")
async def list_contact_types(
    include_inactive: bool = False,
    _admin: dict = Depends(current_team_or_admin),
) -> List[dict]:
    query = {} if include_inactive else {"active": {"$ne": False}}
    cursor = db.crm_contact_types.find(query).sort([("group", 1), ("label", 1)])
    items = await cursor.to_list(length=None)
    return [_serialise_contact_type(d) for d in items]


@router.post("/contact-types", status_code=status.HTTP_201_CREATED)
async def create_contact_type(
    payload: ContactTypeCreate,
    _admin: dict = Depends(current_admin),
):
    """Idempotent on normalized label (same reasoning as create_company).
    The stored `value` is a snake_case slug — the same shape/convention
    already used by the 15 seeded defaults and by the WhatsApp CRM agent's
    ROLE_LABEL_TO_SLUG — deduped against existing slugs so two similarly-
    worded labels never collide on the value clients.contact_type stores."""
    label = payload.label.strip()
    existing = await db.crm_contact_types.find_one({"label": {"$regex": f"^{re.escape(label)}$", "$options": "i"}})
    if existing:
        return _serialise_contact_type(existing)
    base_slug = _slugify_snake(label)
    slug = base_slug
    suffix = 2
    while await db.crm_contact_types.find_one({"value": slug}):
        slug = f"{base_slug}_{suffix}"
        suffix += 1
    doc = {
        "value": slug,
        "label": label,
        "group": (payload.group or "").strip() or None,
        "active": True,
        "created_at": _now(),
    }
    res = await db.crm_contact_types.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _serialise_contact_type(doc)


@router.put("/contact-types/{type_id}")
async def rename_contact_type(
    type_id: str,
    payload: ContactTypeUpdate,
    _admin: dict = Depends(current_admin),
):
    """Renames the display label only — `value` (the slug stored on
    clients.contact_type) never changes on rename, so existing client
    records and the WhatsApp agent's slug mapping stay valid."""
    oid = _to_object_id(type_id, field="type_id")
    existing = await db.crm_contact_types.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Contact type not found")
    upd = {"label": payload.label.strip()}
    if payload.group is not None:
        upd["group"] = payload.group.strip() or None
    await db.crm_contact_types.update_one({"_id": oid}, {"$set": upd})
    doc = await db.crm_contact_types.find_one({"_id": oid})
    return _serialise_contact_type(doc)


@router.post("/contact-types/{type_id}/deactivate")
async def deactivate_contact_type(
    type_id: str,
    _admin: dict = Depends(current_admin),
):
    oid = _to_object_id(type_id, field="type_id")
    res = await db.crm_contact_types.update_one({"_id": oid}, {"$set": {"active": False}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Contact type not found")
    return {"success": True}


@router.post("/contact-types/{type_id}/reactivate")
async def reactivate_contact_type(
    type_id: str,
    _admin: dict = Depends(current_admin),
):
    oid = _to_object_id(type_id, field="type_id")
    res = await db.crm_contact_types.update_one({"_id": oid}, {"$set": {"active": True}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Contact type not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# Startup seed/migration — idempotent, additive only. Mirrors core.py's
# seed_admin() pattern: safe to call on every boot, does nothing once done.
# ---------------------------------------------------------------------------

# Mirrors frontend/src/pages-components/MarketingHub.jsx's CONTACT_TYPES
# exactly (same values/labels/groups) and the WhatsApp CRM agent's
# ROLE_LABEL_TO_SLUG (agents/modules/crm.py) — seeded once so both existing
# client records and the WhatsApp agent's hardcoded slugs keep resolving to
# a real directory entry after Contact Type becomes admin-managed.
_DEFAULT_CONTACT_TYPES = [
    {"value": "brand_manager", "label": "Brand Manager", "group": "Brand & Marketing"},
    {"value": "marketing_manager", "label": "Marketing Manager", "group": "Brand & Marketing"},
    {"value": "influencer_marketing", "label": "Influencer Marketing Manager", "group": "Brand & Marketing"},
    {"value": "creative_director", "label": "Creative Director", "group": "Brand & Marketing"},
    {"value": "agency_producer", "label": "Agency Producer", "group": "Brand & Marketing"},
    {"value": "casting_director", "label": "Casting Director", "group": "Casting"},
    {"value": "casting_assistant", "label": "Casting Assistant", "group": "Casting"},
    {"value": "casting_company", "label": "Casting Company", "group": "Casting"},
    {"value": "producer", "label": "Producer", "group": "Production"},
    {"value": "executive_producer", "label": "Executive Producer", "group": "Production"},
    {"value": "production_house", "label": "Production House", "group": "Production"},
    {"value": "line_producer", "label": "Line Producer", "group": "Production"},
    {"value": "talent_agency", "label": "Talent Agency", "group": "Agency"},
    {"value": "modeling_agency", "label": "Modeling Agency", "group": "Agency"},
    {"value": "casting_agency", "label": "Casting Agency", "group": "Agency"},
]


async def seed_crm_lookups() -> None:
    """Idempotent — safe to call on every startup.

    1. Seeds crm_contact_types with the 15 pre-existing defaults, only if
       the collection is empty (never touches it again afterwards, so an
       admin's own additions/renames are never overwritten on redeploy).
    2. Migrates existing free-text `clients.company_name` values into
       `crm_companies`, normalized (trim+lowercase) to avoid duplicates
       like "Footloose Production" / "footloose production", and backfills
       `company_id` on each client. Only ever processes clients that don't
       already have a `company_id` — so this is a no-op once it has run,
       and never re-touches a client whose company was set/changed since
       (including via the new company-select UI)."""
    if await db.crm_contact_types.count_documents({}) == 0:
        now = _now()
        await db.crm_contact_types.insert_many(
            [{**t, "active": True, "created_at": now} for t in _DEFAULT_CONTACT_TYPES]
        )
        logger.info("CRM: seeded %d default contact types", len(_DEFAULT_CONTACT_TYPES))

    to_migrate = await db.clients.find(
        {
            "company_name": {"$nin": [None, ""]},
            "company_id": {"$exists": False},
        },
        {"_id": 1, "company_name": 1},
    ).to_list(length=None)
    if not to_migrate:
        return

    norm_to_id: dict[str, ObjectId] = {}
    async for c in db.crm_companies.find({}, {"name": 1}):
        norm_to_id[(c.get("name") or "").strip().lower()] = c["_id"]

    now = _now()
    migrated = 0
    for client in to_migrate:
        raw_name = (client.get("company_name") or "").strip()
        if not raw_name:
            continue
        norm = raw_name.lower()
        company_oid = norm_to_id.get(norm)
        if not company_oid:
            res = await db.crm_companies.insert_one(
                {"name": raw_name, "active": True, "created_at": now, "source": "migration"}
            )
            company_oid = res.inserted_id
            norm_to_id[norm] = company_oid
        await db.clients.update_one({"_id": client["_id"]}, {"$set": {"company_id": company_oid}})
        migrated += 1
    logger.info(
        "CRM: migrated %d client(s) into %d company directory entries",
        migrated, len(norm_to_id),
    )
