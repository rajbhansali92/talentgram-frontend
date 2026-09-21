import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import TalentEdit from "./TalentEdit";

// "Merge Different Emails" leaves alternate_emails on the surviving Talent
// (backend/talent_merge_service.py execute_email_merge) but that value was
// completely invisible in the admin UI — an admin merging two profiles had
// no way to confirm the second email actually got linked. This adds the
// smallest possible read-only display, never an editable field.

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
    isAdmin: () => true,
    IMAGE_URL: "",
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/lib/talentPreviewCache", () => ({ talentPreviewCache: { invalidateTalent: vi.fn() } }));
vi.mock("@/components/CommTimeline", () => ({ default: () => null }));
vi.mock("@/components/WorkLinksDisplay", () => ({ default: () => null }));
vi.mock("@/components/SkillsSelector", () => ({ default: () => null }));
vi.mock("@/components/LocationSelector", () => ({ default: () => null }));
vi.mock("@/components/DobInput", () => ({ default: () => null }));
vi.mock("@/components/HlsVideo", () => ({ default: () => null }));
vi.mock("@/components/ConfirmDeleteDialog", () => ({ default: () => null }));

import { adminApi } from "@/lib/api";

afterEach(cleanup);

const BASE_TALENT = {
    id: "talent-1", name: "Juhi Vyas", email: "business.juhivyas@gmail.com", phone: "",
    alternate_contact_number: "", age: "", dob: "", height: "", location: [],
    ethnicity: "", gender: "", instagram_handle: "", instagram_followers: "",
    bio: "", work_links: [], interested_in: [], tags: [], skills: [],
    whatsapp_group_name: "", media: [],
};

async function renderProfile(talentOverrides = {}) {
    adminApi.get.mockReset();
    adminApi.get.mockImplementation((url) => {
        if (url === "/talents/talent-1") return Promise.resolve({ data: { ...BASE_TALENT, ...talentOverrides } });
        if (url === "/tags") return Promise.resolve({ data: { tags: [] } });
        return Promise.resolve({ data: {} });
    });

    render(
        <MemoryRouter initialEntries={["/admin/talents/talent-1"]}>
            <Routes>
                <Route path="/admin/talents/:id" element={<TalentEdit />} />
            </Routes>
        </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Edit")).toBeTruthy());
}

describe("TalentEdit — Alternate Emails (Merge Different Emails result)", () => {
    it("shows a read-only Alternate Emails block when the talent has one", async () => {
        await renderProfile({ alternate_emails: ["juhivyas8@gmail.com"] });
        const block = screen.getByTestId("talent-alternate-emails");
        expect(block.textContent).toContain("Alternate Emails");
        expect(block.textContent).toContain("juhivyas8@gmail.com");
        // Never rendered as an <input> — this must not be editable.
        expect(block.querySelector("input")).toBeNull();
    });

    it("shows every linked alternate email when there is more than one", async () => {
        await renderProfile({ alternate_emails: ["a@example.com", "b@example.com"] });
        const block = screen.getByTestId("talent-alternate-emails");
        expect(block.textContent).toContain("a@example.com");
        expect(block.textContent).toContain("b@example.com");
    });

    it("renders nothing when the talent has no alternate emails (the common case)", async () => {
        await renderProfile({ alternate_emails: [] });
        expect(screen.queryByTestId("talent-alternate-emails")).toBeNull();
    });

    it("renders nothing when alternate_emails is entirely absent from the response", async () => {
        await renderProfile({});
        expect(screen.queryByTestId("talent-alternate-emails")).toBeNull();
    });
});
