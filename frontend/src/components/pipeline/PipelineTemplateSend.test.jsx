import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";

const toastSuccess = vi.fn(); const toastError = vi.fn(); const toastWarning = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign(() => {}, { success: (...a) => toastSuccess(...a), error: (...a) => toastError(...a), warning: (...a) => toastWarning(...a) }),
}));
vi.mock("@/lib/api", () => ({ adminApi: { get: vi.fn(), post: vi.fn() } }));

import { adminApi } from "@/lib/api";
import PipelineTemplateSend from "./PipelineTemplateSend";

afterEach(cleanup);

const CASTING = { id: "t-cc", name: "Casting Call" };
const FOLLOW = { id: "t-fu", name: "Follow Up" };
const SHORT = { id: "t-sl", name: "Shortlisted" };
const worker = (id, label, status = "authenticated", sending = true) => ({ id, label, sending_enabled: sending, session: { status } });
const W1 = worker("default", "Worker 1"); const W2 = worker("worker-2", "Worker 2");

let SEND; // what POST /whatsapp/casting-call/send does
function server({ workers = [W1, W2], templates = [CASTING, FOLLOW, SHORT], send, batch, jobs } = {}) {
    SEND = send || ((body) => Promise.resolve({ data: { success: true, errors: [], batches: [{ project_id: body.project_ids[0], project_name: "Amazon", batch_id: "b1", queued: body.talent_ids.length, skipped: 0, skipped_recipients: [], not_in_project: [] }] } }));
    adminApi.get.mockImplementation((url) => {
        if (url === "/whatsapp/templates") return Promise.resolve({ data: templates });
        if (url === "/whatsapp/workers") return Promise.resolve({ data: workers });
        if (url === "/whatsapp/batches/b1") return Promise.resolve({ data: batch || { id: "b1", status: "running" } });
        if (url === "/whatsapp/batches/b1/jobs") return Promise.resolve({ data: jobs || [] });
        return Promise.reject(new Error("unexpected GET " + url));
    });
    adminApi.post.mockImplementation((url, body) => (url === "/whatsapp/casting-call/send" ? SEND(body) : Promise.reject(new Error("unexpected POST " + url))));
}
beforeEach(() => {
    adminApi.get.mockReset(); adminApi.post.mockReset();
    [toastSuccess, toastError, toastWarning].forEach((f) => f.mockReset());
});

const IDS = ["tal-1", "tal-2", "tal-3"];
function mount(props = {}) {
    const onClose = vi.fn();
    const utils = render(<PipelineTemplateSend talentIds={IDS} projectId="proj-amazon" projectName="Amazon" onClose={onClose} {...props} />);
    return { onClose, ...utils };
}
const sends = () => adminApi.post.mock.calls.filter((c) => c[0] === "/whatsapp/casting-call/send");
async function pick(template = CASTING, w = W1) {
    fireEvent.click(await screen.findByTestId(`template-picker-option-${template.id}`));
    fireEvent.click(await screen.findByTestId(`send-via-${w.id}`));
}

describe("PipelineTemplateSend — the picker", () => {
    it("renders nothing while closed (talentIds null) and makes no request", () => {
        server();
        render(<PipelineTemplateSend talentIds={null} projectId="p" projectName="A" onClose={() => {}} />);
        expect(screen.queryByTestId("template-picker-modal")).toBeNull();
        expect(adminApi.get).not.toHaveBeenCalled();
    });

    it("opens the existing template picker with the selection and the current project named — no project picker", async () => {
        server();
        mount();
        expect((await screen.findByTestId("template-picker-context")).textContent).toBe("3 selected talents · Amazon");
        expect(screen.queryByTestId("add-to-project-modal")).toBeNull();
        expect(screen.queryByText(/add to project/i)).toBeNull();
        expect(adminApi.get).toHaveBeenCalledWith("/whatsapp/templates");           // the one existing catalogue
        expect(adminApi.get).toHaveBeenCalledWith("/whatsapp/workers");             // the one existing worker registry
        for (const t of [CASTING, FOLLOW, SHORT]) expect(await screen.findByTestId(`template-picker-option-${t.id}`)).toBeTruthy();
    });

    it("single talent wording", async () => {
        server();
        mount({ talentIds: ["only"] });
        expect((await screen.findByTestId("template-picker-context")).textContent).toBe("1 selected talent · Amazon");
    });

    it("searches templates by name", async () => {
        server();
        mount();
        await screen.findByTestId("template-picker-option-t-cc");
        fireEvent.change(screen.getByTestId("template-picker-search"), { target: { value: "follow" } });
        expect(screen.queryByTestId("template-picker-option-t-cc")).toBeNull();
        expect(screen.getByTestId("template-picker-option-t-fu")).toBeTruthy();
        fireEvent.change(screen.getByTestId("template-picker-search"), { target: { value: "zzz" } });
        expect(screen.getByText("No templates match your search")).toBeTruthy();
    });

    it("shows the real connection state of each worker; only a connected, sending-enabled one is selectable", async () => {
        server({ workers: [W1, worker("worker-2", "Worker 2", "qr_pending"), worker("w3", "Worker 3", "authenticated", false)] });
        mount();
        expect((await screen.findByTestId("send-via-default")).textContent).toContain("Connected");
        expect(screen.getByTestId("send-via-worker-2").textContent).toContain("Scan QR");
        expect(screen.getByTestId("send-via-worker-2").disabled).toBe(true);
        expect(screen.getByTestId("send-via-w3").textContent).toContain("Sending off");
        expect(screen.getByTestId("send-via-w3").disabled).toBe(true);
    });

    it("Send stays disabled until BOTH a template and a worker are chosen", async () => {
        server();
        mount();
        const send = await screen.findByTestId("template-picker-send");
        expect(send.disabled).toBe(true);
        fireEvent.click(await screen.findByTestId("template-picker-option-t-cc"));
        expect(send.disabled).toBe(true);                                            // two connected workers: nothing pre-chosen
        fireEvent.click(await screen.findByTestId("send-via-worker-2"));
        expect(send.disabled).toBe(false);
        expect(sends()).toHaveLength(0);
    });

    it("no connected worker: a clear message and no way to send", async () => {
        server({ workers: [worker("default", "Worker 1", "disconnected"), worker("worker-2", "Worker 2", "qr_pending")] });
        mount();
        expect(await screen.findByTestId("send-via-none")).toBeTruthy();
        fireEvent.click(await screen.findByTestId("template-picker-option-t-cc"));
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
    });

    it("Cancel closes without sending", async () => {
        server();
        const { onClose } = mount();
        fireEvent.click(await screen.findByTestId("template-picker-cancel"));
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(sends()).toHaveLength(0);
    });
});

describe("PipelineTemplateSend — what is sent", () => {
    it.each([[W1, "default"], [W2, "worker-2"]])("sends the selected template via %s with the CURRENT project id and only the selected talent ids", async (w, wid) => {
        server();
        mount();
        await pick(CASTING, w);
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sends()).toHaveLength(1));
        expect(sends()[0][1]).toEqual({
            talent_ids: IDS, project_ids: ["proj-amazon"], template_id: "t-cc", worker_id: wid,
            apply_pipeline_move: false,                                              // messaging never moves a stage from the pipeline
        });
    });

    it("any other existing template goes through the same request, only the template id differs", async () => {
        server();
        mount();
        await pick(SHORT, W1);
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sends()).toHaveLength(1));
        expect(sends()[0][1]).toMatchObject({ template_id: "t-sl", project_ids: ["proj-amazon"], talent_ids: IDS, apply_pipeline_move: false });
    });

    it("success: tells how many were queued, closes the picker", async () => {
        server();
        const { onClose } = mount();
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Queued 3 messages for Amazon."));
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(toastWarning).not.toHaveBeenCalled();
    });

    it("recipients that could not be queued are named, never silently dropped, and the rest is still reported as queued", async () => {
        server({ send: () => Promise.resolve({ data: { success: true, errors: [], batches: [{ project_id: "proj-amazon", project_name: "Amazon", batch_id: "b1", queued: 1, skipped: 2,
            skipped_recipients: [{ talent_id: "tal-2", name: "Nina NoPhone", reason: "No whatsapp_group_name and no phone number" }, { talent_id: "tal-3", name: "Omar NoPhone", reason: "x" }],
            not_in_project: ["tal-9"] }] } }) });
        mount();
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Queued 1 message for Amazon."));
        expect(toastWarning).toHaveBeenCalledWith(expect.stringContaining("2 not queued"));
        expect(toastWarning.mock.calls[0][0]).toContain("Nina NoPhone");
        expect(toastWarning.mock.calls[0][0]).toContain("Omar NoPhone");
        expect(toastWarning).toHaveBeenCalledWith(expect.stringContaining("not in this project's pipeline"));
    });

    it("nobody can be queued (no WhatsApp number or group): a plain explanation, nothing claimed as queued", async () => {
        server({ send: () => Promise.reject({ response: { status: 400, data: { detail: "Could not queue any casting calls: [{'error': 'No sendable recipients found (3 unresolvable).'}]" } } }) });
        mount();
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastError).toHaveBeenCalledWith("None of the selected talents has a WhatsApp number or group — nothing was queued."));
        expect(toastSuccess).not.toHaveBeenCalled();
    });

    it("an API failure is reported and does not claim success", async () => {
        server({ send: () => Promise.reject({ response: { status: 500, data: { detail: "Boom" } } }) });
        const { onClose } = mount();
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastError).toHaveBeenCalled());
        expect(toastSuccess).not.toHaveBeenCalled();
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it("a project-level error from the backend is surfaced", async () => {
        server({ send: () => Promise.resolve({ data: { success: true, batches: [{ project_id: "proj-amazon", batch_id: "b1", queued: 1, skipped: 0 }], errors: [{ project_id: "x", error: "Project not found" }] } }) });
        mount();
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastError).toHaveBeenCalledWith(expect.stringContaining("Project not found")));
    });

    it("if the backend refuses the chosen worker (409) the picker stays open, the choice is cleared and another can be picked", async () => {
        server({ send: () => Promise.reject({ response: { status: 409, data: { detail: "Worker not connected" } } }) });
        const { onClose } = mount();
        await pick(CASTING, W1);
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastError).toHaveBeenCalled());
        expect(onClose).not.toHaveBeenCalled();
        await waitFor(() => expect(screen.getByTestId("send-via-default").getAttribute("aria-checked")).toBe("false"));
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
    });

    it("a double click (or repeated clicks while the request runs) sends exactly one request", async () => {
        let finish;
        server({ send: () => new Promise((r) => { finish = () => r({ data: { success: true, errors: [], batches: [{ project_id: "proj-amazon", project_name: "Amazon", batch_id: "b1", queued: 3, skipped: 0 }] } }); }) });
        mount();
        await pick();
        const send = screen.getByTestId("template-picker-send");
        fireEvent.click(send); fireEvent.click(send); fireEvent.click(send);
        expect(sends()).toHaveLength(1);
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);     // locked while sending
        expect(screen.getByTestId("template-picker-cancel").disabled).toBe(true);
        await act(async () => { finish(); });
        await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
        expect(sends()).toHaveLength(1);
    });

    it("never sends with an empty selection or without a project", async () => {
        server();
        mount({ talentIds: [] });
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await act(async () => {});
        expect(sends()).toHaveLength(0);
    });
});

describe("PipelineTemplateSend — final delivery result (from the existing batch/job endpoints)", () => {
    beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
    afterEach(() => vi.useRealTimers());

    async function sendOnce(opts) {
        server(opts);
        mount();
        await pick();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(expect.stringContaining("Queued")));
    }

    it("reports who was actually sent to once the batch finishes", async () => {
        await sendOnce({ batch: { id: "b1", status: "completed" }, jobs: [{ status: "sent", talent_name: "A" }, { status: "sent", talent_name: "B" }, { status: "sent", talent_name: "C" }] });
        await act(async () => { await vi.advanceTimersByTimeAsync(3200); });
        await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Sent to 3 talents · Amazon"));
    });

    it("reports failures by name and does not claim full success", async () => {
        await sendOnce({ batch: { id: "b1", status: "completed" }, jobs: [{ status: "sent", talent_name: "A" }, { status: "failed", talent_name: "Fay" }] });
        await act(async () => { await vi.advanceTimersByTimeAsync(3200); });
        await waitFor(() => expect(toastWarning).toHaveBeenCalledWith("Sent to 1; 1 failed: Fay"));
        expect(toastSuccess).not.toHaveBeenCalledWith(expect.stringContaining("Sent to 2"));
    });

    it("everything failed is an error, not a success", async () => {
        await sendOnce({ batch: { id: "b1", status: "completed" }, jobs: [{ status: "failed", talent_name: "Fay" }] });
        await act(async () => { await vi.advanceTimersByTimeAsync(3200); });
        await waitFor(() => expect(toastError).toHaveBeenCalledWith(expect.stringContaining("Sending failed for 1 talent: Fay")));
    });
});
