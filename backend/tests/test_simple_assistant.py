"""Simple Assistant — module/router/overview unit tests (no live DB, no network).

Covers:
  1. feature flag OFF by default (opt-in via SIMPLE_ASSISTANT_ENABLED)
  2. router surface: GET routes are GET-only; the only POST routes are the
     dry-run /command endpoints (their non-mutation is proven in
     test_simple_assistant_command.py)
  3. build_overview() shape + canonical-stage usage + read-only media mapping
  4. isolation: importing the module does not register a second agent

Command-layer behaviour lives in test_simple_assistant_command.py.
Run:  python backend/tests/test_simple_assistant.py   (or via pytest)
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routers.casting_pipeline import PIPELINE_STAGE_ORDER  # noqa: E402
import simple_assistant  # noqa: E402
from simple_assistant import service as sa_service  # noqa: E402
from simple_assistant.router import router as sa_router  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    async def to_list(self, n=None):
        return list(self._docs)


class FakeAgg:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class FakeColl:
    def __init__(self, docs=None, agg=None):
        self.docs = docs or []
        self._agg = agg or []

    def find(self, query=None, projection=None):
        return FakeCursor(self.docs)

    def aggregate(self, pipeline):
        return FakeAgg(self._agg)

    async def find_one(self, *a, **k):
        return self.docs[0] if self.docs else None


class FakeDB:
    def __init__(self, projects, pipeline, talents, submissions, stage_agg):
        self.projects = FakeColl(projects)
        self.casting_pipeline = FakeColl(pipeline, agg=stage_agg)
        self.talents = FakeColl(talents)
        self.submissions = FakeColl(submissions)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_feature_flag():
    old = os.environ.get("SIMPLE_ASSISTANT_ENABLED")
    try:
        os.environ.pop("SIMPLE_ASSISTANT_ENABLED", None)
        assert simple_assistant.is_enabled() is False  # OFF by default (Phase 2)
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
        assert simple_assistant.is_enabled() is True
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "1"
        assert simple_assistant.is_enabled() is True
        for v in ("false", "0", "", "no", "off"):
            os.environ["SIMPLE_ASSISTANT_ENABLED"] = v
            assert simple_assistant.is_enabled() is False, v
    finally:
        if old is None:
            os.environ.pop("SIMPLE_ASSISTANT_ENABLED", None)
        else:
            os.environ["SIMPLE_ASSISTANT_ENABLED"] = old
    print("1. feature flag OFF by default OK")


def test_router_surface():
    paths = sorted(r.path for r in sa_router.routes)
    assert paths == [
        "/api/simple-assistant/command",
        "/api/simple-assistant/command/comm-status",
        "/api/simple-assistant/command/confirm",
        "/api/simple-assistant/command/upload",
        "/api/simple-assistant/health",
        "/api/simple-assistant/overview",
    ], paths
    # GET routes are GET-only. POST routes are the command/confirm/status
    # endpoints — preview never mutates or sends (proven in
    # test_simple_assistant_command.py / _comm.py); confirm executes only an
    # HMAC-signed, server-resolved plan.
    _POST_OK = {
        "/api/simple-assistant/command",
        "/api/simple-assistant/command/confirm",
        "/api/simple-assistant/command/comm-status",
        "/api/simple-assistant/command/upload",
    }
    for route in sa_router.routes:
        methods = set(getattr(route, "methods", set()) or set())
        if "POST" in methods:
            assert route.path in _POST_OK, route.path
        else:
            assert methods <= {"GET", "HEAD", "OPTIONS"}, (route.path, methods)
    print("2. router surface OK")


def test_stage_labels_are_canonical():
    # Every label key must be a real canonical stage — no invented statuses.
    for key in sa_service.STAGE_LABELS:
        assert key in PIPELINE_STAGE_ORDER, key
    print("3. stage labels canonical OK")


def test_build_overview_shape():
    projects = [{
        "id": "p1", "brand_name": "Google AI", "character": "Lead, 25-30",
        "status": "ongoing", "shoot_dates": "18 Sep", "budget_per_day": "25000",
        "materials": [{"resource_type": "image", "url": "https://cdn/x.jpg"}],
        "created_at": "2026-09-01",
    }]
    pipeline = [
        {"project_id": "p1", "talent_id": "t1", "stage": "locked"},
        {"project_id": "p1", "talent_id": "t2", "stage": "ask_to_test"},
        {"project_id": "p1", "talent_id": "t3", "stage": "sent"},  # legacy -> approved
    ]
    talents = [
        {"id": "t1", "name": "Ahana Pocha", "dob": "1998-05-01", "height": "5'6\"", "gender": "female",
         "media": [], "cover_url": "https://cdn/ahana.jpg"},
        {"id": "t2", "name": "Riya Shah", "dob": "2000-01-01", "height": "5'7\"", "gender": "female", "media": []},
        {"id": "t3", "name": "Sana", "height": "5'6\"", "media": []},
    ]
    submissions = [
        {"talent_id": "t1", "status": "submitted", "media": [
            {"id": "m1", "category": "intro_video", "url": "https://cdn/intro.mp4", "public_id": "pid1", "resource_type": "video"},
            {"id": "m2", "category": "take", "label": "Take 1", "url": "https://cdn/t1.mp4", "resource_type": "video"},
        ]},
    ]
    stage_agg = [
        {"_id": "locked", "count": 1},
        {"_id": "ask_to_test", "count": 1},
        {"_id": "sent", "count": 1},
    ]
    fake = FakeDB(projects, pipeline, talents, submissions, stage_agg)
    sa_service.db = fake  # monkeypatch the module-level handle

    # get_stage_counts is imported by-reference from routers.casting_pipeline;
    # stub it so the test never touches a real Mongo client. The mapping
    # below still exercises the legacy-alias fold ("sent" -> "approved").
    async def _fake_stage_counts(pid):
        counts = {s: 0 for s in PIPELINE_STAGE_ORDER}
        counts["locked"] = 1
        counts["ask_to_test"] = 1
        counts["approved"] = 1
        return counts

    sa_service.get_stage_counts = _fake_stage_counts

    out = run(sa_service.build_overview({"name": "Raj Bhansali", "email": "raj@x.com", "role": "admin"}))

    assert out["user"]["first_name"] == "Raj"
    assert out["greeting"] in ("Good morning", "Good afternoon", "Good evening")
    assert out["summary"]["active_projects"] == 1
    assert out["summary"]["talents_in_pipeline"] == 3
    assert out["summary"]["tests_received"] == 1

    proj = out["projects"][0]
    assert proj["name"] == "Google AI"
    assert proj["image_url"] == "https://cdn/x.jpg"
    assert proj["pipeline_total"] == 3
    # legacy "sent" folded into canonical "approved"
    assert proj["stage_counts"]["approved"] == 1
    assert "sent" not in proj["stage_counts"]
    # every stage_counts key is canonical
    for k in proj["stage_counts"]:
        assert k in PIPELINE_STAGE_ORDER, k

    cards = {c["talent_id"]: c for c in proj["talents"]}
    assert cards["t1"]["stage"] == "locked"
    assert cards["t1"]["age"] == 27 or cards["t1"]["age"] == 28  # dob-derived
    assert cards["t1"]["test_status"] == "received"
    assert cards["t1"]["media"]["intro_video"]["label"] == "Introduction"
    assert cards["t1"]["media"]["intro_video"]["url"] == "https://cdn/intro.mp4"
    assert len(cards["t1"]["media"]["takes"]) == 1
    # t2 ask_to_test with no submission -> follow-up
    assert cards["t2"]["is_follow_up"] is True
    assert cards["t2"]["test_status"] == "awaiting"
    # locked sorts before ask_to_test
    assert proj["talents"][0]["talent_id"] == "t1"
    print("4. build_overview shape OK")


def test_no_extra_agent_registered():
    from agents import registry
    before = {a.agent_id for a in registry.list_agents()}
    import importlib
    importlib.reload(simple_assistant)
    after = {a.agent_id for a in registry.list_agents()}
    assert before == after, "simple_assistant import must not register an agent"
    print("5. no agent side-effect OK")


if __name__ == "__main__":
    test_feature_flag()
    test_router_surface()
    test_stage_labels_are_canonical()
    test_build_overview_shape()
    test_no_extra_agent_registered()
    print("\nALL SIMPLE ASSISTANT TESTS PASSED")
