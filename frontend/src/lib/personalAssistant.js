/**
 * Client for /api/personal-assistant/* (backend/personal_assistant). Thin wrappers only: every consequential
 * decision (who may use it, which ids are valid, what is allowed) is made by the backend. All calls go through
 * the admin axios instance, i.e. the logged-in user's own session — no user id is ever sent by the browser.
 */
import { adminApi } from "@/lib/api";

const BASE = "/personal-assistant";
// A hung request must not leave a button spinning forever. On a timeout the server MAY still have done the work, so
// callers refresh the persisted state instead of assuming either outcome (see AssistantPanel.act).
const T = { timeout: 45000 };

export async function getAssistantAccess() {
    const { data } = await adminApi.get(`${BASE}/access`);
    return data;
}
export async function sendAssistantMessage({ text, clientRequestId, context }) {
    const { data } = await adminApi.post(`${BASE}/message`, { text, client_request_id: clientRequestId, context: context || {} }, T);
    return data;
}
export async function clarifyTask(taskId, body) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/clarify`, body, T);
    return data.task;
}
// An approval is for exactly what was shown: the server's `fingerprint` of the task (a hash of the resolved entities, destination
// and content) goes back with the approval. If the plan has changed since, the server refuses and returns the revised task(s).
// Uploading an approved update's files goes through the existing authenticated API, one file at a time; a big video can take a few minutes.
const UPLOAD_TIMEOUT_MS = 10 * 60 * 1000;
export async function uploadUpdateMedia(taskId, category, key, file, onProgress) {
    const fd = new FormData();
    fd.append("category", category);
    fd.append("key", key);
    fd.append("file", file);
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/media`, fd, {
        headers: { "Content-Type": "multipart/form-data" }, timeout: UPLOAD_TIMEOUT_MS,
        onUploadProgress: (e) => { if (onProgress && e.total) onProgress(Math.min(99, Math.round((e.loaded / e.total) * 100))); },
    });
    return data.task;
}
export async function getAssistantTask(taskId) {
    const { data } = await adminApi.get(`${BASE}/tasks/${taskId}`);
    return data.task;
}
/** The same limits as the talent page / backend (20 MB pictures; 200 MB MP4 or MOV video). → the reason a file is not acceptable, or null. */
export const MAX_IMAGE_MB = 20;
export const MAX_VIDEO_MB = 200;
export function fileProblem(category, file) {
    const video = category === "video";
    const cap = (video ? MAX_VIDEO_MB : MAX_IMAGE_MB) * 1024 * 1024;
    if (!file.size) return `${file.name} is empty.`;
    if (file.size > cap) return `${file.name} is too large — the limit is ${video ? MAX_VIDEO_MB : MAX_IMAGE_MB} MB.`;
    const okType = video ? ["video/mp4", "video/quicktime"] : ["image/jpeg", "image/png", "image/webp", "image/jpg", "image/heic", "image/heif"];
    const okExt = video ? /\.(mp4|mov)$/i : /\.(jpe?g|png|webp|heic|heif)$/i;
    if (!okType.includes(file.type) && !(!file.type && okExt.test(file.name || ""))) return `${file.name} isn't an accepted format — use ${video ? "MP4 or MOV" : "JPEG, PNG, WEBP or HEIC"}.`;
    return null;
}
export const fileKey = (f) => `${f.name}|${f.size}`;

export async function confirmTask(taskId, workerId, fingerprint, ackUnverified = false) {
    const body = {};
    if (workerId) body.worker_id = workerId;
    if (fingerprint) body.fingerprint = fingerprint;
    if (ackUnverified) body.ack_unverified = true;      // "send anyway": the worker has never been seen sending to this group
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/confirm`, body, T);
    return data.task;
}
export async function confirmAll(commandId, workerId, fingerprints, ackUnverified = false) {
    const body = {};
    if (workerId) body.worker_id = workerId;
    if (fingerprints) body.fingerprints = fingerprints;
    if (ackUnverified) body.ack_unverified = true;
    const { data } = await adminApi.post(`${BASE}/commands/${commandId}/confirm-all`, body, T);
    return data.results;
}
export async function cancelCommand(commandId) {
    const { data } = await adminApi.post(`${BASE}/commands/${commandId}/cancel`, undefined, T);
    return data;
}
/** The human message of an error: a plain string detail, or {message} (plan changed / needs acknowledgement). */
export function errorDetail(err) {
    const d = err?.response?.data?.detail;
    return typeof d === "string" ? d : d && typeof d === "object" && typeof d.message === "string" ? d.message : null;
}
/** A 409 whose detail is {message, changed, tasks}: the plan changed after it was shown. → that detail, else null. */
export function changedPlan(err) {
    const d = err?.response?.data?.detail;
    return d && typeof d === "object" && d.changed ? d : null;
}
export async function cancelTask(taskId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/cancel`, undefined, T);
    return data.task;
}
export async function retryTask(taskId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/retry`, undefined, T);
    return data.task;
}
export async function followUpSend(taskId, templateId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/follow-up`, { template_id: templateId }, T);
    return data.task;
}
export async function listAssistantTasks(limit = 30) {
    const { data } = await adminApi.get(`${BASE}/tasks`, { params: { limit } });
    return data.tasks || [];
}
export async function getAssistantHistory() {
    const { data } = await adminApi.get(`${BASE}/history`);
    return data.messages || [];
}
export async function clearAssistantHistory() {
    await adminApi.post(`${BASE}/history/clear`);
}
export async function getAssistantWorkers() {
    const { data } = await adminApi.get(`${BASE}/workers`);
    return data.workers || [];
}

/** Statuses the backend reports — one place for labels/tones so the queue and the cards agree. */
export const STATUS_META = {
    needs_clarification: { label: "Needs your pick", tone: "amber" },
    awaiting_confirmation: { label: "Awaiting confirmation", tone: "blue" },
    ready: { label: "Ready", tone: "blue" },
    queued: { label: "Queued", tone: "slate" },
    running: { label: "Running", tone: "blue" },
    completed: { label: "Completed", tone: "green" },
    partial: { label: "Partly completed", tone: "amber" },
    failed: { label: "Failed", tone: "red" },
    cancelled: { label: "Cancelled", tone: "slate" },
};
/** What the badge says. For a WhatsApp send the words matter: "Queued" = waiting in the queue, "Sent" = the worker reports it handed the message to WhatsApp. */
export function statusLabel(status, kind, uncertain = false) {
    const send = kind === "send" || kind === "share";
    if (uncertain && status === "failed") return "Uncertain";      // interrupted mid-run: we can't tell whether it went out — never the same word as a clean failure
    if (send && status === "running") return "Sending";
    if (send && status === "completed") return "Sent";
    if (send && status === "partial") return "Partly sent";
    return (STATUS_META[status] || { label: status }).label;
}

export function isTimeoutError(err) {
    return !err?.response && (err?.code === "ECONNABORTED" || /timeout|network/i.test(err?.message || ""));
}

export const ACTIVE_STATUSES = ["queued", "running"];
export const ATTENTION_STATUSES = ["needs_clarification", "awaiting_confirmation"];

export function newRequestId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** The project the user is looking at, from the admin URL (/admin/projects/:id[...]). */
export function projectIdFromPath(pathname) {
    const m = /^\/admin\/projects\/([^/?#]+)/.exec(pathname || "");
    return m && m[1] !== "new" ? m[1] : null;
}
