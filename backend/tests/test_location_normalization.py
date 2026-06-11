import sys
from pathlib import Path
import os

os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["RESEND_API_KEY"] = "dummy"
os.environ["SENDGRID_API_KEY"] = "dummy"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "password"

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import TalentIn, LocationItem


from scripts.migrate_locations import parse_raw_location, is_ambiguous


def test_is_ambiguous():
    assert is_ambiguous("Punjab / Mumbai") is True
    assert is_ambiguous("Delhi-Mumbai") is True
    assert is_ambiguous("Delhi & Mumbai") is True
    assert is_ambiguous("Delhi and Mumbai") is True
    assert is_ambiguous("Punjab") is True
    assert is_ambiguous("Mumbai") is False
    assert is_ambiguous("mumbai, india") is False


def test_parse_raw_location_simple():
    parsed, review = parse_raw_location("mumbai")
    assert parsed == [{"city": "Mumbai", "country": "India"}]
    assert review is False

    parsed, review = parse_raw_location("Mumbai, India")
    assert parsed == [{"city": "Mumbai", "country": "India"}]
    assert review is False

    parsed, review = parse_raw_location("Dubai, UAE")
    assert parsed == [{"city": "Dubai", "country": "UAE"}]
    assert review is False


def test_parse_raw_location_ambiguous():
    parsed, review = parse_raw_location("Punjab / Mumbai")
    assert parsed == [{"city": "Punjab", "country": "India"}, {"city": "Mumbai", "country": "India"}]
    assert review is True

    parsed, review = parse_raw_location("Delhi-Mumbai")
    assert parsed == [{"city": "Delhi", "country": "India"}, {"city": "Mumbai", "country": "India"}]
    assert review is True


def test_talent_in_validator():
    talent = TalentIn(
        name="John Doe",
        location="Mumbai, India"
    )
    assert len(talent.location) == 1
    assert talent.location[0].city == "Mumbai"
    assert talent.location[0].country == "India"

    talent2 = TalentIn(
        name="Jane Doe",
        location=[{"city": "Dubai", "country": "UAE"}]
    )
    assert len(talent2.location) == 1
    assert talent2.location[0].city == "Dubai"
    assert talent2.location[0].country == "UAE"
