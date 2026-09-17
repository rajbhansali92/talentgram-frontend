"""SubmissionPlan — the resolved, previewable description of an incoming
audition / submission-preparation operation (Phase 4B).

Same discipline as ActionPlan / CommunicationPlan: fully server-resolved,
HMAC-signed for confirmation, re-validated against live data before any
attach. `attach_media` is the only executable action; `inspect_submission`
is preview-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

INSPECT = "inspect_submission"
ATTACH = "attach_media"
INGEST = "ingest_audition"          # NEW incoming file → canonical upload pipeline


@dataclass
class SubMedia:
    """A media item — either already on the submission, or available in the
    talent's library to attach. `source_id` is the LIBRARY media id (the
    thing the canonical service is told to attach); `submission_media_id`
    is the id once it's on the submission."""
    category: str                       # canonical: intro_video | take | image | ...
    label: str
    kind: str = "video"                 # video | image
    source_id: Optional[str] = None     # talent-library media id (for attach)
    submission_media_id: Optional[str] = None
    url: Optional[str] = None
    poster_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category, "label": self.label, "kind": self.kind,
            "source_id": self.source_id, "submission_media_id": self.submission_media_id,
            "url": self.url, "poster_url": self.poster_url,
        }


@dataclass
class SubmissionPlan:
    action_type: str
    talent: Optional[Dict[str, str]] = None            # {"id", "label"}
    project: Optional[Dict[str, str]] = None           # {"id", "label"}
    submission_id: Optional[str] = None
    submission_status: Optional[str] = None
    form_found: bool = False
    form: Dict[str, Any] = field(default_factory=dict)
    existing_media: List[SubMedia] = field(default_factory=list)
    available_media: List[SubMedia] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    proposed: List[SubMedia] = field(default_factory=list)   # what ATTACH would add
    warnings: List[str] = field(default_factory=list)
    requires_confirmation: bool = True
    executable: bool = False
    # INGEST only — the incoming file + its resolved destination slot. Carries
    # NO bytes and NO URL: {filename, size, content_type, sha256,
    # target_category, target_label, replaces (bool)}.
    upload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "talent": self.talent,
            "project": self.project,
            "submission_id": self.submission_id,
            "submission_status": self.submission_status,
            "form_found": self.form_found,
            "form": self.form,
            "existing_media": [m.to_dict() for m in self.existing_media],
            "available_media": [m.to_dict() for m in self.available_media],
            "missing": self.missing,
            "proposed": [m.to_dict() for m in self.proposed],
            "warnings": self.warnings,
            "requires_confirmation": self.requires_confirmation,
            "executable": self.executable,
            "upload": self.upload,
        }
