"""Marketing — lightweight CRM router.

Tracks two collections:
  • `clients`      — basic contact records (name, company, phone).
  • `interactions` — call/meeting/email notes against a client.

Both endpoints use BSON ObjectId for `_id` (per spec) — `clients._id` and
`interactions.client_id` are stored as ObjectId, so the foreign-key
relationship can be queried efficiently. Outgoing JSON converts ObjectId
to string so the FastAPI response is JSON-serialisable.

This module is fully self-contained:
  • No import from `routers.notifications`.
  • No global state mutation.
  • Single `router = APIRouter(...)` definition (line near top).

NOTE on prefix: the spec asked for `prefix="/marketing"`, but this
deployment's Kubernetes ingress only forwards paths under `/api/*` to
the backend pod. Using a bare `/marketing` would 404 at the ingress
layer. Prefix is therefore set to `/api/marketing` so the routes are
reachable end-to-end. Tags remain `Marketing` as requested.
"""
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

# Re-use the singleton Motor client from core. The marketing router
# explicitly does NOT pull in the notifications module — this is a
# clean, parallel surface.
from core import current_team_or_admin, db

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
    phone_number: Optional[str] = Field(default=None, max_length=40)


class InteractionCreate(BaseModel):
    client_id: str = Field(..., description="ObjectId of the client (24-hex)")
    # Free-form so callers can use 'call' / 'email' / 'meeting' / 'whatsapp'
    # without backend changes for new types.
    type: str = Field(..., min_length=1, max_length=40)
    notes: Optional[str] = Field(default=None, max_length=4000)


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
        "phone_number": doc.get("phone_number"),
        "created_at": doc.get("created_at"),
        "last_contacted_date": doc.get("last_contacted_date"),
    }


def _serialise_interaction(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "client_id": str(doc["client_id"]) if doc.get("client_id") else None,
        "type": doc.get("type"),
        "notes": doc.get("notes"),
        "created_at": doc.get("created_at"),
    }


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
    now = _now()
    doc = {
        "name": payload.name.strip(),
        "company_name": (payload.company_name or "").strip() or None,
        "phone_number": (payload.phone_number or "").strip() or None,
        "created_at": now,
        "last_contacted_date": now,
    }
    res = await db.clients.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _serialise_client(doc)


@router.get("/clients")
async def list_clients(
    _admin: dict = Depends(current_team_or_admin),
) -> List[dict]:
    """Return every client. Sorted by most-recent contact first so a
    sales user sees the freshest leads at the top of the table."""
    cursor = db.clients.find().sort("last_contacted_date", -1)
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
