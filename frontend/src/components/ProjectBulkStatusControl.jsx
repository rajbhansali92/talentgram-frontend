import React, { useState } from "react";
import { Loader2 } from "lucide-react";

// The four stored project statuses, in the order the Projects page shows them (value = stored status, label = display).
export const PROJECT_STATUS_OPTIONS = [
    { value: "ongoing", label: "Ongoing" },
    { value: "complete", label: "Complete" },
    { value: "hold", label: "Hold" },
    { value: "locked", label: "Locked" },
];

/**
 * "Change status" control rendered inside the Projects page's bulk-action bar: a status selector + Apply.
 * Nothing is pre-selected, so Apply can never fire on an accidental default; while a request is running both
 * controls are disabled (no double submission).
 */
export default function ProjectBulkStatusControl({ count, busy, onApply, testid = "projects-bulk-status" }) {
    const [value, setValue] = useState("");
    return (
        <div className="flex items-center gap-2 shrink-0" data-testid={testid}>
            <select
                value={value}
                onChange={(e) => setValue(e.target.value)}
                disabled={busy}
                aria-label="New status for the selected projects"
                data-testid={`${testid}-select`}
                className="text-xs h-9 px-2 border border-border bg-background rounded-sm focus:outline-none focus:border-foreground/60 disabled:opacity-50"
            >
                <option value="" disabled hidden>Set status…</option>
                {PROJECT_STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <button
                type="button"
                disabled={!value || busy}
                onClick={() => onApply(value)}
                data-testid={`${testid}-apply`}
                className="text-xs px-3 h-9 bg-foreground text-background rounded-sm inline-flex items-center gap-1.5 hover:opacity-90 shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
            >
                {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                {busy ? "Updating…" : `Apply to ${count}`}
            </button>
        </div>
    );
}
