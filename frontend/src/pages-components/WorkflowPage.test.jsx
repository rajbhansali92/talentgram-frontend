import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, { error: (...a) => toastError(...a), success: (...a) => toastSuccess(...a) }),
}));

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
    getAdmin: () => ({ id: "admin-1", role: "admin" }),
}));

// CallsTab has its own full test suite (CallsTab.test.jsx) — stub it here so
// this file only tests the tab-switch mechanics and the Scouting workspace,
// not CallsTab's own internals.
vi.mock("./CallsTab", () => ({
    default: () => <div data-testid="calls-tab-stub">Calls tab content</div>,
}));

// ScoutCaptureModal is unchanged by this task (still the same AI-capture
// upload/OCR flow) — stub it to a recognizable marker so we can assert it
// mounts/unmounts from the new Scouting tab without re-testing its own
// upload internals.
vi.mock("./ScoutCaptureModal", () => ({
    default: ({ onClose }) => (
        <div data-testid="scout-capture-modal-stub">
            AI Capture modal
            <button onClick={onClose}>close-modal</button>
        </div>
    ),
}));

import { adminApi } from "@/lib/api";
import WorkflowPage from "./WorkflowPage";

afterEach(cleanup);

const TASK_GENERAL = {
    id: "task-general", title: "General Task", description: "", category: "general",
    status: "pending", assignee_id: null, project_id: null, project_name: "",
    subtasks: [], comments: [], created_at: "2026-09-28T00:00:00Z",
};
const TASK_PROJECT = {
    id: "task-project", title: "Project Task", description: "", category: "project",
    status: "pending", assignee_id: null, project_id: null, project_name: "",
    subtasks: [], comments: [], created_at: "2026-09-28T00:00:00Z",
};
const TASK_FINANCE = {
    id: "task-finance", title: "Finance Task", description: "", category: "finance",
    status: "pending", assignee_id: null, project_id: null, project_name: "",
    subtasks: [], comments: [], created_at: "2026-09-28T00:00:00Z",
};

const SCOUT_1 = {
    id: "scout-1", name: "Jane Scout", instagram_link: "https://instagram.com/janescout",
    phone: "+911111100001", notes: "", status: "not_contacted", assigned_id: null,
};
const SCOUT_2 = {
    id: "scout-2", name: "John Scout", instagram_link: "https://instagram.com/johnscout",
    phone: "+911111100002", notes: "", status: "reached_out", assigned_id: null,
};

function mockAdminApi({ tasks = [], scouts = [] } = {}) {
    adminApi.get.mockImplementation((url) => {
        if (url === "/workflow/tasks") return Promise.resolve({ data: tasks });
        if (url === "/workflow/scouting") return Promise.resolve({ data: scouts });
        if (url === "/users") return Promise.resolve({ data: { items: [] } });
        if (url === "/projects") return Promise.resolve({ data: [] });
        if (url === "/workflow/notifications") return Promise.resolve({ data: [] });
        return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    adminApi.post.mockImplementation((url, body) => {
        if (url === "/workflow/scouting") {
            return Promise.resolve({ data: { id: "new-scout-id", ...body } });
        }
        return Promise.reject(new Error(`unexpected POST ${url}`));
    });
    adminApi.put.mockImplementation((url, body) => {
        const id = url.split("/").pop();
        const existing = scouts.find((s) => s.id === id) || {};
        return Promise.resolve({ data: { ...existing, ...body } });
    });
    adminApi.delete.mockImplementation(() => Promise.resolve({ data: {} }));
}

beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    adminApi.put.mockReset();
    adminApi.delete.mockReset();
    toastError.mockReset();
    toastSuccess.mockReset();
});

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------
describe("WorkflowPage — category pill navigation", () => {
    it("All shows every task regardless of category", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL, TASK_PROJECT, TASK_FINANCE] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());
        expect(screen.getByText("Project Task")).toBeTruthy();
        expect(screen.getByText("Finance Task")).toBeTruthy();
    });

    it("General filters to only category=general tasks", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL, TASK_PROJECT, TASK_FINANCE] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: "general" }));
        await waitFor(() => expect(screen.queryByText("Project Task")).toBeNull());
        expect(screen.getByText("General Task")).toBeTruthy();
        expect(screen.queryByText("Finance Task")).toBeNull();
    });

    it("Project filters to only category=project tasks", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL, TASK_PROJECT, TASK_FINANCE] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: "project" }));
        await waitFor(() => expect(screen.queryByText("General Task")).toBeNull());
        expect(screen.getByText("Project Task")).toBeTruthy();
        expect(screen.queryByText("Finance Task")).toBeNull();
    });

    it("Finance filters to only category=finance tasks", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL, TASK_PROJECT, TASK_FINANCE] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: "finance" }));
        await waitFor(() => expect(screen.queryByText("General Task")).toBeNull());
        expect(screen.getByText("Finance Task")).toBeTruthy();
        expect(screen.queryByText("Project Task")).toBeNull();
    });

    it("Scouting opens the Scouting workspace (not the task feed)", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL], scouts: [SCOUT_1] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getByText("Scouting Log")).toBeTruthy());
        expect(screen.getByText("Active Scouting Queue (1)")).toBeTruthy();
        expect(screen.getByText("Jane Scout")).toBeTruthy();
        // The task feed (and its status filter, which only applies to tasks)
        // must not be showing at the same time.
        expect(screen.queryByText("General Task")).toBeNull();
        expect(screen.queryByText("Active Tasks")).toBeNull();
    });

    it("Calls tab remains functional and unaffected by the Scouting consolidation", async () => {
        mockAdminApi({});
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledWith("/workflow/tasks"));
        fireEvent.click(screen.getByRole("button", { name: "Calls" }));
        await waitFor(() => expect(screen.getByTestId("calls-tab-stub")).toBeTruthy());
    });
});

// ---------------------------------------------------------------------------
// Scouting workspace
// ---------------------------------------------------------------------------
describe("WorkflowPage — Scouting workspace", () => {
    it("renders scouting entries with existing statuses", async () => {
        mockAdminApi({ scouts: [SCOUT_1, SCOUT_2] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getByText("Jane Scout")).toBeTruthy());
        expect(screen.getByText("John Scout")).toBeTruthy();
        // Status pills shown collapsed as a badge on the card header.
        expect(screen.getByText("New")).toBeTruthy(); // not_contacted -> "New"
        expect(screen.getByText("Contacted")).toBeTruthy(); // reached_out -> "Contacted"
    });

    it("active queue entries appear in the Scouting view with correct count", async () => {
        mockAdminApi({ scouts: [SCOUT_1, SCOUT_2] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getByText("Active Scouting Queue (2)")).toBeTruthy());
    });

    it("AI Capture remains accessible and opens the existing modal", async () => {
        mockAdminApi({});
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        fireEvent.click(await screen.findByRole("button", { name: /AI Capture/ }));
        await waitFor(() => expect(screen.getByTestId("scout-capture-modal-stub")).toBeTruthy());
    });

    it("manual logging remains accessible and creates a scout entry via the existing endpoint", async () => {
        mockAdminApi({});
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        fireEvent.click(await screen.findByText("or log manually"));

        fireEvent.change(screen.getByPlaceholderText("https://instagram.com/profile..."), {
            target: { value: "https://instagram.com/newscout" },
        });
        fireEvent.change(screen.getByPlaceholderText("+91..."), { target: { value: "+919876543210" } });
        fireEvent.click(screen.getByRole("button", { name: "Log Scout" }));

        await waitFor(() => {
            expect(adminApi.post).toHaveBeenCalledWith(
                "/workflow/scouting",
                expect.objectContaining({ instagram_link: "https://instagram.com/newscout", phone: "+919876543210" })
            );
        });
        // Exactly one create call — no duplicate write.
        expect(adminApi.post.mock.calls.filter(([url]) => url === "/workflow/scouting")).toHaveLength(1);
    });

    it("existing scouting status changes still work via PUT /workflow/scouting/{id}", async () => {
        mockAdminApi({ scouts: [SCOUT_1] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getByText("Jane Scout")).toBeTruthy());

        // Expand the card to reveal the status controls.
        fireEvent.click(screen.getByText("Jane Scout"));
        fireEvent.click(await screen.findByRole("button", { name: "Ignored" }));

        await waitFor(() => {
            expect(adminApi.put).toHaveBeenCalledWith("/workflow/scouting/scout-1", { status: "ignored" });
        });
    });

    it("does not duplicate scouting data — exactly one card per scout entry, one GET on mount", async () => {
        mockAdminApi({ scouts: [SCOUT_1] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getAllByText("Jane Scout")).toHaveLength(1));
        // Only one component fetches /workflow/scouting — no second, competing
        // right-hand-sidebar instance also polling it.
        const scoutingGetCalls = adminApi.get.mock.calls.filter(([url]) => url === "/workflow/scouting");
        expect(scoutingGetCalls).toHaveLength(1);
    });
});

// ---------------------------------------------------------------------------
// Regression
// ---------------------------------------------------------------------------
describe("WorkflowPage — regression", () => {
    it("switching from Scouting to General does not break the task feed", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL], scouts: [SCOUT_1] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getByText("Jane Scout")).toBeTruthy());

        fireEvent.click(screen.getByRole("button", { name: "general" }));
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());
        expect(screen.queryByText("Jane Scout")).toBeNull();
        expect(screen.queryByText("Scouting Log")).toBeNull();
    });

    it("returning to Scouting still shows the same (not re-duplicated) data", async () => {
        mockAdminApi({ tasks: [TASK_GENERAL], scouts: [SCOUT_1] });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        fireEvent.click(await screen.findByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getByText("Jane Scout")).toBeTruthy());

        fireEvent.click(screen.getByRole("button", { name: "all" }));
        await waitFor(() => expect(screen.getByText("General Task")).toBeTruthy());

        fireEvent.click(screen.getByRole("button", { name: "scouting" }));
        await waitFor(() => expect(screen.getAllByText("Jane Scout")).toHaveLength(1));
    });

    it("existing Workflow task functionality (create task) remains intact", async () => {
        mockAdminApi({ tasks: [] });
        adminApi.post.mockImplementation((url, body) => {
            if (url === "/workflow/tasks") return Promise.resolve({ data: { id: "new-task", ...body } });
            return Promise.reject(new Error(`unexpected POST ${url}`));
        });
        render(<MemoryRouter><WorkflowPage /></MemoryRouter>);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledWith("/workflow/tasks"));

        fireEvent.click(screen.getByRole("button", { name: /New Task/ }));
        fireEvent.change(await screen.findByPlaceholderText("Enter operational task title..."), {
            target: { value: "A brand new task" },
        });
        fireEvent.click(screen.getByRole("button", { name: /Create Task/ }));

        await waitFor(() => {
            expect(adminApi.post).toHaveBeenCalledWith(
                "/workflow/tasks",
                expect.objectContaining({ title: "A brand new task" })
            );
        });
    });
});
