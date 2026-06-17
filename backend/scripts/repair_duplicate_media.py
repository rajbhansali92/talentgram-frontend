#!/usr/bin/env python3
"""
One-time production data repair script.
Detects and removes duplicate media items in db.talents.media while preserving the oldest entry.
Supports a --dry-run mode.
"""
import sys
import os
import argparse
import asyncio
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

# Setup path so we can import from core
sys.path.append(str(Path(__file__).parent.parent))

from core import MONGO_URL, DB_NAME, normalize_email

async def run_repair(dry_run=True):
    print(f"Connecting to database {DB_NAME}...")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    
    print("Fetching talents...")
    cursor = db.talents.find({})
    talents = await cursor.to_list(length=100000)
    print(f"Found {len(talents)} talents. Scanning for duplicate media...")
    
    total_removed = 0
    modified_talents = 0
    
    print("\n---------------- REPORT ----------------")
    print(f"{'Talent ID':<38} | {'Before':<6} | {'After':<5} | {'Removed':<7}")
    print("-" * 68)
    
    for t in talents:
        tid = t.get("id")
        media = t.get("media") or []
        if not media:
            continue
            
        before_count = len(media)
        seen_keys = set()
        deduped_media = []
        removed_count = 0
        
        for m in media:
            pub_id = m.get("public_id")
            url = m.get("url")
            sec_url = m.get("secure_url") or url
            asset_id = m.get("asset_id") or m.get("id")
            
            # Form fingerprints
            fingerprints = []
            if pub_id:
                fingerprints.append(f"pub_id:{pub_id}")
            if url:
                fingerprints.append(f"url:{url}")
            if sec_url:
                fingerprints.append(f"url:{sec_url}")
            if asset_id:
                fingerprints.append(f"id:{asset_id}")
                
            is_dup = False
            for fp in fingerprints:
                if fp in seen_keys:
                    is_dup = True
                    break
                    
            if is_dup:
                removed_count += 1
            else:
                deduped_media.append(m)
                for fp in fingerprints:
                    seen_keys.add(fp)
                    
        if removed_count > 0:
            modified_talents += 1
            total_removed += removed_count
            after_count = len(deduped_media)
            print(f"{tid:<38} | {before_count:<6} | {after_count:<5} | {removed_count:<7}")
            
            if not dry_run:
                await db.talents.update_one(
                    {"id": tid},
                    {"$set": {"media": deduped_media}}
                )
                
    print("-" * 68)
    print(f"Scan complete.")
    print(f"Talents modified/requiring changes: {modified_talents}")
    print(f"Total duplicated media items removed: {total_removed}")
    if dry_run:
        print("\n*** DRY RUN MODE: No modifications were made to the database. ***")
    else:
        print("\n*** LIVE MODE: Database updated successfully. ***")

def main():
    parser = argparse.ArgumentParser(description="Repair duplicate media in db.talents.media")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Perform dry run without writing changes")
    args = parser.parse_args()
    
    # Defaults to dry-run if not explicitly specified to prevent accidental writes
    dry_run = args.dry_run
    if not dry_run and len(sys.argv) == 1:
        dry_run = True # Safe default
        print("No args provided. Defaulting to --dry-run mode.")
        
    asyncio.run(run_repair(dry_run=dry_run))

if __name__ == "__main__":
    main()
