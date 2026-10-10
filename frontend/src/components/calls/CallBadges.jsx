import React from "react";
import { PRIORITY_META, STATE_META, PRIORITIES, stageLabel } from "@/lib/calls";

const chip = "inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide leading-none whitespace-nowrap";

export function PriorityBadge({ priority }) {
    const m = PRIORITY_META[priority] || PRIORITY_META.normal;
    return <span className={`${chip} ${m.cls}`} data-testid="call-priority">{m.label}</span>;
}

export function StateBadge({ state }) {
    const m = STATE_META[state] || STATE_META.pending;
    return <span className={`${chip} ${m.cls}`} data-testid="call-state">{m.label}</span>;
}

export function StageChip({ stage }) {
    return <span className={`${chip} ${stage === "follow_up" ? "bg-amber-50 text-amber-800 border-amber-200" : "bg-black/[0.04] text-black/65 border-transparent"}`}>{stageLabel(stage)}</span>;
}

// Priority is a badge for everyone, and a compact picker only for people allowed to change it
// (an admin, or the assignee). The choice persists server-side, so it survives a refresh.
export function PriorityControl({ priority, editable, onChange, testId }) {
    if (!editable) return <PriorityBadge priority={priority} />;
    const m = PRIORITY_META[priority] || PRIORITY_META.normal;
    return (
        <select
            value={priority}
            onChange={(e) => onChange(e.target.value)}
            aria-label="Priority"
            data-testid={testId}
            className={`${chip} ${m.cls} cursor-pointer appearance-none pr-1.5 focus:outline-none focus:ring-1 focus:ring-black/20`}
        >
            {PRIORITIES.map((p) => <option key={p} value={p}>{PRIORITY_META[p].label}</option>)}
        </select>
    );
}
