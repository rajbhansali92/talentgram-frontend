"""Roster / comp-card PDF builder (fpdf2).

Resource model — deliberately NOT the buffer-everything pattern of the client
ZIP in routers/links.py:
  * images are fetched with a small bounded concurrency, each one is decoded,
    EXIF-rotated, downscaled to a print-appropriate long edge and re-encoded as
    a JPEG on DISK (a temp dir that is always cleaned up); the original bytes
    are dropped immediately,
  * only those downscaled JPEGs are embedded (fpdf2 embeds a JPEG file as-is),
  * the finished PDF is written to disk and streamed by the caller,
  * at most ROSTER_PDF_MAX_CONCURRENT builds run at once; a finished PDF is
    cached on local disk keyed by a content hash (so "download again later" is
    instant, and any roster edit naturally produces a new key).

Visuals follow the Talentgram app: Manrope, black / white / neutral, large
photography, generous whitespace. The Talentgram logo asset is drawn at its
native aspect ratio, never altered.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageOps

from services import roster_layout as L

logger = logging.getLogger("talentgram")

_HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.abspath(os.path.join(_HERE, "..", "assets"))
LOGO_PATH = os.path.join(ASSETS, "talentgram-black.png")
FONT_DIR = os.path.join(ASSETS, "fonts")

FETCH_CONCURRENCY = int(os.environ.get("ROSTER_PDF_FETCH_CONCURRENCY", "6"))
MAX_CONCURRENT_BUILDS = int(os.environ.get("ROSTER_PDF_MAX_CONCURRENT", "2"))
MAX_SOURCE_BYTES = 40 * 1024 * 1024
INDEX_MIN_TALENTS = 6
CACHE_DIR = os.path.join(tempfile.gettempdir(), "tg_roster_pdf")
CACHE_MAX_FILES = 24
CACHE_TTL_SEC = 6 * 3600

_build_slots = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)
_key_locks: Dict[str, asyncio.Lock] = {}

INK = (17, 17, 17)
MUTED = (125, 125, 122)
HAIR = (222, 222, 218)


def target_long_edge(total_images: int) -> int:
    """Smaller embedded images as the roster grows, so the PDF stays a sane size."""
    if total_images <= 120:
        return 1600
    if total_images <= 250:
        return 1300
    return 1000


# --------------------------------------------------------------------------
# Image preparation (bounded, on disk)
# --------------------------------------------------------------------------
def _process_image_bytes(data: bytes, dest: str, long_edge: int) -> Tuple[int, int]:
    im = Image.open(io.BytesIO(data))
    try:
        im.draft("RGB", (long_edge, long_edge))  # JPEG: decode at reduced size
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((long_edge, long_edge), Image.LANCZOS)
    im.save(dest, "JPEG", quality=84, optimize=True)
    return im.size


async def _fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, dest: str, long_edge: int):
    async with sem:
        last = None
        for _ in range(2):
            try:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        last = f"HTTP {r.status_code}"
                        continue
                    buf = bytearray()
                    async for chunk in r.aiter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) > MAX_SOURCE_BYTES:
                            raise ValueError("source image too large")
                size = await asyncio.to_thread(_process_image_bytes, bytes(buf), dest, long_edge)
                del buf
                return size
            except Exception as e:  # noqa: BLE001
                last = str(e)
        logger.warning("roster pdf: image fetch/process failed (%s): %s", url[:80], last)
        return None


async def prepare_images(talents: List[dict], workdir: str, client: httpx.AsyncClient) -> Dict[str, dict]:
    """Download + downscale every image (bounded concurrency). Returns
    {media_id: {"path", "w", "h"}} for the images that succeeded."""
    total = sum(len(t["images"]) for t in talents)
    long_edge = target_long_edge(total)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    jobs, order = [], []
    for ti, t in enumerate(talents):
        for ii, img in enumerate(t["images"]):
            dest = os.path.join(workdir, f"{ti:03d}_{ii:02d}.jpg")
            order.append((img["id"], dest))
            jobs.append(_fetch_one(client, sem, img["url"], dest, long_edge))
    results = await asyncio.gather(*jobs)
    out: Dict[str, dict] = {}
    for (mid, dest), size in zip(order, results):
        if size:
            out[mid] = {"path": dest, "w": size[0], "h": size[1]}
    return out


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def _setup_fonts(pdf) -> None:
    for fam, f in (("MLight", "Manrope-Light.ttf"), ("M", "Manrope-Regular.ttf"),
                   ("MMed", "Manrope-Medium.ttf"), ("MSemi", "Manrope-SemiBold.ttf")):
        pdf.add_font(fam, "", os.path.join(FONT_DIR, f))


def _logo(pdf, x: float, y: float, w: float) -> None:
    if os.path.exists(LOGO_PATH):
        with Image.open(LOGO_PATH) as im:
            ratio = im.height / im.width
        pdf.image(LOGO_PATH, x=x, y=y, w=w, h=w * ratio)  # native aspect ratio
    else:  # asset missing: plain wordmark rather than a broken page
        pdf.set_font("MSemi", size=10)
        pdf.set_xy(x, y)
        pdf.cell(w, 5, "TALENTGRAM")


def _text(pdf, x, y, w, text, font="M", size=10, color=INK, align="L", spacing=0.0, h=None, link=None):
    pdf.set_font(font, size=size)
    pdf.set_text_color(*color)
    pdf.set_char_spacing(spacing)
    pdf.set_xy(x, y)
    pdf.cell(w, h or size * 0.45, text, align=align, link=link or "")
    pdf.set_char_spacing(0)


def _fit_text(pdf, text: str, font: str, size: float, max_w: float, spacing: float = 0.0) -> str:
    pdf.set_font(font, size=size)
    pdf.set_char_spacing(spacing)
    try:
        if pdf.get_string_width(text) <= max_w:
            return text
        while len(text) > 1 and pdf.get_string_width(text + "…") > max_w:
            text = text[:-1]
        return text.rstrip() + "…"
    finally:
        pdf.set_char_spacing(0)


def _wrap(pdf, text: str, font: str, size: float, max_w: float, max_lines: int = 2) -> List[str]:
    """Word-wrap into at most `max_lines`; the last line is ellipsised if needed."""
    pdf.set_font(font, size=size)
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if pdf.get_string_width(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit_text(pdf, lines[-1] + " …", font, size, max_w)
    return [_fit_text(pdf, ln, font, size, max_w) for ln in lines] or [""]


def _header(pdf, right_text: str) -> None:
    _logo(pdf, L.MARGIN, 7.5, 28.0)
    _text(pdf, L.PAGE_W - L.MARGIN - 80, 11.5, 80, right_text, "MMed", 6.8, MUTED, "R", 0.9)
    pdf.set_draw_color(*HAIR)
    pdf.set_line_width(0.2)
    pdf.line(L.MARGIN, 22.0, L.PAGE_W - L.MARGIN, 22.0)


def _footer(pdf, roster_title: str, page_no: int) -> None:
    pdf.set_draw_color(*HAIR)
    pdf.set_line_width(0.2)
    pdf.line(L.MARGIN, L.FOOTER_Y - 3.5, L.PAGE_W - L.MARGIN, L.FOOTER_Y - 3.5)
    title = _fit_text(pdf, roster_title.upper(), "MMed", 6.2, 130, 0.9)
    _text(pdf, L.MARGIN, L.FOOTER_Y, 130, title, "MMed", 6.2, MUTED, "L", 0.9)
    _text(pdf, L.PAGE_W - L.MARGIN - 20, L.FOOTER_Y, 20, f"{page_no:02d}", "MMed", 6.2, MUTED, "R", 0.9)


def _draw_slot(pdf, slot: L.Slot, prepared: Dict[str, dict], workdir: str) -> None:
    info = prepared.get(slot.media_id)
    if not info:
        return
    path = info["path"]
    if slot.crop and slot.fit == "cover":
        cx, cy, cw, ch = slot.crop
        if not (cx == 0 and cy == 0 and cw == 1 and ch == 1):
            with Image.open(path) as im:
                W, H = im.size
                box = (int(round(cx * W)), int(round(cy * H)), int(round((cx + cw) * W)), int(round((cy + ch) * H)))
                cropped = im.crop(box)
                path = os.path.join(workdir, f"crop_{slot.media_id[:12]}.jpg")
                cropped.save(path, "JPEG", quality=86, optimize=True)
    pdf.image(path, x=slot.x, y=slot.y, w=slot.w, h=slot.h)


def _draw_info(pdf, placed: List[dict], rule_y: Optional[float]) -> None:
    """Draw metadata exactly where the layout engine placed it (same plan the
    web page renders): hairline above, small tracked labels, wrapped values."""
    if not placed:
        return
    if rule_y is not None:
        pdf.set_draw_color(*HAIR)
        pdf.set_line_width(0.2)
        pdf.line(L.MARGIN, rule_y, L.PAGE_W - L.MARGIN, rule_y)
    for it in placed:
        _text(pdf, it["x"], it["y"], it["w"] - 3, it["label"].upper(), "MMed", 6.2, MUTED, "L", 1.0)
        for k, ln in enumerate(it["lines"]):
            _text(pdf, it["x"], it["y"] + L.LABEL_TO_VALUE + k * L.VALUE_LINE_H, it["w"] - 3, ln, "M", L.VALUE_PT, INK, "L", 0.0)
        if it.get("href"):
            pdf.set_font("M", size=L.VALUE_PT)
            pdf.link(it["x"], it["y"] + 4.8, min(it["w"] - 3, pdf.get_string_width(it["lines"][0]) + 1), 5.5, it["href"])


def _video_button(pdf, url: str, y: float) -> None:
    w, h = 58.0, 9.0
    x = L.MARGIN
    pdf.set_fill_color(*INK)
    pdf.rect(x, y, w, h, style="F")
    # play glyph (drawn — Manrope has no ▶)
    pdf.set_fill_color(255, 255, 255)
    pdf.polygon([(x + 4.0, y + 2.6), (x + 4.0, y + 6.4), (x + 7.2, y + 4.5)], style="F")
    _text(pdf, x + 10.5, y + 2.65, w - 12, "INTRODUCTION VIDEO", "MSemi", 6.6, (255, 255, 255), "L", 1.1, h=3.7)
    pdf.link(x, y, w, h, url)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
INSTAGRAM_URL = "https://www.instagram.com/talentgram.agency/"
INSTAGRAM_HANDLE = "@talentgram.agency"
COVER_LOGO_W = 108.0


INSTAGRAM_ICON_W = 6.0
INSTAGRAM_GAP = 2.8
INSTAGRAM_PT = 8.8


def instagram_text_w() -> float:
    """Width (mm) of the handle as drawn on the cover (Manrope Medium 8.2pt, 0.6pt tracking)."""
    from PIL import ImageFont
    f = ImageFont.truetype(os.path.join(FONT_DIR, "Manrope-Medium.ttf"), 1000)
    return f.getlength(INSTAGRAM_HANDLE) / 1000 * INSTAGRAM_PT * 0.352778 + 0.6 * 0.352778 * len(INSTAGRAM_HANDLE)


def _instagram_mark(pdf, x: float, y: float, size: float, color) -> None:
    """Instagram glyph drawn as vectors (rounded square + lens + dot): crisp at any
    zoom and no image asset."""
    pdf.set_draw_color(*color)
    pdf.set_fill_color(*color)
    pdf.set_line_width(size * 0.075)
    pdf.rect(x, y, size, size, style="D", round_corners=True, corner_radius=size * 0.3)
    # NOTE: this fpdf2 build treats circle()'s x/y as the CENTRE (its docstring says
    # upper-left) — verified by rendering; a test pins the glyph's geometry.
    r = size * 0.22
    pdf.circle(x=x + size / 2, y=y + size / 2, radius=r, style="D")
    d = size * 0.06
    pdf.circle(x=x + size * 0.77, y=y + size * 0.23, radius=d, style="F")


def _cover(pdf, title: str, subtitle: Optional[str]) -> None:
    pdf.add_page()
    _logo(pdf, (L.PAGE_W - COVER_LOGO_W) / 2, 62.0, COVER_LOGO_W)
    t = title.strip().upper() or "TALENTGRAM ROSTER"
    size = 19.0
    pdf.set_font("MSemi", size=size)
    pdf.set_char_spacing(2.6)
    while pdf.get_string_width(t) > L.CONTENT_W - 10 and size > 11:
        size -= 0.5
        pdf.set_font("MSemi", size=size)
    pdf.set_char_spacing(0)
    _text(pdf, L.MARGIN, 168.0, L.CONTENT_W, t, "MSemi", size, INK, "C", 2.6, h=9)
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.35)
    pdf.line(L.PAGE_W / 2 - 9, 185.0, L.PAGE_W / 2 + 9, 185.0)
    if subtitle:
        _text(pdf, L.MARGIN, 193.0, L.CONTENT_W, _fit_text(pdf, subtitle.upper(), "MLight", 9.0, L.CONTENT_W, 2.0),
              "MLight", 9.0, MUTED, "C", 2.0, h=5)
    # Instagram footer: icon + handle, the whole element is one link
    pdf.set_font("MMed", size=INSTAGRAM_PT)
    pdf.set_char_spacing(0.6)
    tw = pdf.get_string_width(INSTAGRAM_HANDLE)
    pdf.set_char_spacing(0)
    icon, gap = INSTAGRAM_ICON_W, INSTAGRAM_GAP
    total = icon + gap + tw
    x0 = (L.PAGE_W - total) / 2
    y0 = 272.5
    _instagram_mark(pdf, x0, y0, icon, INK)
    _text(pdf, x0 + icon + gap, y0 + (icon - 3.1) / 2, tw + 2, INSTAGRAM_HANDLE, "MMed", INSTAGRAM_PT, INK, "L", 0.6, h=3.1)
    pdf.link(x0 - 2, y0 - 2, total + 4, icon + 4, INSTAGRAM_URL)


def _index(pdf, title: str, talents: List[dict], link_ids: List[int], start_page: int) -> int:
    per_page = 20
    pages = (len(talents) + per_page - 1) // per_page
    for p in range(pages):
        pdf.add_page()
        _header(pdf, "ROSTER")
        _text(pdf, L.MARGIN, 32.0, 120, "ROSTER", "MSemi", 22.0, INK, "L", 1.2, h=10)
        chunk = talents[p * per_page:(p + 1) * per_page]
        y = 54.0
        for k, t in enumerate(chunk):
            idx = p * per_page + k
            pdf.set_draw_color(*HAIR)
            pdf.set_line_width(0.2)
            pdf.line(L.MARGIN, y - 2.2, L.PAGE_W - L.MARGIN, y - 2.2)
            _text(pdf, L.MARGIN, y, 14, f"{idx + 1:02d}", "MMed", 8.5, MUTED, "L", 0.8)
            _text(pdf, L.MARGIN + 16, y - 0.4, 120, t["name"].upper(), "MSemi", 10.5, INK, "L", 1.0)
            pdf.link(L.MARGIN, y - 2.2, L.CONTENT_W, 9.0, link_ids[idx])
            y += 10.4
        _footer(pdf, title, start_page + p)
    return pages


def _talent_pages(pdf, title: str, ti: int, total: int, t: dict, prepared: Dict[str, dict],
                  workdir: str, page_no: int, link_id: int) -> int:
    imgs = [(i["id"], L.safe_ratio(prepared[i["id"]]["w"], prepared[i["id"]]["h"]))
            for i in t["images"] if i["id"] in prepared]
    pages = L.plan_talent_pages(imgs, t.get("info") or [], bool(t.get("video_url")), t["name"])
    counter = f"{ti + 1:02d} / {total:02d}"
    name_up = t["name"].upper()
    for pi, page in enumerate(pages):
        pdf.add_page()
        if pi == 0:
            pdf.set_link(link_id, page=pdf.page)
        _header(pdf, counter if pi == 0 else f"{name_up}   ·   {counter}")
        if page.kind == "hero":
            size = page.name_pt or 28.0
            pdf.set_font("MSemi", size=size)
            pdf.set_char_spacing(1.0)
            while pdf.get_string_width(name_up) > L.CONTENT_W and size > 14:
                size -= 1
                pdf.set_font("MSemi", size=size)
            pdf.set_char_spacing(0)
            _text(pdf, L.MARGIN, L.NAME_Y, L.CONTENT_W, name_up, "MSemi", size, INK, "L", 1.0, h=13)
            pdf.set_draw_color(*INK)
            pdf.set_line_width(0.35)
            pdf.line(L.MARGIN, L.RULE_Y, L.MARGIN + 14, L.RULE_Y)
            for s in page.slots:
                _draw_slot(pdf, s, prepared, workdir)
            _draw_info(pdf, page.info, page.rule_y)
            if t.get("video_url") and page.video_y is not None:
                _video_button(pdf, t["video_url"], page.video_y)
        elif page.kind == "info":
            isize = page.name_pt or 20.0
            pdf.set_font("MSemi", size=isize)
            pdf.set_char_spacing(1.0)
            while pdf.get_string_width(name_up) > L.CONTENT_W and isize > 12:
                isize -= 1
                pdf.set_font("MSemi", size=isize)
            pdf.set_char_spacing(0)
            _text(pdf, L.MARGIN, L.NAME_Y, L.CONTENT_W, name_up, "MSemi", isize, INK, "L", 1.0, h=10)
            pdf.set_draw_color(*INK)
            pdf.set_line_width(0.35)
            pdf.line(L.MARGIN, 46.0, L.MARGIN + 14, 46.0)
            _draw_info(pdf, page.info, None)
        else:
            for s in page.slots:
                _draw_slot(pdf, s, prepared, workdir)
        _footer(pdf, title, page_no)
        page_no += 1
    return page_no


# --------------------------------------------------------------------------
# Build + cache
# --------------------------------------------------------------------------
# A cached PDF must never outlive the design that produced it. The cache key therefore
# includes a hash of the layout + PDF source files and the logo asset, computed at import:
# any deploy that changes how pages are drawn invalidates every cached PDF automatically
# (no manual version bump to forget). DESIGN_VERSION remains as a manual override.
DESIGN_VERSION = "2026-10-07.2"


def _design_hash() -> str:
    h = hashlib.sha1()
    for path in (os.path.join(_HERE, "roster_layout.py"), os.path.join(_HERE, "roster_pdf.py"),
                 os.path.join(_HERE, "roster_fields.py"), LOGO_PATH):
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"missing:" + path.encode())
    return h.hexdigest()[:12]


_DESIGN_HASH = _design_hash()


def content_key(data: dict) -> str:
    blob = json.dumps([DESIGN_VERSION, _DESIGN_HASH, data], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


def _meta_path(pdf_path: str) -> str:
    return pdf_path[:-4] + ".json"


def _write_meta(pdf_path: str, meta: dict) -> None:
    try:
        with open(_meta_path(pdf_path), "w") as fh:
            json.dump(meta, fh)
    except OSError:
        logger.warning("roster pdf: could not write build metadata (non-fatal)")


def read_meta(pdf_path: str) -> Optional[dict]:
    """{images_total, images_included} recorded when the PDF was built (None for older cache files)."""
    try:
        with open(_meta_path(pdf_path)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cleanup_cache() -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".pdf")]
        for f in os.listdir(CACHE_DIR):           # drop metadata whose PDF is gone
            if f.endswith(".json") and not os.path.exists(os.path.join(CACHE_DIR, f[:-5] + ".pdf")):
                os.remove(os.path.join(CACHE_DIR, f))
        now = time.time()
        for f in files:
            if now - os.path.getmtime(f) > CACHE_TTL_SEC:
                os.remove(f)
        files = sorted((f for f in files if os.path.exists(f)), key=os.path.getmtime)
        for f in files[:-CACHE_MAX_FILES]:
            os.remove(f)
    except Exception:
        logger.exception("roster pdf cache cleanup failed (non-fatal)")


def _render(data: dict, prepared: Dict[str, dict], workdir: str, out_path: str) -> int:
    from fpdf import FPDF
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_margin(0)
    pdf.c_margin = 0  # text starts exactly on the grid line (aligns with images)
    _setup_fonts(pdf)
    pdf.set_title(data["title"])
    pdf.set_author("Talentgram")
    pdf.set_creator("Talentgram")
    pdf.set_compression(True)

    talents = data["talents"]
    _cover(pdf, data["title"], data.get("subtitle"))
    page_no = 2
    link_ids = [pdf.add_link() for _ in talents]
    if len(talents) >= INDEX_MIN_TALENTS:
        page_no += _index(pdf, data["title"], talents, link_ids, page_no)
    for i, t in enumerate(talents):
        page_no = _talent_pages(pdf, data["title"], i, len(talents), t, prepared, workdir, page_no, link_ids[i])
    tmp = out_path + ".part"
    pdf.output(tmp)
    os.replace(tmp, out_path)
    return pdf.page


async def get_or_build_pdf(cache_key: str, data: dict, http_client: Optional[httpx.AsyncClient] = None) -> str:
    """Return the path of the roster PDF for `cache_key`, building it if needed."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"{cache_key}.pdf")
    if os.path.exists(out_path):
        os.utime(out_path, None)
        return out_path
    lock = _key_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        if os.path.exists(out_path):
            return out_path
        async with _build_slots:
            workdir = tempfile.mkdtemp(prefix="tg_roster_")
            own = http_client is None
            client = http_client or httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(45.0, connect=10.0))
            try:
                started = time.monotonic()
                prepared = await prepare_images(data["talents"], workdir, client)
                pages = await asyncio.to_thread(_render, data, prepared, workdir, out_path)
                total_images = sum(len(t["images"]) for t in data["talents"])
                _write_meta(out_path, {"images_total": total_images, "images_included": len(prepared)})
                if len(prepared) < total_images:
                    logger.warning("roster pdf: %d of %d images could not be fetched and were left out (key=%s)",
                                   total_images - len(prepared), total_images, cache_key)
                logger.info("roster pdf built: key=%s talents=%d images=%d/%d pages=%d %.1fs",
                            cache_key, len(data["talents"]), len(prepared),
                            sum(len(t["images"]) for t in data["talents"]), pages, time.monotonic() - started)
            finally:
                if own:
                    await client.aclose()
                shutil.rmtree(workdir, ignore_errors=True)
        await asyncio.to_thread(_cleanup_cache)
    _key_locks.pop(cache_key, None)
    return out_path
