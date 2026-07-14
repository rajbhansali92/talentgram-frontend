import os
os.environ["JWT_SECRET"] = "supersecretkey123"
os.environ["MONGO_URL"] = "mongodb+srv://team_db_user:Wxp0xYSOiwzb9GyE@cluster0.sipmssu.mongodb.net/talentgram?retryWrites=true&w=majority"

import sys
sys.path.insert(0, "/Users/rajrbhansali/.gemini/antigravity/scratch/talentgram-frontend/backend")

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from server import app
from services.import_service import auto_detect_headers, apply_transforms
from services.import_transformers import (
    transform_name, transform_phone, transform_instagram,
    transform_height, transform_gender, transform_location, transform_list,
    transform_media_item, apply_auto_label_rules
)
from services.import_validators import validate_row
from services.import_duplicates import merge_duplicate_profile

client = TestClient(app)

def test_auto_detect_headers():
    headers = ["Full Name", "Contact", "ig handle", "D.O.B.", "unrelated"]
    mapping = auto_detect_headers(headers)
    assert mapping["name"] == "Full Name"
    assert mapping["phone"] == "Contact"
    assert mapping["instagram_handle"] == "ig handle"
    assert mapping["dob"] == "D.O.B."
    assert mapping["email"] is None

def test_transformers():
    # Name
    assert transform_name("john doe") == "John Doe"
    assert transform_name("   RAJ    BHANSALI   ") == "Raj Bhansali"
    assert transform_name("N/A") is None
    
    # Phone
    assert transform_phone("9820443626") == "+919820443626"
    assert transform_phone("+91 98204 43626") == "+919820443626"
    assert transform_phone("919820443626") == "+919820443626"
    
    # Instagram
    assert transform_instagram("abc") == "https://instagram.com/abc"
    assert transform_instagram("instagram.com/abc") == "https://instagram.com/abc"
    assert transform_instagram("https://www.instagram.com/abc/") == "https://instagram.com/abc"
    assert transform_instagram("@abc") == "https://instagram.com/abc"
    
    # Height
    assert transform_height("5'5\"") == "5'5\""
    assert transform_height("5 ft 5") == "5'5\""
    assert transform_height("165 cm") == "5'5\""
    
    # Gender
    assert transform_gender("m") == "Male"
    assert transform_gender("FEMALE") == "Female"
    assert transform_gender("non-binary") == "Non-binary"
    assert transform_gender("something") == "Prefer not to say"
    
    # Location
    locs = transform_location("Bombay-India; delhi-India")
    assert len(locs) == 2
    assert locs[0]["city"] == "Mumbai"
    assert locs[0]["country"] == "India"
    assert locs[1]["city"] == "Delhi"
    assert locs[1]["country"] == "India"

    # Media items
    drive_m = transform_media_item("https://drive.google.com/file/d/12345/view", "profile_image")
    assert drive_m is not None
    assert drive_m["public_id"] == "google_drive_12345"
    assert "export=download&id=12345" in drive_m["url"]

    cl_m = transform_media_item("https://res.cloudinary.com/talentgram/image/upload/v12345/folder/sample.jpg", "portfolio")
    assert cl_m is not None
    assert cl_m["public_id"] == "folder/sample"


def test_row_validation():
    # Valid row
    row_valid = {
        "name": "Deeya Damini",
        "phone": "+919820443626",
        "email": "deeya@talent.com",
        "age": 25,
        "dob": "2001-01-01"
    }
    v1 = validate_row(row_valid)
    assert v1["status"] == "valid"
    assert not v1["errors"]
    
    # Missing required name
    row_no_name = {
        "phone": "+919820443626"
    }
    v2 = validate_row(row_no_name)
    assert v2["status"] == "error"
    assert "name" in v2["errors"]
    
    # Inconsistent Age vs DOB
    row_inconsistent = {
        "name": "Deeya Damini",
        "phone": "+919820443626",
        "age": 50,
        "dob": "2001-01-01"
    }
    v3 = validate_row(row_inconsistent)
    assert v3["status"] == "warning"
    assert "age" in v3["warnings"]

def test_duplicate_merge():
    existing = {
        "name": "Raj Bhansali",
        "phone": "+919820443626",
        "email": "",
        "skills": ["Actor"],
        "media": [{"public_id": "profile_1", "url": "https://img.com/p1.jpg"}]
    }
    incoming = {
        "name": "Raj Bhansali",
        "phone": "+919820443626",
        "email": "raj@talentgram.com",
        "skills": ["Actor", "Dancer"],
        "media": [{"public_id": "profile_1", "url": "https://img.com/p1.jpg"}, {"public_id": "profile_2", "url": "https://img.com/p2.jpg"}]
    }
    
    # Test merge arrays mode
    m_arr = merge_duplicate_profile(existing, incoming, "merge")
    assert m_arr["email"] == "raj@talentgram.com"
    assert "Dancer" in m_arr["skills"]
    assert "Actor" in m_arr["skills"]
    assert len(m_arr["skills"]) == 2
    assert len(m_arr["media"]) == 2

@pytest.mark.asyncio
async def test_auto_labeling():
    doc = {
        "location": [{"city": "Mumbai", "country": "India"}],
        "gender": "Female",
        "height": "5'9\"",
        "tags": []
    }
    
    from core import db
    rules = [
        {"field": "location", "operator": "city_equals", "value": "Mumbai", "label": "Mumbai"},
        {"field": "gender", "operator": "equals", "value": "Female", "label": "Female"},
        {"field": "height", "operator": "height_greater_than", "value": "5'8\"", "label": "Tall"}
    ]
    
    await db.label_rules.delete_many({"field": {"$in": ["location", "gender", "height"]}})
    await db.label_rules.insert_many(rules)
    
    try:
        labeled = await apply_auto_label_rules(doc)
        tag_names = {t["name"] for t in labeled["tags"]}
        assert "Mumbai" in tag_names
        assert "Female" in tag_names
        assert "Tall" in tag_names
        assert len(tag_names) == 3
    finally:
        await db.label_rules.delete_many({"field": {"$in": ["location", "gender", "height"]}})



