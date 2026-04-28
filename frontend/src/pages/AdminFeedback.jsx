import React, { useEffect, useMemo, useState, useCallback } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import {
    Mic,
    MessageCircle,
    Check,
    XCircle,
    Edit3,
    Trash2,
    Loader2,
    ShieldCheck,
    Clock,
    ExternalLink,
} from "lucide-react";
import { FEEDBACK_STATUSES } from "@/lib/talentSchema";

// Filter tabs add an `all` synthetic option on top of the canonical statuses.
const FILTERS = [
    ...FEEDBACK_STATUSES,
    { key: "all", label: "All" },
];

function timeAgo(iso) {
    if (!iso) return "";
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return "";
    const diff = (Date.now() - ts) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

export default function AdminFeedback() {
    const [filter, setFilter] = useState("pending");
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [editing, setEditing] = useState(null); // {id, text}
    const [meta, setMeta] = useState({ projects: {} });

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const params = filter === "all" ? {} : { status: filter };
            const { data } = await adminApi.get("/admin/feedback", { params });
            setItems(data || []);
            // Lazy-load project names (used by the FeedbackCard subtitle).
            const projectIds = Array.from(
                new Set((data || []).map((f) => f.project_id).filter(Boolean)),
            );
            const projects = {};
            await Promise.all(
                projectIds.map(async (pid) => {
                    try {
                        const { data: p } = await adminApi.get(`/projects/${pid}`);
                        projects[pid] = p;
                    } catch (e) { console.error(e); }
                }),
            );
            setMeta({ projects });
        } catch (e) {
            toast.error("Failed to load feedback");
        } finally {
            setLoading(false);
        }
    }, [filter]);

    useEffect(() => {
        load();
    }, [load]);

    const counts = useMemo(() => {
        // We only have current-filter data loaded; pending-tab badge stays accurate
        // by recounting whatever the server returned.
        return { current: items.length };
    }, [items]);

    const approve = async (fid) => {
        try {
            await adminApi.post(`/admin/feedback/${fid}/approve`);
            toast.success("Approved & shared with talent");
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        }
    };

    const reject = async (fid) => {
        try {
            await adminApi.post(`/admin/feedback/${fid}/reject`);
            toast.success("Rejected");
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        }
    };

    const saveEdit = async () => {
        if (!editing) return;
        try {
            await adminApi.post(`/admin/feedback/${editing.id}/edit`, {
                text: editing.text,
            });
            toast.success("Saved");
            setEditing(null);
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        }
    };

    const remove = async (fid) => {
        if (!window.confirm("Delete this feedback permanently?")) return;
        try {
            await adminApi.delete(`/admin/feedback/${fid}`);
            toast.success("Deleted");
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        }
    };

    return (
        <div className="p-6 md:p-12 max-w-6xl mx-auto" data-testid="admin-feedback-page">
            <div className="mb-10 flex items-end justify-between flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Moderation Queue</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Client Feedback
                    </h1>
                    <p className="text-sm text-white/50 mt-3 max-w-xl inline-flex items-center gap-2">
                        <ShieldCheck className="w-4 h-4 text-[#c9a961]" />
                        You are the only relay between client and talent. Approve to
                        share, reject to discard, edit to refine the wording first.
                    </p>
                </div>
            </div>

            <div
                className="mb-6 flex items-center gap-2 flex-wrap border-b border-white/10 pb-3"
                data-testid="feedback-filters"
            >
                {FILTERS.map((f) => {
                    const active = filter === f.key;
                    return (
                        <button
                            key={f.key}
                            type="button"
                            onClick={() => setFilter(f.key)}
                            data-testid={`feedback-filter-${f.key}`}
                            className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-all ${
                                active
                                    ? "border-white bg-white text-black"
                                    : "border-white/15 text-white/60 hover:border-white/40 hover:text-white"
                            }`}
                        >
                            {f.label}
                            {active && (
                                <span className="ml-2 tg-mono text-black/60">
                                    {counts.current}
                                </span>
                            )}
                        </button>
                    );
                })}
            </div>

            {loading ? (
                <div className="py-16 text-center text-white/40 text-sm inline-flex items-center gap-2 justify-center w-full">
                    <Loader2 className="w-4 h-4 animate-spin" /> Loading…
                </div>
            ) : items.length === 0 ? (
                <div
                    className="py-16 text-center text-white/40 text-sm border border-white/10 rounded-sm"
                    data-testid="feedback-empty"
                >
                    {filter === "pending"
                        ? "Nothing waiting for moderation."
                        : "No feedback in this view."}
                </div>
            ) : (
                <div className="space-y-3" data-testid="feedback-list">
                    {items.map((fb) => (
                        <FeedbackCard
                            key={fb.id}
                            fb={fb}
                            project={meta.projects[fb.project_id]}
                            onApprove={() => approve(fb.id)}
                            onReject={() => reject(fb.id)}
                            onEdit={() =>
                                setEditing({ id: fb.id, text: fb.text || "" })
                            }
                            onDelete={() => remove(fb.id)}
                        />
                    ))}
                </div>
            )}

            {editing && (
                <EditModal
                    text={editing.text}
                    onChange={(t) => setEditing({ ...editing, text: t })}
                    onCancel={() => setEditing(null)}
                    onSave={saveEdit}
                />
            )}
        </div>
    );
}

function FeedbackCard({ fb, project, onApprove, onReject, onEdit, onDelete }) {
    const isVoice = fb.type === "voice";
    const status = fb.status || "pending";
    const statusColor = {
        pending: "text-white/60 border-white/15",
        approved: "text-[#34C759] border-[#34C759]/40",
        rejected: "text-[#FF3B30] border-[#FF3B30]/40",
    }[status];

    return (
        <div
            className="border border-white/10 p-5 hover:border-white/30 transition-all"
            data-testid={`feedback-card-${fb.id}`}
        >
            <div className="flex items-start justify-between gap-4 flex-wrap">
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap mb-1.5">
                        <span
                            className={`inline-flex items-center gap-1 text-[10px] tracking-widest uppercase px-2 py-0.5 border rounded-sm ${statusColor}`}
                            data-testid={`feedback-status-${fb.id}`}
                        >
                            {status === "pending" && <Clock className="w-3 h-3" />}
                            {status === "approved" && <Check className="w-3 h-3" />}
                            {status === "rejected" && <XCircle className="w-3 h-3" />}
                            {status}
                        </span>
                        <span className="inline-flex items-center gap-1 text-[10px] tracking-widest uppercase text-white/40">
                            {isVoice ? (
                                <Mic className="w-3 h-3" />
                            ) : (
                                <MessageCircle className="w-3 h-3" />
                            )}
                            {isVoice ? "Voice" : "Text"}
                        </span>
                    </div>
                    <div className="font-display text-lg truncate">
                        {fb.client_viewer_name || fb.client_viewer_email || "Client"}
                    </div>
                    <div className="text-[11px] tg-mono text-white/40 mt-0.5 truncate">
                        {project?.brand_name || "—"} · sub {fb.submission_id?.slice(0, 8)} · {timeAgo(fb.created_at)}
                    </div>
                </div>
                <div className="flex items-center gap-1 flex-wrap">
                    {status === "pending" && (
                        <>
                            <button
                                onClick={onApprove}
                                data-testid={`feedback-approve-${fb.id}`}
                                title="Approve & share"
                                className="inline-flex items-center gap-1.5 text-xs px-3 py-2 bg-[#34C759] text-black hover:opacity-90 rounded-sm"
                            >
                                <Check className="w-3.5 h-3.5" />
                                Approve & Share
                            </button>
                            {!isVoice && (
                                <button
                                    onClick={onEdit}
                                    data-testid={`feedback-edit-${fb.id}`}
                                    title="Edit text"
                                    className="inline-flex items-center text-xs px-3 py-2 border border-white/15 hover:border-white rounded-sm"
                                >
                                    <Edit3 className="w-3.5 h-3.5" />
                                </button>
                            )}
                            <button
                                onClick={onReject}
                                data-testid={`feedback-reject-${fb.id}`}
                                title="Reject"
                                className="inline-flex items-center text-xs px-3 py-2 border border-white/15 hover:border-[#FF3B30] hover:text-[#FF3B30] rounded-sm"
                            >
                                <XCircle className="w-3.5 h-3.5" />
                            </button>
                        </>
                    )}
                    <button
                        onClick={onDelete}
                        data-testid={`feedback-delete-${fb.id}`}
                        title="Delete"
                        className="inline-flex items-center text-xs px-3 py-2 border border-white/15 hover:border-white/40 text-white/40 rounded-sm"
                    >
                        <Trash2 className="w-3.5 h-3.5" />
                    </button>
                </div>
            </div>

            <div className="mt-4 bg-white/[0.03] border border-white/5 p-4 rounded-sm">
                {isVoice ? (
                    <audio
                        src={fb.content_url}
                        controls
                        className="w-full"
                        data-testid={`feedback-audio-${fb.id}`}
                    />
                ) : (
                    <p
                        className="text-sm text-white/85 whitespace-pre-wrap"
                        data-testid={`feedback-text-${fb.id}`}
                    >
                        {fb.text}
                    </p>
                )}
            </div>

            {fb.edited_at && (
                <p className="text-[10px] text-white/30 tg-mono mt-2">
                    Edited {timeAgo(fb.edited_at)} · approve to push the latest text
                </p>
            )}
            {fb.approved_at && (
                <p className="text-[10px] text-[#34C759]/80 tg-mono mt-2 inline-flex items-center gap-1">
                    <ExternalLink className="w-3 h-3" />
                    Shared with talent {timeAgo(fb.approved_at)}
                </p>
            )}
        </div>
    );
}

function EditModal({ text, onChange, onCancel, onSave }) {
    return (
        <div
            className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4"
            onClick={onCancel}
            data-testid="feedback-edit-modal"
        >
            <div
                className="bg-[#0a0a0a] border border-white/10 max-w-xl w-full"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="px-6 py-4 border-b border-white/10">
                    <p className="eyebrow">Edit feedback before sharing</p>
                </div>
                <div className="p-6">
                    <textarea
                        value={text}
                        onChange={(e) => onChange(e.target.value)}
                        rows={6}
                        maxLength={4000}
                        data-testid="feedback-edit-textarea"
                        className="w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none resize-none"
                    />
                    <p className="text-[10px] text-white/30 tg-mono mt-2">
                        {text.length} / 4000 — saving does NOT auto-share. Click
                        Approve & Share separately.
                    </p>
                </div>
                <div className="px-6 py-4 border-t border-white/10 flex items-center justify-end gap-2">
                    <button
                        onClick={onCancel}
                        className="text-xs text-white/50 hover:text-white px-3 py-2"
                        data-testid="feedback-edit-cancel"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onSave}
                        disabled={!text.trim()}
                        data-testid="feedback-edit-save"
                        className="bg-white text-black px-5 py-2 text-sm rounded-sm hover:opacity-90 disabled:opacity-40"
                    >
                        Save edit
                    </button>
                </div>
            </div>
        </div>
    );
}
