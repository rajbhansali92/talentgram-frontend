"""
WhatsApp Worker — MongoDB Client
Async motor client, mirroring the backend connection pattern.
"""
import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

import config

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect() -> AsyncIOMotorDatabase:
    """Connect to MongoDB and return the database handle."""
    global _client, _db
    if _db is not None:
        return _db
    logger.info("db: connecting to MongoDB…")
    _client = AsyncIOMotorClient(config.MONGO_URL)
    _db = _client[config.MONGO_DB_NAME]
    logger.info("db: connected")
    return _db


async def close() -> None:
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
        logger.info("db: connection closed")


def get_db() -> AsyncIOMotorDatabase:
    """Return the cached database handle. Must call connect() first."""
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db
