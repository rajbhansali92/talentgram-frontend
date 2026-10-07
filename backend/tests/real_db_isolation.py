"""Suite-safe real-DB handle for tests that exercise the real FastAPI app / agents against the
local dev Mongo.

Why this exists: when the whole backend suite runs in one process, earlier-collected test
modules leave global state behind — some replace `core.db` (and every `from core import db`
copy) with a MagicMock or a hand-rolled FakeDB, one sets MONGO_URL to a dummy host, one forces
DB_NAME to its own scratch database, and a motor client can stay bound to an event loop that is
already closed. A real-DB test collected later then errors before asserting anything (the same
reason test_casting_call_send.py errors in a full run).

`install_real_db()` gives the CALLING module its own real client on the running loop and points
every module-level `db` at it; the returned `restore()` puts the previous objects back so no
other module sees any difference. Call it from a module-scoped async fixture.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import core
from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


def _is_db_or_stand_in(value) -> bool:
    return isinstance(value, (MagicMock, AsyncIOMotorDatabase)) or type(value).__name__.startswith("Fake")


def intended_db_name() -> str:
    """The DB an un-polluted run uses (backend/.env, where the seeded admin lives) — not
    core.DB_NAME, which an earlier-collected module may already have forced to its own."""
    return dotenv_values(Path(core.__file__).parent / ".env").get("DB_NAME") or core.DB_NAME


def install_real_db(mongo_url: str):
    """Returns (real_db, restore). Must be called while the target event loop is running."""
    client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=10_000)
    real_db = client[intended_db_name()]
    swapped = [
        (mod, mod.db) for mod in list(sys.modules.values())
        if _is_db_or_stand_in(getattr(mod, "db", None))
    ]
    for mod, _ in swapped:
        mod.db = real_db

    def restore() -> None:
        for mod, original in swapped:
            mod.db = original
        client.close()

    return real_db, restore
