import os
import sys
import asyncio
import logging
from pymongo import ASCENDING, DESCENDING

# Ensure backend folder is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# JWT_SECRET and MONGO_URL are required and must be set in the environment —
# core.py reads them via os.environ[...] (no fallback) and raises loudly if
# either is missing. This script must never supply its own default.

from core import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("migrations")

async def run_migrations():
    logger.info("Starting Data Hub production database index migrations...")
    
    # 1. talents collection indexes
    # `phone` uniqueness is scoped to admin-created talents only
    # (source.type == "admin") -- kept here for consistency with core.py's
    # own startup index setup (core.py, seed_admin(), which is the
    # authoritative, always-applied version of this same index and will
    # self-heal it on every boot regardless of whether this standalone
    # script ever runs). A collection-wide unique constraint would block
    # the intentional coexistence of a legacy admin-created talent and its
    # later, authenticated Project Submission counterpart for the SAME
    # real person -- scoping to the admin population keeps real
    # accidental-duplicate protection for CSV imports (see also
    # check_duplicates(), services/import_duplicates.py, which reviews
    # collisions with the admin at import time) without blocking that
    # migration path.
    try:
        await db.talents.create_index(
            [("phone", ASCENDING)],
            unique=True,
            name="talents_phone_unique_admin_scope",
            partialFilterExpression={
                "phone": {"$type": "string"},
                "source.type": "admin",
            },
        )
        logger.info("Created admin-scoped partial unique index on talents.phone")
    except Exception as e:
        logger.warning(f"Could not create admin-scoped unique index on talents.phone (perhaps duplicate data already exists within the admin population): {e}")

    # Partial unique index for email
    try:
        await db.talents.create_index(
            [("email", ASCENDING)],
            unique=True,
            partialFilterExpression={"email": {"$type": "string"}}
        )
        logger.info("Created partial unique index on talents.email")
    except Exception as e:
        logger.warning(f"Could not create unique index on talents.email (perhaps duplicate data already exists): {e}")

    # Instagram handle index
    await db.talents.create_index([("instagram_handle", ASCENDING)])
    logger.info("Created index on talents.instagram_handle")
    
    # Import session references index
    await db.talents.create_index([("import_id", ASCENDING)])
    logger.info("Created index on talents.import_id")
    
    # Status and updated_at
    await db.talents.create_index([("status", ASCENDING)])
    await db.talents.create_index([("updated_at", DESCENDING)])
    logger.info("Created indexes on talents.status and talents.updated_at")

    # 2. import_sessions collection indexes
    await db.import_sessions.create_index([("status", ASCENDING)])
    await db.import_sessions.create_index([("created_at", DESCENDING)])
    logger.info("Created indexes on import_sessions.status and import_sessions.created_at")

    # 3. import_history collection indexes
    await db.import_history.create_index([("file_checksum", ASCENDING)])
    logger.info("Created index on import_history.file_checksum")
    
    logger.info("=============================================")
    logger.info("Database index migration completed successfully!")
    logger.info("=============================================")

if __name__ == "__main__":
    asyncio.run(run_migrations())
