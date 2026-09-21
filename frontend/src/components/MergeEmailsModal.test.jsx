import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const toastError = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, { error: (...a) => toastError(...a), success: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import { adminApi } from "@/lib/api";
import MergeEmailsModal from "./MergeEmailsModal";

afterEach(cleanup);

const TALENT_A = {
    id: "talent-a", name: "Juhi Vyas", email: "juhivyas8@gmail.com", status: "SUBMITTED",
    relationship_counts: { submissions: 3 }, media_count: 5,
};
const TALENT_B = {
    id: "talent-b", name: "Juhi Vyas", email: "business.juhivyas@gmail.com", status: "SUBMITTED",
    relationship_counts: { submissions: 1 }, media_count: 2,
};
const PREVIEW = {
    talent_a: TALENT_A, talent_b: TALENT_B,
    recommended_canonical_id: "talent-a", recommendation_reason: "older profile",
    either_already_merged: { "talent-a": false, "talent-b": false },
};

beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    toastError.mockReset();
});

describe("MergeEmailsModal — separate action from Merge Talents", () => {
    it("loads the preview, shows both profiles' emails, and confirms the merge via /talents/merge-emails", async () => {
        adminApi.post.mockImplementation((url) => {
            if (url === "/talents/merge-emails/preview") return Promise.resolve({ data: PREVIEW });
            if (url === "/talents/merge-emails") {
                return Promise.resolve({
                    data: { ok: true, canonical_talent_id: "talent-a", submissions_preserved: 4, media_preserved: 7 },
                });
            }
            throw new Error(`unexpected url ${url}`);
        });

        const onSuccess = vi.fn();
        const onClose = vi.fn();
        render(<MergeEmailsModal open talentAId="talent-a" talentBId="talent-b" onClose={onClose} onSuccess={onSuccess} />);

        await screen.findByTestId("merge-emails-step-review");
        expect(screen.getByText("juhivyas8@gmail.com")).toBeTruthy();
        expect(screen.getByText("business.juhivyas@gmail.com")).toBeTruthy();
        // Recommended (older) profile is pre-selected as canonical.
        expect(screen.getByTestId("merge-emails-canonical-choice-talent-a").querySelector('input[type="radio"]').checked).toBe(true);

        fireEvent.click(screen.getByTestId("merge-emails-continue-to-confirm"));
        await screen.findByTestId("merge-emails-step-confirm");
        expect(screen.getByText("business.juhivyas@gmail.com")).toBeTruthy();

        fireEvent.click(screen.getByTestId("merge-emails-confirm-button"));
        await screen.findByTestId("merge-emails-step-success");
        const [, payload] = adminApi.post.mock.calls.find(([url]) => url === "/talents/merge-emails");
        expect(payload).toEqual({ canonical_talent_id: "talent-a", duplicate_talent_id: "talent-b" });
    });

    it("shows an error step and never calls the execute endpoint when the preview is rejected (e.g. same/missing emails)", async () => {
        adminApi.post.mockImplementation((url) => {
            if (url === "/talents/merge-emails/preview") {
                return Promise.reject({ response: { data: { detail: "Both profiles must have an email address to use Merge Different Emails." } } });
            }
            throw new Error(`unexpected url ${url}`);
        });

        render(<MergeEmailsModal open talentAId="talent-a" talentBId="talent-b" onClose={vi.fn()} onSuccess={vi.fn()} />);

        await screen.findByTestId("merge-emails-step-error");
        expect(screen.getByText(/Both profiles must have an email address/)).toBeTruthy();
        expect(adminApi.post).toHaveBeenCalledTimes(1);
    });

    it("lets the admin flip which profile survives before confirming", async () => {
        adminApi.post.mockImplementation((url) => {
            if (url === "/talents/merge-emails/preview") return Promise.resolve({ data: PREVIEW });
            if (url === "/talents/merge-emails") {
                return Promise.resolve({ data: { ok: true, canonical_talent_id: "talent-b", submissions_preserved: 4, media_preserved: 7 } });
            }
            throw new Error(`unexpected url ${url}`);
        });

        render(<MergeEmailsModal open talentAId="talent-a" talentBId="talent-b" onClose={vi.fn()} onSuccess={vi.fn()} />);
        await screen.findByTestId("merge-emails-step-review");

        fireEvent.click(screen.getByTestId("merge-emails-canonical-choice-talent-b").querySelector('input[type="radio"]'));
        fireEvent.click(screen.getByTestId("merge-emails-continue-to-confirm"));
        await screen.findByTestId("merge-emails-step-confirm");
        fireEvent.click(screen.getByTestId("merge-emails-confirm-button"));

        await waitFor(() => {
            const call = adminApi.post.mock.calls.find(([url]) => url === "/talents/merge-emails");
            expect(call?.[1]).toEqual({ canonical_talent_id: "talent-b", duplicate_talent_id: "talent-a" });
        });
    });
});
