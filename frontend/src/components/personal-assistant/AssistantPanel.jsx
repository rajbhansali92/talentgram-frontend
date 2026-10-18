import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { toast } from "sonner";
import { Loader2, Maximize2, Mic, Minimize2, Minus, Send, Sparkles, Volume2, VolumeX, X } from "lucide-react";
import {
    ACTIVE_STATUSES, ATTENTION_STATUSES, cancelTask, clarifyTask, clearAssistantHistory, confirmAll, confirmTask, followUpSend,
    getAssistantHistory, listAssistantTasks, newRequestId, projectIdFromPath, retryTask, sendAssistantMessage,
} from "@/lib/personalAssistant";
import { useAssistantContext } from "@/lib/assistantContext";
import { speak, speechOutputSupported, useSpeechInput } from "@/hooks/useSpeechInput";
import TaskCard, { StatusChip, WorkerPicker } from "./TaskCard";

const EXAMPLES = [
    "Show me Angela Kumar's profile",
    "Add these selected talents to the current project",
    "Send the casting call to the selected talents",
    "Show me everyone in Ask to Test",
];

const readPref = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v === "1"; } catch { return d; } };
const writePref = (k, v) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch { /* storage blocked */ } };

function useKeyboardFit(open) {
    // iOS Safari doesn't shrink the layout viewport for the keyboard: keep the panel inside the VISIBLE viewport.
    const [fit, setFit] = useState(null);
    useEffect(() => {
        const vv = typeof window !== "undefined" ? window.visualViewport : null;
        if (!open || !vv) return undefined;
        const update = () => setFit({ height: vv.height, bottom: Math.max(0, window.innerHeight - vv.height - vv.offsetTop) });
        update();
        vv.addEventListener("resize", update);
        vv.addEventListener("scroll", update);
        return () => { vv.removeEventListener("resize", update); vv.removeEventListener("scroll", update); };
    }, [open]);
    return fit;
}

export default function AssistantPanel({ open, onClose, onAttention, lifted = false }) {
    const { pathname } = useLocation();
    const ctx = useAssistantContext();
    const [messages, setMessages] = useState([]);
    const [tasks, setTasks] = useState([]);
    const [text, setText] = useState("");
    const [sending, setSending] = useState(false);
    const [failed, setFailed] = useState(null);                  // {text, id}: a retry reuses the id → the server dedupes
    const [tab, setTab] = useState("chat");
    const [minimized, setMinimized] = useState(false);
    const [expanded, setExpanded] = useState(false);
    const [voiceOut, setVoiceOut] = useState(() => readPref("pa_voice_out", false));
    const [busy, setBusy] = useState({});
    const [errors, setErrors] = useState({});
    const [batchWorker, setBatchWorker] = useState("");
    const inputRef = useRef(null);
    const endRef = useRef(null);
    const loaded = useRef(false);
    const fit = useKeyboardFit(open && !minimized);

    const speech = useSpeechInput({ onFinal: (t) => setText((cur) => (cur ? `${cur} ${t}` : t)) });

    // history + persisted tasks once, the first time it opens (survives refresh: both live on the backend)
    useEffect(() => {
        if (!open || loaded.current) return;
        loaded.current = true;
        Promise.all([getAssistantHistory().catch(() => []), listAssistantTasks(40).catch(() => [])]).then(([h, t]) => {
            setMessages((cur) => (cur.length ? cur : h));
            setTasks(t);
        });
    }, [open]);

    // status of queued/running work is polled from the backend — never inferred in the browser
    const hasActive = tasks.some((t) => ACTIVE_STATUSES.includes(t.status));
    useEffect(() => {
        if (!open || !hasActive) return undefined;
        const id = setInterval(() => listAssistantTasks(40).then(setTasks).catch(() => {}), 4000);
        return () => clearInterval(id);
    }, [open, hasActive]);

    const attention = tasks.filter((t) => ATTENTION_STATUSES.includes(t.status)).length;
    useEffect(() => { onAttention?.(attention); }, [attention, onAttention]);

    useEffect(() => { if (open && !minimized && messages.length > 0) { endRef.current?.scrollIntoView?.({ block: "end" }); } }, [messages, tasks, open, minimized, tab]);
    useEffect(() => { if (open && !minimized) setTimeout(() => inputRef.current?.focus(), 60); }, [open, minimized]);

    const mergeTask = useCallback((t) => setTasks((cur) => (cur.some((x) => x.id === t.id) ? cur.map((x) => (x.id === t.id ? t : x)) : [t, ...cur])), []);

    const context = useMemo(() => ({ project_id: ctx.projectId || projectIdFromPath(pathname) || null, talent_ids: ctx.talentIds || [], page: pathname }), [ctx, pathname]);

    const submit = useCallback(async (override) => {
        const body = (override ?? text).trim();
        if (!body || sending) return;
        const rid = failed && failed.text === body ? failed.id : newRequestId();
        setSending(true);
        setFailed(null);
        if (!(failed && failed.text === body)) setMessages((cur) => [...cur, { id: `u-${rid}`, role: "user", text: body }]);
        setText("");
        try {
            const out = await sendAssistantMessage({ text: body, clientRequestId: rid, context });
            setMessages((cur) => [...cur, { id: `a-${out.command_id}`, role: "assistant", text: out.reply, command_id: out.command_id }]);
            setTasks((cur) => [...out.tasks, ...cur.filter((x) => !out.tasks.some((n) => n.id === x.id))]);
            if (voiceOut) speak(out.reply);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            const msg = typeof detail === "string" ? detail : "I couldn't reach the server. Nothing was done — tap Retry.";
            setMessages((cur) => [...cur, { id: `e-${rid}`, role: "assistant", text: msg, error: true }]);
            setFailed({ text: body, id: rid });
            setText(body);
        } finally {
            setSending(false);
        }
    }, [text, sending, failed, context, voiceOut]);

    const act = useCallback(async (taskId, fn) => {
        if (busy[taskId]) return;
        setBusy((b) => ({ ...b, [taskId]: true }));
        setErrors((e) => ({ ...e, [taskId]: null }));
        try {
            mergeTask(await fn());
        } catch (err) {
            const detail = err?.response?.data?.detail;
            setErrors((e) => ({ ...e, [taskId]: typeof detail === "string" ? detail : "That didn't work — nothing was changed." }));
            listAssistantTasks(40).then(setTasks).catch(() => {});
        } finally {
            setBusy((b) => ({ ...b, [taskId]: false }));
        }
    }, [busy, mergeTask]);

    const onClarify = (t, slot, body) => act(t.id, async () => {
        const updated = await clarifyTask(t.id, { slot, ...body });
        listAssistantTasks(40).then(setTasks).catch(() => {});      // dependents of this task may have changed too
        return updated;
    });
    const onConfirm = (t, worker) => act(t.id, () => confirmTask(t.id, worker));
    const onCancel = (t) => act(t.id, () => cancelTask(t.id));
    const onRetry = (t) => act(t.id, () => retryTask(t.id));
    const onFollowUp = (t, templateId) => act(t.id, () => followUpSend(t.id, templateId));

    const doConfirmAll = async (commandId, ids) => {
        ids.forEach((i) => setBusy((b) => ({ ...b, [i]: true })));
        try {
            await confirmAll(commandId, batchWorker);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Couldn't confirm those.");
        } finally {
            setTasks(await listAssistantTasks(40).catch(() => tasks));
            ids.forEach((i) => setBusy((b) => ({ ...b, [i]: false })));
        }
    };

    const clearChat = async () => {
        try { await clearAssistantHistory(); setMessages([]); } catch { toast.error("Couldn't clear the conversation."); }
    };

    const tasksFor = (commandId) => tasks.filter((t) => t.command_id === commandId).sort((a, b) => a.seq - b.seq);
    const status = speech.listening ? "Listening…" : sending ? "Working on it…" : tasks.some((t) => t.status === "needs_clarification") ? "Needs your pick"
        : tasks.some((t) => t.status === "awaiting_confirmation") ? "Waiting for your OK" : hasActive ? "Running in the background" : "Ready";

    if (!open) return null;
    const mobileStyle = fit ? { maxHeight: fit.height - 12, bottom: fit.bottom + 6 } : undefined;
    return (
        <section role="dialog" aria-label="Talentgram assistant" data-testid="pa-panel" style={mobileStyle}
            className={`fixed z-40 right-2 left-2 bottom-2 sm:left-auto sm:right-6 ${lifted ? "sm:bottom-28" : "sm:bottom-6"} ${expanded ? "sm:w-[560px]" : "sm:w-[400px]"} flex flex-col bg-white border border-black/[0.12] rounded-2xl shadow-[0_12px_40px_rgba(0,0,0,0.22)] overflow-hidden ${minimized ? "" : expanded ? "h-[min(88dvh,820px)]" : lifted ? "h-[min(70dvh,640px)]" : "h-[min(78dvh,640px)]"}`}
            onKeyDown={(e) => { if (e.key === "Escape" && !speech.listening) onClose(); }}>
            <header className="flex items-center gap-2 px-3 py-2.5 border-b border-black/[0.08] shrink-0">
                <Sparkles className="w-4 h-4 text-black/60" aria-hidden />
                <div className="min-w-0 flex-1">
                    <h2 className="text-sm font-semibold text-black/90 leading-tight">Assistant</h2>
                    <p className="text-[11px] text-black/50 truncate" data-testid="pa-state" aria-live="polite">{status}</p>
                </div>
                {speechOutputSupported() && (
                    <button type="button" onClick={() => { setVoiceOut((v) => { writePref("pa_voice_out", !v); return !v; }); }} aria-pressed={voiceOut} aria-label={voiceOut ? "Turn spoken replies off" : "Turn spoken replies on"}
                        className="p-2 rounded-lg hover:bg-black/[0.05] text-black/55">{voiceOut ? <Volume2 className="w-4 h-4" /> : <VolumeX className="w-4 h-4" />}</button>
                )}
                <button type="button" onClick={() => setExpanded((v) => !v)} aria-label={expanded ? "Smaller panel" : "Larger panel"} className="hidden sm:block p-2 rounded-lg hover:bg-black/[0.05] text-black/55">{expanded ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}</button>
                <button type="button" onClick={() => setMinimized((v) => !v)} aria-label={minimized ? "Expand assistant" : "Minimize assistant"} data-testid="pa-minimize" className="p-2 rounded-lg hover:bg-black/[0.05] text-black/55"><Minus className="w-4 h-4" /></button>
                <button type="button" onClick={onClose} aria-label="Close assistant" data-testid="pa-close" className="p-2 rounded-lg hover:bg-black/[0.05] text-black/55"><X className="w-4 h-4" /></button>
            </header>

            {!minimized && (
                <>
                    <nav className="flex gap-1 px-3 pt-2 shrink-0" aria-label="Assistant sections">
                        {[["chat", "Chat"], ["tasks", `Tasks${tasks.length ? ` (${tasks.length})` : ""}`]].map(([k, label]) => (
                            <button key={k} type="button" onClick={() => setTab(k)} aria-pressed={tab === k} data-testid={`pa-tab-${k}`}
                                className={`px-3 min-h-[32px] rounded-full text-xs ${tab === k ? "bg-black text-white" : "text-black/60 hover:bg-black/[0.05]"}`}>{label}</button>
                        ))}
                        {attention > 0 && <span className="ml-auto self-center text-[11px] text-amber-700" data-testid="pa-attention">{attention} need{attention === 1 ? "s" : ""} you</span>}
                    </nav>

                    <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3 min-h-0" data-testid="pa-scroll">
                        {tab === "chat" ? (
                            <>
                                {messages.length === 0 && (
                                    <div className="text-sm text-black/60" data-testid="pa-empty">
                                        <p>Hi — tell me what to do. You can type or tap the mic.</p>
                                        <ul className="mt-2 flex flex-wrap gap-1.5">{EXAMPLES.map((e) => <li key={e}><button type="button" onClick={() => { setText(e); inputRef.current?.focus(); }} className="px-2.5 min-h-[32px] rounded-full border border-black/[0.12] text-[11px] text-black/65 hover:border-black/40">{e}</button></li>)}</ul>
                                    </div>
                                )}
                                {messages.map((m) => {
                                    const ts = m.command_id && m.role === "assistant" ? tasksFor(m.command_id) : [];
                                    const waiting = ts.filter((t) => t.status === "awaiting_confirmation");
                                    const needsW = waiting.some((t) => t.kind === "send" || t.kind === "share");
                                    return (
                                        <div key={m.id} className={m.role === "user" ? "flex justify-end" : ""}>
                                            <div className={m.role === "user" ? "max-w-[85%] bg-black text-white rounded-2xl rounded-br-md px-3 py-2 text-sm" : "max-w-full w-full"}>
                                                {m.role === "user" ? m.text : (
                                                    <>
                                                        <p className={`text-sm ${m.error ? "text-red-700" : "text-black/80"}`}>{m.text}</p>
                                                        {m.error && failed && <button type="button" onClick={() => submit(failed.text)} className="mt-1 text-xs underline" data-testid="pa-retry-send">Retry</button>}
                                                        {ts.length > 0 && (
                                                            <div className="mt-2 space-y-2">
                                                                {ts.map((t) => <TaskCard key={t.id} task={t} busy={!!busy[t.id]} actionError={errors[t.id]} onClarify={onClarify} onConfirm={onConfirm} onCancel={onCancel} onRetry={onRetry} onFollowUp={onFollowUp} onPrefill={(v) => { setText(v); inputRef.current?.focus(); }} />)}
                                                                {waiting.length > 1 && (
                                                                    <div className="rounded-xl border border-black/[0.1] p-3 bg-black/[0.02] space-y-2" data-testid="pa-confirm-all">
                                                                        {needsW && <WorkerPicker value={batchWorker} onChange={setBatchWorker} disabled={false} />}
                                                                        <button type="button" disabled={needsW && !batchWorker} onClick={() => doConfirmAll(m.command_id, waiting.map((t) => t.id))} className="px-3.5 min-h-[40px] rounded-lg bg-black text-white text-xs font-medium disabled:opacity-40">Confirm all {waiting.length}</button>
                                                                    </div>
                                                                )}
                                                            </div>
                                                        )}
                                                    </>
                                                )}
                                            </div>
                                        </div>
                                    );
                                })}
                                {sending && <p className="text-xs text-black/50 flex items-center gap-1.5"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Working on it…</p>}
                            </>
                        ) : (
                            <ul className="space-y-2" data-testid="pa-queue">
                                {tasks.length === 0 && <li className="text-sm text-black/50">No tasks yet.</li>}
                                {tasks.map((t) => (
                                    <li key={t.id} className="rounded-lg border border-black/[0.08] p-2.5">
                                        <div className="flex items-start justify-between gap-2"><span className="text-xs text-black/85 leading-snug">{t.title}</span><StatusChip status={t.status} /></div>
                                        <p className="text-[11px] text-black/45 mt-1">{t.status_detail || t.error || ""}{t.params?.worker_id ? `${t.status_detail || t.error ? " · " : ""}worker ${t.params.worker_id}` : ""}{t.updated_at ? ` · ${new Date(t.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</p>
                                    </li>
                                ))}
                            </ul>
                        )}
                        <div ref={endRef} />
                    </div>

                    <form className="border-t border-black/[0.08] p-2.5 shrink-0" onSubmit={(e) => { e.preventDefault(); submit(); }}>
                        {speech.listening && <p className="text-xs text-red-600 mb-1.5 flex items-center gap-1.5" data-testid="pa-listening"><span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" /> Listening… {speech.interim || "speak now"}</p>}
                        {speech.error && <p className="text-xs text-amber-700 mb-1.5" role="alert" data-testid="pa-voice-error">{speech.error}</p>}
                        {!speech.supported && <p className="text-[11px] text-black/45 mb-1.5" data-testid="pa-voice-unsupported">Voice input isn&apos;t available in this browser — type your command instead.</p>}
                        <div className="flex items-end gap-2">
                            <textarea ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} rows={1} maxLength={2000} placeholder="Type a command…" aria-label="Command"
                                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
                                className="flex-1 min-w-0 resize-none max-h-28 border border-black/[0.12] rounded-xl px-3 py-2.5 text-base sm:text-sm focus:outline-none focus:border-black/50" />
                            <button type="button" onClick={speech.toggle} disabled={!speech.supported} aria-pressed={speech.listening} aria-label={speech.listening ? "Stop listening" : "Start voice input"} data-testid="pa-mic"
                                className={`w-11 h-11 shrink-0 rounded-xl border flex items-center justify-center ${speech.listening ? "bg-red-500 text-white border-red-500" : "border-black/[0.15] text-black/70"} disabled:opacity-40`}><Mic className="w-4 h-4" /></button>
                            <button type="submit" disabled={!text.trim() || sending} aria-label="Send" data-testid="pa-send" className="w-11 h-11 shrink-0 rounded-xl bg-black text-white flex items-center justify-center disabled:opacity-40"><Send className="w-4 h-4" /></button>
                        </div>
                        <div className="flex items-center justify-between mt-1.5">
                            <p className="text-[10px] text-black/40 truncate">{ctx.talentIds?.length ? `${ctx.talentIds.length} selected talent${ctx.talentIds.length === 1 ? "" : "s"} on screen` : "No talents selected on screen"}</p>
                            {messages.length > 0 && <button type="button" onClick={clearChat} className="text-[11px] text-black/50 underline min-h-[32px] px-1">Clear chat</button>}
                        </div>
                    </form>
                </>
            )}
        </section>
    );
}
