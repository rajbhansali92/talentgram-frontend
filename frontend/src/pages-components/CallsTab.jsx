import React, { useEffect, useMemo, useRef, useState } from "react";
import { adminApi } from "@/lib/api";
import {
    Phone,
    Eye,
    History,
    X,
    Loader2,
    Search,
    ChevronDown,
    Check,
    PhoneCall,
} from "lucide-react";
import { toast } from "sonner";
import { PIPELINE_STAGE_ORDER, STAGE_LABELS, getStageLabel } from "@/components/pipeline/constants";

const CALL_RESULT_OPTIONS = [
    { id: "answered", label: "Answered" },
    { id: "no_answer", label: "No Answer" },
    { id: "busy", label: "Busy" },
    { id: "switched_off", label: "Switched Off" },
    { id: "call_back", label: "Call Back" },
];
const CALL_RESULT_LABELS = Object.fromEntries(CALL_RESULT_OPTIONS.map((o) => [o.id, o.label]));

const UPDATE_STATUS_OPTIONS = [
    { id: "sending", label: "Sending" },
    { id: "not_sending", label: "Not Sending" },
    { id: "not_interested", label: "Not Interested" },
];
const UPDATE_STATUS_LABELS = Object.fromEntries(UPDATE_STATUS_OPTIONS.map((o) => [o.id, o.label]));

const CALL_STATUS_OPTIONS = [
    { id: "", label: "All" },
    { id: "never", label: "Never Called" },
    { id: "today", label: "Called Today" },
    { id: "recent", label: "Called Recently" },
    { id: "stale", label: "Needs Follow Up" },
];

function fmtLocal(iso) {
    if (!iso) return "Never";
    try {
        return new Date(iso).toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
        });
    } catch {
        return iso;
    }
}

function genId() {
    return (crypto?.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
// Full call history for one Talent + Project pair — fetched on demand.
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
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
            <div
                className="w-full max-w-lg max-h-[80vh] bg-white rounded-md shadow-xl flex flex-col"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between p-4 border-b border-black/[0.06]">
                    <div>
                        <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Call History</p>
                        <p className="text-[11px] text-black/45">{talentName} — {projectName}</p>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full text-black/40 hover:text-black hover:bg-black/[0.04]">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="overflow-y-auto p-4 space-y-3">
                    {loading ? (
                        <div className="flex items-center gap-2 text-xs text-black/50">
                            <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading...
                        </div>
                    ) : history.length === 0 ? (
                        <p className="text-xs text-black/45">No calls logged yet.</p>
                    ) : (
                        history.map((h) => (
                            <div key={h.id} className="border border-black/[0.06] rounded-sm p-2.5">
                                <div className="flex items-center justify-between gap-2">
                                    <span className="text-xs font-semibold text-black/85">
                                        {CALL_RESULT_LABELS[h.call_result] || h.call_result}
                                    </span>
                                    <span className="text-[10px] text-black/40">{fmtLocal(h.called_at)}</span>
                                </div>
                                <p className="text-[11px] text-black/50 mt-0.5">
                                    by {h.called_by_name || "Unknown"}
                                    {h.update_status && ` · ${UPDATE_STATUS_LABELS[h.update_status] || h.update_status}`}
                                </p>
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
// Fast call-entry flow: Answered/No Answer (+optional extras) -> if
// Answered, optional Sending/Not Sending/Not Interested -> optional note
// -> Save. A single client-generated id is minted once per modal open and
// reused on save (the router's own idempotency key), and Save disables
// itself immediately on click (server-side dedupe is the real guarantee;
// this just avoids an obvious accidental double-submit).
// ---------------------------------------------------------------------------
function CallEntryModal({ row, onClose, onSaved }) {
    const [callId] = useState(genId());
    const [result, setResult] = useState(null);
    const [updateStatus, setUpdateStatus] = useState(null);
    const [note, setNote] = useState("");
    const [saving, setSaving] = useState(false);

    const handleSave = async () => {
        if (!result || saving) return;
        setSaving(true);
        try {
            const { data } = await adminApi.post("/workflow/calls", {
                id: callId,
                talent_id: row.talent_id,
                project_id: row.project_id,
                call_result: result,
                update_status: updateStatus || null,
                update_text: note.trim() || null,
            });
            toast.success("Call logged");
            onSaved(data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to save call");
            setSaving(false);
        }
    };

    return (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
            <div className="w-full max-w-sm bg-white rounded-md shadow-xl p-4 space-y-4" onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Record Call</p>
                        <p className="text-[11px] text-black/45">{row.talent_name} — {row.project_name}</p>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full text-black/40 hover:text-black hover:bg-black/[0.04]">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div>
                    <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">Result</p>
                    <div className="flex flex-wrap gap-1.5">
                        {CALL_RESULT_OPTIONS.map((o) => (
                            <button
                                key={o.id}
                                onClick={() => { setResult(o.id); if (o.id !== "answered") setUpdateStatus(null); }}
                                className={`px-2.5 py-1.5 rounded-sm text-xs border ${
                                    result === o.id ? "bg-black text-white border-black" : "border-black/[0.1] hover:border-black/25"
                                }`}
                            >
                                {o.label}
                            </button>
                        ))}
                    </div>
                </div>

                {result === "answered" && (
                    <div>
                        <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">Update (optional)</p>
                        <div className="flex flex-wrap gap-1.5">
                            {UPDATE_STATUS_OPTIONS.map((o) => (
                                <button
                                    key={o.id}
                                    onClick={() => setUpdateStatus(updateStatus === o.id ? null : o.id)}
                                    className={`px-2.5 py-1.5 rounded-sm text-xs border ${
                                        updateStatus === o.id ? "bg-black text-white border-black" : "border-black/[0.1] hover:border-black/25"
                                    }`}
                                >
                                    {o.label}
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                <div>
                    <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">Note (optional)</p>
                    <textarea
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        rows={2}
                        placeholder="Add a note about this call..."
                        className="w-full text-xs px-2.5 py-2 border border-black/[0.08] rounded-sm focus:outline-none focus:border-black/30"
                    />
                </div>

                <button
                    onClick={handleSave}
                    disabled={!result || saving}
                    className="w-full px-3.5 py-2 bg-black text-white hover:bg-black/95 disabled:opacity-40 rounded-sm text-xs font-semibold uppercase tracking-wider inline-flex items-center justify-center gap-1.5 focus:outline-none"
                >
                    {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    Save
                </button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main tab
// ---------------------------------------------------------------------------
export default function CallsTab({ isAdmin, currentUserId, users }) {
    const [rows, setRows] = useState([]);
    const [loading, setLoading] = useState(true);
    const [baseline, setBaseline] = useState({ projects: [], talents: [] });

    const [search, setSearch] = useState("");
    const [debouncedSearch, setDebouncedSearch] = useState("");
    const [projectFilter, setProjectFilter] = useState([]);
    const [pipelineFilter, setPipelineFilter] = useState([]);
    const [talentFilter, setTalentFilter] = useState("");
    const [assignmentFilter, setAssignmentFilter] = useState(isAdmin ? "all" : "mine");
    const [callStatusFilter, setCallStatusFilter] = useState("");

    const [selectedKeys, setSelectedKeys] = useState(new Set());
    const [assignTarget, setAssignTarget] = useState("");
    const [assigning, setAssigning] = useState(false);

    const [profileTarget, setProfileTarget] = useState(null); // {talentId, projectName}
    const [historyTarget, setHistoryTarget] = useState(null); // row
    const [callTarget, setCallTarget] = useState(null); // row

    useEffect(() => {
        const t = setTimeout(() => setDebouncedSearch(search.trim()), 300);
        return () => clearTimeout(t);
    }, [search]);

    // Baseline (unfiltered) fetch — populates the Project/Talent filter
    // option lists from the SAME ongoing-project-scoped source the main
    // list itself uses, so "All Projects"/"All Talents" can never surface
    // anything outside an ongoing project (never a second, invented
    // status system, never the full database).
    useEffect(() => {
        adminApi
            .get("/workflow/calls")
            .then(({ data }) => {
                const rows_ = data.rows || [];
                const projectMap = new Map();
                const talentMap = new Map();
                rows_.forEach((r) => {
                    projectMap.set(r.project_id, r.project_name);
                    talentMap.set(r.talent_id, r.talent_name);
                });
                setBaseline({
                    projects: [...projectMap.entries()].map(([id, label]) => ({ id, label })).sort((a, b) => a.label.localeCompare(b.label)),
                    talents: [...talentMap.entries()].map(([id, label]) => ({ id, label })).sort((a, b) => a.label.localeCompare(b.label)),
                });
            })
            .catch(() => {});
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const fetchRows = () => {
        setLoading(true);
        const params = {};
        if (projectFilter.length) params.project_ids = projectFilter.join(",");
        if (talentFilter) params.talent_id = talentFilter;
        if (assignmentFilter) params.assignment = assignmentFilter;
        if (pipelineFilter.length) params.pipeline = pipelineFilter.join(",");
        if (callStatusFilter) params.call_status = callStatusFilter;
        if (debouncedSearch) params.search = debouncedSearch;
        adminApi
            .get("/workflow/calls", { params })
            .then(({ data }) => setRows(data.rows || []))
            .catch(() => toast.error("Failed to load calls"))
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        fetchRows();
        setSelectedKeys(new Set());
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [projectFilter, talentFilter, assignmentFilter, pipelineFilter, callStatusFilter, debouncedSearch]);

    const rowKey = (r) => `${r.talent_id}::${r.project_id}`;

    const toggleSelect = (r) => {
        const key = rowKey(r);
        setSelectedKeys((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key); else next.add(key);
            return next;
        });
    };

    const selectedRows = rows.filter((r) => selectedKeys.has(rowKey(r)));

    const handleBulkAssign = async () => {
        if (selectedRows.length === 0 || assigning) return;
        setAssigning(true);
        try {
            await adminApi.post("/workflow/calls/assign", {
                pairs: selectedRows.map((r) => ({ talent_id: r.talent_id, project_id: r.project_id })),
                assigned_to_id: assignTarget || null,
            });
            toast.success(`Assigned ${selectedRows.length} row(s)`);
            setSelectedKeys(new Set());
            fetchRows();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to assign");
        } finally {
            setAssigning(false);
        }
    };

    const onCallSaved = (call) => {
        setRows((prev) =>
            prev.map((r) =>
                r.talent_id === callTarget.talent_id && r.project_id === callTarget.project_id
                    ? {
                          ...r,
                          last_call_at: call.called_at,
                          last_call_result: call.call_result,
                          last_update_status: call.update_status,
                          last_update_text: call.update_text,
                          call_status_bucket: "today",
                      }
                    : r
            )
        );
        setCallTarget(null);
    };

    const pipelineOptions = useMemo(
        () => PIPELINE_STAGE_ORDER.map((s) => ({ id: s, label: getStageLabel(s) || STAGE_LABELS[s] || s })),
        []
    );

    return (
        <div className="space-y-4">
            {/* Filters toolbar */}
            <div className="border border-black/[0.06] bg-white p-3 rounded-md shadow-sm space-y-2.5">
                <div className="flex items-center gap-1.5 border border-black/[0.08] rounded-sm px-2.5 py-1.5">
                    <Search className="w-3.5 h-3.5 text-black/35" />
                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder="Search talent, project, or phone..."
                        className="text-xs w-full focus:outline-none"
                    />
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <MultiSelectPopover label="Project" options={baseline.projects} selected={projectFilter} onChange={setProjectFilter} />
                    <MultiSelectPopover label="Pipeline" options={pipelineOptions} selected={pipelineFilter} onChange={setPipelineFilter} />
                    <select
                        value={talentFilter}
                        onChange={(e) => setTalentFilter(e.target.value)}
                        className="px-2.5 py-1.5 border border-black/[0.1] rounded-sm text-xs bg-white focus:outline-none"
                    >
                        <option value="">All Talents</option>
                        {baseline.talents.map((t) => (
                            <option key={t.id} value={t.id}>{t.label}</option>
                        ))}
                    </select>
                    <select
                        value={callStatusFilter}
                        onChange={(e) => setCallStatusFilter(e.target.value)}
                        className="px-2.5 py-1.5 border border-black/[0.1] rounded-sm text-xs bg-white focus:outline-none"
                    >
                        {CALL_STATUS_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>{o.label === "All" ? "All Call Statuses" : o.label}</option>
                        ))}
                    </select>
                    {isAdmin && (
                        <div className="flex items-center gap-1 ml-auto">
                            {[
                                { id: "all", label: "All Calls" },
                                { id: "mine", label: "My Calls" },
                                { id: "unassigned", label: "Unassigned" },
                                { id: "assigned", label: "Assigned" },
                            ].map((o) => (
                                <button
                                    key={o.id}
                                    onClick={() => setAssignmentFilter(o.id)}
                                    className={`px-2.5 py-1.5 rounded-sm text-[11px] font-medium border ${
                                        assignmentFilter === o.id
                                            ? "bg-black text-white border-black"
                                            : "bg-black/[0.015] text-black/50 border-black/[0.06] hover:bg-black/[0.03]"
                                    }`}
                                >
                                    {o.label}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {/* Bulk assign bar (admin only) */}
            {isAdmin && selectedKeys.size > 0 && (
                <div className="flex items-center gap-3 bg-black text-white px-4 py-2.5 rounded-sm shadow-sm text-xs">
                    <span>{selectedKeys.size} selected</span>
                    <select
                        value={assignTarget}
                        onChange={(e) => setAssignTarget(e.target.value)}
                        className="text-black text-xs px-2 py-1 rounded-sm ml-auto"
                    >
                        <option value="">Unassign</option>
                        {users.map((u) => (
                            <option key={u.id} value={u.id}>{u.name || u.email}</option>
                        ))}
                    </select>
                    <button
                        onClick={handleBulkAssign}
                        disabled={assigning}
                        className="px-3 py-1.5 bg-white text-black rounded-sm font-semibold disabled:opacity-50"
                    >
                        {assigning ? "Assigning..." : "Apply"}
                    </button>
                    <button onClick={() => setSelectedKeys(new Set())} className="text-white/70 hover:text-white">
                        <X className="w-3.5 h-3.5" />
                    </button>
                </div>
            )}

            {/* Desktop table */}
            <div className="hidden md:block border border-black/[0.06] bg-white rounded-md shadow-sm overflow-x-auto">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="border-b border-black/[0.06] text-[10px] uppercase font-bold text-black/45">
                            {isAdmin && <th className="p-2.5 w-8"></th>}
                            <th className="p-2.5 text-left">Talent</th>
                            <th className="p-2.5 text-left">Project</th>
                            <th className="p-2.5 text-left">Pipeline</th>
                            <th className="p-2.5 text-left">Assigned To</th>
                            <th className="p-2.5 text-left">Last Call</th>
                            <th className="p-2.5 text-left">Latest Update</th>
                            <th className="p-2.5 text-left">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {loading ? (
                            <tr><td colSpan={8} className="p-6 text-center text-black/40"><Loader2 className="w-4 h-4 animate-spin inline" /></td></tr>
                        ) : rows.length === 0 ? (
                            <tr><td colSpan={8} className="p-6 text-center text-black/40">No calls to show.</td></tr>
                        ) : (
                            rows.map((r) => (
                                <tr key={rowKey(r)} className="border-b border-black/[0.04] hover:bg-black/[0.015]">
                                    {isAdmin && (
                                        <td className="p-2.5">
                                            <input type="checkbox" checked={selectedKeys.has(rowKey(r))} onChange={() => toggleSelect(r)} />
                                        </td>
                                    )}
                                    <td className="p-2.5 font-medium text-black/85">{r.talent_name}</td>
                                    <td className="p-2.5 text-black/70">{r.project_name}</td>
                                    <td className="p-2.5">
                                        <span className="px-1.5 py-0.5 bg-black/[0.04] rounded-sm text-[10px] font-semibold uppercase">
                                            {STAGE_LABELS[r.pipeline_stage] || r.pipeline_stage}
                                        </span>
                                    </td>
                                    <td className="p-2.5 text-black/60">{r.assigned_to_name || "Unassigned"}</td>
                                    <td className="p-2.5 text-black/60">{fmtLocal(r.last_call_at)}</td>
                                    <td className="p-2.5 text-black/60 max-w-[160px] truncate" title={r.last_update_text || ""}>
                                        {r.last_update_status ? (UPDATE_STATUS_LABELS[r.last_update_status] || r.last_update_status) : "—"}
                                        {r.last_update_text ? ` · ${r.last_update_text}` : ""}
                                    </td>
                                    <td className="p-2.5">
                                        <div className="flex items-center gap-1">
                                            <a
                                                href={r.talent_phone ? `tel:${r.talent_phone}` : undefined}
                                                onClick={(e) => { if (!r.talent_phone) e.preventDefault(); }}
                                                className={`p-1.5 rounded-sm ${r.talent_phone ? "text-black/60 hover:bg-black/[0.06]" : "text-black/20 cursor-not-allowed"}`}
                                                title={r.talent_phone || "No phone on file"}
                                            >
                                                <Phone className="w-3.5 h-3.5" />
                                            </a>
                                            <button onClick={() => setProfileTarget({ talentId: r.talent_id, projectName: r.project_name })} className="p-1.5 rounded-sm text-black/60 hover:bg-black/[0.06]" title="View profile">
                                                <Eye className="w-3.5 h-3.5" />
                                            </button>
                                            <button onClick={() => setHistoryTarget(r)} className="p-1.5 rounded-sm text-black/60 hover:bg-black/[0.06]" title="Call history">
                                                <History className="w-3.5 h-3.5" />
                                            </button>
                                            <button onClick={() => setCallTarget(r)} className="px-2 py-1 rounded-sm bg-black text-white text-[10px] font-semibold uppercase">
                                                Record
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                            ))
                        )}
                    </tbody>
                </table>
            </div>

            {/* Mobile cards */}
            <div className="md:hidden space-y-2.5">
                {loading ? (
                    <div className="p-6 text-center text-black/40"><Loader2 className="w-4 h-4 animate-spin inline" /></div>
                ) : rows.length === 0 ? (
                    <div className="p-6 text-center text-black/40 text-xs">No calls to show.</div>
                ) : (
                    rows.map((r) => (
                        <div key={rowKey(r)} className="border border-black/[0.06] bg-white rounded-md p-3 space-y-2 shadow-sm">
                            <div className="flex items-start justify-between gap-2">
                                <div className="flex items-start gap-2">
                                    {isAdmin && (
                                        <input type="checkbox" className="mt-1" checked={selectedKeys.has(rowKey(r))} onChange={() => toggleSelect(r)} />
                                    )}
                                    <div>
                                        <p className="text-sm font-medium text-black/90">{r.talent_name}</p>
                                        <p className="text-[11px] text-black/50">{r.project_name}</p>
                                    </div>
                                </div>
                                <span className="px-1.5 py-0.5 bg-black/[0.04] rounded-sm text-[10px] font-semibold uppercase shrink-0">
                                    {STAGE_LABELS[r.pipeline_stage] || r.pipeline_stage}
                                </span>
                            </div>
                            <div className="text-[11px] text-black/55 space-y-0.5">
                                <p>Assigned: {r.assigned_to_name || "Unassigned"}</p>
                                <p>Last Call: {fmtLocal(r.last_call_at)}</p>
                                {r.last_update_status && <p>Update: {UPDATE_STATUS_LABELS[r.last_update_status]}</p>}
                            </div>
                            <div className="flex items-center gap-1.5 pt-1">
                                <a
                                    href={r.talent_phone ? `tel:${r.talent_phone}` : undefined}
                                    onClick={(e) => { if (!r.talent_phone) e.preventDefault(); }}
                                    className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm text-xs font-semibold ${r.talent_phone ? "bg-black text-white" : "bg-black/10 text-black/30"}`}
                                >
                                    <PhoneCall className="w-3.5 h-3.5" /> Call
                                </a>
                                <button onClick={() => setProfileTarget({ talentId: r.talent_id, projectName: r.project_name })} className="p-2.5 rounded-sm border border-black/[0.1] text-black/60">
                                    <Eye className="w-3.5 h-3.5" />
                                </button>
                                <button onClick={() => setHistoryTarget(r)} className="p-2.5 rounded-sm border border-black/[0.1] text-black/60">
                                    <History className="w-3.5 h-3.5" />
                                </button>
                                <button onClick={() => setCallTarget(r)} className="px-3 py-2.5 rounded-sm border border-black/[0.1] text-xs font-semibold">
                                    Record
                                </button>
                            </div>
                        </div>
                    ))
                )}
            </div>

            {profileTarget && (
                <ProfileDrawer talentId={profileTarget.talentId} projectName={profileTarget.projectName} onClose={() => setProfileTarget(null)} />
            )}
            {historyTarget && (
                <HistoryModal
                    talentId={historyTarget.talent_id}
                    projectId={historyTarget.project_id}
                    talentName={historyTarget.talent_name}
                    projectName={historyTarget.project_name}
                    onClose={() => setHistoryTarget(null)}
                />
            )}
            {callTarget && (
                <CallEntryModal row={callTarget} onClose={() => setCallTarget(null)} onSaved={onCallSaved} />
            )}
        </div>
    );
}
