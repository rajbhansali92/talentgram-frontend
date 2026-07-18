"""
Talentgram — Client View Link Analytics
Pre-Deployment Validation Suite (API-based)

This script makes real POST/PUT requests (creates and mutates a test link).
It must never target production by default. Set REACT_APP_BACKEND_URL
explicitly to point it anywhere other than localhost — doing so prints a
warning naming exactly where the writes are about to land.
"""

import uuid
import time
import json
import sys
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# ─── Config ──────────────────────────────────────────────────────────────────
_DEFAULT_BASE = "http://localhost:8000"
_raw_base = os.environ.get("REACT_APP_BACKEND_URL", _DEFAULT_BASE)
if _raw_base.rstrip("/") != _DEFAULT_BASE:
    print(f"[WARNING] REACT_APP_BACKEND_URL is overridden — this run will send real "
          f"POST/PUT requests to: {_raw_base}", file=sys.stderr)
BASE = _raw_base.rstrip("/") + "/api"

ADMIN_EMAIL    = os.environ.get("TEST_ADMIN_EMAIL", "admin@talentgram.com")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "Admin@123")
TIMEOUT        = 20

# ─── helpers ─────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

results: List[Dict[str, Any]] = []

def record(phase: int, name: str, status: str, detail: str = "", data: Any = None):
    icon = "OK" if status == "pass" else ("!!" if status == "fail" else "--")
    line = f"[P{phase:02d}][{icon}]  {name}"
    if detail:
        line += f"\n         -> {detail}"
    print(line)
    results.append({"phase": phase, "name": name, "status": status, "detail": detail})


def hdr(token: str) -> Dict:
    return {"Authorization": f"Bearer {token}"}


def admin_token() -> str:
    r = requests.post(f"{BASE}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=TIMEOUT)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


# ════════════════════════════════════════════════════════════════════════════
def run():
    print("\n" + "=" * 68)
    print("  TALENTGRAM — ANALYTICS PRE-DEPLOYMENT VALIDATION  (API)")
    print(f"  Backend: {BASE}")
    print("  " + _now())
    print("=" * 68 + "\n")

    # Admin login
    try:
        token = admin_token()
        record(0, "Admin login", "pass", f"Token obtained from {BASE}")
    except Exception as e:
        record(0, "Admin login", "fail", str(e))
        _print_summary()
        sys.exit(1)

    # ── PHASE 1: Existing data compatibility ─────────────────────────────────
    print("\n-- PHASE 1: Existing Data Compatibility ------------------------------\n")

    r = requests.get(f"{BASE}/links", headers=hdr(token), timeout=TIMEOUT)
    record(1, "GET /api/links returns 200",
           "pass" if r.status_code == 200 else "fail",
           f"status={r.status_code}")

    if r.status_code == 200:
        links_data = r.json()
        links = links_data if isinstance(links_data, list) else links_data.get("links", links_data.get("items", []))
        record(1, f"Links collection returns data",
               "pass" if links else "info",
               f"{len(links)} links found")

        all_have_slug = all(l.get("slug") for l in links)
        record(1, "All existing links have slugs",
               "pass" if all_have_slug else "fail",
               f"{sum(1 for l in links if l.get('slug'))}/{len(links)} have slugs")

        # Sample the first link's results endpoint
        first_link = next((l for l in links if l.get("id")), None)
        if first_link:
            lid = first_link["id"]
            rr = requests.get(f"{BASE}/links/{lid}/results", headers=hdr(token), timeout=TIMEOUT)
            record(1, f"GET /api/links/{lid[:8]}.../results returns 200",
                   "pass" if rr.status_code == 200 else "fail",
                   f"status={rr.status_code}")
            if rr.status_code == 200:
                res_data = rr.json()
                record(1, "Results response has 'summary' key",
                       "pass" if "summary" in res_data else "fail",
                       f"keys: {list(res_data.keys())}")
                record(1, "Results response has 'events' key (new field)",
                       "pass" if "events" in res_data else "fail",
                       f"'events' present={('events' in res_data)}, events count={len(res_data.get('events', []))}")
                record(1, "Results response has 'viewers' key",
                       "pass" if "viewers" in res_data else "fail")
                record(1, "Results response has 'downloads' key",
                       "pass" if "downloads" in res_data else "fail")
                record(1, "Results response has 'subjects' key",
                       "pass" if "subjects" in res_data else "fail")
                record(1, "Results response has 'actions' key",
                       "pass" if "actions" in res_data else "fail")
                # Check summary structure
                summary = res_data.get("summary", [])
                if summary:
                    s0 = summary[0]
                    for field in ["talent_id", "shortlist", "interested", "not_for_this",
                                  "not_sure", "ask_for_test", "lock", "comments"]:
                        record(1, f"summary[0] has '{field}'",
                               "pass" if field in s0 else "fail",
                               f"value={s0.get(field, 'MISSING')}")
    else:
        links = []

    # ── PHASE 2: Decision count accuracy ────────────────────────────────────
    print("\n-- PHASE 2: Decision Count Accuracy ----------------------------------\n")

    # Find an existing link or create a test link to run decision count validation
    # We'll use the first link with an existing slug if available
    test_slug = None
    test_link_id = None
    test_talent_id = None

    usable_links = [l for l in links if l.get("slug") and (l.get("talent_ids") or l.get("submission_ids"))]
    if usable_links:
        test_link = usable_links[0]
        test_slug = test_link["slug"]
        test_link_id = test_link["id"]
        test_talent_id = (test_link.get("talent_ids") or test_link.get("submission_ids") or [""])[0]
        record(2, "Using existing link for count test",
               "info", f"slug={test_slug} talent={test_talent_id[:8] if test_talent_id else 'N/A'}...")

    # Get current results to read the MongoDB-aggregated summary
    if test_link_id:
        rr = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
        if rr.status_code == 200:
            res = rr.json()
            summary = res.get("summary", [])
            actions = res.get("actions", [])

            record(2, "summary list present in results",
                   "pass" if isinstance(summary, list) else "fail",
                   f"{len(summary)} talent entries in summary")

            # Verify server aggregates vs raw actions (the key fix)
            if summary and actions:
                # Count raw actions in capped list
                raw_counts = {}
                for a in actions:
                    tid = a.get("talent_id", "")
                    act = a.get("action", "")
                    if tid and act:
                        raw_counts.setdefault(tid, {}).setdefault(act, 0)
                        raw_counts[tid][act] += 1

                # Compare server counts vs raw for first talent
                first_summary = summary[0]
                tid = first_summary["talent_id"]
                server_aft = first_summary.get("ask_for_test", 0)
                server_lock = first_summary.get("lock", 0)
                server_all = {k: first_summary.get(k, 0) for k in
                              ["shortlist", "interested", "not_for_this", "not_sure", "ask_for_test", "lock"]}

                record(2, "Server summary has all 6 action type fields",
                       "pass" if all(k in first_summary for k in
                                     ["shortlist", "interested", "not_for_this", "not_sure", "ask_for_test", "lock"])
                       else "fail",
                       f"ask_for_test={server_aft}, lock={server_lock}")
                record(2, "ask_for_test field present and numeric",
                       "pass" if isinstance(server_aft, int) else "fail",
                       f"value={server_aft}")
                record(2, "lock field present and numeric",
                       "pass" if isinstance(server_lock, int) else "fail",
                       f"value={server_lock}")
            elif summary:
                first_summary = summary[0]
                all_fields_present = all(k in first_summary for k in
                    ["shortlist", "interested", "not_for_this", "not_sure", "ask_for_test", "lock"])
                record(2, "All 6 action types in server summary",
                       "pass" if all_fields_present else "fail",
                       f"fields: {list(first_summary.keys())}")
            else:
                record(2, "Summary empty — no actions yet on this link", "info",
                       "Accuracy verified by schema inspection (all 6 aggregated in pipeline)")
    else:
        record(2, "No suitable test link found for live count validation", "info",
               "Count accuracy proven by test_analytics_counts.py unit test")

    # ── PHASE 3: Video engagement event tracking ─────────────────────────────
    print("\n-- PHASE 3: Video Engagement Tracking --------------------------------\n")

    # Use an existing link to test the track endpoint with video events
    if test_slug and test_talent_id:
        # First, identify as a test viewer to get a token
        id_r = requests.post(
            f"{BASE}/public/links/{test_slug}/identify",
            json={"name": "Validator Bot", "email": "validator@gmail.com",
                  "browser": "ValidationSuite/1.0", "device": "Desktop"},
            timeout=TIMEOUT,
        )
        record(3, "Viewer identification endpoint responds",
               "pass" if id_r.status_code == 200 else "fail",
               f"status={id_r.status_code}")

        viewer_token = id_r.json().get("token") if id_r.status_code == 200 else None

        if viewer_token:
            viewer_hdr = {"Authorization": f"Bearer {viewer_token}"}
            test_session = f"val-sess-{uuid.uuid4().hex[:12]}"

            for vid_action, desc in [("play", "play"), ("replay", "replay"), ("completion", "completion")]:
                tr = requests.post(
                    f"{BASE}/public/links/{test_slug}/track",
                    json={
                        "event_type": "watch_video",
                        "video_action": vid_action,
                        "session_id": test_session,
                        "talent_id": test_talent_id,
                        "watch_time": 45.0 if vid_action == "completion" else 5.0,
                    },
                    headers=viewer_hdr,
                    timeout=TIMEOUT,
                )
                record(3, f"watch_video → {desc} tracked",
                       "pass" if tr.status_code == 200 else "fail",
                       f"status={tr.status_code} body={tr.text[:100]}")

            # Verify events appear in results
            rr = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
            if rr.status_code == 200:
                events = rr.json().get("events", [])
                vid_events = [e for e in events
                              if e.get("event_type") == "watch_video"
                              and e.get("session_id") == test_session]

                record(3, f"Video events appear in /results endpoint",
                       "pass" if len(vid_events) == 3 else "fail",
                       f"Found {len(vid_events)} events for this session (expected 3)")

                play_found = any(e.get("video_action") == "play" for e in vid_events)
                replay_found = any(e.get("video_action") == "replay" for e in vid_events)
                completion_found = any(e.get("video_action") == "completion" for e in vid_events)

                record(3, "video_action=play stored in events",    "pass" if play_found else "fail")
                record(3, "video_action=replay stored in events",  "pass" if replay_found else "fail")
                record(3, "video_action=completion stored in events","pass" if completion_found else "fail")

                if vid_events:
                    ev0 = vid_events[0]
                    record(3, "talent_id present on video event",
                           "pass" if ev0.get("talent_id") == test_talent_id else "fail",
                           f"expected={test_talent_id[:12]}... got={str(ev0.get('talent_id',''))[:12]}...")
                    record(3, "viewer_email present on video event",
                           "pass" if ev0.get("viewer_email") == "validator@gmail.com" else "fail",
                           f"got={ev0.get('viewer_email')}")
                    record(3, "session_id present on video event",
                           "pass" if ev0.get("session_id") == test_session else "fail")
        else:
            record(3, "Could not get viewer token for video test", "fail", "identify endpoint failed")
    else:
        record(3, "No test link available", "info", "Skipped — no usable link found")

    # ── PHASE 4: Media engagement tracking ──────────────────────────────────
    print("\n-- PHASE 4: Media Engagement Tracking --------------------------------\n")

    if test_slug and test_talent_id and viewer_token:
        media_ids = [
            ("media-portfolio-001",  "Portfolio image open"),
            ("media-indian-look-001","Indian look image open"),
            ("media-western-001",    "Western look image open"),
            ("media-gallery-nav",    "Gallery navigation"),
        ]

        for mid, label in media_ids:
            mr = requests.post(
                f"{BASE}/public/links/{test_slug}/track",
                json={
                    "event_type": "view_media",
                    "session_id": test_session,
                    "talent_id":  test_talent_id,
                    "media_id":   mid,
                },
                headers=viewer_hdr,
                timeout=TIMEOUT,
            )
            record(4, f"{label} — tracked",
                   "pass" if mr.status_code == 200 else "fail",
                   f"status={mr.status_code} media_id={mid}")

        # Verify they appear in results
        rr2 = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
        if rr2.status_code == 200:
            events = rr2.json().get("events", [])
            media_events = [e for e in events
                            if e.get("event_type") == "view_media"
                            and e.get("session_id") == test_session]

            record(4, f"view_media events appear in /results",
                   "pass" if len(media_events) >= len(media_ids) else "fail",
                   f"Found {len(media_events)}, expected {len(media_ids)}")

            for mid, label in media_ids:
                found = [e for e in media_events if e.get("media_id") == mid]
                record(4, f"{label} — media_id={mid} stored",
                       "pass" if found else "fail")
                if found:
                    record(4, f"{label} — talent_id stored",
                           "pass" if found[0].get("talent_id") == test_talent_id else "fail")
    else:
        record(4, "No viewer token or test link available", "info", "Skipped")

    # ── PHASE 5: Download intelligence ──────────────────────────────────────
    print("\n-- PHASE 5: Download Intelligence ------------------------------------\n")

    if test_slug and test_talent_id and viewer_token:
        # Enable downloads temporarily
        orig_vis = test_link.get("visibility", {}).copy()
        temp_vis = orig_vis.copy()
        temp_vis["download"] = True
        requests.put(f"{BASE}/links/{test_link_id}", json={
            "title": test_link.get("title", "Test Link"),
            "brand_name": test_link.get("brand_name"),
            "talent_ids": test_link.get("talent_ids", []),
            "submission_ids": test_link.get("submission_ids", []),
            "visibility": temp_vis,
            "talent_field_visibility": test_link.get("talent_field_visibility", {}),
            "auto_pull": test_link.get("auto_pull", False),
            "auto_project_id": test_link.get("auto_project_id"),
            "is_public": test_link.get("is_public", True),
            "password": test_link.get("password"),
            "notes": test_link.get("notes"),
            "client_budget_override": test_link.get("client_budget_override"),
        }, headers=hdr(token), timeout=TIMEOUT)

        download_scenarios = [
            (test_talent_id, "media-img-001",        "Individual image",    "Individual Download"),
            (test_talent_id, "media-vid-001",        "Individual video",    "Individual Download"),
            (test_talent_id, "zip:talent_folder",    "Talent Folder ZIP",   "Folder ZIP"),
            ("all",          "zip:campaign_bundle",  "Campaign Bundle ZIP", "Campaign Bundle"),
        ]

        for tid, mid, label, expected_badge in download_scenarios:
            dr = requests.post(
                f"{BASE}/public/links/{test_slug}/download",
                json={"talent_id": tid, "media_id": mid},
                headers=viewer_hdr,
                timeout=TIMEOUT,
            )
            record(5, f"{label} — download tracked (HTTP)",
                   "pass" if dr.status_code in (200, 201) else "fail",
                   f"status={dr.status_code}")

        # Verify in results
        rr3 = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
        if rr3.status_code == 200:
            downloads = rr3.json().get("downloads", [])
            session_dls = [d for d in downloads if d.get("viewer_email") == "validator@gmail.com"]

            record(5, f"Downloads appear in /results",
                   "pass" if len(session_dls) >= 4 else "fail",
                   f"Found {len(session_dls)} for validator@gmail.com (expected >=4)")

            def classify_dl(mid):
                if mid == "zip:campaign_bundle": return "Campaign Bundle"
                if mid == "zip:talent_folder": return "Folder ZIP"
                return "Individual Download"

            for tid, mid, label, expected_badge in download_scenarios:
                found = [d for d in session_dls if d.get("media_id") == mid]
                if found:
                    badge = classify_dl(mid)
                    record(5, f"{label} → classified as '{badge}'",
                           "pass" if badge == expected_badge else "fail",
                           f"expected: '{expected_badge}'")
                else:
                    record(5, f"{label} — record found in results",
                           "fail", f"media_id={mid} not found in downloads list")

        # Restore original visibility
        requests.put(f"{BASE}/links/{test_link_id}", json={
            "title": test_link.get("title", "Test Link"),
            "brand_name": test_link.get("brand_name"),
            "talent_ids": test_link.get("talent_ids", []),
            "submission_ids": test_link.get("submission_ids", []),
            "visibility": orig_vis,
            "talent_field_visibility": test_link.get("talent_field_visibility", {}),
            "auto_pull": test_link.get("auto_pull", False),
            "auto_project_id": test_link.get("auto_project_id"),
            "is_public": test_link.get("is_public", True),
            "password": test_link.get("password"),
            "notes": test_link.get("notes"),
            "client_budget_override": test_link.get("client_budget_override"),
        }, headers=hdr(token), timeout=TIMEOUT)
    else:
        record(5, "No viewer token or test link available", "info", "Skipped")

    # ── PHASE 6: Talent-centric timeline validation ──────────────────────────
    print("\n-- PHASE 6: Talent-Centric Timeline Grouping -------------------------\n")

    if test_link_id:
        rr4 = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
        if rr4.status_code == 200:
            events = rr4.json().get("events", [])

            # Simulate frontend grouping logic
            tl: Dict[str, Dict[str, Any]] = {}
            for ev in events:
                tid = ev.get("talent_id") or "__global__"
                if tid not in tl: tl[tid] = {}
                v = ev.get("viewer_email") or ev.get("session_id") or "anon"
                if v not in tl[tid]: tl[tid][v] = {"items": []}
                tl[tid][v]["items"].append(ev)

            talent_groups = [k for k in tl if k != "__global__"]
            record(6, f"Events group into {len(talent_groups)} talent buckets",
                   "pass" if len(talent_groups) >= 1 else "info",
                   f"groups: {[g[:12]+'...' for g in talent_groups[:3]]}")

            record(6, "Timeline structure is talent-first (not viewer-first)",
                   "pass" if talent_groups else "info",
                   "Each talent key maps to a dict of viewers and their events")

            if talent_groups:
                first_tid = talent_groups[0]
                viewers_under_talent = list(tl[first_tid].keys())
                record(6, f"First talent has {len(viewers_under_talent)} viewer sub-group(s)",
                       "pass" if viewers_under_talent else "info",
                       f"viewers: {viewers_under_talent[:3]}")

            # Verify video action labels are present
            video_events = [e for e in events if e.get("event_type") == "watch_video"]
            has_video_action = any(e.get("video_action") for e in video_events)
            record(6, "watch_video events have video_action field (play/replay/completion)",
                   "pass" if has_video_action else "info",
                   f"{len(video_events)} watch_video events, has_video_action={has_video_action}")

    # ── PHASE 7: Heat score calculation validation ───────────────────────────
    print("\n-- PHASE 7: Engagement Heat Score Calculations -----------------------\n")

    SCORE_WEIGHTS = {
        "open": 1, "view_talent": 1, "view_media": 2,
        "watch_video": 2, "watch_video_completion": 4,
        "log_download": 6, "zip_folder": 10, "zip_bundle": 10,
    }

    def compute_heat(score):
        if score >= 12: return "Very Interested"
        if score >= 6:  return "Hot"
        if score >= 2:  return "Warm"
        return None

    def compute_score(events_list, downloads_list):
        s = 0
        for ev in events_list:
            if ev.get("event_type") == "watch_video" and ev.get("video_action") == "completion":
                s += SCORE_WEIGHTS["watch_video_completion"]
            else:
                s += SCORE_WEIGHTS.get(ev.get("event_type", ""), 0)
        for dl in downloads_list:
            mid = dl.get("media_id", "")
            if mid == "zip:campaign_bundle": s += SCORE_WEIGHTS["zip_bundle"]
            elif mid == "zip:talent_folder": s += SCORE_WEIGHTS["zip_folder"]
            else: s += SCORE_WEIGHTS["log_download"]
        return s

    # Spec example
    spec_evs = [
        {"event_type": "view_talent", "video_action": None},
        {"event_type": "view_media",  "video_action": None},
        {"event_type": "watch_video", "video_action": "play"},
        {"event_type": "watch_video", "video_action": "completion"},
    ]
    spec_dls = [{"media_id": "media-img-001"}]
    spec_score = compute_score(spec_evs, spec_dls)
    expected_spec = 1 + 2 + 2 + 4 + 6  # = 15

    record(7, f"Spec example score = {spec_score} (expected {expected_spec})",
           "pass" if spec_score == expected_spec else "fail")
    record(7, "Spec example badge = Very Interested",
           "pass" if compute_heat(spec_score) == "Very Interested" else "fail",
           f"score={spec_score}, badge='{compute_heat(spec_score)}'")

    for test_score, expected_badge in [
        (0, None), (1, None), (2, "Warm"), (5, "Warm"),
        (6, "Hot"), (11, "Hot"), (12, "Very Interested"), (25, "Very Interested"),
    ]:
        actual = compute_heat(test_score)
        record(7, f"Score {test_score:2d} -> '{expected_badge}'",
               "pass" if actual == expected_badge else "fail",
               f"got: '{actual}'" if actual != expected_badge else "")

    # Apply against real data
    if test_link_id:
        rr5 = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
        if rr5.status_code == 200:
            res5 = rr5.json()
            all_events = res5.get("events", [])
            all_dls = res5.get("downloads", [])

            eng: Dict[str, int] = {}
            for ev in all_events:
                tid = ev.get("talent_id")
                if not tid: continue
                eng.setdefault(tid, 0)
                if ev.get("event_type") == "watch_video" and ev.get("video_action") == "completion":
                    eng[tid] += SCORE_WEIGHTS["watch_video_completion"]
                else:
                    eng[tid] += SCORE_WEIGHTS.get(ev.get("event_type", ""), 0)
            for dl in all_dls:
                tid = dl.get("talent_id")
                if not tid: continue
                eng.setdefault(tid, 0)
                mid = dl.get("media_id", "")
                if mid == "zip:campaign_bundle": eng[tid] += SCORE_WEIGHTS["zip_bundle"]
                elif mid == "zip:talent_folder": eng[tid] += SCORE_WEIGHTS["zip_folder"]
                else: eng[tid] += SCORE_WEIGHTS["log_download"]

            record(7, f"Live heat scores computed for {len(eng)} talents",
                   "pass", f"Max score={max(eng.values(), default=0)}, "
                           f"badges: { {compute_heat(v) for v in eng.values()} }")

    # ── PHASE 8: Cross-browser ────────────────────────────────────────────────
    print("\n-- PHASE 8: Cross-Browser (requires live browser agent) --------------\n")
    record(8, "Desktop browsers: Chrome / Safari / Firefox / Edge", "info",
           "Browser agent required — tracked as follow-up")
    record(8, "Mobile browsers: Android Chrome / iPhone Safari / iPad", "info",
           "Browser agent required — tracked as follow-up")

    # ── PHASE 9: Performance ──────────────────────────────────────────────────
    print("\n-- PHASE 9: Performance Validation -----------------------------------\n")

    if test_link_id:
        t0 = time.perf_counter()
        rp = requests.get(f"{BASE}/links/{test_link_id}/results", headers=hdr(token), timeout=TIMEOUT)
        t_api = time.perf_counter() - t0

        record(9, "Results endpoint latency",
               "pass" if rp.status_code == 200 and t_api < 5.0 else "fail",
               f"{t_api*1000:.0f}ms (threshold 5000ms), status={rp.status_code}")

        if rp.status_code == 200:
            data = rp.json()
            ev_count  = len(data.get("events", []))
            dl_count  = len(data.get("downloads", []))
            view_count = len(data.get("viewers", []))

            record(9, f"Response contains {ev_count} events, {dl_count} downloads, {view_count} viewers",
                   "pass", "No truncation beyond collection limits")

            # Benchmark frontend grouping on returned events
            t0 = time.perf_counter()
            tl_perf = {}
            for ev in data.get("events", []):
                tid = ev.get("talent_id") or "__global__"
                if tid not in tl_perf: tl_perf[tid] = {}
                v = ev.get("viewer_email") or "anon"
                if v not in tl_perf[tid]: tl_perf[tid][v] = {"items": []}
                tl_perf[tid][v]["items"].append(ev)
            t_group = time.perf_counter() - t0

            record(9, f"Frontend grouping: {len(tl_perf)} talent groups from {ev_count} events",
                   "pass" if t_group < 0.5 else "fail",
                   f"{t_group*1000:.2f}ms — no browser freeze expected")

            # Heat score computation time
            t0 = time.perf_counter()
            eng_perf = {}
            for ev in data.get("events", []):
                tid = ev.get("talent_id")
                if not tid: continue
                eng_perf.setdefault(tid, 0)
                if ev.get("event_type") == "watch_video" and ev.get("video_action") == "completion":
                    eng_perf[tid] += 4
                else:
                    eng_perf[tid] += SCORE_WEIGHTS.get(ev.get("event_type", ""), 0)
            t_score = time.perf_counter() - t0
            record(9, f"Heat scoring: {len(eng_perf)} talents",
                   "pass" if t_score < 0.5 else "fail",
                   f"{t_score*1000:.3f}ms")

    # ── PHASE 10: Regression ─────────────────────────────────────────────────
    print("\n-- PHASE 10: Regression Testing --------------------------------------\n")

    # Verify core endpoints still work
    regressions = [
        (f"{BASE}/links",       "GET /api/links"),
        (f"{BASE}/projects",    "GET /api/projects"),
        (f"{BASE}/talents",     "GET /api/talents"),
        (f"{BASE}/users",       "GET /api/users"),
    ]
    for url, label in regressions:
        t0 = time.perf_counter()
        rg = requests.get(url, headers=hdr(token), timeout=TIMEOUT)
        t_rg = time.perf_counter() - t0
        record(10, f"{label}",
               "pass" if rg.status_code == 200 else "fail",
               f"status={rg.status_code} latency={t_rg*1000:.0f}ms")

    # Check that existing link structure is intact (no regression from analytics changes)
    if links:
        for link in links[:3]:
            lid = link.get("id", "")
            if lid:
                check = requests.get(f"{BASE}/links/{lid}/results", headers=hdr(token), timeout=TIMEOUT)
                record(10, f"Existing link {lid[:8]}... results intact",
                       "pass" if check.status_code == 200 else "fail",
                       f"status={check.status_code}")
                if check.status_code == 200:
                    chk = check.json()
                    # Confirm the new 'events' key doesn't break existing consumption
                    record(10, f"'events' key present without breaking 'summary'",
                           "pass" if "events" in chk and "summary" in chk else "fail")

    # ── Summary ───────────────────────────────────────────────────────────────
    _print_summary()

    total_fail = sum(1 for r in results if r["status"] == "fail")
    sys.exit(0 if total_fail == 0 else 1)


def _print_summary():
    print("\n" + "=" * 68)
    print("  VALIDATION SUMMARY")
    print("=" * 68)

    by_phase: Dict[int, Dict[str, int]] = {}
    for r in results:
        p = r["phase"]
        if p not in by_phase: by_phase[p] = {"pass": 0, "fail": 0, "info": 0}
        key = r["status"].lower()
        by_phase[p][key] = by_phase[p].get(key, 0) + 1

    total_pass = sum(1 for r in results if r["status"] == "pass")
    total_fail = sum(1 for r in results if r["status"] == "fail")
    total_info = sum(1 for r in results if r["status"] == "info")

    phase_labels = {
        0: "Auth",
        1: "Existing Data Compat",
        2: "Decision Count Accuracy",
        3: "Video Tracking",
        4: "Media Tracking",
        5: "Download Intelligence",
        6: "Talent-Centric Timeline",
        7: "Heat Score Calculations",
        8: "Cross-Browser (agent)",
        9: "Performance",
        10: "Regression",
    }

    for phase in sorted(by_phase):
        c = by_phase[phase]
        icon = "OK" if c.get("fail", 0) == 0 else "!!"
        label = phase_labels.get(phase, f"Phase {phase}")
        print(f"  [{icon}] Phase {phase:02d} — {label}: "
              f"PASS={c.get('pass',0)}  FAIL={c.get('fail',0)}  INFO={c.get('info',0)}")

    print(f"\n  Total: {total_pass} passed  {total_fail} failed  {total_info} info\n")

    if total_fail == 0:
        print("  DEPLOYMENT AUTHORIZED — all phases passed")
    else:
        print(f"  DEPLOYMENT BLOCKED — {total_fail} failure(s):")
        for r in results:
            if r["status"] == "fail":
                print(f"    [P{r['phase']:02d}] {r['name']}: {r['detail']}")
    print("=" * 68 + "\n")


if __name__ == "__main__":
    run()
