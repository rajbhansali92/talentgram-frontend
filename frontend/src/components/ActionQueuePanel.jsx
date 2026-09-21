import React, { useEffect, useRef, useState, useCallback } from "react";
import { adminApi } from "@/lib/api";
import {
    ListTodo, ChevronDown, ChevronUp, X, Loader2, CheckCircle2,
    XCircle, RotateCcw, Upload, Send,
} from "lucide-react";

// Mirrors NotificationBell.jsx's own shared-singleton polling pattern —
// AdminLayout mounts this panel once, globally, independent of whichever
// Submission Review page (if any) is currently open, per this feature's
// own explicit requirement: closing/navigating away from a Submission
// Review page must never interrupt an in-progress Approve + Upload/Send;
// the backend action already keeps running regardless (see
// backend/agents/modules/submission_action_queue.py) — this panel is
// purely a read-only window onto that durable state, polled from
// wherever the admin currently is in the app.
let sharedActions = [];
let sharedSubscribers = new Set();
let sharedPollTimer = null;
let sharedInFlight = null;
// Last-seen state per action id — lets a poll tick detect a NEW
// transition into a terminal state (vs. an action that was ALREADY
// terminal on a previous fetch) so the completion event below fires
// exactly once per real completion, never repeatedly on every poll.
const lastSeenState = new Map();
// Bumped by __resetForTests (and, harmlessly, never in production) — an
// in-flight fetch started before a reset applies its result ONLY if the
// generation it captured is still current, so a stale resolution can
// never clobber fresher state that arrived (or was reset) after it.
let generation = 0;

const POLL_MS = 3000;

async function fetchSharedActions() {
    if (sharedInFlight) return sharedInFlight;
    const myGeneration = generation;
    sharedInFlight = adminApi
        .get("/submission-actions")
        .then(({ data }) => {
            if (myGeneration !== generation) return;
            const next = data?.actions || [];
            for (const a of next) {
                const prevState = lastSeenState.get(a.id);
                const isNewlyTerminal = (a.state === "COMPLETED" || a.state === "FAILED") && prevState && prevState !== a.state;
                if (isNewlyTerminal) {
                    window.dispatchEvent(new CustomEvent("submission-action-completed", {
                        detail: { submissionId: a.submission_id, projectId: a.project_id, ok: a.state === "COMPLETED" },
                    }));
                }
                lastSeenState.set(a.id, a.state);
            }
            sharedActions = next;
            sharedSubscribers.forEach((cb) => cb(sharedActions));
        })
        .catch((e) => console.error("[ActionQueuePanel] fetch failed", e))
        .finally(() => {
            sharedInFlight = null;
        });
    return sharedInFlight;
}

function subscribeToSharedActions(cb) {
    sharedSubscribers.add(cb);
    cb(sharedActions);
    if (sharedSubscribers.size === 1) {
        fetchSharedActions();
        sharedPollTimer = setInterval(() => {
            if (document.visibilityState === "visible") fetchSharedActions();
        }, POLL_MS);
    }
    return () => {
        sharedSubscribers.delete(cb);
        if (sharedSubscribers.size === 0 && sharedPollTimer) {
            clearInterval(sharedPollTimer);
            sharedPollTimer = null;
        }
    };
}

// Notify every mounted subscriber to refetch immediately — used right
// after a Retry click so the panel doesn't wait out a full poll cycle
// to reflect it.
function forceRefreshSharedActions() {
    fetchSharedActions();
}

const ACTIVE_STATES = new Set([
    "QUEUED", "VERIFYING", "MEDIA_RESOLVED", "DOWNLOADING", "UPLOADING", "SENDING", "VERIFYING_RESULT",
]);

const STATE_LABEL = {
    QUEUED: "Queued…",
    VERIFYING: "Verifying media…",
    MEDIA_RESOLVED: "Media resolved…",
    DOWNLOADING: "Downloading…",
    UPLOADING: "Uploading…",
    SENDING: "Sending…",
    VERIFYING_RESULT: "Verifying result…",
    COMPLETED: "Completed",
    FAILED: "Failed",
};

function ActionRow({ action, onRetry, retryingId }) {
    const displayState = action.display_state || action.state;
    const isActive = ACTIVE_STATES.has(action.state);
    const isFailed = action.state === "FAILED";
    const isCompleted = action.state === "COMPLETED";
    const Icon = action.action_type === "upload" ? Upload : Send;

    return (
        <div className="px-3.5 py-2.5 border-b border-black/[0.06] last:border-b-0">
            <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-[13px] font-medium text-black/85 truncate">
                        {isCompleted ? (
                            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
                        ) : isFailed ? (
                            <XCircle className="w-3.5 h-3.5 text-red-500 shrink-0" />
                        ) : (
                            <Loader2 className="w-3.5 h-3.5 text-black/40 animate-spin shrink-0" />
                        )}
                        <span className="truncate">{action.talent_label}</span>
                    </div>
                    <div className="text-[12px] text-black/50 truncate mt-0.5 flex items-center gap-1">
                        <Icon className="w-3 h-3 shrink-0" />
                        <span className="truncate">{action.project_label}</span>
                        <span className="text-black/30">·</span>
                        <span className="uppercase tracking-wide text-[10px] font-semibold text-black/40">
                            {action.action_type}
                        </span>
                    </div>
                    <div className={`text-[12px] mt-1 ${isFailed ? "text-red-600" : isCompleted ? "text-emerald-700" : "text-black/60"}`}>
                        {isFailed ? (action.error_message || STATE_LABEL[displayState] || displayState) : (STATE_LABEL[displayState] || displayState)}
                    </div>
                    {isCompleted && action.has_unverified_media && (
                        <div className="text-[12px] mt-1 text-amber-600">
                            Delivery unconfirmed for one or more items — check the destination group.
                        </div>
                    )}
                </div>
                {isFailed && action.retryable && (
                    <button
                        onClick={() => onRetry(action)}
                        disabled={retryingId === action.id}
                        className="shrink-0 mt-0.5 flex items-center gap-1 text-[11px] font-medium text-black/60 hover:text-black bg-black/[0.04] hover:bg-black/[0.08] rounded-md px-2 py-1 transition-colors disabled:opacity-50"
                        title="Retry"
                    >
                        {retryingId === action.id ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                        ) : (
                            <RotateCcw className="w-3 h-3" />
                        )}
                        Retry
                    </button>
                )}
            </div>
        </div>
    );
}

// Panel presentation modes (2026-09-21 side-panel redesign). "open" shows
// the header + scrollable action list; "collapsed" shows just the header
// bar (click it, or the chevron, to re-expand); "closed" hides the panel
// completely and replaces it with a small, unobtrusive reopen icon — NOT
// a third variant of the same big box, and never re-entered automatically
// (see the auto-expand effect below, which is gated on userInteractedRef
// exactly like "collapsed" always was, so a close is respected exactly as
// durably as a manual collapse always has been).
const MODE_OPEN = "open";
const MODE_COLLAPSED = "collapsed";
const MODE_CLOSED = "closed";

export default function ActionQueuePanel() {
    const [actions, setActions] = useState(sharedActions);
    const [mode, setMode] = useState(MODE_COLLAPSED);
    const [retryingId, setRetryingId] = useState(null);
    const userInteractedRef = useRef(false);

    useEffect(() => subscribeToSharedActions(setActions), []);

    // Auto-expand the FIRST time an active action appears, so the
    // recruiter notices it started — never re-force it open again once
    // the admin has deliberately collapsed OR closed it even once (a
    // close is a stronger, equally durable preference as a collapse —
    // new actions, completions, talent switches, and ordinary polling
    // must never override either one).
    useEffect(() => {
        if (userInteractedRef.current) return;
        if (actions.some((a) => ACTIVE_STATES.has(a.state))) {
            setMode(MODE_OPEN);
        }
    }, [actions]);

    const openPanel = useCallback(() => {
        userInteractedRef.current = true;
        setMode(MODE_OPEN);
    }, []);

    const toggleCollapse = useCallback(() => {
        userInteractedRef.current = true;
        setMode((m) => (m === MODE_OPEN ? MODE_COLLAPSED : MODE_OPEN));
    }, []);

    const closePanel = useCallback(() => {
        userInteractedRef.current = true;
        setMode(MODE_CLOSED);
    }, []);

    const handleRetry = useCallback(async (action) => {
        setRetryingId(action.id);
        try {
            await adminApi.post(
                `/projects/${action.project_id}/submissions/${action.submission_id}/whatsapp-action/${action.id}/retry`,
            );
            forceRefreshSharedActions();
        } catch (e) {
            console.error("[ActionQueuePanel] retry failed", e);
        } finally {
            setRetryingId(null);
        }
    }, []);

    if (actions.length === 0) return null;

    const activeCount = actions.filter((a) => ACTIVE_STATES.has(a.state)).length;

    // Closed: nothing but a small, unobtrusive reopen affordance — never
    // a large permanent floating button, never leftover collapsed-bar
    // chrome, never an overlay. Anchored top-right on every breakpoint
    // (never bottom) so it can never sit over the mobile Decision Making
    // footer regardless of that footer's own expand/collapse state.
    if (mode === MODE_CLOSED) {
        return (
            <button
                onClick={openPanel}
                aria-label={activeCount > 0 ? `Open Action Queue (${activeCount} active)` : "Open Action Queue"}
                data-testid="action-queue-reopen"
                className="fixed top-16 right-4 z-30 w-10 h-10 rounded-full bg-white hover:bg-black/[0.04] border border-black/[0.08] shadow-[0_4px_16px_rgba(0,0,0,0.12)] flex items-center justify-center transition-colors"
            >
                <ListTodo className="w-4 h-4 text-black/60" />
                {activeCount > 0 && (
                    <span className="absolute -top-1 -right-1 inline-flex items-center justify-center min-w-[16px] h-[16px] px-1 rounded-full bg-black text-white text-[9px] font-bold">
                        {activeCount}
                    </span>
                )}
            </button>
        );
    }

    const sorted = [...actions].sort((a, b) => {
        const aActive = ACTIVE_STATES.has(a.state) ? 0 : 1;
        const bActive = ACTIVE_STATES.has(b.state) ? 0 : 1;
        if (aActive !== bActive) return aActive - bActive;
        return new Date(b.created_at) - new Date(a.created_at);
    });
    const isOpen = mode === MODE_OPEN;

    return (
        <div
            className="fixed top-16 right-4 z-30 w-[320px] max-w-[calc(100vw-2rem)] max-h-[min(70vh,32rem)] bg-white rounded-xl shadow-[0_8px_30px_rgba(0,0,0,0.14)] border border-black/[0.08] overflow-hidden flex flex-col transition-[max-height] duration-200"
            data-testid="action-queue-panel"
        >
            <div className="flex items-center gap-1 pl-3.5 pr-2 py-2.5 bg-black/[0.02] shrink-0">
                <button
                    onClick={toggleCollapse}
                    className="flex-1 min-w-0 flex items-center gap-2 text-left"
                    aria-expanded={isOpen}
                    aria-controls="action-queue-list"
                    data-testid="action-queue-toggle"
                >
                    <ListTodo className="w-4 h-4 text-black/60 shrink-0" />
                    <span className="text-[13px] font-semibold text-black/80 truncate">Action Queue</span>
                    {activeCount > 0 && (
                        <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-black text-white text-[10px] font-bold shrink-0">
                            {activeCount}
                        </span>
                    )}
                    {isOpen ? (
                        <ChevronUp className="w-4 h-4 text-black/40 shrink-0 ml-auto" />
                    ) : (
                        <ChevronDown className="w-4 h-4 text-black/40 shrink-0 ml-auto" />
                    )}
                </button>
                <button
                    onClick={closePanel}
                    aria-label="Close Action Queue"
                    data-testid="action-queue-close"
                    className="shrink-0 w-6 h-6 flex items-center justify-center rounded-md text-black/40 hover:text-black/70 hover:bg-black/[0.06] transition-colors"
                >
                    <X className="w-3.5 h-3.5" />
                </button>
            </div>
            {isOpen && (
                <div id="action-queue-list" className="overflow-y-auto min-h-0">
                    {sorted.map((action) => (
                        <ActionRow key={action.id} action={action} onRetry={handleRetry} retryingId={retryingId} />
                    ))}
                </div>
            )}
        </div>
    );
}

// Test-only: the shared singleton above is module-scoped (deliberately,
// same reasoning as NotificationBell.jsx's own — dedupe polling across
// however many times this panel gets mounted), which means its state
// otherwise leaks between test cases within the same file. Never called
// from application code.
export function __resetForTests() {
    generation += 1;
    sharedActions = [];
    sharedSubscribers = new Set();
    if (sharedPollTimer) clearInterval(sharedPollTimer);
    sharedPollTimer = null;
    sharedInFlight = null;
    lastSeenState.clear();
}
