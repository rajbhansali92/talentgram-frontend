/**
 * API client functions for the WhatsApp Engine module.
 */
import { adminApi } from "./api";

// --- TEMPLATES ---
export async function getTemplates() {
  const res = await adminApi.get("/whatsapp/templates");
  return res.data;
}

export async function getTemplate(id) {
  const res = await adminApi.get(`/whatsapp/templates/${id}`);
  return res.data;
}

export async function createTemplate(data) {
  const res = await adminApi.post("/whatsapp/templates", data);
  return res.data;
}

export async function updateTemplate(id, data) {
  const res = await adminApi.put(`/whatsapp/templates/${id}`, data);
  return res.data;
}

export async function deleteTemplate(id) {
  await adminApi.delete(`/whatsapp/templates/${id}`);
}

// --- PROJECTS & PIPELINES ---
export async function getProjects() {
  const res = await adminApi.get("/whatsapp/projects");
  return res.data;
}

export async function getPipelineSummary(projectId) {
  const res = await adminApi.get(`/whatsapp/projects/${projectId}/pipeline-summary`);
  return res.data;
}

export async function resolveRecipients(projectId, stages) {
  const res = await adminApi.get(`/whatsapp/projects/${projectId}/resolve-recipients`, {
    params: { stages }
  });
  return res.data;
}

// --- BATCHES ---
export async function createBatch(data) {
  const res = await adminApi.post("/whatsapp/batches", data);
  return res.data;
}

export async function getBatches(projectId = null) {
  const res = await adminApi.get("/whatsapp/batches", {
    params: projectId ? { project_id: projectId } : {}
  });
  return res.data;
}

export async function getBatch(id) {
  const res = await adminApi.get(`/whatsapp/batches/${id}`);
  return res.data;
}

export async function runBatchAction(id, action) {
  const res = await adminApi.post(`/whatsapp/batches/${id}/action`, { action });
  return res.data;
}

// --- JOBS ---
export async function getJobs(batchId, statusFilter = null) {
  const res = await adminApi.get(`/whatsapp/batches/${batchId}/jobs`, {
    params: statusFilter ? { status_filter: statusFilter } : {}
  });
  return res.data;
}

export async function retryJob(batchId, jobId) {
  const res = await adminApi.post(`/whatsapp/batches/${batchId}/jobs/${jobId}/retry`);
  return res.data;
}

// --- SESSIONS ---
export async function getSessionStatus() {
  const res = await adminApi.get("/whatsapp/session");
  return res.data;
}

export async function clearQrCode() {
  await adminApi.post("/whatsapp/session/clear-qr");
}

export async function resetSession() {
  await adminApi.post("/whatsapp/session/reset");
}

// --- CONFIG ---
export async function getWaConfig() {
  const res = await adminApi.get("/whatsapp/config");
  return res.data;
}

export async function updateWaConfig(key, value) {
  const res = await adminApi.put(`/whatsapp/config/${key}`, { value: String(value) });
  return res.data;
}

// --- AUDIT LOG ---
export async function getAuditLog(params = {}) {
  const res = await adminApi.get("/whatsapp/audit-log", { params });
  return res.data;
}
