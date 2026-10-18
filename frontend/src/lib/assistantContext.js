/**
 * Tiny page → assistant context bridge (no state library): a page that has a selection or a "current project"
 * publishes it; the assistant reads it when sending a command so "these selected talents" / "the current project"
 * mean what is on screen. The backend validates every id it receives — this is only a convenience channel.
 */
import { useEffect, useSyncExternalStore } from "react";

const EMPTY = [];
let state = { projectId: null, talentIds: EMPTY };
const listeners = new Set();

function emit() {
    listeners.forEach((l) => l());
}

export function getAssistantContext() {
    return state;
}

export function setAssistantContext(patch) {
    const next = { ...state, ...patch };
    if (next.projectId === state.projectId && next.talentIds === state.talentIds) return;
    state = next;
    emit();
}

export function useAssistantContext() {
    return useSyncExternalStore(
        (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
        () => state,
        () => state,
    );
}

/** Called by pages: publishes while mounted, clears on unmount. `talentIds` must be a stable array per change. */
export function usePublishAssistantContext({ projectId, talentIds }) {
    useEffect(() => {
        setAssistantContext({ projectId: projectId ?? null, talentIds: talentIds || [] });
        return () => setAssistantContext({ projectId: null, talentIds: EMPTY });
    }, [projectId, talentIds]);
}
