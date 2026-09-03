"""AI Scout — Gate 2 tests.

Run standalone (``pytest tests/test_ai_scout.py``) — module-scoped event loop,
like the other in-process-Motor DB test files here.

  * Pure logic: lenient candidate query, deterministic requirement/location/
    confidence scoring, result assembly + hallucination guard, tiering,
    Gate-1 criteria mapping.
  * Router flow (local MongoDB, LLM monkeypatched): a run ranks candidates
    without touching the pipeline; the LLM failing changes no pipeline state;
    selecting talents adds them through the EXISTING
    casting_pipeline.add_talents_to_pipeline(..., "ask_to_test"); duplicates
    are idempotent.
"""
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("MONGO_URL", os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017"))
# Gate 2 queries the whole talent pool, so this file needs an ISOLATED db —
# the shared `talentgram` test db accumulates hundreds of fixture talents from
# other suites, which would swamp the candidate pool. Set before importing core.
os.environ.setdefault("DB_NAME", "talentgram_ai_scout_test")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from ai import scout  # noqa: E402
from core import db, _now  # noqa: E402
from routers import ai_scout as scout_router  # noqa: E402

_aio = pytest.mark.asyncio(loop_scope="module")

ADMIN = {"id": "scout-test-admin", "email": "scout@talentgram.com", "role": "admin"}
PID = "scout-test-project"


def _crit(**over):
    c = dict(scout.EMPTY_CRITERIA)
    c.update({"gender": "female", "age_min": 22, "age_max": 30, "height_min": "5'6\"", "locations": ["Mumbai"]})
    c.update(over)
    return c


def _talent(tid, **f):
    base = {
        "id": tid, "name": f"T {tid}", "status": "SUBMITTED",
        "gender": "female", "dob": "1998-06-01", "height_inches": 67.0,
        "location": [{"city": "Mumbai", "country": "India"}],
        "ethnicity": "indian", "instagram_handle": "h", "instagram_followers": "100K+",
        "bio": "Premium lifestyle content creator", "interested_in": ["Acting", "Influencer Campaigns"],
        "skills": ["Actor"], "tags": [{"id": "t", "name": "Beauty"}], "work_links": ["l1"],
        "total_submissions": 4, "cover_url": "u", "cover_thumbnail_url": "u",
    }
    base.update(f)
    return base


# ===========================================================================
# Pure logic
# ===========================================================================
def test_candidate_query_gender_is_lenient_others_off_by_default():
    q = scout.build_candidate_query(_crit())
    assert q["status"] == {"$nin": ["DRAFT", "ARCHIVED", "MERGED"]}
    # only gender is filtered by default; it keeps matches AND unknowns
    assert len(q["$and"]) == 1
    gender_or = q["$and"][0]["$or"]
    assert {"gender": "female"} in gender_or
    assert {"gender": {"$exists": False}} in gender_or


def test_candidate_query_hard_filters_add_clauses_keeping_unknowns():
    q = scout.build_candidate_query(_crit(hard_filters=["age", "height", "location"]))
    dumped = str(q)
    assert "dob" in dumped and "height_inches" in dumped and "location.city" in dumped
    # a hard height filter still keeps talents with NO height on file
    assert "{'height_inches': {'$exists': False}}" in dumped


def test_requirement_fit_full_match_and_dimensions():
    score, dims = scout.requirement_fit(_talent("a"), _crit())
    assert score == 100
    assert dims["gender"] == "match" and dims["age"] == "match" and dims["height"] == "match"


def test_requirement_fit_gender_mismatch_scores_zero_for_that_dimension():
    score, dims = scout.requirement_fit(_talent("a", gender="male"), _crit())
    assert dims["gender"] == "mismatch"
    assert score < 100


def test_missing_age_is_unknown_not_rejected():
    score, dims = scout.requirement_fit(_talent("a", dob=None, age=None), _crit())
    assert dims["age"] == "unknown"          # not counted, not a 0
    assert score == 100                       # gender + height still match


def test_no_criteria_dimension_yields_none():
    score, dims = scout.requirement_fit(_talent("a"), dict(scout.EMPTY_CRITERIA))
    assert score is None
    assert set(dims.values()) == {"n/a"}


def test_location_fit_states():
    c = _crit()
    assert scout.location_fit(_talent("a"), c) == 100                                  # in-city
    assert scout.location_fit(_talent("a", location=[{"city": "Delhi", "country": "India"}]), c) == 40  # elsewhere
    assert scout.location_fit(_talent("a", location=[]), c) is None                     # unknown, not a miss
    assert scout.location_fit(_talent("a"), _crit(locations=[])) is None               # not a factor


def test_profile_confidence_is_always_a_number():
    assert isinstance(scout.profile_confidence(_talent("a")), int)
    assert scout.profile_confidence({"id": "x"}) == 0


def test_assemble_result_overall_from_present_components_only():
    res = scout.assemble_result(
        _talent("a"), _crit(),
        {"character_fit": 90, "experience_fit": 80, "confidence": 0.9, "strengths": [], "risks": [], "reason": "ok"},
    )
    assert 0 <= res["overall"] <= 100
    assert res["scores"]["requirement_fit"] == 100
    assert res["scores"]["character_fit"] == 90


def test_unknown_subscore_becomes_none_and_an_unknown_note():
    res = scout.assemble_result(
        _talent("a", bio=None, cover_url=None, cover_thumbnail_url=None),
        _crit(),
        {"character_fit": -1, "experience_fit": -1, "confidence": 0.3, "strengths": [], "risks": [], "reason": "thin"},
    )
    assert res["scores"]["character_fit"] is None
    assert any("Character fit not assessed" in u for u in res["unknowns"])
    assert any("No experience signal" in u for u in res["unknowns"])


def test_hallucination_guard_drops_unbacked_claims():
    t = _talent("a", dob=None, age=None, height_inches=None, height=None, location=[])
    res = scout.assemble_result(
        t, _crit(),
        {
            "character_fit": 70, "experience_fit": 60, "confidence": 0.5,
            "strengths": ["27 years old and Mumbai-based", "5'8\" tall", "Beauty tag on file"],
            "risks": [], "reason": "x",
        },
    )
    joined = " ".join(res["strengths"]).lower()
    assert "27 years old" not in joined
    assert "5'8" not in joined
    assert "mumbai" not in joined
    assert "beauty tag on file" in joined          # this one IS backed by the data
    assert res["field_verification"]["age"]["status"] == "unknown"
    assert res["field_verification"]["competitive_brand_history"]["status"] == "not_tracked"


def test_tier_assignment():
    strong_res = scout.assemble_result(_talent("a"), _crit(),
                                       {"character_fit": 88, "experience_fit": 80, "confidence": 0.9,
                                        "strengths": [], "risks": [], "reason": "x"})
    assert scout.tier(strong_res) == "top"

    mismatch_res = scout.assemble_result(_talent("b", gender="male"), _crit(),
                                         {"character_fit": 90, "experience_fit": 90, "confidence": 0.9,
                                          "strengths": [], "risks": [], "reason": "x"})
    assert scout.tier(mismatch_res) == "possible"

    thin_res = scout.assemble_result(_talent("c", dob=None, age=None, height_inches=None, height=None, location=[]),
                                     _crit(),
                                     {"character_fit": -1, "experience_fit": -1, "confidence": 0.2,
                                      "strengths": [], "risks": [], "reason": "x"})
    assert scout.tier(thin_res) == "possible"


def test_criteria_from_gate1_extraction():
    ext = {
        "fields": {
            "gender": {"value": "Female", "confidence": "stated"},
            "age_range": {"value": "24-30", "confidence": "stated"},
            "height": {"value": "5'5\"", "confidence": "stated"},
            "shoot_location": {"value": "Mumbai", "confidence": "stated"},
            "look": {"value": "girl next door", "confidence": "stated"},
            "character": {"value": "relatable lead", "confidence": "stated"},
        },
        "summary": "Casting a female lead.",
    }
    c = scout.criteria_from_gate1_extraction(ext)
    assert c["gender"] == "female"
    assert c["age_min"] == 24 and c["age_max"] == 30
    assert c["locations"] == ["Mumbai"]
    assert "relatable lead" in c["character_summary"]


def test_normalise_criteria_guards():
    c = scout.normalise_criteria({
        "gender": "women", "age_min": 30, "age_max": 22, "categories": ["Acting", "Nonsense"],
        "hard_filters": ["gender", "bogus"], "locations": ["Mumbai", "Pune"],
    })
    assert c["gender"] == "female"
    assert c["age_min"] == 22 and c["age_max"] == 30      # swapped
    assert c["categories"] == ["Acting"]
    assert c["hard_filters"] == ["gender"]


# ===========================================================================
# Thin-profile scoring (evidence coverage)
# ===========================================================================
def _ai(cf=85, ef=75):
    return {"character_fit": cf, "experience_fit": ef, "confidence": 0.8, "strengths": [], "risks": [], "reason": "x"}


def test_well_profiled_talent_is_not_dampened():
    res = scout.assemble_result(_talent("full"), _crit(), _ai())
    assert res["evidence_coverage"] == 1.0
    assert res["overall"] == res["base_overall"]
    assert res["evidence_count"] >= 3


def test_thin_profile_score_is_dampened_and_downgraded():
    # only gender + location are known; no bio/photo (character unknown),
    # no auditions/tags/links (experience unknown); age criterion unmet on file.
    thin = _talent(
        "thin", dob=None, age=None, bio=None, cover_url=None, cover_thumbnail_url=None,
        tags=[], interested_in=[], skills=[], work_links=[], total_submissions=0,
        instagram_followers=None,
    )
    res = scout.assemble_result(thin, _crit(), {"character_fit": -1, "experience_fit": -1,
                                                "confidence": 0.2, "strengths": [], "risks": [], "reason": "thin"})
    assert res["evidence_coverage"] < 1.0
    assert res["overall"] < res["base_overall"]
    assert scout.tier(res) == "possible"          # never Top/Strong on this little data


def test_thin_profile_never_visually_equivalent_to_a_strong_match():
    full = scout.assemble_result(_talent("full"), _crit(), _ai())
    thin = scout.assemble_result(
        _talent("thin", dob=None, age=None, bio=None, cover_url=None, cover_thumbnail_url=None,
                tags=[], interested_in=[], skills=[], work_links=[], total_submissions=0),
        _crit(), {"character_fit": -1, "experience_fit": -1, "confidence": 0.2,
                  "strengths": [], "risks": [], "reason": "x"},
    )
    # a well-profiled 90+ and a barely-known talent must not read the same
    assert full["overall"] - thin["overall"] >= 15


def test_requirement_fit_from_a_single_dimension_counts_as_half_evidence():
    # gender only on file, nothing else the criteria asks about
    t = _talent("g", dob=None, age=None, height_inches=None, height=None, location=[],
                bio=None, cover_url=None, cover_thumbnail_url=None, tags=[], interested_in=[],
                skills=[], work_links=[], total_submissions=0, instagram_followers=None, ethnicity=None)
    res = scout.assemble_result(t, _crit(), {"character_fit": -1, "experience_fit": -1,
                                             "confidence": 0.1, "strengths": [], "risks": [], "reason": "x"})
    assert res["evidence_count"] < 1.75
    assert scout.tier(res) == "possible"


# ===========================================================================
# Hard requirements — Cases A–G
# ===========================================================================
def test_case_A_male_talent_never_top_or_strong_for_a_female_project():
    # query-level: an explicit male is not even a candidate
    q = scout.build_candidate_query(_crit())
    gender_or = q["$and"][0]["$or"]
    assert {"gender": "female"} in gender_or and {"gender": "male"} not in gender_or
    # score-level: even if one reached assembly, tiering demotes it
    res = scout.assemble_result(_talent("m", gender="male"), _crit(), _ai(90, 90))
    assert scout.tier(res) == "possible"
    assert res["field_verification"]["gender"]["verdict"] == "mismatch"


def test_case_B_hard_age_filter_excludes_and_downranks_out_of_range():
    q = scout.build_candidate_query(_crit(hard_filters=["age"]))
    dumped = str(q)
    assert "dob" in dumped and "1988" in dumped or "$gte" in dumped   # a dob/age band clause exists
    # explicit age well outside band -> "mismatch" dimension, never a normal match
    old = _talent("old", dob="1975-01-01")
    score, dims = scout.requirement_fit(old, _crit())
    assert dims["age"] == "mismatch"
    res = scout.assemble_result(old, _crit(), _ai())
    assert scout.tier(res) == "possible"


def test_case_C_hard_height_filter_excludes_and_downranks_below_requirement():
    q = scout.build_candidate_query(_crit(hard_filters=["height"]))
    assert "height_inches" in str(q)
    short = _talent("short", height_inches=60.0, height="5'0\"")
    score, dims = scout.requirement_fit(short, _crit())
    assert dims["height"] == "mismatch"
    res = scout.assemble_result(short, _crit(), _ai())
    assert scout.tier(res) == "possible"


def test_case_D_missing_age_height_stay_eligible_but_flagged_unknown():
    unknown = _talent("u", dob=None, age=None, height_inches=None, height=None)
    # still a candidate even with age/height hard-filtered (incomplete != rejected)
    q = scout.build_candidate_query(_crit(hard_filters=["age", "height"]))
    assert "'$exists': False" in str(q)
    score, dims = scout.requirement_fit(unknown, _crit())
    assert dims["age"] == "unknown" and dims["height"] == "unknown"
    res = scout.assemble_result(unknown, _crit(), _ai())
    fv = res["field_verification"]
    assert fv["age"]["status"] == "unknown" and fv["height"]["status"] == "unknown"
    assert any("Age" in u for u in res["unknowns"]) and any("Height not on file" in u for u in res["unknowns"])


def test_case_E_location_city_match_and_different_city():
    assert scout.location_fit(_talent("a"), _crit()) == 100
    diff = _talent("a", location=[{"city": "Bengaluru", "country": "India"}])
    assert scout.location_fit(diff, _crit()) == 40
    res = scout.assemble_result(diff, _crit(), _ai())
    assert res["field_verification"]["location"]["verdict"] == "different_city"
    assert scout.tier(res) != "top"                    # travel dampens it out of Top
    # hard location filter excludes a different city but keeps no-location
    q = scout.build_candidate_query(_crit(hard_filters=["location"]))
    loc_or = q["$and"][-1]["$or"]
    assert {"location": {"$exists": False}} in loc_or


def test_case_F_competitive_brand_history_is_never_claimed():
    res = scout.assemble_result(_talent("a"), _crit(competitive_brands_note="No competing skincare brands"), _ai())
    assert res["field_verification"]["competitive_brand_history"] == {"value": None, "status": "not_tracked"}
    assert any("not tracked in talentgram" in u.lower() for u in res["unknowns"])


def test_case_G_availability_is_never_claimed():
    res = scout.assemble_result(_talent("a"), _crit(), _ai())
    assert res["field_verification"]["availability"] == {"value": None, "status": "not_confirmed"}
    assert any("availability" in u.lower() and "not confirmed" in u.lower() for u in res["unknowns"])


# ===========================================================================
# Router flow
# ===========================================================================
@pytest_asyncio.fixture(loop_scope="module")
async def seeded():
    await db.projects.delete_many({"id": PID})
    await db.talents.delete_many({"id": {"$regex": "^scout-test-t"}})
    await db.casting_pipeline.delete_many({"project_id": PID})
    await db[scout_router.COLLECTION].delete_many({"project_id": PID})

    await db.projects.insert_one({
        "id": PID, "brand_name": "Aura Skincare", "slug": "aura-skincare-x",
        "status": "ongoing", "character": "Young urban woman, premium lifestyle look",
        "additional_details": "Shoot in Mumbai. No competing skincare brands.",
        "medium_usage": "Digital", "competitive_brand_enabled": True,
        "submission_requirements": {"fields": {"competitive_brand": "required"}},
        "created_at": _now(), "created_by": ADMIN["id"],
    })
    talents = [
        _talent("scout-test-t1"),                                             # perfect
        _talent("scout-test-t2", location=[{"city": "Delhi", "country": "India"}]),  # different city
        _talent("scout-test-t3", gender="male"),                              # wrong gender -> filtered OUT
        _talent("scout-test-t4", dob=None, age=None, bio=None, cover_url=None, cover_thumbnail_url=None,
                interested_in=[], skills=[], tags=[], work_links=[], total_submissions=0,
                instagram_followers=None, instagram_handle=None),             # thin / unknowns
        _talent("scout-test-t5", dob="1972-01-01"),                           # well outside age band
    ]
    await db.talents.insert_many(talents)
    yield
    await db.projects.delete_many({"id": PID})
    await db.talents.delete_many({"id": {"$regex": "^scout-test-t"}})
    await db.casting_pipeline.delete_many({"project_id": PID})
    await db[scout_router.COLLECTION].delete_many({"project_id": PID})


@pytest.fixture
def mock_rank(monkeypatch):
    calls = {"n": 0, "batches": []}

    async def _fake(**kwargs):
        calls["n"] += 1
        cands = kwargs["user"]
        import json as _j
        # crude: parse the JSON candidate list out of the user message
        arr = _j.loads(cands[cands.index("["): cands.rindex("]") + 1])
        calls["batches"].append(len(arr))
        rankings = []
        for c in arr:
            has_signal = bool(c.get("bio")) or c.get("has_photo_on_file")
            rankings.append({
                "talent_id": c["talent_id"],
                "character_fit": 85 if has_signal else -1,
                "experience_fit": 75 if c.get("prior_auditions_with_agency") else -1,
                "confidence": 0.8 if has_signal else 0.2,
                "strengths": ["Beauty tag on file"] if c.get("tags_on_file") else [],
                "risks": ["Competitive brand history not verified"],
                "reason": "Reads well for a premium skincare lead.",
            })
        return {"rankings": rankings}

    monkeypatch.setattr("ai.client.call_tool_json", _fake)
    return calls


@_aio
async def test_run_ranks_without_touching_pipeline(seeded, mock_rank):
    out = await scout_router.run_scout(
        PID, scout_router.RunIn(criteria=_crit()), user=ADMIN
    )
    run = out["run"]
    assert run["status"] == "complete"
    assert run["candidate_count"] >= 3
    assert mock_rank["n"] >= 1
    ids = {r["talent_id"]: r for r in run["results"]}
    # perfect candidate is top-tier
    assert ids["scout-test-t1"]["tier"] == "top"
    # explicit wrong gender is FILTERED OUT of the candidate pool entirely
    assert "scout-test-t3" not in ids
    # a different-city female is still a candidate, just scored lower
    assert ids["scout-test-t2"]["scores"]["location_fit"] == 40
    # an out-of-age female enters the pool (age isn't a hard filter) and is demoted
    assert ids["scout-test-t5"]["field_verification"]["age"]["verdict"] == "mismatch"
    assert ids["scout-test-t5"]["tier"] == "possible"
    # thin profile lands in possible with explicit unknowns
    assert ids["scout-test-t4"]["tier"] == "possible"
    assert any("not on file" in u for u in ids["scout-test-t4"]["unknowns"])
    # every result has a reason
    assert all(r["reason"] for r in run["results"])
    # NOTHING added to the pipeline
    assert await db.casting_pipeline.count_documents({"project_id": PID}) == 0


@_aio
async def test_run_is_cached_by_criteria(seeded, mock_rank):
    c = _crit()
    await scout_router.run_scout(PID, scout_router.RunIn(criteria=c), user=ADMIN)
    n_after_first = mock_rank["n"]
    out2 = await scout_router.run_scout(PID, scout_router.RunIn(criteria=c), user=ADMIN)
    assert out2["cached"] is True
    assert mock_rank["n"] == n_after_first          # no second LLM call
    out3 = await scout_router.run_scout(PID, scout_router.RunIn(criteria=c, force=True), user=ADMIN)
    assert out3["cached"] is False
    assert mock_rank["n"] > n_after_first

    # changing a criterion busts the cache -> a fresh run + a fresh LLM call
    n_before = mock_rank["n"]
    out4 = await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit(age_min=25, age_max=35)), user=ADMIN)
    assert out4["cached"] is False
    assert mock_rank["n"] > n_before
    assert out4["run"]["criteria_hash"] != out3["run"]["criteria_hash"]

    # identical results across two cache hits (consistency)
    a = await scout_router.run_scout(PID, scout_router.RunIn(criteria=c), user=ADMIN)
    b = await scout_router.run_scout(PID, scout_router.RunIn(criteria=c), user=ADMIN)
    assert a["run"]["id"] == b["run"]["id"]
    assert [r["talent_id"] for r in a["run"]["results"]] == [r["talent_id"] for r in b["run"]["results"]]


@_aio
async def test_llm_failure_leaves_pipeline_untouched(seeded, monkeypatch):
    async def _boom(**kwargs):
        raise scout.llm.LLMError("model exploded")

    monkeypatch.setattr("ai.client.call_tool_json", _boom)
    with pytest.raises(Exception) as ei:
        await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit()), user=ADMIN)
    assert getattr(ei.value, "status_code", None) == 502
    assert await db.casting_pipeline.count_documents({"project_id": PID}) == 0
    run = await db[scout_router.COLLECTION].find_one({"project_id": PID}, sort=[("created_at", -1)])
    assert run["status"] == "error"


@_aio
async def test_select_adds_through_existing_pipeline(seeded, mock_rank):
    out = await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit()), user=ADMIN)
    run_id = out["run"]["id"]

    sel = await scout_router.select_talents(
        PID, scout_router.SelectIn(run_id=run_id, talent_ids=["scout-test-t1", "scout-test-t2"]), user=ADMIN
    )
    assert sel["added"] == 2
    assert sel["stage"] == "ask_to_test"

    rows = await db.casting_pipeline.find({"project_id": PID}).to_list(10)
    assert {r["talent_id"] for r in rows} == {"scout-test-t1", "scout-test-t2"}
    assert all(r["stage"] == "ask_to_test" for r in rows)

    run = await db[scout_router.COLLECTION].find_one({"id": run_id})
    assert set(run["selected_talent_ids"]) == {"scout-test-t1", "scout-test-t2"}
    assert len(run["selections"]) == 1


@_aio
async def test_select_is_idempotent(seeded, mock_rank):
    out = await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit()), user=ADMIN)
    run_id = out["run"]["id"]
    await scout_router.select_talents(PID, scout_router.SelectIn(run_id=run_id, talent_ids=["scout-test-t1"]), user=ADMIN)
    sel2 = await scout_router.select_talents(
        PID, scout_router.SelectIn(run_id=run_id, talent_ids=["scout-test-t1", "scout-test-t2"]), user=ADMIN
    )
    assert sel2["added"] == 1
    assert "scout-test-t1" in sel2["already_in_pipeline"]
    assert await db.casting_pipeline.count_documents({"project_id": PID}) == 2   # no duplicate t1


@_aio
async def test_select_rejects_unknown_run_and_unranked_talent(seeded, mock_rank):
    out = await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit()), user=ADMIN)
    run_id = out["run"]["id"]
    with pytest.raises(Exception) as ei:
        await scout_router.select_talents(PID, scout_router.SelectIn(run_id="nope", talent_ids=["x"]), user=ADMIN)
    assert getattr(ei.value, "status_code", None) == 404
    with pytest.raises(Exception) as ei2:
        await scout_router.select_talents(PID, scout_router.SelectIn(run_id=run_id, talent_ids=["not-in-results"]), user=ADMIN)
    assert getattr(ei2.value, "status_code", None) == 400


@_aio
async def test_run_reflects_talents_already_in_pipeline(seeded, mock_rank):
    from routers import casting_pipeline
    await casting_pipeline.add_talents_to_pipeline(PID, ["scout-test-t1"], "ask_to_test")
    out = await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit(), force=True), user=ADMIN)
    row = next(r for r in out["run"]["results"] if r["talent_id"] == "scout-test-t1")
    assert row["in_pipeline_stage"] == "ask_to_test"


@_aio
async def test_no_candidates_path(seeded, mock_rank, monkeypatch):
    # force an impossible candidate query — covers the no-match branch without
    # depending on how sparse the shared test DB happens to be.
    monkeypatch.setattr("ai.scout.build_candidate_query", lambda c: {"id": "__none__"})
    out = await scout_router.run_scout(PID, scout_router.RunIn(criteria=_crit(), force=True), user=ADMIN)
    assert out["run"]["status"] == "no_candidates"
    assert out["run"]["results"] == []
    assert mock_rank["n"] == 0
    assert await db.casting_pipeline.count_documents({"project_id": PID}) == 0
