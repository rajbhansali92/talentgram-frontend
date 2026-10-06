"""Roster / comp-card layout engine — pure geometry, no I/O.

One plan drives BOTH the PDF (services/roster_pdf.py) and the web roster
(frontend RosterView), so the two always match. Units are millimetres on an
A4-portrait page (210 x 297). Nothing here knows about Mongo, HTTP or PDF.

Principles (from the product brief):
  * Never stretch: every placed image keeps its exact aspect ratio.
  * Never destructively crop: images are placed with `contain` semantics (the
    slot rectangle has the image's own ratio). The one exception is the hero,
    which may be cropped by at most HERO_MAX_CROP per axis, toward the face
    (focal point in the upper third) — and only when that avoids a visibly
    letter-boxed hero.
  * Images are NOT forced into identical boxes. For each page the engine tries
    every slicing layout (side-by-side / stacked, nested) of the images on that
    page and keeps the one that fills the page best.
  * Pagination is a small DP over the admin's order (order is never changed):
    it minimises unused page area plus a per-page penalty, which is what makes
    "5 images -> 2 pages, 10 images -> 3 pages, 2 images -> 1 page" fall out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Dict, List, Optional, Sequence, Tuple

PAGE_W = 210.0
PAGE_H = 297.0
MARGIN = 14.0
GAP = 4.0
CONTENT_W = PAGE_W - 2 * MARGIN  # 182

# Vertical rhythm (mm)
HEADER_Y = 12.0
NAME_Y = 30.0
RULE_Y = 49.0
BODY_TOP = 54.0
FOOTER_Y = 284.0
GALLERY_TOP = 30.0
GALLERY_BOTTOM = 276.0

MAX_PER_GALLERY_PAGE = 4
PAGE_PENALTY = 0.5
HERO_MAX_CROP = 0.12
HERO_FOCAL = (0.5, 0.28)
LANDSCAPE_HERO_RATIO = 1.15
DEFAULT_RATIO = 0.8  # 4:5 when an image's size is unknown


@dataclass
class Slot:
    media_id: str
    x: float
    y: float
    w: float
    h: float
    fit: str = "contain"  # "contain" | "cover"
    focal: Tuple[float, float] = (0.5, 0.5)
    crop: Optional[Tuple[float, float, float, float]] = None  # (cx, cy, cw, ch) normalised source rect

    def as_dict(self) -> dict:
        d = {"media_id": self.media_id, "x": round(self.x, 3), "y": round(self.y, 3),
             "w": round(self.w, 3), "h": round(self.h, 3), "fit": self.fit,
             "focal": [round(self.focal[0], 3), round(self.focal[1], 3)]}
        if self.crop:
            d["crop"] = [round(v, 5) for v in self.crop]
        return d


@dataclass
class Page:
    kind: str  # "hero" | "gallery"
    slots: List[Slot] = field(default_factory=list)
    template: str = ""  # hero: "portrait" | "landscape"; informational for web/tests
    fill: float = 0.0
    info: List[dict] = field(default_factory=list)  # placed metadata (x/y/w/lines in mm)
    rule_y: Optional[float] = None
    video_y: Optional[float] = None
    hero_h: Optional[float] = None
    name_pt: Optional[float] = None

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "template": self.template, "fill": round(self.fill, 3),
             "slots": [s.as_dict() for s in self.slots]}
        if self.kind == "info":
            d["name_pt"] = self.name_pt
        if self.kind in ("hero", "info"):
            d["info"] = [{**{k: v for k, v in it.items() if k != "block"},
                          "x": round(it["x"], 3), "y": round(it["y"], 3), "w": round(it["w"], 3)} for it in self.info]
        if self.kind == "hero":
            d["rule_y"] = round(self.rule_y, 3) if self.rule_y is not None else None
            d["video_y"] = round(self.video_y, 3) if self.video_y is not None else None
            d["hero_h"] = round(self.hero_h, 3) if self.hero_h is not None else None
            d["name_pt"] = self.name_pt
        return d


# --------------------------------------------------------------------------
# Slicing-tree fitting
# --------------------------------------------------------------------------
# A node is ("leaf", media_id, ratio) | ("H", left, right) | ("V", top, bottom).
# Every node's width is affine in its height:  w(h) = a*h + b  (gaps make b != 0).
def _ab(node, gap: float) -> Tuple[float, float]:
    kind = node[0]
    if kind == "leaf":
        return node[2], 0.0
    (aA, bA), (aB, bB) = _ab(node[1], gap), _ab(node[2], gap)
    if kind == "H":  # side by side, equal heights
        return aA + aB, bA + bB + gap
    # V: stacked, equal widths W; heights h_X(W) = (W - b_X)/a_X ; sum + gap = H
    inv = 1.0 / aA + 1.0 / aB
    a = 1.0 / inv
    b = (-gap + bA / aA + bB / aB) / inv
    return a, b


def _place(node, x: float, y: float, w: float, h: float, gap: float, out: List[Slot]) -> None:
    kind = node[0]
    if kind == "leaf":
        out.append(Slot(media_id=node[1], x=x, y=y, w=w, h=h))
        return
    left, right = node[1], node[2]
    aA, bA = _ab(left, gap)
    aB, bB = _ab(right, gap)
    if kind == "H":
        wA = aA * h + bA
        wB = aB * h + bB
        _place(left, x, y, wA, h, gap, out)
        _place(right, x + wA + gap, y, wB, h, gap, out)
    else:
        hA = (w - bA) / aA
        hB = (w - bB) / aB
        _place(left, x, y, w, hA, gap, out)
        _place(right, x, y + hA + gap, w, hB, gap, out)


def _trees(items: Sequence[Tuple[str, float]]):
    """All slicing trees over `items` keeping their order."""
    if len(items) == 1:
        yield ("leaf", items[0][0], items[0][1])
        return
    for i in range(1, len(items)):
        for l in _trees(items[:i]):
            for r in _trees(items[i:]):
                yield ("H", l, r)
                yield ("V", l, r)


def fit_images(
    items: Sequence[Tuple[str, float]], box_x: float, box_y: float, box_w: float, box_h: float,
    gap: float = GAP, valign: str = "center", halign: str = "center",
) -> Tuple[List[Slot], float]:
    """Best slicing layout of `items` [(media_id, ratio w/h)] inside the box.
    Returns (slots, fill) where fill = placed image area / box area. Every slot
    has exactly its image's ratio (no stretch, no crop)."""
    best: Tuple[float, int, List[Slot]] = (-1.0, 99, [])
    for tree in _trees(list(items)):
        a, b = _ab(tree, gap)
        if a <= 0:
            continue
        h = box_h
        w = a * h + b
        if w > box_w:
            h = (box_w - b) / a
            w = box_w
        if h <= 0:
            continue
        slots: List[Slot] = []
        ox = box_x + ((box_w - w) / 2.0 if halign == "center" else 0.0)
        oy = box_y + ((box_h - h) / 2.0 if valign == "center" else 0.0)
        _place(tree, ox, oy, w, h, gap, slots)
        area = sum(s.w * s.h for s in slots)
        fill = area / (box_w * box_h)
        nodes = len(slots)
        if fill > best[0] + 1e-9:
            best = (fill, nodes, slots)
    return best[2], max(best[0], 0.0)


# --------------------------------------------------------------------------
# Pagination of gallery pages
# --------------------------------------------------------------------------
def paginate_gallery(items: Sequence[Tuple[str, float]]) -> List[Page]:
    """Split `items` (admin order preserved) into gallery pages of 1..4 images
    minimising  sum(1 - fill) + PAGE_PENALTY * pages  via DP."""
    n = len(items)
    if n == 0:
        return []
    box = (MARGIN, GALLERY_TOP, CONTENT_W, GALLERY_BOTTOM - GALLERY_TOP)
    cache: Dict[Tuple[int, int], Tuple[List[Slot], float]] = {}

    def group(i: int, j: int):
        if (i, j) not in cache:
            cache[(i, j)] = fit_images(items[i:j], *box)
        return cache[(i, j)]

    INF = float("inf")
    cost = [INF] * (n + 1)
    prev = [0] * (n + 1)
    cost[0] = 0.0
    for j in range(1, n + 1):
        for k in range(1, MAX_PER_GALLERY_PAGE + 1):
            i = j - k
            if i < 0 or cost[i] == INF:
                continue
            _, fill = group(i, j)
            c = cost[i] + (1.0 - fill) + PAGE_PENALTY
            if c < cost[j] - 1e-9:
                cost[j], prev[j] = c, i
    pages: List[Page] = []
    j = n
    while j > 0:
        i = prev[j]
        slots, fill = group(i, j)
        pages.append(Page(kind="gallery", slots=slots, fill=fill, template=f"gallery-{j - i}"))
        j = i
    pages.reverse()
    return pages


# --------------------------------------------------------------------------
# Information area (talent metadata) — wrapped with the real font metrics
# --------------------------------------------------------------------------
import os
from functools import lru_cache

_FONT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "Manrope-Regular.ttf"))
PT_MM = 0.352778
VALUE_PT = 10.5
VALUE_LINE_H = 4.8
LABEL_TO_VALUE = 5.2
ROW_GAP = 5.0
INFO_ROW_CAP = 5.0          # total column weight that fits one row
INFO_ROW_MIN_TOTAL = 4.65   # sparse rows keep this scale (never stretch to full width)
INFO_WEIGHTS = {"age": 0.55, "height": 0.7, "gender": 0.75, "ethnicity": 1.1,
                "instagram_followers": 1.1, "location": 1.9, "instagram": 1.5}
BUTTON_H = 9.0
INFO_LIMIT_Y = 272.0
HERO_BASE_H = 176.0
HERO_MIN_H = 128.0
INFO_PAGE_TOP = 58.0
INFO_PAGE_BOTTOM = 270.0


@lru_cache(maxsize=1)
def _font():
    from PIL import ImageFont
    return ImageFont.truetype(_FONT_PATH, 1000)


def text_width_mm(text: str, size_pt: float = VALUE_PT) -> float:
    return _font().getlength(text) / 1000.0 * size_pt * PT_MM


NAME_BASE_PT = 28.0
NAME_MIN_PT = 14.0


def name_pt(name: str, base: float = NAME_BASE_PT) -> float:
    """Headline size for a talent name: 28pt, stepped down (never below 14pt) until the
    upper-cased, tracked name fits the content width. SemiBold is a little wider than the
    Regular metrics measured here, hence the 1.08 allowance. Both the web page and the PDF
    use this, so a very long name can never run off the page."""
    up = (name or "").upper()
    size = base
    while size > NAME_MIN_PT and text_width_mm(up, size) * 1.08 + 0.352778 * 1.0 * len(up) > CONTENT_W:
        size -= 1.0
    return size


def wrap_lines(text: str, max_w: float, max_lines: int, size_pt: float = VALUE_PT) -> List[str]:
    """Greedy word wrap using Manrope metrics; the last allowed line is
    ellipsised when the text does not fit. Never returns an empty list."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_width_mm(trial, size_pt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1] + " …"
        while len(last) > 2 and text_width_mm(last, size_pt) > max_w:
            last = last[:-3].rstrip() + " …"
        lines[-1] = last
    out = []
    for ln in lines:  # a single unbreakable word wider than the column
        while text_width_mm(ln, size_pt) > max_w and len(ln) > 1:
            ln = ln[:-2].rstrip() + "…"
        out.append(ln)
    return out or [""]


def _needs_full_row(it: dict) -> bool:
    """A short-field value that cannot be shown IN FULL in its normal column within two
    lines (e.g. a 30-character handle, a four-city location) gets its own full-width row
    instead of being ellipsised. Information is never cut."""
    w = INFO_WEIGHTS.get(it["key"], 1.0) * (CONTENT_W / INFO_ROW_MIN_TOTAL) - 5.0
    words = it["value"].split()
    if any(text_width_mm(wd) > w for wd in words):
        return True
    lines, cur = 0, ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if text_width_mm(trial) <= w or not cur:
            cur = trial
        else:
            lines += 1
            cur = wd
    lines += 1 if cur else 0
    return lines > 2


def place_info(items: Sequence[dict], y0: float, max_lines: int = 2, block_lines: int = 6,
               left: float = MARGIN, width: float = CONTENT_W) -> Tuple[List[dict], float]:
    """Pack info items into rows starting at y0. Short items share a row (by
    column weight); `block` items (skills, languages) take a full-width row.
    Returns (placed items with x/y/w/lines, visual bottom of the last row)."""
    placed: List[dict] = []
    y = y0
    bottom = y0
    row: List[dict] = []
    row_total = 0.0

    def flush():
        nonlocal y, bottom, row, row_total
        if not row:
            return
        scale = width / max(row_total, INFO_ROW_MIN_TOTAL)
        x = left
        row_lines = 1
        cells = []
        for it in row:
            w = INFO_WEIGHTS.get(it["key"], 1.0) * scale
            lines = wrap_lines(it["value"], w - 5.0, max_lines)
            row_lines = max(row_lines, len(lines))
            cells.append((it, x, w, lines))
            x += w
        for it, cx, w, lines in cells:
            placed.append({**it, "x": cx, "y": y, "w": w, "lines": lines})
        bottom = y + LABEL_TO_VALUE + VALUE_LINE_H * row_lines
        y += LABEL_TO_VALUE + VALUE_LINE_H * row_lines + ROW_GAP
        row, row_total = [], 0.0

    for it in items:
        if not it.get("block") and _needs_full_row(it):
            it = {**it, "block": True}
        if it.get("block"):
            flush()
            lines = wrap_lines(it["value"], width - 5.0, block_lines)
            placed.append({**it, "x": left, "y": y, "w": width, "lines": lines})
            bottom = y + LABEL_TO_VALUE + VALUE_LINE_H * len(lines)
            y += LABEL_TO_VALUE + VALUE_LINE_H * len(lines) + ROW_GAP
            continue
        wgt = INFO_WEIGHTS.get(it["key"], 1.0)
        if row and row_total + wgt > INFO_ROW_CAP:
            flush()
        row.append(it)
        row_total += wgt
    flush()
    return placed, bottom


# --------------------------------------------------------------------------
# Hero / comp-card page
# --------------------------------------------------------------------------
def _hero_slot(media_id: str, ratio: float, box: Tuple[float, float, float, float]) -> Slot:
    """Place the hero in `box`. Crops only if that removes (nearly) all letter-
    boxing AND needs <= HERO_MAX_CROP on the cropped axis; otherwise `contain`."""
    bx, by, bw, bh = box
    box_ratio = bw / bh
    if abs(ratio - box_ratio) / box_ratio > 1e-6:
        if ratio > box_ratio:  # image wider than box: would crop width
            keep = box_ratio / ratio  # fraction of width kept
            if 1.0 - keep <= HERO_MAX_CROP:
                fx = HERO_FOCAL[0]
                cx = min(max(fx - keep / 2.0, 0.0), 1.0 - keep)
                return Slot(media_id, bx, by, bw, bh, fit="cover", focal=HERO_FOCAL, crop=(cx, 0.0, keep, 1.0))
        else:  # taller than box: would crop height
            keep = ratio / box_ratio
            if 1.0 - keep <= HERO_MAX_CROP:
                fy = HERO_FOCAL[1]
                cy = min(max(fy - keep / 2.0, 0.0), 1.0 - keep)
                return Slot(media_id, bx, by, bw, bh, fit="cover", focal=HERO_FOCAL, crop=(0.0, cy, 1.0, keep))
    else:
        return Slot(media_id, bx, by, bw, bh, fit="cover", focal=HERO_FOCAL, crop=(0.0, 0.0, 1.0, 1.0))
    # contain, centred
    if ratio >= box_ratio:
        w, h = bw, bw / ratio
    else:
        w, h = bh * ratio, bh
    return Slot(media_id, bx + (bw - w) / 2.0, by + (bh - h) / 2.0, w, h, fit="contain")


# Hero page geometry (mm). Heights depend on `hero_h` (default HERO_BASE_H); the
# info area below it grows by shrinking the hero, never by crushing the type.
def _hero_boxes(hero_h: float, supports: bool, landscape: bool, hero_ratio: float = 0.72):
    if landscape:
        # landscape heroes scale with hero_h but keep the supports strip
        top = hero_h * (114.0 / HERO_BASE_H)
        strip = hero_h - top - GAP
        return ((MARGIN, BODY_TOP, CONTENT_W, top),
                (MARGIN, BODY_TOP + top + GAP, CONTENT_W, max(strip, 30.0)))
    # Default geometry: 120mm hero column. When the info area forces a shorter hero,
    # the column narrows to the photo's own width (no letter-box gutter) and the
    # supporting images get the freed space.
    hero_w = 120.0 if hero_h >= HERO_BASE_H - 1e-6 else min(120.0, max(hero_h * hero_ratio, 80.0))
    return ((MARGIN, BODY_TOP, hero_w if supports else CONTENT_W, hero_h),
            (MARGIN + hero_w + GAP, BODY_TOP, CONTENT_W - hero_w - GAP, hero_h))


HERO_PORTRAIT_BOX = (MARGIN, BODY_TOP, 120.0, HERO_BASE_H)  # default-geometry reference (tests / docs)


HERO_COMFORT_H = 150.0  # below this, long text blocks move to an information page instead of shrinking the hero further


def _fit_info(main: List[dict], has_video: bool, min_h: float):
    """Try to fit `main` under the hero, shrinking the hero down to `min_h`.
    Returns (fit_ok, hero_h, placed, rule_y, video_y)."""
    hero_h = HERO_BASE_H
    placed, rule_y, video_y = [], BODY_TOP + hero_h + 6.0, None
    for _ in range(40):
        rule_y = BODY_TOP + hero_h + 6.0
        y0 = rule_y + 4.0
        placed, rows_bottom = place_info(main, y0)
        if main:
            # legacy position (rule + 23) when short; pushed down only if the rows need it
            video_y = max(y0 + 19.0, rows_bottom + 4.2) if has_video else None
        else:
            video_y = y0 if has_video else None
        total_bottom = (video_y + BUTTON_H) if has_video else (rows_bottom if main else y0)
        over = total_bottom - INFO_LIMIT_Y
        if over <= 1e-6:
            return True, hero_h, placed, rule_y, video_y
        if hero_h - over >= min_h:
            hero_h -= over
            continue
        return False, hero_h, placed, rule_y, video_y
    return False, hero_h, placed, rule_y, video_y


def _plan_info(info_items: Sequence[dict], has_video: bool):
    """Choose hero height + which info items stay on the hero page. Returns
    (hero_h, placed_main, rule_y, video_y, overflow_items). Preference order:
    keep everything under the hero (shrinking it up to the comfort limit) ->
    move the long text blocks (skills/languages) to an information page ->
    shrink the hero further -> spill the trailing items."""
    main = list(info_items)
    ok, hero_h, placed, rule_y, video_y = _fit_info(main, has_video, HERO_COMFORT_H)
    if ok:
        return hero_h, placed, rule_y, video_y, []
    blocks = [it for it in main if it.get("block")]
    if blocks:
        main = [it for it in main if not it.get("block")]
        overflow = blocks
    else:
        overflow = []
    while True:
        ok, hero_h, placed, rule_y, video_y = _fit_info(main, has_video, HERO_MIN_H)
        if ok or not main:
            return hero_h, placed, rule_y, video_y, overflow
        overflow.insert(0, main.pop())


def plan_hero_page(items: Sequence[Tuple[str, float]], info_items: Sequence[dict] = (), has_video: bool = False,
                   ) -> Tuple[Page, List[Tuple[str, float]], List[dict]]:
    """Hero page = first image (hero) + up to two supporting images + the info
    area. Returns the page, the images that did not fit (for gallery pages) and
    the info items that did not fit (for an information page)."""
    hero_h, placed, rule_y, video_y, overflow = _plan_info(info_items, has_video)
    hero_id, hero_ratio = items[0] if items else (None, 1.0)
    supports = list(items[1:3])
    rest = list(items[3:])
    landscape = hero_ratio >= LANDSCAPE_HERO_RATIO
    slots: List[Slot] = []
    template = "landscape" if landscape else "portrait"
    if hero_id is not None:
        hero_box, sup_box = _hero_boxes(hero_h, bool(supports), landscape, hero_ratio)
        slots.append(_hero_slot(hero_id, hero_ratio, hero_box))
        if supports:
            s_, _ = fit_images(supports, *sup_box, valign="top",
                               halign="center" if hero_h >= HERO_BASE_H - 1e-6 else "left")
            slots.extend(s_)
    area = sum(s.w * s.h for s in slots)
    page = Page(kind="hero", slots=slots, template=template, fill=area / (CONTENT_W * hero_h))
    page.info = placed
    page.rule_y = rule_y
    page.video_y = video_y
    page.hero_h = hero_h
    return page, rest, overflow


def info_y(template: str) -> float:  # legacy accessor (default geometry)
    return BODY_TOP + HERO_BASE_H + 6.0 + 4.0


def plan_info_pages(overflow: Sequence[dict], page_name: str = "") -> List[Page]:
    """Clean information page(s) for the fields that did not fit under the hero."""
    pages: List[Page] = []
    items = list(overflow)
    while items:
        chunk: List[dict] = []
        placed: List[dict] = []
        for it in items:
            trial, bottom = place_info(chunk + [it], INFO_PAGE_TOP, max_lines=3, block_lines=14)
            if bottom > INFO_PAGE_BOTTOM and chunk:
                break
            chunk.append(it)
            placed = trial
        items = items[len(chunk):]
        pg = Page(kind="info", slots=[], template="info")
        pg.info = placed
        pg.name_pt = name_pt(page_name, 20.0) if page_name else 20.0
        pages.append(pg)
    return pages


def plan_talent_pages(items: Sequence[Tuple[str, float]], info_items: Sequence[dict] = (),
                      has_video: bool = False, name: str = "") -> List[Page]:
    """items = [(media_id, ratio)] in the admin's order; the first is the hero.
    A talent with no images still gets a (text-only) hero page."""
    hero, rest, overflow = plan_hero_page(items, info_items, has_video)
    hero.name_pt = name_pt(name) if name else NAME_BASE_PT
    pages = [hero]
    pages += plan_info_pages(overflow, name)
    pages += paginate_gallery(rest)
    return pages


def safe_ratio(w: Optional[float], h: Optional[float]) -> float:
    try:
        if w and h and w > 0 and h > 0:
            r = float(w) / float(h)
            return min(max(r, 0.2), 5.0)
    except Exception:
        pass
    return DEFAULT_RATIO
