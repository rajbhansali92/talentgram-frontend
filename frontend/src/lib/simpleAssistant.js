import { adminApi } from "@/lib/api";

/**
 * Simple Assistant — isolated feature module.
 *
 * Feature flag: OFF by default. Set NEXT_PUBLIC_SIMPLE_ASSISTANT_ENABLED="true"
 * to show the nav entry + route; anything else (unset included) hides it.
 * The backend has an independent SIMPLE_ASSISTANT_ENABLED kill-switch.
 *
 * Phase 2 is dry-run only: /command and /command/confirm never mutate
 * production data.
 */
export const SIMPLE_ASSISTANT_ENABLED =
    ["true", "1", "yes", "on"].includes(
        (process.env.NEXT_PUBLIC_SIMPLE_ASSISTANT_ENABLED ?? "false")
            .toString()
            .trim()
            .toLowerCase(),
    );

export const SIMPLE_ASSISTANT_PATH = "/admin/simple-assistant";

/** Read-only situational-awareness snapshot. Resolves to {data} | {error}. */
export function fetchAssistantOverview(signal) {
    return adminApi.get("/simple-assistant/overview", { signal }).then(
        (res) => ({ data: res.data }),
        (error) => ({ error }),
    );
}

/** Dry-run command. Resolves to {data} | {error}; never rejects.
 *  `fileMeta` (name/size/type/hash only — never bytes) is sent when the user
 *  has attached an audition file; the bytes go later via uploadAssistantAudition. */
export function postAssistantCommand({ message, conversationId, context, fileMeta }, signal) {
    return adminApi
        .post(
            "/simple-assistant/command",
            { message, conversation_id: conversationId, context, file_meta: fileMeta ?? null },
            { signal },
        )
        .then(
            (res) => ({ data: res.data }),
            (error) => ({ error }),
        );
}

/** Reconcile the real per-job WhatsApp delivery outcome after a send. */
export function assistantCommStatus({ conversationId, planId, batchIds, statusToken }, signal) {
    return adminApi
        .post(
            "/simple-assistant/command/comm-status",
            { conversation_id: conversationId, plan_id: planId, batch_ids: batchIds, status_token: statusToken },
            { signal },
        )
        .then(
            (res) => ({ data: res.data }),
            (error) => ({ error }),
        );
}

/** SHA-256 hex of a File, for early tamper-evidence + duplicate detection.
 *  Best-effort: returns null where SubtleCrypto is unavailable (the server
 *  computes the authoritative hash at upload time regardless). */
export async function sha256Hex(file) {
    try {
        const subtle = globalThis.crypto?.subtle;
        if (!subtle || !file?.arrayBuffer) return null;
        const buf = await file.arrayBuffer();
        const digest = await subtle.digest("SHA-256", buf);
        return Array.from(new Uint8Array(digest))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");
    } catch {
        return null;
    }
}

/**
 * Upload a NEW incoming audition file for a previewed INGEST plan. The bytes
 * travel here exactly once, only after the user confirmed the preview. The
 * backend re-verifies the signed plan + the file hash before it touches the
 * canonical submission upload service.
 */
export function uploadAssistantAudition({ conversationId, context, file }, signal) {
    const fd = new FormData();
    fd.append("context", JSON.stringify(context ?? {}));
    if (conversationId) fd.append("conversation_id", conversationId);
    fd.append("file", file);
    return adminApi
        .post("/simple-assistant/command/upload", fd, {
            signal,
            headers: { "Content-Type": "multipart/form-data" },
        })
        .then(
            (res) => ({ data: res.data }),
            (error) => ({ error }),
        );
}

/** Execute / send a previewed plan. Backend re-validates the signed plan. */
export function confirmAssistantCommand({ conversationId, context, finalText }, signal) {
    return adminApi
        .post(
            "/simple-assistant/command/confirm",
            { conversation_id: conversationId, context, final_text: finalText ?? null },
            { signal },
        )
        .then(
            (res) => ({ data: res.data }),
            (error) => ({ error }),
        );
}

export const QUICK_COMMANDS = [
    "Show today's priorities",
    "What needs my attention?",
    "Which talents have tests received?",
    "Mark a talent unavailable for a project",
    "Send a talent's intro to the casting group",
];
