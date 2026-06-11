import sys
import os
import math

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
from routers.links import _generate_talent_details_pdf, _safe_text

talent_doc = {
    "name": "Priya Sharma",
    "custom_answers": [
        {"question": "Do you have a passport?", "answer": "Yes"}
    ]
}

class DebugFPDF(FPDF):
    def multi_cell(self, w, h, text, *args, **kwargs):
        print(f"DEBUG multi_cell: w={w}, h={h}, x={self.get_x()}, y={self.get_y()}, rmargin={self.r_margin}, w_pt={self.w_pt}, text='{text}'")
        super().multi_cell(w, h, text, *args, **kwargs)

import routers.links
routers.links.FPDF = DebugFPDF

_generate_talent_details_pdf(talent_doc, "1000", "interested")
