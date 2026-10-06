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

    def as_dict(self) -> dict:
        return {"kind": self.kind, "template": self.template, "fill": round(self.fill, 3),
                "slots": [s.as_dict() for s in self.slots]}


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
    gap: float = GAP, valign: str = "center",
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
        ox = box_x + (box_w - w) / 2.0
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


# Hero page geometry (mm)
HERO_PORTRAIT_BOX = (MARGIN, BODY_TOP, 120.0, 176.0)
HERO_PORTRAIT_SUPPORT_BOX = (MARGIN + 120.0 + GAP, BODY_TOP, CONTENT_W - 120.0 - GAP, 176.0)
HERO_FULL_BOX = (MARGIN, BODY_TOP, CONTENT_W, 176.0)
HERO_LANDSCAPE_BOX = (MARGIN, BODY_TOP, CONTENT_W, 114.0)
HERO_LANDSCAPE_SUPPORT_BOX = (MARGIN, BODY_TOP + 114.0 + GAP, CONTENT_W, 58.0)
INFO_Y_PORTRAIT = 240.0
INFO_Y_LANDSCAPE = 238.0


def plan_hero_page(items: Sequence[Tuple[str, float]]) -> Tuple[Page, List[Tuple[str, float]]]:
    """Hero page = first image (hero) + up to two supporting images. Returns the
    page and the images that did not fit (for gallery pages)."""
    hero_id, hero_ratio = items[0]
    supports = list(items[1:3])
    rest = list(items[3:])
    landscape = hero_ratio >= LANDSCAPE_HERO_RATIO
    slots: List[Slot] = []
    if landscape:
        slots.append(_hero_slot(hero_id, hero_ratio, HERO_LANDSCAPE_BOX))
        if supports:
            s, _ = fit_images(supports, *HERO_LANDSCAPE_SUPPORT_BOX, valign="top")
            slots.extend(s)
        template = "landscape"
    else:
        hero_box = HERO_PORTRAIT_BOX if supports else HERO_FULL_BOX
        slots.append(_hero_slot(hero_id, hero_ratio, hero_box))
        if supports:
            s, _ = fit_images(supports, *HERO_PORTRAIT_SUPPORT_BOX, valign="top")
            slots.extend(s)
        template = "portrait"
    area = sum(s.w * s.h for s in slots)
    return Page(kind="hero", slots=slots, template=template, fill=area / (CONTENT_W * 176.0)), rest


def info_y(template: str) -> float:
    return INFO_Y_LANDSCAPE if template == "landscape" else INFO_Y_PORTRAIT


def plan_talent_pages(items: Sequence[Tuple[str, float]]) -> List[Page]:
    """items = [(media_id, ratio)] in the admin's order; the first is the hero.
    A talent with no images still gets a (text-only) hero page."""
    if not items:
        return [Page(kind="hero", slots=[], template="portrait")]
    hero, rest = plan_hero_page(items)
    return [hero] + paginate_gallery(rest)


def safe_ratio(w: Optional[float], h: Optional[float]) -> float:
    try:
        if w and h and w > 0 and h > 0:
            r = float(w) / float(h)
            return min(max(r, 0.2), 5.0)
    except Exception:
        pass
    return DEFAULT_RATIO
