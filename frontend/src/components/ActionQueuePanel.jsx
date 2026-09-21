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

export default function ActionQueuePanel() {
    const [actions, setActions] = useState(sharedActions);
    const [collapsed, setCollapsed] = useState(true);
    const [retryingId, setRetryingId] = useState(null);
    const userToggledRef = useRef(false);

    useEffect(() => subscribeToSharedActions(setActions), []);

    // Auto-expand the FIRST time an active action appears, so the
    // recruiter notices it started — never re-force it open again after
    // they've deliberately collapsed it once.
    useEffect(() => {
        if (userToggledRef.current) return;
        if (actions.some((a) => ACTIVE_STATES.has(a.state))) {
            setCollapsed(false);
        }
    }, [actions]);

    const toggle = useCallback(() => {
        userToggledRef.current = true;
        setCollapsed((c) => !c);
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
    const sorted = [...actions].sort((a, b) => {
        const aActive = ACTIVE_STATES.has(a.state) ? 0 : 1;
        const bActive = ACTIVE_STATES.has(b.state) ? 0 : 1;
        if (aActive !== bActive) return aActive - bActive;
        return new Date(b.created_at) - new Date(a.created_at);
    });

    return (
        <div
            className="fixed bottom-4 right-4 z-50 w-[320px] max-w-[calc(100vw-2rem)] bg-white rounded-xl shadow-[0_8px_30px_rgba(0,0,0,0.12)] border border-black/[0.08] overflow-hidden"
            data-testid="action-queue-panel"
        >
            <button
                onClick={toggle}
                className="w-full flex items-center justify-between gap-2 px-3.5 py-2.5 bg-black/[0.02] hover:bg-black/[0.04] transition-colors"
                aria-expanded={!collapsed}
                data-testid="action-queue-toggle"
            >
                <div className="flex items-center gap-2">
                    <ListTodo className="w-4 h-4 text-black/60" />
                    <span className="text-[13px] font-semibold text-black/80">Action Queue</span>
                    {activeCount > 0 && (
                        <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-black text-white text-[10px] font-bold">
                            {activeCount}
                        </span>
                    )}
                </div>
                {collapsed ? (
                    <ChevronUp className="w-4 h-4 text-black/40" />
                ) : (
                    <ChevronDown className="w-4 h-4 text-black/40" />
                )}
            </button>
            {!collapsed && (
                <div className="max-h-[360px] overflow-y-auto">
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
