"""Roster — admin switch "Allow PDF Download" (roster.allow_pdf_download).

The setting only controls whether the PUBLIC viewer may retrieve the PDF. It is enforced server-side
on the public PDF endpoint (no bytes, no download log, no build when OFF), defaults to ON, and a
missing/null value on a legacy roster means ON. PDF generation / cache / design are untouched.
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest

from routers import roster as R
from services import roster_pdf
from test_roster import _create, _default_sel, _payload, _talent, _viewer, cdn, env  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _mk(tdb, http, cdn, allow=None, title="R"):
    """Create a one-talent roster through the real endpoint; `allow=None` sends no setting at all."""
    t = await _talent(tdb, cdn)
    body = _payload(title, [_default_sel(t)])
    if allow is not None:
        body["roster"]["allow_pdf_download"] = allow
    r = await http.post("/api/links", json=body)
    assert r.status_code == 200, r.text
    return r.json(), t


async def _stored(tdb, lid):
    return (await tdb.links.find_one({"id": lid}, {"_id": 0}))["roster"].get("allow_pdf_download", "MISSING")


def _pdf(http, slug, headers=None, token=None):
    return http.get(f"/api/public/links/{slug}/roster/pdf" + (f"?token={token}" if token else ""), headers=headers or {})


async def _view(http, slug, h):
    r = await http.get(f"/api/public/links/{slug}/roster", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ default + persistence
async def test_a_new_roster_defaults_to_allowing_pdf_download(env, cdn):
    tdb, http = env
    link, _ = await _mk(tdb, http, cdn, allow=None)
    assert await _stored(tdb, link["id"]) is True
    h = await _viewer(http, link["slug"])
    assert (await _view(http, link["slug"], h))["pdf_available"] is True
    r = await _pdf(http, link["slug"], h)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


async def test_explicit_true_allows_and_explicit_false_rejects(env, cdn):
    tdb, http = env
    on, _ = await _mk(tdb, http, cdn, allow=True, title="On")
    off, _ = await _mk(tdb, http, cdn, allow=False, title="Off")
    assert await _stored(tdb, on["id"]) is True and await _stored(tdb, off["id"]) is False
    h_on, h_off = await _viewer(http, on["slug"]), await _viewer(http, off["slug"])
    assert (await _pdf(http, on["slug"], h_on)).status_code == 200
    r = await _pdf(http, off["slug"], h_off)
    assert r.status_code == 403 and r.content[:5] != b"%PDF-"
    assert "PDF" in r.json()["detail"]


async def test_an_off_roster_cannot_be_bypassed_by_any_way_of_asking(env, cdn):
    tdb, http = env
    off, _ = await _mk(tdb, http, cdn, allow=False)
    s = off["slug"]
    token = (await http.post(f"/api/public/links/{s}/identify", json={"name": "CD", "email": "cd@agency.com"})).json()["token"]
    for r in (
        await _pdf(http, s, {"Authorization": f"Bearer {token}"}),     # bearer header
        await _pdf(http, s, token=token),                              # ?token= (what the Download button uses)
        await _pdf(http, s, {"Authorization": f"Bearer {token}"}, token=token),
    ):
        assert r.status_code == 403 and not r.content.startswith(b"%PDF")
        assert r.headers["content-type"].startswith("application/json")
    assert (await _pdf(http, s)).status_code == 401                    # still behind the access gate
    other, _ = await _mk(tdb, http, cdn, allow=True, title="Other")
    other_token = (await http.post(f"/api/public/links/{other['slug']}/identify", json={"name": "X", "email": "x@a.com"})).json()["token"]
    assert (await _pdf(http, s, token=other_token)).status_code == 401  # another roster's token is no key
    assert (await _pdf(http, "no-such-slug", token=token)).status_code in (401, 404)
    # the gate also runs before the permission: a private link is still 403 for a different reason, never a PDF
    await tdb.links.update_one({"id": off["id"]}, {"$set": {"is_public": False}})
    assert (await _pdf(http, s, token=token)).status_code == 403


async def test_a_rejected_request_does_not_log_a_download_or_build_a_pdf(env, cdn, monkeypatch):
    tdb, http = env
    off, _ = await _mk(tdb, http, cdn, allow=False)
    h = await _viewer(http, off["slug"])
    async def boom(*a, **k): raise AssertionError("PDF must not be built/read for an OFF roster")
    monkeypatch.setattr(roster_pdf, "get_or_build_pdf", boom)
    assert (await _pdf(http, off["slug"], h)).status_code == 403
    assert await tdb.link_downloads.count_documents({"link_id": off["id"]}) == 0


async def test_the_public_payload_hides_the_button_but_is_otherwise_identical(env, cdn):
    tdb, http = env
    on, _ = await _mk(tdb, http, cdn, allow=True, title="Same")
    h = await _viewer(http, on["slug"])
    before = await _view(http, on["slug"], h)
    assert before["pdf_available"] is True
    up = _payload("Same", [{"talent_id": on["talent_ids"][0], "media_ids": [m for m in (await tdb.links.find_one({"id": on["id"]}))["roster"]["talents"][0]["media_ids"]]}])
    up["roster"]["allow_pdf_download"] = False
    assert (await http.put(f"/api/links/{on['id']}", json=up)).status_code == 200
    after = await _view(http, on["slug"], h)
    assert after["pdf_available"] is False
    assert {k: v for k, v in after.items() if k != "pdf_available"} == {k: v for k, v in before.items() if k != "pdf_available"}
    assert "allow_pdf_download" not in after                           # the setting itself is not exposed to clients


# ------------------------------------------------------------------ legacy rosters
async def test_a_legacy_roster_without_the_field_still_allows_pdf(env, cdn):
    tdb, http = env
    link, _ = await _mk(tdb, http, cdn, allow=None)
    await tdb.links.update_one({"id": link["id"]}, {"$unset": {"roster.allow_pdf_download": ""}})
    assert await _stored(tdb, link["id"]) == "MISSING"
    h = await _viewer(http, link["slug"])
    assert (await _view(http, link["slug"], h))["pdf_available"] is True
    r = await _pdf(http, link["slug"], h)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    await tdb.links.update_one({"id": link["id"]}, {"$set": {"roster.allow_pdf_download": None}})   # null is ON as well
    assert (await _pdf(http, link["slug"], h)).status_code == 200


async def test_only_an_explicit_false_turns_it_off_and_non_boolean_input_is_ignored(env, cdn):
    assert R.pdf_download_allowed(None) is True
    assert R.pdf_download_allowed({}) is True
    assert R.pdf_download_allowed({"roster": {}}) is True
    assert R.pdf_download_allowed({"roster": {"allow_pdf_download": None}}) is True
    assert R.pdf_download_allowed({"roster": {"allow_pdf_download": True}}) is True
    assert R.pdf_download_allowed({"roster": {"allow_pdf_download": False}}) is False
    tdb, http = env
    link, t = await _mk(tdb, http, cdn, allow=False)
    for junk in ("false", 0, "off", None, [], {}):                      # a sloppy client can't flip it via a non-bool
        body = _payload("R", [_default_sel(t)]); body["roster"]["allow_pdf_download"] = junk
        assert (await http.put(f"/api/links/{link['id']}", json=body)).status_code == 200
        assert await _stored(tdb, link["id"]) is False


# ------------------------------------------------------------------ editing
async def test_editing_on_to_off_and_off_to_on_persists(env, cdn):
    tdb, http = env
    link, t = await _mk(tdb, http, cdn, allow=True)
    h = await _viewer(http, link["slug"])
    def edit(allow):
        body = _payload("R", [_default_sel(t)]); body["roster"]["allow_pdf_download"] = allow
        return http.put(f"/api/links/{link['id']}", json=body)
    assert (await edit(False)).status_code == 200
    assert await _stored(tdb, link["id"]) is False
    assert (await http.get(f"/api/links/{link['id']}")).json()["roster"]["allow_pdf_download"] is False   # what the builder reloads
    assert (await _pdf(http, link["slug"], h)).status_code == 403
    assert (await edit(True)).status_code == 200
    assert await _stored(tdb, link["id"]) is True
    assert (await http.get(f"/api/links/{link['id']}")).json()["roster"]["allow_pdf_download"] is True
    r = await _pdf(http, link["slug"], h)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert (await edit(False)).status_code == 200 and (await _pdf(http, link["slug"], h)).status_code == 403   # and back again


async def test_a_save_that_omits_the_setting_keeps_the_stored_value_and_a_settings_only_update_never_touches_it(env, cdn):
    tdb, http = env
    link, t = await _mk(tdb, http, cdn, allow=False)
    assert (await http.put(f"/api/links/{link['id']}", json=_payload("R2", [_default_sel(t)]))).status_code == 200   # no key sent
    assert await _stored(tdb, link["id"]) is False
    toggle = {"title": "R2", "brand_name": None, "talent_ids": [t["id"]], "submission_ids": [], "visibility": {}, "talent_field_visibility": {},
              "auto_pull": False, "auto_project_id": None, "is_public": True, "password": None, "notes": None, "client_budget_override": None}
    assert (await http.put(f"/api/links/{link['id']}", json=toggle)).status_code == 200         # the Generated Links toggle shape
    assert await _stored(tdb, link["id"]) is False
    # a legacy roster edited without the key becomes an explicit ON (no destructive migration, no behaviour change)
    legacy, t2 = await _mk(tdb, http, cdn, allow=None, title="Legacy")
    await tdb.links.update_one({"id": legacy["id"]}, {"$unset": {"roster.allow_pdf_download": ""}})
    assert (await http.put(f"/api/links/{legacy['id']}", json=_payload("Legacy", [_default_sel(t2)]))).status_code == 200
    assert await _stored(tdb, legacy["id"]) is True


# ------------------------------------------------------------------ PDF itself untouched
async def test_when_allowed_the_response_is_the_same_pdf_as_before_and_the_cache_key_ignores_the_setting(env, cdn):
    tdb, http = env
    link, t = await _mk(tdb, http, cdn, allow=True)
    h = await _viewer(http, link["slug"])
    pub = await _pdf(http, link["slug"], h)
    admin = await http.get(f"/api/links/{link['id']}/roster/pdf")
    assert pub.status_code == 200 and pub.headers["content-type"] == "application/pdf"
    assert pub.content == admin.content                                  # same cached file, byte for byte
    assert "no-store" in pub.headers["cache-control"] and pub.headers["x-content-type-options"] == "nosniff"
    key_on = roster_pdf.content_key(await R.resolve_roster(await tdb.links.find_one({"id": link["id"]})))
    await tdb.links.update_one({"id": link["id"]}, {"$set": {"roster.allow_pdf_download": False}})
    key_off = roster_pdf.content_key(await R.resolve_roster(await tdb.links.find_one({"id": link["id"]})))
    assert key_on == key_off                                             # toggling never invalidates / rebuilds the PDF


async def test_admins_can_still_download_an_off_roster_pdf(env, cdn):
    tdb, http = env
    off, _ = await _mk(tdb, http, cdn, allow=False)
    r = await http.get(f"/api/links/{off['id']}/roster/pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


async def test_the_shortlist_and_the_classic_endpoints_are_unaffected(env, cdn):
    tdb, http = env
    off, t = await _mk(tdb, http, cdn, allow=False)
    h = await _viewer(http, off["slug"])
    r = await http.post(f"/api/public/links/{off['slug']}/roster/shortlist", json={"key": "t1", "shortlisted": True}, headers=h)
    assert r.status_code == 200 and (await _view(http, off["slug"], h))["shortlisted"] == ["t1"]
    assert (await http.post(f"/api/public/links/{off['slug']}/action", json={"talent_id": t["id"], "action": "shortlist"}, headers=h)).status_code == 404
