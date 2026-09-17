import { useCallback, useRef, useState } from "react";
import {
    confirmAssistantCommand,
    postAssistantCommand,
    sha256Hex,
    uploadAssistantAudition,
} from "@/lib/simpleAssistant";

const PROCESSING_STEPS = ["UNDERSTANDING COMMAND", "RESOLVING", "PREPARING ACTION"];

/**
 * Drives the Simple Assistant conversational command layer.
 *
 * Phase 2: understand → resolve → preview.
 * Phase 3: "Confirm" on an executable plan calls /command/confirm, which
 * runs the approved action through the existing Talentgram services and
 * returns a per-talent outcome + a fresh overview. Cancel stays a local
 * no-op. The signed `context` is consumed on confirm (one-shot).
 * Phase 4C: an attached audition file is previewed as an INGEST plan;
 * "Confirm Upload" sends the bytes once to /command/upload.
 */
export function useAssistantCommand({ onExecuted } = {}) {
    const [turns, setTurns] = useState([]); // [{id, user, response, status}]
    const [busy, setBusy] = useState(false);
    const [step, setStep] = useState(null);
    const contextRef = useRef(null);
    const convIdRef = useRef(null);
    const stepTimer = useRef(null);
    const pendingFileRef = useRef(null); // File awaiting an INGEST confirm

    const _startProcessing = (steps) => {
        let i = 0;
        setStep(steps[0]);
        stepTimer.current = setInterval(() => {
            i = Math.min(i + 1, steps.length - 1);
            setStep(steps[i]);
        }, 260);
    };
    const _stopProcessing = () => {
        if (stepTimer.current) clearInterval(stepTimer.current);
        stepTimer.current = null;
        setStep(null);
    };

    const send = useCallback(async (message, file = null) => {
        const text = (message || "").trim();
        if ((!text && !file) || busy) return;
        const turnId = `t_${Date.now()}`;
        setTurns((t) => [...t, { id: turnId, user: text || (file ? `📎 ${file.name}` : ""), response: null, status: "pending" }]);
        setBusy(true);
        _startProcessing(file ? ["READING FILE", "RESOLVING", "PREPARING UPLOAD"] : PROCESSING_STEPS);

        let fileMeta = null;
        if (file) {
            fileMeta = {
                filename: file.name,
                size: file.size,
                content_type: file.type || null,
                sha256: await sha256Hex(file),
            };
            pendingFileRef.current = { turnId, file };
        }

        const { data, error } = await postAssistantCommand({
            message: text,
            conversationId: convIdRef.current,
            context: contextRef.current,
            fileMeta,
        });

        _stopProcessing();
        setBusy(false);

        if (error) {
            const msg =
                error?.response?.status === 404
                    ? "Simple Assistant is not enabled on this environment."
                    : error?.response?.data?.detail || "Couldn't process that command.";
            setTurns((t) =>
                t.map((x) => (x.id === turnId ? { ...x, status: "error", response: { state: "error", message: msg } } : x)),
            );
            return;
        }

        convIdRef.current = data.conversation_id || convIdRef.current;
        contextRef.current = data.context || null;
        setTurns((t) => t.map((x) => (x.id === turnId ? { ...x, status: "done", response: data } : x)));
    }, [busy]);

    const confirm = useCallback(async (turnId, extra) => {
        if (busy) return;
        const ctx = contextRef.current;
        contextRef.current = null; // one-shot — the plan is consumed on confirm

        const turn = turns.find((x) => x.id === turnId);
        const isIngest = turn?.response?.sub?.action_type === "ingest_audition";
        const isAiReply = !!turn?.response?.intelligence?.ai_draft && !turn.response.intelligence.ai_draft.failed;
        const pf = pendingFileRef.current;

        if (isIngest && (!pf || pf.turnId !== turnId || !pf.file)) {
            setTurns((t) =>
                t.map((x) => (x.id === turnId
                    ? { ...x, response: { ...x.response, execError: "The attached file is no longer available — attach it again." } }
                    : x)),
            );
            return;
        }

        setBusy(true);
        _startProcessing(isIngest ? ["UPLOADING AUDITION"] : isAiReply ? ["SENDING REPLY"] : ["APPLYING CHANGE"]);

        const { data, error } = isIngest
            ? await uploadAssistantAudition({ conversationId: convIdRef.current, context: ctx, file: pf.file })
            : await confirmAssistantCommand({
                  conversationId: convIdRef.current, context: ctx,
                  finalText: isAiReply ? (extra?.finalText ?? undefined) : undefined,
              });

        _stopProcessing();
        setBusy(false);
        if (isIngest) pendingFileRef.current = null;

        if (error) {
            const msg = error?.response?.data?.detail || "Couldn't complete that action.";
            setTurns((t) =>
                t.map((x) => (x.id === turnId ? { ...x, response: { ...x.response, execError: msg } } : x)),
            );
            return;
        }

        setTurns((t) =>
            t.map((x) => (x.id === turnId ? { ...x, response: { ...x.response, ...data, executed: true } } : x)),
        );
        if (data?.overview && onExecuted) onExecuted(data.overview);
    }, [busy, turns, onExecuted]);

    const cancel = useCallback((turnId) => {
        contextRef.current = null;
        if (pendingFileRef.current?.turnId === turnId) pendingFileRef.current = null;
        setTurns((t) =>
            t.map((x) =>
                x.id === turnId
                    ? { ...x, response: { ...x.response, state: "cancelled", message: "Cancelled. Nothing was changed." } }
                    : x,
            ),
        );
    }, []);

    const reset = useCallback(() => {
        contextRef.current = null;
        convIdRef.current = null;
        pendingFileRef.current = null;
        setTurns([]);
    }, []);

    return { turns, busy, step, send, confirm, cancel, reset, awaitingClarification: !!contextRef.current };
}
