from typing import List, Optional
from pydantic import BaseModel

# Call outcome — "Answered"/"No Answer" are the hard minimum per spec;
# Busy/Switched Off/Call Back are the explicitly-allowed optional extras,
# never more than this fixed set (never free-text — that would defeat the
# one-tap fast-entry UX this whole feature exists for).
CALL_RESULTS = ("answered", "no_answer", "busy", "switched_off", "call_back")

# Quick-update status — one-tap choices for an answered call, plus "other"
# when a free-text note is used instead (update_text still applies to any
# result, this just distinguishes "picked a preset" from "typed one").
UPDATE_STATUSES = ("sending", "not_sending", "not_interested", "other")


class TalentProjectPair(BaseModel):
    talent_id: str
    project_id: str


class CallIn(BaseModel):
    # Client-generated once per Save tap and reused verbatim on any retry —
    # this IS the idempotency key (see workflow_calls.py's own docstring on
    # create_call for the exact mechanism, mirroring the unique-index
    # "claim" pattern already used elsewhere in this codebase, e.g.
    # agents/modules/casting_command_interpreter.py's _claim).
    id: str
    talent_id: str
    project_id: str
    call_result: str  # one of CALL_RESULTS
    update_status: Optional[str] = None  # one of UPDATE_STATUSES, only meaningful when call_result == "answered"
    update_text: Optional[str] = None


class AssignIn(BaseModel):
    pairs: List[TalentProjectPair]
    assigned_to_id: Optional[str] = None  # None = unassign
