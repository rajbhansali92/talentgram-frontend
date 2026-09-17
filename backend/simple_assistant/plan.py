"""Dry-run action model for Simple Assistant (Phase 2).

An ``ActionPlan`` is a fully-resolved, human-readable description of what a
command WOULD do. It is never executed in this phase — there is no code
path from this module (or anything that builds one) to a database write.
The shape is deliberately close to what an executor will eventually need
so the execution phase is a thin addition, not a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProposedChange:
    """One concrete change the plan would make. `current_*` always reflects
    a freshly-read value; `proposed_*` is what it would become."""
    op: str                       # "update" | "add" | "remove" | "create"
    entity_type: str              # "talent" | "project"
    entity_label: str
    entity_id: Optional[str] = None
    field: Optional[str] = None    # e.g. "pipeline_stage" | "pipeline_membership"
    current_value: Optional[str] = None
    current_label: Optional[str] = None
    proposed_value: Optional[str] = None
    proposed_label: Optional[str] = None
    # For op == "create": the fields that would be set on the new record.
    fields: Optional[Dict[str, Any]] = None
    note: Optional[str] = None     # reason / source / caveat for this row

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "op": self.op,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_label": self.entity_label,
            "field": self.field,
            "current_value": self.current_value,
            "current_label": self.current_label,
            "proposed_value": self.proposed_value,
            "proposed_label": self.proposed_label,
            "note": self.note,
        }
        if self.fields is not None:
            d["fields"] = self.fields
        return d


@dataclass
class ActionPlan:
    intent: str
    summary: str
    changes: List[ProposedChange] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    project: Optional[Dict[str, str]] = None   # {"id", "label"}
    requires_confirmation: bool = True
    # ALWAYS False in Phase 2. The confirm endpoint checks this and refuses
    # to pretend it executed anything.
    executable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "summary": self.summary,
            "project": self.project,
            "changes": [c.to_dict() for c in self.changes],
            "warnings": self.warnings,
            "requires_confirmation": self.requires_confirmation,
            "executable": self.executable,
        }
