import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add backend directory to sys.path to import core
backend_dir = Path(__file__).parent.parent
sys.path.append(str(backend_dir))

from motor.motor_asyncio import AsyncIOMotorClient

async def repair_pending_assets(dry_run=True):
    # Retrieve connection string from env
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        print("Error: MONGO_URL environment variable is not set.")
        sys.exit(1)
        
    db_name = os.environ.get("DB_NAME", "talentgram")
    
    # Connect to MongoDB
    client = AsyncIOMotorClient(mongo_url, tlsAllowInvalidCertificates=True)
    db = client[db_name]
    
    print(f"Connecting to DB: {db_name}")
    print(f"Dry-run Mode: {dry_run}")
    
    # Query for all pending assets
    pending_assets = []
    async for asset in db.asset_metadata.find({"upload_status": "pending"}):
        pending_assets.append(asset)
        
    print(f"Discovered {len(pending_assets)} pending assets.")
    
    cleaned_count = 0
    for idx, asset in enumerate(pending_assets, 1):
        public_id = asset.get("public_id")
        submission_id = asset.get("submission_id")
        created_at = asset.get("created_at")
        
        print(f"\n[{idx}/{len(pending_assets)}] Inspecting asset:")
        print(f"  public_id: {public_id}")
        print(f"  submission_id: {submission_id}")
        print(f"  created_at: {created_at}")
        
        # Check if referenced in submission
        is_referenced = False
        if submission_id:
            sub = await db.submissions.find_one({"id": submission_id})
            if not sub:
                sub = await db.submissions.find_one({"_id": submission_id})
            if sub:
                media_list = sub.get("media", [])
                for media_item in media_list:
                    if media_item.get("public_id") == public_id:
                        is_referenced = True
                        break
            else:
                print("  Warning: Submission not found in database.")
        else:
            print("  Warning: No submission_id associated with this asset.")
            
        if is_referenced:
            print("  Status: Active (referenced in submission.media). Skipping.")
        else:
            print("  Status: Orphan (NOT referenced in submission.media). Needs cleanup.")
            if not dry_run:
                # Update status to failed
                await db.asset_metadata.update_one(
                    {"public_id": public_id},
                    {
                        "$set": {
                            "upload_status": "failed",
                            "error_reason": "Orphaned upload cleanup script",
                            "updated_at": datetime.now(timezone.utc)
                        }
                    }
                )
                print("  -> Marked as failed in database.")
                cleaned_count += 1
            else:
                print("  -> Dry-run: Would mark as failed.")
                cleaned_count += 1
                
    print(f"\nSummary:")
    print(f"Total pending assets inspected: {len(pending_assets)}")
    print(f"Total orphan pending assets {'cleaned' if not dry_run else 'detected for cleanup'}: {cleaned_count}")
    client.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair pending assets that are not referenced in submissions.")
    parser.add_argument("--execute", action="store_true", help="Run in write mode to apply changes (default is dry-run)")
    args = parser.parse_args()
    
    asyncio.run(repair_pending_assets(dry_run=not args.execute))
