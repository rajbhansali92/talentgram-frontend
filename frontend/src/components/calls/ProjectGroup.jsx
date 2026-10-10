import React from "react";
import { ChevronDown, ChevronRight, PhoneCall } from "lucide-react";
import CallRow from "./CallRow";
import { StageChip } from "./CallBadges";
import { rowKey, toDo } from "@/lib/calls";

function CountLine({ c }) {
    return (
        <span className="text-[11px] text-black/50 flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
            <span><b className="text-black/75 font-semibold">{toDo(c)}</b> to do</span>
            <span>{c.assigned} assigned</span>
            {c.urgent > 0 && <span className="text-red-700 font-medium">{c.urgent} urgent</span>}
            {c.new > 0 && <span className="text-sky-700 font-medium">{c.new} new</span>}
            <span>{c.completed} done</span>
        </span>
    );
}

// Project → pipeline lanes → calls. Both levels collapse; the counts stay visible when collapsed so a
// closed project still tells the team how much work is in it.
export default function ProjectGroup({ group: g, open, onToggle, laneClosed, onToggleLane, isAdmin, selectedKeys, onSelectLane, rowProps }) {
    return (
        <section className="border border-black/[0.07] bg-white rounded-md shadow-sm overflow-hidden" data-testid={`call-project-${g.project_id}`}>
            <button type="button" onClick={onToggle} className="w-full flex items-start gap-2 px-3 py-2.5 text-left hover:bg-black/[0.015]" aria-expanded={open} data-testid={`call-project-toggle-${g.project_id}`}>
                {open ? <ChevronDown className="w-4 h-4 mt-0.5 text-black/40 shrink-0" /> : <ChevronRight className="w-4 h-4 mt-0.5 text-black/40 shrink-0" />}
                <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold text-black/85 truncate" data-testid="call-project-name">{g.project_name}</span>
                    <CountLine c={g.counts} />
                </span>
                {g.next_call && !open && (
                    <span className="hidden sm:inline-flex items-center gap-1 text-[11px] text-black/45 shrink-0 max-w-[40%] truncate"><PhoneCall className="w-3 h-3" /> Next: {g.next_call.talent_name}</span>
                )}
            </button>
            {open && g.pipelines.map((lane) => {
                const closed = laneClosed(g.project_id, lane.stage);
                const laneKeys = lane.rows.map(rowKey);
                const allSelected = laneKeys.length > 0 && laneKeys.every((k) => selectedKeys.has(k));
                return (
                    <div key={lane.stage} data-testid={`call-lane-${g.project_id}-${lane.stage}`}>
                        <div className="flex items-center gap-2 px-3 py-1.5 bg-black/[0.02] border-t border-black/[0.05]">
                            {isAdmin && <input type="checkbox" checked={allSelected} onChange={() => onSelectLane(lane.rows, !allSelected)} aria-label={`Select all in ${lane.stage}`} />}
                            <button type="button" onClick={() => onToggleLane(g.project_id, lane.stage)} className="flex items-center gap-2 min-w-0 flex-1 text-left" aria-expanded={!closed}>
                                {closed ? <ChevronRight className="w-3.5 h-3.5 text-black/35" /> : <ChevronDown className="w-3.5 h-3.5 text-black/35" />}
                                <StageChip stage={lane.stage} />
                                <span className="text-[11px] text-black/45 truncate"><b className="text-black/70">{lane.counts.total}</b> · {toDo(lane.counts)} to do{lane.counts.urgent ? ` · ${lane.counts.urgent} urgent` : ""}</span>
                            </button>
                        </div>
                        {!closed && lane.rows.map((r) => (
                            <CallRow key={rowKey(r)} row={r} selected={selectedKeys.has(rowKey(r))} {...rowProps} />
                        ))}
                    </div>
                );
            })}
        </section>
    );
}
