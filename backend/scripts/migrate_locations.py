"""Normalize location strings to a list of structured objects and flag ambiguous ones.

Run from /app:
    cd /app/backend && python -m scripts.migrate_locations           # LIVE
    cd /app/backend && python -m scripts.migrate_locations --dry-run # preview only
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
sys.path.insert(0, str(ROOT_DIR))
from core import db  # noqa: E402

DRY_RUN = "--dry-run" in sys.argv


def is_ambiguous(raw_loc: str) -> bool:
    low = raw_loc.lower().strip()
    # Flag separators
    if any(sep in low for sep in ("/", "-", "&", " and ")):
        return True
    # Flag states instead of cities
    states = {"punjab", "maharashtra", "karnataka", "tamil nadu", "telangana", "west bengal", "gujarat", "rajasthan", "uttar pradesh"}
    if any(state in low for state in states):
        return True
    return False


def parse_raw_location(raw_loc: any) -> tuple[list[dict], bool]:
    if not raw_loc:
        return [], False

    if isinstance(raw_loc, list):
        parsed = []
        needs_review = False
        for item in raw_loc:
            if isinstance(item, dict):
                city = item.get("city", "").strip()
                country = item.get("country", "").strip()
                if city and country:
                    parsed.append({"city": city.title(), "country": country.title() if country.lower() != "uae" else "UAE"})
            elif isinstance(item, str):
                p, r = parse_raw_location(item)
                parsed.extend(p)
                if r:
                    needs_review = True
        return parsed, needs_review

    if not isinstance(raw_loc, str):
        raw_loc = str(raw_loc)

    raw_loc = raw_loc.strip()
    if not raw_loc:
        return [], False

    needs_review = is_ambiguous(raw_loc)

    # Convert separators
    temp = raw_loc.replace("/", ";").replace("-", ";").replace("&", ";").replace(" and ", ";")
    parts = [p.strip() for p in temp.split(";") if p.strip()]

    parsed = []
    for part in parts:
        low_part = part.lower()
        if low_part in ("mumbai", "mumbai,india", "mumbai india"):
            parsed.append({"city": "Mumbai", "country": "India"})
        elif low_part in ("delhi", "delhi,india", "delhi india", "new delhi"):
            parsed.append({"city": "Delhi", "country": "India"})
        elif low_part in ("punjab", "punjab,india", "punjab india"):
            parsed.append({"city": "Punjab", "country": "India"})
        elif low_part in ("dubai", "dubai, uae", "dubai uae"):
            parsed.append({"city": "Dubai", "country": "UAE"})
        elif low_part in ("chennai", "chennai,india", "chennai india"):
            parsed.append({"city": "Chennai", "country": "India"})
        elif low_part in ("bangalore", "bengaluru", "bangalore,india", "bangalore india"):
            parsed.append({"city": "Bangalore", "country": "India"})
        elif low_part in ("hyderabad", "hyderabad,india", "hyderabad india"):
            parsed.append({"city": "Hyderabad", "country": "India"})
        elif low_part in ("kolkata", "kolkata,india", "kolkata india"):
            parsed.append({"city": "Kolkata", "country": "India"})
        elif low_part in ("pune", "pune,india", "pune india"):
            parsed.append({"city": "Pune", "country": "India"})
        else:
            if "," in part:
                sub_parts = [sp.strip() for sp in part.split(",")]
                city = sub_parts[0].title()
                country = sub_parts[-1].title() if sub_parts[-1].lower() != "uae" else "UAE"
                parsed.append({"city": city, "country": country})
            else:
                parsed.append({"city": part.title(), "country": "India"})

    return parsed, needs_review


async def main() -> None:
    print(f"== Location Normalization Migration {'(DRY RUN)' if DRY_RUN else '(LIVE)'} ==")
    collections = ["talents", "submissions"]
    
    for coll in collections:
        print(f"\nProcessing collection: {coll}")
        count_updated = 0
        count_flagged = 0
        count_scanned = 0
        
        async for doc in db[coll].find({"location": {"$exists": True}}):
            count_scanned += 1
            raw_loc = doc.get("location")
            parsed, needs_review = parse_raw_location(raw_loc)
            
            # Print mapping details
            if needs_review:
                print(f"  [FLAGGED] {raw_loc} -> {parsed}")
                count_flagged += 1
            else:
                if raw_loc != parsed:
                    print(f"  [AUTO] {raw_loc} -> {parsed}")
                    
            if not DRY_RUN:
                update_doc = {
                    "location": parsed,
                }
                if needs_review:
                    update_doc["needs_location_review"] = True
                else:
                    update_doc["needs_location_review"] = False
                    
                await db[coll].update_one({"id": doc["id"]}, {"$set": update_doc})
                count_updated += 1
                
        print(f"Finished {coll}: scanned={count_scanned}, updated={count_updated}, flagged={count_flagged}")


if __name__ == "__main__":
    asyncio.run(main())
