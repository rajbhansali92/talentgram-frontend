"""Shared test-fixture constants for the backend test suite.

Local/test-database credentials, not production secrets — these are the
seed-admin values every test file already assumes exist in whatever database
MONGO_URL points at for that run (see each test file's own MONGO_URL default,
which is mongodb://localhost:27017 unless explicitly overridden). Centralized
here, rather than duplicated as a literal string in ~18 files, and overridable
via TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD for a differently-seeded database.
"""
import os

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@talentgram.com")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "Admin@123")
