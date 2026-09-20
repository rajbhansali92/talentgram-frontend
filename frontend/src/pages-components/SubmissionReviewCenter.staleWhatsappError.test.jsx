import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// Stale-async-error-state fix (2026-09-20): a delayed Approve + Send/Upload
// response for submission A must never write its error onto the page once
// the admin has already moved on to submission B. These tests drive the
// REAL component (not a stripped-down harness) through the exact reported
// scenario — start an action on one submission, switch to a different one
// before the backend responds, then let the stale response land — and
// assert the stale error never appears. Heavy, unrelated child pieces
// (upload manager, location picker, add-submission modal) are stubbed out,
// matching this suite's existing convention (see WhatsAppEnginePage.test.jsx).

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, {
        error: (...a) => toastError(...a),
        success: (...a) => toastSuccess(...a),
    }),
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

// Modeling the reported scenario as two submissions within the same
// project ("Raj Mehta / Limca Film 2" -> "Coca cola"): the fix's guard
// (currentSelectionRef) compares BOTH submissionId and projectId, and this
// exercises the submissionId half of that check through the real
// selection-list click handler — the identical code path a cross-project
// navigation would hit, since both update the same ref the same way.
const FILM2_SUBMISSION = {
    id: "sub-film2", talent_name: "Raj Mehta", talent_email: "raj@example.com",
    decision: "pending", media: [], form_data: {},
};
const COCA_COLA_SUBMISSION = {
    id: "sub-cocacola", talent_name: "Some Other Talent", talent_email: "other@example.com",
    decision: "pending", media: [], form_data: {},
};

function detailFor(sub) {
    return { ...sub, field_visibility: {}, talent_portfolio_media: [] };
}

function mockCommonEndpoints() {
    adminApi.get.mockImplementation((url) => {
        if (url === `/projects/${PROJECT_ID}`) return Promise.resolve({ data: PROJECT });
        if (url === `/projects/${PROJECT_ID}/submissions`) {
            return Promise.resolve({ data: [FILM2_SUBMISSION, COCA_COLA_SUBMISSION] });
        }
        if (url === `/projects/${PROJECT_ID}/submissions/sub-film2`) {
            return Promise.resolve({ data: detailFor(FILM2_SUBMISSION) });
        }
        if (url === `/projects/${PROJECT_ID}/submissions/sub-cocacola`) {
            return Promise.resolve({ data: detailFor(COCA_COLA_SUBMISSION) });
        }
        return Promise.resolve({ data: {} });
    });
    // admin-token (fire-and-forget, has its own .catch — still give it a
    // resolved value so it doesn't leave a dangling unresolved mock call).
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

function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

// Every talent name is rendered twice once its submission is selected (the
// sidebar list-item row AND the detail panel's <h2> heading) — these
// helpers disambiguate instead of relying on a bare, ambiguous getByText.
function listItemFor(name) {
    return screen.getAllByText(name).find((el) => el.tagName === "SPAN");
}
function detailHeadingFor(name) {
    return screen.getByRole("heading", { name, level: 2 });
}

beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    toastError.mockReset();
    toastSuccess.mockReset();
    mockCommonEndpoints();
});

describe("SubmissionReviewCenter — stale WhatsApp-action error state", () => {
    it("a late-rejecting Approve + Send for Film2 never shows its error once the admin has switched to Coca Cola", async () => {
        renderPage();

        // Film2 is the first submission returned, so it's auto-selected.
        await waitFor(() => expect(detailHeadingFor("Raj Mehta")).toBeTruthy());
        const sendButton = await screen.findByRole("button", { name: /Approve \+ Send/i });

        const approveSendCall = deferred();
        adminApi.post.mockImplementation((url) => {
            if (url === `/projects/${PROJECT_ID}/submissions/sub-film2/approve-send`) {
                return approveSendCall.promise;
            }
            if (url.endsWith("/admin-token")) return Promise.resolve({ data: { token: "tok" } });
            return Promise.resolve({ data: {} });
        });

        fireEvent.click(sendButton);
        // Button now reflects the in-flight action for Film2.
        await screen.findByText(/Sending…/i);

        // Admin switches away to Coca Cola BEFORE the approve-send call
        // has resolved at all.
        fireEvent.click(listItemFor("Some Other Talent"));
        await waitFor(() => expect(detailHeadingFor("Some Other Talent")).toBeTruthy());
        // Coca Cola is now selected and showing no error, exactly as the
        // "load selected submission" effect's own clear should produce.
        expect(screen.queryByText(/Couldn't verify marked media/i)).toBeNull();

        // NOW the stale Film2 request finally rejects — this is the exact
        // "SEND ATTENTION REQUIRED / Couldn't verify marked media" shape
        // from the real incident.
        approveSendCall.reject({ response: { data: { detail: "Couldn't verify marked media for this talent/project right now — try again shortly." } } });

        // Give the rejected promise's .catch handler a full turn to run
        // (and, if the bug were present, to call setWhatsappActionError).
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        // The stale Film2 error must never have surfaced anywhere on the
        // page, regardless of which submission is currently showing.
        expect(screen.queryByText(/Couldn't verify marked media/i)).toBeNull();
        // Coca Cola must still be the one selected/displayed — the stale
        // response must not have reverted or otherwise disturbed selection.
        expect(detailHeadingFor("Some Other Talent")).toBeTruthy();
    });

    // Deliberately real timers, not fake ones — the poll loop's own
    // setTimeout(POLL_MS) is real production code, and vi.useFakeTimers()
    // is well known to interact badly with React 18's own scheduler
    // (which also relies on setTimeout/MessageChannel internally),
    // silently stalling a state update's commit without any error. Real
    // timers here reproduce the bug reliably; fake timers did not.
    it("a late status.ok===false response for Film2 never shows its report once the admin has switched to Coca Cola", { timeout: 10000 }, async () => {
        renderPage();
        await waitFor(() => expect(detailHeadingFor("Raj Mehta")).toBeTruthy());
        const sendButton = await screen.findByRole("button", { name: /Approve \+ Send/i });

        const statusCall = deferred();
        adminApi.post.mockImplementation((url) => {
            if (url === `/projects/${PROJECT_ID}/submissions/sub-film2/approve-send`) {
                return Promise.resolve({ data: { request_id: "req-film2-1" } });
            }
            if (url.endsWith("/admin-token")) return Promise.resolve({ data: { token: "tok" } });
            return Promise.resolve({ data: {} });
        });
        adminApi.get.mockImplementation((url) => {
            if (url === `/projects/${PROJECT_ID}/submissions/sub-film2/whatsapp-action-status/req-film2-1`) {
                return statusCall.promise;
            }
            if (url === `/projects/${PROJECT_ID}`) return Promise.resolve({ data: PROJECT });
            if (url === `/projects/${PROJECT_ID}/submissions`) {
                return Promise.resolve({ data: [FILM2_SUBMISSION, COCA_COLA_SUBMISSION] });
            }
            if (url === `/projects/${PROJECT_ID}/submissions/sub-film2`) {
                return Promise.resolve({ data: detailFor(FILM2_SUBMISSION) });
            }
            if (url === `/projects/${PROJECT_ID}/submissions/sub-cocacola`) {
                return Promise.resolve({ data: detailFor(COCA_COLA_SUBMISSION) });
            }
            return Promise.resolve({ data: {} });
        });

        fireEvent.click(sendButton);
        await screen.findByText(/Sending…/i);

        // Admin switches to Coca Cola while the FIRST status poll is still
        // pending (real ~3s poll interval — bounded by this test's own
        // 10s timeout).
        await new Promise((r) => setTimeout(r, 3200));
        fireEvent.click(listItemFor("Some Other Talent"));
        await waitFor(() => expect(detailHeadingFor("Some Other Talent")).toBeTruthy());
        expect(screen.queryByText(/SEND ATTENTION REQUIRED/i)).toBeNull();

        // The stale status finally resolves: done, but ok === false — the
        // exact "operation ran but failed" shape.
        statusCall.resolve({
            data: { done: true, ok: false, report: "SEND ATTENTION REQUIRED\n\n0/2 media sent, 2 failed" },
        });
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        expect(screen.queryByText(/SEND ATTENTION REQUIRED/i)).toBeNull();
        expect(detailHeadingFor("Some Other Talent")).toBeTruthy();
    });

    it("selecting a different submission still clears any existing error immediately (unchanged pre-existing behavior)", async () => {
        renderPage();
        await waitFor(() => expect(detailHeadingFor("Raj Mehta")).toBeTruthy());
        const sendButton = await screen.findByRole("button", { name: /Approve \+ Send/i });

        adminApi.post.mockImplementation((url) => {
            if (url === `/projects/${PROJECT_ID}/submissions/sub-film2/approve-send`) {
                return Promise.reject({ response: { data: { detail: "Some real Film2 error" } } });
            }
            if (url.endsWith("/admin-token")) return Promise.resolve({ data: { token: "tok" } });
            return Promise.resolve({ data: {} });
        });

        fireEvent.click(sendButton);
        await screen.findByText(/Some real Film2 error/i);

        fireEvent.click(listItemFor("Some Other Talent"));
        await waitFor(() => expect(screen.queryByText(/Some real Film2 error/i)).toBeNull());
    });
});
