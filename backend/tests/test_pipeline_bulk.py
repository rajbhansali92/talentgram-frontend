import os
# Local/test-only defaults, matching every other test file in this suite —
# pytest must never connect to production by default. Set TEST_MONGO_URL to
# override for a deliberate, explicit run against a different database.
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import uuid
import httpx
from server import app
from core import db, _now

@pytest.mark.asyncio
async def test_bulk_pipeline_operations():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Get admin token
        r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
        assert r.status_code == 200
        token = r.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        project_id = f"test-proj-{uuid.uuid4().hex[:6]}"
        talent_1 = f"test-t1-{uuid.uuid4().hex[:6]}"
        talent_2 = f"test-t2-{uuid.uuid4().hex[:6]}"

        # Insert test project
        await db.projects.insert_one({"id": project_id, "title": "Test Bulk Project", "slug": project_id})

        # Insert test talents
        await db.talents.insert_many([
            {"id": talent_1, "name": "Talent One", "email": "t1@test.com", "tags": [], "notes": ""},
            {"id": talent_2, "name": "Talent Two", "email": "t2@test.com", "tags": [], "notes": ""}
        ])

        # Insert pipeline entries
        await db.casting_pipeline.insert_many([
            {"id": f"p1-{uuid.uuid4().hex[:6]}", "project_id": project_id, "talent_id": talent_1, "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()},
            {"id": f"p2-{uuid.uuid4().hex[:6]}", "project_id": project_id, "talent_id": talent_2, "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()}
        ])

        try:
            # 1. Test Bulk Move
            r = await client.post(
                f"/api/projects/{project_id}/pipeline/bulk-move",
                json={"talent_ids": [talent_1, talent_2], "stage": "approved"},
                headers=headers
            )
            assert r.status_code == 200
            assert r.json()["moved"] == 2

            cursor = db.casting_pipeline.find({"project_id": project_id})
            docs = await cursor.to_list(10)
            for doc in docs:
                assert doc["stage"] == "approved"

            # 2. Test Bulk Label (Add)
            r = await client.post(
                f"/api/projects/{project_id}/pipeline/bulk-label",
                json={"talent_ids": [talent_1, talent_2], "labels": ["Mumbai", "Premium"], "action": "add"},
                headers=headers
            )
            assert r.status_code == 200
            assert r.json()["updated"] >= 2

            t1_doc = await db.talents.find_one({"id": talent_1})
            tag_names = [tg["name"] for tg in t1_doc["tags"]]
            assert "Mumbai" in tag_names
            assert "Premium" in tag_names

            # 3. Test Bulk Label (Remove)
            r = await client.post(
                f"/api/projects/{project_id}/pipeline/bulk-label",
                json={"talent_ids": [talent_1], "labels": ["Premium"], "action": "remove"},
                headers=headers
            )
            assert r.status_code == 200
            t1_doc = await db.talents.find_one({"id": talent_1})
            tag_names = [tg["name"] for tg in t1_doc["tags"]]
            assert "Mumbai" in tag_names
            assert "Premium" not in tag_names

            # 4. Test Bulk Note
            r = await client.post(
                f"/api/projects/{project_id}/pipeline/bulk-note",
                json={"talent_ids": [talent_1, talent_2], "note": "Client liked their range"},
                headers=headers
            )
            assert r.status_code == 200
            assert r.json()["updated"] == 2
            t1_doc = await db.talents.find_one({"id": talent_1})
            assert "Client liked their range" in t1_doc["notes"]

            # 5. Test Bulk Export
            r = await client.post(
                f"/api/projects/{project_id}/pipeline/bulk-export",
                json={"talent_ids": [talent_1, talent_2]},
                headers=headers
            )
            assert r.status_code == 200
            talents = r.json()["talents"]
            assert len(talents) == 2

            # 6. Test Bulk Delete
            r = await client.post(
                f"/api/projects/{project_id}/pipeline/bulk-delete",
                json={"talent_ids": [talent_1, talent_2]},
                headers=headers
            )
            assert r.status_code == 200
            assert r.json()["deleted"] == 2

            count = await db.casting_pipeline.count_documents({"project_id": project_id})
            assert count == 0

        finally:
            await db.projects.delete_one({"id": project_id})
            await db.talents.delete_many({"id": {"$in": [talent_1, talent_2]}})
            await db.casting_pipeline.delete_many({"project_id": project_id})
