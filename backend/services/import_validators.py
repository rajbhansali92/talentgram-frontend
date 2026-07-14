import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from services.import_transformers import clean_placeholder

def validate_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    # Basic email regex
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(pattern, email):
        return "Invalid email address format"
    return None

def validate_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return "Phone number is required"
    # Must start with + followed by 10-15 digits
    pattern = r"^\+[1-9]\d{9,14}$"
    if not re.match(pattern, phone):
        return "Invalid phone format. Expected E.164 format (e.g. +91XXXXXXXXXX)"
    return None

def validate_instagram(instagram: Optional[str]) -> Optional[str]:
    if not instagram:
        return None
    # Must start with http/https and instagram.com
    pattern = r"^https?://(www\.)?instagram\.com/[a-zA-Z0-9_.-]+/?$"
    if not re.match(pattern, instagram):
        return "Invalid Instagram URL format"
    return None

def validate_age_range(age: Optional[int]) -> Optional[str]:
    if age is None:
        return None
    if not (0 <= age <= 120):
        return "Age must be between 0 and 120"
    return None

def validate_dob(dob: Optional[str]) -> Optional[str]:
    if not dob:
        return None
    # Expect YYYY-MM-DD
    pattern = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(pattern, dob):
        return "Invalid DOB format. Expected YYYY-MM-DD"
    try:
        y, m, d = [int(x) for x in dob.split("-")]
        # Date must be in the past
        dt = datetime(y, m, d)
        if dt.year < 1900 or dt > datetime.now():
            return "Date of Birth must be between 1900 and today"
    except Exception:
        return "Invalid calendar date"
    return None

def validate_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a single row document, returning status ('valid', 'error', 'warning') and messages."""
    errors = {}
    warnings = {}
    
    # 1. Required fields
    if not clean_placeholder(row.get("name")):
        errors["name"] = "Full name is required"
        
    phone = row.get("phone")
    if not phone:
        errors["phone"] = "Phone number is required"
    else:
        phone_err = validate_phone(phone)
        if phone_err:
            errors["phone"] = phone_err
            
    # 2. Field format validations
    email_err = validate_email(row.get("email"))
    if email_err:
        errors["email"] = email_err
        
    insta_err = validate_instagram(row.get("instagram_handle"))
    if insta_err:
        errors["instagram_handle"] = insta_err
        
    age_err = validate_age_range(row.get("age"))
    if age_err:
        errors["age"] = age_err
        
    dob_err = validate_dob(row.get("dob"))
    if dob_err:
        errors["dob"] = dob_err
        
    # 3. Cross-field consistency (Age vs DOB)
    age = row.get("age")
    dob = row.get("dob")
    if age is not None and dob and not dob_err:
        try:
            y, m, d = [int(x) for x in dob.split("-")[:3]]
            today = datetime.now(timezone.utc).date()
            computed_age = today.year - y - (1 if (today.month, today.day) < (m, d) else 0)
            if abs(computed_age - age) > 1:
                warnings["age"] = f"Date of Birth inconsistent with Age (computed: {computed_age}, provided: {age})"
        except Exception:
            pass
            
    # 4. Underage safety checks (e.g. Alcohol ads limitation check if age is below 18)
    if age is not None and age < 18:
        skills_and_tags = (row.get("skills") or []) + (row.get("tags") or [])
        underage_restricted = ["alcohol", "bar", "pub", "smoke", "smoking", "vape"]
        for term in underage_restricted:
            if any(term in str(item).lower() for item in skills_and_tags):
                errors["skills"] = f"Underage talent (age {age}) cannot be associated with restricted categories: {term}"
                break
                
    status = "valid"
    if errors:
        status = "error"
    elif warnings:
        status = "warning"
        
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings
    }

async def verify_row_media(row: Dict[str, Any]) -> List[str]:
    """Asynchronously checks if any mapped media references are valid and reachable using DB configuration settings."""
    import urllib.request
    import urllib.error
    import asyncio
    from core import db
    
    media_list = row.get("media", [])
    if not media_list or not isinstance(media_list, list):
        return []
        
    failures = []
    
    # Load media validation config from database
    config = await db.media_validation_config.find_one({})
    if not config:
        config = {
            "max_size_bytes": 200 * 1024 * 1024,
            "allowed_mime_types": ["image/", "video/", "application/pdf"]
        }
        
    max_size = config.get("max_size_bytes", 200 * 1024 * 1024)
    allowed_types = config.get("allowed_mime_types", ["image/", "video/", "application/pdf"])
    
    def check_url(url: str, category: str):
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
                method='HEAD'
            )
            with urllib.request.urlopen(req, timeout=3.0) as response:
                status_code = response.getcode()
                if status_code not in (200, 206, 301, 302):
                    return f"Unreachable URL (status code: {status_code})"
                
                # Check MIME/Content-Type
                ct = response.headers.get("Content-Type", "")
                
                # Verify content type matches allowed mime types from DB config
                is_allowed = False
                for t in allowed_types:
                    if ct and ct.startswith(t):
                        is_allowed = True
                        break
                if ct and not is_allowed:
                    return f"MIME type '{ct}' is not allowed by Data Hub configurations"
                        
                # Check Size
                size_str = response.headers.get("Content-Length")
                if size_str:
                    try:
                        size = int(size_str)
                        if size > max_size:
                            return f"File size too large: {size / (1024*1024):.1f}MB (max allowed: {max_size / (1024*1024):.1f}MB)"
                    except ValueError:
                        pass
        except urllib.error.URLError as ue:
            return f"Broken URL or DNS failure: {ue.reason}"
        except Exception as e:
            return f"Connection error: {str(e)}"
        return None

    for m in media_list:
        if not isinstance(m, dict):
            continue
        url = m.get("url")
        category = m.get("category", "")
        
        # Skip relative or placeholder paths
        if not url or not url.startswith("http"):
            continue
            
        err = await asyncio.to_thread(check_url, url, category)
        if err:
            failures.append(f"Media validation error for {category} ({url}): {err}")
            
    return failures

def compute_file_checksum(content: bytes) -> str:
    """Generates SHA256 hex checksum of spreadsheet file contents to prevent duplicate uploads."""
    import hashlib
    return hashlib.sha256(content).hexdigest()


