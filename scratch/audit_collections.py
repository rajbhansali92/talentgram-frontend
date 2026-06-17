import asyncio
import os
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.append(str(backend_dir))

from core import db, mongo_client

async def audit():
    print("Connecting to database...")
    collections = await db.list_collection_names()
    print("Found collections:", collections)
    
    # Check if there is anything in profile_configs or onboarding_config
    for name in collections:
        count = await db[name].count_documents({})
        print(f"Collection: {name} (count: {count})")
        if count > 0:
            sample = await db[name].find_one()
            print(f"  Sample: {sample}")
            
    mongo_client.close()

if __name__ == "__main__":
    asyncio.run(audit())
