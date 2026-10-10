import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { success: vi.fn(), error: vi.fn() }) }));
let mockIsAdmin = true;
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() },
    isAdmin: () => mockIsAdmin,
    getSubdomainUrl: () => "https://example.test",
}));
// heavy tab bodies are irrelevant to the header
const { stub } = vi.hoisted(() => ({ stub: () => ({ default: () => null }) }));
vi.mock("@/pages-components/ProjectPipeline", () => stub());
vi.mock("@/pages-components/ProjectAIScout", () => stub());
vi.mock("@/pages-components/ProductionDesk", () => stub());
vi.mock("@/components/WhatsAppShareButton", () => stub());
vi.mock("@/components/MaterialModal", () => stub());

import { adminApi } from "@/lib/api";
import ProjectEdit from "./ProjectEdit";

afterEach(cleanup);

// jsdom lacks these browser APIs, which the page's layout code uses
globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
globalThis.IntersectionObserver = globalThis.IntersectionObserver || class { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } };
window.scrollTo = window.scrollTo || (() => {});

const PROJECT = { id: "p-1", slug: "baskin-1", brand_name: "Baskin Robbins Film 3", status: "ongoing", materials: [], custom_questions: [], talent_budget: [], client_budget: [], submission_requirements: { fields: {} } };

function Where() { const l = useLocation(); return <div data-testid="where">{l.pathname}{l.search}</div>; }
function renderAt(path = "/admin/projects/p-1?tab=details") {
    return render(
        <MemoryRouter initialEntries={[path]}>
            <Routes>
                <Route path="/admin/projects/:id" element={<><ProjectEdit /><Where /></>} />
            </Routes>
        </MemoryRouter>,
    );
}

beforeEach(() => {
    mockIsAdmin = true;
    Object.values(adminApi).forEach((f) => f.mockReset());
    adminApi.get.mockImplementation((url) => {
        if (url === "/projects/p-1") return Promise.resolve({ data: PROJECT });
        if (url === "/projects/n-2") return Promise.resolve({ data: { ...PROJECT, id: "n-2", brand_name: "Baskin Robbins Film 3 (Copy)" } });
        return Promise.resolve({ data: [] });
    });
});

describe("Project header — Duplicate", () => {
    it("shows Duplicate next to Edit for an admin, leaving Edit, Delete and View Audition Material in place", async () => {
        renderAt();
        const dup = await screen.findByTestId("duplicate-project-btn");
        const edit = screen.getByTestId("edit-project-btn");
        expect(dup.textContent).toMatch(/Duplicate/);
        expect(dup.nextElementSibling).toBe(edit);                                        // directly beside Edit
        expect(screen.getByTestId("delete-project-btn")).toBeTruthy();
        expect(screen.getByTestId("view-audition-material-btn")).toBeTruthy();
    });

    it("is not offered to non-admins", async () => {
        mockIsAdmin = false;
        renderAt();
        await screen.findByTestId("edit-project-btn");
        expect(screen.queryByTestId("duplicate-project-btn")).toBeNull();
    });

    it("is hidden while editing, so unsaved changes are never ambiguous", async () => {
        renderAt();
        fireEvent.click(await screen.findByTestId("edit-project-btn"));
        await waitFor(() => expect(screen.queryByTestId("duplicate-project-btn")).toBeNull());
        expect(screen.getByTestId("save-project-btn")).toBeTruthy();
    });

    it("opens the dialog with the original name; Cancel changes nothing", async () => {
        renderAt();
        fireEvent.click(await screen.findByTestId("duplicate-project-btn"));
        const dlg = await screen.findByTestId("duplicate-project-dialog");
        expect(within(dlg).getByTestId("duplicate-project-name").value).toBe("Baskin Robbins Film 3 (Copy)");
        fireEvent.click(within(dlg).getByTestId("duplicate-project-cancel"));
        expect(screen.queryByTestId("duplicate-project-dialog")).toBeNull();
        expect(adminApi.post).not.toHaveBeenCalled();
        expect(screen.getByTestId("where").textContent).toBe("/admin/projects/p-1?tab=details");
    });

    it("on success it opens the new project's details page", async () => {
        adminApi.post.mockResolvedValue({ data: { id: "n-2", brand_name: "Baskin Robbins Film 3 (Copy)" } });
        renderAt();
        fireEvent.click(await screen.findByTestId("duplicate-project-btn"));
        fireEvent.click(await screen.findByTestId("duplicate-project-confirm"));
        await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/admin/projects/n-2?tab=details"));
        expect(adminApi.post).toHaveBeenCalledWith("/projects/p-1/duplicate", expect.objectContaining({ name: "Baskin Robbins Film 3 (Copy)" }));
        expect(screen.queryByTestId("duplicate-project-dialog")).toBeNull();
    });

    it("on failure it stays on the original project and shows the error", async () => {
        adminApi.post.mockRejectedValue({ response: { data: { detail: "Admin access required" } } });
        renderAt();
        fireEvent.click(await screen.findByTestId("duplicate-project-btn"));
        fireEvent.click(await screen.findByTestId("duplicate-project-confirm"));
        expect((await screen.findByTestId("duplicate-project-error")).textContent).toBe("Admin access required");
        expect(screen.getByTestId("where").textContent).toBe("/admin/projects/p-1?tab=details");
    });
});
