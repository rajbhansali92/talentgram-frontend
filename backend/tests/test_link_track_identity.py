"""Client-View identity-attribution audit — /public/links/{slug}/track.

Root cause of the reported "Client"/"Guest" mislabeling on the admin results
page (Talent Interaction Details, Client Activity Timeline, viewer sessions):
every /track call site in the frontend (ClientView.jsx's "open",
"review_talent", "view_talent", "view_media" and LazyVideoPlayer.jsx's
"watch_video") was missing the Authorization header, so
track_link_event()'s decode_viewer(authorization) always received None and
every event was stored with viewer_email/viewer_name = null — indistinguishable
from a genuinely anonymous visit. Downloads and decision actions were never
affected; their frontend call sites already send the header (see
markReviewed/setAction/logDownload), which is why those already displayed the
real viewer name in production.

This backend behavior itself was already correct — decode_viewer() resolves
the identity whenever it's given a valid token. These tests lock that in and
document the (intentional, unrelated to the bug) graceful-degradation
behavior when no header is present at all, so a future regression on either
side is caught here rather than live on the admin results page again.

Each test does its own setup/teardown inline (no shared async fixture) —
matching this suite's own convention (see test_help_command.py) rather than
a yield-based pytest_asyncio.fixture, which trips over motor's AsyncIOMotorClient
being bound to whichever event loop was live at import time when torn down
across more than one test in the same session.
"""
import datetime
import os
import sys
import uuid

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "talentgram")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")

sys.path.insert(0, os.path.abspath("backend"))

import jwt as pyjwt

from core import db, JWT_SECRET  # noqa: E402
from routers.links import track_link_event, LinkTrackIn  # noqa: E402

# core.mongo_client (an AsyncIOMotorClient) is created once at import time and
# stays bound to whichever event loop is running then — pytest-asyncio's
# default per-function loop scope tears that loop down after the first test,
# leaving every later test's DB call hitting a closed loop. module-scoping the
# loop for this whole file keeps one live loop across all of it (same fix as
# test_help_command.py's `pytestmark = pytest.mark.asyncio(loop_scope="module")`).
pytestmark = pytest.mark.asyncio(loop_scope="module")


def _viewer_token(slug, email, name, expired=False):
    """Builds a viewer JWT directly (not via core.make_token) — make_token()
    fires an unawaited loop.create_task(db.sessions.insert_one(...)) that
    outlives a single pytest-asyncio function-scoped event loop, breaking
    every test after the first in the same session with "Event loop is
    closed". That side effect (session-list bookkeeping) is irrelevant to
    what these tests exercise (decode_viewer's identity resolution), so
    building the token's exact same payload shape directly sidesteps it."""
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=-1 if expired else 7)
    return pyjwt.encode({"role": "viewer", "slug": slug, "email": email, "name": name, "exp": exp}, JWT_SECRET, algorithm="HS256")


async def _make_test_link():
    link_id = str(uuid.uuid4())
    slug = f"zzz-test-track-identity-{uuid.uuid4().hex[:8]}"
    await db.links.insert_one({
        "id": link_id,
        "slug": slug,
        "title": "ZZZ_TEST Track Identity",
        "talent_ids": [],
        "submission_ids": [],
        "is_public": True,
        "password": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    return link_id, slug


async def _cleanup_test_link(link_id):
    await db.links.delete_one({"id": link_id})
    await db.link_events.delete_many({"link_id": link_id})


async def test_track_event_with_valid_viewer_token_is_attributed_to_that_viewer():
    link_id, slug = await _make_test_link()
    try:
        token = _viewer_token(slug, "aman@example.com", "Aman Gupta")

        await track_link_event(
            slug,
            LinkTrackIn(event_type="view_talent", session_id="sess-1", talent_id="t1"),
            authorization=f"Bearer {token}",
        )

        events = await db.link_events.find({"link_id": link_id}).to_list(10)
        assert len(events) == 1
        assert events[0]["viewer_email"] == "aman@example.com"
        assert events[0]["viewer_name"] == "Aman Gupta"
        assert events[0]["event_type"] == "view_talent"
    finally:
        await _cleanup_test_link(link_id)


async def test_track_event_for_every_event_type_is_attributed_when_a_token_is_present():
    """Covers all four event types the frontend fires through this endpoint —
    "open", "review_talent", "view_talent", "view_media" (LazyVideoPlayer's
    "watch_video" goes through the exact same code path) — none of them may
    silently drop identity when a valid token is sent."""
    link_id, slug = await _make_test_link()
    try:
        token = _viewer_token(slug, "harshita@example.com", "Harshita K")

        for event_type, extra in [
            ("open", {}),
            ("review_talent", {"talent_id": "t1"}),
            ("view_talent", {"talent_id": "t1"}),
            ("view_media", {"talent_id": "t1", "media_id": "m1"}),
        ]:
            await track_link_event(
                slug,
                LinkTrackIn(event_type=event_type, session_id="sess-2", **extra),
                authorization=f"Bearer {token}",
            )

        events = await db.link_events.find({"link_id": link_id}).to_list(10)
        assert len(events) == 4
        assert all(e["viewer_email"] == "harshita@example.com" for e in events)
        assert all(e["viewer_name"] == "Harshita K" for e in events)
    finally:
        await _cleanup_test_link(link_id)


async def test_track_event_with_no_authorization_header_stores_null_identity_not_a_guess():
    """Documents the (correct, unrelated-to-the-bug) graceful-degradation
    behavior: a genuinely missing token stores null, never a fabricated
    identity. The actual bug was the frontend never sending this header in
    normal operation, not this fallback itself."""
    link_id, slug = await _make_test_link()
    try:
        await track_link_event(
            slug,
            LinkTrackIn(event_type="open", session_id="sess-3"),
            authorization=None,
        )

        events = await db.link_events.find({"link_id": link_id}).to_list(10)
        assert len(events) == 1
        assert events[0]["viewer_email"] is None
        assert events[0]["viewer_name"] is None
    finally:
        await _cleanup_test_link(link_id)


async def test_track_event_with_expired_token_stores_null_identity():
    """An expired session degrades to anonymous rather than throwing or
    misattributing to a stale identity — matches the intentional 7-day
    re-authentication policy in identify_viewer()."""
    link_id, slug = await _make_test_link()
    try:
        expired = _viewer_token(slug, "old@example.com", "Old Viewer", expired=True)

        await track_link_event(
            slug,
            LinkTrackIn(event_type="open", session_id="sess-4"),
            authorization=f"Bearer {expired}",
        )

        events = await db.link_events.find({"link_id": link_id}).to_list(10)
        assert len(events) == 1
        assert events[0]["viewer_email"] is None
    finally:
        await _cleanup_test_link(link_id)


async def test_track_event_with_a_token_scoped_to_a_different_link_stores_null_identity():
    """Hardening-pass finding: every other identity-bearing endpoint in this
    file (/seen, /reviewed, /action, /download-log, …) rejects a token whose
    own `slug` claim doesn't match the link being acted on; track_link_event
    was the one exception. A viewer's legitimate token for link A must not be
    usable to attribute fabricated tracking events to their own identity on
    unrelated link B — it should degrade to anonymous, exactly like a missing
    token, never to a hard failure (this endpoint intentionally still accepts
    genuinely anonymous beacons) and never by trusting the mismatched scope."""
    link_a_id, slug_a = await _make_test_link()
    link_b_id, slug_b = await _make_test_link()
    try:
        token_for_a = _viewer_token(slug_a, "aman@example.com", "Aman Gupta")

        await track_link_event(
            slug_b,
            LinkTrackIn(event_type="view_talent", session_id="sess-5", talent_id="t1"),
            authorization=f"Bearer {token_for_a}",
        )

        events_b = await db.link_events.find({"link_id": link_b_id}).to_list(10)
        assert len(events_b) == 1
        assert events_b[0]["viewer_email"] is None
        assert events_b[0]["viewer_name"] is None

        events_a = await db.link_events.find({"link_id": link_a_id}).to_list(10)
        assert len(events_a) == 0
    finally:
        await _cleanup_test_link(link_a_id)
        await _cleanup_test_link(link_b_id)
