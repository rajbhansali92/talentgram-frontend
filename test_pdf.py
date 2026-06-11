import sys
import os

os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["RESEND_API_KEY"] = "dummy"
os.environ["SENDGRID_API_KEY"] = "dummy"

sys.path.insert(0, os.path.abspath('backend'))

from routers.links import _generate_talent_details_pdf

# Mock talent_doc
talent_doc = {
    "name": "Priya Sharma",
    "age": 25,
    "height": "5'7\"",
    "location": "Mumbai",
    "availability": {"status": "yes", "note": "All next week"},
    "budget": {"status": "accept"},
    "competitive_brand": "None",
    "instagram_handle": "priyasharma_official",
    "instagram_followers": "10k",
    "work_links": [
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://instagram.com/p/1234567890"
    ],
    "media": [
        {"category": "video", "url": "https://example.com/video1.mp4"}
    ],
    "custom_answers": [
        {"question": "Do you have a passport?", "answer": "Yes"}
    ]
}

try:
    pdf_bytes = _generate_talent_details_pdf(talent_doc, "1000 INR / day", "interested")
    print("Success! Generated PDF of size:", len(pdf_bytes))
except Exception as e:
    import traceback
    traceback.print_exc()

