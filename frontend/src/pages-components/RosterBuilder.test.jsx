import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), success: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
    getSubdomainUrl: (s) => `https://${s}.talentgramagency.com`,
}));

import { adminApi } from "@/lib/api";
import RosterBuilder from "./RosterBuilder";

afterEach(cleanup);

const TALENTS = [
    { id: "t1", name: "Angela Kumar", age: 25, height: "5'7\"", cover_thumbnail_url: "https://x/a.jpg" },
    { id: "t2", name: "Jasmine Singh", age: 27, height: "5'5\"", cover_thumbnail_url: "https://x/j.jpg" },
];
const OPTIONS = {
    talents: [
        { talent_id: "t1", name: "Angela Kumar", short_name: "Angela K", has_video: true, default_media_ids: ["i1", "w1"],
          groups: [
              { key: "indian", label: "Indian Look Images", items: [{ id: "i1", url: "https://x/i1.jpg" }, { id: "i2", url: "https://x/i2.jpg" }] },
              { key: "western", label: "Western Look Images", items: [{ id: "w1", url: "https://x/w1.jpg" }] },
              { key: "portfolio", label: "Additional Portfolio", items: [] },
          ] },
        { talent_id: "t2", name: "Jasmine Singh", short_name: "Jasmine S", has_video: false, default_media_ids: ["j1"],
          groups: [
              { key: "indian", label: "Indian Look Images", items: [{ id: "j1", url: "https://x/j1.jpg" }] },
              { key: "western", label: "Western Look Images", items: [] },
              { key: "portfolio", label: "Additional Portfolio", items: [] },
          ] },
    ],
};

const REGISTRY = {
    groups: [
        { key: "basic", label: "Basic information", fields: [
            { key: "name", label: "Name", default: true }, { key: "age", label: "Age", default: true },
            { key: "ethnicity", label: "Ethnicity", default: false }] },
        { key: "social", label: "Social", fields: [
            { key: "instagram", label: "Instagram", default: true },
            { key: "instagram_followers", label: "Instagram Followers", default: false }] },
        { key: "media", label: "Media", fields: [{ key: "intro_video", label: "Introduction Video", default: true }] },
    ],
    defaults: { name: true, age: true, ethnicity: false, instagram: true, instagram_followers: false, intro_video: true },
};

beforeEach(() => {
    adminApi.get.mockReset(); adminApi.post.mockReset(); adminApi.put.mockReset();
    adminApi.get.mockImplementation((url) => {
        if (url === "/roster/fields") return Promise.resolve({ data: REGISTRY });
        return Promise.resolve({ data: { items: TALENTS, has_more: false } });
    });
    adminApi.post.mockImplementation((url) => {
        if (url === "/roster/media-options") return Promise.resolve({ data: OPTIONS });
        if (url === "/links") return Promise.resolve({ data: { id: "L1", slug: "talentgram-x-pepsi-abc", title: "Talentgram X Pepsi", talent_ids: ["t2", "t1"] } });
        return Promise.resolve({ data: {} });
    });
});

const renderBuilder = () => render(<MemoryRouter><RosterBuilder /></MemoryRouter>);

describe("RosterBuilder", () => {
    it("walks details -> talents -> images and saves a roster with the chosen order and images", async () => {
        renderBuilder();
        // Step 1: name required
        const next = screen.getByTestId("roster-next");
        fireEvent.change(screen.getByTestId("roster-title"), { target: { value: "" } });
        expect(next.disabled).toBe(true);
        fireEvent.change(screen.getByTestId("roster-title"), { target: { value: "Talentgram X Pepsi" } });
        fireEvent.change(screen.getByTestId("roster-subtitle"), { target: { value: "Selected Talent" } });
        expect(next.disabled).toBe(false);
        fireEvent.click(next);

        // Step 2: pick two talents, reorder, count shown
        await screen.findByTestId("roster-talent-t1");
        expect(screen.getByTestId("roster-next").disabled).toBe(true); // none selected yet
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        fireEvent.click(screen.getByTestId("roster-talent-t2"));
        expect(screen.getByTestId("roster-selected-count").textContent).toContain("2");
        // roster display name is shown to the admin as "First L"
        expect(within(screen.getByTestId("roster-selected-t1")).getByText("as Angela K")).toBeTruthy();
        fireEvent.click(screen.getByLabelText("Move Jasmine Singh up"));
        const order = screen.getAllByTestId(/^roster-selected-t\d$/).map((e) => e.getAttribute("data-testid"));
        expect(order).toEqual(["roster-selected-t2", "roster-selected-t1"]);
        // deselect + reselect via the grid
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        expect(screen.getByTestId("roster-selected-count").textContent).toContain("1");
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        expect(screen.getByTestId("roster-selected-count").textContent).toContain("2");
        fireEvent.click(screen.getByTestId("roster-next"));

        // Step 3: suggested images preselected; toggle one more; hero reorder; save
        await screen.findByTestId("roster-images-t1");
        expect(adminApi.post).toHaveBeenCalledWith("/roster/media-options", { talent_ids: expect.any(Array) });
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("2 of 12");
        fireEvent.click(screen.getByTestId("roster-img-i2"));
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("3 of 12");
        fireEvent.click(screen.getByTestId("roster-img-i1")); // deselect
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("2 of 12");
        expect(screen.getByText("Introduction video included")).toBeTruthy();
        expect(screen.getByText("No introduction video")).toBeTruthy();
        fireEvent.click(screen.getByTestId("roster-save"));

        await screen.findByTestId("roster-created");
        const [, payload] = adminApi.post.mock.calls.find((c) => c[0] === "/links");
        expect(payload.link_type).toBe("roster");
        expect(payload.title).toBe("Talentgram X Pepsi");
        expect(payload.roster.subtitle).toBe("Selected Talent");
        expect(payload.talent_ids).toEqual(["t2", "t1"]);
        expect(payload.roster.talents).toEqual([
            { talent_id: "t2", media_ids: ["j1"] },
            { talent_id: "t1", media_ids: ["w1", "i2"] },
        ]);
        expect(screen.getByText("Open Roster")).toBeTruthy();
        expect(screen.getByText("Copy Link")).toBeTruthy();
        expect(screen.getByText("Download PDF")).toBeTruthy();
    });

    it("select all / deselect all / suggested per talent", async () => {
        renderBuilder();
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-talent-t1");
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t1");
        const card = within(screen.getByTestId("roster-images-t1"));
        fireEvent.click(card.getByText("Select all"));
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("3 of 12");
        fireEvent.click(card.getByText("Deselect all"));
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("0 of 12");
        expect(screen.getByTestId("roster-save").disabled).toBe(true); // nothing selected anywhere
        fireEvent.click(card.getByText("Suggested"));
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("2 of 12");
    });

    it("shows the grouped field panel with the standard defaults and sends the chosen fields", async () => {
        renderBuilder();
        await screen.findByTestId("roster-fields");
        const on = (k) => screen.getByTestId(`roster-field-${k}`).getAttribute("aria-checked");
        // defaults: Name, Age, Instagram, Introduction Video ON; the rest OFF
        expect(["name", "age", "instagram", "intro_video"].map(on)).toEqual(["true", "true", "true", "true"]);
        expect(["ethnicity", "instagram_followers"].map(on)).toEqual(["false", "false"]);
        expect(screen.getByTestId("roster-fields-count").textContent).toContain("4 selected");
        // group headings are shown
        expect(screen.getByText("Basic information")).toBeTruthy();
        expect(screen.getByText("Social")).toBeTruthy();
        // toggle Instagram OFF and Followers ON
        fireEvent.click(screen.getByTestId("roster-field-instagram"));
        fireEvent.click(screen.getByTestId("roster-field-instagram_followers"));
        expect(on("instagram")).toBe("false");
        expect(on("instagram_followers")).toBe("true");
        // go through the flow and save
        fireEvent.change(screen.getByTestId("roster-title"), { target: { value: "Talentgram X BKB" } });
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-talent-t1");
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t1");
        fireEvent.click(screen.getByTestId("roster-save"));
        await screen.findByTestId("roster-created");
        const [, payload] = adminApi.post.mock.calls.find((c) => c[0] === "/links");
        expect(payload.roster.fields).toEqual({ name: true, age: true, ethnicity: false, instagram: false, instagram_followers: true, intro_video: true });
    });

    it("edit loads the roster's SAVED field configuration (and old rosters fall back to the defaults)", async () => {
        const saved = { name: true, age: false, ethnicity: true, instagram: false, instagram_followers: false, intro_video: false };
        adminApi.get.mockImplementation((url) => {
            if (url === "/roster/fields") return Promise.resolve({ data: REGISTRY });
            if (url === "/links/L9") return Promise.resolve({ data: { id: "L9", title: "Existing", slug: "s", roster: { subtitle: null, fields: saved, talents: [{ talent_id: "t1", media_ids: ["i1"] }] } } });
            return Promise.resolve({ data: { items: TALENTS, has_more: false } });
        });
        adminApi.post.mockImplementation((url) => (url === "/talents/bulk" ? Promise.resolve({ data: TALENTS }) : Promise.resolve({ data: OPTIONS })));
        render(<MemoryRouter><RosterBuilder editId="L9" /></MemoryRouter>);
        await screen.findByTestId("roster-fields");
        await waitFor(() => expect(screen.getByTestId("roster-field-ethnicity").getAttribute("aria-checked")).toBe("true"));
        expect(screen.getByTestId("roster-field-age").getAttribute("aria-checked")).toBe("false");
        expect(screen.getByTestId("roster-field-intro_video").getAttribute("aria-checked")).toBe("false");
        cleanup();

        // a roster created before this feature has no stored `fields` -> standard defaults
        adminApi.get.mockImplementation((url) => {
            if (url === "/roster/fields") return Promise.resolve({ data: REGISTRY });
            if (url === "/links/L8") return Promise.resolve({ data: { id: "L8", title: "Old", slug: "o", roster: { subtitle: null, talents: [{ talent_id: "t1", media_ids: ["i1"] }] } } });
            return Promise.resolve({ data: { items: TALENTS, has_more: false } });
        });
        render(<MemoryRouter><RosterBuilder editId="L8" /></MemoryRouter>);
        await screen.findByTestId("roster-fields");
        await waitFor(() => expect(screen.getByTestId("roster-field-name").getAttribute("aria-checked")).toBe("true"));
        expect(screen.getByTestId("roster-field-age").getAttribute("aria-checked")).toBe("true");
        expect(screen.getByTestId("roster-field-ethnicity").getAttribute("aria-checked")).toBe("false");
    });
});
