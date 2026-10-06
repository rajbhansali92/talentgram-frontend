"""Roster field visibility + cover refinement.

Reuses test_roster's harness (real Mongo, in-process ASGI app, local image CDN).
"""
import io
import os
import re

import pytest
from pypdf import PdfReader

from routers import roster as R
from services import roster_fields as F
from services import roster_layout as L
from services import roster_pdf

from tests.test_roster import (  # noqa: F401  (fixtures + helpers)
    _create, _default_sel, _m, _payload, _talent, _viewer, cdn, env,
)

pytestmark = pytest.mark.asyncio

RICH = dict(
    ethnicity="indian", gender="female", instagram_followers="50K+",
    skills=["Actor", "Model", "Hip Hop", "Yoga", "English", "Hindi"],
)
ALL_ON = {k: True for k in F.FIELD_KEYS}


def _text(pdf_bytes):
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


def _uris(pdf_bytes):
    out = []
    for p in PdfReader(io.BytesIO(pdf_bytes)).pages:
        for a in p.get("/Annots") or []:
            a = a.get_object()
            if a.get("/A") and a["/A"].get("/URI"):
                out.append(str(a["/A"]["/URI"]))
    return out


async def _roster(http, tdb, cdn, talents, fields="UNSET", title="Talentgram X BKB"):
    sels = [_default_sel(t) for t in talents]
    payload = _payload(title, sels)
    if fields != "UNSET":
        payload["roster"]["fields"] = fields
    r = await http.post("/api/links", json=payload)
    assert r.status_code == 200, r.text
    link = r.json()
    return link, await _viewer(http, link["slug"])


async def _view(http, link, h):
    return (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()


async def _pdf(http, link, h):
    r = await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)
    assert r.status_code == 200
    return r.content


def _info(view, i=0):
    return {x["label"]: x["value"] for x in view["talents"][i]["info"]}


# ----------------------------------------------------------------- registry
async def test_registry_exposes_only_real_public_fields_in_groups(env):
    tdb, http = env
    reg = (await http.get("/api/roster/fields")).json()
    keys = [f["key"] for g in reg["groups"] for f in g["fields"]]
    assert [g["key"] for g in reg["groups"]] == ["basic", "social", "professional", "media"]
    assert keys == ["name", "age", "height", "location", "gender", "ethnicity", "instagram",
                    "instagram_followers", "skills", "languages", "intro_video"]
    assert reg["defaults"] == {"name": True, "age": True, "height": True, "location": True, "gender": False,
                               "ethnicity": False, "instagram": True, "instagram_followers": False,
                               "skills": False, "languages": False, "intro_video": True}
    for private in ("phone", "email", "notes", "id", "availability", "budget", "portal_access_token", "whatsapp_group_name"):
        assert private not in keys


async def test_unknown_or_private_field_keys_are_ignored_never_rendered(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link, h = await _roster(http, tdb, cdn, [t], fields={**F.DEFAULT_FIELDS, "phone": True, "email": True, "notes": True, "age": "yes"})
    stored = (await tdb.links.find_one({"id": link["id"]}))["roster"]["fields"]
    assert set(stored) == set(F.FIELD_KEYS) and stored["age"] is True  # non-bool falls back to default
    body = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).text
    for secret in ("private@example.com", "+919999999999", "INTERNAL NOTE"):
        assert secret not in body


# ----------------------------------------------------------------- defaults
async def test_new_roster_defaults_to_the_standard_fields(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **RICH)
    link, h = await _roster(http, tdb, cdn, [t])  # no fields sent
    stored = (await tdb.links.find_one({"id": link["id"]}))["roster"]["fields"]
    assert stored == F.DEFAULT_FIELDS
    assert [k for k, v in stored.items() if v] == ["name", "age", "height", "location", "instagram", "intro_video"]
    view = await _view(http, link, h)
    assert view["fields"] == F.DEFAULT_FIELDS
    assert _info(view) == {"Age": "25", "Height": "5'7\"", "Location": "Mumbai, India", "Instagram": "@ak_official"}
    assert view["talents"][0]["video"] is not None and view["talents"][0]["name"] == "Angela K"


# ------------------------------------------------------------------ toggles
async def test_instagram_off_removes_it_from_web_and_pdf_and_on_restores_it(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link, h = await _roster(http, tdb, cdn, [t], fields={**F.DEFAULT_FIELDS, "instagram": False})
    view = await _view(http, link, h)
    assert "Instagram" not in _info(view)
    pdf = await _pdf(http, link, h)
    assert not any("instagram.com/ak_official" in u for u in _uris(pdf))
    assert "@ak_official" not in _text(pdf)

    up = await http.put(f"/api/links/{link['id']}", json={**_payload("Talentgram X BKB", [_default_sel(t)]),
                                                         "roster": {"subtitle": None, "talents": [_default_sel(t)], "fields": F.DEFAULT_FIELDS}})
    assert up.status_code == 200
    view = await _view(http, link, h)
    assert _info(view)["Instagram"] == "@ak_official"
    pdf = await _pdf(http, link, h)
    assert "@ak_official" in _text(pdf) and any("instagram.com/ak_official" in u for u in _uris(pdf))


async def test_name_off_uses_numbered_labels_and_never_the_real_name(env, cdn):
    tdb, http = env
    a, b = await _talent(tdb, cdn, name="Angela Kumar"), await _talent(tdb, cdn, name="Jasmine Singh")
    link, h = await _roster(http, tdb, cdn, [a, b], fields={**F.DEFAULT_FIELDS, "name": False})
    view = await _view(http, link, h)
    assert [t["name"] for t in view["talents"]] == ["Talent 01", "Talent 02"]
    pdf = _text(await _pdf(http, link, h))
    assert "TALENT 01" in pdf and "ANGELA" not in pdf and "JASMINE" not in pdf


async def test_intro_video_toggle_off_removes_the_video_everywhere(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link, h = await _roster(http, tdb, cdn, [t], fields={**F.DEFAULT_FIELDS, "intro_video": False})
    assert (await _view(http, link, h))["talents"][0]["video"] is None
    pdf = await _pdf(http, link, h)
    assert "INTRODUCTION VIDEO" not in _text(pdf) and not any("cloudinary" in u for u in _uris(pdf))


# ------------------------------------------------------- additional fields
async def test_additional_fields_render_with_professional_labels_and_values(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **RICH)
    link, h = await _roster(http, tdb, cdn, [t], fields=ALL_ON)
    info = _info(await _view(http, link, h))
    assert info == {"Age": "25", "Height": "5'7\"", "Location": "Mumbai, India", "Gender": "Female", "Ethnicity": "Indian",
                    "Instagram": "@ak_official", "Followers": "50K+",
                    "Skills": "Actor, Model, Hip Hop, Yoga", "Languages": "English, Hindi"}
    text = _text(await _pdf(http, link, h))
    for expect in ("GENDER", "Female", "ETHNICITY", "Indian", "FOLLOWERS", "50K+", "SKILLS", "Actor, Model, Hip Hop, Yoga",
                   "LANGUAGES", "English, Hindi"):
        assert expect in text, expect


async def test_field_order_is_professional_not_alphabetical(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **RICH)
    link, h = await _roster(http, tdb, cdn, [t], fields=ALL_ON)
    order = [i["key"] for i in (await _view(http, link, h))["talents"][0]["info"]]
    assert order == ["age", "height", "location", "gender", "ethnicity", "instagram", "instagram_followers", "skills", "languages"]


async def test_empty_values_are_omitted_with_no_placeholder_text(env, cdn):
    tdb, http = env
    full = await _talent(tdb, cdn, name="Angela Kumar", **RICH)
    sparse = await _talent(tdb, cdn, name="Jasmine Singh", age=None, height=None, location=[], instagram_handle=None,
                           ethnicity=None, gender="", skills=[], instagram_followers="")
    partial = await _talent(tdb, cdn, name="Fanny Gandhi", ethnicity="", instagram_followers=None, skills=["English"], gender="prefer_not_say")
    link, h = await _roster(http, tdb, cdn, [full, sparse, partial], fields=ALL_ON)
    view = await _view(http, link, h)
    assert _info(view, 1) == {}
    assert _info(view, 2) == {"Age": "25", "Height": "5'7\"", "Location": "Mumbai, India", "Instagram": "@ak_official", "Languages": "English"}
    text = _text(await _pdf(http, link, h))
    for bad in ("N/A", "undefined", "None", "null", "Location —", "Age -"):
        assert bad not in text, bad
    # the sparse talent's page still renders (image + name), no empty labels directly followed by another label
    assert "JASMINE S" in text


async def test_language_taxonomy_matches_the_frontend_skills_selector():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "components", "SkillsSelector.jsx")).read()
    block = re.search(r'"Languages":\s*\[(.*?)\]', src, re.S).group(1)
    assert re.findall(r'"([^"]+)"', block) == F.LANGUAGE_SKILLS


# ---------------------------------------------------- existing rosters / edit
async def test_existing_roster_without_stored_fields_renders_exactly_as_before(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **RICH)
    link, h = await _roster(http, tdb, cdn, [t])
    await tdb.links.update_one({"id": link["id"]}, {"$unset": {"roster.fields": ""}})  # a pre-feature roster
    assert "fields" not in (await tdb.links.find_one({"id": link["id"]}))["roster"]
    view = await _view(http, link, h)
    assert _info(view) == {"Age": "25", "Height": "5'7\"", "Location": "Mumbai, India", "Instagram": "@ak_official"}
    page = view["talents"][0]["pages"][0]
    assert (page["hero_h"], page["rule_y"], page["video_y"]) == (176.0, 236.0, 259.0)  # legacy geometry
    assert [(i["key"], round(i["x"], 1), round(i["w"], 1), i["y"]) for i in page["info"]] == [
        ("age", 14.0, 21.5, 240.0), ("height", 35.5, 27.4, 240.0), ("location", 62.9, 74.4, 240.0), ("instagram", 137.3, 58.7, 240.0)]
    assert len(PdfReader(io.BytesIO(await _pdf(http, link, h))).pages) >= 2
    # rendering never rewrites the stored roster
    assert "fields" not in (await tdb.links.find_one({"id": link["id"]}))["roster"]


async def test_edit_changes_fields_for_web_and_pdf_and_save_without_fields_keeps_them(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **RICH)
    link, h = await _roster(http, tdb, cdn, [t])
    before = await _pdf(http, link, h)
    sel = _default_sel(t)
    up = await http.put(f"/api/links/{link['id']}", json=_payload("Talentgram X BKB", [sel]) | {
        "roster": {"subtitle": None, "talents": [sel], "fields": {**F.DEFAULT_FIELDS, "ethnicity": True, "age": False}}})
    assert up.status_code == 200 and up.json()["slug"] == link["slug"]  # same canonical link
    view = await _view(http, link, h)
    info = _info(view)
    assert info["Ethnicity"] == "Indian" and "Age" not in info
    after = await _pdf(http, link, h)
    assert after != before and "Indian" in _text(after) and "AGE" not in _text(after).split("ANGELA K")[1]
    # a later edit that sends no `fields` keeps the stored configuration
    up2 = await http.put(f"/api/links/{link['id']}", json=_payload("Talentgram X BKB v2", [sel]))
    assert up2.status_code == 200
    stored = (await tdb.links.find_one({"id": link["id"]}))["roster"]["fields"]
    assert stored["ethnicity"] is True and stored["age"] is False
    # the roster setting never touches the talent's profile
    prof = await tdb.talents.find_one({"id": t["id"]})
    assert prof["age"] == 25 and prof["ethnicity"] == "indian" and "fields" not in prof


# ----------------------------------------------------------------- name rule
async def test_name_format_unchanged_with_any_field_selection(env, cdn):
    tdb, http = env
    ts = [await _talent(tdb, cdn, name=n, **RICH) for n in ("Angela Kumar", "Jasmine Singh", "Fanny Gandhi")]
    link, h = await _roster(http, tdb, cdn, ts, fields=ALL_ON)
    view = await _view(http, link, h)
    assert [t["name"] for t in view["talents"]] == ["Angela K", "Jasmine S", "Fanny G"]
    text = _text(await _pdf(http, link, h)).upper()
    assert "ANGELA K" in text and "FANNY G" in text and "JASMINE S" in text
    for surname in ("KUMAR", "SINGH", "GANDHI"):
        assert surname not in text


# ------------------------------------------------------------------ layout
def _rect_ok(placed):
    for i, a in enumerate(placed):
        assert L.MARGIN - 1e-6 <= a["x"] and a["x"] + a["w"] <= L.PAGE_W - L.MARGIN + 1e-6, "outside margins"
        for b in placed[i + 1:]:
            ha = L.LABEL_TO_VALUE + L.VALUE_LINE_H * len(a["lines"])
            hb = L.LABEL_TO_VALUE + L.VALUE_LINE_H * len(b["lines"])
            sep = a["x"] + a["w"] <= b["x"] + 1e-6 or b["x"] + b["w"] <= a["x"] + 1e-6 or a["y"] + ha <= b["y"] + 1e-6 or b["y"] + hb <= a["y"] + 1e-6
            assert sep, f"overlap {a['key']} / {b['key']}"


LONG_SKILLS = ", ".join(["Actor", "Model", "Dancer", "Hip Hop", "Bollywood", "Contemporary", "Bharatanatyam", "Kathak", "Salsa",
                         "Ballet", "Singer", "Piano", "Keyboard", "Guitar", "Violin", "Drums", "Flute", "Ukulele", "DJ", "Beatboxing",
                         "Rapper", "Composer", "Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing", "Kickboxing"])


def _items(n):
    base = [("age", "25"), ("height", "5'7\""), ("location", "Mumbai, India, Los Angeles, United States"), ("gender", "Female"),
            ("ethnicity", "South Asian"), ("instagram", "@some_long_instagram_handle"), ("instagram_followers", "500K+"),
            ("skills", LONG_SKILLS), ("languages", "English, Hindi, Marathi, Gujarati, Tamil")]
    return [{"key": k, "label": k.title(), "value": v, **({"block": True} if k in F.BLOCK_KEYS else {})} for k, v in base[:n]]


@pytest.mark.parametrize("n", [0, 1, 4, 5, 7, 9])
@pytest.mark.parametrize("video", [True, False])
async def test_any_amount_of_metadata_never_overlaps_overflows_or_crushes_the_type(n, video):
    pages = L.plan_talent_pages([("a", 0.75), ("b", 0.8), ("c", 0.7), ("d", 0.7)], _items(n), video)
    shown = sum(len(p.info) for p in pages if p.kind in ("hero", "info"))
    assert shown == n, "no field is ever dropped"
    for p in pages:
        if p.kind in ("hero", "info"):
            _rect_ok(p.info)
            for it in p.info:
                assert it["y"] + L.LABEL_TO_VALUE + L.VALUE_LINE_H * len(it["lines"]) <= L.INFO_LIMIT_Y + 1e-6 or p.kind == "info"
                assert L.VALUE_PT == 10.5, "font size is never reduced to fit"
        if p.kind == "hero":
            assert p.hero_h >= L.HERO_MIN_H - 1e-6
            if p.video_y is not None:
                assert p.video_y + L.BUTTON_H <= L.INFO_LIMIT_Y + 1e-6
            for s in p.slots:  # images stay clear of the info area and inside the page
                assert s.y + s.h <= p.rule_y + 1e-6
        if p.kind == "info":
            assert max(it["y"] for it in p.info) <= L.INFO_PAGE_BOTTOM


async def test_default_geometry_is_byte_for_byte_the_legacy_layout():
    pg = L.plan_talent_pages([("a", 0.75), ("b", 0.8), ("c", 0.7)], _items(0)[:0] + [
        {"key": "age", "label": "Age", "value": "25"}, {"key": "height", "label": "Height", "value": "5'7\""},
        {"key": "location", "label": "Location", "value": "Mumbai, India"}, {"key": "instagram", "label": "Instagram", "value": "@x"}], True)[0]
    assert (pg.hero_h, pg.rule_y, pg.video_y) == (176.0, 236.0, 259.0)
    assert L.HERO_PORTRAIT_BOX == (14.0, 54.0, 120.0, 176.0)


async def test_large_field_set_moves_long_text_to_an_information_page_instead_of_shrinking_type():
    pages = L.plan_talent_pages([("a", 0.75), ("b", 0.8), ("c", 0.7)], _items(9), True)
    kinds = [p.kind for p in pages]
    assert kinds[:2] == ["hero", "info"]
    assert {i["key"] for i in pages[1].info} == {"skills", "languages"}
    assert pages[0].hero_h >= L.HERO_COMFORT_H
    skills = next(i for i in pages[1].info if i["key"] == "skills")
    assert len(skills["lines"]) >= 2 and not skills["lines"][-1].endswith("…"), "full skill list, not truncated"


async def test_images_are_never_stretched_when_the_hero_shrinks_for_metadata():
    pages = L.plan_talent_pages([("a", 0.75), ("b", 1.5), ("c", 0.7)], _items(7), True)
    for s in pages[0].slots:
        ratio = {"a": 0.75, "b": 1.5, "c": 0.7}[s.media_id]
        if s.fit == "contain":
            assert abs(s.w / s.h - ratio) < 1e-6
        else:
            cx, cy, cw, ch = s.crop
            assert 1 - cw <= L.HERO_MAX_CROP + 1e-9 and 1 - ch <= L.HERO_MAX_CROP + 1e-9


async def test_large_field_roster_pdf_has_an_info_page_and_stays_valid(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **{**RICH, "skills": [s for s in LONG_SKILLS.split(", ")] + ["English", "Hindi"]})
    link, h = await _roster(http, tdb, cdn, [t], fields=ALL_ON)
    pdf = await _pdf(http, link, h)
    reader = PdfReader(io.BytesIO(pdf))
    web = (await _view(http, link, h))["talents"][0]["pages"]
    assert [p["kind"] for p in web][:2] == ["hero", "info"]
    assert len(reader.pages) == 1 + len(web)  # cover + exactly the planned pages (web == pdf)
    text = _text(pdf)
    assert "Beatboxing" in text and "Kickboxing" in text  # full skills present


# -------------------------------------------------------------------- cover
async def test_pdf_cover_has_larger_logo_title_instagram_and_no_website_footer(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link, h = await _roster(http, tdb, cdn, [t], title="Talentgram X BKB")
    pdf = await _pdf(http, link, h)
    reader = PdfReader(io.BytesIO(pdf))
    cover = reader.pages[0].extract_text()
    assert "TALENTGRAM X BKB" in cover and "@talentgram.agency" in cover
    assert "TALENTGRAMAGENCY" not in cover.upper() and ".COM" not in cover.upper()
    assert roster_pdf.COVER_LOGO_W >= 100 and roster_pdf.COVER_LOGO_W > 66, "logo is much larger than the previous 66mm"
    ann = [a.get_object() for a in reader.pages[0].get("/Annots") or []]
    uris = [str(a["/A"]["/URI"]) for a in ann if a.get("/A") and a["/A"].get("/URI")]
    assert uris == ["https://www.instagram.com/talentgram.agency/"]
    # the whole element (icon + handle) is one clickable rectangle
    rect = [float(v) for v in ann[0]["/Rect"]]
    w_mm = (rect[2] - rect[0]) * 25.4 / 72
    assert w_mm > 30  # icon + gap + "@talentgram.agency"
    assert len(list(reader.pages[0].images)) == 1  # the canonical logo asset, unmodified


async def test_cover_logo_keeps_its_native_aspect_ratio_and_size(env, cdn):
    pypdfium2 = pytest.importorskip("pypdfium2")
    from PIL import Image
    tdb, http = env
    t = await _talent(tdb, cdn)
    link, h = await _roster(http, tdb, cdn, [t])
    doc = pypdfium2.PdfDocument(await _pdf(http, link, h))
    img = doc[0].render(scale=3).to_pil().convert("L")
    w_px, h_px = img.size
    mm = 210.0 / w_px
    # the logo block is everything above the title (title starts at y=168mm)
    top = img.crop((0, 0, w_px, int(168 / 297 * h_px) - 5)).point(lambda v: 255 if v < 200 else 0)
    bx = top.getbbox()
    assert bx, "logo not rendered"
    drawn_w, drawn_h = (bx[2] - bx[0]) * mm, (bx[3] - bx[1]) * mm
    with Image.open(roster_pdf.LOGO_PATH) as asset:
        flat = Image.new("RGBA", asset.size, (255, 255, 255, 255))
        flat.alpha_composite(asset.convert("RGBA"))
        native_ink_box = flat.convert("L").point(lambda v: 255 if v < 200 else 0).getbbox()
    native_ink = (native_ink_box[2] - native_ink_box[0]) / (native_ink_box[3] - native_ink_box[1])
    # the visible mark has the asset's own proportions (not stretched or squashed)
    assert abs(drawn_w / drawn_h - native_ink) / native_ink < 0.04, (drawn_w / drawn_h, native_ink)
    assert drawn_w > 50, f"cover logo mark is only {drawn_w:.0f}mm wide — must be a clear hero (was ~34mm before)"
    assert drawn_w < 0.7 * 210, "but not oversized"


async def test_pdf_has_clickable_video_and_valid_structure_with_many_fields(env, cdn):
    tdb, http = env
    ts = [await _talent(tdb, cdn, name=n, **RICH) for n in ("Angela Kumar", "Jasmine Singh")]
    link, h = await _roster(http, tdb, cdn, ts, fields=ALL_ON)
    pdf = await _pdf(http, link, h)
    assert pdf[:5] == b"%PDF-"
    uris = _uris(pdf)
    assert uris.count("https://res.cloudinary.com/demo/video/upload/dog.mp4") == 2
    assert "https://www.instagram.com/talentgram.agency/" in uris and "https://www.instagram.com/ak_official/" in uris
    reader = PdfReader(io.BytesIO(pdf))
    for page in reader.pages[1:]:
        assert len(list(page.images)) >= 1


async def test_instagram_glyph_is_a_proper_centred_camera_mark(env, cdn):
    """The vector glyph: rounded square, ring centred in it, small dot top-right (pins the
    fpdf2 circle() centre-vs-corner quirk that once misplaced the lens)."""
    pypdfium2 = pytest.importorskip("pypdfium2")
    tdb, http = env
    t = await _talent(tdb, cdn)
    link, h = await _roster(http, tdb, cdn, [t])
    page = pypdfium2.PdfDocument(await _pdf(http, link, h))[0]
    scale = 12
    img = page.render(scale=scale).to_pil().convert("L")
    mm = img.size[0] / 210.0
    # locate the glyph: dark pixels in the footer band, left part (icon), above the handle text
    # icon only: ends ~2.6mm before the handle text begins
    xs = (210 - (roster_pdf.INSTAGRAM_ICON_W + roster_pdf.INSTAGRAM_GAP + roster_pdf.instagram_text_w())) / 2
    band = img.crop((int((xs - 2) * mm), int(268 * mm), int((xs + roster_pdf.INSTAGRAM_ICON_W + 1.0) * mm), int(284 * mm))).point(lambda v: 255 if v < 128 else 0)
    box = band.getbbox()
    assert box
    gw, gh = box[2] - box[0], box[3] - box[1]
    # square (icon only: handle text starts further right, outside this crop)
    size = max(gw, gh)
    assert abs(gw - gh) / size < 0.05
    cx, cy = box[0] + gw / 2, box[1] + gh / 2
    dark = lambda px, py: band.getpixel((int(px), int(py))) == 255
    assert not dark(cx, cy), "lens interior must be empty at the centre"
    ring = size * 0.22 * 1.0  # ring radius ~ 0.22 of the square
    assert dark(cx + ring, cy) and dark(cx - ring, cy) and dark(cx, cy + ring) and dark(cx, cy - ring), "lens ring centred"
    assert dark(cx + size * 0.27, cy - size * 0.27), "dot sits top-right"
    assert not dark(cx - size * 0.33, cy + size * 0.30), "bottom-left interior stays empty (no stray shapes)"


# =====================================================================
# Acceptance review: isolation, registry integrity, consistency, messy data
# =====================================================================
import hashlib
import json as _json


async def _doc_hash(tdb, tid):
    d = await tdb.talents.find_one({"id": tid}, {"_id": 0})
    return hashlib.sha256(_json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


async def test_field_config_is_per_roster_and_never_touches_talents(env, cdn):
    tdb, http = env
    ts = [await _talent(tdb, cdn, name=n, **RICH) for n in ("Angela Kumar", "Jasmine Singh")]
    before = {t["id"]: await _doc_hash(tdb, t["id"]) for t in ts}
    a, ha = await _roster(http, tdb, cdn, ts, fields={**F.DEFAULT_FIELDS, "instagram": False}, title="Roster A")
    b, hb = await _roster(http, tdb, cdn, ts, fields={**F.DEFAULT_FIELDS, "ethnicity": True}, title="Roster B")
    a_doc = await tdb.links.find_one({"id": a["id"]}, {"_id": 0})
    b_doc = await tdb.links.find_one({"id": b["id"]}, {"_id": 0})
    assert a_doc["roster"]["fields"]["instagram"] is False and b_doc["roster"]["fields"]["instagram"] is True
    assert "Ethnicity" not in _info(await _view(http, a, ha)) and "Ethnicity" in _info(await _view(http, b, hb))
    # edit A -> B's persisted document is byte-for-byte unchanged
    b_before = _json.dumps(await tdb.links.find_one({"id": b["id"]}, {"_id": 0}), sort_keys=True, default=str)
    up = await http.put(f"/api/links/{a['id']}", json=_payload("Roster A", [_default_sel(t) for t in ts]) | {
        "roster": {"subtitle": None, "talents": [_default_sel(t) for t in ts], "fields": ALL_ON}})
    assert up.status_code == 200
    assert _json.dumps(await tdb.links.find_one({"id": b["id"]}, {"_id": 0}), sort_keys=True, default=str) == b_before
    assert _info(await _view(http, b, hb)).get("Instagram") == "@ak_official" and "Skills" not in _info(await _view(http, b, hb))
    # full lifecycle (render, PDFs, duplicate, settings toggle, delete) never writes a talent
    await _pdf(http, a, ha); await _pdf(http, b, hb)
    assert (await http.post(f"/api/links/{a['id']}/duplicate")).status_code == 200
    assert (await http.delete(f"/api/links/{b['id']}")).status_code == 200
    for t in ts:
        assert await _doc_hash(tdb, t["id"]) == before[t["id"]], "a talent document was modified"


async def test_existing_roster_survives_every_non_field_operation_without_rewrites(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, **RICH)
    link, h = await _roster(http, tdb, cdn, [t])
    pdf_default = await _pdf(http, link, h)
    await tdb.links.update_one({"id": link["id"]}, {"$unset": {"roster.fields": ""}})   # pre-feature roster
    legacy = _json.dumps(await tdb.links.find_one({"id": link["id"]}, {"_id": 0}), sort_keys=True, default=str)
    # existing public URL + token still work; existing PDF still downloads and is identical to the default design
    assert (await http.get(f"/api/public/links/{link['slug']}/meta")).json()["link_type"] == "roster"
    assert await _pdf(http, link, h) == pdf_default
    # public toggle (settings-only update) never invents a field config
    toggle = {"title": "Talentgram X BKB", "brand_name": None, "talent_ids": [t["id"]], "submission_ids": [], "visibility": {},
              "talent_field_visibility": {}, "auto_pull": False, "auto_project_id": None, "is_public": False,
              "password": None, "notes": None, "client_budget_override": None}
    assert (await http.put(f"/api/links/{link['id']}", json=toggle)).status_code == 200
    after = await tdb.links.find_one({"id": link["id"]}, {"_id": 0})
    assert "fields" not in after["roster"], "settings-only save must not rewrite roster config"
    assert after["roster"]["talents"] == _json.loads(legacy)["roster"]["talents"] and after["slug"] == link["slug"]
    # duplicating an old roster does not silently add config either
    dup = (await http.post(f"/api/links/{link['id']}/duplicate")).json()
    assert "fields" not in dup["roster"]


async def test_registry_is_the_single_source_and_mirrors_the_profile_taxonomies():
    base = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "lib", "talentSchema.js")
    src = open(base).read()
    eth = dict(re.findall(r'\{ key: "([a-z_]+)", label: "([^"]+)" \}', src[src.index("ETHNICITY_OPTIONS"):src.index("FOLLOWER_TIERS")]))
    assert eth == F.ETHNICITY_LABELS
    gen_block = src[src.index("GENDER_OPTIONS"):src.index("ETHNICITY_OPTIONS")]
    gen = dict(re.findall(r'\{ key: "([a-z_]+)", label: "([^"]+)" \}', gen_block))
    assert {k: v for k, v in gen.items() if k != "prefer_not_say"} == F.GENDER_LABELS
    # every selectable field is fetched (projection derived from the registry) and has layout weight/label coverage
    proj = F.projection()
    for key in F.FIELD_KEYS:
        assert all(src_key in proj for src_key in F.SOURCE_KEYS[key]), key
        if key not in ("name", "intro_video"):
            assert key in F.INFO_LABELS, key
    # nothing private is ever projected for the roster
    for private in ("phone", "email", "notes", "portal_access_token", "whatsapp_group_name", "alternate_contact_number", "normalized_email"):
        assert private not in proj
    # the registry only names attributes that exist on the real talent model
    from core import TalentIn
    real = set(TalentIn.model_fields) | {"dob", "media"}
    for key in F.FIELD_KEYS:
        assert set(F.SOURCE_KEYS[key]) <= real | {"status"}, key


async def test_messy_values_never_leave_placeholders_separators_or_blank_labels(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, name="Messy Data", age="", height="   ", location=[{"city": "", "country": ""}],
                      instagram_handle="@", ethnicity="  ", gender="Prefer_Not_Say", instagram_followers="  ",
                      skills=["", "  ", None, "Yoga", "  Actor ", "English", "", "Hindi", 7])
    link, h = await _roster(http, tdb, cdn, [t], fields=ALL_ON)
    items = (await _view(http, link, h))["talents"][0]["info"]
    got = {i["label"]: i["value"] for i in items}
    assert got == {"Skills": "Yoga, Actor", "Languages": "English, Hindi"}, got
    for i in items:
        assert i["value"] == i["value"].strip() and "  " not in i["value"] and not i["value"].startswith(",") and ", ," not in i["value"] and not i["value"].endswith(",")
    text = _text(await _pdf(http, link, h))
    for bad in ("N/A", "undefined", "None", "null", " - ", "—", ", ,"):
        assert bad not in text, repr(bad)


async def test_web_and_pdf_show_exactly_the_same_information(env, cdn):
    tdb, http = env
    ts = [await _talent(tdb, cdn, name="Angela Kumar", **RICH),
          await _talent(tdb, cdn, name="Jasmine Singh", ethnicity="mixed", skills=["Actor", "Hindi"], instagram_handle=None),
          await _talent(tdb, cdn, name="Fanny Gandhi", age=None, height=None)]
    link, h = await _roster(http, tdb, cdn, ts, fields=ALL_ON)
    view = await _view(http, link, h)
    pdf_text = " ".join(_text(await _pdf(http, link, h)).split())
    for t in view["talents"]:
        # desktop pages and the mobile list are fed by the same items
        page_items = [(i["key"], " ".join(i["lines"])) for p in t["pages"] for i in p.get("info", [])]
        flat = [(i["key"], i["value"]) for i in t["info"]]
        assert sorted(page_items) == sorted(flat), t["name"]
        assert (t["video"] is not None) == bool(t["video"])
        for key, value in flat:                         # ...and every one of them is in the PDF
            assert " ".join(value.split()) in pdf_text, (t["name"], key, value)
        assert t["name"].upper() in pdf_text
    # a PDF value that is not in the web payload does not exist either (no extra labels)
    web_labels = {i["label"].upper() for t in view["talents"] for i in t["info"]}
    for label in ("AGE", "HEIGHT", "LOCATION", "GENDER", "ETHNICITY", "INSTAGRAM", "FOLLOWERS", "SKILLS", "LANGUAGES"):
        assert (label in pdf_text) == (label in web_labels), label


async def test_hostile_content_wraps_inside_its_column_and_the_page():
    long_items = [
        {"key": "age", "label": "Age", "value": "25"},
        {"key": "location", "label": "Location", "value": "Mumbai, India, Los Angeles, United States, Dubai, United Arab Emirates, London, United Kingdom"},
        {"key": "instagram", "label": "Instagram", "value": "@abcdefghij_klmnopqrst_uvwxyz.123", "href": "x"},
        {"key": "ethnicity", "label": "Ethnicity", "value": "Middle Eastern"},
        {"key": "skills", "label": "Skills", "value": ", ".join(f"Skill Number {i}" for i in range(40)), "block": True},
        {"key": "languages", "label": "Languages", "value": "Mandarin Chinese, " * 8 + "Other", "block": True},
    ]
    for pages in (L.plan_talent_pages([("a", 0.75)], long_items, True), L.plan_talent_pages([], long_items, False)):
        for p in pages:
            for it in p.info:
                assert it["x"] + it["w"] <= L.PAGE_W - L.MARGIN + 1e-6
                for ln in it["lines"]:
                    assert L.text_width_mm(ln) <= it["w"] - 5.0 + 0.5, (it["key"], ln)  # fits its column (type size unchanged)


async def test_intro_video_never_leaves_an_empty_button_or_dead_link(env, cdn):
    tdb, http = env
    no_video = await _talent(tdb, cdn, name="Angela Kumar", media=[_m(cdn, "indian", "/portrait.jpg", "p1"), _m(cdn, "indian", "/landscape.jpg", "p2")])
    with_video = await _talent(tdb, cdn, name="Jasmine Singh")
    link, h = await _roster(http, tdb, cdn, [no_video, with_video])
    view = await _view(http, link, h)
    assert view["talents"][0]["video"] is None and view["talents"][1]["video"]["url"].endswith("dog.mp4")
    assert view["talents"][0]["pages"][0]["video_y"] is None and view["talents"][1]["pages"][0]["video_y"] is not None
    pdf = await _pdf(http, link, h)
    assert _text(pdf).count("INTRODUCTION VIDEO") == 1
    assert _uris(pdf).count("https://res.cloudinary.com/demo/video/upload/dog.mp4") == 1
    assert not any(u in ("", "None", "null", "undefined") for u in _uris(pdf))


async def test_very_long_names_shrink_in_steps_never_below_14pt_and_fit_the_page(env, cdn):
    assert L.name_pt("Angela K") == 28.0
    long_name = "Maximilian-Alexander Bartholomew-Montgomery F"
    pt = L.name_pt(long_name)
    assert L.NAME_MIN_PT <= pt < 28.0
    assert L.text_width_mm(long_name.upper(), pt) <= L.CONTENT_W
    pypdfium2 = pytest.importorskip("pypdfium2")
    tdb, http = env
    t = await _talent(tdb, cdn, name="Maximilian-Alexander Bartholomew-Montgomery Fitzgerald")
    link, h = await _roster(http, tdb, cdn, [t])
    view = await _view(http, link, h)
    assert view["talents"][0]["name"] == "Maximilian-Alexander Bartholomew-Montgomery F"
    assert view["talents"][0]["pages"][0]["name_pt"] == L.name_pt(view["talents"][0]["name"]) < 28.0
    page = pypdfium2.PdfDocument(await _pdf(http, link, h))[1]
    img = page.render(scale=4).to_pil().convert("L")
    mm = img.size[0] / 210.0
    band = img.crop((0, int(28 * mm), img.size[0], int(46 * mm))).point(lambda v: 255 if v < 128 else 0)
    bx = band.getbbox()
    assert bx and bx[0] / mm >= L.MARGIN - 0.6 and bx[2] / mm <= L.PAGE_W - L.MARGIN + 0.6, (bx[0] / mm, bx[2] / mm)


async def test_no_value_is_ever_truncated_with_an_ellipsis_it_gets_a_full_width_row():
    items = [
        {"key": "age", "label": "Age", "value": "29"},
        {"key": "location", "label": "Location", "value": "Mumbai, India, Los Angeles, United States, Dubai, United Arab Emirates, London, United Kingdom"},
        {"key": "instagram", "label": "Instagram", "value": "@abcdefghij_klmnopqrst_uvwxy.1", "href": "https://www.instagram.com/x/"},
    ]
    placed, _ = L.place_info(items, 240.0)
    by = {i["key"]: i for i in placed}
    assert " ".join(by["location"]["lines"]) == items[1]["value"] and by["location"]["w"] == L.CONTENT_W
    assert by["instagram"]["lines"] == [items[2]["value"]] and by["instagram"]["w"] == L.CONTENT_W
    assert not any("…" in ln for i in placed for ln in i["lines"])
    # the everyday cases keep their compact single-row geometry (Rhea's two-line location stays in its column)
    ok = [{"key": "age", "label": "Age", "value": "20"}, {"key": "height", "label": "Height", "value": "5'7\""},
          {"key": "location", "label": "Location", "value": "Mumbai, India, Los Angeles, United States"},
          {"key": "instagram", "label": "Instagram", "value": "@__rheagupta"}]
    rows = {i["y"] for i in L.place_info(ok, 240.0)[0]}
    assert rows == {240.0}


async def test_information_page_name_is_fitted_and_hostile_pdf_text_is_complete(env, cdn):
    tdb, http = env
    long_name = "Maximilian-Alexander Bartholomew-Montgomery Fitzgerald"
    t = await _talent(tdb, cdn, name=long_name, instagram_handle="abcdefghij_klmnopqrst_uvwxy.1",
                      location=[{"city": c, "country": k} for c, k in [("Mumbai", "India"), ("Los Angeles", "United States"), ("Dubai", "United Arab Emirates"), ("London", "United Kingdom")]],
                      skills=[f"Skill {i}" for i in range(60)] + ["English", "Hindi"])
    link, h = await _roster(http, tdb, cdn, [t], fields=ALL_ON)
    view = await _view(http, link, h)
    pages = view["talents"][0]["pages"]
    info_pages = [p for p in pages if p["kind"] == "info"]
    assert info_pages and all(p["name_pt"] <= 20 and p["name_pt"] >= L.NAME_MIN_PT for p in info_pages)
    pdf = _text(await _pdf(http, link, h))
    text = " ".join(pdf.split())
    assert "@abcdefghij_klmnopqrst_uvwxy.1" in text
    assert "London, United Kingdom" in text and "…" not in text
    assert "Skill 59" in text
    pypdfium2 = pytest.importorskip("pypdfium2")
    doc = pypdfium2.PdfDocument(await _pdf(http, link, h))
    for idx in range(1, len(doc)):          # every page: the name band stays inside the margins
        img = doc[idx].render(scale=4).to_pil().convert("L")
        mm = img.size[0] / 210.0
        band = img.crop((0, int(28 * mm), img.size[0], int(46 * mm))).point(lambda v: 255 if v < 128 else 0)
        bx = band.getbbox()
        if bx:
            assert bx[0] / mm >= L.MARGIN - 0.6 and bx[2] / mm <= L.PAGE_W - L.MARGIN + 0.6, (idx, bx[0] / mm, bx[2] / mm)


async def test_pdf_cache_key_changes_with_the_design_so_stale_pdfs_are_never_served(monkeypatch):
    data = {"title": "T", "talents": [{"name": "A", "images": [], "info": []}]}
    k1 = roster_pdf.content_key(data)
    assert k1 == roster_pdf.content_key(data)                      # stable for the same design + data
    monkeypatch.setattr(roster_pdf, "_DESIGN_HASH", "different-layout-code")
    assert roster_pdf.content_key(data) != k1                      # a layout/PDF code change -> new key
    monkeypatch.undo()
    assert roster_pdf.content_key({**data, "title": "T2"}) != k1  # content change -> new key
    assert len(roster_pdf._design_hash()) == 12 and roster_pdf._DESIGN_HASH == roster_pdf._design_hash()
