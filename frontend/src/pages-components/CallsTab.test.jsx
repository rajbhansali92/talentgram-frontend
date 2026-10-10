import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign(() => {}, { error: (...a) => toastError(...a), success: (...a) => toastSuccess(...a) }),
}));
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import { adminApi } from "@/lib/api";
import CallsTab from "./CallsTab";

afterEach(cleanup);

const USERS = [
    { id: "u-admin", name: "Admin User", email: "admin@example.com" },
    { id: "u-team", name: "Team Member One", email: "team1@example.com" },
];
const NOW = Date.now();
const iso = (hoursAgo) => new Date(NOW - hoursAgo * 3600 * 1000).toISOString();

const base = {
    talent_phone: "+911111", assigned_to_id: null, assigned_to_name: null, assigned_by_name: null, assigned_at: null, priority: "normal",
    call_state: "pending", is_new: false, call_count: 0, last_call_at: null, last_call_result: null, last_update_status: null, last_update_text: null,
    last_call_by_name: null, last_call_synced: false, last_call_source_project: null, last_activity_at: null, call_status_bucket: "never",
    other_projects_count: 0, raw_stage: "follow_up", is_follow_up: true,
};
const R = (over) => ({ ...base, ...over });

const ROW_FOLLOW = R({ talent_id: "t-f", talent_name: "Follow Talent", project_id: "p-1", project_name: "Tyaani Jewellery", pipeline_stage: "follow_up", other_projects_count: 2 });
const ROW_URGENT = R({ talent_id: "t-u", talent_name: "Urgent Talent", project_id: "p-1", project_name: "Tyaani Jewellery", pipeline_stage: "follow_up", priority: "urgent", assigned_to_id: "u-team", assigned_to_name: "Team Member One", assigned_at: iso(2), is_new: true });
const ROW_APPROVED = R({ talent_id: "t-a", talent_name: "Approved Talent", project_id: "p-1", project_name: "Tyaani Jewellery", pipeline_stage: "approved", raw_stage: "approved", is_follow_up: false, call_state: "completed", last_call_at: iso(5), last_call_result: "answered", last_update_status: "sending", call_count: 1 });
const ROW_OTHER = R({ talent_id: "t-o", talent_name: "Other Project Talent", project_id: "p-2", project_name: "Second Project", pipeline_stage: "already_tested", raw_stage: "already_tested", is_follow_up: false, assigned_to_id: "u-admin", assigned_to_name: "Admin User", assigned_at: iso(80) });
const ALL = [ROW_FOLLOW, ROW_URGENT, ROW_APPROVED, ROW_OTHER];

const counts = (rows) => ({
    total: rows.length, pending: rows.filter((r) => r.call_state === "pending").length, attempted: 0, completed: rows.filter((r) => r.call_state === "completed").length,
    assigned: rows.filter((r) => r.assigned_to_id).length, unassigned: rows.filter((r) => !r.assigned_to_id).length,
    urgent: rows.filter((r) => r.priority === "urgent" && r.call_state !== "completed" && r.assigned_to_id).length, new: rows.filter((r) => r.is_new).length,
});

// The server does the grouping/filtering/paging; the mock mirrors its response shape.
function serverResponse(rows, params = {}) {
    let f = rows;
    if (params.project_ids) f = f.filter((r) => params.project_ids.split(",").includes(r.project_id));
    if (params.pipeline) f = f.filter((r) => params.pipeline.split(",").includes(r.pipeline_stage));
    if (params.call_state) f = f.filter((r) => r.call_state === params.call_state);
    if (params.priority) f = f.filter((r) => r.priority === params.priority);
    if (params.assignment === "mine") f = f.filter((r) => r.assigned_to_id === "u-team");
    if (params.assignment === "unassigned") f = f.filter((r) => !r.assigned_to_id);
    if (params.assigned_to_id) f = f.filter((r) => (params.assigned_to_id === "unassigned" ? !r.assigned_to_id : r.assigned_to_id === params.assigned_to_id));
    if (params.search) f = f.filter((r) => `${r.talent_name} ${r.project_name}`.toLowerCase().includes(params.search.toLowerCase()));
    const facets = {
        projects: [...new Map(rows.map((r) => [r.project_id, { id: r.project_id, label: r.project_name }])).values()],
        talents: rows.map((r) => ({ id: r.talent_id, label: r.talent_name })),
        assignees: [...new Map(rows.filter((r) => r.assigned_to_id).map((r) => [r.assigned_to_id, { id: r.assigned_to_id, label: r.assigned_to_name }])).values()],
    };
    const out = { summary: counts(f), facets, next_up: f.filter((r) => r.call_state !== "completed").slice(0, 5), pipeline_stages: ["follow_up", "ask_to_test", "already_tested", "approved"] };
    if (params.view === "list") return { ...out, rows: f.slice(params.page * params.size, (params.page + 1) * params.size), total: f.length, has_more: (params.page + 1) * params.size < f.length };
    const byProject = new Map();
    f.forEach((r) => {
        if (!byProject.has(r.project_id)) byProject.set(r.project_id, { project_id: r.project_id, project_name: r.project_name, rows: [] });
        byProject.get(r.project_id).rows.push(r);
    });
    const groups = [...byProject.values()].map((g) => {
        const lanes = [...new Set(g.rows.map((r) => r.pipeline_stage))].map((st) => ({ stage: st, rows: g.rows.filter((r) => r.pipeline_stage === st) }));
        return { project_id: g.project_id, project_name: g.project_name, counts: counts(g.rows), next_call: null, pipelines: lanes.map((l) => ({ stage: l.stage, counts: counts(l.rows), rows: l.rows })) };
    });
    return { ...out, projects: groups.slice(params.page * params.size, (params.page + 1) * params.size), total_projects: groups.length, has_more: (params.page + 1) * params.size < groups.length };
}

function mockApi(rows = ALL, extra = {}) {
    adminApi.get.mockImplementation((url, cfg) => {
        if (url === "/workflow/calls") return Promise.resolve({ data: extra.list ? extra.list(cfg.params) : serverResponse(rows, cfg.params || {}) });
        const ctx = url.match(/^\/workflow\/calls\/([^/]+)\/([^/]+)\/context$/);
        if (ctx) return Promise.resolve({ data: extra.context ? extra.context(ctx[1], ctx[2]) : { talent_id: ctx[1], current: rows.find((r) => r.talent_id === ctx[1] && r.project_id === ctx[2]), other_projects: [] } });
        if (/\/history$/.test(url)) return Promise.resolve({ data: { history: extra.history || [] } });
        return Promise.reject(new Error(`unexpected GET ${url}`));
    });
}
const inRows = () => within(document.querySelector('[data-testid="calls-grouped"], [data-testid="calls-list"]') || document.body);
const lastListParams = () => { const c = adminApi.get.mock.calls.filter(([u]) => u === "/workflow/calls"); return c[c.length - 1][1].params; };

const mem = new Map();
Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k), clear: () => mem.clear() },
});

beforeEach(() => {
    adminApi.get.mockReset(); adminApi.post.mockReset(); adminApi.patch.mockReset();
    toastError.mockReset(); toastSuccess.mockReset();
    mem.clear();
});

const renderTab = (props = {}) => render(<CallsTab isAdmin currentUserId="u-admin" users={USERS} {...props} />);

describe("Calls — project → pipeline grouping", () => {
    it("groups calls by project first, then pipeline, with counts at both levels", async () => {
        mockApi();
        renderTab();
        await waitFor(() => expect(screen.getByTestId("call-project-p-1")).toBeTruthy());
        const p1 = screen.getByTestId("call-project-p-1");
        expect(within(p1).getByTestId("call-project-name").textContent).toBe("Tyaani Jewellery");
        expect(p1.textContent).toMatch(/2 to do/);                       // pending + attempted in the project header
        expect(p1.textContent).toMatch(/1 urgent/); expect(p1.textContent).toMatch(/1 new/); expect(p1.textContent).toMatch(/1 done/);
        // pipeline lanes inside the project, each with its own count
        expect(within(p1).getByTestId("call-lane-p-1-follow_up").textContent).toMatch(/2 · 2 to do/);
        expect(within(p1).getByTestId("call-lane-p-1-approved")).toBeTruthy();
        expect(screen.getByTestId("call-project-p-2")).toBeTruthy();
    });

    it("shows the Follow-up pipeline (derived + stored) as its own labelled lane", async () => {
        mockApi();
        renderTab();
        const lane = await screen.findByTestId("call-lane-p-1-follow_up");
        expect(lane.textContent).toMatch(/FOLLOW-UP/);
        expect(within(lane).getByText("Follow Talent")).toBeTruthy(); expect(within(lane).getByText("Urgent Talent")).toBeTruthy();
    });

    it("projects and pipeline lanes collapse and expand, keeping the counts visible", async () => {
        mockApi();
        renderTab();
        await screen.findByTestId("call-lane-p-1-follow_up");
        fireEvent.click(screen.getByTestId("call-project-toggle-p-1"));
        expect(screen.queryByTestId("call-lane-p-1-follow_up")).toBeNull();
        expect(screen.getByTestId("call-project-p-1").textContent).toMatch(/2 to do/);          // still informative when closed
        fireEvent.click(screen.getByTestId("call-project-toggle-p-1"));
        const lane = await screen.findByTestId("call-lane-p-1-approved");
        expect(within(lane).getByText("Approved Talent")).toBeTruthy();
        fireEvent.click(within(lane).getAllByRole("button", { expanded: true })[0]);
        expect(within(screen.getByTestId("call-lane-p-1-approved")).queryByText("Approved Talent")).toBeNull();
    });

    it("the default view is grouped, the flat list is an optional secondary view and the choice is remembered", async () => {
        mockApi();
        renderTab();
        await screen.findByTestId("calls-grouped");
        expect(lastListParams().view).toBe("grouped");
        fireEvent.click(screen.getByTestId("view-list"));
        await screen.findByTestId("calls-list");
        expect(lastListParams().view).toBe("list");
        expect(screen.getAllByTestId("call-row-project").length).toBe(4);                        // the flat list names the project on each row
        cleanup();
        mockApi();
        renderTab();
        await screen.findByTestId("calls-list");                                                  // remembered
    });
});

describe("Calls — assignment, priority, queue", () => {
    it("shows priority, status, assignee and when it was assigned on each call", async () => {
        mockApi();
        renderTab();
        const row = await screen.findByTestId("call-row-t-u::p-1");
        expect(row.textContent).toMatch(/Urgent/); expect(row.textContent).toMatch(/Pending/); expect(row.textContent).toMatch(/Team Member One/);
        expect(within(row).getByTestId("call-assignment").textContent).toMatch(/assigned 2h ago/);
        expect(within(row).getByTestId("call-new")).toBeTruthy();
        const done = screen.getByTestId("call-row-t-a::p-1");
        expect(done.textContent).toMatch(/Completed/); expect(done.textContent).toMatch(/Answered · Sending/);
    });

    it("the summary strip and Next up come from the server for the whole filtered set", async () => {
        mockApi();
        renderTab();
        const summary = await screen.findByTestId("calls-summary");
        expect(within(summary).getByTestId("calls-summary-to-do").textContent).toMatch(/^3/);
        expect(within(summary).getByTestId("calls-summary-urgent").textContent).toMatch(/^1/);
        const next = screen.getAllByTestId("next-up-item");
        expect(next[0].textContent).toMatch(/Follow Talent/);                                    // the server's queue order, not re-sorted here
    });

    it("an admin can change any call's priority; it is saved to the server and reloaded", async () => {
        mockApi();
        adminApi.patch.mockResolvedValue({ data: { updated: 1, skipped: [] } });
        renderTab();
        const select = await screen.findByTestId("call-priority-select-t-f::p-1");
        fireEvent.change(select, { target: { value: "semi_urgent" } });
        await waitFor(() => expect(adminApi.patch).toHaveBeenCalledWith("/workflow/calls/priority", { pairs: [{ talent_id: "t-f", project_id: "p-1" }], priority: "semi_urgent" }));
        await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
    });

    it("a team member can change priority only on calls assigned to them", async () => {
        mockApi();
        renderTab({ isAdmin: false, currentUserId: "u-team" });
        expect(await screen.findByTestId("call-priority-select-t-u::p-1")).toBeTruthy();         // assigned to me
        expect(screen.queryByTestId("call-priority-select-t-f::p-1")).toBeNull();                 // unassigned
        expect(screen.queryByTestId("call-priority-select-t-o::p-2")).toBeNull();                 // someone else's
        expect(within(screen.getByTestId("call-row-t-o::p-2")).getByTestId("call-priority").textContent).toBe("Normal");
    });

    it("team members get no assignment controls, but can still record any call", async () => {
        mockApi();
        renderTab({ isAdmin: false, currentUserId: "u-team" });
        await screen.findByTestId("call-row-t-o::p-2");
        expect(screen.queryByLabelText(/Select /)).toBeNull();
        expect(within(screen.getByTestId("call-row-t-o::p-2")).getByTestId("call-record")).toBeTruthy();
    });

    it("bulk assignment sends the assignee and the chosen priority (admin only)", async () => {
        mockApi();
        adminApi.post.mockResolvedValue({ data: { updated: 1 } });
        renderTab();
        fireEvent.click(await screen.findByLabelText("Select Follow Talent"));
        const bar = await screen.findByTestId("calls-bulk");
        fireEvent.change(within(bar).getByLabelText("Assign to"), { target: { value: "u-team" } });
        fireEvent.change(within(bar).getByLabelText("Priority for assignment"), { target: { value: "urgent" } });
        fireEvent.click(within(bar).getByText("Apply"));
        await waitFor(() => expect(adminApi.post).toHaveBeenCalledWith("/workflow/calls/assign", { pairs: [{ talent_id: "t-f", project_id: "p-1" }], assigned_to_id: "u-team", priority: "urgent" }));
        // the choice does not leak into the next batch
        fireEvent.click(await screen.findByLabelText("Select Follow Talent"));
        const bar2 = await screen.findByTestId("calls-bulk");
        expect(within(bar2).getByLabelText("Assign to").value).toBe("");
        expect(within(bar2).getByLabelText("Priority for assignment").value).toBe("");
    });
});

describe("Calls — other ongoing projects for the same talent", () => {
    const OTHERS = [
        { project_id: "p-9", project_name: "Nine Project", pipeline_stage: "approved", call_state: "attempted", priority: "urgent", assigned_to_id: "u-team", assigned_to_name: "Team Member One", assigned_at: iso(3), last_call_at: iso(1), last_call_result: "no_answer", last_call_by_name: "Admin User", last_call_synced: false },
        { project_id: "p-8", project_name: "Eight Project", pipeline_stage: "follow_up", call_state: "completed", priority: "normal", assigned_to_id: null, last_call_at: iso(4), last_call_result: "answered", last_update_status: "sending", last_call_synced: true, last_call_source_project: "Tyaani Jewellery" },
    ];

    it("flags a talent who is in other ongoing projects, and opens their details", async () => {
        mockApi(ALL, { context: (t, p) => ({ talent_id: t, current: ALL.find((r) => r.talent_id === t && r.project_id === p), other_projects: OTHERS }) });
        renderTab();
        const row = await screen.findByTestId("call-row-t-f::p-1");
        expect(within(row).getByTestId("call-other-projects").textContent).toMatch(/\+2 other projects/);
        fireEvent.click(within(row).getByTestId("call-other-projects"));
        const panel = await screen.findByTestId("detail-other-projects");
        await waitFor(() => expect(within(panel).getByTestId("other-project-p-9")).toBeTruthy());
        const nine = within(panel).getByTestId("other-project-p-9").textContent;
        expect(nine).toMatch(/Nine Project/); expect(nine).toMatch(/APPROVED/); expect(nine).toMatch(/Attempted/); expect(nine).toMatch(/Urgent/);
        expect(nine).toMatch(/Team Member One/); expect(nine).toMatch(/No Answer/);
        const eight = within(panel).getByTestId("other-project-p-8").textContent;
        expect(eight).toMatch(/Unassigned/); expect(eight).toMatch(/Completed/); expect(eight).toMatch(/synced from Tyaani Jewellery/);
        expect(screen.getByTestId("detail-this-project").textContent).toMatch(/FOLLOW-UP/);
    });

    it("says so when the talent is in no other ongoing project", async () => {
        mockApi();
        renderTab();
        fireEvent.click(within(await screen.findByTestId("call-row-t-o::p-2")).getByTestId("call-open"));
        expect((await screen.findByTestId("detail-other-projects")).textContent).toMatch(/Not in any other ongoing project/);
    });
});

describe("Calls — recording an outcome", () => {
    it("names the other projects the call will also be recorded for, and sends the sync choice", async () => {
        mockApi(ALL, { context: (t, p) => ({ talent_id: t, current: ALL.find((r) => r.talent_id === t), other_projects: [{ project_id: "p-9", project_name: "Nine Project" }, { project_id: "p-8", project_name: "Eight Project" }] }) });
        adminApi.post.mockResolvedValue({ data: { id: "c1", synced_projects: [{ project_id: "p-9", project_name: "Nine Project" }, { project_id: "p-8", project_name: "Eight Project" }] } });
        renderTab();
        fireEvent.click(within(await screen.findByTestId("call-row-t-f::p-1")).getByTestId("call-record"));
        const note = await screen.findByTestId("record-sync");
        expect(note.textContent).toMatch(/2 other ongoing projects/); expect(note.textContent).toMatch(/Nine Project, Eight Project/);
        expect(note.textContent).toMatch(/Pipeline stages and project decisions are not changed/);
        fireEvent.click(screen.getByText("No Answer"));
        fireEvent.click(screen.getByTestId("record-save"));
        await waitFor(() => expect(adminApi.post).toHaveBeenCalledTimes(1));
        expect(adminApi.post.mock.calls[0][1]).toMatchObject({ talent_id: "t-f", project_id: "p-1", call_result: "no_answer", sync_other_projects: true });
        await waitFor(() => expect(toastSuccess.mock.calls[0][0]).toMatch(/also recorded for Nine Project, Eight Project/));
    });

    it("lets the user record for this project only", async () => {
        mockApi(ALL, { context: (t) => ({ talent_id: t, other_projects: [{ project_id: "p-9", project_name: "Nine Project" }] }) });
        adminApi.post.mockResolvedValue({ data: { id: "c1", synced_projects: [] } });
        renderTab();
        fireEvent.click(within(await screen.findByTestId("call-row-t-f::p-1")).getByTestId("call-record"));
        fireEvent.click(within(await screen.findByTestId("record-sync")).getByRole("checkbox"));
        fireEvent.click(screen.getByText("Answered"));
        fireEvent.click(screen.getByTestId("record-save"));
        await waitFor(() => expect(adminApi.post.mock.calls[0][1].sync_other_projects).toBe(false));
    });

    it("a double tap on Save sends one request with one id; a retry reuses the same id", async () => {
        mockApi();
        adminApi.post.mockRejectedValueOnce({ response: { data: { detail: "boom" } } }).mockResolvedValue({ data: { id: "c1", synced_projects: [] } });
        renderTab();
        fireEvent.click(within(await screen.findByTestId("call-row-t-o::p-2")).getByTestId("call-record"));
        fireEvent.click(screen.getByText("Busy"));
        const save = screen.getByTestId("record-save");
        fireEvent.click(save); fireEvent.click(save); fireEvent.click(save);      // same-tick taps
        await waitFor(() => expect(toastError).toHaveBeenCalled());
        expect(adminApi.post).toHaveBeenCalledTimes(1);                                          // locked while saving
        fireEvent.click(screen.getByTestId("record-save"));
        await waitFor(() => expect(adminApi.post).toHaveBeenCalledTimes(2));
        expect(adminApi.post.mock.calls[1][1].id).toBe(adminApi.post.mock.calls[0][1].id);       // the idempotency key survives a retry
    });

    it("history labels entries that were synced from another project", async () => {
        mockApi(ALL, { history: [
            { id: "h1", call_result: "answered", called_at: iso(3), called_by_name: "Admin User", update_status: "sending", synced_from_call_id: "src", synced_from_project_name: "Tyaani Jewellery" },
            { id: "h2", call_result: "no_answer", called_at: iso(30), called_by_name: "Team Member One" },
        ] });
        renderTab();
        fireEvent.click(within(await screen.findByTestId("call-row-t-a::p-1")).getByLabelText("Call history"));
        const entries = await screen.findAllByTestId("history-entry");
        expect(entries).toHaveLength(2);
        expect(within(entries[0]).getByTestId("history-synced").textContent).toBe("Synced from Tyaani Jewellery");
        expect(within(entries[1]).queryByTestId("history-synced")).toBeNull();
    });
});

describe("Calls — filters, search, pagination and states", () => {
    it("sends every filter to the server and combines them", async () => {
        mockApi();
        renderTab();
        await screen.findByTestId("calls-grouped");
        fireEvent.change(screen.getByLabelText("Filter by priority"), { target: { value: "urgent" } });
        fireEvent.change(screen.getByLabelText("Call status"), { target: { value: "pending" } });
        fireEvent.change(screen.getByLabelText("Assigned to"), { target: { value: "u-team" } });
        fireEvent.change(screen.getByTestId("calls-search"), { target: { value: "urgent" } });
        await waitFor(() => expect(lastListParams()).toMatchObject({ view: "grouped", priority: "urgent", call_state: "pending", assigned_to_id: "u-team", search: "urgent" }));
        await waitFor(() => expect(inRows().queryByText("Follow Talent")).toBeNull());
        expect(inRows().getByText("Urgent Talent")).toBeTruthy();
        fireEvent.click(screen.getByTestId("calls-clear"));
        await waitFor(() => expect(lastListParams().priority).toBeUndefined());
        await waitFor(() => expect(inRows().getByText("Follow Talent")).toBeTruthy());
    });

    it("fetches the dropdown option lists once, not on every filter change", async () => {
        mockApi();
        renderTab();
        await screen.findByTestId("calls-grouped");
        const first = adminApi.get.mock.calls.filter(([u]) => u === "/workflow/calls")[0][1].params;
        expect(first.include_facets).toBeUndefined();
        fireEvent.change(screen.getByLabelText("Filter by priority"), { target: { value: "urgent" } });
        await waitFor(() => expect(lastListParams().priority).toBe("urgent"));
        expect(lastListParams().include_facets).toBe(false);
        expect(screen.getByLabelText("Talent").querySelectorAll("option").length).toBeGreaterThan(1);     // the options are still there
    });

    it("the scope tabs map to server filters, and Completed shows only recorded calls", async () => {
        mockApi();
        renderTab();
        await screen.findByTestId("calls-grouped");
        fireEvent.click(screen.getByTestId("scope-completed"));
        await waitFor(() => expect(lastListParams().call_state).toBe("completed"));
        await waitFor(() => expect(inRows().queryByText("Follow Talent")).toBeNull());
        expect(inRows().getByText("Approved Talent")).toBeTruthy();
        fireEvent.click(screen.getByTestId("scope-mine"));
        await waitFor(() => expect(lastListParams().assignment).toBe("mine"));
    });

    it("the Pipeline filter offers Follow-up first", async () => {
        mockApi();
        renderTab();
        await screen.findByTestId("calls-grouped");
        fireEvent.click(screen.getByText("Pipeline"));
        const firstOption = screen.getAllByRole("button").find((b) => /FOLLOW-UP/.test(b.textContent) && b.className.includes("w-full"));
        expect(firstOption).toBeTruthy();
    });

    it("pages by project with Load more, appending (not replacing) the groups", async () => {
        const many = Array.from({ length: 8 }, (_, i) => R({ talent_id: `t${i}`, talent_name: `Talent ${i}`, project_id: `px${i}`, project_name: `Project ${i}`, pipeline_stage: "follow_up" }));
        mockApi(many);
        renderTab();
        await screen.findByTestId("calls-grouped");
        expect(screen.getAllByTestId(/^call-project-px/).filter((e) => e.tagName === "SECTION")).toHaveLength(6);
        expect(lastListParams()).toMatchObject({ page: 0, size: 6 });
        fireEvent.click(screen.getByTestId("calls-load-more"));
        await waitFor(() => expect(screen.getAllByTestId(/^call-project-px/).filter((e) => e.tagName === "SECTION")).toHaveLength(8));
        expect(lastListParams()).toMatchObject({ page: 1, size: 6 });
        expect(screen.queryByTestId("calls-load-more")).toBeNull();
    });

    it("shows loading, empty and error states", async () => {
        mockApi([]);
        renderTab();
        expect(screen.getByTestId("calls-loading")).toBeTruthy();
        expect((await screen.findByTestId("calls-empty")).textContent).toMatch(/No calls to show/);
        cleanup();
        adminApi.get.mockRejectedValue(new Error("down"));
        renderTab();
        expect((await screen.findByTestId("calls-error")).textContent).toMatch(/Could not load calls/);
        expect(toastError).toHaveBeenCalled();
    });

    it("never lets a stale, slower response overwrite a newer, faster one", async () => {
        let releaseSlow;
        adminApi.get.mockImplementation((url, cfg) => {
            if (url !== "/workflow/calls") return Promise.reject(new Error(url));
            const params = cfg.params || {};
            if (params.priority === "urgent") return new Promise((res) => { releaseSlow = () => res({ data: serverResponse(ALL, params) }); });
            if (params.priority === "normal") return Promise.resolve({ data: serverResponse([ROW_OTHER], { ...params, priority: undefined }) });
            return Promise.resolve({ data: serverResponse(ALL, params) });
        });
        renderTab();
        await screen.findByTestId("calls-grouped");
        fireEvent.change(screen.getByLabelText("Filter by priority"), { target: { value: "urgent" } });     // slow
        fireEvent.change(screen.getByLabelText("Filter by priority"), { target: { value: "normal" } });     // fast, newer
        await waitFor(() => expect(inRows().getByText("Other Project Talent")).toBeTruthy());
        releaseSlow();
        await new Promise((r) => setTimeout(r, 30));
        expect(inRows().queryByText("Follow Talent")).toBeNull();                                    // the stale response was discarded
    });
});
