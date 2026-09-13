import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
    toast: Object.assign((...args) => {}, { error: (...a) => toastError(...a), success: (...a) => toastSuccess(...a) }),
}));

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
    isAdmin: () => true,
    getSubdomainUrl: (sub) => `https://${sub}.talentgramagency.com`,
}));

import { adminApi } from "@/lib/api";
import LinkHistory from "./LinkHistory";

afterEach(cleanup);

// Full LinkIn-shaped row, exactly what GET /links returns for one document —
// the toggle must reuse every one of these fields on PUT, never a partial
// payload, since the backend route replaces the whole stored document.
const LINK = {
    id: "link1",
    slug: "talentgram-x-color-capital-abc123",
    title: "Talentgram x Color Capital",
    brand_name: "Color Capital",
    talent_ids: ["t1", "t2"],
    submission_ids: [],
    visibility: { portfolio: true, takes: true },
    talent_field_visibility: {},
    auto_pull: false,
    auto_project_id: null,
    is_public: true,
    password: null,
    notes: "some notes",
    client_budget_override: null,
    view_count: 5,
    unique_viewers: 3,
};

// Resets happen here, once per test, BEFORE each test configures its own
// adminApi.put behavior — renderPage() itself must never reset `put` again,
// since every test sets up its mock response before calling renderPage().
beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.put.mockReset();
    toastError.mockReset();
    toastSuccess.mockReset();
});

function renderPage(links = [LINK]) {
    adminApi.get.mockResolvedValue({ data: links });
    return render(
        <MemoryRouter>
            <LinkHistory />
        </MemoryRouter>,
    );
}

describe("LinkHistory — Generated Links list public-link toggle", () => {
    it("public -> private: flips immediately and PUTs is_public:false while preserving every other existing field", async () => {
        adminApi.put.mockResolvedValue({ data: { ...LINK, is_public: false } });
        renderPage();
        const toggle = await screen.findByTestId("link-public-toggle-link1");
        expect(toggle.getAttribute("aria-pressed")).toBe("true");

        fireEvent.click(toggle);

        expect(toggle.getAttribute("aria-pressed")).toBe("false"); // optimistic, before the request resolves
        await waitFor(() => expect(adminApi.put).toHaveBeenCalledTimes(1));
        const [url, payload] = adminApi.put.mock.calls[0];
        expect(url).toBe("/links/link1");
        expect(payload).toEqual({
            title: LINK.title,
            brand_name: LINK.brand_name,
            talent_ids: LINK.talent_ids,
            submission_ids: LINK.submission_ids,
            visibility: LINK.visibility,
            talent_field_visibility: LINK.talent_field_visibility,
            auto_pull: LINK.auto_pull,
            auto_project_id: LINK.auto_project_id,
            is_public: false,
            password: LINK.password,
            notes: LINK.notes,
            client_budget_override: LINK.client_budget_override,
        });
        await waitFor(() => expect(toggle.getAttribute("aria-pressed")).toBe("false"));
    });

    it("private -> public: flips and PUTs is_public:true", async () => {
        adminApi.put.mockResolvedValue({ data: { ...LINK, is_public: true } });
        renderPage([{ ...LINK, is_public: false }]);
        const toggle = await screen.findByTestId("link-public-toggle-link1");
        expect(toggle.getAttribute("aria-pressed")).toBe("false");

        fireEvent.click(toggle);

        await waitFor(() => expect(adminApi.put).toHaveBeenCalledTimes(1));
        expect(adminApi.put.mock.calls[0][1].is_public).toBe(true);
        await waitFor(() => expect(toggle.getAttribute("aria-pressed")).toBe("true"));
    });

    it("API failure: reverts the toggle to its previous state and shows an error toast — never leaves an unsaved state displayed", async () => {
        adminApi.put.mockRejectedValue({ response: { data: { detail: "Server exploded" } } });
        renderPage();
        const toggle = await screen.findByTestId("link-public-toggle-link1");
        expect(toggle.getAttribute("aria-pressed")).toBe("true");

        fireEvent.click(toggle);
        expect(toggle.getAttribute("aria-pressed")).toBe("false"); // optimistic flip

        await waitFor(() => expect(toggle.getAttribute("aria-pressed")).toBe("true")); // reverted
        expect(toastError).toHaveBeenCalledWith("Server exploded");
    });

    it("prevents a duplicate request while an update is already in flight", async () => {
        let resolvePut;
        adminApi.put.mockReturnValue(new Promise((resolve) => { resolvePut = resolve; }));
        renderPage();
        const toggle = await screen.findByTestId("link-public-toggle-link1");

        fireEvent.click(toggle);
        fireEvent.click(toggle); // fired again before the first request resolves
        fireEvent.click(toggle);

        expect(adminApi.put).toHaveBeenCalledTimes(1);
        expect(toggle.disabled).toBe(true);

        resolvePut({ data: { ...LINK, is_public: false } });
        await waitFor(() => expect(toggle.disabled).toBe(false));
    });

    it("both the desktop and mobile toggle instances render with the same synced state (one row, two responsive layouts)", async () => {
        renderPage();
        const desktopToggle = await screen.findByTestId("link-public-toggle-link1");
        const mobileToggle = await screen.findByTestId("link-public-toggle-mobile-link1");
        expect(desktopToggle.getAttribute("aria-pressed")).toBe("true");
        expect(mobileToggle.getAttribute("aria-pressed")).toBe("true");
    });
});
