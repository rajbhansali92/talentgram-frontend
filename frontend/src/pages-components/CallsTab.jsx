import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { adminApi } from "@/lib/api";
import { X, Loader2, Search, ChevronDown, Check, Layers, List as ListIcon, PhoneCall } from "lucide-react";
import { toast } from "sonner";
import CallRow from "@/components/calls/CallRow";
import ProjectGroup from "@/components/calls/ProjectGroup";
import CallDetailDrawer from "@/components/calls/CallDetailDrawer";
import RecordCallModal from "@/components/calls/RecordCallModal";
import { PriorityBadge, StageChip } from "@/components/calls/CallBadges";
import {
    CALL_RESULT_LABELS, UPDATE_STATUS_LABELS, CALL_STATE_FILTERS, LAST_CALLED_FILTERS, CALL_PIPELINE_ORDER,
    PRIORITIES, PRIORITY_META, stageLabel, fmtLocal, rowKey, toDo,
} from "@/lib/calls";

const GROUPED_PAGE = 6;     // projects per page in the grouped view
const LIST_PAGE = 40;       // calls per page in the flat view
const SCOPES = [
    { id: "all", label: "All Calls" },
    { id: "mine", label: "My Calls" },
    { id: "unassigned", label: "Unassigned" },
    { id: "assigned", label: "Assigned" },
    { id: "completed", label: "Completed" },
];

function readPref(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function writePref(key, value) {
    try { localStorage.setItem(key, value); } catch { /* per-viewer convenience only */ }
}

// ---------------------------------------------------------------------------
// Small reusable searchable multi-select popover — project/pipeline filters.
// ---------------------------------------------------------------------------
function MultiSelectPopover({ label, options, selected, onChange }) {
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState("");
    const ref = useRef(null);

    useEffect(() => {
        const onDocClick = (e) => {
            if (ref.current && !ref.current.contains(e.target)) setOpen(false);
        };
        document.addEventListener("mousedown", onDocClick);
        return () => document.removeEventListener("mousedown", onDocClick);
    }, []);

    const filtered = options.filter((o) => o.label.toLowerCase().includes(q.toLowerCase()));
    const toggle = (id) => {
        onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
    };

    return (
        <div className="relative" ref={ref}>
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                className="px-2.5 py-1.5 border border-black/[0.1] rounded-sm text-xs bg-white inline-flex items-center gap-1.5 hover:border-black/25 focus:outline-none"
            >
                {label}
                {selected.length > 0 && (
                    <span className="bg-black text-white rounded-full px-1.5 text-[10px] leading-4">{selected.length}</span>
                )}
                <ChevronDown className="w-3 h-3 text-black/40" />
            </button>
            {open && (
                <div className="absolute z-20 mt-1 w-56 bg-white border border-black/[0.1] rounded-md shadow-lg p-2">
                    <div className="flex items-center gap-1.5 border border-black/[0.08] rounded-sm px-1.5 py-1 mb-1.5">
                        <Search className="w-3 h-3 text-black/35" />
                        <input
                            value={q}
                            onChange={(e) => setQ(e.target.value)}
                            placeholder="Search..."
                            className="text-xs w-full focus:outline-none"
                        />
                    </div>
                    <div className="max-h-56 overflow-y-auto space-y-0.5">
                        {filtered.length === 0 && <p className="text-[11px] text-black/40 px-1.5 py-1">No matches</p>}
                        {filtered.map((o) => (
                            <button
                                type="button"
                                key={o.id}
                                onClick={() => toggle(o.id)}
                                className="w-full flex items-center gap-2 px-1.5 py-1 rounded-sm text-xs hover:bg-black/[0.04] text-left"
                            >
                                <span
                                    className={`w-3.5 h-3.5 rounded-sm border flex items-center justify-center shrink-0 ${
                                        selected.includes(o.id) ? "bg-black border-black" : "border-black/25"
                                    }`}
                                >
                                    {selected.includes(o.id) && <Check className="w-2.5 h-2.5 text-white" />}
                                </span>
                                <span className="truncate">{o.label}</span>
                            </button>
                        ))}
                    </div>
                    {selected.length > 0 && (
                        <button
                            type="button"
                            onClick={() => onChange([])}
                            className="w-full text-[10px] uppercase font-semibold text-black/45 hover:text-black mt-1.5 pt-1.5 border-t border-black/[0.06]"
                        >
                            Clear
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Lightweight Calls-specific talent profile view (TalentPreviewDrawer does
// not show contact fields — see this feature's own audit — so this exists
// purely to show name/phone/alternate/email/project; it is a read GET
// only and never mutates the talent record).
// ---------------------------------------------------------------------------
function ProfileDrawer({ talentId, projectName, onClose }) {
    const [talent, setTalent] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        adminApi
            .get(`/talents/${talentId}`)
            .then(({ data }) => { if (!cancelled) setTalent(data); })
            .catch(() => { if (!cancelled) setTalent(null); })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [talentId]);

    return (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
            <div
                className="w-full sm:w-96 h-full bg-white shadow-xl p-5 overflow-y-auto"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between mb-4">
                    <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Talent Profile</p>
                    <button onClick={onClose} className="p-1 rounded-full text-black/40 hover:text-black hover:bg-black/[0.04]">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                {loading ? (
                    <div className="flex items-center gap-2 text-xs text-black/50">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading...
                    </div>
                ) : !talent ? (
                    <p className="text-xs text-black/50">Could not load this talent's profile.</p>
                ) : (
                    <div className="space-y-4">
                        <div>
                            <p className="text-[10px] uppercase font-bold text-black/45">Name</p>
                            <p className="text-sm text-black/90">{talent.name || "Unnamed"}</p>
                        </div>
                        <div>
                            <p className="text-[10px] uppercase font-bold text-black/45">Primary Phone</p>
                            <p className="text-sm text-black/90">{talent.phone || "—"}</p>
                        </div>
                        {talent.alternate_contact_number && (
                            <div>
                                <p className="text-[10px] uppercase font-bold text-black/45">Alternate Number</p>
                                <p className="text-sm text-black/90">{talent.alternate_contact_number}</p>
                            </div>
                        )}
                        <div>
                            <p className="text-[10px] uppercase font-bold text-black/45">Email</p>
                            <p className="text-sm text-black/90 break-all">{talent.email || "—"}</p>
                        </div>
                        <div>
                            <p className="text-[10px] uppercase font-bold text-black/45">Project</p>
                            <p className="text-sm text-black/90">{projectName}</p>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Full call history for one Talent + Project pair — fetched on demand. Entries synced from another
// project's call are labelled with where they came from (the audit trail).
// ---------------------------------------------------------------------------
function HistoryModal({ talentId, projectId, talentName, projectName, onClose }) {
    const [history, setHistory] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        adminApi
            .get(`/workflow/calls/${talentId}/${projectId}/history`)
            .then(({ data }) => { if (!cancelled) setHistory(data.history || []); })
            .catch(() => { if (!cancelled) toast.error("Failed to load call history"); })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [talentId, projectId]);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
            <div className="w-full max-w-lg max-h-[80vh] bg-white rounded-md shadow-xl flex flex-col" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Call history">
                <div className="flex items-center justify-between gap-2 p-4 border-b border-black/[0.06]">
                    <div className="min-w-0">
                        <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Call History</p>
                        <p className="text-[11px] text-black/45 break-words">{talentName} — {projectName}</p>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full text-black/40 hover:text-black hover:bg-black/[0.04] shrink-0" aria-label="Close"><X className="w-4 h-4" /></button>
                </div>
                <div className="overflow-y-auto p-4 space-y-3">
                    {loading ? (
                        <div className="flex items-center gap-2 text-xs text-black/50"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading...</div>
                    ) : history.length === 0 ? (
                        <p className="text-xs text-black/45">No calls logged yet.</p>
                    ) : (
                        history.map((h) => (
                            <div key={h.id} className="border border-black/[0.06] rounded-sm p-2.5" data-testid="history-entry">
                                <div className="flex items-center justify-between gap-2">
                                    <span className="text-xs font-semibold text-black/85">{CALL_RESULT_LABELS[h.call_result] || h.call_result}</span>
                                    <span className="text-[10px] text-black/40">{fmtLocal(h.called_at)}</span>
                                </div>
                                <p className="text-[11px] text-black/50 mt-0.5">
                                    by {h.called_by_name || "Unknown"}
                                    {h.update_status && ` · ${UPDATE_STATUS_LABELS[h.update_status] || h.update_status}`}
                                </p>
                                {h.synced_from_call_id && (
                                    <p className="text-[10px] text-sky-700 mt-0.5" data-testid="history-synced">Synced from {h.synced_from_project_name || "another project"}</p>
                                )}
                                {h.update_text && <p className="text-xs text-black/70 mt-1">{h.update_text}</p>}
                            </div>
                        ))
                    )}
                </div>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main tab
// ---------------------------------------------------------------------------
function SummaryChip({ label, value, tone }) {
    const toneCls = { warn: "text-red-700", info: "text-sky-700", good: "text-emerald-700" }[tone] || "text-black/80";
    return (
        <div className="flex items-baseline gap-1.5 px-2.5 py-1.5 border border-black/[0.07] bg-white rounded-sm" data-testid={`calls-summary-${label.toLowerCase().replace(/\s+/g, "-")}`}>
            <span className={`text-sm font-semibold ${toneCls}`}>{value}</span>
            <span className="text-[10px] uppercase tracking-wide text-black/45">{label}</span>
        </div>
    );
}

export default function CallsTab({ isAdmin, currentUserId, users }) {
    const [data, setData] = useState({ projects: [], rows: [], summary: null, next_up: [], facets: { projects: [], talents: [], assignees: [] }, pipeline_stages: [] });
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [error, setError] = useState(false);
    const [pagesLoaded, setPagesLoaded] = useState(1);
    const [hasMore, setHasMore] = useState(false);

    const [view, setView] = useState(() => (readPref("calls_view", "grouped") === "list" ? "list" : "grouped"));
    const [scope, setScope] = useState("all");
    const [search, setSearch] = useState("");
    const [debouncedSearch, setDebouncedSearch] = useState("");
    const [projectFilter, setProjectFilter] = useState([]);
    const [pipelineFilter, setPipelineFilter] = useState([]);
    const [talentFilter, setTalentFilter] = useState("");
    const [stateFilter, setStateFilter] = useState("");
    const [priorityFilter, setPriorityFilter] = useState("");
    const [assignedToFilter, setAssignedToFilter] = useState("");
    const [lastCalledFilter, setLastCalledFilter] = useState("");

    const [openProjects, setOpenProjects] = useState({});   // project_id -> bool (explicit choice)
    const [closedLanes, setClosedLanes] = useState({});     // `${project}:${stage}` -> true

    const fetchSeq = useRef(0);
    const [selectedKeys, setSelectedKeys] = useState(new Set());
    const [assignTarget, setAssignTarget] = useState("");
    const [assignPriority, setAssignPriority] = useState("");
    const [assigning, setAssigning] = useState(false);

    const [detailTarget, setDetailTarget] = useState(null);
    const [profileTarget, setProfileTarget] = useState(null);
    const [historyTarget, setHistoryTarget] = useState(null);
    const [callTarget, setCallTarget] = useState(null);

    useEffect(() => {
        const t = setTimeout(() => setDebouncedSearch(search.trim()), 300);
        return () => clearTimeout(t);
    }, [search]);

    const pageSize = view === "grouped" ? GROUPED_PAGE : LIST_PAGE;

    const buildParams = useCallback((page, size) => {
        const params = { view, page, size };
        if (projectFilter.length) params.project_ids = projectFilter.join(",");
        if (talentFilter) params.talent_id = talentFilter;
        if (scope === "mine") params.assignment = "mine";
        if (scope === "unassigned") params.assignment = "unassigned";
        if (scope === "assigned") params.assignment = "assigned";
        if (scope === "completed") params.call_state = "completed"; else if (stateFilter) params.call_state = stateFilter;
        if (assignedToFilter) params.assigned_to_id = assignedToFilter;
        if (pipelineFilter.length) params.pipeline = pipelineFilter.join(",");
        if (lastCalledFilter) params.call_status = lastCalledFilter;
        if (priorityFilter) params.priority = priorityFilter;
        if (debouncedSearch) params.search = debouncedSearch;
        return params;
    }, [view, projectFilter, talentFilter, scope, stateFilter, assignedToFilter, pipelineFilter, lastCalledFilter, priorityFilter, debouncedSearch]);

    // Guards against out-of-order responses: rapid filter changes fire overlapping requests, and a slower,
    // older response must never overwrite a newer one.
    const load = useCallback(({ page = 0, append = false, pages = 1, facets = false } = {}) => {
        const seq = ++fetchSeq.current;
        append ? setLoadingMore(true) : setLoading(true);
        setError(false);
        // a refresh re-reads everything already on screen in one request (the server caps a page at 200)
        const size = Math.min(200, pageSize * pages);
        const params = buildParams(page, size);
        if (!facets) params.include_facets = false;        // the dropdown option lists are fetched once, then kept
        adminApi.get("/workflow/calls", { params })
            .then(({ data: d }) => {
                if (seq !== fetchSeq.current) return;
                setData((prev) => ({
                    ...d,
                    facets: d.facets || prev.facets,
                    projects: append ? [...prev.projects, ...(d.projects || [])] : (d.projects || []),
                    rows: append ? [...prev.rows, ...(d.rows || [])] : (d.rows || []),
                }));
                setHasMore(!!d.has_more);
                setPagesLoaded(append ? page + 1 : pages);
            })
            .catch(() => {
                if (seq !== fetchSeq.current) return;
                setError(true);
                toast.error("Failed to load calls");
            })
            .finally(() => {
                if (seq !== fetchSeq.current) return;
                setLoading(false); setLoadingMore(false);
            });
    }, [buildParams, pageSize]);

    const firstLoad = useRef(true);
    useEffect(() => {
        load({ page: 0, facets: firstLoad.current });
        firstLoad.current = false;
        setSelectedKeys(new Set());
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [buildParams]);

    // pass facets:true when who-is-assigned may have changed (a new assignee must appear in the dropdown)
    const reload = (opts = {}) => load({ page: 0, pages: pagesLoaded, ...opts });
    const loadMore = () => load({ page: pagesLoaded, append: true });

    const switchView = (v) => { setView(v); writePref("calls_view", v); };

    const filtersActive = !!(projectFilter.length || pipelineFilter.length || talentFilter || stateFilter || priorityFilter || assignedToFilter || lastCalledFilter || debouncedSearch || scope !== "all");
    const clearFilters = () => {
        setProjectFilter([]); setPipelineFilter([]); setTalentFilter(""); setStateFilter(""); setPriorityFilter("");
        setAssignedToFilter(""); setLastCalledFilter(""); setSearch(""); setScope("all");
    };

    const toggleSelect = (r) => setSelectedKeys((prev) => { const n = new Set(prev); const k = rowKey(r); n.has(k) ? n.delete(k) : n.add(k); return n; });
    const selectLane = (rows, on) => setSelectedKeys((prev) => { const n = new Set(prev); rows.forEach((r) => (on ? n.add(rowKey(r)) : n.delete(rowKey(r)))); return n; });

    const allRows = useMemo(() => (view === "grouped" ? data.projects.flatMap((g) => g.pipelines.flatMap((p) => p.rows)) : data.rows), [view, data]);
    const selectedRows = allRows.filter((r) => selectedKeys.has(rowKey(r)));

    const handleBulkAssign = async () => {
        if (selectedRows.length === 0 || assigning) return;
        setAssigning(true);
        try {
            const body = { pairs: selectedRows.map((r) => ({ talent_id: r.talent_id, project_id: r.project_id })), assigned_to_id: assignTarget || null };
            if (assignPriority && assignTarget) body.priority = assignPriority;
            await adminApi.post("/workflow/calls/assign", body);
            toast.success(`Assigned ${selectedRows.length} call${selectedRows.length !== 1 ? "s" : ""}`);
            setSelectedKeys(new Set());
            setAssignTarget(""); setAssignPriority("");      // the next batch starts from a clean choice
            reload({ facets: true });
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to assign");
        } finally {
            setAssigning(false);
        }
    };

    const setPriority = async (row, priority) => {
        try {
            await adminApi.patch("/workflow/calls/priority", { pairs: [{ talent_id: row.talent_id, project_id: row.project_id }], priority });
            toast.success(`Priority set to ${PRIORITY_META[priority].label}`);
            setDetailTarget((d) => (d && rowKey(d) === rowKey(row) ? { ...d, priority } : d));
            reload();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Could not change priority");
        }
    };

    const onCallSaved = () => {
        setCallTarget(null);
        reload();
    };

    const projectOpen = (id, idx) => (openProjects[id] !== undefined ? openProjects[id] : (filtersActive || idx < 2));
    const laneClosed = (pid, stage) => !!closedLanes[`${pid}:${stage}`];
    const toggleLane = (pid, stage) => setClosedLanes((c) => ({ ...c, [`${pid}:${stage}`]: !c[`${pid}:${stage}`] }));

    const pipelineOptions = useMemo(
        () => (data.pipeline_stages?.length ? data.pipeline_stages : CALL_PIPELINE_ORDER).map((s) => ({ id: s, label: stageLabel(s) })),
        [data.pipeline_stages],
    );
    const projectOptions = useMemo(() => (data.facets.projects || []).map((p) => ({ id: p.id, label: p.label })), [data.facets]);

    const rowProps = {
        isAdmin, currentUserId,
        onSelect: toggleSelect, onOpen: setDetailTarget, onRecord: setCallTarget,
        onHistory: setHistoryTarget, onProfile: (r) => setProfileTarget({ talentId: r.talent_id, projectName: r.project_name }),
        onPriority: setPriority,
    };
    const empty = !loading && !error && (view === "grouped" ? data.projects.length === 0 : data.rows.length === 0);
    const s = data.summary;
    const selectCls = "px-2.5 py-1.5 border border-black/[0.1] rounded-sm text-xs bg-white focus:outline-none max-w-full";

    return (
        <div className="space-y-3" data-testid="calls-root">
            {/* Summary — describes the whole filtered set, not just the loaded page */}
            {s && (
                <div className="flex flex-wrap gap-2" data-testid="calls-summary">
                    <SummaryChip label="To do" value={toDo(s)} />
                    <SummaryChip label="New" value={s.new} tone={s.new ? "info" : undefined} />
                    <SummaryChip label="Urgent" value={s.urgent} tone={s.urgent ? "warn" : undefined} />
                    <SummaryChip label="Completed" value={s.completed} tone="good" />
                    <SummaryChip label="Unassigned" value={s.unassigned} />
                </div>
            )}

            {/* Next up — the first calls in the queue, whatever the project */}
            {data.next_up?.length > 0 && !loading && (
                <div className="border border-black/[0.07] bg-white rounded-md p-2.5" data-testid="calls-next-up">
                    <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5 inline-flex items-center gap-1"><PhoneCall className="w-3 h-3" /> Next up</p>
                    <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
                        {data.next_up.slice(0, 3).map((r) => (
                            <button key={rowKey(r)} type="button" onClick={() => setDetailTarget(r)} className="text-left border border-black/[0.07] rounded-sm px-2.5 py-1.5 hover:border-black/25 min-w-0" data-testid="next-up-item">
                                <span className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-black/85 truncate">{r.talent_name}</span><PriorityBadge priority={r.priority} /></span>
                                <span className="flex items-center gap-1.5 text-[11px] text-black/45 min-w-0"><span className="truncate">{r.project_name}</span><StageChip stage={r.pipeline_stage} />{r.is_new && <span className="text-sky-700 font-semibold">NEW</span>}</span>
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {/* Filters toolbar */}
            <div className="border border-black/[0.06] bg-white p-3 rounded-md shadow-sm space-y-2.5">
                <div className="flex items-center gap-1.5 border border-black/[0.08] rounded-sm px-2.5 py-1.5">
                    <Search className="w-3.5 h-3.5 text-black/35" />
                    <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search talent, project, or phone..." className="text-xs w-full focus:outline-none" data-testid="calls-search" />
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <MultiSelectPopover label="Project" options={projectOptions} selected={projectFilter} onChange={setProjectFilter} />
                    <MultiSelectPopover label="Pipeline" options={pipelineOptions} selected={pipelineFilter} onChange={setPipelineFilter} />
                    <select value={talentFilter} onChange={(e) => setTalentFilter(e.target.value)} className={selectCls} aria-label="Talent">
                        <option value="">All Talents</option>
                        {(data.facets.talents || []).map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
                    </select>
                    <select value={scope === "completed" ? "completed" : stateFilter} onChange={(e) => { setScope("all"); setStateFilter(e.target.value); }} disabled={scope === "completed"} className={selectCls} aria-label="Call status">
                        {scope === "completed" && <option value="completed">Completed</option>}
                        {CALL_STATE_FILTERS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                    </select>
                    <select value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)} className={selectCls} aria-label="Filter by priority">
                        <option value="">All priorities</option>
                        {PRIORITIES.map((p) => <option key={p} value={p}>{PRIORITY_META[p].label}</option>)}
                    </select>
                    <select value={assignedToFilter} onChange={(e) => setAssignedToFilter(e.target.value)} className={selectCls} aria-label="Assigned to">
                        <option value="">All Team Members</option>
                        <option value="unassigned">Unassigned</option>
                        {(data.facets.assignees || []).map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                    </select>
                    <select value={lastCalledFilter} onChange={(e) => setLastCalledFilter(e.target.value)} className={selectCls} aria-label="Last called">
                        {LAST_CALLED_FILTERS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                    </select>
                    {filtersActive && <button type="button" onClick={clearFilters} className="text-[11px] text-black/50 hover:text-black underline" data-testid="calls-clear">Clear filters</button>}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-1">
                        {SCOPES.map((o) => (
                            <button key={o.id} onClick={() => setScope(o.id)} data-testid={`scope-${o.id}`}
                                className={`px-2.5 py-1.5 rounded-sm text-[11px] font-medium border ${scope === o.id ? "bg-black text-white border-black" : "bg-black/[0.015] text-black/50 border-black/[0.06] hover:bg-black/[0.03]"}`}>
                                {o.label}
                            </button>
                        ))}
                    </div>
                    <div className="flex items-center gap-1" role="group" aria-label="View">
                        <button onClick={() => switchView("grouped")} data-testid="view-grouped" aria-pressed={view === "grouped"} className={`inline-flex items-center gap-1 px-2.5 py-1.5 rounded-sm text-[11px] font-medium border ${view === "grouped" ? "bg-black text-white border-black" : "border-black/[0.1] text-black/55"}`}><Layers className="w-3 h-3" /> Projects</button>
                        <button onClick={() => switchView("list")} data-testid="view-list" aria-pressed={view === "list"} className={`inline-flex items-center gap-1 px-2.5 py-1.5 rounded-sm text-[11px] font-medium border ${view === "list" ? "bg-black text-white border-black" : "border-black/[0.1] text-black/55"}`}><ListIcon className="w-3 h-3" /> List</button>
                    </div>
                </div>
            </div>

            {/* Bulk assign bar (admin only) */}
            {isAdmin && selectedKeys.size > 0 && (
                <div className="flex flex-wrap items-center gap-2 bg-black text-white px-3 py-2.5 rounded-sm shadow-sm text-xs" data-testid="calls-bulk">
                    <span>{selectedKeys.size} selected</span>
                    <select value={assignTarget} onChange={(e) => setAssignTarget(e.target.value)} className="text-black text-xs px-2 py-1 rounded-sm sm:ml-auto" aria-label="Assign to">
                        <option value="">Unassign</option>
                        {users.map((u) => <option key={u.id} value={u.id}>{u.name || u.email}</option>)}
                    </select>
                    <select value={assignPriority} onChange={(e) => setAssignPriority(e.target.value)} disabled={!assignTarget} className="text-black text-xs px-2 py-1 rounded-sm disabled:opacity-50" aria-label="Priority for assignment">
                        <option value="">Keep priority (Normal if new)</option>
                        {PRIORITIES.map((p) => <option key={p} value={p}>{PRIORITY_META[p].label}</option>)}
                    </select>
                    <button onClick={handleBulkAssign} disabled={assigning} className="px-3 py-1.5 bg-white text-black rounded-sm font-semibold disabled:opacity-50">{assigning ? "Assigning..." : "Apply"}</button>
                    <button onClick={() => setSelectedKeys(new Set())} className="text-white/70 hover:text-white" aria-label="Clear selection"><X className="w-3.5 h-3.5" /></button>
                </div>
            )}

            {/* Results */}
            {loading ? (
                <div className="p-8 text-center text-black/40" data-testid="calls-loading"><Loader2 className="w-4 h-4 animate-spin inline" /></div>
            ) : error ? (
                <div className="p-6 text-center text-xs text-black/55 border border-black/[0.06] bg-white rounded-md" data-testid="calls-error">
                    Could not load calls. <button onClick={() => load({ page: 0 })} className="underline">Try again</button>
                </div>
            ) : empty ? (
                <div className="p-6 text-center text-xs text-black/45 border border-black/[0.06] bg-white rounded-md" data-testid="calls-empty">
                    No calls to show.{filtersActive && <> <button onClick={clearFilters} className="underline">Clear filters</button></>}
                </div>
            ) : view === "grouped" ? (
                <div className="space-y-2.5" data-testid="calls-grouped">
                    {data.projects.map((g, idx) => (
                        <ProjectGroup key={g.project_id} group={g} open={projectOpen(g.project_id, idx)}
                            onToggle={() => setOpenProjects((o) => ({ ...o, [g.project_id]: !projectOpen(g.project_id, idx) }))}
                            laneClosed={laneClosed} onToggleLane={toggleLane} isAdmin={isAdmin} selectedKeys={selectedKeys} onSelectLane={selectLane} rowProps={rowProps} />
                    ))}
                </div>
            ) : (
                <div className="border border-black/[0.06] bg-white rounded-md shadow-sm overflow-hidden" data-testid="calls-list">
                    {data.rows.map((r, i) => <div key={rowKey(r)} className={i === 0 ? "[&>div]:border-t-0" : ""}><CallRow row={r} selected={selectedKeys.has(rowKey(r))} showProject {...rowProps} /></div>)}
                </div>
            )}

            {hasMore && !loading && (
                <div className="text-center">
                    <button onClick={loadMore} disabled={loadingMore} className="px-4 py-2 border border-black/[0.12] rounded-sm text-xs font-medium hover:border-black/30 disabled:opacity-50" data-testid="calls-load-more">
                        {loadingMore ? "Loading..." : view === "grouped" ? "Load more projects" : "Load more calls"}
                    </button>
                </div>
            )}

            {detailTarget && (
                <CallDetailDrawer row={detailTarget} isAdmin={isAdmin} currentUserId={currentUserId}
                    onClose={() => setDetailTarget(null)} onRecord={setCallTarget} onHistory={setHistoryTarget}
                    onProfile={rowProps.onProfile} onPriority={setPriority} />
            )}
            {profileTarget && <ProfileDrawer talentId={profileTarget.talentId} projectName={profileTarget.projectName} onClose={() => setProfileTarget(null)} />}
            {historyTarget && (
                <HistoryModal talentId={historyTarget.talent_id} projectId={historyTarget.project_id} talentName={historyTarget.talent_name} projectName={historyTarget.project_name} onClose={() => setHistoryTarget(null)} />
            )}
            {callTarget && <RecordCallModal row={callTarget} onClose={() => setCallTarget(null)} onSaved={onCallSaved} />}
        </div>
    );
}
