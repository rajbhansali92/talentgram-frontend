import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, waitFor, screen } from "@testing-library/react";
import SubmissionPage from "./SubmissionPage";
import { UploadManagerProvider } from "@/context/UploadManagerContext";

// Regression (2026-09-19): a talent who already submitted, then reopened
// their project link with the WhatsApp "?material=1" deep link, saw the
// Audition Material page flash and immediately jump back to the "Thank
// You" / Submission Hub screen — and that Hub screen had no way at all to
// reopen the brief (no "View Audition Material" entry).
//
// Root cause: the Hub screen (rendered whenever the resumed submission's
// status isn't "draft") is a separate early `return` that never rendered
// MaterialModal, regardless of the `showMaterial` state the ?material=1
// deep-link effect already flips to true. Fixed by rendering the same
// MaterialModal/button the pre-submission form already uses inside that
// branch too — no new state, no new routing, no change to the branch
// condition itself.

// jsdom in this project's vitest environment doesn't provide window.localStorage
// at all (a pre-existing test-environment gap, unrelated to this fix) — shim it
// in-memory, scoped to just this file, rather than touching global test config.
class MemoryStorage {
    constructor() { this.store = {}; }
    getItem(k) { return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null; }
    setItem(k, v) { this.store[k] = String(v); }
    removeItem(k) { delete this.store[k]; }
    clear() { this.store = {}; }
}
Object.defineProperty(window, "localStorage", { value: new MemoryStorage(), writable: true });

const SLUG = "test-project";

vi.mock("next/navigation", () => ({
    useParams: () => ({ slug: SLUG }),
    useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const PROJECT_WITH_MATERIAL = {
    id: "proj-1",
    slug: SLUG,
    brand_name: "Tide with SRK (Film 2)",
    character: "Lead",
    materials: [{ id: "m1", category: "script", url: "https://example.com/script.pdf" }],
    video_links: [],
};

const PROJECT_WITHOUT_MATERIAL = { ...PROJECT_WITH_MATERIAL, materials: [], video_links: [] };

const SUBMITTED_SUBMISSION = {
    id: "sub-1",
    status: "submitted",
    talent_email: "talent@example.com",
    form_data: {},
};

function mockApiGet(project, submission) {
    return vi.fn((url) => {
        if (url === `/public/projects/${SLUG}`) return Promise.resolve({ data: project });
        if (url === `/public/projects/${SLUG}/submission/me`) return Promise.resolve({ data: submission });
        // The ATK-resume effect sets `saved`, which a second, separate
        // "resume by JWT" effect watches and re-fetches by id — exactly
        // what a real backend would also do here. Answering it with the
        // same submission keeps the two effects consistent, matching prod.
        if (url === `/public/submissions/${submission.id}`) return Promise.resolve({ data: submission });
        return Promise.resolve({ data: {} });
    });
}

vi.mock("@/lib/api", async () => {
    const actual = await vi.importActual("@/lib/api");
    return {
        ...actual,
        api: { get: vi.fn(), post: vi.fn() },
        adminApi: { get: vi.fn(), post: vi.fn() },
        portalApi: { get: vi.fn(), post: vi.fn() },
    };
});

import { api } from "@/lib/api";

function renderSubmissionPage() {
    return render(
        <UploadManagerProvider>
            <SubmissionPage />
        </UploadManagerProvider>
    );
}

beforeEach(() => {
    window.localStorage.clear();
    window.history.pushState({}, "", `/submit/${SLUG}`);
});
afterEach(cleanup);

describe("SubmissionPage — Thank You screen honours ?material=1 for an already-submitted talent", () => {
    it("a fresh, never-submitted talent still gets the normal application wizard, unaffected", async () => {
        // No ATK in localStorage — nothing to resume, exactly a new applicant.
        api.get = mockApiGet(PROJECT_WITH_MATERIAL, { id: "unused", status: "draft" });

        renderSubmissionPage();

        await waitFor(() => expect(screen.getByTestId("submission-content")).toBeTruthy());
        expect(screen.queryByTestId("submission-thank-you")).toBeNull();
        expect(screen.queryByTestId("audition-material-modal")).toBeNull();
    });

    it("opens the Audition Material modal instead of only showing the Thank You screen", async () => {
        window.history.pushState({}, "", `/submit/${SLUG}?material=1`);
        window.localStorage.setItem(`tg_atk_${SLUG}`, "test-atk-token");
        api.get = mockApiGet(PROJECT_WITH_MATERIAL, SUBMITTED_SUBMISSION);

        renderSubmissionPage();

        await waitFor(() => expect(screen.getByTestId("submission-thank-you")).toBeTruthy());
        await waitFor(() => expect(screen.getByTestId("audition-material-modal")).toBeTruthy());
    });

    it("does not show the material modal on the Thank You screen without ?material=1", async () => {
        window.localStorage.setItem(`tg_atk_${SLUG}`, "test-atk-token");
        api.get = mockApiGet(PROJECT_WITH_MATERIAL, SUBMITTED_SUBMISSION);

        renderSubmissionPage();

        await waitFor(() => expect(screen.getByTestId("submission-thank-you")).toBeTruthy());
        expect(screen.queryByTestId("audition-material-modal")).toBeNull();
    });

    it("still opens the material modal after a simulated refresh (URL alone determines the view)", async () => {
        window.history.pushState({}, "", `/submit/${SLUG}?material=1`);
        window.localStorage.setItem(`tg_atk_${SLUG}`, "test-atk-token");
        api.get = mockApiGet(PROJECT_WITH_MATERIAL, SUBMITTED_SUBMISSION);

        renderSubmissionPage();

        await waitFor(() => expect(screen.getByTestId("audition-material-modal")).toBeTruthy());
    });

    it("exposes a 'View Audition Material' button on the Thank You screen's project-details panel", async () => {
        window.localStorage.setItem(`tg_atk_${SLUG}`, "test-atk-token");
        api.get = mockApiGet(PROJECT_WITH_MATERIAL, SUBMITTED_SUBMISSION);

        renderSubmissionPage();

        await waitFor(() => expect(screen.getByTestId("submission-thank-you")).toBeTruthy());
        const detailsToggle = screen.getByTestId("thank-you-view-project-details-btn");
        detailsToggle.click();
        await waitFor(() => expect(screen.getByTestId("view-audition-material-btn")).toBeTruthy());
    });

    it("does not render a broken/empty material section when the project has no material", async () => {
        window.localStorage.setItem(`tg_atk_${SLUG}`, "test-atk-token");
        api.get = mockApiGet(PROJECT_WITHOUT_MATERIAL, SUBMITTED_SUBMISSION);

        renderSubmissionPage();

        await waitFor(() => expect(screen.getByTestId("submission-thank-you")).toBeTruthy());
        const detailsToggle = screen.getByTestId("thank-you-view-project-details-btn");
        detailsToggle.click();
        await waitFor(() => expect(screen.getByTestId("thank-you-view-project-details-btn")).toBeTruthy());
        expect(screen.queryByTestId("view-audition-material-btn")).toBeNull();
    });
});
