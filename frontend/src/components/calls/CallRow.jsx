import React from "react";
import { Phone, Eye, History, Layers } from "lucide-react";
import { PriorityControl, StateBadge, StageChip } from "./CallBadges";
import { CALL_RESULT_LABELS, UPDATE_STATUS_LABELS, fmtAgo, fmtLocal, rowKey } from "@/lib/calls";

// One call. A single responsive block (no separate desktop/mobile copies): a five-column grid from md up,
// a wrapped stack below it. The name opens the call details, where the talent's other projects live.
export default function CallRow({ row: r, isAdmin, currentUserId, selected, onSelect, onOpen, onRecord, onHistory, onProfile, onPriority, showProject }) {
    const canPrioritise = isAdmin || (!!currentUserId && r.assigned_to_id === currentUserId);
    const done = r.call_state === "completed";
    return (
        <div
            className={`px-3 py-2 border-t border-black/[0.05] flex flex-wrap items-center gap-x-3 gap-y-1.5 md:grid md:grid-cols-[minmax(0,1.5fr)_96px_minmax(0,1.3fr)_minmax(0,1.2fr)_auto] ${done ? "bg-black/[0.012]" : ""}`}
            data-testid={`call-row-${rowKey(r)}`}
        >
            <div className="flex items-start gap-2 min-w-0 w-full md:w-auto">
                {isAdmin && <input type="checkbox" className="mt-1 shrink-0" checked={!!selected} onChange={() => onSelect(r)} aria-label={`Select ${r.talent_name}`} />}
                <div className="min-w-0">
                    <button type="button" onClick={() => onOpen(r)} className="text-left text-[13px] font-medium text-black/85 hover:underline truncate max-w-full block" data-testid="call-open">
                        {r.talent_name}
                    </button>
                    <div className="flex flex-wrap items-center gap-x-2 text-[11px] text-black/40">
                        {showProject && <span className="inline-flex items-center gap-1.5 min-w-0"><span className="truncate max-w-[14rem] text-black/55" data-testid="call-row-project">{r.project_name}</span><StageChip stage={r.pipeline_stage} /></span>}
                        {r.talent_phone && <span>{r.talent_phone}</span>}
                        {r.other_projects_count > 0 && (
                            <button type="button" onClick={() => onOpen(r)} className="inline-flex items-center gap-1 text-[#0c2340] hover:underline" data-testid="call-other-projects" title="This talent is in other ongoing projects — open to see them">
                                <Layers className="w-3 h-3" /> +{r.other_projects_count} other project{r.other_projects_count !== 1 ? "s" : ""}
                            </button>
                        )}
                        {r.is_new && <span className="text-sky-700 font-semibold uppercase tracking-wide text-[10px]" data-testid="call-new">New</span>}
                    </div>
                </div>
            </div>

            <div>
                <PriorityControl priority={r.priority} editable={canPrioritise} onChange={(p) => onPriority(r, p)} testId={`call-priority-select-${rowKey(r)}`} />
            </div>

            <div className="min-w-0 text-[11px] text-black/55 flex-1 md:flex-none">
                <div className="flex items-center gap-1.5 flex-wrap"><StateBadge state={r.call_state} />
                    {r.call_count > 0 && <span className="text-black/35">{r.call_count} call{r.call_count !== 1 ? "s" : ""}</span>}
                </div>
                <div className="truncate" title={r.last_update_text || ""}>
                    {r.last_call_at ? (
                        <>
                            {CALL_RESULT_LABELS[r.last_call_result] || r.last_call_result}
                            {r.last_update_status ? ` · ${UPDATE_STATUS_LABELS[r.last_update_status] || r.last_update_status}` : ""}
                            <span className="text-black/35"> · {fmtAgo(r.last_call_at)}</span>
                            {r.last_call_synced && <span className="text-black/35" title={`Recorded on ${r.last_call_source_project || "another project"}`}> · synced</span>}
                        </>
                    ) : "No calls yet"}
                </div>
            </div>

            <div className="min-w-0 text-[11px] text-black/55 w-full md:w-auto" data-testid="call-assignment">
                {r.assigned_to_id ? (
                    <>
                        <div className="truncate text-black/70">{r.assigned_to_name || "Assigned"}</div>
                        <div className="text-black/35" title={fmtLocal(r.assigned_at)}>{r.assigned_at ? `assigned ${fmtAgo(r.assigned_at)}` : "assigned"}</div>
                    </>
                ) : <span className="text-black/35">Unassigned</span>}
            </div>

            <div className="flex items-center gap-1 w-full md:w-auto md:justify-end">
                <a
                    href={r.talent_phone ? `tel:${r.talent_phone}` : undefined}
                    onClick={(e) => { if (!r.talent_phone) e.preventDefault(); }}
                    className={`p-1.5 rounded-sm ${r.talent_phone ? "text-black/60 hover:bg-black/[0.06]" : "text-black/20 cursor-not-allowed"}`}
                    title={r.talent_phone || "No phone on file"} aria-label="Call"
                ><Phone className="w-3.5 h-3.5" /></a>
                <button type="button" onClick={() => onProfile(r)} className="p-1.5 rounded-sm text-black/60 hover:bg-black/[0.06]" title="View profile" aria-label="View profile"><Eye className="w-3.5 h-3.5" /></button>
                <button type="button" onClick={() => onHistory(r)} className="p-1.5 rounded-sm text-black/60 hover:bg-black/[0.06]" title="Call history" aria-label="Call history"><History className="w-3.5 h-3.5" /></button>
                <button type="button" onClick={() => onRecord(r)} className="px-2 py-1 rounded-sm bg-black text-white text-[10px] font-semibold uppercase" data-testid="call-record">Record</button>
            </div>
        </div>
    );
}
