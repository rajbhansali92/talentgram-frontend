import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import TalentEdit from "./TalentEdit";

// Multi-select fix (2026-09-18): the "Internal Tags" dropdown on the
// individual talent profile used to close itself (setIsTagDropdownOpen
// (false)) immediately after adding an EXISTING tag, forcing the user to
// re-click the search input before adding a second one. Persistence was
// already immediate per tag and stays that way — only the premature
// close is removed.

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
    isAdmin: () => true,
    IMAGE_URL: "",
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/lib/talentPreviewCache", () => ({ talentPreviewCache: { invalidateTalent: vi.fn() } }));

// Heavy/unrelated child components — stubbed so this test stays focused
// on the Internal Tags interaction and doesn't depend on their own
// network calls or media rendering.
vi.mock("@/components/CommTimeline", () => ({ default: () => null }));
vi.mock("@/components/WorkLinksDisplay", () => ({ default: () => null }));
vi.mock("@/components/SkillsSelector", () => ({ default: () => null }));
vi.mock("@/components/LocationSelector", () => ({ default: () => null }));
vi.mock("@/components/DobInput", () => ({ default: () => null }));
vi.mock("@/components/HlsVideo", () => ({ default: () => null }));
vi.mock("@/components/ConfirmDeleteDialog", () => ({ default: () => null }));

import { adminApi } from "@/lib/api";

afterEach(cleanup);

const ALL_TAGS = [
    { id: "t1", name: "actor 1" },
    { id: "t2", name: "actor 2" },
    { id: "t3", name: "actor 3" },
];

const BASE_TALENT = {
    id: "talent-1", name: "Dhanashree Parikh", email: "d@example.com", phone: "",
    alternate_contact_number: "", age: "", dob: "", height: "", location: [],
    ethnicity: "", gender: "", instagram_handle: "", instagram_followers: "",
    bio: "", work_links: [], interested_in: [], tags: [], skills: [],
    whatsapp_group_name: "", media: [],
};

async function renderEditingProfile(talentOverrides = {}) {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    adminApi.get.mockImplementation((url) => {
        if (url === "/talents/talent-1") return Promise.resolve({ data: { ...BASE_TALENT, ...talentOverrides } });
        if (url === "/tags") return Promise.resolve({ data: { tags: ALL_TAGS } });
        return Promise.resolve({ data: {} });
    });
    adminApi.post.mockResolvedValue({ data: {} });

    render(
        <MemoryRouter initialEntries={["/admin/talents/talent-1"]}>
            <Routes>
                <Route path="/admin/talents/:id" element={<TalentEdit />} />
            </Routes>
        </MemoryRouter>
    );

    await waitFor(() => expect(screen.getByText("Edit")).toBeTruthy());
    fireEvent.click(screen.getByText("Edit"));
    await waitFor(() => expect(screen.getByTestId("tag-input")).toBeTruthy());
}

describe("TalentEdit — Internal Tags multi-select", () => {
    it("keeps the dropdown open after adding an existing tag, so a second can be added immediately", async () => {
        await renderEditingProfile();

        fireEvent.focus(screen.getByTestId("tag-input"));
        await waitFor(() => expect(screen.getByTestId("profile-tag-option-t1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("profile-tag-option-t1"));

        // The dropdown must still be open — the second tag's own option
        // is immediately clickable with no re-focus/re-open step.
        await waitFor(() => expect(screen.getByTestId("profile-tag-option-t2")).toBeTruthy());
        fireEvent.click(screen.getByTestId("profile-tag-option-t2"));

        await waitFor(() => expect(adminApi.post).toHaveBeenCalledTimes(2));
        expect(adminApi.post).toHaveBeenCalledWith("/talents/talent-1/tag/t1");
        expect(adminApi.post).toHaveBeenCalledWith("/talents/talent-1/tag/t2");
    });

    it("assigned tags update correctly and are never offered again in the dropdown", async () => {
        await renderEditingProfile();
        fireEvent.focus(screen.getByTestId("tag-input"));
        await waitFor(() => expect(screen.getByTestId("profile-tag-option-t1")).toBeTruthy());

        fireEvent.click(screen.getByTestId("profile-tag-option-t1"));
        await waitFor(() => expect(screen.getByTestId("talent-tag-t1")).toBeTruthy());
        expect(screen.queryByTestId("profile-tag-option-t1")).toBeNull(); // never duplicated/re-offered
    });

    it("existing assigned tags remain intact when a new one is added", async () => {
        await renderEditingProfile({ tags: [{ id: "t3", name: "actor 3" }] });
        expect(screen.getByTestId("talent-tag-t3")).toBeTruthy();

        fireEvent.focus(screen.getByTestId("tag-input"));
        await waitFor(() => expect(screen.getByTestId("profile-tag-option-t1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("profile-tag-option-t1"));

        await waitFor(() => expect(screen.getByTestId("talent-tag-t1")).toBeTruthy());
        expect(screen.getByTestId("talent-tag-t3")).toBeTruthy(); // untouched
    });

    it("search filtering keeps working after adding one tag", async () => {
        await renderEditingProfile();
        fireEvent.focus(screen.getByTestId("tag-input"));
        await waitFor(() => expect(screen.getByTestId("profile-tag-option-t1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("profile-tag-option-t1"));

        fireEvent.change(screen.getByTestId("tag-input"), { target: { value: "actor 3" } });
        await waitFor(() => expect(screen.getByTestId("profile-tag-option-t3")).toBeTruthy());
        expect(screen.queryByTestId("profile-tag-option-t2")).toBeNull();
    });
});
