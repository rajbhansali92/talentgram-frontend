// Shared vocabulary + formatting for Workflow → Calls. The values mirror the backend's canonical enums
// (routers/workflow_calls_schemas.py, workflow_calls_logic.py) — nothing is invented here.
import { STAGE_LABELS } from "@/components/pipeline/constants";

export const CALL_RESULT_OPTIONS = [
    { id: "answered", label: "Answered" },
    { id: "no_answer", label: "No Answer" },
    { id: "busy", label: "Busy" },
    { id: "switched_off", label: "Switched Off" },
    { id: "call_back", label: "Call Back" },
];
export const CALL_RESULT_LABELS = Object.fromEntries(CALL_RESULT_OPTIONS.map((o) => [o.id, o.label]));

export const UPDATE_STATUS_OPTIONS = [
    { id: "sending", label: "Sending" },
    { id: "not_sending", label: "Not Sending" },
    { id: "not_interested", label: "Not Interested" },
];
export const UPDATE_STATUS_LABELS = Object.fromEntries(UPDATE_STATUS_OPTIONS.map((o) => [o.id, o.label]));

export const PRIORITIES = ["urgent", "semi_urgent", "normal"];
export const PRIORITY_META = {
    urgent: { label: "Urgent", cls: "bg-red-50 text-red-700 border-red-200" },
    semi_urgent: { label: "Semi-urgent", cls: "bg-amber-50 text-amber-800 border-amber-200" },
    normal: { label: "Normal", cls: "bg-black/[0.03] text-black/55 border-black/[0.08]" },
};

export const STATE_META = {
    pending: { label: "Pending", cls: "bg-sky-50 text-sky-700 border-sky-200" },
    attempted: { label: "Attempted", cls: "bg-amber-50 text-amber-800 border-amber-200" },
    completed: { label: "Completed", cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
};

export const CALL_STATE_FILTERS = [
    { id: "", label: "All statuses" },
    { id: "pending", label: "Pending" },
    { id: "attempted", label: "Attempted" },
    { id: "completed", label: "Completed" },
];

export const LAST_CALLED_FILTERS = [
    { id: "", label: "Any last call" },
    { id: "never", label: "Never called" },
    { id: "today", label: "Called today" },
    { id: "recent", label: "Called recently" },
    { id: "stale", label: "Not called for 7+ days" },
];

// Follow-up first (the working lane), then the rest in the order the team works through them. Mirrors
// STAGE_DISPLAY_ORDER on the server, which is what orders the lanes in the grouped response.
export const CALL_PIPELINE_ORDER = [
    "follow_up", "ask_to_test", "already_tested", "approved", "shortlisted", "hold",
    "locked", "pitch", "not_available", "not_interested", "rejected",
];

export const stageLabel = (stage) => STAGE_LABELS[stage] || String(stage || "").replace(/_/g, " ").toUpperCase();

export function fmtLocal(iso, empty = "Never") {
    if (!iso) return empty;
    try {
        return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch {
        return iso;
    }
}

// "2h ago" / "3d ago" — compact, for assignment and last-call lines. Falls back to the date past ~30 days.
export function fmtAgo(iso, now = Date.now()) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const mins = Math.max(0, Math.round((now - t) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
    const days = Math.round(mins / (60 * 24));
    return days <= 30 ? `${days}d ago` : fmtLocal(iso);
}

export const toDo = (c) => (c ? c.pending + c.attempted : 0);

export function genId() {
    return (typeof crypto !== "undefined" && crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export const rowKey = (r) => `${r.talent_id}::${r.project_id}`;
