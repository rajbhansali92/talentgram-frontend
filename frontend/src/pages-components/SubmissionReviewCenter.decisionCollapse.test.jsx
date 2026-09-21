import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// Mobile Decision Making collapse (2026-09-21) — the panel (Review Decision
// Note + Reject/Hold/Approve+Upload/Approve+Send/Approve) is never
// conditionally unmounted; it stays in the DOM at all times and is shown/
// hidden purely via Tailwind's `hidden lg:flex` classes on
// #decision-making-panel, toggled by its own preceding <button>'s
// aria-expanded state. jsdom has no real CSS engine, so these tests assert
// on aria-expanded and the panel's own className (the same two signals a
// screen reader and a real browser's CSS respectively rely on) rather than
// visibility, matching this file's sibling
// (SubmissionReviewCenter.staleWhatsappError.test.jsx) and this project's
// own no-jest-dom convention throughout.

vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, { error: vi.fn(), success: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
    isAdmin: () => true,
    IMAGE_URL: (p) => p,
}));

vi.mock("@/context/UploadManagerContext", () => ({
    useUploadManager: () => ({ uploadFile: vi.fn() }),
}));

vi.mock("@/components/LocationSelector", () => ({ default: () => null }));
vi.mock("@/components/submission/AdminAddSubmissionModal", () => ({ default: () => null }));

import { adminApi } from "@/lib/api";
import SubmissionReviewCenter from "./SubmissionReviewCenter";

afterEach(cleanup);

const PROJECT_ID = "proj-1";
const PROJECT = { id: PROJECT_ID, brand_name: "Some Project", custom_questions: [] };

const SUB_A = { id: "sub-a", talent_name: "Talent A", talent_email: "a@example.com", decision: "pending", media: [], form_data: {} };
const SUB_B = { id: "sub-b", talent_name: "Talent B", talent_email: "b@example.com", decision: "pending", media: [], form_data: {} };

function detailFor(sub) {
    return { ...sub, field_visibility: {}, talent_portfolio_media: [] };
}

function mockCommonEndpoints() {
    adminApi.get.mockImplementation((url) => {
        if (url === `/projects/${PROJECT_ID}`) return Promise.resolve({ data: PROJECT });
        if (url === `/projects/${PROJECT_ID}/submissions`) return Promise.resolve({ data: [SUB_A, SUB_B] });
        if (url === `/projects/${PROJECT_ID}/submissions/sub-a`) return Promise.resolve({ data: detailFor(SUB_A) });
        if (url === `/projects/${PROJECT_ID}/submissions/sub-b`) return Promise.resolve({ data: detailFor(SUB_B) });
        return Promise.resolve({ data: {} });
    });
    adminApi.post.mockImplementation((url) => {
        if (url.endsWith("/admin-token")) return Promise.resolve({ data: { token: "tok" } });
        return Promise.resolve({ data: {} });
    });
}

function renderPage() {
    return render(
        <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}/submissions`]}>
            <Routes>
                <Route path="/projects/:id/submissions" element={<SubmissionReviewCenter />} />
            </Routes>
        </MemoryRouter>,
    );
}

function listItemFor(name) {
    return screen.getAllByText(name).find((el) => el.tagName === "SPAN");
}
function detailHeadingFor(name) {
    return screen.getByRole("heading", { name, level: 2 });
}

function getToggleAndPanel(container) {
    const panel = container.querySelector("#decision-making-panel");
    return { panel, toggle: panel?.previousElementSibling };
}

beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    mockCommonEndpoints();
});

describe("SubmissionReviewCenter — mobile Decision Making collapse", () => {
    it("is collapsed by default when a talent's detail loads", async () => {
        const { container } = renderPage();
        await waitFor(() => expect(detailHeadingFor("Talent A")).toBeTruthy());

        const { panel, toggle } = getToggleAndPanel(container);
        expect(toggle).toBeTruthy();
        expect(toggle.getAttribute("aria-expanded")).toBe("false");
        expect(panel.className).toMatch(/\bhidden\b/);
        expect(panel.className).toMatch(/\blg:flex\b/); // desktop always shown regardless of state
    });

    it("expands on click, revealing all four decision actions, and collapses again on a second click", async () => {
        const { container } = renderPage();
        await waitFor(() => expect(detailHeadingFor("Talent A")).toBeTruthy());
        const { toggle } = getToggleAndPanel(container);

        fireEvent.click(toggle);
        await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
        const { panel: expandedPanel } = getToggleAndPanel(container);
        expect(expandedPanel.className).not.toMatch(/\bhidden\b/);
        // Existing labels/handlers untouched — same buttons, same text.
        // Scoped to the panel itself: the sidebar's own filter tabs (e.g.
        // a "hold" status filter) would otherwise collide with these names.
        const withinPanel = within(expandedPanel);
        expect(withinPanel.getByRole("button", { name: /^Reject$/i })).toBeTruthy();
        expect(withinPanel.getByRole("button", { name: /^Hold$/i })).toBeTruthy();
        expect(withinPanel.getByRole("button", { name: /Approve \+ Upload/i })).toBeTruthy();
        expect(withinPanel.getByRole("button", { name: /Approve \+ Send/i })).toBeTruthy();
        expect(withinPanel.getByRole("button", { name: /^Approve$/i })).toBeTruthy();

        fireEvent.click(toggle);
        await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
        const { panel: collapsedAgain } = getToggleAndPanel(container);
        expect(collapsedAgain.className).toMatch(/\bhidden\b/);
    });

    it("resets to collapsed after switching to a different talent", async () => {
        const { container } = renderPage();
        await waitFor(() => expect(detailHeadingFor("Talent A")).toBeTruthy());
        const { toggle: toggleA } = getToggleAndPanel(container);

        fireEvent.click(toggleA);
        await waitFor(() => expect(toggleA.getAttribute("aria-expanded")).toBe("true"));

        fireEvent.click(listItemFor("Talent B"));
        await waitFor(() => expect(detailHeadingFor("Talent B")).toBeTruthy());

        const { toggle: toggleB } = getToggleAndPanel(container);
        expect(toggleB.getAttribute("aria-expanded")).toBe("false");
    });

    it("the toggle is a real <button> with aria-controls linking to the panel (accessible, not a clickable div)", async () => {
        const { container } = renderPage();
        await waitFor(() => expect(detailHeadingFor("Talent A")).toBeTruthy());
        const { panel, toggle } = getToggleAndPanel(container);

        expect(toggle.tagName).toBe("BUTTON");
        expect(toggle.getAttribute("type")).toBe("button");
        expect(toggle.getAttribute("aria-controls")).toBe(panel.id);
    });
});
