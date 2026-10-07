import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, { error: (...a) => toastError(...a), success: (...a) => toastSuccess(...a) }),
}));

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import { adminApi } from "@/lib/api";
import AddToProjectModal from "./AddToProjectModal";

afterEach(cleanup);

const PROJECT_A = { id: "proj-a", brand_name: "Project A", status: "ongoing" };
const PROJECT_B = { id: "proj-b", brand_name: "Project B", status: "ongoing" };

const TEMPLATE_CASTING_CALL = { id: "tmpl-casting-call", name: "Casting Call", slug: "casting_call" };
const TEMPLATE_FOLLOW_UP = { id: "tmpl-follow-up", name: "Follow Up", slug: "follow_up" };

// Worker registry rows exactly as GET /whatsapp/workers returns them (each enriched with its live session).
const worker = (id, label, status = "authenticated", sending = true) => ({ id, label, sending_enabled: sending, session: { status } });
const WORKER_1 = worker("default", "Worker 1");
const WORKER_2 = worker("worker-2", "Worker 2");

function mockAdminApi({ projects = [PROJECT_A], templates = [TEMPLATE_CASTING_CALL, TEMPLATE_FOLLOW_UP], workers = [WORKER_1], addResponse, sendResponse } = {}) {
    adminApi.get.mockImplementation((url) => {
        if (url === "/projects") return Promise.resolve({ data: projects });
        if (url === "/whatsapp/templates") return Promise.resolve({ data: templates });
        if (url === "/whatsapp/workers") return Promise.resolve({ data: typeof workers === "function" ? workers() : workers });
        return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    adminApi.post.mockImplementation((url, body) => {
        if (url === "/projects/bulk-add-talents") {
            return Promise.resolve({
                data: addResponse || { added: body.talent_ids.length, project_count: body.project_ids.length, skipped: 0 },
            });
        }
        if (url === "/whatsapp/casting-call/send") {
            return Promise.resolve({ data: sendResponse || { success: true, batches: [{ project_id: body.project_ids[0], batch_id: "batch-1", queued: 1, skipped: 0 }], errors: [] } });
        }
        return Promise.reject(new Error(`unexpected POST ${url}`));
    });
}

beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    toastError.mockReset();
    toastSuccess.mockReset();
});

// Drives the modal from the project picker through "Add", through the
// "Send Casting Call?" confirmation's "Yes"/"Send Casting Call" button, into
// the template picker, matching the real click sequence a user performs.
async function addThenOpenTemplatePicker(projectIds) {
    for (const pid of projectIds) {
        fireEvent.click(await screen.findByTestId(`add-to-project-option-${pid}`));
    }
    fireEvent.click(screen.getByTestId("add-to-project-submit"));
    await screen.findByTestId("send-casting-call-modal");
    fireEvent.click(screen.getByTestId("send-casting-call-confirm"));
    await screen.findByTestId("template-picker-modal");
}

describe("AddToProjectModal — existing Add to Project behaviour is unchanged (regression)", () => {
    it("loads active projects, adds selected talents, and reaches the Send Casting Call? step exactly as before", async () => {
        mockAdminApi({ projects: [PROJECT_A, PROJECT_B] });
        const onSuccess = vi.fn();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={onSuccess} />);

        await screen.findByTestId("add-to-project-option-proj-a");
        expect(screen.getByTestId("add-to-project-option-proj-b")).toBeTruthy();

        fireEvent.click(screen.getByTestId("add-to-project-option-proj-a"));
        fireEvent.click(screen.getByTestId("add-to-project-submit"));

        await screen.findByTestId("send-casting-call-modal");
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/projects/bulk-add-talents");
        expect(payload).toEqual({ project_ids: ["proj-a"], talent_ids: ["t1"] });
    });

    it("'Not Now' sends no WhatsApp request and calls onSuccess/onClose unchanged", async () => {
        mockAdminApi();
        const onSuccess = vi.fn();
        const onClose = vi.fn();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={onClose} onSuccess={onSuccess} />);

        fireEvent.click(await screen.findByTestId("add-to-project-option-proj-a"));
        fireEvent.click(screen.getByTestId("add-to-project-submit"));
        await screen.findByTestId("send-casting-call-modal");

        fireEvent.click(screen.getByTestId("send-casting-call-not-now"));

        expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(false);
        expect(onSuccess).toHaveBeenCalled();
        expect(onClose).toHaveBeenCalled();
    });
});

describe("AddToProjectModal — template picker (new behaviour)", () => {
    it("loads the existing WhatsApp templates via GET /whatsapp/templates when the picker opens", async () => {
        mockAdminApi();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a"]);

        await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`);
        expect(screen.getByText("Casting Call")).toBeTruthy();
        expect(screen.getByText("Follow Up")).toBeTruthy();
        expect(adminApi.get.mock.calls.some(([url]) => url === "/whatsapp/templates")).toBe(true);
    });

    it("Send is disabled until a template is selected — no request is sent without one", async () => {
        mockAdminApi();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a"]);
        await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`);

        const sendBtn = screen.getByTestId("template-picker-send");
        expect(sendBtn.disabled).toBe(true);

        fireEvent.click(sendBtn);
        expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(false);
    });

    it("Cancel from the template picker sends nothing and closes like Not Now", async () => {
        mockAdminApi();
        const onClose = vi.fn();
        const onSuccess = vi.fn();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={onClose} onSuccess={onSuccess} />);
        await addThenOpenTemplatePicker(["proj-a"]);

        fireEvent.click(screen.getByTestId("template-picker-cancel"));

        expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(false);
        expect(onSuccess).toHaveBeenCalled();
        expect(onClose).toHaveBeenCalled();
    });

    it("single talent + single project: selecting a template and sending passes template_id through to the existing send pipeline", async () => {
        mockAdminApi();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_FOLLOW_UP.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1"], project_ids: ["proj-a"], template_id: TEMPLATE_FOLLOW_UP.id, worker_id: "default" });
    });

    it("multiple talents + single project: the selected template is used, existing recipient/talent_ids logic preserved", async () => {
        mockAdminApi();
        render(<AddToProjectModal open talentIds={["t1", "t2", "t3"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1", "t2", "t3"], project_ids: ["proj-a"], template_id: TEMPLATE_CASTING_CALL.id, worker_id: "default" });
    });

    it("single talent + multiple projects: existing multi-project selection is preserved and the selected template is used for all", async () => {
        mockAdminApi({ projects: [PROJECT_A, PROJECT_B] });
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a", "proj-b"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_FOLLOW_UP.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1"], project_ids: ["proj-a", "proj-b"], template_id: TEMPLATE_FOLLOW_UP.id, worker_id: "default" });
    });

    it("multiple talents + multiple projects: existing behaviour preserved end to end with the selected template", async () => {
        mockAdminApi({ projects: [PROJECT_A, PROJECT_B] });
        render(<AddToProjectModal open talentIds={["t1", "t2"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a", "proj-b"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1", "t2"], project_ids: ["proj-a", "proj-b"], template_id: TEMPLATE_CASTING_CALL.id, worker_id: "default" });
    });

    it("search filters the template list by name", async () => {
        mockAdminApi();
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a"]);
        await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`);

        fireEvent.change(screen.getByTestId("template-picker-search"), { target: { value: "Follow" } });

        expect(screen.queryByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`)).toBeNull();
        expect(screen.getByTestId(`template-picker-option-${TEMPLATE_FOLLOW_UP.id}`)).toBeTruthy();
    });
});

// ---------------------------------------------------------------------------------------------------
// "Send via" — the admin chooses which WhatsApp worker sends the casting call.
// ---------------------------------------------------------------------------------------------------
describe("AddToProjectModal — Send via worker selection", () => {
    const sendCalls = () => adminApi.post.mock.calls.filter(([url]) => url === "/whatsapp/casting-call/send");
    const pickTemplate = async (t = TEMPLATE_CASTING_CALL) => fireEvent.click(await screen.findByTestId(`template-picker-option-${t.id}`));
    const open = async (workers, { talents = ["t1"], projects = ["proj-a"], extra = {} } = {}) => {
        mockAdminApi({ workers, projects: [PROJECT_A, PROJECT_B], ...extra });
        const onClose = vi.fn(); const onSuccess = vi.fn();
        render(<AddToProjectModal open talentIds={talents} onClose={onClose} onSuccess={onSuccess} />);
        await addThenOpenTemplatePicker(projects);
        await screen.findByTestId("send-via");
        return { onClose, onSuccess };
    };

    it("renders a 'Send via' selector listing every registered worker with its live status", async () => {
        await open([WORKER_1, WORKER_2]);
        const group = screen.getByRole("radiogroup", { name: "Send via" });
        expect(group).toBeTruthy();
        expect(screen.getByTestId("send-via-default").textContent).toContain("Worker 1");
        expect(screen.getByTestId("send-via-default").textContent).toContain("Connected");
        expect(screen.getByTestId("send-via-worker-2").textContent).toContain("Worker 2");
    });

    it("connected Worker 1 can be selected and its id reaches the send request", async () => {
        await open([WORKER_1, WORKER_2]);
        await pickTemplate();
        fireEvent.click(screen.getByTestId("send-via-default"));
        expect(screen.getByTestId("send-via-default").getAttribute("aria-checked")).toBe("true");
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1]).toEqual({ talent_ids: ["t1"], project_ids: ["proj-a"], template_id: TEMPLATE_CASTING_CALL.id, worker_id: "default" });
    });

    it("connected Worker 2 can be selected and its id reaches the send request", async () => {
        await open([WORKER_1, WORKER_2]);
        await pickTemplate();
        fireEvent.click(screen.getByTestId("send-via-worker-2"));
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1].worker_id).toBe("worker-2");
    });

    it("switching workers updates what is sent (the last choice wins, one request)", async () => {
        await open([WORKER_1, WORKER_2]);
        await pickTemplate();
        fireEvent.click(screen.getByTestId("send-via-default"));
        fireEvent.click(screen.getByTestId("send-via-worker-2"));
        expect(screen.getByTestId("send-via-default").getAttribute("aria-checked")).toBe("false");
        expect(screen.getByTestId("send-via-worker-2").getAttribute("aria-checked")).toBe("true");
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1].worker_id).toBe("worker-2");
    });

    it("with several connected workers nothing is pre-chosen: Send stays disabled until the admin picks one", async () => {
        await open([WORKER_1, WORKER_2]);
        await pickTemplate();
        expect(screen.getByTestId("send-via-default").getAttribute("aria-checked")).toBe("false");
        expect(screen.getByTestId("send-via-worker-2").getAttribute("aria-checked")).toBe("false");
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
        fireEvent.click(screen.getByTestId("template-picker-send"));
        expect(sendCalls().length).toBe(0);
        fireEvent.click(screen.getByTestId("send-via-worker-2"));
        expect(screen.getByTestId("template-picker-send").disabled).toBe(false);
    });

    it("a disconnected worker is shown as such and cannot be selected; the one connected worker is pre-selected", async () => {
        await open([worker("default", "Worker 1", "disconnected"), WORKER_2]);
        const down = screen.getByTestId("send-via-default");
        expect(down.disabled).toBe(true);
        expect(down.textContent).toContain("Disconnected");
        fireEvent.click(down);
        expect(down.getAttribute("aria-checked")).toBe("false");
        expect(screen.getByTestId("send-via-worker-2").getAttribute("aria-checked")).toBe("true");   // only one available -> preselected
        await pickTemplate();
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1].worker_id).toBe("worker-2");
    });

    it("a worker waiting for a QR scan, or with sending switched off, is not selectable either", async () => {
        await open([worker("default", "Worker 1", "qr_pending"), worker("worker-2", "Worker 2", "authenticated", false), worker("worker-3", "Worker 3")]);
        expect(screen.getByTestId("send-via-default").disabled).toBe(true);
        expect(screen.getByTestId("send-via-default").textContent).toContain("Scan QR");
        expect(screen.getByTestId("send-via-worker-2").disabled).toBe(true);
        expect(screen.getByTestId("send-via-worker-2").textContent).toContain("Sending off");
        expect(screen.getByTestId("send-via-worker-3").getAttribute("aria-checked")).toBe("true");
    });

    it("no available worker: sending is prevented with a clear message and no request is made", async () => {
        await open([worker("default", "Worker 1", "disconnected"), worker("worker-2", "Worker 2", "qr_pending")]);
        await pickTemplate();
        expect(screen.getByTestId("send-via-none").textContent).toContain("No WhatsApp worker is currently connected");
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
        fireEvent.click(screen.getByTestId("template-picker-send"));
        expect(sendCalls().length).toBe(0);
    });

    it("an empty registry also blocks sending with the same message", async () => {
        await open([]);
        await pickTemplate();
        expect(screen.getByTestId("send-via-none")).toBeTruthy();
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
    });

    it("renders however many workers the registry returns (not a fixed pair)", async () => {
        await open([WORKER_1, WORKER_2, worker("worker-3", "Worker 3"), worker("worker-4", "Worker 4", "disconnected")]);
        for (const id of ["default", "worker-2", "worker-3", "worker-4"]) expect(screen.getByTestId(`send-via-${id}`)).toBeTruthy();
        await pickTemplate();
        fireEvent.click(screen.getByTestId("send-via-worker-3"));
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1].worker_id).toBe("worker-3");
    });

    it("the template is still required: a connected, chosen worker alone does not enable Send", async () => {
        await open([WORKER_1]);
        expect(screen.getByTestId("send-via-default").getAttribute("aria-checked")).toBe("true");
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
        await pickTemplate(TEMPLATE_FOLLOW_UP);
        expect(screen.getByTestId("template-picker-send").disabled).toBe(false);
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1].template_id).toBe(TEMPLATE_FOLLOW_UP.id);
    });

    it("multiple talents x multiple projects: ONE request carries the chosen worker for the whole operation", async () => {
        await open([WORKER_1, WORKER_2], { talents: ["t1", "t2", "t3"], projects: ["proj-a", "proj-b"] });
        await pickTemplate(TEMPLATE_FOLLOW_UP);
        fireEvent.click(screen.getByTestId("send-via-worker-2"));
        fireEvent.click(screen.getByTestId("template-picker-send"));
        fireEvent.click(screen.getByTestId("template-picker-send"));         // double click while sending
        await waitFor(() => expect(sendCalls().length).toBe(1));
        expect(sendCalls()[0][1]).toEqual({ talent_ids: ["t1", "t2", "t3"], project_ids: ["proj-a", "proj-b"], template_id: TEMPLATE_FOLLOW_UP.id, worker_id: "worker-2" });
    });

    it("if the backend refuses the chosen worker (409), nothing closes: the error is shown, the choice is cleared and the admin can pick another", async () => {
        const { onClose } = await open([WORKER_1, WORKER_2]);
        adminApi.post.mockImplementation((url) => url === "/whatsapp/casting-call/send"
            ? Promise.reject({ response: { status: 409, data: { detail: "Worker 2 is not connected to WhatsApp right now. Choose a connected worker." } } })
            : Promise.reject(new Error("unexpected")));
        await pickTemplate();
        fireEvent.click(screen.getByTestId("send-via-worker-2"));
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toastError).toHaveBeenCalledWith("Worker 2 is not connected to WhatsApp right now. Choose a connected worker."));
        expect(onClose).not.toHaveBeenCalled();
        expect(screen.getByTestId("template-picker-modal")).toBeTruthy();
        await waitFor(() => expect(screen.getByTestId("send-via-worker-2").getAttribute("aria-checked")).toBe("false"));   // no silent switch to Worker 1
        expect(screen.getByTestId("send-via-default").getAttribute("aria-checked")).toBe("false");
        expect(screen.getByTestId("template-picker-send").disabled).toBe(true);
    });

    it("a selected worker that drops offline while the picker is open is de-selected, never sent to", async () => {
        let live = [WORKER_1, WORKER_2];
        vi.useFakeTimers({ shouldAdvanceTime: true });
        try {
            await open(() => live);
            fireEvent.click(screen.getByTestId("send-via-worker-2"));
            expect(screen.getByTestId("send-via-worker-2").getAttribute("aria-checked")).toBe("true");
            live = [WORKER_1, worker("worker-2", "Worker 2", "disconnected")];
            await vi.advanceTimersByTimeAsync(8100);
            await waitFor(() => expect(screen.getByTestId("send-via-worker-2").disabled).toBe(true));
            expect(screen.getByTestId("send-via-worker-2").getAttribute("aria-checked")).toBe("false");
        } finally { vi.useRealTimers(); }
    });

    it("Cancel and Not Now still send nothing", async () => {
        const { onClose } = await open([WORKER_1, WORKER_2]);
        fireEvent.click(screen.getByTestId("template-picker-cancel"));
        expect(sendCalls().length).toBe(0);
        expect(onClose).toHaveBeenCalled();
    });
});
