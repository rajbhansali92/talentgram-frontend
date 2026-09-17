"""Generate Link adapter for Simple Assistant (Phase 4A) — READ-ONLY.

Finds an EXISTING Talentgram generated link that presents exactly one
talent (a "profile link"). Does NOT create links in this phase — the
existing `routers.links.create_link` is admin-only and creating a public
link is a real side effect, so 4A previews the "no link yet" case rather
than auto-generating.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from simple_assistant.readonly_db import RDB

# Same public host the app itself uses for /l/<slug> (see frontend
# lib/api.js PUBLIC_FRONTEND_URL / links. subdomain).
_LINKS_HOST = "https://links.talentgramagency.com"


def link_url(slug: str) -> str:
    return f"{_LINKS_HOST}/l/{slug}"


async def find_profile_links(talent_id: str) -> List[Dict[str, Any]]:
    """Every non-deleted manual link whose subject list is exactly this one
    talent, newest first. Empty list => the talent has no generated profile
    link yet."""
    rows = await RDB.links.find(
        {"talent_ids": [talent_id]},
        {"_id": 0, "id": 1, "slug": 1, "title": 1, "created_at": 1, "deleted": 1, "deleted_at": 1},
    ).to_list(50)
    live = [r for r in rows if not r.get("deleted") and not r.get("deleted_at")]
    live.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [
        {"slug": r["slug"], "url": link_url(r["slug"]), "title": r.get("title") or "Talentgram Profile"}
        for r in live
        if r.get("slug")
    ]


async def best_profile_link(talent_id: str) -> Optional[Dict[str, Any]]:
    links = await find_profile_links(talent_id)
    return links[0] if links else None
