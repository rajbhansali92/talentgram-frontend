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

// --- UNIFIED RESOLUTION (Slice 1/6) ---
// body: { source_type: "PROJECT"|"CRM"|"MANUAL", source_params, excluded_recipient_ids }
export async function resolveTargets(body) {
  const res = await adminApi.post("/whatsapp/resolve", body);
  return res.data;
}

// --- CRM SOURCE (Slice 2) ---
export async function getCrmContactTypes() {
  const res = await adminApi.get("/whatsapp/crm/contact-types");
  return res.data.contact_types || [];
}
export async function getCrmContacts(params = {}) {
  const res = await adminApi.get("/whatsapp/crm/contacts", { params });
  return res.data;
}

// --- MANUAL (Slice 2 / Feature 5) ---
export async function validateManual(contacts) {
  const res = await adminApi.post("/whatsapp/manual/validate", { contacts });
  return res.data;
}

// --- PROJECT PICKER (Slice 3 / Feature 4) ---
export async function searchProjects(params = {}) {
  const res = await adminApi.get("/whatsapp/projects/search", { params });
  return res.data;
}
export async function getRecentProjects(limit = 10) {
  const res = await adminApi.get("/whatsapp/projects/recent", { params: { limit } });
  return res.data.items || [];
}
export async function getPinnedProjects() {
  const res = await adminApi.get("/whatsapp/projects/pins");
  return res.data.items || [];
}
export async function pinProject(projectId) {
  await adminApi.post(`/whatsapp/projects/pins/${projectId}`);
}
export async function unpinProject(projectId) {
  await adminApi.delete(`/whatsapp/projects/pins/${projectId}`);
}

// --- COMMUNICATION TIMELINE (Slice 4 / Feature 2) ---
export async function getTimeline(subjectType, subjectId, params = {}) {
  const res = await adminApi.get("/whatsapp/timeline", {
    params: { subject_type: subjectType, subject_id: subjectId, ...params },
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

// --- TEMP TEST TOOL — REMOVE AFTER WHATSAPP VALIDATION ---
export async function testInternalNotification() {
  const res = await adminApi.post("/admin/whatsapp/test-internal-notification");
  return res.data;
}
