import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), success: vi.fn(), info: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn() },
    isAdmin: vi.fn(),
    getRole: vi.fn(),
}));

import { adminApi, isAdmin, getRole } from "@/lib/api";
import { toast } from "sonner";
import ProjectList from "./ProjectList";

afterEach(cleanup);

// server-side truth, mutated by the fake bulk-status endpoint
let DB;
const fresh = () => [
    { id: "o1", brand_name: "Ongoing One", status: "ongoing", materials: [] },
    { id: "o2", brand_name: "Ongoing Two", status: "ongoing", materials: [] },
    { id: "h1", brand_name: "Hold One", status: "hold", materials: [] },
    { id: "c1", brand_name: "Complete One", status: "complete", materials: [] },
    { id: "l1", brand_name: "Locked One", status: "locked", materials: [] },
];

function serverOk() {
    adminApi.get.mockImplementation(() => Promise.resolve({ data: DB.map((p) => ({ ...p })) }));
    adminApi.post.mockImplementation((url, body) => {
        if (url !== "/projects/bulk-status") return Promise.reject(new Error("unexpected " + url));
        const updated = [], unchanged = [];
        for (const id of body.ids) {
            const p = DB.find((x) => x.id === id);
            if (p.status === body.status) unchanged.push(id);
            else { p.status = body.status; updated.push(id); }
        }
        return Promise.resolve({ data: { status: body.status, updated, unchanged, failed: [] } });
    });
}

beforeEach(() => {
    adminApi.get.mockReset(); adminApi.post.mockReset();
    Object.values(toast).forEach((f) => f.mockClear?.());
    isAdmin.mockReturnValue(true); getRole.mockReturnValue("admin");
    DB = fresh();
    serverOk();
});

const renderList = () => render(
    <MemoryRouter initialEntries={["/admin/projects"]}>
        <Routes>
            <Route path="/admin/projects" element={<ProjectList />} />
            <Route path="/admin/projects/:id" element={<div data-testid="detail-page" />} />
        </Routes>
    </MemoryRouter>,
);
async function open() {
    renderList();
    await screen.findByTestId("project-card-o1");
    // hold / complete / locked sections start collapsed — expand them so every card is on screen
    for (const k of ["hold", "complete", "locked"]) fireEvent.click(screen.getByLabelText(new RegExp(`Expand ${k} projects`, "i")));
    await screen.findByTestId("project-card-l1");
}
const check = (id) => screen.getByTestId(`project-check-${id}`);
const bar = () => screen.queryByTestId("projects-bulk-bar");
const inGroup = (key) => within(screen.getByTestId(`project-group-${key}`));
const choose = (v) => fireEvent.change(screen.getByTestId("projects-bulk-status-select"), { target: { value: v } });
const apply = () => fireEvent.click(screen.getByTestId("projects-bulk-status-apply"));

describe("Projects page — selecting", () => {
    it("selects and deselects individual projects; the toolbar shows the count and appears only with a selection", async () => {
        await open();
        expect(bar()).toBeNull();
        fireEvent.click(check("o1"));
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("1 project selected");
        expect(check("o1").getAttribute("aria-label")).toBe("Deselect");
        fireEvent.click(check("o1"));
        expect(bar()).toBeNull();
        expect(check("o1").getAttribute("aria-label")).toBe("Select");
    });

    it("selects several projects across sections and Clear selection empties it", async () => {
        await open();
        ["o1", "h1", "c1"].forEach((id) => fireEvent.click(check(id)));
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("3 projects selected");
        fireEvent.click(screen.getByTestId("projects-bulk-bar-clear"));
        expect(bar()).toBeNull();
        ["o1", "h1", "c1"].forEach((id) => expect(check(id).getAttribute("aria-label")).toBe("Select"));
    });

    it("a section's Select all / Deselect all toggles just that section", async () => {
        await open();
        fireEvent.click(screen.getByTestId("project-group-select-ongoing"));
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("2 projects selected");
        expect(check("h1").getAttribute("aria-label")).toBe("Select");
        expect(screen.getByTestId("project-group-select-ongoing").textContent).toBe("Deselect all");
        fireEvent.click(screen.getByTestId("project-group-select-ongoing"));
        expect(bar()).toBeNull();
    });
});

describe("Projects page — status toolbar", () => {
    it("offers exactly Ongoing, Complete, Hold, Locked mapped to the stored values; Apply needs a choice", async () => {
        await open();
        fireEvent.click(check("o1"));
        const opts = [...screen.getByTestId("projects-bulk-status-select").querySelectorAll("option")].filter((o) => !o.disabled);
        expect(opts.map((o) => [o.value, o.textContent])).toEqual([["ongoing", "Ongoing"], ["complete", "Complete"], ["hold", "Hold"], ["locked", "Locked"]]);
        expect(screen.getByTestId("projects-bulk-status-apply").disabled).toBe(true);        // nothing chosen yet
        choose("hold");
        expect(screen.getByTestId("projects-bulk-status-apply").disabled).toBe(false);
        expect(screen.getByTestId("projects-bulk-status-apply").textContent).toContain("Apply to 1");
        expect(adminApi.post).not.toHaveBeenCalled();
    });

    it.each([["ongoing", "Ongoing"], ["complete", "Complete"], ["hold", "Hold"], ["locked", "Locked"]])(
        "applying %s sends the stored value for every selected project",
        async (value, label) => {
            await open();
            ["o1", "h1"].forEach((id) => fireEvent.click(check(id)));                      // an ongoing + a hold project
            choose(value); apply();
            await waitFor(() => expect(adminApi.post).toHaveBeenCalledWith("/projects/bulk-status", { ids: ["o1", "h1"], status: value }));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith(expect.stringContaining(label)));
            await waitFor(() => expect(bar()).toBeNull());                                  // selection cleared after success
            expect(DB.filter((p) => ["o1", "h1"].includes(p.id)).every((p) => p.status === value)).toBe(true);
            await waitFor(() => expect(inGroup(value).getByTestId("project-card-o1")).toBeTruthy());
            expect(inGroup(value).getByTestId("project-card-h1")).toBeTruthy();
        },
    );

    it("a mixed selection from different sections changes all of them, and the cards move to their new section", async () => {
        await open();
        ["o1", "h1", "l1"].forEach((id) => fireEvent.click(check(id)));                      // ongoing + hold + locked
        choose("complete"); apply();
        await waitFor(() => expect(adminApi.post).toHaveBeenCalledWith("/projects/bulk-status", { ids: ["o1", "h1", "l1"], status: "complete" }));
        await waitFor(() => expect(inGroup("complete").getByTestId("project-card-o1")).toBeTruthy());
        expect(inGroup("complete").getByTestId("project-card-h1")).toBeTruthy();
        expect(inGroup("complete").getByTestId("project-card-l1")).toBeTruthy();
        expect(inGroup("ongoing").queryByTestId("project-card-o1")).toBeNull();
        expect(inGroup("ongoing").getByTestId("project-card-o2")).toBeTruthy();             // unselected one stays put
        expect(inGroup("hold").queryByTestId("project-card-h1")).toBeNull();
        expect(screen.getByTestId("project-group-complete").textContent).toContain("4");    // section count follows
        // and the page re-read the saved state from the server
        await waitFor(() => expect(adminApi.get.mock.calls.length).toBeGreaterThanOrEqual(2));
    });

    it("selecting projects already in that status reports 'already' and changes nothing", async () => {
        await open();
        fireEvent.click(check("o1"));
        choose("ongoing"); apply();
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith(expect.stringContaining("Already Ongoing")));
    });
});

describe("Projects page — failures and double submission", () => {
    it("an API failure is reported, nothing moves, the selection is kept and Apply is usable again", async () => {
        await open();
        adminApi.post.mockRejectedValueOnce({ response: { status: 500, data: { detail: "Boom" } } });
        ["o1", "h1"].forEach((id) => fireEvent.click(check(id)));
        choose("locked"); apply();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Boom"));
        expect(toast.success).not.toHaveBeenCalled();
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("2 projects selected");
        expect(inGroup("ongoing").getByTestId("project-card-o1")).toBeTruthy();
        expect(inGroup("hold").getByTestId("project-card-h1")).toBeTruthy();
        await waitFor(() => expect(screen.getByTestId("projects-bulk-status-apply").disabled).toBe(false));
        // retry succeeds
        apply();
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
    });

    it("a network error without a server message still gives a clear failure and keeps the selection", async () => {
        await open();
        adminApi.post.mockRejectedValueOnce(new Error("Network Error"));
        fireEvent.click(check("o1")); choose("hold"); apply();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("nothing was changed")));
        expect(bar()).not.toBeNull();
    });

    it("a partial failure never claims full success: names the projects not updated and keeps only those selected", async () => {
        await open();
        adminApi.post.mockImplementationOnce(() => {
            DB.find((p) => p.id === "o1").status = "hold";                                  // the server saved o1 only
            return Promise.resolve({ data: { status: "hold", updated: ["o1"], unchanged: [], failed: [{ id: "o2", reason: "Project not found or deleted" }] } });
        });
        ["o1", "o2"].forEach((id) => fireEvent.click(check(id)));
        choose("hold"); apply();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/1 updated, 1 NOT updated: Ongoing Two/)));
        expect(toast.success).not.toHaveBeenCalled();
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("1 project selected");
        expect(check("o2").getAttribute("aria-label")).toBe("Deselect");
        await waitFor(() => expect(inGroup("hold").queryByTestId("project-card-o1")).toBeTruthy());   // the one that did save moved
    });

    it("ignores a second Apply (and selection changes) while the first request is still running", async () => {
        await open();
        let finish;
        adminApi.post.mockImplementationOnce(() => new Promise((r) => { finish = () => r({ data: { status: "hold", updated: ["o1"], unchanged: [], failed: [] } }); }));
        fireEvent.click(check("o1")); choose("hold"); apply();
        const btn = screen.getByTestId("projects-bulk-status-apply");
        expect(btn.disabled).toBe(true);
        expect(btn.textContent).toContain("Updating");
        expect(screen.getByTestId("projects-bulk-status-select").disabled).toBe(true);
        fireEvent.click(btn); fireEvent.click(btn);
        fireEvent.click(check("o2"));                                                         // frozen while saving
        expect(adminApi.post).toHaveBeenCalledTimes(1);
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("1 project selected");
        finish();
        await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
        expect(adminApi.post).toHaveBeenCalledTimes(1);
    });
});

describe("Projects page — permissions", () => {
    it("admin gets selection, the status toolbar AND Delete", async () => {
        await open();
        fireEvent.click(check("o1"));
        expect(screen.getByTestId("projects-bulk-status")).toBeTruthy();
        expect(screen.getByTestId("projects-bulk-bar-delete")).toBeTruthy();
    });

    it("a team member (who can already change statuses) gets the status toolbar but never Delete", async () => {
        isAdmin.mockReturnValue(false); getRole.mockReturnValue("team");
        await open();
        fireEvent.click(check("o1"));
        expect(screen.getByTestId("projects-bulk-status")).toBeTruthy();
        expect(screen.queryByTestId("projects-bulk-bar-delete")).toBeNull();
        choose("hold"); apply();
        await waitFor(() => expect(adminApi.post).toHaveBeenCalledWith("/projects/bulk-status", { ids: ["o1"], status: "hold" }));
    });

    it("anyone else sees no selection controls and no toolbar at all", async () => {
        isAdmin.mockReturnValue(false); getRole.mockReturnValue(null);
        renderList();
        await screen.findByTestId("project-card-o1");
        expect(screen.queryByTestId("project-check-o1")).toBeNull();
        expect(screen.queryByTestId("project-group-select-ongoing")).toBeNull();
        expect(bar()).toBeNull();
    });

    it("the server rejecting a user (403) is shown and nothing changes on screen", async () => {
        await open();
        adminApi.post.mockRejectedValueOnce({ response: { status: 403, data: { detail: "Access denied" } } });
        fireEvent.click(check("o1")); choose("hold"); apply();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Access denied"));
        expect(inGroup("ongoing").getByTestId("project-card-o1")).toBeTruthy();
    });
});

describe("Projects page — card navigation is unchanged", () => {
    it("clicking a card (nothing selected) opens the project", async () => {
        await open();
        fireEvent.click(within(screen.getByTestId("project-card-o2")).getByText("Ongoing Two"));
        expect(await screen.findByTestId("detail-page")).toBeTruthy();
    });

    it("the selection checkbox never navigates", async () => {
        await open();
        fireEvent.click(check("o1"));
        expect(screen.queryByTestId("detail-page")).toBeNull();
        expect(screen.getByTestId("project-list-page")).toBeTruthy();
    });

    it("while a selection exists a card click toggles it instead of opening the project (existing behaviour)", async () => {
        await open();
        fireEvent.click(check("o1"));
        fireEvent.click(within(screen.getByTestId("project-card-o2")).getByText("Ongoing Two"));
        expect(screen.queryByTestId("detail-page")).toBeNull();
        expect(screen.getByTestId("projects-bulk-bar-count").textContent).toContain("2 projects selected");
    });
});
