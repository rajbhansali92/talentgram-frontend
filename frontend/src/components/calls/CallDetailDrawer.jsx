import React, { useEffect, useState } from "react";
import { X, Loader2, Phone, History, Eye } from "lucide-react";
import { adminApi } from "@/lib/api";
import { PriorityControl, StateBadge, StageChip } from "./CallBadges";
import { CALL_RESULT_LABELS, UPDATE_STATUS_LABELS, fmtAgo, fmtLocal } from "@/lib/calls";

function Outcome({ r }) {
    if (!r.last_call_at) return <span className="text-black/35">No calls yet</span>;
    return (
        <span>
            {CALL_RESULT_LABELS[r.last_call_result] || r.last_call_result}
            {r.last_update_status ? ` · ${UPDATE_STATUS_LABELS[r.last_update_status] || r.last_update_status}` : ""}
            <span className="text-black/40"> · {fmtAgo(r.last_call_at)}{r.last_call_by_name ? ` · ${r.last_call_by_name}` : ""}</span>
            {r.last_call_synced && <span className="text-black/40"> · synced from {r.last_call_source_project || "another project"}</span>}
        </span>
    );
}

// Everything the caller needs before dialling: this call's own assignment/priority, and the same talent's
// OTHER ongoing projects (stage, status, owner, latest outcome) so nobody makes a redundant call.
export default function CallDetailDrawer({ row, isAdmin, currentUserId, onClose, onRecord, onHistory, onProfile, onPriority }) {
    const [ctx, setCtx] = useState(null);
    const [error, setError] = useState(false);

    useEffect(() => {
        let cancelled = false;
        setCtx(null); setError(false);
        adminApi.get(`/workflow/calls/${row.talent_id}/${row.project_id}/context`)
            .then(({ data }) => { if (!cancelled) setCtx(data); })
            .catch(() => { if (!cancelled) setError(true); });
        return () => { cancelled = true; };
    }, [row.talent_id, row.project_id, row.last_call_at, row.priority, row.assigned_to_id]);

    const cur = ctx?.current || row;
    const canPrioritise = isAdmin || (!!currentUserId && cur.assigned_to_id === currentUserId);
    const others = ctx?.other_projects || [];

    return (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
            <aside className="w-full sm:w-[26rem] h-full bg-white shadow-xl overflow-y-auto" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Call details" data-testid="call-detail">
                <div className="sticky top-0 bg-white border-b border-black/[0.06] px-4 py-3 flex items-start justify-between gap-2 z-10">
                    <div className="min-w-0">
                        <p className="text-sm font-semibold text-black/90 break-words">{row.talent_name}</p>
                        <p className="text-[11px] text-black/45 break-words">{row.project_name}</p>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full text-black/40 hover:text-black hover:bg-black/[0.04] shrink-0" aria-label="Close"><X className="w-4 h-4" /></button>
                </div>

                <div className="p-4 space-y-5">
                    <div className="flex flex-wrap gap-1.5">
                        <a href={row.talent_phone ? `tel:${row.talent_phone}` : undefined} onClick={(e) => { if (!row.talent_phone) e.preventDefault(); }}
                            className={`inline-flex items-center gap-1.5 px-3 py-2 rounded-sm text-xs font-semibold ${row.talent_phone ? "bg-black text-white" : "bg-black/10 text-black/30"}`}>
                            <Phone className="w-3.5 h-3.5" /> {row.talent_phone || "No phone"}
                        </a>
                        <button onClick={() => onRecord(cur)} className="px-3 py-2 rounded-sm border border-black/[0.12] text-xs font-semibold" data-testid="detail-record">Record call</button>
                        <button onClick={() => onHistory(cur)} className="p-2 rounded-sm border border-black/[0.12] text-black/60" title="Call history" aria-label="Call history"><History className="w-3.5 h-3.5" /></button>
                        <button onClick={() => onProfile(cur)} className="p-2 rounded-sm border border-black/[0.12] text-black/60" title="View profile" aria-label="View profile"><Eye className="w-3.5 h-3.5" /></button>
                    </div>

                    <section data-testid="detail-this-project">
                        <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">This project</p>
                        <dl className="grid grid-cols-[96px_1fr] gap-y-1.5 text-xs">
                            <dt className="text-black/40">Pipeline</dt><dd><StageChip stage={cur.pipeline_stage} /></dd>
                            <dt className="text-black/40">Status</dt><dd><StateBadge state={cur.call_state} /></dd>
                            <dt className="text-black/40">Priority</dt>
                            <dd><PriorityControl priority={cur.priority} editable={canPrioritise} onChange={(p) => onPriority(cur, p)} testId="detail-priority-select" /></dd>
                            <dt className="text-black/40">Assigned to</dt>
                            <dd>{cur.assigned_to_id ? <>{cur.assigned_to_name}<span className="block text-[11px] text-black/40">{fmtLocal(cur.assigned_at)}{cur.assigned_by_name ? ` · by ${cur.assigned_by_name}` : ""}</span></> : <span className="text-black/40">Unassigned</span>}</dd>
                            <dt className="text-black/40">Last call</dt><dd><Outcome r={cur} /></dd>
                            <dt className="text-black/40">Calls made</dt><dd>{cur.call_count || 0}</dd>
                        </dl>
                    </section>

                    <section data-testid="detail-other-projects">
                        <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">Other ongoing projects{ctx ? ` (${others.length})` : ""}</p>
                        {error ? <p className="text-xs text-red-600">Could not load this talent's other projects.</p>
                            : !ctx ? <div className="flex items-center gap-2 text-xs text-black/50"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading...</div>
                                : others.length === 0 ? <p className="text-xs text-black/40">Not in any other ongoing project.</p>
                                    : (
                                        <ul className="space-y-2">
                                            {others.map((o) => (
                                                <li key={o.project_id} className="border border-black/[0.07] rounded-sm p-2.5 space-y-1" data-testid={`other-project-${o.project_id}`}>
                                                    <div className="flex items-center justify-between gap-2">
                                                        <span className="text-xs font-semibold text-black/85 truncate">{o.project_name}</span>
                                                        <StageChip stage={o.pipeline_stage} />
                                                    </div>
                                                    <div className="flex flex-wrap items-center gap-1.5">
                                                        <StateBadge state={o.call_state} />
                                                        <PriorityControl priority={o.priority} editable={false} />
                                                        <span className="text-[11px] text-black/50">{o.assigned_to_id ? `${o.assigned_to_name} · ${fmtAgo(o.assigned_at)}` : "Unassigned"}</span>
                                                    </div>
                                                    <p className="text-[11px] text-black/60"><Outcome r={o} /></p>
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                    </section>
                </div>
            </aside>
        </div>
    );
}
