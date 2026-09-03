"""Material text extraction for the AI Casting Desk.

Turns an uploaded file into (a) a Talentgram material category and (b) the
text the Gate-1 parser should read. Every path is best-effort and
non-fatal: a file that can't be read is still attached to the project as a
material, it just contributes no text to the analysis.

  PDF   -> pypdf text
  image -> EasyOCR (reuses backend/scout_capture.ocr_image — the same
           engine AI Scout Capture already runs; no new OCR stack)
  audio -> OpenAI Whisper transcription, ONLY if OPENAI_API_KEY is set
           (the openai package is already a dependency). Otherwise the
           audio is kept as a material and flagged "not transcribed".
  video -> never analysed here; attached as a reference material only.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Talentgram's existing material categories (core.MATERIAL_CATEGORIES).
CATEGORY_SCRIPT = "script"
CATEGORY_IMAGE = "image"
CATEGORY_AUDIO = "audio"
CATEGORY_VIDEO = "video_file"

_IMAGE_CT = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/heic", "image/heif"}
_AUDIO_CT = {"audio/mpeg", "audio/mp3", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac", "audio/m4a", "audio/webm"}
_VIDEO_CT = {"video/mp4", "video/quicktime", "video/webm"}

_EXT_CATEGORY = {
    "pdf": CATEGORY_SCRIPT, "doc": CATEGORY_SCRIPT, "docx": CATEGORY_SCRIPT, "txt": CATEGORY_SCRIPT,
    "png": CATEGORY_IMAGE, "jpg": CATEGORY_IMAGE, "jpeg": CATEGORY_IMAGE, "webp": CATEGORY_IMAGE,
    "heic": CATEGORY_IMAGE, "heif": CATEGORY_IMAGE,
    "mp3": CATEGORY_AUDIO, "wav": CATEGORY_AUDIO, "ogg": CATEGORY_AUDIO, "m4a": CATEGORY_AUDIO, "aac": CATEGORY_AUDIO,
    "mp4": CATEGORY_VIDEO, "mov": CATEGORY_VIDEO, "webm": CATEGORY_VIDEO,
}


def classify_material(filename: str, content_type: Optional[str], override: Optional[str] = None) -> str:
    """Decide the Talentgram material category. Honours an explicit override
    (one of the 4 categories) from the caller."""
    from core import MATERIAL_CATEGORIES

    if override in MATERIAL_CATEGORIES:
        return override
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == "application/pdf":
        return CATEGORY_SCRIPT
    if ct in _IMAGE_CT:
        return CATEGORY_IMAGE
    if ct in _AUDIO_CT:
        return CATEGORY_AUDIO
    if ct in _VIDEO_CT:
        return CATEGORY_VIDEO
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return _EXT_CATEGORY.get(ext, CATEGORY_SCRIPT)


def extract_text_from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        logger.warning("pypdf not installed — PDF material will not be analysed")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:40]:  # a casting brief is never 40+ pages of signal
            pages.append(page.extract_text() or "")
        return "\n".join(pages).strip()
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return ""


def extract_text_from_image(data: bytes) -> str:
    try:
        import scout_capture
        lines, _height = scout_capture.ocr_image(data)
        return "\n".join(l.get("text", "") for l in lines if l.get("text")).strip()
    except Exception as exc:
        logger.warning("image OCR failed: %s", exc)
        return ""


def audio_transcription_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def transcribe_audio(data: bytes, filename: str) -> Optional[str]:
    """Whisper transcription via the already-installed openai package.
    Returns None (not "") when transcription is unavailable so the caller
    can distinguish "no key / failed" from "transcribed to empty"."""
    if not audio_transcription_available():
        return None
    try:
        from openai import OpenAI

        model = os.environ.get("CASTING_DESK_WHISPER_MODEL", "whisper-1")
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        buf = io.BytesIO(data)
        buf.name = filename or "audio.mp3"
        result = client.audio.transcriptions.create(model=model, file=buf, response_format="text")
        return (result if isinstance(result, str) else getattr(result, "text", "")).strip()
    except Exception as exc:
        logger.warning("audio transcription failed: %s", exc)
        return None


def extract_material_text(category: str, data: bytes, filename: str, content_type: Optional[str]) -> Tuple[str, str]:
    """Return (text, status). status ∈ {extracted, empty, skipped, unavailable, failed}."""
    if category == CATEGORY_SCRIPT:
        ct = (content_type or "").lower()
        if ct == "application/pdf" or (filename or "").lower().endswith(".pdf") or data[:5] == b"%PDF-":
            text = extract_text_from_pdf(data)
            return (text, "extracted" if text else "empty")
        # plain text upload
        try:
            text = data.decode("utf-8", errors="ignore").strip()
            return (text, "extracted" if text else "empty")
        except Exception:
            return ("", "failed")
    if category == CATEGORY_IMAGE:
        text = extract_text_from_image(data)
        return (text, "extracted" if text else "empty")
    if category == CATEGORY_AUDIO:
        text = transcribe_audio(data, filename)
        if text is None:
            return ("", "unavailable")
        return (text, "extracted" if text else "empty")
    # video_file — attached as reference only, never analysed
    return ("", "skipped")
