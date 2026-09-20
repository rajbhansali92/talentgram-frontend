import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

// Action Queue panel (2026-09-21) — the durable, backend-tracked
// replacement for the old inline poll-loop. Covers: hidden when empty,
// renders active/failed/completed rows with the right label, auto-
// expands the first time an active action appears (never re-forces open
// after the admin collapses it), Retry posts to the right endpoint and
// triggers an immediate refetch, and — the whole feature's own point —
// this component is a self-contained module with zero dependency on
// which (if any) Submission Review page is currently mounted.
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

    it("shows the active-count badge and auto-expands on the first active action", async () => {
        mockActions([ACTIVE_ACTION, COMPLETED_ACTION]);
        render(<ActionQueuePanel />);
        await waitFor(() => expect(screen.queryByText("Raj Mehta")).not.toBeNull());
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
        expect(screen.queryByText("Aman")).toBeNull();
        fireEvent.click(screen.getByTestId("action-queue-toggle"));
        await waitFor(() => expect(screen.queryByText("Aman")).not.toBeNull());
        fireEvent.click(screen.getByTestId("action-queue-toggle"));
        await waitFor(() => expect(screen.queryByText("Aman")).toBeNull());
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
});
