import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const renders = vi.hoisted(() => ({ video: 0 }));

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
    getSubdomainUrl: (s) => `https://${s}.talentgramagency.com`,
}));
// <Video> is rendered exactly once per TalentImages section render — a cheap probe for "which sections re-rendered".
vi.mock("lucide-react", async (orig) => {
    const actual = await orig();
    return { ...actual, Video: () => { renders.video += 1; return null; } };
});

import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import RosterBuilder from "./RosterBuilder";
import { OPTIONS_CHUNK } from "@/components/roster/useRosterMedia";

afterEach(cleanup);

const REGISTRY = (limits) => ({
    groups: [{ key: "basic", label: "Basic information", fields: [{ key: "name", label: "Name", default: true }] }],
    defaults: { name: true },
    limits: { max_talents: 100, max_images_per_talent: 12, max_total_images: 1000, ...limits },
});

const mkTalents = (n, prefix = "t") => Array.from({ length: n }, (_, i) => ({
    id: `${prefix}${i + 1}`, name: `Talent ${prefix}${i + 1}`, age: 20 + i, height: "5'7\"", cover_thumbnail_url: `https://x/${prefix}${i + 1}.jpg`,
}));

const optionFor = (id, n = 4) => ({
    talent_id: id, name: `Talent ${id}`, short_name: `T ${id}`, has_video: false,
    default_media_ids: [`${id}-0`, `${id}-1`],
    groups: [
        { key: "indian", label: "Indian Look Images", items: Array.from({ length: n }, (_, k) => ({ id: `${id}-${k}`, url: `https://x/full-${id}-${k}.jpg`, thumb_url: `https://x/thumb-${id}-${k}.jpg` })) },
        { key: "western", label: "Western Look Images", items: [] },
        { key: "portfolio", label: "Additional Portfolio", items: [] },
    ],
});

// A server stand-in: `all` talents, a gender filter, q search, and server-side paging.
let ALL;
function mockServer({ registry, optionsDelay = 0, failMedia = false } = {}) {
    adminApi.get.mockImplementation((url, cfg = {}) => {
        if (url === "/roster/fields") return Promise.resolve({ data: registry || REGISTRY() });
        if (url === "/tags") return Promise.resolve({ data: { tags: [] } });
        if (url === "/talents/facets") return Promise.resolve({ data: { locations: [] } });
        if (url === "/talents") {
            const p = cfg.params || {};
            let rows = ALL;
            if (p.q) rows = rows.filter((t) => t.name.toLowerCase().includes(String(p.q).toLowerCase()));
            if (p.gender) rows = rows.filter((t) => t.gender === p.gender);
            const size = p.size || 24;
            const page = p.page || 0;
            const data = rows.slice(page * size, page * size + size);
            return Promise.resolve({ data: { data, items: data, total: rows.length, pages: Math.ceil(rows.length / size), has_more: (page + 1) * size < rows.length } });
        }
        return Promise.resolve({ data: {} });
    });
    adminApi.post.mockImplementation((url, body) => {
        if (url === "/roster/media-options") {
            if (failMedia) return Promise.reject(new Error("boom"));
            const res = { data: { talents: body.talent_ids.map((id) => optionFor(id)) } };
            return optionsDelay ? new Promise((r) => setTimeout(() => r(res), optionsDelay)) : Promise.resolve(res);
        }
        if (url === "/links") return Promise.resolve({ data: { id: "L1", slug: "s", title: "T", talent_ids: body.talent_ids } });
        return Promise.resolve({ data: {} });
    });
}

beforeEach(() => {
    adminApi.get.mockReset(); adminApi.post.mockReset(); adminApi.put.mockReset();
    Object.values(toast).forEach((f) => f.mockClear?.());
    renders.video = 0;
    ALL = mkTalents(60);
    ALL.forEach((t, i) => { t.gender = i % 2 ? "female" : "male"; });
});

const renderBuilder = () => render(<MemoryRouter><RosterBuilder /></MemoryRouter>);
async function toTalents() {
    renderBuilder();
    await screen.findByTestId("roster-fields-count").catch(() => null);
    fireEvent.click(screen.getByTestId("roster-next"));
    await screen.findByTestId("roster-talent-t1");
}
const count = () => Number(screen.getByTestId("roster-selected-count").textContent);
const talentsCalls = () => adminApi.get.mock.calls.filter((c) => c[0] === "/talents").map((c) => c[1].params);

describe("Talents step — filters, search and selection", () => {
    it("sends the Global Talent filter params and server-side paging, 24 per page", async () => {
        mockServer();
        await toTalents();
        expect(talentsCalls().at(-1)).toMatchObject({ page: 0, size: 24 });
        expect(screen.getByTestId("roster-result-count").textContent).toContain("60 talents");
        fireEvent.click(screen.getByTestId("roster-filter-toggle"));
        const panel = within(screen.getByTestId("roster-filter-panel"));
        fireEvent.change(panel.getAllByRole("combobox")[0], { target: { value: "female" } }); // Gender
        await waitFor(() => expect(talentsCalls().at(-1)).toMatchObject({ gender: "female", page: 0 }));
        await waitFor(() => expect(screen.getByTestId("roster-result-count").textContent).toContain("30 matching talents"));
        // only a page is ever requested — never the whole directory
        expect(talentsCalls().every((p) => p.size <= 24)).toBe(true);
        // clearing the filters brings the full list back (and would keep any selection)
        fireEvent.click(screen.getByTestId("roster-select-page"));
        fireEvent.click(screen.getByText(/clear all/i));
        await waitFor(() => expect(screen.getByTestId("roster-result-count").textContent).toContain("60 talents"));
        expect(count()).toBe(24);
    });

    it("keeps the selection while searching and filtering, and after clearing the filters", async () => {
        mockServer();
        await toTalents();
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        fireEvent.click(screen.getByTestId("roster-talent-t2"));
        expect(count()).toBe(2);
        fireEvent.change(screen.getByTestId("roster-search"), { target: { value: "t45" } });
        await waitFor(() => expect(screen.queryByTestId("roster-talent-t1")).toBeNull());
        expect(screen.getByTestId("roster-talent-t45")).toBeTruthy();
        expect(count()).toBe(2);                                              // hidden talents stay selected
        expect(screen.getByTestId("roster-selected-t1")).toBeTruthy();
        fireEvent.click(screen.getByTestId("roster-talent-t45"));
        expect(count()).toBe(3);
        fireEvent.change(screen.getByTestId("roster-search"), { target: { value: "" } });
        await screen.findByTestId("roster-talent-t1");
        expect(count()).toBe(3);
        expect(screen.getByTestId("roster-talent-t1").getAttribute("aria-pressed")).toBe("true");
    });

    it("Select this page adds only the visible page; Deselect this page removes only it", async () => {
        mockServer();
        await toTalents();
        fireEvent.click(screen.getByTestId("roster-select-page"));
        expect(count()).toBe(24);
        fireEvent.click(screen.getByTestId("roster-deselect-page"));
        expect(count()).toBe(0);
    });

    it("Select all matching adds every match (not just the page) in server order; never hidden extras", async () => {
        mockServer();
        await toTalents();
        fireEvent.change(screen.getByTestId("roster-search"), { target: { value: "Talent t1" } }); // t1, t10..t19
        await waitFor(() => expect(screen.getByTestId("roster-result-count").textContent).toContain("11 matching talents"));
        fireEvent.click(screen.getByTestId("roster-select-all-matching"));
        await waitFor(() => expect(count()).toBe(11));
        const ids = screen.getAllByTestId(/^roster-selected-t\d+$/).map((e) => e.getAttribute("data-testid").replace("roster-selected-", ""));
        expect(ids).toEqual(["t1", ...Array.from({ length: 10 }, (_, i) => `t${10 + i}`)]);
        // the all-matching request used the same search, asked for a single big page
        expect(talentsCalls().some((p) => p.q === "Talent t1" && p.size === 200 && p.page === 0)).toBe(true);
    });

    it("is atomic at the talent limit: nothing is half-added, and the reason is shown", async () => {
        mockServer({ registry: REGISTRY({ max_talents: 10 }) });
        await toTalents();
        await waitFor(() => expect(screen.getByTestId("roster-selected-of").textContent).toContain("of 10"));
        fireEvent.click(screen.getByTestId("roster-select-page"));              // 24 > 10
        expect(count()).toBe(0);
        expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("nothing was added"));
        expect(screen.getByTestId("roster-select-all-matching").disabled).toBe(true); // 60 matches > 10
        for (let i = 1; i <= 10; i += 1) fireEvent.click(screen.getByTestId(`roster-talent-t${i}`));
        expect(count()).toBe(10);
        fireEvent.click(screen.getByTestId("roster-talent-t11"));               // over the limit
        expect(count()).toBe(10);
        expect(screen.getByTestId("roster-talent-t11").getAttribute("aria-pressed")).toBe("false");
    });

    it("Clear selection asks first, then removes everyone; removing one keeps the order of the rest", async () => {
        mockServer();
        await toTalents();
        ["t1", "t2", "t3"].forEach((id) => fireEvent.click(screen.getByTestId(`roster-talent-${id}`)));
        fireEvent.click(screen.getByLabelText("Remove Talent t2"));
        expect(screen.getAllByTestId(/^roster-selected-t\d+$/).map((e) => e.getAttribute("data-testid"))).toEqual(["roster-selected-t1", "roster-selected-t3"]);
        fireEvent.click(screen.getByTestId("roster-clear-selection"));
        expect(count()).toBe(2);                                                // not yet
        fireEvent.click(screen.getByTestId("roster-clear-confirm"));
        expect(count()).toBe(0);
    });

    it("keeps selection AND the search/filters when you go to Images and back", async () => {
        mockServer();
        await toTalents();
        fireEvent.change(screen.getByTestId("roster-search"), { target: { value: "t5" } });
        await waitFor(() => expect(screen.queryByTestId("roster-talent-t1")).toBeNull());
        fireEvent.click(screen.getByTestId("roster-talent-t5"));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t5");
        fireEvent.click(screen.getByTestId("roster-back"));
        await screen.findByTestId("roster-talent-t5");
        expect(screen.getByTestId("roster-search").value).toBe("t5");
        expect(count()).toBe(1);
    });
});

describe("Images step — state, caching and rendering cost", () => {
    async function toImages(ids) {
        await toTalents();
        ids.forEach((id) => fireEvent.click(screen.getByTestId(`roster-talent-${id}`)));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId(`roster-images-${ids[0]}`);
    }

    it("uses the small thumbnail in the grid and the order strip, never the full-size url", async () => {
        mockServer();
        await toImages(["t1"]);
        const tile = screen.getByTestId("roster-img-t1-0");
        expect(tile.querySelector("img").getAttribute("src")).toBe("https://x/thumb-t1-0.jpg");
        expect(tile.querySelector("img").getAttribute("loading")).toBe("lazy");
        const srcs = [...screen.getByTestId("roster-images-t1").querySelectorAll("img")].map((i) => i.getAttribute("src"));
        expect(srcs.every((s) => s.includes("/thumb-"))).toBe(true);
    });

    it("toggling one image re-renders only that talent's section, and only changes that talent", async () => {
        mockServer();
        const ids = Array.from({ length: 12 }, (_, i) => `t${i + 1}`);
        await toImages(ids);
        await waitFor(() => expect(screen.getAllByTestId(/^roster-count-/).length).toBe(12));
        const before = renders.video;
        const others = ids.slice(1).map((id) => screen.getByTestId(`roster-count-${id}`).textContent);
        fireEvent.click(screen.getByTestId("roster-img-t1-3"));
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("3 of 12");
        expect(renders.video - before).toBe(1);                                // ONE section, not 12
        expect(ids.slice(1).map((id) => screen.getByTestId(`roster-count-${id}`).textContent)).toEqual(others);
        fireEvent.click(screen.getByTestId("roster-img-t7-2"));
        expect(renders.video - before).toBe(2);
        expect(screen.getByTestId("roster-count-t7").textContent).toContain("3 of 12");
        expect(screen.getByTestId("roster-image-total").textContent).toContain("26 of 1000");
    });

    it("loads media options once per talent, in chunks, and never again on revisit or selection changes", async () => {
        mockServer();
        const ids = Array.from({ length: OPTIONS_CHUNK + 5 }, (_, i) => `t${i + 1}`);
        await toTalents();
        fireEvent.click(screen.getByTestId("roster-select-page")); // t1..t24
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t1");
        const calls = () => adminApi.post.mock.calls.filter((c) => c[0] === "/roster/media-options");
        await waitFor(() => expect(calls().length).toBe(2));
        expect(calls().map((c) => c[1].talent_ids.length).sort((a, b) => a - b)).toEqual([4, 20]);
        expect(calls().flatMap((c) => c[1].talent_ids).length).toBe(24);
        expect(ids.length).toBe(25);
        fireEvent.click(screen.getByTestId("roster-back"));
        await screen.findByTestId("roster-talent-t1");
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t1");
        expect(calls().length).toBe(2);                                         // cached
        // one more talent selected -> only that one is fetched
        fireEvent.click(screen.getByTestId("roster-back"));
        await screen.findByTestId("roster-talent-t1");
        fireEvent.click(screen.getByLabelText("Remove Talent t3"));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t1");
        expect(calls().length).toBe(2);
        expect(screen.queryByTestId("roster-images-t3")).toBeNull();
        expect(screen.getByTestId("roster-summary").textContent).toContain("23 talents");
    });

    it("a removed-then-re-added talent gets its suggested images back without a refetch", async () => {
        mockServer();
        await toTalents();
        ["t1", "t2"].forEach((id) => fireEvent.click(screen.getByTestId(`roster-talent-${id}`)));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t2");
        fireEvent.click(screen.getByTestId("roster-back"));
        await screen.findByTestId("roster-talent-t1");
        fireEvent.click(screen.getByLabelText("Remove Talent t2"));
        fireEvent.click(screen.getByTestId("roster-talent-t2"));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t2");
        expect(screen.getByTestId("roster-count-t2").textContent).toContain("2 of 12");
        expect(adminApi.post.mock.calls.filter((c) => c[0] === "/roster/media-options").length).toBe(1);
    });

    it("a failed image load never saves silently: save stays disabled, a retry is offered and works", async () => {
        mockServer({ failMedia: true });
        await toTalents();
        fireEvent.click(screen.getByTestId("roster-talent-t1"));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-failed");
        expect(screen.getByTestId("roster-save").disabled).toBe(true);
        mockServer();
        fireEvent.click(screen.getByTestId("roster-images-retry"));
        await screen.findByTestId("roster-images-t1");
        await waitFor(() => expect(screen.getByTestId("roster-save").disabled).toBe(false));
        expect(screen.queryByTestId("roster-images-failed")).toBeNull();
    });

    it("a broken thumbnail shows a placeholder but the image can still be selected", async () => {
        mockServer();
        await toImages(["t1"]);
        const tile = screen.getByTestId("roster-img-t1-3");
        fireEvent.error(tile.querySelector("img"));
        expect(within(tile).getByTestId("roster-img-unavailable")).toBeTruthy();
        fireEvent.click(tile);
        expect(tile.getAttribute("aria-pressed")).toBe("true");
    });

    it("the roster-wide image limit is enforced and explained", async () => {
        mockServer({ registry: REGISTRY({ max_total_images: 3 }) });
        await toImages(["t1"]);                                                  // suggested: 2
        fireEvent.click(screen.getByTestId("roster-img-t1-2"));                 // 3 = at the limit
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("3 of 12");
        fireEvent.click(screen.getByTestId("roster-img-t1-3"));                 // would be 4
        expect(screen.getByTestId("roster-count-t1").textContent).toContain("3 of 12");
        expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("at most 3 images"));
    });

    it("saves exactly the selected talents and images in order, with 100 talents", async () => {
        ALL = mkTalents(100);
        mockServer();
        await toTalents();
        await waitFor(() => expect(screen.getByTestId("roster-result-count").textContent).toContain("100 talents"));
        fireEvent.click(screen.getByTestId("roster-select-all-matching"));
        await waitFor(() => expect(count()).toBe(100));
        fireEvent.click(screen.getByTestId("roster-next"));
        await screen.findByTestId("roster-images-t100", {}, { timeout: 4000 });
        await waitFor(() => expect(screen.getByTestId("roster-save").disabled).toBe(false));
        fireEvent.click(screen.getByTestId("roster-img-t100-3"));
        await act(async () => { fireEvent.click(screen.getByTestId("roster-save")); });
        await screen.findByTestId("roster-created");
        const [, payload] = adminApi.post.mock.calls.find((c) => c[0] === "/links");
        expect(payload.talent_ids).toHaveLength(100);
        expect(payload.roster.talents.map((t) => t.talent_id)).toEqual(ALL.map((t) => t.id));
        expect(payload.roster.talents[99].media_ids).toEqual(["t100-0", "t100-1", "t100-3"]);
        expect(payload.roster.talents[0].media_ids).toEqual(["t1-0", "t1-1"]);
        expect(payload.roster.allow_pdf_download).toBe(true);
    });
});
