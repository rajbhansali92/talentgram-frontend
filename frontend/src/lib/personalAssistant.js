/**
 * Client for /api/personal-assistant/* (backend/personal_assistant). Thin wrappers only: every consequential
 * decision (who may use it, which ids are valid, what is allowed) is made by the backend. All calls go through
 * the admin axios instance, i.e. the logged-in user's own session — no user id is ever sent by the browser.
 */
import { adminApi } from "@/lib/api";

const BASE = "/personal-assistant";

export async function getAssistantAccess() {
    const { data } = await adminApi.get(`${BASE}/access`);
    return data;
}
export async function sendAssistantMessage({ text, clientRequestId, context }) {
    const { data } = await adminApi.post(`${BASE}/message`, { text, client_request_id: clientRequestId, context: context || {} });
    return data;
}
export async function clarifyTask(taskId, body) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/clarify`, body);
    return data.task;
}
export async function confirmTask(taskId, workerId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/confirm`, workerId ? { worker_id: workerId } : {});
    return data.task;
}
export async function confirmAll(commandId, workerId) {
    const { data } = await adminApi.post(`${BASE}/commands/${commandId}/confirm-all`, workerId ? { worker_id: workerId } : {});
    return data.results;
}
export async function cancelTask(taskId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/cancel`);
    return data.task;
}
export async function retryTask(taskId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/retry`);
    return data.task;
}
export async function followUpSend(taskId, templateId) {
    const { data } = await adminApi.post(`${BASE}/tasks/${taskId}/follow-up`, { template_id: templateId });
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
