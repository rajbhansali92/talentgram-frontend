import sys
import os

os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["RESEND_API_KEY"] = "dummy"
os.environ["SENDGRID_API_KEY"] = "dummy"
os.environ["ADMIN_EMAIL"] = "dummy"
os.environ["ADMIN_PASSWORD"] = "dummy"
os.environ["CLOUDINARY_URL"] = "cloudinary://foo:bar@baz"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"

sys.path.insert(0, os.path.abspath('backend'))
from fpdf import FPDF
from routers.links import _safe_text, _get_link_label

def _generate_talent_details_pdf(talent_doc: dict, agreed_val: str, client_status: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 10, "TALENTGRAM", ln=True, align="L")
    
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, "Talent Profile", ln=True, align="L")
    pdf.ln(10)

    work_links = talent_doc.get("work_links") or []
    videos = []
    
    idx = 1
    for wl in work_links:
        pdf.set_text_color(0, 102, 204)
        lbl = _get_link_label(wl)
        pdf.set_x(10)
        pdf.cell(0, 6, f"{idx}. {lbl}", ln=True, link=wl)
        idx += 1
        
    custom_answers = talent_doc.get("custom_answers") or []
    for qa in custom_answers:
        q = qa.get("question") or ""
        a = qa.get("answer") or ""
        pdf.set_text_color(17, 17, 17)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_x(10)
        pdf.multi_cell(0, 6, f"Question: {_safe_text(q)}")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(10)
        pdf.multi_cell(0, 6, f"Answer: {_safe_text(a)}")
        pdf.ln(2)
        
    return pdf.output()

talent_doc = {
    "name": "Priya Sharma",
    "work_links": ["https://youtube.com/watch?v=dQw4w9WgXcQ"],
    "custom_answers": [{"question": "Do you have a passport?", "answer": "Yes"}]
}

try:
    _generate_talent_details_pdf(talent_doc, "1000", "interested")
    print("SUCCESS")
except Exception as e:
    import traceback
    traceback.print_exc()

