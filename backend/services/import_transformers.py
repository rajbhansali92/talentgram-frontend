import re
from typing import Any, List, Optional, Dict

def clean_placeholder(val: Any) -> Optional[str]:
    """Clean empty, whitespace-only, and typical placeholder values to None."""
    if val is None:
        return None
    s = str(val).strip()
    # Remove invisible/zero-width unicode characters
    s = re.sub(r'[\u200b-\u200d\ufeff]', '', s)
    # Collapse multiple spaces
    s = re.sub(r'\s+', ' ', s).strip()
    if s.lower() in ("", "na", "n/a", "-", "blank", "none", "null", "undefined"):
        return None
    return s

def transform_name(val: Any) -> Optional[str]:
    """Normalize full name to Title Case."""
    s = clean_placeholder(val)
    if not s:
        return None
    return s.title()

def transform_phone(val: Any) -> Optional[str]:
    """Normalize phone number to standard format with country code (default +91)."""
    s = clean_placeholder(val)
    if not s:
        return None
    
    # Strip everything except digits and leading '+'
    is_positive = s.startswith("+")
    digits = "".join(c for c in s if c.isdigit())
    
    if not digits:
        return None
        
    # If it is a 10-digit number, prepend +91
    if len(digits) == 10:
        return f"+91{digits}"
    # If it is 12 digits and starts with 91, prepend +
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
        
    if is_positive:
        return f"+{digits}"
    return f"+{digits}"

def transform_instagram(val: Any) -> Optional[str]:
    """Normalize Instagram handle or URL to https://instagram.com/handle."""
    s = clean_placeholder(val)
    if not s:
        return None
        
    # Remove leading '@', 'www.', 'http://', 'https://', 'instagram.com/', 'instagr.am/'
    s = s.replace("http://", "").replace("https://", "")
    s = s.replace("www.", "")
    s = s.replace("instagram.com/", "").replace("instagr.am/", "")
    s = s.lstrip("@").strip("/")
    
    if not s:
        return None
    return f"https://instagram.com/{s}"

def transform_height(val: Any) -> Optional[str]:
    """Normalize height formats (5'5\", 5 ft 5, 165 cm, 165) to standard 5'5\"."""
    s = clean_placeholder(val)
    if not s:
        return None
        
    # Check if it already matches feet/inches format e.g. 5'5" or 5'11"
    if re.match(r"^\d+'\d+\"?$", s):
        if not s.endswith('"'):
            s = s + '"'
        return s
        
    # Try parsing feet and inches
    ft_in_match = re.search(r"(\d+)\s*(?:ft|feet|'|ft\.)\s*(\d+)?\s*(?:in|inches|\"|in\.)?", s, re.IGNORECASE)
    if ft_in_match:
        ft = ft_in_match.group(1)
        inch = ft_in_match.group(2) or "0"
        return f"{ft}'{inch}\""
        
    # Try parsing pure decimal feet e.g., 5.5
    decimal_match = re.match(r"^([456])\.(\d+)$", s)
    if decimal_match:
        ft = decimal_match.group(1)
        inch = decimal_match.group(2)
        # Handle single digit decimal like 5.5 (which usually means 5 feet 6 inches or just 5'5")
        if len(inch) == 1:
            inch = str(int(inch) * 2) # e.g. 5.5 -> 5'10" or keep it literal
        return f"{ft}'{inch}\""

    # Try parsing centimeter number
    cm_match = re.search(r"(\d+)\s*(?:cm|centimeters)?", s, re.IGNORECASE)
    if cm_match:
        try:
            cm = float(cm_match.group(1))
            if 100 <= cm <= 250:
                # Convert cm to feet/inches
                total_inches = round(cm / 2.54)
                ft = total_inches // 12
                inch = total_inches % 12
                return f"{ft}'{inch}\""
        except ValueError:
            pass
            
    return s

def transform_gender(val: Any) -> Optional[str]:
    """Normalize gender enum values."""
    s = clean_placeholder(val)
    if not s:
        return None
    sl = s.lower()
    if sl in ("m", "male"):
        return "Male"
    if sl in ("f", "female"):
        return "Female"
    if sl in ("nb", "nonbinary", "non-binary"):
        return "Non-binary"
    return "Prefer not to say"

def transform_location(val: Any) -> List[Dict[str, str]]:
    """Normalize location to [{"city": "Mumbai", "country": "India"}]."""
    s = clean_placeholder(val)
    if not s:
        return []
        
    # Common city mapping
    city_map = {
        "bombay": "Mumbai",
        "calcutta": "Kolkata",
        "madras": "Chennai",
        "bangalore": "Bengaluru",
        "delhi NCR": "Delhi",
        "gurgaon": "Gurugram"
    }
    
    parts = [p.strip() for p in re.split(r'[,;/|]', s) if p.strip()]
    res = []
    for p in parts:
        # Match city / country
        city = p
        country = "India"
        if "-" in p:
            subparts = [sp.strip() for sp in p.split("-") if sp.strip()]
            if len(subparts) >= 2:
                city = subparts[0]
                country = subparts[1]
        
        # Apply map
        mapped_city = city_map.get(city.lower(), city)
        res.append({"city": mapped_city.title(), "country": country.title()})
        
    return res

def transform_list(val: Any) -> List[str]:
    """Split comma/slash/semicolon separated items into list of trimmed strings."""
    s = clean_placeholder(val)
    if not s:
        return []
    items = [item.strip() for item in re.split(r'[,;/|]', s) if item.strip()]
    # Normalize tag/skill formatting
    return [item.title() if len(item) > 3 else item for item in items]

def transform_integer(val: Any) -> Optional[int]:
    """Coerce value to integer."""
    s = clean_placeholder(val)
    if not s:
        return None
    try:
        # Extract digits
        digits = "".join(c for c in s if c.isdigit())
        if digits:
            return int(digits)
    except Exception:
        pass
    return None

def transform_boolean(val: Any) -> bool:
    """Normalize truthy values to boolean True/False."""
    s = clean_placeholder(val)
    if not s:
        return False
    return s.lower() in ("yes", "y", "true", "1", "t", "active")

def transform_media_item(val: Any, category: str) -> Optional[Dict[str, Any]]:
    """Parse media reference (Drive, Cloudinary, relative/local paths) into MediaItem dictionary."""
    import uuid
    from datetime import datetime, timezone
    s = clean_placeholder(val)
    if not s:
        return None

    # Default fields
    media_id = str(uuid.uuid4())
    url = s
    public_id = f"imported_{media_id}"
    resource_type = "image"
    if category in ("intro_video", "video", "audition_video", "take", "take_1", "take_2", "take_3"):
        resource_type = "video"
    elif category in ("resume", "portfolio_pdf"):
        resource_type = "raw"
        
    content_type = "application/octet-stream"
    original_filename = s.split("/")[-1].split("?")[0]

    # Google Drive file patterns
    drive_file_match = re.search(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)', s)
    drive_open_match = re.search(r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)', s)
    drive_id = (drive_file_match or drive_open_match)
    
    if drive_id:
        file_id = drive_id.group(1)
        public_id = f"google_drive_{file_id}"
        # Direct download link that works for rendering
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        content_type = "image/jpeg" if resource_type == "image" else "video/mp4"
    elif "res.cloudinary.com" in s:
        # Extract public_id from Cloudinary URL: res.cloudinary.com/<cloud>/image/upload/v<version>/<public_id>
        parts = s.split("/upload/")
        if len(parts) > 1:
            # Strip folder version prefix if present e.g. v12345/folder/name
            pub_part = parts[1]
            if pub_part.startswith("v") and "/" in pub_part:
                pub_part = "/".join(pub_part.split("/")[1:])
            # Strip extension
            public_id = ".".join(pub_part.split(".")[:-1]) if "." in pub_part else pub_part

    return {
        "id": media_id,
        "category": category,
        "url": url,
        "public_id": public_id,
        "resource_type": resource_type,
        "content_type": content_type,
        "original_filename": original_filename,
        "size": 0,
        "created_at": datetime.now(timezone.utc).isoformat()
    }

def transform_media_list(val: Any, category: str) -> List[Dict[str, Any]]:
    """Parse comma/newline separated media paths/urls into a list of MediaItem dictionaries."""
    s = clean_placeholder(val)
    if not s:
        return []
        
    # Split by comma or newline
    items = [item.strip() for item in re.split(r'[,\n]', s) if item.strip()]
    res = []
    for item in items:
        media = transform_media_item(item, category)
        if media:
            res.append(media)
    return res

def is_height_greater_than_5_8(h_str: str) -> bool:
    if not h_str:
        return False
    match = re.match(r"(\d+)'(\d+)\"?", h_str)
    if match:
        try:
            ft = int(match.group(1))
            inches = int(match.group(2))
            if ft > 5:
                return True
            if ft == 5 and inches > 8:
                return True
        except ValueError:
            pass
    return False

async def apply_auto_label_rules(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Appends internal labels/tags dynamically matching rules loaded from MongoDB."""
    import uuid
    from core import db
    
    tags = doc.get("tags", [])
    if not isinstance(tags, list):
        tags = []
        
    existing_tag_names = {t.get("name", "").lower() for t in tags if isinstance(t, dict)}
    
    # Fetch all custom auto-label rules from DB
    rules_cursor = db.label_rules.find({})
    rules = await rules_cursor.to_list(length=100)
    
    for rule in rules:
        field = rule.get("field")
        val = rule.get("value")
        op = rule.get("operator", "equals")
        label = rule.get("label")
        if not field or not label:
            continue
            
        should_label = False
        
        # 1. Location matches
        if field == "location":
            locs = doc.get("location", [])
            if isinstance(locs, list):
                if op == "city_equals":
                    should_label = any(isinstance(l, dict) and l.get("city", "").lower() == str(val).lower() for l in locs)
                elif op == "country_equals":
                    should_label = any(isinstance(l, dict) and l.get("country", "").lower() == str(val).lower() for l in locs)
                    
        # 2. Height matches
        elif field == "height":
            height = doc.get("height")
            if height:
                if op == "height_greater_than":
                    should_label = is_height_greater_than_5_8(height)
                    
        # 3. Simple equality matches (gender, ethnicity, status, etc)
        else:
            doc_val = doc.get(field)
            if doc_val is not None:
                if op == "equals":
                    should_label = str(doc_val).lower() == str(val).lower()
                elif op == "contains":
                    should_label = str(val).lower() in str(doc_val).lower()
                    
        if should_label and label.lower() not in existing_tag_names:
            tags.append({"id": str(uuid.uuid4()), "name": label})
            existing_tag_names.add(label.lower())
            
    doc["tags"] = tags
    return doc



