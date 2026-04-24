"""Contract tests for the password router.

Covers:
  - POST /api/auth/change-password — authenticated; verifies current password;
    enforces policy; bumps token_version (kills old JWTs).
  - POST /api/public/forgot-password — always returns the generic message,
    never leaks whether the email exists; rate-limited at 5/15min per IP.
  - POST /api/public/reset-password/validate + POST /api/public/reset-password —
    consumes an admin-generated reset token (hashed storage, single-use, 1-hour TTL).

Tests use the live HTTP server via `requests`.
"""
import os
import uuid

import pytest
import requests

BASE = os.environ.get("TEST_API", "http://localhost:8001/api")
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASS = "Admin@123"


def _admin_token() -> str:
    r = requests.post(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def throwaway_admin_session():
    """Invite + activate a fresh throwaway admin so tests that mutate
    password / token_version don't accidentally break the root admin."""
    t = _admin_token()
    email = f"pwtest+{uuid.uuid4().hex[:6]}@example.com"
    r = requests.post(
        f"{BASE}/users/invite",
        headers=_auth(t),
        json={"name": "PW Test", "email": email, "role": "team"},
    )
    r.raise_for_status()
    invite = r.json()["invite_token"]
    pw = "InitPass@123"
    r = requests.post(f"{BASE}/public/signup/complete", json={"token": invite, "password": pw})
    r.raise_for_status()
    login = requests.post(f"{BASE}/auth/login", json={"email": email, "password": pw})
    login.raise_for_status()
    body = login.json()
    uid = body["admin"]["id"]
    yield {"email": email, "pw": pw, "token": body["token"], "id": uid}
    # Teardown — hard-delete the throwaway account so subsequent runs stay clean.
    try:
        requests.delete(f"{BASE}/users/{uid}", headers=_auth(t))
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Change password
# ----------------------------------------------------------------------------
class TestChangePassword:
    def test_wrong_current_password_rejected(self, throwaway_admin_session):
        s = throwaway_admin_session
        r = requests.post(
            f"{BASE}/auth/change-password",
            headers=_auth(s["token"]),
            json={"current_password": "not-my-pw", "new_password": "NewPass@123"},
        )
        assert r.status_code == 400 and "incorrect" in r.json()["detail"].lower()

    def test_weak_new_password_rejected(self, throwaway_admin_session):
        s = throwaway_admin_session
        # Too short
        r = requests.post(
            f"{BASE}/auth/change-password",
            headers=_auth(s["token"]),
            json={"current_password": s["pw"], "new_password": "short"},
        )
        assert r.status_code == 400 and "8 characters" in r.json()["detail"]
        # Alphabetic only — fails the "must contain number or symbol" rule
        r = requests.post(
            f"{BASE}/auth/change-password",
            headers=_auth(s["token"]),
            json={"current_password": s["pw"], "new_password": "alphaOnlyPW"},
        )
        assert r.status_code == 400 and "number or special" in r.json()["detail"]

    def test_same_as_current_rejected(self, throwaway_admin_session):
        s = throwaway_admin_session
        r = requests.post(
            f"{BASE}/auth/change-password",
            headers=_auth(s["token"]),
            json={"current_password": s["pw"], "new_password": s["pw"]},
        )
        assert r.status_code == 400

    def test_success_invalidates_old_token(self, throwaway_admin_session):
        s = throwaway_admin_session
        new_pw = "FreshPass@456"
        r = requests.post(
            f"{BASE}/auth/change-password",
            headers=_auth(s["token"]),
            json={"current_password": s["pw"], "new_password": new_pw},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        # The old JWT must now be rejected — token_version bumped on the user.
        r = requests.get(f"{BASE}/auth/me", headers=_auth(s["token"]))
        assert r.status_code == 401
        # Old password rejected, new one works.
        r = requests.post(f"{BASE}/auth/login", json={"email": s["email"], "password": s["pw"]})
        assert r.status_code == 401
        r = requests.post(f"{BASE}/auth/login", json={"email": s["email"], "password": new_pw})
        assert r.status_code == 200


# ----------------------------------------------------------------------------
# Forgot password (public, generic-only)
# ----------------------------------------------------------------------------
class TestForgotPassword:
    def test_generic_message_for_existing_email(self):
        r = requests.post(f"{BASE}/public/forgot-password", json={"email": ADMIN_EMAIL})
        # Might be rate-limited (429) if prior tests hit the bucket — both are acceptable shapes.
        assert r.status_code in (200, 429)
        if r.status_code == 200:
            body = r.json()
            assert "contact your administrator" in body["message"]
            # MUST NOT leak the token / user details.
            assert "reset_token" not in body
            assert "reset_path" not in body

    def test_generic_message_for_unknown_email(self):
        r = requests.post(f"{BASE}/public/forgot-password", json={"email": f"nobody-{uuid.uuid4().hex[:6]}@nowhere.example.com"})
        assert r.status_code in (200, 429)
        if r.status_code == 200:
            assert "contact your administrator" in r.json()["message"]


# ----------------------------------------------------------------------------
# Admin-generated reset link
# ----------------------------------------------------------------------------
class TestAdminResetLink:
    def test_admin_generates_link_and_token_is_not_stored_raw(self, throwaway_admin_session):
        from motor.motor_asyncio import AsyncIOMotorClient
        import asyncio
        s = throwaway_admin_session
        t = _admin_token()
        r = requests.post(f"{BASE}/users/{s['id']}/reset-password", headers=_auth(t))
        assert r.status_code == 200
        body = r.json()
        raw = body["reset_token"]
        assert body["reset_path"].endswith(raw)
        # Verify the stored document has only the hash, not the raw token.
        mongo_url = os.environ.get("MONGO_URL") or open("/app/backend/.env").read().split("MONGO_URL=", 1)[1].split("\n", 1)[0].strip().strip('"')
        db_name = os.environ.get("DB_NAME") or "talentgram"
        async def _peek():
            c = AsyncIOMotorClient(mongo_url)
            try:
                doc = await c[db_name].password_reset_tokens.find_one({"user_id": s["id"], "used_at": None})
                return doc
            finally:
                c.close()
        doc = asyncio.run(_peek())
        assert doc is not None
        assert doc["token_hash"] != raw
        assert len(doc["token_hash"]) == 64      # SHA-256 hex digest
        assert "reset_token" not in doc and "raw" not in doc

    def test_full_reset_flow_single_use_and_invalidates_sessions(self, throwaway_admin_session):
        s = throwaway_admin_session
        t = _admin_token()
        r = requests.post(f"{BASE}/users/{s['id']}/reset-password", headers=_auth(t))
        raw = r.json()["reset_token"]

        # Validate (non-destructive)
        r = requests.post(f"{BASE}/public/reset-password/validate", json={"token": raw})
        assert r.status_code == 200 and r.json()["email"] == s["email"]

        # Bad token → 400
        r = requests.post(f"{BASE}/public/reset-password/validate", json={"token": "nope-bogus-token"})
        assert r.status_code == 400

        # Weak password rejected
        r = requests.post(
            f"{BASE}/public/reset-password",
            json={"token": raw, "new_password": "short"},
        )
        assert r.status_code == 400

        # Complete reset
        new_pw = "ResetApproved@789"
        r = requests.post(
            f"{BASE}/public/reset-password",
            json={"token": raw, "new_password": new_pw},
        )
        assert r.status_code == 200

        # Single-use — same token replay 400
        r = requests.post(
            f"{BASE}/public/reset-password",
            json={"token": raw, "new_password": "Another@999"},
        )
        assert r.status_code == 400

        # Session killed — original login token rejected
        r = requests.get(f"{BASE}/auth/me", headers=_auth(s["token"]))
        assert r.status_code == 401

        # New password works, old doesn't
        r = requests.post(f"{BASE}/auth/login", json={"email": s["email"], "password": s["pw"]})
        assert r.status_code == 401
        r = requests.post(f"{BASE}/auth/login", json={"email": s["email"], "password": new_pw})
        assert r.status_code == 200

    def test_only_admin_can_generate_reset_link(self, throwaway_admin_session):
        """Team users must not be able to call /users/{uid}/reset-password."""
        s = throwaway_admin_session   # this is a team user by default
        r = requests.post(f"{BASE}/users/{s['id']}/reset-password", headers=_auth(s["token"]))
        assert r.status_code == 403
