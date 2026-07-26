"""Regression tests for the concurrent session+user lookup in core.current_user.

Perf audit finding: each Mongo round trip measured ~500-600ms in production
(Railway<->Atlas cross-region latency). current_user ran the session-
revocation check and the user lookup SEQUENTIALLY even though neither
depends on the other's result — on every single admin-plane request. Fixed
by running them concurrently via asyncio.gather; these tests prove
correctness is unchanged (revoked session still rejected, missing user
still rejected, valid session+user still succeeds) with a fake db that
requires no live Mongo.
"""
import os
import sys

for k, v in {
    "MONGO_URL": "mongodb://localhost:27017", "DB_NAME": "test", "JWT_SECRET": "d",
    "RESEND_API_KEY": "d", "SENDGRID_API_KEY": "d", "CLOUDINARY_CLOUD_NAME": "d",
    "CLOUDINARY_API_KEY": "d", "CLOUDINARY_API_SECRET": "d",
    "ADMIN_EMAIL": "a@b.co", "ADMIN_PASSWORD": "d",
}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.abspath("backend"))

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import core


class FakeCollection:
    def __init__(self, doc=None):
        self.doc = doc
        self.find_one = AsyncMock(return_value=doc)


class FakeDB:
    def __init__(self, session_doc=None, user_doc=None):
        self.sessions = FakeCollection(session_doc)
        self.users = FakeCollection(user_doc)


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _token_for(email="a@b.co", jti="jti-1", tv=0, role="admin"):
    return core.make_token({"email": email, "role": role, "id": "u1", "tv": tv, "jti": jti}, days=1)


@pytest.mark.asyncio
async def test_current_user_succeeds_with_valid_session_and_user(monkeypatch):
    token = _token_for()
    fake_db = FakeDB(
        session_doc={"jti": "jti-1", "revoked": False},
        user_doc={"email": "a@b.co", "status": "active", "role": "admin", "token_version": 0},
    )
    monkeypatch.setattr(core, "db", fake_db)

    user = await core.current_user(_creds(token))

    assert user["email"] == "a@b.co"
    fake_db.sessions.find_one.assert_awaited_once_with({"jti": "jti-1"})
    fake_db.users.find_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_user_rejects_revoked_session(monkeypatch):
    token = _token_for()
    fake_db = FakeDB(
        session_doc={"jti": "jti-1", "revoked": True},
        user_doc={"email": "a@b.co", "status": "active", "role": "admin", "token_version": 0},
    )
    monkeypatch.setattr(core, "db", fake_db)

    with pytest.raises(HTTPException) as exc:
        await core.current_user(_creds(token))
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_current_user_rejects_missing_user(monkeypatch):
    token = _token_for()
    fake_db = FakeDB(session_doc={"jti": "jti-1", "revoked": False}, user_doc=None)
    monkeypatch.setattr(core, "db", fake_db)

    with pytest.raises(HTTPException) as exc:
        await core.current_user(_creds(token))
    assert exc.value.status_code == 401
    assert "not found" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_current_user_works_without_jti_and_skips_session_lookup(monkeypatch):
    """No jti in the token -> no session lookup should even be attempted."""
    token = core.make_token({"email": "a@b.co", "role": "admin", "id": "u1", "tv": 0}, days=1)
    # Remove jti make_token always injects, to simulate a legacy token shape.
    import jwt as pyjwt
    data = pyjwt.decode(token, options={"verify_signature": False})
    data.pop("jti", None)
    token = pyjwt.encode(data, core.JWT_SECRET, algorithm="HS256")

    fake_db = FakeDB(user_doc={"email": "a@b.co", "status": "active", "role": "admin", "token_version": 0})
    monkeypatch.setattr(core, "db", fake_db)

    user = await core.current_user(_creds(token))

    assert user["email"] == "a@b.co"
    fake_db.sessions.find_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_and_user_lookups_run_concurrently(monkeypatch):
    """Proves the fix: both reads must be in-flight at the same time, not
    sequential. A sequential implementation would only ever have one
    in-flight count > 0 at a time; gather puts both in flight together."""
    order = []

    async def slow_session_lookup(_query):
        order.append("session:start")
        await asyncio.sleep(0.05)
        order.append("session:end")
        return {"jti": "jti-1", "revoked": False}

    async def slow_user_lookup(_query, _proj):
        order.append("user:start")
        await asyncio.sleep(0.05)
        order.append("user:end")
        return {"email": "a@b.co", "status": "active", "role": "admin", "token_version": 0}

    fake_db = MagicMock()
    fake_db.sessions.find_one = slow_session_lookup
    fake_db.users.find_one = slow_user_lookup
    monkeypatch.setattr(core, "db", fake_db)

    token = _token_for()
    await core.current_user(_creds(token))

    # Concurrent: both starts happen before either end. Sequential would
    # produce session:start, session:end, user:start, user:end instead.
    assert order.index("user:start") < order.index("session:end"), (
        f"lookups ran sequentially, not concurrently: {order}"
    )
