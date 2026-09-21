import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

// Action Queue panel (2026-09-21) — the durable, backend-tracked
// replacement for the old inline poll-loop. Covers: hidden when empty,
// renders active/failed/completed rows with the right label, auto-
// expands the first time an active action appears (never re-forces open
// after the admin collapses OR closes it), Retry posts to the right
// endpoint and triggers an immediate refetch, and — the whole feature's
// own point — this component is a self-contained module with zero
// dependency on which (if any) Submission Review page is currently
// mounted.
//
// Side-panel redesign (2026-09-21) — the panel now has three presentation
// modes (open/collapsed/closed) instead of a boolean, with a dedicated X
// that hides it completely (replaced by a small reopen icon button, never
// a large permanent floating button) until the admin explicitly reopens
// it. See ActionQueuePanel.jsx's own MODE_* docstring for the full
// behavior contract this file exercises.
//
// This project has no @testing-library/jest-dom (not a dependency —
// see SubmissionReviewCenter.staleWhatsappError.test.jsx's own
// convention), so assertions use plain queryBy*/getBy* truthiness
// rather than .toBeInTheDocument().
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn() },
}));

import { adminApi } from "@/lib/api";
import ActionQueuePanel, { __resetForTests } from "./ActionQueuePanel";

beforeEach(() => {
    __resetForTests();
});

afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    __resetForTests();
});

const ACTIVE_ACTION = {
    id: "action-1", action_type: "send", talent_label: "Raj Mehta", project_label: "Limca Film2",
    project_id: "proj-1", submission_id: "sub-1", state: "VERIFYING", display_state: "VERIFYING",
    error_message: null, retryable: false, created_at: "2026-09-21T00:00:00Z",
};

const FAILED_ACTION = {
    id: "action-2", action_type: "send", talent_label: "Priya Sharma", project_label: "Pepsi",
    project_id: "proj-2", submission_id: "sub-2", state: "FAILED", display_state: "FAILED",
    error_message: "No marked WhatsApp media found yet.", retryable: true, created_at: "2026-09-21T00:00:01Z",
};

const COMPLETED_ACTION = {
    id: "action-3", action_type: "upload", talent_label: "Aman", project_label: "Coca Cola",
    project_id: "proj-3", submission_id: "sub-3", state: "COMPLETED", display_state: "COMPLETED",
    error_message: null, retryable: false, created_at: "2026-09-21T00:00:02Z",
};

// Regression fixture for the real production incident (2026-09-21, Akarsh
// Kumar Gowda / Snapdragon Computer): a send that genuinely completed but
// had one item WhatsApp couldn't confirm delivery for must still surface
// that nuance — a bare "Completed" badge would silently hide it.
const COMPLETED_ACTION_UNVERIFIED = {
    id: "action-4", action_type: "send", talent_label: "Akarsh Kumar Gowda", project_label: "Snapdragon Computer",
    project_id: "proj-4", submission_id: "sub-4", state: "COMPLETED", display_state: "COMPLETED",
    error_message: null, retryable: false, has_unverified_media: true, created_at: "2026-09-21T00:00:03Z",
};

function mockActions(actions) {
    adminApi.get.mockResolvedValue({ data: { actions } });
}

describe("ActionQueuePanel", () => {
    it("renders nothing when the queue is empty", async () => {
        mockActions([]);
        const { container } = render(<ActionQueuePanel />);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledWith("/submission-actions"));
        expect(container.firstChild).toBeNull();
    });

    it("shows the active-count badge and auto-expands (open) on the first active action", async () => {
        mockActions([ACTIVE_ACTION, COMPLETED_ACTION]);
        render(<ActionQueuePanel />);
        await waitFor(() => expect(screen.queryByText("Raj Mehta")).not.toBeNull());
        expect(screen.getByTestId("action-queue-toggle").getAttribute("aria-expanded")).toBe("true");
        expect(screen.queryByText("1")).not.toBeNull(); // active-count badge
        expect(screen.queryByText("Verifying media…")).not.toBeNull();
        expect(screen.queryByText("Aman")).not.toBeNull();
        expect(screen.queryByText("Completed")).not.toBeNull();
    });

    it("shows the failed action's real error message and a Retry button", async () => {
        // A lone FAILED action does NOT auto-expand (only an ACTIVE one
        // does — see the component's own ACTIVE_STATES set) — a stale
        // failure from a prior session shouldn't force the panel open on
        // every page load. Expand manually, matching how an admin would.
        mockActions([FAILED_ACTION]);
        render(<ActionQueuePanel />);
        await waitFor(() => expect(screen.queryByTestId("action-queue-toggle")).not.toBeNull());
        fireEvent.click(screen.getByTestId("action-queue-toggle"));
        await waitFor(() => expect(screen.queryByText("Priya Sharma")).not.toBeNull());
        expect(screen.queryByText("No marked WhatsApp media found yet.")).not.toBeNull();
        expect(screen.queryByRole("button", { name: /retry/i })).not.toBeNull();
    });

    it("collapsing stays collapsed even if new active actions appear later", async () => {
        mockActions([COMPLETED_ACTION]);
        render(<ActionQueuePanel />);
        await waitFor(() => expect(screen.queryByTestId("action-queue-panel")).not.toBeNull());
        // Starts collapsed (nothing active yet on first fetch).
        expect(screen.getByTestId("action-queue-toggle").getAttribute("aria-expanded")).toBe("false");
        expect(screen.queryByText("Aman")).toBeNull();
        fireEvent.click(screen.getByTestId("action-queue-toggle"));
        await waitFor(() => expect(screen.queryByText("Aman")).not.toBeNull());
        fireEvent.click(screen.getByTestId("action-queue-toggle"));
        await waitFor(() => expect(screen.queryByText("Aman")).toBeNull());
    });

    it("shows a delivery-unconfirmed notice only on the completed action with has_unverified_media", async () => {
        mockActions([ACTIVE_ACTION, COMPLETED_ACTION, COMPLETED_ACTION_UNVERIFIED]);
        render(<ActionQueuePanel />);
        await waitFor(() => expect(screen.queryByText("Akarsh Kumar Gowda")).not.toBeNull());
        expect(screen.queryByText("Aman")).not.toBeNull(); // plain completed action still renders normally
        // Exactly one notice — the plain COMPLETED_ACTION (Aman) never gets it.
        expect(screen.queryAllByText(/delivery unconfirmed/i)).toHaveLength(1);
    });

    it("Retry posts to the action's own endpoint and refetches immediately", async () => {
        mockActions([FAILED_ACTION]);
        adminApi.post.mockResolvedValue({ data: { ok: true } });
        render(<ActionQueuePanel />);
        await waitFor(() => expect(screen.queryByTestId("action-queue-toggle")).not.toBeNull());
        fireEvent.click(screen.getByTestId("action-queue-toggle"));
        await waitFor(() => expect(screen.queryByText("Priya Sharma")).not.toBeNull());

        fireEvent.click(screen.getByRole("button", { name: /retry/i }));

        await waitFor(() => expect(adminApi.post).toHaveBeenCalledWith(
            `/projects/${FAILED_ACTION.project_id}/submissions/${FAILED_ACTION.submission_id}/whatsapp-action/${FAILED_ACTION.id}/retry`,
        ));
        // Forces an immediate refetch rather than waiting out the next poll tick.
        await waitFor(() => expect(adminApi.get.mock.calls.length).toBeGreaterThanOrEqual(2));
    });

    describe("side panel: open / collapsed / closed with X", () => {
        it("X closes the panel completely — no leftover box, bar, or overlay, replaced by a small reopen icon", async () => {
            mockActions([FAILED_ACTION]);
            const { container } = render(<ActionQueuePanel />);
            await waitFor(() => expect(screen.queryByTestId("action-queue-panel")).not.toBeNull());

            fireEvent.click(screen.getByTestId("action-queue-close"));

            expect(screen.queryByTestId("action-queue-panel")).toBeNull();
            expect(screen.queryByTestId("action-queue-toggle")).toBeNull();
            expect(screen.queryByTestId("action-queue-close")).toBeNull();
            const reopen = screen.queryByTestId("action-queue-reopen");
            expect(reopen).not.toBeNull();
            // Genuinely small — a single icon button, not a big empty container.
            expect(container.childElementCount).toBe(1);
            expect(reopen.tagName).toBe("BUTTON");
        });

        it("reopening via the small icon returns the panel to OPEN", async () => {
            mockActions([FAILED_ACTION]);
            render(<ActionQueuePanel />);
            await waitFor(() => expect(screen.queryByTestId("action-queue-panel")).not.toBeNull());

            fireEvent.click(screen.getByTestId("action-queue-close"));
            await waitFor(() => expect(screen.queryByTestId("action-queue-reopen")).not.toBeNull());

            fireEvent.click(screen.getByTestId("action-queue-reopen"));

            await waitFor(() => expect(screen.queryByTestId("action-queue-panel")).not.toBeNull());
            expect(screen.getByTestId("action-queue-toggle").getAttribute("aria-expanded")).toBe("true");
            expect(screen.queryByText("Priya Sharma")).not.toBeNull();
        });

        it("stays closed across ordinary polling ticks, a new active action appearing, and an action completing — never auto-reopens", async () => {
            // Drives the SAME mounted instance's real setInterval poll loop
            // (POLL_MS) forward, rather than mounting a second instance —
            // a second mount would get its own fresh, unrelated
            // userInteractedRef/mode and wouldn't prove anything about
            // this one staying closed.
            vi.useFakeTimers({ shouldAdvanceTime: true });
            try {
                mockActions([FAILED_ACTION]);
                render(<ActionQueuePanel />);
                await waitFor(() => expect(screen.queryByTestId("action-queue-panel")).not.toBeNull());

                fireEvent.click(screen.getByTestId("action-queue-close"));
                expect(screen.queryByTestId("action-queue-reopen")).not.toBeNull();

                // A poll tick brings in a brand-new ACTIVE action (the exact
                // condition that auto-opens the panel from its DEFAULT
                // state) — an explicit close must override that entirely.
                mockActions([FAILED_ACTION, ACTIVE_ACTION]);
                await vi.advanceTimersByTimeAsync(3100);
                expect(screen.queryByTestId("action-queue-panel")).toBeNull();
                expect(screen.queryByTestId("action-queue-reopen")).not.toBeNull();

                // The active action completing is also just another poll
                // tick — still must not reopen anything.
                mockActions([{ ...ACTIVE_ACTION, state: "COMPLETED", display_state: "COMPLETED" }, FAILED_ACTION]);
                await vi.advanceTimersByTimeAsync(3100);
                expect(screen.queryByTestId("action-queue-panel")).toBeNull();
            } finally {
                vi.useRealTimers();
            }
        });

        it("collapsing (not closing) leaves the small reopen icon absent — collapsed is its own minimal state, not the same as closed", async () => {
            mockActions([FAILED_ACTION]);
            render(<ActionQueuePanel />);
            await waitFor(() => expect(screen.queryByTestId("action-queue-panel")).not.toBeNull());
            fireEvent.click(screen.getByTestId("action-queue-toggle")); // open
            await waitFor(() => expect(screen.getByTestId("action-queue-toggle").getAttribute("aria-expanded")).toBe("true"));
            fireEvent.click(screen.getByTestId("action-queue-toggle")); // back to collapsed, not closed
            await waitFor(() => expect(screen.getByTestId("action-queue-toggle").getAttribute("aria-expanded")).toBe("false"));

            // Still the full panel shell (header + toggle + close), just
            // without the list — never replaced by the reopen icon.
            expect(screen.queryByTestId("action-queue-panel")).not.toBeNull();
            expect(screen.queryByTestId("action-queue-close")).not.toBeNull();
            expect(screen.queryByTestId("action-queue-reopen")).toBeNull();
        });
    });

    // "Persists while navigating talents" (AdminLayout.jsx renders
    // <ActionQueuePanel /> once, as a sibling of <Outlet />, never inside
    // a routed page — see that file's own comment next to the mount
    // point). React Router only re-renders <Outlet />'s content on
    // navigation, so this component is architecturally NEVER unmounted
    // by a talent switch or a page change; its mode/data are ordinary
    // component/module state that simply keeps living. The "stays closed
    // across ordinary polling ticks..." test above already proves the
    // part of that guarantee this file CAN exercise in isolation (mode
    // surviving new data arriving without user action) — an unmount/
    // remount would test something this architecture never actually
    // does, so it isn't simulated here.
});
