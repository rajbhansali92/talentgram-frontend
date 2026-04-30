"""Regression tests for the user/role system.

Validates:
- `generate_temp_password` is cryptographically strong and has required classes
- `generate_invite_token` is URL-safe and long enough
- `_public_user` strips hashes / invite tokens
- `USER_ROLES` / `USER_STATUSES` constants are the canonical source.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import (  # noqa: E402
    USER_ROLES,
    USER_STATUSES,
    _public_user,
    generate_invite_token,
    generate_temp_password,
)


def test_roles_are_canonical():
    assert USER_ROLES == ("admin", "team")


def test_statuses_are_canonical():
    assert USER_STATUSES == ("active", "invited", "disabled")


def test_temp_password_defaults_to_14_chars():
    pw = generate_temp_password()
    assert len(pw) == 14


def test_temp_password_has_required_classes():
    for _ in range(50):
        pw = generate_temp_password()
        assert re.search(r"[a-zA-Z]", pw), f"letters missing: {pw!r}"
        assert re.search(r"\d", pw), f"digits missing: {pw!r}"
        assert re.search(r"[!@#$%^&*]", pw), f"symbols missing: {pw!r}"


def test_temp_password_avoids_ambiguous_chars():
    # Must not contain O, 0, l, 1, I to avoid copy-paste confusion.
    for _ in range(50):
        pw = generate_temp_password()
        for ch in "O0l1I":
            assert ch not in pw, f"ambiguous char {ch!r} in {pw!r}"


def test_invite_tokens_are_unique_and_urlsafe():
    tokens = {generate_invite_token() for _ in range(200)}
    assert len(tokens) == 200  # no collisions in 200 samples
    for t in tokens:
        assert len(t) >= 40
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", t), f"non-URL-safe: {t!r}"


def test_public_user_strips_secrets():
    u = {
        "id": "u1",
        "name": "Raj",
        "email": "raj@example.com",
        "role": "team",
        "status": "active",
        "created_at": "2026-04-24T00:00:00+00:00",
        "last_login": None,
        "password_hash": "bcrypt$hash",
        "invite_token": "super-secret",
        "invite_expires_at": "2026-05-01T00:00:00+00:00",
        "invited_by": "admin-1",
    }
    out = _public_user(u)
    assert "password_hash" not in out
    assert "invite_token" not in out
    assert "invite_expires_at" not in out
    assert "invited_by" not in out
    assert out["role"] == "team"
