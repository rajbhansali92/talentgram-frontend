"""Regression tests for the Client View -> Download Talent Folder PDF.

Reproduces the production incident where downloading a talent folder failed
with HTTP 500 "Unable to generate Talent Folder. Please try again or contact
Talentgram support." The root cause was `_generate_talent_details_pdf` calling
a bare `", ".join(location)` which raised `TypeError` when `location` was
stored as a list of `{city, country}` objects — a legitimate, existing shape
(see Open Issue #4 "Mixed Location Formats" and
`scripts/migrate_locations.parse_raw_location`, which emits exactly that shape,
e.g. `[{"city": "Dubai", "country": "UAE"}]`).

These tests exercise the REAL `_generate_talent_details_pdf` helper from
`routers.links` for every supported location shape. The `array_of_objects`
case raises on the pre-fix implementation (test fails) and produces a valid
PDF on the fixed implementation (test passes).
"""

import os
import sys
from pathlib import Path

# Dummy config so importing backend modules doesn't fail on missing env.
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("RESEND_API_KEY", "dummy")
os.environ.setdefault("SENDGRID_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "dummy")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from routers.links import _generate_talent_details_pdf  # noqa: E402


def _talent_doc(location):
    """A minimal client-shaped talent doc (as produced by
    `_submission_to_client_shape`), varying only `location`."""
    return {
        "name": "Dia S",
        "age": 33,
        "height": "5'7\"",
        "location": location,
        "availability": {"status": "yes", "note": "10th July"},
        "budget": {"status": "accept"},
        "custom_answers": [{"question": "Any allergies?", "answer": "None"}],
    }


# Every location shape the app can persist (Open Issue #4). `array_of_objects`
# is the shape that crashed production.
LOCATION_SHAPES = [
    ("string", "Dubai, United Arab Emirates"),
    ("array_of_strings", ["Dubai", "United Arab Emirates"]),
    ("array_of_objects", [{"city": "Dubai", "country": "United Arab Emirates"}]),
    ("single_object", {"city": "Dubai", "country": "United Arab Emirates"}),
    ("empty", ""),
]


@pytest.mark.parametrize(
    "shape_id, location",
    LOCATION_SHAPES,
    ids=[s[0] for s in LOCATION_SHAPES],
)
def test_talent_folder_pdf_generates_for_every_location_shape(shape_id, location):
    """The Talent Folder PDF must generate for every supported location shape.

    Regression guard: the `array_of_objects` case raises `TypeError` on the
    pre-fix implementation (bare `", ".join` over dicts), which surfaces to the
    client as the HTTP 500 "Unable to generate Talent Folder." It must produce
    a valid PDF on the fixed implementation.
    """
    pdf = _generate_talent_details_pdf(_talent_doc(location), "50k / day", "interested")
    data = bytes(pdf)
    assert data[:4] == b"%PDF", f"{shape_id}: output is not a PDF"
    assert len(data) > 100, f"{shape_id}: PDF is suspiciously small"


def test_format_location_is_backwards_compatible_and_safe():
    """The location formatter must never raise and must preserve the existing
    rendering for the shapes that already worked (string, list of strings)."""
    from routers.links import _format_location

    # Backwards compatible: unchanged output for previously-working shapes.
    assert _format_location("Dubai, United Arab Emirates") == "Dubai, United Arab Emirates"
    assert _format_location(["Dubai", "United Arab Emirates"]) == "Dubai, United Arab Emirates"
    # The formerly-crashing shapes now render cleanly instead of raising.
    assert (
        _format_location([{"city": "Dubai", "country": "United Arab Emirates"}])
        == "Dubai, United Arab Emirates"
    )
    assert (
        _format_location({"city": "Dubai", "country": "United Arab Emirates"})
        == "Dubai, United Arab Emirates"
    )
    # Empty / missing values.
    assert _format_location("") == ""
    assert _format_location(None) == ""
    assert _format_location([]) == ""
