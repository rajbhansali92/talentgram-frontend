"""Roster web view — client shortlist.

A roster viewer can shortlist a talent through POST /public/links/{slug}/roster/shortlist.
It REUSES the existing shortlist mechanism (`link_actions` rows keyed by link + viewer email +
talent, action "shortlist", plus `link_action_history`) — the classic /action endpoint stays
locked out for rosters, and a raw talent id is never accepted from the public.
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest

# Reuse the roster suite's real-Mongo / in-process-ASGI / fake-CDN fixtures and helpers.
from test_roster import _create, _default_sel, _talent, _viewer, cdn, env  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _roster(tdb, http, cdn, n=3, **kw):
    talents = [await _talent(tdb, cdn, name=f"Person{i} Surname{i}") for i in range(n)]
    link = await _create(http, "R", [_default_sel(t) for t in talents], **kw)
    return link, talents


async def _shortlist(http, slug, key, on, headers):
    return await http.post(f"/api/public/links/{slug}/roster/shortlist", json={"key": key, "shortlisted": on}, headers=headers)


async def _get(http, slug, headers):
    r = await http.get(f"/api/public/links/{slug}/roster", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_shortlist_and_unshortlist_round_trip_through_the_roster_payload(env, cdn):
    tdb, http = env
    link, talents = await _roster(tdb, http, cdn)
    h = await _viewer(http, link["slug"])
    assert (await _get(http, link["slug"], h))["shortlisted"] == []
    r = await _shortlist(http, link["slug"], "t2", True, h)
    assert r.status_code == 200 and r.json() == {"ok": True, "key": "t2", "shortlisted": True}
    assert (await _get(http, link["slug"], h))["shortlisted"] == ["t2"]
    await _shortlist(http, link["slug"], "t1", True, h)
    assert (await _get(http, link["slug"], h))["shortlisted"] == ["t1", "t2"]
    r = await _shortlist(http, link["slug"], "t2", False, h)
    assert r.json()["shortlisted"] is False
    assert (await _get(http, link["slug"], h))["shortlisted"] == ["t1"]


async def test_it_is_the_existing_mechanism_one_link_actions_row_per_viewer_and_talent(env, cdn):
    tdb, http = env
    link, talents = await _roster(tdb, http, cdn)
    h = await _viewer(http, link["slug"], email="cd@agency.com")
    for on in (True, True, False, True):           # repeats / double taps never create a second row
        assert (await _shortlist(http, link["slug"], "t1", on, h)).status_code == 200
    rows = await tdb.link_actions.find({"link_id": link["id"]}).to_list(10)
    assert len(rows) == 1
    row = rows[0]
    assert row["talent_id"] == talents[0]["id"] and row["viewer_email"] == "cd@agency.com" and row["action"] == "shortlist"
    assert row["comments"] == [] and row["id"] and row["created_at"] and row["updated_at"]
    # same history log the classic action writes; idempotent repeats add nothing (True, False, True = 3 changes)
    hist = await tdb.link_action_history.find({"link_id": link["id"]}).to_list(10)
    assert [x["action"] for x in sorted(hist, key=lambda x: x["created_at"])] == ["shortlist", None, "shortlist"]
    # and the admin analytics aggregation sees it exactly like a classic shortlist
    agg = await tdb.link_actions.aggregate([
        {"$match": {"link_id": link["id"]}},
        {"$group": {"_id": "$talent_id", "shortlist": {"$sum": {"$cond": [{"$eq": ["$action", "shortlist"]}, 1, 0]}}}},
    ]).to_list(10)
    assert agg == [{"_id": talents[0]["id"], "shortlist": 1}]


async def test_each_viewer_only_sees_and_changes_their_own_shortlist(env, cdn):
    tdb, http = env
    link, _ = await _roster(tdb, http, cdn)
    a = await _viewer(http, link["slug"], email="a@agency.com")
    b = await _viewer(http, link["slug"], email="b@agency.com")
    await _shortlist(http, link["slug"], "t1", True, a)
    assert (await _get(http, link["slug"], b))["shortlisted"] == []
    await _shortlist(http, link["slug"], "t3", True, b)
    await _shortlist(http, link["slug"], "t1", False, b)           # b cannot remove a's
    assert (await _get(http, link["slug"], a))["shortlisted"] == ["t1"]
    assert (await _get(http, link["slug"], b))["shortlisted"] == ["t3"]


async def test_keys_are_validated_and_raw_talent_ids_are_never_accepted(env, cdn):
    tdb, http = env
    link, talents = await _roster(tdb, http, cdn)
    h = await _viewer(http, link["slug"])
    for bad in ("t0", "t4", "t99", "t01", "T1", "1", "", "t1; x", talents[0]["id"], "../t1"):
        r = await _shortlist(http, link["slug"], bad, True, h)
        assert r.status_code == 404, bad
    assert await tdb.link_actions.count_documents({}) == 0
    r = await http.post(f"/api/public/links/{link['slug']}/roster/shortlist", json={"talent_id": talents[0]["id"], "shortlisted": True}, headers=h)
    assert r.status_code == 422                                      # the public contract has no talent_id


async def test_requires_a_viewer_token_for_this_roster_and_an_active_roster_link(env, cdn):
    tdb, http = env
    link, _ = await _roster(tdb, http, cdn)
    other, _ = await _roster(tdb, http, cdn)
    s = link["slug"]
    assert (await _shortlist(http, s, "t1", True, {})).status_code == 401
    assert (await _shortlist(http, s, "t1", True, {"Authorization": "Bearer garbage"})).status_code == 401
    wrong = await _viewer(http, other["slug"])                       # a valid viewer token, but for ANOTHER link
    assert (await _shortlist(http, s, "t1", True, wrong)).status_code == 401
    h = await _viewer(http, s)
    await tdb.links.update_one({"id": link["id"]}, {"$set": {"is_public": False}})
    assert (await _shortlist(http, s, "t1", True, h)).status_code == 403
    assert await tdb.link_actions.count_documents({}) == 0


async def test_the_classic_action_endpoint_is_still_locked_out_for_rosters(env, cdn):
    tdb, http = env
    link, talents = await _roster(tdb, http, cdn)
    h = await _viewer(http, link["slug"])
    r = await http.post(f"/api/public/links/{link['slug']}/action", json={"talent_id": talents[0]["id"], "action": "shortlist"}, headers=h)
    assert r.status_code == 404
    assert await tdb.link_actions.count_documents({}) == 0


async def test_it_cannot_be_used_on_a_classic_link(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    r = await http.post("/api/links", json={
        "title": "Classic", "brand_name": None, "talent_ids": [t["id"]], "submission_ids": [], "visibility": {},
        "talent_field_visibility": {}, "auto_pull": False, "auto_project_id": None, "is_public": True, "password": None,
        "notes": None, "client_budget_override": None})
    assert r.status_code == 200, r.text
    h = await _viewer(http, r.json()["slug"])
    assert (await _shortlist(http, r.json()["slug"], "t1", True, h)).status_code == 404


async def test_keys_follow_the_same_skipping_as_the_payload_when_a_talent_is_gone(env, cdn):
    tdb, http = env
    link, talents = await _roster(tdb, http, cdn, n=3)
    h = await _viewer(http, link["slug"])
    await tdb.talents.update_one({"id": talents[0]["id"]}, {"$set": {"status": "ARCHIVED"}})   # first talent drops out
    payload = await _get(http, link["slug"], h)
    assert [t["key"] for t in payload["talents"]] == ["t1", "t2"]
    await _shortlist(http, link["slug"], "t1", True, h)                                          # = the 2nd original talent
    assert (await tdb.link_actions.find_one({"link_id": link["id"]}))["talent_id"] == talents[1]["id"]
    assert (await _get(http, link["slug"], h))["shortlisted"] == ["t1"]
    assert (await _shortlist(http, link["slug"], "t3", True, h)).status_code == 404


async def test_the_pdf_and_the_rest_of_the_payload_are_unaffected(env, cdn):
    """`shortlisted` is the only addition to the web payload; resolve_roster() (which the PDF and
    its cache key are built from) is untouched, so shortlisting never changes the PDF."""
    from routers import roster as R
    from services import roster_pdf
    tdb, http = env
    link, _ = await _roster(tdb, http, cdn)
    h = await _viewer(http, link["slug"])
    before = roster_pdf.content_key(await R.resolve_roster(await tdb.links.find_one({"id": link["id"]})))
    await _shortlist(http, link["slug"], "t1", True, h)
    after = roster_pdf.content_key(await R.resolve_roster(await tdb.links.find_one({"id": link["id"]})))
    assert before == after
    payload = await _get(http, link["slug"], h)
    assert {"title", "subtitle", "fields", "page", "talents", "pdf_available", "shortlisted"} == set(payload)
