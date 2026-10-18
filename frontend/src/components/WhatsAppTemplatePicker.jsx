import React, { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { Search, Check, Loader2, MessageSquare } from "lucide-react";
import { toast } from "sonner";
import { getTemplates, getWorkers } from "@/lib/whatsappApi";

/**
 * WhatsAppTemplatePicker — the "Choose WhatsApp Template" + "Send via" step, shared by every place that
 * sends an existing WhatsApp template through POST /whatsapp/casting-call/send:
 *   - Global Talent → Add to Project → "Send Casting Call?" (AddToProjectModal)
 *   - Casting Pipeline → selected talents → "Send Template" (PipelineBoard)
 *
 * It was extracted unchanged from AddToProjectModal so both screens use the SAME template source
 * (GET /whatsapp/templates — no second catalogue), the SAME worker registry (GET /whatsapp/workers, polled
 * while open) and the SAME rules: nothing is sent until a template AND a connected worker are chosen; a worker
 * is selectable only when sending is enabled and its session is authenticated; with several connected workers
 * nothing is pre-selected. It owns no network call for the send itself — `onSend({ templateId, workerId })`
 * does that, so each caller keeps its own request shape and result handling.
 *
 * Ref: `clearWorkerChoice()` — call after the backend refused the chosen worker (409/403): drops the choice and
 * re-reads the live worker list so the admin picks another one (never auto-switched).
 */
// A worker can send only when sending is enabled for it AND its live session is authenticated.
export const isSendableWorker = (w) => w?.sending_enabled === true && w?.session?.status === "authenticated";
export const workerStatusLabel = (w) => {
    if (w?.sending_enabled !== true) return "Sending off";
    const st = w?.session?.status;
    return st === "authenticated" ? "Connected" : st === "qr_pending" ? "Scan QR" : "Disconnected";
};

const WhatsAppTemplatePicker = forwardRef(function WhatsAppTemplatePicker(
    { sending = false, onSend, onCancel, contextLabel = null },
    ref,
) {
    const [templates, setTemplates] = useState([]);
    const [loadingTemplates, setLoadingTemplates] = useState(false);
    const [templateSearch, setTemplateSearch] = useState("");
    const [selectedTemplateId, setSelectedTemplateId] = useState("");
    // "Send via" (2026-10-07): the WhatsApp worker the admin explicitly chooses. Rendered from the
    // live worker registry (GET /whatsapp/workers), so any number of workers shows up — never a
    // hard-coded pair. Nothing is chosen for the admin when several are available.
    const [workers, setWorkers] = useState([]);
    const [loadingWorkers, setLoadingWorkers] = useState(false);
    const [selectedWorkerId, setSelectedWorkerId] = useState("");
    const templateSearchInputRef = useRef(null);

    // Fetch the existing WhatsApp templates on demand — only once the picker is actually shown.
    // Same GET /whatsapp/templates call the WhatsApp Engine campaign launcher uses.
    useEffect(() => {
        let isMounted = true;
        setLoadingTemplates(true);
        getTemplates()
            .then((data) => {
                if (isMounted) setTemplates(Array.isArray(data) ? data : []);
            })
            .catch(() => {
                if (isMounted) toast.error("Failed to load WhatsApp templates");
            })
            .finally(() => {
                if (isMounted) setLoadingTemplates(false);
            });
        setTimeout(() => templateSearchInputRef.current?.focus(), 50);
        return () => { isMounted = false; };
    }, []);

    // A worker can be chosen only while it can actually send: sending enabled AND its live session
    // authenticated (the status the WhatsApp Engine shows as "Connected").
    const refreshWorkers = useCallback(() => {
        return getWorkers()
            .then((list) => {
                const arr = Array.isArray(list) ? list : [];
                setWorkers(arr);
                setSelectedWorkerId((cur) => {
                    const ready = arr.filter(isSendableWorker);
                    if (cur && ready.some((w) => w.id === cur)) return cur;   // keep a still-valid choice
                    return ready.length === 1 ? ready[0].id : "";              // only ONE available -> preselect; else the admin must choose
                });
            })
            .catch(() => {})
            .finally(() => setLoadingWorkers(false));
    }, []);

    // Same moment the template list loads; re-polled slowly so a worker that drops while the picker is
    // open can't stay selected.
    useEffect(() => {
        setLoadingWorkers(true);
        refreshWorkers();
        const timer = setInterval(refreshWorkers, 8000);
        return () => clearInterval(timer);
    }, [refreshWorkers]);

    useImperativeHandle(ref, () => ({
        clearWorkerChoice() {
            setSelectedWorkerId("");
            refreshWorkers();
        },
    }), [refreshWorkers]);

    const filteredTemplates = useMemo(() => {
        const q = templateSearch.trim().toLowerCase();
        if (!q) return templates;
        return templates.filter((t) => (t.name || "").toLowerCase().includes(q));
    }, [templates, templateSearch]);

    const submit = () => {
        // Required, no silent fallback: the Send button is already disabled until a template and a worker are
        // picked, but this guard keeps the invariant true even if this is ever called some other way.
        if (sending || !selectedTemplateId || !selectedWorkerId) return;
        onSend({ templateId: selectedTemplateId, workerId: selectedWorkerId });
    };

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
            onClick={sending ? undefined : onCancel}
        >
            <div
                className="bg-white rounded-2xl p-6 max-w-md w-full shadow-2xl border border-black/[0.08] text-black relative flex flex-col max-h-[80vh]"
                onClick={(e) => e.stopPropagation()}
                data-testid="template-picker-modal"
            >
                <div className="flex items-center gap-2 mb-4 shrink-0">
                    <MessageSquare className="w-4 h-4 text-black/50" />
                    <h3 className="font-semibold text-sm text-neutral-800">Choose WhatsApp Template</h3>
                </div>
                {contextLabel && (
                    <p className="text-xs text-black/55 -mt-2 mb-3 shrink-0" data-testid="template-picker-context">{contextLabel}</p>
                )}

                <div className="relative mb-3 shrink-0">
                    <Search className="absolute left-0 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-black/30" />
                    <input
                        ref={templateSearchInputRef}
                        type="text"
                        value={templateSearch}
                        onChange={(e) => setTemplateSearch(e.target.value)}
                        placeholder="Search templates..."
                        data-testid="template-picker-search"
                        className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2 pl-5 text-sm text-black/85 placeholder:text-black/30"
                    />
                </div>

                <div
                    className="flex-1 overflow-y-auto mb-4 border border-black/[0.04] rounded-lg divide-y divide-black/[0.04] min-h-[120px]"
                    data-testid="template-picker-list"
                >
                    {loadingTemplates ? (
                        <div className="flex items-center justify-center py-8">
                            <Loader2 className="w-4 h-4 animate-spin text-black/40" />
                        </div>
                    ) : filteredTemplates.length === 0 ? (
                        <div className="p-4 text-xs text-black/30 italic text-center">
                            {templates.length === 0 ? "No WhatsApp templates found" : "No templates match your search"}
                        </div>
                    ) : (
                        filteredTemplates.map((t) => {
                            const isSelected = selectedTemplateId === t.id;
                            return (
                                <button
                                    key={t.id}
                                    type="button"
                                    onClick={() => setSelectedTemplateId(t.id)}
                                    data-testid={`template-picker-option-${t.id}`}
                                    className={[
                                        "w-full text-left px-3.5 py-2.5 text-xs flex items-center gap-2.5 transition-colors",
                                        isSelected ? "bg-black/[0.03]" : "hover:bg-black/[0.02]",
                                    ].join(" ")}
                                >
                                    <span
                                        className={[
                                            "w-4 h-4 rounded-full border flex items-center justify-center shrink-0",
                                            isSelected ? "bg-black border-black text-white" : "border-black/25 text-transparent",
                                        ].join(" ")}
                                    >
                                        {isSelected && <Check className="w-3 h-3" strokeWidth={2.5} />}
                                    </span>
                                    <span className="font-medium text-neutral-800 truncate">{t.name || "Untitled template"}</span>
                                </button>
                            );
                        })
                    )}
                </div>

                <div className="mb-4 shrink-0" data-testid="send-via">
                    <p className="text-[10px] tracking-widest uppercase text-black/40 mb-2">Send via</p>
                    {loadingWorkers && workers.length === 0 ? (
                        <div className="flex items-center gap-2 text-xs text-black/40"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Checking WhatsApp workers…</div>
                    ) : (
                        <>
                            <div role="radiogroup" aria-label="Send via" className="flex flex-wrap gap-2">
                                {workers.map((w) => {
                                    const ready = isSendableWorker(w);
                                    const chosen = selectedWorkerId === w.id;
                                    return (
                                        <button
                                            key={w.id}
                                            type="button"
                                            role="radio"
                                            aria-checked={chosen}
                                            disabled={!ready || sending}
                                            onClick={() => setSelectedWorkerId(w.id)}
                                            data-testid={`send-via-${w.id}`}
                                            className={[
                                                "inline-flex items-center gap-2 px-3 min-h-[40px] rounded-lg border text-xs transition-colors",
                                                chosen ? "bg-black text-white border-black" : "bg-white border-black/[0.12] text-black/80 hover:border-black/40",
                                                !ready ? "opacity-45 cursor-not-allowed hover:border-black/[0.12]" : "",
                                            ].join(" ")}
                                        >
                                            <span className={["w-1.5 h-1.5 rounded-full shrink-0", ready ? "bg-emerald-500" : "bg-red-400"].join(" ")} />
                                            <span className="font-medium">{w.label || w.id}</span>
                                            <span className={chosen ? "text-white/70" : "text-black/45"}>{workerStatusLabel(w)}</span>
                                        </button>
                                    );
                                })}
                            </div>
                            {!loadingWorkers && !workers.some(isSendableWorker) && (
                                <p className="mt-2 text-xs text-red-600/90" data-testid="send-via-none">
                                    No WhatsApp worker is currently connected. Connect a worker in WhatsApp Engine, then try again.
                                </p>
                            )}
                        </>
                    )}
                </div>

                <div className="flex gap-2 shrink-0">
                    <button
                        onClick={onCancel}
                        disabled={sending}
                        data-testid="template-picker-cancel"
                        className="flex-1 py-2.5 border border-black/[0.08] hover:bg-black/[0.02] text-black/60 hover:text-black text-xs font-medium rounded-lg transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={submit}
                        disabled={sending || !selectedTemplateId || !selectedWorkerId}
                        data-testid="template-picker-send"
                        className="flex-1 py-2.5 bg-black text-white text-xs font-medium rounded-lg transition-colors disabled:opacity-40 hover:bg-black/85 flex items-center justify-center gap-1.5"
                    >
                        {sending && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                        Send
                    </button>
                </div>
            </div>
        </div>
    );
});

export default WhatsAppTemplatePicker;
