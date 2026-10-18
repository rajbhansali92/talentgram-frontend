import React, { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { getBatch, getJobs, sendCastingCall } from "@/lib/whatsappApi";
import WhatsAppTemplatePicker from "@/components/WhatsAppTemplatePicker";

const TERMINAL = ["completed", "paused", "cancelled"];
const POLL_MS = 3000;

/**
 * Casting Pipeline → selected talents → "Send Template".
 *
 * The SAME template + worker picker and the SAME backend send path as Global Talent's "Send Casting Call"
 * (POST /whatsapp/casting-call/send), with the project fixed to the one whose pipeline this is — no project
 * picker, the current `projectId` goes straight into the request, and only the talent ids selected on the board
 * are sent. Differences on purpose: `apply_pipeline_move: false` (messaging from inside a project never moves
 * anyone between stages) and the result is narrated completely — who was queued, who was skipped and why, and
 * (by polling the existing batch/job status endpoints) who was actually delivered or failed.
 *
 * `talentIds` is captured by the caller at click time; `null` = closed.
 */
export default function PipelineTemplateSend({ talentIds, projectId, projectName, onClose }) {
    const open = Array.isArray(talentIds);
    const [sending, setSending] = useState(false);
    const inFlight = useRef(false);        // synchronous lock: a double click can never start a second request
    const pickerRef = useRef(null);
    const [watch, setWatch] = useState([]); // [{ batch_id, project_name }] awaiting a final delivery result

    const handleSend = async ({ templateId, workerId }) => {
        if (inFlight.current || !open || !talentIds.length || !projectId || !templateId || !workerId) return;
        inFlight.current = true;
        setSending(true);
        let keepOpen = false;
        try {
            const result = await sendCastingCall({
                talent_ids: talentIds,
                project_ids: [projectId],
                template_id: templateId,
                worker_id: workerId,
                apply_pipeline_move: false,
            });
            const batch = (result.batches || [])[0];
            const n = batch?.queued || 0;
            const name = batch?.project_name || projectName || "this project";
            toast.success(`Queued ${n} message${n === 1 ? "" : "s"} for ${name}.`);
            if (batch?.skipped_recipients?.length) {
                const names = batch.skipped_recipients.map((s) => s.name || s.talent_id);
                const shown = names.slice(0, 4).join(", ") + (names.length > 4 ? ` +${names.length - 4} more` : "");
                (toast.warning || toast)(`${names.length} not queued — no WhatsApp number or group on file: ${shown}.`);
            }
            if (batch?.not_in_project?.length) {
                (toast.warning || toast)(`${batch.not_in_project.length} selected talent${batch.not_in_project.length === 1 ? " is" : "s are"} not in this project's pipeline and ${batch.not_in_project.length === 1 ? "was" : "were"} skipped.`);
            }
            if (result.errors?.length) toast.error(`Not queued: ${result.errors.map((e) => e.error || e.project_name || e.project_id).join("; ")}`);
            if (batch?.batch_id) setWatch((prev) => [...prev, { batch_id: batch.batch_id, project_name: name }]);
        } catch (err) {
            const st = err?.response?.status;
            const detail = err?.response?.data?.detail;
            toast.error(
                typeof detail === "string" && /no sendable recipients/i.test(detail)
                    ? "None of the selected talents has a WhatsApp number or group — nothing was queued."
                    : formatErrorDetail(err, "Failed to queue the messages"),
            );
            // The chosen worker was refused (not connected / sending off): nothing was queued, so stay on this
            // step with a fresh worker list and let the admin pick another — never auto-switch.
            if (st === 409 || st === 403) {
                keepOpen = true;
                pickerRef.current?.clearWorkerChoice();
            }
        } finally {
            inFlight.current = false;
            setSending(false);
            if (!keepOpen) onClose();
        }
    };

    // Narrates the final delivery result from the EXISTING batch/job endpoints (the worker, not this click, decides
    // what was sent) — same approach as Global Talent's casting-call watcher, minus any pipeline move.
    useEffect(() => {
        if (watch.length === 0) return undefined;
        let active = true;
        const tick = async () => {
            try {
                const states = await Promise.all(watch.map(async (w) => ({ w, b: await getBatch(w.batch_id).catch(() => null) })));
                if (!active) return;
                const done = states.filter((s) => s.b && TERMINAL.includes(s.b.status));
                for (const { w } of done) {
                    const jobs = await getJobs(w.batch_id).catch(() => []);
                    const sent = jobs.filter((j) => j.status === "sent");
                    const failed = jobs.filter((j) => j.status === "failed");
                    if (sent.length === 0 && failed.length === 0) continue;
                    if (failed.length === 0) toast.success(`Sent to ${sent.length} talent${sent.length === 1 ? "" : "s"} · ${w.project_name}`);
                    else if (sent.length === 0) toast.error(`Sending failed for ${failed.length} talent${failed.length === 1 ? "" : "s"}: ${failed.slice(0, 3).map((j) => j.talent_name || "Unknown").join(", ")}`);
                    else (toast.warning || toast)(`Sent to ${sent.length}; ${failed.length} failed: ${failed.slice(0, 3).map((j) => j.talent_name || "Unknown").join(", ")}`);
                }
                if (!active) return;
                const finished = new Set(done.map((s) => s.w.batch_id));
                if (finished.size) setWatch((prev) => prev.filter((x) => !finished.has(x.batch_id)));
            } catch { /* transient — keep polling */ }
        };
        const timer = setInterval(tick, POLL_MS);
        return () => { active = false; clearInterval(timer); };
    }, [watch]);

    if (!open) return null;
    const n = talentIds.length;
    return (
        <WhatsAppTemplatePicker
            ref={pickerRef}
            sending={sending}
            onSend={handleSend}
            onCancel={onClose}
            contextLabel={`${n} selected talent${n === 1 ? "" : "s"} · ${projectName || "this project"}`}
        />
    );
}
