import React, { useEffect, useRef, useState } from "react";
import { X, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";
import { CALL_RESULT_OPTIONS, UPDATE_STATUS_OPTIONS, genId } from "@/lib/calls";

// Fast call entry. One client-generated id is minted per modal open and reused on every Save attempt (the
// server's idempotency key), and Save locks immediately. The call is one phone conversation, so it is also
// recorded against the talent's other ongoing-project calls — this modal says which ones, before saving.
export default function RecordCallModal({ row, onClose, onSaved }) {
    const [callId] = useState(genId());
    const [result, setResult] = useState(null);
    const [updateStatus, setUpdateStatus] = useState(null);
    const [note, setNote] = useState("");
    const [saving, setSaving] = useState(false);
    const lock = useRef(false);                      // synchronous: a second tap in the same tick cannot slip past `saving` state
    const [others, setOthers] = useState(null);       // null = still loading
    const [sync, setSync] = useState(true);

    useEffect(() => {
        if (!row.other_projects_count) { setOthers([]); return undefined; }
        let cancelled = false;
        adminApi.get(`/workflow/calls/${row.talent_id}/${row.project_id}/context`)
            .then(({ data }) => { if (!cancelled) setOthers(data.other_projects || []); })
            .catch(() => { if (!cancelled) setOthers([]); });
        return () => { cancelled = true; };
    }, [row.talent_id, row.project_id, row.other_projects_count]);

    const handleSave = async () => {
        if (!result || saving || lock.current) return;
        lock.current = true;
        setSaving(true);
        try {
            const { data } = await adminApi.post("/workflow/calls", {
                id: callId,
                talent_id: row.talent_id,
                project_id: row.project_id,
                call_result: result,
                update_status: updateStatus || null,
                update_text: note.trim() || null,
                sync_other_projects: sync,
            });
            const synced = data.synced_projects || [];
            toast.success(synced.length
                ? `Call logged — also recorded for ${synced.map((s) => s.project_name).join(", ")}`
                : "Call logged");
            onSaved(data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to save call");
            lock.current = false;
            setSaving(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
            <div className="w-full max-w-sm max-h-[90vh] overflow-y-auto bg-white rounded-md shadow-xl p-4 space-y-4" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Record call">
                <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                        <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Record Call</p>
                        <p className="text-[11px] text-black/45 break-words">{row.talent_name} — {row.project_name}</p>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full text-black/40 hover:text-black hover:bg-black/[0.04] shrink-0" aria-label="Close"><X className="w-4 h-4" /></button>
                </div>

                <div>
                    <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">Result</p>
                    <div className="flex flex-wrap gap-1.5">
                        {CALL_RESULT_OPTIONS.map((o) => (
                            <button key={o.id} onClick={() => { setResult(o.id); if (o.id !== "answered") setUpdateStatus(null); }}
                                className={`px-2.5 py-1.5 rounded-sm text-xs border ${result === o.id ? "bg-black text-white border-black" : "border-black/[0.1] hover:border-black/25"}`}>
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
                                <button key={o.id} onClick={() => setUpdateStatus(updateStatus === o.id ? null : o.id)}
                                    className={`px-2.5 py-1.5 rounded-sm text-xs border ${updateStatus === o.id ? "bg-black text-white border-black" : "border-black/[0.1] hover:border-black/25"}`}>
                                    {o.label}
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                <div>
                    <p className="text-[10px] uppercase font-bold text-black/45 mb-1.5">Note (optional)</p>
                    <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Add a note about this call..."
                        className="w-full text-xs px-2.5 py-2 border border-black/[0.08] rounded-sm focus:outline-none focus:border-black/30" />
                </div>

                {others && others.length > 0 && (
                    <label className="flex items-start gap-2 rounded-sm bg-sky-50/60 border border-sky-100 px-2.5 py-2 text-[11px] text-black/65" data-testid="record-sync">
                        <input type="checkbox" className="mt-0.5" checked={sync} onChange={(e) => setSync(e.target.checked)} />
                        <span>
                            Also record this call for {others.length} other ongoing project{others.length !== 1 ? "s" : ""}:{" "}
                            <b className="font-medium text-black/80">{others.map((o) => o.project_name).join(", ")}</b>.
                            <span className="block text-black/40">Pipeline stages and project decisions are not changed.</span>
                        </span>
                    </label>
                )}

                <button onClick={handleSave} disabled={!result || saving} data-testid="record-save"
                    className="w-full px-3.5 py-2 bg-black text-white hover:bg-black/95 disabled:opacity-40 rounded-sm text-xs font-semibold uppercase tracking-wider inline-flex items-center justify-center gap-1.5 focus:outline-none">
                    {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    Save
                </button>
            </div>
        </div>
    );
}
