from fpdf import FPDF
from typing import Optional

def privatize_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "Unnamed"
    parts = s.split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    last_initial = parts[-1][0].upper()
    return f"{first} {last_initial}."

def _safe_text(s: str, max_len: int = 50) -> str:
    if not s:
        return "—"
    s = str(s)
    words = s.split()
    safe_words = []
    for w in words:
        if len(w) > max_len:
            safe_words.append(w[:max_len-3] + "...")
        else:
            safe_words.append(w)
    return " ".join(safe_words)

def _get_link_label(url: str) -> str:
    url_lower = url.lower()
    if "youtube" in url_lower or "youtu.be" in url_lower:
        return "YouTube"
    if "instagram" in url_lower:
        return "Instagram"
    return "Work Link"

def _generate_talent_details_pdf(talent_doc: dict, agreed_val: Optional[str], client_status: Optional[str]) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    
    # Header - Branding
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 10, "TALENTGRAM", ln=True, align="L")
    
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, "Talent Profile", ln=True, align="L")
    pdf.ln(10)
    
    # Metadata sections
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(138, 138, 138)
    pdf.cell(0, 6, "GENERAL INFORMATION", ln=True)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_text_color(17, 17, 17)
    fields = [
        ("Name", privatize_name(talent_doc.get("name") or "Unnamed")),
        ("Age", str(talent_doc.get("age") or "—")),
        ("Height", talent_doc.get("height") or "—"),
        ("Location", talent_doc.get("location") or "—"),
    ]
    
    # Budget
    budget = talent_doc.get("budget")
    fields.append(("Budget", "—"))
            
    for label, val in fields:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(17, 17, 17)
        pdf.cell(50, 7, f"{label}", ln=False)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 7, _safe_text(val))
        pdf.ln(1)
        
    # Media and Links
    work_links = talent_doc.get("work_links") or []
    videos = [m for m in (talent_doc.get("media") or []) if m.get("category") == "video"]
    
    if work_links or videos:
        pdf.ln(5)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(138, 138, 138)
        pdf.cell(0, 6, "WORK LINKS & MEDIA", ln=True)
        pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 190, pdf.get_y())
        pdf.ln(4)
        
        pdf.set_font("Helvetica", "", 10)
        idx = 1
        for v in videos:
            pdf.set_text_color(0, 102, 204)
            url = v.get("url") or ""
            if url:
                pdf.cell(0, 6, f"{idx}. Introduction Video", ln=True, link=url)
                idx += 1
                
        for wl in work_links:
            pdf.set_text_color(0, 102, 204)
            lbl = _get_link_label(wl)
            pdf.cell(0, 6, f"{idx}. {lbl}", ln=True, link=wl)
            idx += 1
            
    return pdf.output()

talent_doc = {
    "name": "Priya Sharma",
    "age": 25,
    "work_links": ["https://youtube.com/watch?v=123"],
    "media": [{"category": "video", "url": "https://example.com/video1.mp4"}]
}

try:
    pdf_bytes = _generate_talent_details_pdf(talent_doc, "1000", "interested")
    print("Success!", type(pdf_bytes))
except Exception as e:
    import traceback
    traceback.print_exc()
