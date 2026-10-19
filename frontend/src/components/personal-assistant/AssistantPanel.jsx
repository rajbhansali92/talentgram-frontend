import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { toast } from "sonner";
import { Loader2, Maximize2, Mic, Minimize2, Minus, Paperclip, Send, Sparkles, Volume2, VolumeX, X } from "lucide-react";
import {
    ACTIVE_STATUSES, ATTENTION_STATUSES, cancelCommand, cancelTask, changedPlan, clarifyTask, errorDetail, clearAssistantHistory, confirmAll, confirmTask, followUpSend,
    fileKey, getAssistantHistory, getAssistantWorkers, isTimeoutError, listAssistantTasks, newRequestId, projectIdFromPath, retryTask, sendAssistantMessage, statusLabel, uploadUpdateMedia,
} from "@/lib/personalAssistant";
import { useAssistantContext } from "@/lib/assistantContext";
import { speak, speechOutputSupported, useSpeechInput } from "@/hooks/useSpeechInput";
import TaskCard, { StatusChip, WorkerPicker } from "./TaskCard";

const EXAMPLES = [
    "Update Angela Kumar's age to 24",
    "Show me Angela Kumar's profile",
    "Add these selected talents to the current project",
    "Send the casting call to the selected talents",
    "Show me everyone in Ask to Test",
];

// The reply text saved with a message describes the moment it was planned ("1 needs your pick"). Once the user has answered, that
// line would be stale — so it is recomputed from the tasks' CURRENT statuses. Messages without tasks (errors) keep their text.
export function liveReply(saved, tasks) {
    if (!tasks.length) return saved;
    const n = (st) => tasks.filter((t) => t.status === st).length;
    const need = n("needs_clarification"); const conf = n("awaiting_confirmation");
    const busy = tasks.filter((t) => ["queued", "running"].includes(t.status)).length;
    const bits = [];
    if (need) bits.push(`${need} need${need === 1 ? "s" : ""} your pick`);
    if (conf) bits.push(`${conf} waiting for your OK`);
    if (busy) bits.push(`${busy} in progress`);
    const tail = (saved || "").match(/ I didn't understand: .*$/)?.[0] || "";
    if (bits.length) return `Here's what I understood: ${bits.join(", ")}.${tail}`;
    const failed = n("failed") + n("partial");
    if (n("cancelled") === tasks.length) return "Cancelled — nothing was run or changed." + tail;
    return (failed ? "Finished — some steps need attention, see below." : n("cancelled") ? "Done — the cancelled steps were not run." : "All done — see below.") + tail;
}

// Codes only (never audio or words): what the browser said, so a failure report says WHY it failed.
export function describeVoiceDiagnostics(d) {
    const pol = d.policy === false ? "blocked by site policy" : d.policy === true ? "allowed" : "unknown";
    return [d.error ? `error ${d.error}` : d.result ? d.result : null, d.permission ? `browser permission ${d.permission}` : null, `page policy ${pol}`, d.ios ? "iOS" : null, d.standalone ? "home-screen app" : null].filter(Boolean).join(" · ");
}

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
    const [planAck, setPlanAck] = useState({});                   // command id → the user ticked "send anyway" for an unverified group
    const [attached, setAttached] = useState([]);                 // files the user attached BEFORE (or while) issuing a command — used only when they tap "Use"
    const [uploadInfo, setUploadInfo] = useState({});              // task id → {name, index, total, pct} while its approved files upload
    const localFiles = useRef(new Map());                          // `${taskId}|${category}|${name}|${size}` → File (kept in this tab only; the server holds just the approved list)
    const [workerNames, setWorkerNames] = useState({});          // worker id → registry label, only to name workers on tasks saved before labels were stored
    const workerNamesAsked = useRef(false);
    const inputRef = useRef(null);
    const endRef = useRef(null);
    const loaded = useRef(false);
    const fit = useKeyboardFit(open && !minimized);

    // PRESS AND HOLD to speak: pressing starts one recording, releasing stops it and — only for a normal release with real words — submits the
    // transcript ONCE through the same path as typing (planning only; every consequential action still waits for its approval). Every other way a
    // recording can end (cancel, interruption, error, panel closing, a limit) leaves the words in the text box as a draft and sends nothing.
    const holdRef = useRef(null);                            // {kind: "pointer"|"key", id} while a press is active; null otherwise
    const releaseMode = useRef("draft");                     // "submit" only between a normal release and the delivery of that session's transcript
    const sendingRef = useRef(false);
    const voiceFinal = useRef(null);
    const speech = useSpeechInput({ onFinal: (t) => voiceFinal.current?.(t) });
    // the panel stays mounted while closed/minimised: a microphone session must not keep running with nothing on screen
    useEffect(() => { if (!open || minimized) { holdRef.current = null; releaseMode.current = "draft"; speech.abort(); } }, [open, minimized]);          // eslint-disable-line react-hooks/exhaustive-deps

    // history + persisted tasks once, the first time it opens (survives refresh: both live on the backend)
    useEffect(() => {
        if (!open || loaded.current) return;
        loaded.current = true;
        Promise.all([getAssistantHistory().catch(() => []), listAssistantTasks(40).catch(() => [])]).then(([h, t]) => {
            setMessages((cur) => (cur.length ? cur : h));
            setTasks(t);
        });
    }, [open]);

    // Tasks saved by the first release carry only the raw worker id ("default"): name those from the live registry (presentation only).
    useEffect(() => {
        if (!open || tab !== "tasks" || workerNamesAsked.current) return;
        if (!tasks.some((t) => t.params?.worker_id && !t.params?.worker_label)) return;
        workerNamesAsked.current = true;
        getAssistantWorkers().then((list) => setWorkerNames(Object.fromEntries((list || []).map((w) => [w.id, w.label || w.id])))).catch(() => {});
    }, [open, tab, tasks]);

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

    const submit = useCallback(async (override, { keepDraft = false } = {}) => {      // keepDraft: a spoken command must not touch what is typed in the box
        const body = (override ?? text).trim();
        if (!body || sending || sendingRef.current) return;
        sendingRef.current = true;
        const rid = failed && failed.text === body ? failed.id : newRequestId();
        setSending(true);
        setFailed(null);
        if (!(failed && failed.text === body)) setMessages((cur) => [...cur, { id: `u-${rid}`, role: "user", text: body }]);
        if (!keepDraft) setText("");
        try {
            const out = await sendAssistantMessage({ text: body, clientRequestId: rid, context });
            setMessages((cur) => [...cur, { id: `a-${out.command_id}`, role: "assistant", text: out.reply, command_id: out.command_id }]);
            setTasks((cur) => [...out.tasks, ...cur.filter((x) => !out.tasks.some((n) => n.id === x.id))]);
            if (voiceOut) speak(out.reply);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            const msg = typeof detail === "string" ? detail : "I couldn't get an answer from the server. Nothing has been sent or changed (planning never does either) — tap Retry.";
            setMessages((cur) => [...cur, { id: `e-${rid}`, role: "assistant", text: msg, error: true }]);
            setFailed({ text: body, id: rid });
            setText((cur) => (keepDraft ? (cur ? `${cur} ${body}` : body) : body));            // never lost: a failed spoken command lands in the box for editing / retry
        } finally {
            sendingRef.current = false;
            setSending(false);
        }
    }, [text, sending, failed, context, voiceOut]);
    voiceFinal.current = (t) => {
        const mode = releaseMode.current;
        releaseMode.current = "draft";
        const words = (t || "").trim();
        if (!words) return;                                                       // nothing meaningful was said: never an empty command
        if (mode === "submit" && !sendingRef.current) submit(words, { keepDraft: true });
        else setText((cur) => (cur ? `${cur} ${words}` : words));                 // not a clean release (or another command is still being planned): keep it, send nothing
    };

    // --- the microphone button: a press is the recording -------------------------------------------------------------------------
    const beginHold = (kind, id) => {
        if (holdRef.current || !speech.supported) return false;                   // a second finger / key repeat can never start a second session
        holdRef.current = { kind, id };
        releaseMode.current = "draft";
        speech.start();
        return true;
    };
    const endHold = (how) => {                                                    // how: "submit" (released) | "draft" (interrupted) | "cancel" (discard)
        if (!holdRef.current) return;
        holdRef.current = null;
        if (how === "cancel") { releaseMode.current = "draft"; speech.abort({ discard: true }); return; }
        releaseMode.current = how === "submit" ? "submit" : "draft";
        speech.stop();
    };
    const micHandlers = {
        onPointerDown: (e) => {
            if (e.pointerType === "mouse" && e.button !== 0) return;
            if (!beginHold("pointer", e.pointerId)) return;
            try { e.currentTarget.setPointerCapture?.(e.pointerId); } catch { /* not capturable: the window listeners below still end the press */ }
            e.preventDefault();
        },
        onPointerUp: (e) => { if (holdRef.current?.kind === "pointer" && holdRef.current.id === e.pointerId) endHold("submit"); },
        onPointerCancel: (e) => { if (holdRef.current?.kind === "pointer" && holdRef.current.id === e.pointerId) endHold("draft"); },
        onLostPointerCapture: (e) => { if (holdRef.current?.kind === "pointer" && holdRef.current.id === e.pointerId) endHold("draft"); },
        onContextMenu: (e) => e.preventDefault(),                                 // a long press must not open the phone's context menu
        onKeyDown: (e) => {
            if (e.key !== " " && e.key !== "Enter") return;
            e.preventDefault();                                                   // the browser's own click-on-key would toggle
            if (!e.repeat) beginHold("key", "key");
        },
        onKeyUp: (e) => { if ((e.key === " " || e.key === "Enter") && holdRef.current?.kind === "key") { e.preventDefault(); endHold("submit"); } },
        onBlur: () => { if (holdRef.current?.kind === "key") endHold("draft"); },
        // A click that did NOT come from a pointer press (detail 0: assistive technology, voice control) toggles, so the button stays operable
        // without holding. Real mouse/touch presses are handled by the pointer events above and their trailing click is ignored.
        onClick: (e) => { if (e.detail === 0 && !holdRef.current) speech.toggle(); },
    };
    useEffect(() => {
        // Safety nets: the page is hidden / loses focus, or a pointer is released somewhere we never hear about → the recording ends now.
        const interrupt = () => endHold("draft");
        const hidden = () => { if (document.visibilityState === "hidden") interrupt(); };
        const up = (e) => { if (holdRef.current?.kind === "pointer" && holdRef.current.id === e.pointerId) endHold(e.type === "pointerup" ? "submit" : "draft"); };
        window.addEventListener("blur", interrupt);
        document.addEventListener("visibilitychange", hidden);
        window.addEventListener("pointerup", up);
        window.addEventListener("pointercancel", up);
        return () => { window.removeEventListener("blur", interrupt); document.removeEventListener("visibilitychange", hidden); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", up); };
    });                                                                             // eslint-disable-line react-hooks/exhaustive-deps

    const act = useCallback(async (taskId, fn) => {
        if (busy[taskId]) return;
        setBusy((b) => ({ ...b, [taskId]: true }));
        setErrors((e) => ({ ...e, [taskId]: null }));
        try {
            mergeTask(await fn());
        } catch (err) {
            const changed = changedPlan(err);
            if (changed) (changed.tasks || []).forEach(mergeTask);          // the plan moved since it was shown: show the revised one for a fresh approval
            const detail = changed ? changed.message : errorDetail(err);
            setErrors((e) => ({ ...e, [taskId]: typeof detail === "string" ? detail
                : isTimeoutError(err) ? "No answer from the server — it may still have run. I've refreshed this task from the server; check its status before trying again."
                : "That didn't work — I've refreshed this task from the server." }));
            listAssistantTasks(40).then(setTasks).catch(() => {});          // the persisted state is the truth, not the failed request
        } finally {
            setBusy((b) => ({ ...b, [taskId]: false }));
        }
    }, [busy, mergeTask]);

    const onClarify = (t, slot, body) => act(t.id, async () => {
        const updated = await clarifyTask(t.id, { slot, ...body });
        listAssistantTasks(40).then(setTasks).catch(() => {});      // dependents of this task may have changed too
        return updated;
    });
    // `update`: the server applies the approved FIELD changes, then each approved file is uploaded one by one through the existing authenticated
    // API. A file is accepted only if it matches the approved list; the result shown is whatever the server read back, never the browser's belief.
    const runUploads = async (task) => {
        let cur = task;
        const todo = [];
        (cur.params?.updates?.media || []).forEach((m) => (m.files || []).forEach((x) => { if (["pending", "failed"].includes(x.state)) todo.push([m.category, x]); }));
        let n = 0;
        for (const [category, x] of todo) {
            const f = localFiles.current.get(`${task.id}|${category}|${x.key}`);
            if (!f) continue;
            n += 1;
            setUploadInfo((u) => ({ ...u, [task.id]: { name: x.name, index: n, total: todo.length, pct: 0 } }));
            try {
                cur = await uploadUpdateMedia(task.id, category, x.key, f, (pct) => setUploadInfo((u) => ({ ...u, [task.id]: { name: x.name, index: n, total: todo.length, pct } })));
            } catch (err) {
                setErrors((e) => ({ ...e, [task.id]: `${x.name}: ${errorDetail(err) || (isTimeoutError(err) ? "no answer from the server — check the result below before trying again." : "the upload failed.")}` }));
                try { cur = (await listAssistantTasks(40)).find((t) => t.id === task.id) || cur; } catch { /* keep what we have */ }
            }
        }
        setUploadInfo((u) => { const { [task.id]: _gone, ...rest } = u; return rest; });
        return cur;
    };
    const onConfirm = (t, worker, ack) => act(t.id, async () => {
        const done = await confirmTask(t.id, worker, t.fingerprint, !!ack);
        return done.kind === "update" && done.status === "running" ? runUploads(done) : done;
    });
    const onRetryUploads = (t) => act(t.id, () => runUploads(t));
    const lacksLocalFiles = (t) => t.kind === "update" && (t.params?.updates?.media || []).some((m) => (m.files || []).some((x) => ["pending", "failed"].includes(x.state) && !localFiles.current.has(`${t.id}|${m.category}|${x.key}`)));
    const onFiles = (t, category, files, fromAttached) => act(t.id, async () => {
        const out = await clarifyTask(t.id, { slot: `files:${category}`, files: files.map((f) => ({ name: f.name, size: f.size, type: f.type || "" })) });
        files.forEach((f) => localFiles.current.set(`${t.id}|${category}|${fileKey(f)}`, f));
        if (fromAttached) setAttached((a) => a.filter((f) => !files.includes(f)));
        return out;
    });
    const onReselect = (t, files) => {
        // the page was reloaded: match chosen files to the APPROVED list by name + size — anything else is ignored, never uploaded
        let hit = 0;
        (t.params?.updates?.media || []).forEach((m) => (m.files || []).forEach((x) => { const f = files.find((g) => fileKey(g) === x.key); if (f) { localFiles.current.set(`${t.id}|${m.category}|${x.key}`, f); hit += 1; } }));
        if (!hit) toast.error("None of those files is on the approved list.");
        else onRetryUploads(t);
    };
    const onCancel = (t) => act(t.id, () => cancelTask(t.id));
    const onRetry = (t) => act(t.id, () => retryTask(t.id));
    const onFollowUp = (t, templateId) => act(t.id, () => followUpSend(t.id, templateId));

    // One approval for a whole plan (several different actions from one message). The server re-checks every waiting action against
    // what was shown BEFORE running any of them; it runs them in dependency order and a step whose dependency did not succeed does
    // not run. If anything changed meanwhile, nothing runs and the revised actions come back for a fresh look.
    const approvePlan = async (commandId, waiting, ack) => {
        const ids = waiting.map((t) => t.id);
        ids.forEach((i) => setBusy((b) => ({ ...b, [i]: true })));
        try {
            const fps = waiting.every((t) => t.fingerprint) ? Object.fromEntries(waiting.map((t) => [t.id, t.fingerprint])) : undefined;
            const results = await confirmAll(commandId, batchWorker, fps, !!ack);
            for (const r of Object.values(results || {})) {
                if (r?.task?.kind === "update" && r.task.status === "running") mergeTask(await runUploads(r.task));
            }
            const held = Object.entries(results || {}).filter(([, r]) => r.error);
            setErrors((e) => ({ ...e, ...Object.fromEntries(held.map(([id, r]) => [id, r.error])) }));
            if (held.length) toast.error(`${held.length} step${held.length === 1 ? " was" : "s were"} not run — see the reason on ${held.length === 1 ? "its" : "each"} card.`);
        } catch (err) {
            const changed = changedPlan(err);
            if (changed) (changed.tasks || []).forEach(mergeTask);
            const detail = changed ? changed.message : errorDetail(err);
            toast.error(typeof detail === "string" ? detail : "Couldn't approve the plan — I've refreshed from the server.");
        } finally {
            setTasks(await listAssistantTasks(40).catch(() => tasks));
            ids.forEach((i) => setBusy((b) => ({ ...b, [i]: false })));
        }
    };
    const cancelPlan = async (commandId, ids) => {
        ids.forEach((i) => setBusy((b) => ({ ...b, [i]: true })));
        try { await cancelCommand(commandId); } catch { toast.error("Couldn't cancel the whole plan — I've refreshed from the server."); }
        setTasks(await listAssistantTasks(40).catch(() => tasks));
        ids.forEach((i) => setBusy((b) => ({ ...b, [i]: false })));
    };

    const clearChat = async () => {
        try { await clearAssistantHistory(); setMessages([]); } catch { toast.error("Couldn't clear the conversation."); }
    };

    const tasksFor = (commandId) => tasks.filter((t) => t.command_id === commandId).sort((a, b) => a.seq - b.seq);
    // The header describes the CURRENT request. An older request that was never answered stays visible as a quiet note, not as the
    // state of what you just asked.
    const latestCmd = tasks.length ? [...tasks].sort((x, y) => String(y.created_at).localeCompare(String(x.created_at)))[0].command_id : null;
    const latest = tasks.filter((t) => t.command_id === latestCmd);
    const olderOpen = tasks.filter((t) => t.command_id !== latestCmd && ATTENTION_STATUSES.includes(t.status)).length;
    const older = olderOpen ? ` · ${olderOpen} older request${olderOpen === 1 ? "" : "s"} still open` : "";
    const status = speech.phase === "starting" ? "Starting microphone…" : speech.phase === "processing" ? "Finishing transcript…" : speech.listening ? "Listening — release to send" : sending ? "Working on it…"
        : latest.some((t) => t.status === "needs_clarification") ? `Needs your pick${older}`
            : latest.some((t) => t.status === "awaiting_confirmation") ? `Waiting for your OK${older}`
                : hasActive ? `Running in the background${older}` : `Ready${older}`;

    if (!open) return null;
    const mobileStyle = fit ? { maxHeight: fit.height - 12, bottom: fit.bottom + 6 } : undefined;
    return (
        <section role="dialog" aria-label="Talentgram assistant" data-testid="pa-panel" style={mobileStyle}
            className={`fixed z-40 right-2 left-2 bottom-2 sm:left-auto sm:right-6 ${lifted ? "sm:bottom-28" : "sm:bottom-6"} ${expanded ? "sm:w-[560px]" : "sm:w-[400px]"} flex flex-col bg-white border border-black/[0.12] rounded-2xl shadow-[0_12px_40px_rgba(0,0,0,0.22)] overflow-hidden ${minimized ? "" : expanded ? "h-[min(88dvh,820px)]" : lifted ? "h-[min(70dvh,640px)]" : "h-[min(78dvh,640px)]"}`}
            onKeyDown={(e) => { if (e.key === "Escape") { if (speech.listening) { endHold("cancel"); speech.abort({ discard: true }); } else onClose(); } }}>
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
                        {attention > 0 && <span className="ml-auto self-center text-[11px] text-amber-700" data-testid="pa-attention">{attention} need{attention === 1 ? "s" : ""} you{olderOpen > 0 && olderOpen < attention ? ` (${olderOpen} older)` : ""}</span>}
                    </nav>

                    <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3 min-h-0" data-testid="pa-scroll">
                        {tab === "chat" ? (
                            <>
                                {messages.length === 0 && (
                                    <div className="text-sm text-black/60" data-testid="pa-empty">
                                        <p>Hi — tell me what to do. You can type, or hold the mic to speak and release to send.</p>
                                        <ul className="mt-2 flex flex-wrap gap-1.5">{EXAMPLES.map((e) => <li key={e}><button type="button" onClick={() => { setText(e); inputRef.current?.focus(); }} className="px-2.5 min-h-[32px] rounded-full border border-black/[0.12] text-[11px] text-black/65 hover:border-black/40">{e}</button></li>)}</ul>
                                    </div>
                                )}
                                {messages.map((m) => {
                                    const ts = m.command_id && m.role === "assistant" ? tasksFor(m.command_id) : [];
                                    const waiting = ts.filter((t) => t.status === "awaiting_confirmation");
                                    // a PLAN = several actions of one message waiting together. Anything in a dependency chain is approved only through the plan.
                                    const open_ = ts.filter((t) => t.status === "awaiting_confirmation" || t.status === "needs_clarification");
                                    const isPlan = waiting.length > 1;
                                    const chained = (t) => isPlan && ((t.depends_on || []).some((d) => waiting.some((w) => w.id === d)) || waiting.some((w) => (w.depends_on || []).includes(t.id)));
                                    const needsW = waiting.some((t) => t.kind === "send" || t.kind === "share");
                                    const unverified = needsW && waiting.filter((t) => t.kind === "share" && t.preview?.worker_access?.[batchWorker]?.state === "unknown").map((t) => t.preview.group);
                                    const blocked = needsW && waiting.some((t) => t.kind === "share" && t.preview?.worker_access?.[batchWorker]?.state === "failed_before");
                                    const hint = waiting.find((t) => t.params?.worker_hint)?.params.worker_hint;
                                    return (
                                        <div key={m.id} className={m.role === "user" ? "flex justify-end" : ""}>
                                            <div className={m.role === "user" ? "max-w-[85%] bg-black text-white rounded-2xl rounded-br-md px-3 py-2 text-sm" : "max-w-full w-full"}>
                                                {m.role === "user" ? m.text : (
                                                    <>
                                                        <p className={`text-sm ${m.error ? "text-red-700" : "text-black/80"}`} data-testid="pa-reply">{m.error ? m.text : liveReply(m.text, ts)}</p>
                                                        {m.error && failed && <button type="button" onClick={() => submit(failed.text)} className="mt-1 text-xs underline" data-testid="pa-retry-send">Retry</button>}
                                                        {ts.length > 0 && (
                                                            <div className="mt-2 space-y-2">
                                                                {ts.map((t) => <TaskCard key={t.id} task={t} busy={!!busy[t.id]} actionError={errors[t.id]} inPlan={chained(t)} onClarify={onClarify} onConfirm={onConfirm} onCancel={onCancel} onRetry={onRetry} onFollowUp={onFollowUp} onPrefill={(v) => { setText(v); inputRef.current?.focus(); }}
                                                                    attached={attached} onFiles={onFiles} uploadInfo={uploadInfo[t.id] || null} onRetryUploads={onRetryUploads} onReselect={onReselect} missingLocal={lacksLocalFiles(t)} />)}
                                                                {isPlan && (
                                                                    <div className="rounded-xl border border-black/[0.15] p-3 bg-black/[0.02] space-y-2" data-testid="pa-plan">
                                                                        <p className="text-xs font-medium text-black/85">Plan — {waiting.length} actions. One approval covers exactly these, in this order:</p>
                                                                        <ol className="text-[11px] text-black/65 list-decimal pl-4 space-y-0.5" data-testid="pa-plan-steps">
                                                                            {open_.map((t, i) => {
                                                                                const after = (t.depends_on || []).map((d) => open_.findIndex((x) => x.id === d) + 1).filter((n) => n > 0);
                                                                                return <li key={t.id} className={t.status === "needs_clarification" ? "text-amber-700" : ""}>{t.title}{after.length ? ` — after step ${after.join(", ")}, only if it succeeds` : ""}{t.status === "needs_clarification" ? " — needs your pick first; it will not run" : ""}</li>;
                                                                            })}
                                                                        </ol>
                                                                        {needsW && <WorkerPicker value={batchWorker} onChange={setBatchWorker} hint={hint} disabled={waiting.some((t) => busy[t.id])} />}
                                                                        {blocked && <p className="text-[11px] text-red-700" role="alert" data-testid="pa-plan-blocked">This worker could not find a destination group last time, so the plan is held. Pick another worker.</p>}
                                                                        {unverified.length > 0 && !blocked && (
                                                                            <label className="flex items-start gap-2 text-[11px] text-amber-800" data-testid="pa-plan-ack">
                                                                                <input type="checkbox" className="mt-0.5" checked={!!planAck[m.command_id]} onChange={(e) => setPlanAck((a) => ({ ...a, [m.command_id]: e.target.checked }))} />
                                                                                <span>This worker has never been seen sending to {unverified.map((g) => `“${g}”`).join(", ")} and the app can&apos;t check which chats it belongs to. Send anyway — if it isn&apos;t in the group nothing is delivered and the failure is shown.</span>
                                                                            </label>
                                                                        )}
                                                                        <div className="flex gap-2">
                                                                            <button type="button" disabled={(needsW && !batchWorker) || blocked || (unverified.length > 0 && !planAck[m.command_id]) || waiting.some((t) => busy[t.id])} onClick={() => approvePlan(m.command_id, waiting, unverified.length > 0 && planAck[m.command_id])} data-testid="pa-plan-approve" className="px-3.5 min-h-[40px] rounded-lg bg-black text-white text-xs font-medium disabled:opacity-40">Approve plan</button>
                                                                            <button type="button" disabled={waiting.some((t) => busy[t.id])} onClick={() => cancelPlan(m.command_id, open_.map((t) => t.id))} data-testid="pa-plan-cancel" className="px-3 min-h-[40px] rounded-lg border border-black/[0.15] text-xs text-black/65">Cancel plan</button>
                                                                        </div>
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
                                        <div className="flex items-start justify-between gap-2"><span className="text-xs text-black/85 leading-snug">{t.title}</span><StatusChip status={t.status} kind={t.kind} uncertain={!!t.uncertain} /></div>
                                        <p className="text-[11px] text-black/45 mt-1">{t.status_detail || t.error || ""}{t.params?.worker_id ? `${t.status_detail || t.error ? " · " : ""}Worker: ${t.params.worker_label || workerNames[t.params.worker_id] || t.params.worker_id}` : ""}{t.updated_at ? ` · ${new Date(t.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</p>
                                    </li>
                                ))}
                            </ul>
                        )}
                        <div ref={endRef} />
                    </div>

                    <form className="border-t border-black/[0.08] p-2.5 shrink-0" onSubmit={(e) => { e.preventDefault(); submit(); }}>
                        {speech.listening && (
                            <div className="mb-1.5" data-testid="pa-listening">
                                <p className="text-xs text-red-600 flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" /> {speech.phase === "starting" ? "Starting microphone… allow it if the browser asks" : speech.phase === "processing" ? "Finishing transcript…" : "Listening — keep holding, pause as long as you like, release to send"}</p>
                                {speech.phase !== "starting" && <p className="text-[11px] text-black/55 mt-0.5 line-clamp-3" data-testid="pa-live-text" aria-live="off">{speech.liveText || "speak now"}</p>}
                            </div>
                        )}
                        {speech.notice && !speech.listening && <p className="text-xs text-black/65 mb-1.5" role="status" data-testid="pa-voice-notice">{speech.notice}</p>}
                        {speech.error && (
                            <div className="mb-1.5" role="alert" data-testid="pa-voice-error-box">
                                <p className="text-xs text-amber-700" data-testid="pa-voice-error">{speech.error}</p>
                                {speech.diagnostics && <p className="text-[10px] text-black/40 mt-0.5" data-testid="pa-voice-diag">Technical detail: {describeVoiceDiagnostics(speech.diagnostics)}</p>}
                            </div>
                        )}
                        {!speech.supported && <p className="text-[11px] text-black/45 mb-1.5" data-testid="pa-voice-unsupported">Voice input isn&apos;t available in this browser — type your command instead{typeof navigator !== "undefined" && navigator.maxTouchPoints > 0 ? ", or tap the microphone key on your keyboard to dictate" : ""}.</p>}
                        {attached.length > 0 && (
                            <ul className="flex flex-wrap gap-1.5 mb-1.5" data-testid="pa-attached" aria-label="Attached files">
                                {attached.map((f) => (
                                    <li key={`${f.name}|${f.size}|${f.lastModified}`} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-black/[0.05] text-[11px] text-black/70 max-w-full">
                                        <span className="truncate max-w-[160px]">{f.name}</span>
                                        <button type="button" aria-label={`Remove ${f.name}`} onClick={() => setAttached((a) => a.filter((x) => x !== f))} className="text-black/50 hover:text-black"><X className="w-3 h-3" /></button>
                                    </li>
                                ))}
                                <li className="text-[10px] text-black/40 self-center">Say what they are for (e.g. “update Angela's introduction video”), then tap “Use attached” on the card.</li>
                            </ul>
                        )}
                        <div className="flex items-end gap-2">
                            <label className="w-11 h-11 shrink-0 rounded-xl border border-black/[0.15] flex items-center justify-center text-black/70 cursor-pointer hover:border-black/40" title="Attach pictures or a video">
                                <Paperclip className="w-4 h-4" aria-hidden />
                                <input type="file" multiple className="sr-only" accept="image/*,video/mp4,video/quicktime,.mp4,.mov,.heic,.heif" data-testid="pa-attach" aria-label="Attach files"
                                    onChange={(e) => { const add = Array.from(e.target.files || []); e.target.value = ""; setAttached((a) => [...a, ...add.filter((f) => !a.some((x) => x.name === f.name && x.size === f.size))].slice(0, 20)); }} />
                            </label>
                            <textarea ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} rows={1} maxLength={2000} placeholder="Type a command…" aria-label="Command"
                                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
                                className="flex-1 min-w-0 resize-none max-h-28 border border-black/[0.12] rounded-xl px-3 py-2.5 text-base sm:text-sm focus:outline-none focus:border-black/50" />
                            <button type="button" {...micHandlers} disabled={!speech.supported} aria-pressed={speech.listening} data-state={speech.error && !speech.listening ? "failed" : speech.phase}
                                style={{ touchAction: "none", WebkitTouchCallout: "none", WebkitUserSelect: "none", userSelect: "none" }}
                                aria-label={speech.phase === "processing" ? "Finishing transcript…" : speech.phase === "starting" ? "Starting microphone — release to cancel" : speech.listening ? "Listening — release to send" : "Hold to speak"} data-testid="pa-mic"
                                className={`w-11 h-11 shrink-0 rounded-xl border flex items-center justify-center select-none touch-none ${speech.listening ? "bg-red-500 text-white border-red-500" : speech.error ? "border-amber-400 text-amber-700" : "border-black/[0.15] text-black/70"} disabled:opacity-40`}>
                                {speech.phase === "processing" || speech.phase === "starting" ? <Loader2 className="w-4 h-4 animate-spin" aria-hidden /> : <Mic className="w-4 h-4" aria-hidden />}
                            </button>
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
