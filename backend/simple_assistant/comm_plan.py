"""CommunicationPlan — the resolved, previewable description of an external
communication (Phase 4A). Mirrors ActionPlan: fully server-resolved, HMAC-
signed for confirmation, and re-validated against live data before send.

`executable` is True only for the narrowly-enabled operations (send a
talent's existing submission media / existing profile link to a resolved
WhatsApp destination). `attach_media` is resolution+preview only in 4A.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# The action kinds this phase understands.
SEND_MEDIA = "send_media"          # existing Talentgram media -> WhatsApp
SEND_PROFILE_LINK = "send_profile_link"  # existing generated link -> WhatsApp
SEND_PROJECT_INFO = "send_project_info"  # project details -> talent's own WhatsApp
ATTACH_MEDIA = "attach_media"      # media -> Talentgram submission (preview only in 4A)

DEST_GROUP = "whatsapp_group"
DEST_NUMBER = "whatsapp_number"
DEST_SUBMISSION = "submission"


@dataclass
class MediaRef:
    """A pointer to an EXISTING Talentgram media asset — never a new copy."""
    media_id: Optional[str]
    category: str              # canonical: intro_video | take | image | ...
    label: str                 # "Intro Video" / "Audition Take 2"
    url: Optional[str] = None  # the existing hosted URL (Cloudinary/Stream)
    poster_url: Optional[str] = None
    kind: str = "video"        # video | image
    submission_id: Optional[str] = None
    public_id: Optional[str] = None   # content fingerprint — pinned at preview,
                                      # re-checked at confirm (STEP 13 stale guard)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "media_id": self.media_id, "category": self.category, "label": self.label,
            "url": self.url, "poster_url": self.poster_url, "kind": self.kind,
            "submission_id": self.submission_id, "public_id": self.public_id,
        }


@dataclass
class CommunicationPlan:
    action_type: str
    talent: Optional[Dict[str, str]] = None       # {"id", "label"}
    project: Optional[Dict[str, str]] = None      # {"id", "label"}
    submission_id: Optional[str] = None
    destination: Optional[str] = None             # group name OR phone number (server-derived)
    destination_type: Optional[str] = None        # whatsapp_group | whatsapp_number | submission
    destination_label: Optional[str] = None       # human label, e.g. "Google AI Casting"
    destination_source: Optional[str] = None      # "project_casting_group" | "talent_own" —
                                                  # tells confirm which resolver to re-run
    media: List[MediaRef] = field(default_factory=list)
    message: Optional[str] = None
    profile_link: Optional[Dict[str, str]] = None  # {"slug", "url", "title"}
    warnings: List[str] = field(default_factory=list)
    requires_confirmation: bool = True
    executable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "talent": self.talent,
            "project": self.project,
            "submission_id": self.submission_id,
            "destination": self.destination,
            "destination_type": self.destination_type,
            "destination_label": self.destination_label,
            "destination_source": self.destination_source,
            "media": [m.to_dict() for m in self.media],
            "message": self.message,
            "profile_link": self.profile_link,
            "warnings": self.warnings,
            "requires_confirmation": self.requires_confirmation,
            "executable": self.executable,
        }
