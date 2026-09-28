import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, { error: (...a) => toastError(...a), success: (...a) => toastSuccess(...a) }),
}));

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import { adminApi } from "@/lib/api";
import CallsTab from "./CallsTab";

afterEach(cleanup);

const USERS = [
    { id: "u-admin", name: "Admin User", email: "admin@example.com" },
    { id: "u-team", name: "Team Member One", email: "team1@example.com" },
];

const ROW_UNASSIGNED = {
    talent_id: "t-unassigned", talent_name: "Unassigned Talent", talent_phone: "+911111",
    project_id: "p-1", project_name: "Project One", pipeline_stage: "ask_to_test",
    assigned_to_id: null, assigned_to_name: null,
    last_call_at: null, last_call_result: null, last_update_status: null, last_update_text: null,
    last_call_by_name: null, call_status_bucket: "never",
};
const ROW_ASSIGNED_TEAM = {
    talent_id: "t-team", talent_name: "Team Talent", talent_phone: "+912222",
    project_id: "p-1", project_name: "Project One", pipeline_stage: "approved",
    assigned_to_id: "u-team", assigned_to_name: "Team Member One",
    last_call_at: null, last_call_result: null, last_update_status: null, last_update_text: null,
    last_call_by_name: null, call_status_bucket: "never",
};
const ROW_ASSIGNED_ADMIN = {
    talent_id: "t-admin", talent_name: "Admin Talent", talent_phone: "+913333",
    project_id: "p-2", project_name: "Project Two", pipeline_stage: "shortlisted",
    assigned_to_id: "u-admin", assigned_to_name: "Admin User",
    last_call_at: null, last_call_result: null, last_update_status: null, last_update_text: null,
    last_call_by_name: null, call_status_bucket: "never",
};

const ALL_ROWS = [ROW_UNASSIGNED, ROW_ASSIGNED_TEAM, ROW_ASSIGNED_ADMIN];

function mockAdminApiStatic(rows = ALL_ROWS) {
    adminApi.get.mockImplementation((url, config) => {
        if (url === "/workflow/calls") {
            const params = (config && config.params) || {};
            let filtered = rows;
            if (params.project_ids) {
                const ids = params.project_ids.split(",");
                filtered = filtered.filter((r) => ids.includes(r.project_id));
            }
            if (params.assigned_to_id) {
                filtered = params.assigned_to_id === "unassigned"
                    ? filtered.filter((r) => !r.assigned_to_id)
                    : filtered.filter((r) => r.assigned_to_id === params.assigned_to_id);
            }
            return Promise.resolve({ data: { rows: filtered, pipeline_stages: [] } });
        }
        return Promise.reject(new Error(`unexpected GET ${url}`));
    });
}

beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    toastError.mockReset();
    toastSuccess.mockReset();
});

// Desktop table + mobile cards are BOTH always present in jsdom (CSS
// `hidden`/`md:hidden` classes have no effect outside a real browser), so
// every row's talent name renders twice. Assert presence/absence via
// count rather than a single-match getByText.
// The Talent filter's own <select> always lists every baseline talent
// name regardless of the active row filters (same as the Project/Pipeline
// baseline dropdowns) — exclude <option> text so this only counts actual
// rendered ROWS (desktop table + mobile cards, both present in jsdom).
const nameCount = (name) => screen.queryAllByText(name).filter((el) => el.tagName !== "OPTION").length;

describe("CallsTab — Assigned To filter", () => {
    it("shows all rows (assigned + unassigned) by default for an admin", async () => {
        mockAdminApiStatic();
        render(<CallsTab isAdmin currentUserId="u-admin" users={USERS} />);
        await waitFor(() => expect(nameCount("Unassigned Talent")).toBeGreaterThan(0));
        expect(nameCount("Team Talent")).toBeGreaterThan(0);
        expect(nameCount("Admin Talent")).toBeGreaterThan(0);
    });

    it("filters to a specific assignee via the Assigned To dropdown", async () => {
        mockAdminApiStatic();
        render(<CallsTab isAdmin currentUserId="u-admin" users={USERS} />);
        await waitFor(() => expect(nameCount("Unassigned Talent")).toBeGreaterThan(0));

        const select = screen.getByDisplayValue("All Team Members");
        fireEvent.change(select, { target: { value: "u-team" } });

        await waitFor(() => {
            const calls = adminApi.get.mock.calls.filter(([url]) => url === "/workflow/calls");
            const last = calls[calls.length - 1];
            expect(last[1].params.assigned_to_id).toBe("u-team");
        });
        await waitFor(() => expect(nameCount("Team Talent")).toBeGreaterThan(0));
        expect(nameCount("Admin Talent")).toBe(0);
        expect(nameCount("Unassigned Talent")).toBe(0);
    });

    it("filters to Unassigned via the Assigned To dropdown", async () => {
        mockAdminApiStatic();
        render(<CallsTab isAdmin currentUserId="u-admin" users={USERS} />);
        await waitFor(() => expect(nameCount("Unassigned Talent")).toBeGreaterThan(0));

        const select = screen.getByDisplayValue("All Team Members");
        fireEvent.change(select, { target: { value: "unassigned" } });

        await waitFor(() => {
            const calls = adminApi.get.mock.calls.filter(([url]) => url === "/workflow/calls");
            const last = calls[calls.length - 1];
            expect(last[1].params.assigned_to_id).toBe("unassigned");
        });
        await waitFor(() => expect(nameCount("Unassigned Talent")).toBeGreaterThan(0));
        expect(nameCount("Team Talent")).toBe(0);
        expect(nameCount("Admin Talent")).toBe(0);
    });

    it("is available to a non-admin team member too (visibility is broadened)", async () => {
        mockAdminApiStatic();
        render(<CallsTab isAdmin={false} currentUserId="u-team" users={[]} />);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalled());
        // A team member's default assignment filter is "mine" — switch to "All Calls" to see everything.
        fireEvent.click(screen.getAllByText("All Calls")[0]);
        await waitFor(() => expect(nameCount("Unassigned Talent")).toBeGreaterThan(0));
        expect(nameCount("Team Talent")).toBeGreaterThan(0);
        expect(nameCount("Admin Talent")).toBeGreaterThan(0);

        const select = screen.getByDisplayValue("All Team Members");
        fireEvent.change(select, { target: { value: "u-team" } });
        await waitFor(() => expect(nameCount("Team Talent")).toBeGreaterThan(0));
        expect(nameCount("Admin Talent")).toBe(0);
    });

    it("combines Assigned To with the Project filter", async () => {
        mockAdminApiStatic();
        render(<CallsTab isAdmin currentUserId="u-admin" users={USERS} />);
        await waitFor(() => expect(nameCount("Unassigned Talent")).toBeGreaterThan(0));

        // Select Project "Project Two" via the Project MultiSelectPopover.
        fireEvent.click(screen.getByRole("button", { name: /^Project\d*$/ }));
        fireEvent.click(await screen.findByRole("button", { name: "Project Two" }));

        await waitFor(() => expect(nameCount("Admin Talent")).toBeGreaterThan(0));
        expect(nameCount("Team Talent")).toBe(0);
        expect(nameCount("Unassigned Talent")).toBe(0);
    });
});

describe("CallsTab — Project filter race condition (regression)", () => {
    it("never lets a stale, slower response overwrite a newer, faster one", async () => {
        // Deterministic reproduction of the reported bug: selecting Project A
        // (slow to resolve) then Project B (fast to resolve) must leave the
        // table showing Project B's rows, not Project A's — even though
        // Project A's request was sent FIRST but resolves SECOND.
        let resolveSlow;
        const slow = new Promise((resolve) => { resolveSlow = resolve; });

        adminApi.get.mockImplementation((url, config) => {
            if (url !== "/workflow/calls") return Promise.reject(new Error(`unexpected GET ${url}`));
            const params = (config && config.params) || {};
            if (params.project_ids === "p-1") {
                return slow.then(() => ({ data: { rows: [ROW_UNASSIGNED, ROW_ASSIGNED_TEAM], pipeline_stages: [] } }));
            }
            if (params.project_ids === "p-2") {
                return Promise.resolve({ data: { rows: [ROW_ASSIGNED_ADMIN], pipeline_stages: [] } });
            }
            return Promise.resolve({ data: { rows: ALL_ROWS, pipeline_stages: [] } });
        });

        render(<CallsTab isAdmin currentUserId="u-admin" users={USERS} />);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalled());

        fireEvent.click(screen.getByRole("button", { name: /^Project\d*$/ })); // opens the popover
        fireEvent.click(await screen.findByRole("button", { name: "Project One" })); // slow request in flight now

        // Immediately switch to Project Two (popover stays open across toggles) — fast request, resolves before the slow one.
        fireEvent.click(await screen.findByRole("button", { name: "Project One" })); // deselect A
        fireEvent.click(await screen.findByRole("button", { name: "Project Two" })); // select B

        await waitFor(() => expect(nameCount("Admin Talent")).toBeGreaterThan(0));

        // Now let the stale Project-A request finally resolve.
        resolveSlow();
        await new Promise((r) => setTimeout(r, 10));

        // The stale response must NOT have overwritten the table.
        expect(nameCount("Admin Talent")).toBeGreaterThan(0);
        expect(nameCount("Unassigned Talent")).toBe(0);
        expect(nameCount("Team Talent")).toBe(0);
    });
});

describe("CallsTab — Record button availability for team members", () => {
    it("shows Record for a team member on a call assigned to someone else", async () => {
        mockAdminApiStatic();
        render(<CallsTab isAdmin={false} currentUserId="u-team" users={[]} />);
        fireEvent.click((await screen.findAllByText("All Calls"))[0]);
        await waitFor(() => expect(nameCount("Admin Talent")).toBeGreaterThan(0));

        // The desktop table row is the one inside a <tr>; assert Record exists there.
        const nameEls = screen.getAllByText("Admin Talent");
        const tableNameEl = nameEls.find((el) => el.closest("tr"));
        const row = tableNameEl.closest("tr");
        expect(within(row).getByText("Record")).toBeTruthy();
    });
});
