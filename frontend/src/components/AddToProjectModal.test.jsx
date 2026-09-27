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

function mockAdminApi({ projects = [PROJECT_A], templates = [TEMPLATE_CASTING_CALL, TEMPLATE_FOLLOW_UP], addResponse, sendResponse } = {}) {
    adminApi.get.mockImplementation((url) => {
        if (url === "/projects") return Promise.resolve({ data: projects });
        if (url === "/whatsapp/templates") return Promise.resolve({ data: templates });
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
        expect(payload).toEqual({ talent_ids: ["t1"], project_ids: ["proj-a"], template_id: TEMPLATE_FOLLOW_UP.id });
    });

    it("multiple talents + single project: the selected template is used, existing recipient/talent_ids logic preserved", async () => {
        mockAdminApi();
        render(<AddToProjectModal open talentIds={["t1", "t2", "t3"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1", "t2", "t3"], project_ids: ["proj-a"], template_id: TEMPLATE_CASTING_CALL.id });
    });

    it("single talent + multiple projects: existing multi-project selection is preserved and the selected template is used for all", async () => {
        mockAdminApi({ projects: [PROJECT_A, PROJECT_B] });
        render(<AddToProjectModal open talentIds={["t1"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a", "proj-b"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_FOLLOW_UP.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1"], project_ids: ["proj-a", "proj-b"], template_id: TEMPLATE_FOLLOW_UP.id });
    });

    it("multiple talents + multiple projects: existing behaviour preserved end to end with the selected template", async () => {
        mockAdminApi({ projects: [PROJECT_A, PROJECT_B] });
        render(<AddToProjectModal open talentIds={["t1", "t2"]} onClose={vi.fn()} onSuccess={vi.fn()} />);
        await addThenOpenTemplatePicker(["proj-a", "proj-b"]);

        fireEvent.click(await screen.findByTestId(`template-picker-option-${TEMPLATE_CASTING_CALL.id}`));
        fireEvent.click(screen.getByTestId("template-picker-send"));

        await waitFor(() => expect(adminApi.post.mock.calls.some(([url]) => url === "/whatsapp/casting-call/send")).toBe(true));
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/whatsapp/casting-call/send");
        expect(payload).toEqual({ talent_ids: ["t1", "t2"], project_ids: ["proj-a", "proj-b"], template_id: TEMPLATE_CASTING_CALL.id });
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
