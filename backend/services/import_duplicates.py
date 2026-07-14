from typing import Dict, Any, List, Optional
from core import db

async def check_duplicates(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Scan database to identify duplicates by email, phone, instagram, or name+phone."""
    email = row.get("email")
    phone = row.get("phone")
    insta = row.get("instagram_handle")
    name = row.get("name")
    
    query_or = []
    if email:
        query_or.append({"email": email})
        query_or.append({"normalized_email": email})
    if phone:
        query_or.append({"phone": phone})
    if insta:
        query_or.append({"instagram_handle": insta})
    if name and phone:
        query_or.append({"name": name, "phone": phone})
        
    if not query_or:
        return None
        
    cursor = db.talents.find({"$or": query_or}, {"_id": 0, "created_by": 0})
    matches = await cursor.to_list(length=5)
    
    if not matches:
        return None
        
    # Use the first match as primary collision target
    existing = matches[0]
    
    collision_reason = "name+phone"
    if email and (existing.get("email") == email or existing.get("normalized_email") == email):
        collision_reason = "email"
    elif phone and existing.get("phone") == phone:
        collision_reason = "phone"
    elif insta and existing.get("instagram_handle") == insta:
        collision_reason = "instagram_handle"
        
    return {
        "is_duplicate": True,
        "collision_type": collision_reason,
        "existing_talent_id": existing.get("id"),
        "existing_name": existing.get("name"),
        "existing": {
            "name": existing.get("name"),
            "email": existing.get("email"),
            "phone": existing.get("phone"),
            "instagram_handle": existing.get("instagram_handle"),
            "dob": existing.get("dob"),
            "age": existing.get("age"),
            "location": existing.get("location")
        },
        "incoming": {
            "name": row.get("name"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "instagram_handle": row.get("instagram_handle"),
            "dob": row.get("dob"),
            "age": row.get("age"),
            "location": row.get("location")
        }
    }

def merge_duplicate_profile(existing: Dict[str, Any], incoming: Dict[str, Any], action: Any) -> Dict[str, Any]:
    """Applies merge strategy policies to combine existing talent and incoming sheet data."""
    merged = existing.copy()
    
    # Standard actions: overwrite, merge, merge_blanks, keep_oldest
    action_str = action if isinstance(action, str) else "merge"
    field_actions = action if isinstance(action, dict) else {}
    
    for key, inc_val in incoming.items():
        if key in ("id", "_id"):
            continue
            
        ex_val = existing.get(key)
        
        # Determine strategy for this key
        strategy = action_str
        if key in field_actions:
            strategy = field_actions[key]
            
        # Execute strategy
        if strategy == "skip" or strategy == "keep_oldest":
            continue
        elif strategy == "overwrite" or strategy == "replace_all" or strategy == "keep_newest":
            if inc_val not in (None, "", [], {}):
                merged[key] = inc_val
        elif strategy == "merge_blanks" or strategy == "update_empty":
            # Only update if existing is blank/missing
            if ex_val in (None, "", [], {}):
                if inc_val not in (None, "", [], {}):
                    merged[key] = inc_val
        elif strategy == "merge_arrays" or strategy == "merge" or strategy == "merge_lists":
            # For lists, merge and deduplicate
            if isinstance(ex_val, list) and isinstance(inc_val, list):
                if key == "media":
                    # Deduplicate media objects by public_id
                    existing_pubs = {m.get("public_id") for m in ex_val if m.get("public_id")}
                    new_media = list(ex_val)
                    for m in inc_val:
                        pub = m.get("public_id")
                        if not pub or pub not in existing_pubs:
                            new_media.append(m)
                    merged[key] = new_media
                elif key == "location":
                    # Deduplicate locations by city+country
                    existing_locs = {f"{l.get('city')}:{l.get('country')}".lower() for l in ex_val}
                    new_locs = list(ex_val)
                    for l in inc_val:
                        loc_key = f"{l.get('city')}:{l.get('country')}".lower()
                        if loc_key not in existing_locs:
                            new_locs.append(l)
                    merged[key] = new_locs
                else:
                    # Simple items list deduplication
                    merged[key] = list(set(ex_val + inc_val))
            elif inc_val not in (None, "", [], {}):
                # Fallback to overwrite if not a list
                merged[key] = inc_val
                
    return merged

