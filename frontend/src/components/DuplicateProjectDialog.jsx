import React, { useEffect, useRef, useState } from "react";
import { Copy, Loader2, X } from "lucide-react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";

const MAX_NAME = 200;

function newRequestId() {
    return (typeof crypto !== "undefined" && crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// Duplicate a project: its configuration, submission requirements and audition material, with a fresh
// submission link — and no talents, pipeline or operational records. One request id is minted per dialog open
// and reused on every attempt, so a double click or a retry after a lost response can never create two
// projects (the server returns the one that already exists). The Duplicate button locks synchronously.
export default function DuplicateProjectDialog({ open, project, onCancel, onCreated }) {
    const sourceName = project?.brand_name || "Untitled";
    const [name, setName] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const lock = useRef(false);
    const requestId = useRef(null);

    useEffect(() => {
        if (open) {
            setName(`${sourceName} (Copy)`);
            setError("");
            setBusy(false);
            lock.current = false;
            requestId.current = newRequestId();
        }
    }, [open, sourceName]);

    useEffect(() => {
        if (!open) return undefined;
        const onKey = (e) => { if (e.key === "Escape" && !lock.current) onCancel(); };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [open, onCancel]);

    if (!open) return null;

    const trimmed = name.trim();
    const invalid = !trimmed || trimmed.length > MAX_NAME;

    const run = async () => {
        if (invalid || lock.current) return;
        lock.current = true;
        setBusy(true);
        setError("");
        try {
            const { data } = await adminApi.post(`/projects/${project.id}/duplicate`, { name: trimmed, request_id: requestId.current });
            toast.success(`Duplicated as "${data.brand_name}"`);
            await onCreated(data);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            setError(typeof detail === "string" ? detail : "Could not duplicate the project. Nothing was created — please try again.");
            lock.current = false;
            setBusy(false);
        }
    };

    return (
        <div className="fixed inset-0 z-[60] bg-black/70 backdrop-blur flex items-center justify-center p-4" data-testid="duplicate-project-dialog" role="dialog" aria-modal="true" aria-label="Duplicate project">
            <div className="w-full max-w-md max-h-[90vh] overflow-y-auto border border-border bg-background p-6 md:p-7 rounded-sm relative">
                <button type="button" onClick={onCancel} disabled={busy} className="absolute top-4 right-4 text-muted-foreground hover:text-foreground disabled:opacity-40" aria-label="Close" data-testid="duplicate-project-close">
                    <X className="w-4 h-4" />
                </button>
                <p className="eyebrow mb-1">Duplicate project</p>
                <h3 className="font-display text-xl leading-tight mb-1 break-words pr-6">{sourceName}</h3>
                <p className="text-sm text-muted-foreground mb-5 leading-relaxed">
                    Creates a new project with the same details, submission requirements and audition material, and its own submission link.
                    No talents, casting pipeline, submissions or other activity are copied. The original is not changed.
                </p>
                <label className="block mb-2">
                    <span className="text-[11px] tracking-widest uppercase text-muted-foreground">Name of the new project</span>
                    <input
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") run(); }}
                        autoFocus
                        disabled={busy}
                        maxLength={MAX_NAME + 20}
                        data-testid="duplicate-project-name"
                        className="mt-2 w-full bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm"
                    />
                </label>
                {trimmed.length > MAX_NAME && <p className="text-xs text-red-600 mb-2">Keep the name to {MAX_NAME} characters or fewer.</p>}
                {error && <p className="text-xs text-red-600 mb-2" role="alert" data-testid="duplicate-project-error">{error}</p>}
                <div className="flex gap-2 mt-5">
                    <button type="button" onClick={onCancel} disabled={busy} className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm disabled:opacity-40" data-testid="duplicate-project-cancel">
                        Cancel
                    </button>
                    <button type="button" onClick={run} disabled={invalid || busy} className="flex-1 bg-black text-white py-2.5 rounded-sm text-sm inline-flex items-center justify-center gap-2 disabled:opacity-40" data-testid="duplicate-project-confirm">
                        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Copy className="w-3.5 h-3.5" />}
                        {busy ? "Duplicating…" : "Duplicate"}
                    </button>
                </div>
            </div>
        </div>
    );
}
