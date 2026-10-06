"""P1 incident 2026-10-06 — "Too many requests. Please try again later." for legitimate talents.

Two defects, one regression file:

A. Rate limits were keyed on the Vercel proxy's shared AWS egress IP, so unrelated
   talents pooled into one bucket. The proxy now sends the real client IP in
   X-TG-Client-IP, authenticated by X-TG-Proxy-Secret; core.get_client_ip honours it
   ONLY with a valid secret (env TG_PROXY_SHARED_SECRET) and otherwise behaves exactly
   as before. Thresholds are unchanged (otp_send 5/h, otp_verify 10/h, ...).

B. A talent who verified via an ALTERNATE ("Merge Different Emails") email got a portal
   token minted with the PRIMARY email, so core.verify_email_ownership said no, the
   submission start returned 403 "verify your email", and the frontend sent another OTP
   — burning the rate-limit bucket. Ownership now also succeeds when the credential's
   canonical talent id equals the request email's canonical talent id.

The secret used here is a throwaway test string, never a real one.
"""
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import httpx
import jwt
import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.requests import Request

import core
import routers.auth as rauth
import routers.password as rpassword
import routers.submissions as rsub
from server import app

pytestmark = pytest.mark.asyncio(loop_scope="module")

_TAG = "TEST_IPALT_"
_RUN = _uuid.uuid4().hex[:8]
_TEST_SECRET = "unit-test-proxy-secret-not-real"
_real_db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


# --------------------------------------------------------------------------- helpers
def _req(headers: dict, peer: str = "100.64.0.9", cookies: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "headers": raw, "client": (peer, 5555), "method": "POST", "path": "/"})


def _tg(ip: str, secret: str = _TEST_SECRET) -> dict:
    return {"x-tg-client-ip": ip, "x-tg-proxy-secret": secret}


_ip_counter = {"n": 0}


def _fresh_ip() -> str:
    """Unique TEST-NET-2 address per call, so rows never collide across tests/runs."""
    _ip_counter["n"] += 1
    return f"198.51.{int(_RUN[:2], 16)}.{_ip_counter['n'] % 250 + 1}"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("TG_PROXY_SHARED_SECRET", _TEST_SECRET)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def _real_db_for_this_file():
    prior = (core.db, rauth.db, rsub.db)
    core.db = rauth.db = rsub.db = _real_db
    try:
        yield
    finally:
        core.db, rauth.db, rsub.db = prior
        await _real_db.talents.delete_many({"id": {"$regex": f"^{_TAG}{_RUN}"}})
        await _real_db.projects.delete_many({"id": {"$regex": f"^{_TAG}{_RUN}"}})
        await _real_db.submissions.delete_many({"project_id": {"$regex": f"^{_TAG}{_RUN}"}})
        await _real_db.submission_drafts.delete_many({"email": {"$regex": f"^{_TAG.lower()}{_RUN}"}})
        for coll, field in (("otp_codes", "email"), ("otp_audit_logs", "email"), ("rate_limits", "email")):
            await _real_db[coll].delete_many({field: {"$regex": f"^{_TAG.lower()}{_RUN}"}})
        await _real_db.rate_limits.delete_many({"ip": {"$regex": f"^198\\.51\\.{int(_RUN[:2], 16)}\\."}})
        await _real_db.trusted_devices.delete_many({"talent_id": {"$regex": f"^{_TAG}{_RUN}"}})


@pytest.fixture
def sent_codes(monkeypatch):
    """Capture OTP codes instead of emailing them."""
    box = {}

    async def fake_send(email, otp):
        box[email] = otp
        return True

    monkeypatch.setattr(rauth, "send_otp_email", fake_send)
    return box


def _email(label: str) -> str:
    return f"{_TAG.lower()}{_RUN}.{label}@example.com"


async def _talent(idx, email, alternates=(), status="SUBMITTED", **extra):
    doc = {
        "id": f"{_TAG}{_RUN}_{idx}", "name": f"Test {idx}", "email": email, "normalized_email": email,
        "alternate_emails": list(alternates), "status": status, "media": [],
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    doc.update(extra)
    await _real_db.talents.insert_one(doc)
    return doc


async def _send(client, email, headers):
    return await client.post("/api/auth/otp/send", json={"email": email}, headers=headers)


# =========================================================================== A. client IP
async def test_trusted_header_with_valid_secret_sets_the_client_ip(secret):
    r = _req({"x-forwarded-for": "54.1.2.3", **_tg("49.36.10.20")})
    assert core.get_client_ip(r) == "49.36.10.20"
    assert core.client_ip(r) == "49.36.10.20"
    assert rpassword._client_ip(r) == "49.36.10.20"          # no private copies of the parsing logic
    assert core.get_client_ip(_req(_tg("2001:DB8:0:0:0:0:0:1"))) == "2001:db8::1"   # one key per client


async def test_invalid_or_missing_secret_is_ignored(secret):
    xff = {"x-forwarded-for": "54.1.2.3"}
    assert core.get_client_ip(_req({**xff, **_tg("49.36.10.20", secret="wrong")})) == "54.1.2.3"
    assert core.get_client_ip(_req({**xff, "x-tg-client-ip": "49.36.10.20"})) == "54.1.2.3"          # no secret header
    assert core.get_client_ip(_req({**xff, **_tg("49.36.10.20", secret="")})) == "54.1.2.3"          # empty secret


async def test_unconfigured_server_ignores_the_header_and_keeps_previous_behaviour(monkeypatch):
    monkeypatch.delenv("TG_PROXY_SHARED_SECRET", raising=False)
    assert core.get_client_ip(_req({"x-forwarded-for": "54.1.2.3", **_tg("49.36.10.20")})) == "54.1.2.3"
    assert core.get_client_ip(_req({**_tg("49.36.10.20", secret="")}, peer="100.64.0.9")) == "100.64.0.9"
    # an empty configured secret must never "match" an empty/absent header
    monkeypatch.setenv("TG_PROXY_SHARED_SECRET", "")
    assert core.get_client_ip(_req({"x-forwarded-for": "54.1.2.3", "x-tg-client-ip": "1.1.1.1"})) == "54.1.2.3"


async def test_garbled_client_ip_falls_back_even_with_a_valid_secret(secret):
    for bad in ("", "not-an-ip", "1.2.3.4, 5.6.7.8", "1.2.3.4:80", "999.1.1.1"):
        assert core.get_client_ip(_req({"x-forwarded-for": "54.1.2.3", **_tg(bad)})) == "54.1.2.3", bad


async def test_attacker_cannot_pick_another_users_ip(secret):
    """An outsider talking to Railway directly doesn't know the secret: whatever IP they
    claim, their effective identity stays what Railway's edge reports (their own)."""
    victim = "49.36.10.20"
    for guess in ("", "guess", _TEST_SECRET[:-1], _TEST_SECRET + "x", _TEST_SECRET.upper()):
        r = _req({"x-forwarded-for": "198.51.100.77", **_tg(victim, secret=guess)})
        assert core.get_client_ip(r) == "198.51.100.77"


async def test_pre_existing_xff_and_peer_behaviour_is_unchanged(monkeypatch):
    monkeypatch.delenv("TG_PROXY_SHARED_SECRET", raising=False)
    assert core.get_client_ip(_req({"x-forwarded-for": " 1.1.1.1 , 2.2.2.2"})) == "1.1.1.1"
    assert core.get_client_ip(_req({}, peer="10.0.0.5")) == "10.0.0.5"
    assert core.get_client_ip(Request({"type": "http", "headers": [], "client": None})) == "127.0.0.1"
    assert core.client_ip(Request({"type": "http", "headers": [], "client": None})) == "unknown"


# ------------------------------------------------------------ A. rate limits end to end
async def test_two_talents_behind_the_same_vercel_egress_ip_no_longer_pool(client, secret, sent_codes):
    """The incident: every request reaches Railway from the SAME egress IP (XFF = 54.0.0.1).
    With the trusted header each talent is keyed by their own real IP."""
    egress = {"x-forwarded-for": "54.0.0.1"}
    a, b = _email("poolA"), _email("poolB")
    ip_a, ip_b = _fresh_ip(), _fresh_ip()
    for _ in range(5):                                           # A uses its whole hourly allowance
        assert (await _send(client, a, {**egress, **_tg(ip_a)})).status_code == 200
    r = await _send(client, b, {**egress, **_tg(ip_b)})
    assert r.status_code == 200, "B was rejected merely because A used 5 sends"
    # ...and A's own limit is intact: the 6th is rejected (even from a brand-new IP -> the EMAIL leg)
    r6 = await _send(client, a, {**egress, **_tg(_fresh_ip())})
    assert r6.status_code == 429 and "Too many requests" in r6.json()["detail"]
    assert "Retry-After" in r6.headers


async def test_without_the_secret_the_old_pooling_is_still_what_you_get(client, secret, sent_codes):
    """Fallback path = previous behaviour (documented, not a feature): no valid secret ->
    the egress IP is the key, so the same-IP pool still fills."""
    egress = {"x-forwarded-for": _fresh_ip()}
    for i in range(5):
        assert (await _send(client, _email(f"fb{i}"), egress)).status_code == 200
    r = await _send(client, _email("fb_next"), egress)
    assert r.status_code == 429


async def test_a_forged_trusted_header_without_the_secret_does_not_dodge_or_frame_anyone(client, secret, sent_codes):
    egress_ip = _fresh_ip()
    victim_ip = _fresh_ip()
    for i in range(5):                                           # attacker fills ITS OWN (egress) bucket
        assert (await _send(client, _email(f"atk{i}"), {"x-forwarded-for": egress_ip, "x-tg-client-ip": victim_ip})).status_code == 200
    # forged header neither bought the attacker a fresh bucket...
    assert (await _send(client, _email("atk_more"), {"x-forwarded-for": egress_ip, "x-tg-client-ip": _fresh_ip()})).status_code == 429
    # ...nor charged the claimed victim IP: a request with a real secret for that IP is untouched.
    assert (await _send(client, _email("victim"), {"x-forwarded-for": "54.0.0.2", **_tg(victim_ip)})).status_code == 200


async def test_same_real_ip_still_shares_the_ip_leg_thresholds_unchanged(client, secret, sent_codes):
    """Thresholds were NOT touched: genuinely one client IP still gets 5 sends/hour in total."""
    ip = _fresh_ip()
    for label in ("x1", "x1", "x2", "x2", "x3"):                 # 2 + 2 + 1 across three emails on one IP
        assert (await _send(client, _email(f"same_{label}"), _tg(ip))).status_code == 200
    r = await _send(client, _email("same_x4"), _tg(ip))
    assert r.status_code == 429


async def test_otp_verify_brute_force_protection_is_intact(client, secret, sent_codes):
    email = _email("brute")
    await _send(client, email, _tg(_fresh_ip()))
    codes = []
    for _ in range(10):                                          # 10 verify attempts/hour per email
        r = await client.post("/api/auth/otp/verify", json={"email": email, "otp": "000000", "slug": "portal"}, headers=_tg(_fresh_ip()))
        codes.append(r.status_code)
    assert all(c == 400 for c in codes), codes                   # wrong code -> 400 (and the code locks after 5)
    r = await client.post("/api/auth/otp/verify", json={"email": email, "otp": "000000", "slug": "portal"}, headers=_tg(_fresh_ip()))
    assert r.status_code == 429


# =========================================================================== B. ownership
def _bearer(email):
    return f"Bearer {core.mint_portal_token(email)}"


async def test_primary_email_with_primary_token_passes():
    e = _email("p1"); await _talent("p1", e, alternates=[_email("p1alt")])
    assert await core.verify_email_ownership(_bearer(e), e) is True


async def test_alternate_email_with_token_of_the_same_canonical_talent_passes():
    e, alt = _email("p2"), _email("p2alt"); await _talent("p2", e, alternates=[alt])
    assert await core.verify_email_ownership(_bearer(e), alt) is True       # token(primary) -> request(alternate)
    assert await core.verify_email_ownership(_bearer(alt), e) is True       # and the reverse direction


async def test_alternate_email_with_another_talents_token_fails():
    e1, a1 = _email("p3a"), _email("p3a_alt"); await _talent("p3a", e1, alternates=[a1])
    e2, a2 = _email("p3b"), _email("p3b_alt"); await _talent("p3b", e2, alternates=[a2])
    assert await core.verify_email_ownership(_bearer(e2), a1) is False
    assert await core.verify_email_ownership(_bearer(a2), a1) is False
    assert await core.verify_email_ownership(_bearer(e2), e1) is False


async def test_stale_or_invalid_token_fails():
    e, alt = _email("p4"), _email("p4alt"); await _talent("p4", e, alternates=[alt])
    expired = jwt.encode({"role": "portal", "email": e, "exp": datetime.now(timezone.utc) - timedelta(days=1)}, core.JWT_SECRET, algorithm="HS256")
    forged = jwt.encode({"role": "portal", "email": e}, "some-other-secret", algorithm="HS256")
    wrong_role = jwt.encode({"role": "submitter", "email": e}, core.JWT_SECRET, algorithm="HS256")
    for tok in (expired, forged, wrong_role, "garbage"):
        assert await core.verify_email_ownership(f"Bearer {tok}", alt) is False, tok[:12]


async def test_no_token_or_anonymous_fails():
    e, alt = _email("p5"), _email("p5alt"); await _talent("p5", e, alternates=[alt])
    assert await core.verify_email_ownership(None, alt) is False
    assert await core.verify_email_ownership("", alt) is False
    assert await core.verify_email_ownership("Bearer ", alt) is False
    assert await core.verify_email_ownership(_bearer(e), "") is False


async def test_unknown_or_unrelated_email_with_a_valid_token_fails():
    e = _email("p6"); await _talent("p6", e, alternates=[_email("p6alt")])
    assert await core.verify_email_ownership(_bearer(e), _email("nobody")) is False
    assert await core.verify_email_ownership(_bearer(_email("nobody")), e) is False     # token for an email with no talent
    # similarity is never identity: same domain / same local part don't count
    assert await core.verify_email_ownership(_bearer(e), f"x.{e}") is False
    assert await core.verify_email_ownership(_bearer(e), e.replace("@example.com", "@example.org")) is False


async def test_merged_absorbed_identities_resolve_to_the_canonical_talent_only():
    canonical_email, old_email = _email("p7"), _email("p7old")
    canon = await _talent("p7", canonical_email, alternates=[old_email])
    # the absorbed record keeps its old email in source.talent_email (the real merge doesn't clear it)
    await _talent("p7abs", None, status="MERGED", source={"talent_email": old_email}, alternate_emails=[])
    assert await core.verify_email_ownership(_bearer(canonical_email), old_email) is True
    assert await core.verify_email_ownership(_bearer(old_email), canonical_email) is True
    # an email that exists ONLY on an absorbed record (not carried to the canonical talent) proves nothing
    orphan = _email("p7orphan")
    await _talent("p7abs2", None, status="MERGED", source={"talent_email": orphan})
    assert await core.verify_email_ownership(_bearer(canonical_email), orphan) is False
    assert canon["id"]


async def test_different_canonical_talent_ids_can_never_pass():
    shared_local = _email("p8shared")
    await _talent("p8a", _email("p8a"), alternates=[shared_local])
    t_b = await _talent("p8b", _email("p8b"))
    assert await core.verify_email_ownership(_bearer(t_b["email"]), shared_local) is False


async def test_trusted_device_cookie_accepts_the_alternate_email_of_its_own_talent_only():
    e, alt = _email("p9"), _email("p9alt"); t = await _talent("p9", e, alternates=[alt])
    other = await _talent("p9other", _email("p9other"))
    cookie_mine = await core.mint_trusted_device(t["id"])
    cookie_other = await core.mint_trusted_device(other["id"])
    assert await core.verify_email_ownership(None, alt, _req({}, cookies={core.TRUSTED_DEVICE_COOKIE: cookie_mine})) is True
    assert await core.verify_email_ownership(None, e, _req({}, cookies={core.TRUSTED_DEVICE_COOKIE: cookie_mine})) is True
    assert await core.verify_email_ownership(None, alt, _req({}, cookies={core.TRUSTED_DEVICE_COOKIE: cookie_other})) is False
    assert await core.verify_email_ownership(None, alt, _req({}, cookies={core.TRUSTED_DEVICE_COOKIE: "bogus"})) is False


# =========================================================================== C. the whole loop
async def test_alternate_email_full_loop_has_no_403_no_resend_no_rate_limit_exhaustion(client, secret, sent_codes):
    primary, alt = _email("loop"), _email("loopalt")
    t = await _talent("loop", primary, alternates=[alt])
    project_id, slug = f"{_TAG}{_RUN}_proj", f"{_TAG.lower()}{_RUN}-proj"
    await _real_db.projects.insert_one({"id": project_id, "slug": slug, "brand_name": "T", "title": "T", "status": "active", "created_at": "2026-08-01T00:00:00+00:00"})
    ip = _fresh_ip()
    h = {"x-forwarded-for": "54.0.0.3", **_tg(ip)}

    sent = await _send(client, alt, h)                                     # 1. OTP send (alternate email)
    assert sent.status_code == 200
    v = await client.post("/api/auth/otp/verify", json={"email": alt, "otp": sent_codes[alt], "slug": slug}, headers=h)
    assert v.status_code == 200 and v.json()["existing"] is True           # 2. OTP verify -> recognised
    token = v.json()["portal_token"]
    assert token                                                            # 3. portal token

    start = await client.post(f"/api/public/projects/{slug}/submission", json={"email": alt, "name": "Loop"},
                              headers={**h, "Authorization": f"Bearer {token}"})
    assert start.status_code == 200, start.text                            # 4-6. start submission, ownership OK, proceeds
    assert start.json().get("id") or start.json().get("submission_id") or start.json()

    # nothing re-triggered an OTP and the bucket is nowhere near exhausted
    assert await _real_db.otp_audit_logs.count_documents({"email": alt, "action": {"$in": ["sent", "resent"]}}) == 1
    assert await _real_db.rate_limits.count_documents({"email": alt, "endpoint": "otp_send"}) == 1
    assert await _real_db.rate_limits.count_documents({"email": alt, "endpoint": "otp_verify"}) == 1

    # the gate itself is still shut for everyone else on this same project/email
    anon = await client.post(f"/api/public/projects/{slug}/submission", json={"email": alt, "name": "Loop"}, headers=h)
    assert anon.status_code == 403
    other_t = await _talent("loop_other", _email("loop_other"))
    stranger = await client.post(f"/api/public/projects/{slug}/submission", json={"email": alt, "name": "Loop"},
                                 headers={**h, "Authorization": f"Bearer {core.mint_portal_token(other_t['email'])}"})
    assert stranger.status_code == 403
    assert t["id"]
