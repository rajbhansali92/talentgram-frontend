#!/usr/bin/env python3
"""
Orphan Detection Tool.
Finds orphan records (e.g. pipeline entries or submissions pointing to non-existent projects or talents).
Does NOT perform any deletions.
"""
import sys
import asyncio
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

# Setup path so we can import from core
sys.path.append(str(Path(__file__).parent.parent))

from core import MONGO_URL, DB_NAME

async def detect_orphans():
    print(f"Connecting to database {DB_NAME}...")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    
    # Pre-fetch valid project and talent IDs to check against
    project_docs = await db.projects.find({}, {"id": 1}).to_list(length=100000)
    valid_project_ids = {p["id"] for p in project_docs if "id" in p}
    
    talent_docs = await db.talents.find({}, {"id": 1}).to_list(length=100000)
    valid_talent_ids = {t["id"] for t in talent_docs if "id" in t}
    
    application_docs = await db.applications.find({}, {"id": 1}).to_list(length=100000)
    valid_app_ids = {a["id"] for a in application_docs if "id" in a}
    
    # 1. Orphan pipeline records
    # Defined as: casting_pipeline entries pointing to a project_id or talent_id that does not exist.
    pipeline_cursor = db.casting_pipeline.find({})
    pipeline_entries = await pipeline_cursor.to_list(length=100000)
    orphan_pipelines = []
    for entry in pipeline_entries:
        pid = entry.get("project_id")
        tid = entry.get("talent_id")
        if pid not in valid_project_ids or tid not in valid_talent_ids:
            orphan_pipelines.append(entry.get("id"))
            
    # 2. Orphan submissions
    # Defined as: submissions pointing to a project_id that does not exist.
    subs_cursor = db.submissions.find({})
    submissions = await subs_cursor.to_list(length=100000)
    orphan_subs = []
    for sub in submissions:
        pid = sub.get("project_id")
        if pid not in valid_project_ids:
            orphan_subs.append(sub.get("id"))
            
    # 3. Orphan applications
    # Defined as: applications pointing to a talent_id that does not exist.
    apps_cursor = db.applications.find({})
    applications = await apps_cursor.to_list(length=100000)
    orphan_apps = []
    for app in applications:
        tid = app.get("talent_id")
        if tid and tid not in valid_talent_ids:
            orphan_apps.append(app.get("id"))
            
    # 4. Orphan audits
    # Defined as: profile_audits pointing to a talent_id that does not exist.
    audits_cursor = db.profile_audits.find({})
    audits = await audits_cursor.to_list(length=100000)
    orphan_audits = []
    for audit in audits:
        tid = audit.get("talent_id")
        if tid not in valid_talent_ids:
            orphan_audits.append(str(audit.get("_id")))
            
    # 5. Orphan Cloudinary references in db.asset_metadata
    # Defined as: asset_metadata pointing to a project_id or talent_id that does not exist.
    assets_cursor = db.asset_metadata.find({})
    assets = await assets_cursor.to_list(length=100000)
    orphan_assets = []
    for asset in assets:
        pid = asset.get("project_id")
        tid = asset.get("talent_id")
        # If it's a project asset, check project_id. If a talent asset, check talent_id.
        if pid and pid not in valid_project_ids:
            orphan_assets.append(asset.get("id") or str(asset.get("_id")))
        elif tid and tid not in valid_talent_ids:
            orphan_assets.append(asset.get("id") or str(asset.get("_id")))

    print("\n================== ORPHAN DETECTION REPORT ==================")
    reports = [
        ("casting_pipeline", len(orphan_pipelines), orphan_pipelines[:10]),
        ("submissions", len(orphan_subs), orphan_subs[:10]),
        ("applications", len(orphan_apps), orphan_apps[:10]),
        ("profile_audits", len(orphan_audits), orphan_audits[:10]),
        ("asset_metadata (Cloudinary ref)", len(orphan_assets), orphan_assets[:10]),
    ]
    
    print(f"{'Collection':<35} | {'Orphan Count':<12} | {'Sample IDs'}")
    print("-" * 75)
    for col, count, samples in reports:
        samples_str = ", ".join(samples) if count > 0 else "None"
        print(f"{col:<35} | {count:<12} | {samples_str}")
    print("-" * 75)

def main():
    asyncio.run(detect_orphans())

if __name__ == "__main__":
    main()
