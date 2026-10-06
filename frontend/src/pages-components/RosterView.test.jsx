import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, within, fireEvent, act } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useParams: () => ({ slug: "talentgram-x-bkb-abc" }) }));
const toast = vi.hoisted(() => Object.assign(() => {}, { error: vi.fn(), success: vi.fn() }));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/components/Logo", () => ({ default: (p) => <img alt="Talentgram" data-testid="brand-logo" data-size={p.size} className={p.className} /> }));
let viewerToken = "viewer-token";
vi.mock("@/lib/api", () => ({
    api: { get: vi.fn(), post: vi.fn() },
    API: "https://api.example.test/api",
    IMAGE_URL: (m) => m.url,
    getViewerToken: () => viewerToken,
    saveViewerToken: vi.fn(),
}));

import { api } from "@/lib/api";
import RosterView from "./RosterView";

/* ---- a controllable IntersectionObserver: tests decide which sections are "near" the viewport ---- */
let observers = [];
class MockIO {
    constructor(cb) { this.cb = cb; this.els = []; observers.push(this); }
    observe(el) { this.els.push(el); }
    disconnect() { this.els = []; }
    unobserve() {}
}
const setNear = (testId, isIntersecting) => act(() => {
    for (const o of observers) for (const el of o.els) if (el.getAttribute("data-testid") === testId) o.cb([{ isIntersecting, target: el }]);
});
const everythingNear = () => act(() => { for (const o of observers) for (const el of o.els) o.cb([{ isIntersecting: true, target: el }]); });

const img = (id, ratio = 0.75) => ({ id, url: `https://img.test/${id}.jpg`, category: "indian", ratio });
const INFO_FULL = [
    { key: "age", label: "Age", value: "25" },
    { key: "location", label: "Location", value: "Mumbai, India" },
    { key: "instagram", label: "Instagram", value: "@ak_official", href: "https://www.instagram.com/ak_official/" },
];
const talent = (i, over = {}) => ({
    key: `t${i}`, index: i, name: `Talent ${String.fromCharCode(64 + i)}`, info: [{ key: "age", label: "Age", value: String(20 + i) }],
    video: null, images: [img(`t${i}a`), img(`t${i}b`), img(`t${i}c`)], pages: [], ...over,
});
const PAYLOAD = (over = {}) => ({
    title: "Talentgram X BKB", subtitle: "Selected Talent — October 2026", fields: {}, page: { w: 210, h: 297, margin: 14 }, pdf_available: true,
    shortlisted: [],
    talents: [
        talent(1, { name: "Angela K", info: INFO_FULL, video: { url: "https://v.test/a.mp4" } }),
        talent(2, { name: "Jasmine S", info: [{ key: "skills", label: "Skills", value: "Actor, Model", block: true }], images: [img("j1"), img("j2"), img("j3"), img("j4"), img("j5"), img("j6")] }),
        talent(3, { name: "Dia M", info: [], images: [img("d1")] }),
    ],
    ...over,
});

let payload;
beforeEach(() => {
    observers = [];
    globalThis.IntersectionObserver = MockIO;
    payload = PAYLOAD();
    viewerToken = "viewer-token";
    api.get.mockReset(); api.post.mockReset();
    api.get.mockImplementation((url) => Promise.resolve({ data: url.endsWith("/meta") ? { link_type: "roster", title: payload.title, subtitle: payload.subtitle } : payload }));
    api.post.mockResolvedValue({ data: { ok: true } });
    toast.error.mockReset();
});
afterEach(() => { cleanup(); delete globalThis.IntersectionObserver; });

async function openRoster(over) {
    if (over) payload = PAYLOAD(over);
    render(<RosterView />);
    const sections = await screen.findByTestId("roster-sections");
    await everythingNear();
    return sections;
}
const section = (k) => screen.getByTestId(`roster-talent-${k}`);
const heroSrc = (k) => screen.getByTestId(`roster-image-${k}`).getAttribute("src");
const counter = (k) => screen.getByTestId(`roster-counter-${k}`).textContent;
const next = (k) => fireEvent.click(screen.getByTestId(`roster-next-${k}`));
const prev = (k) => fireEvent.click(screen.getByTestId(`roster-prev-${k}`));

describe("access (unchanged gate)", () => {
    it("no viewer token -> identity gate; the roster is not requested", async () => {
        viewerToken = null;
        render(<RosterView />);
        expect(await screen.findByTestId("roster-identify")).toBeTruthy();
        expect(api.get.mock.calls.some(([u]) => u.endsWith("/roster"))).toBe(false);
        expect(screen.queryByTestId("roster-sections")).toBeNull();
    });

    it("a rejected token (401) returns to the gate; a disabled link (403) shows 'no longer active'", async () => {
        api.get.mockImplementation((url) => url.endsWith("/meta") ? Promise.resolve({ data: { title: "T" } }) : Promise.reject({ response: { status: 401 } }));
        render(<RosterView />);
        expect(await screen.findByTestId("roster-identify")).toBeTruthy();
        cleanup();
        api.get.mockImplementation((url) => url.endsWith("/meta") ? Promise.resolve({ data: { title: "T" } }) : Promise.reject({ response: { status: 403 } }));
        render(<RosterView />);
        expect((await screen.findByTestId("roster-unavailable")).textContent).toContain("no longer active");
    });

    it("sends the viewer token with the roster request", async () => {
        await openRoster();
        const call = api.get.mock.calls.find(([u]) => u.endsWith("/roster"));
        expect(call[1].headers.Authorization).toBe("Bearer viewer-token");
    });
});

describe("one vertical page, one section per talent", () => {
    it("renders every talent as its own section, in order, on the same page (no deck, no talent navigation)", async () => {
        await openRoster();
        const keys = [...screen.getByTestId("roster-sections").children].map((s) => s.getAttribute("data-testid"));
        expect(keys).toEqual(["roster-talent-t1", "roster-talent-t2", "roster-talent-t3"]);
        expect(within(section("t1")).getByRole("heading", { level: 2 }).textContent).toBe("Angela K");
        expect(within(section("t2")).getByRole("heading", { level: 2 }).textContent).toBe("Jasmine S");
        expect(screen.getByTestId("roster-position-t2").textContent).toBe("02 / 03");
        // the old deck / talent-to-talent controls do not exist
        for (const id of ["roster-start", "roster-talent-next", "roster-talent-prev", "roster-deck", "roster-hero-image"]) expect(screen.queryByTestId(id)).toBeNull();
        expect(screen.queryByLabelText(/next talent|previous talent/i)).toBeNull();
    });

    it("keeps the Talentgram cover (logo, title, subtitle, count) and a footer Instagram link; the title stays in the header", async () => {
        await openRoster();
        const cover = screen.getByTestId("roster-cover");
        expect(within(cover).getByTestId("brand-logo")).toBeTruthy();
        expect(screen.getByTestId("roster-title").textContent).toBe("Talentgram X BKB");
        expect(screen.getByTestId("roster-subtitle").textContent).toContain("Selected Talent");
        expect(within(cover).getByText("3 talents")).toBeTruthy();
        expect(screen.getByTestId("roster-header-title").textContent).toBe("Talentgram X BKB");
        const ig = screen.getByTestId("roster-instagram");
        expect(ig.getAttribute("href")).toBe("https://www.instagram.com/talentgram.agency/");
        expect(ig.textContent).toContain("@talentgram.agency");
    });

    it("the page never listens for keys globally: arrow / page keys on the window change nothing", async () => {
        await openRoster();
        for (const key of ["ArrowRight", "ArrowLeft", "PageDown", "PageUp", "ArrowDown"]) fireEvent.keyDown(window, { key });
        expect(counter("t1")).toBe("01 / 03");
        expect(counter("t2")).toBe("01 / 06");
        expect(heroSrc("t1")).toBe("https://img.test/t1a.jpg");
    });

    it("the 'All talents' index scrolls the page to that talent's section; it does not replace the page", async () => {
        await openRoster({ shortlisted: ["t2"] });
        const target = section("t3");
        target.scrollIntoView = vi.fn();
        fireEvent.click(screen.getByTestId("roster-index-open"));
        expect(within(screen.getByTestId("roster-index-t2")).getByLabelText("Shortlisted")).toBeTruthy();
        expect(within(screen.getByTestId("roster-index-t1")).queryByLabelText("Shortlisted")).toBeNull();
        fireEvent.click(screen.getByTestId("roster-index-t3"));
        expect(target.scrollIntoView).toHaveBeenCalled();
        expect(screen.queryByTestId("roster-index")).toBeNull();
        expect(screen.getByTestId("roster-sections").children.length).toBe(3);   // still the whole page
        fireEvent.click(screen.getByTestId("roster-index-open"));
        fireEvent.keyDown(window, { key: "Escape" });
        expect(screen.queryByTestId("roster-index")).toBeNull();
    });
});

describe("each talent's own image carousel", () => {
    it("clicking next/previous changes ONLY that talent's image, and each talent keeps its own position", async () => {
        await openRoster();
        next("t1"); next("t1");
        expect(heroSrc("t1")).toBe("https://img.test/t1c.jpg");
        expect(counter("t1")).toBe("03 / 03");
        expect(heroSrc("t2")).toBe("https://img.test/j1.jpg");              // Talent 2 untouched
        expect(counter("t2")).toBe("01 / 06");
        next("t2"); next("t2"); next("t2");
        expect(heroSrc("t2")).toBe("https://img.test/j4.jpg");
        expect(counter("t2")).toBe("04 / 06");
        expect(heroSrc("t1")).toBe("https://img.test/t1c.jpg");              // Talent 1 still where it was
        prev("t1");
        expect(heroSrc("t1")).toBe("https://img.test/t1b.jpg");
        expect(counter("t1")).toBe("02 / 03");
        expect(counter("t2")).toBe("04 / 06");
    });

    it("the counter is right, and the arrows are disabled (subtle) at the first and last image", async () => {
        await openRoster();
        expect(counter("t1")).toBe("01 / 03");
        expect(screen.getByTestId("roster-prev-t1").disabled).toBe(true);
        expect(screen.getByTestId("roster-next-t1").disabled).toBe(false);
        next("t1"); expect(counter("t1")).toBe("02 / 03");
        expect(screen.getByTestId("roster-prev-t1").disabled).toBe(false);
        next("t1"); expect(counter("t1")).toBe("03 / 03");
        expect(screen.getByTestId("roster-next-t1").disabled).toBe(true);
        fireEvent.click(screen.getByTestId("roster-next-t1"));                 // a disabled button does nothing
        expect(counter("t1")).toBe("03 / 03");
        expect(screen.getByTestId("roster-next-t1").className).toContain("disabled:opacity-30");
        expect(screen.getByLabelText("Next image of Angela K")).toBeTruthy();  // labelled per talent
    });

    it("a one-image talent has no arrows and no counter (minimal), but the image is shown", async () => {
        await openRoster();
        expect(heroSrc("t3")).toBe("https://img.test/d1.jpg");
        expect(screen.queryByTestId("roster-prev-t3")).toBeNull();
        expect(screen.queryByTestId("roster-next-t3")).toBeNull();
        expect(screen.queryByTestId("roster-counter-t3")).toBeNull();
    });

    it("a talent with no images still shows name, details and Shortlist (no broken image)", async () => {
        await openRoster({ talents: [talent(1, { images: [] })] });
        expect(screen.queryByTestId("roster-image-t1")).toBeNull();
        expect(screen.getByTestId("roster-shortlist-t1")).toBeTruthy();
    });

    it("arrow keys control only the FOCUSED talent's carousel", async () => {
        await openRoster();
        const hero2 = screen.getByTestId("roster-hero-t2");
        expect(hero2.getAttribute("tabindex")).toBe("0");
        fireEvent.keyDown(hero2, { key: "ArrowRight" });
        fireEvent.keyDown(hero2, { key: "ArrowRight" });
        fireEvent.keyDown(hero2, { key: "ArrowLeft" });
        expect(counter("t2")).toBe("02 / 06");
        expect(counter("t1")).toBe("01 / 03");
        fireEvent.keyDown(hero2, { key: "PageDown" });                          // never moves between talents
        fireEvent.keyDown(hero2, { key: "ArrowRight", shiftKey: true });
        expect(counter("t2")).toBe("02 / 06");
        expect(screen.getAllByTestId(/^roster-talent-t\d+$/).length).toBe(3);
    });

    it("swipe left/right changes THAT talent's image; a vertical drag (scrolling) changes nothing", async () => {
        await openRoster();
        const hero = screen.getByTestId("roster-hero-t2");
        expect(hero.style.touchAction).toBe("pan-y");                            // vertical scrolling stays native
        const swipe = (x0, y0, x1, y1) => { fireEvent.touchStart(hero, { touches: [{ clientX: x0, clientY: y0 }] }); fireEvent.touchEnd(hero, { changedTouches: [{ clientX: x1, clientY: y1 }] }); };
        swipe(300, 200, 180, 205); expect(counter("t2")).toBe("02 / 06");
        swipe(100, 200, 260, 210); expect(counter("t2")).toBe("01 / 06");
        swipe(200, 600, 205, 120); expect(counter("t2")).toBe("01 / 06");        // scroll gesture
        swipe(200, 200, 210, 205); expect(counter("t2")).toBe("01 / 06");        // tap-sized move
        expect(counter("t1")).toBe("01 / 03");
    });

    it("images are never stretched or cropped: object-contain inside a fixed hero box", async () => {
        await openRoster();
        expect(screen.getByTestId("roster-image-t1").className).toContain("object-contain");
        expect(screen.getByTestId("roster-image-t1").className).not.toMatch(/object-cover/);
        expect(screen.getByTestId("roster-hero-t1").className).toContain("overflow-hidden");
    });
});

describe("information, name and video", () => {
    it("shows exactly the configured fields; Instagram is a link when enabled and absent when the roster hides it", async () => {
        await openRoster();
        const info = screen.getByTestId("roster-info-t1");
        for (const k of ["age", "location", "instagram"]) expect(within(info).getByTestId(`roster-field-t1-${k}`)).toBeTruthy();
        expect(within(screen.getByTestId("roster-field-t1-instagram")).getByRole("link").getAttribute("href")).toBe("https://www.instagram.com/ak_official/");
        expect(screen.queryByTestId("roster-field-t1-height")).toBeNull();            // not configured -> not drawn
        expect(screen.queryByTestId("roster-field-t2-instagram")).toBeNull();         // hidden Instagram stays hidden
        expect(screen.getByTestId("roster-field-t2-skills").className).toContain("col-span-2");
        expect(screen.queryByTestId("roster-info-t3")).toBeNull();                    // nothing configured -> no empty block
        expect(document.body.textContent).not.toMatch(/undefined|N\/A|null/);
    });

    it("a roster with Instagram switched off shows no Instagram field anywhere", async () => {
        await openRoster({ talents: [talent(1, { info: [{ key: "age", label: "Age", value: "25" }] }), talent(2)] });
        expect(document.querySelectorAll('[data-testid*="-instagram"]').length).toBe(1);   // only the Talentgram footer link
        expect(screen.getByTestId("roster-instagram").getAttribute("href")).toContain("talentgram.agency");
    });

    it("names are shown exactly as the server sends them (First + initial; neutral label when the name is off)", async () => {
        await openRoster({ talents: [talent(1, { name: "Angela K" }), talent(2, { name: "Talent 02" })] });
        expect(screen.getByTestId("roster-name-t1").textContent).toBe("Angela K");
        expect(screen.getByTestId("roster-name-t2").textContent).toBe("Talent 02");
        expect(document.body.textContent).not.toMatch(/Kumar/);
    });

    it("Introduction Video is shown only for talents that have one, opens the existing link, and never autoplays", async () => {
        await openRoster();
        const v = screen.getByTestId("roster-video-t1");
        expect(v.getAttribute("href")).toBe("https://v.test/a.mp4");
        expect(v.getAttribute("target")).toBe("_blank");
        expect(v.textContent).toContain("Introduction Video");
        expect(screen.queryByTestId("roster-video-t2")).toBeNull();
        expect(screen.queryByTestId("roster-video-t3")).toBeNull();
        expect(document.querySelectorAll("video, iframe").length).toBe(0);
        next("t1");                                                                   // the carousel does not touch the video
        expect(screen.getByTestId("roster-video-t1").getAttribute("href")).toBe("https://v.test/a.mp4");
    });
});

describe("shortlist — one button per talent", () => {
    it("every section has its own button; clicking one shortlists ONLY that talent and posts that talent's key", async () => {
        await openRoster();
        expect(screen.getAllByRole("button", { name: /^Shortlist$/ }).length).toBe(3);
        const b2 = screen.getByTestId("roster-shortlist-t2");
        fireEvent.click(b2);
        expect(b2.getAttribute("aria-pressed")).toBe("true");                          // immediate
        expect(b2.textContent).toContain("Shortlisted");
        expect(screen.getByTestId("roster-shortlist-t1").getAttribute("aria-pressed")).toBe("false");
        expect(screen.getByTestId("roster-shortlist-t3").getAttribute("aria-pressed")).toBe("false");
        expect(screen.getByTestId("roster-shortlist-count").textContent).toBe("1 shortlisted");
        await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
        const [url, body, cfg] = api.post.mock.calls[0];
        expect(url).toBe("/public/links/talentgram-x-bkb-abc/roster/shortlist");
        expect(body).toMatchObject({ key: "t2", shortlisted: true });
        expect(cfg.headers.Authorization).toBe("Bearer viewer-token");
        await waitFor(() => expect(b2.getAttribute("aria-busy")).toBe("false"));
        fireEvent.click(b2);                                                           // and removable
        expect(b2.getAttribute("aria-pressed")).toBe("false");
        await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
        expect(api.post.mock.calls[1][1]).toMatchObject({ key: "t2", shortlisted: false });
        expect(screen.queryByTestId("roster-shortlist-count")).toBeNull();
    });

    it("talents are independent, and the carousel position is untouched by shortlisting", async () => {
        await openRoster();
        next("t1");
        fireEvent.click(screen.getByTestId("roster-shortlist-t1"));
        fireEvent.click(screen.getByTestId("roster-shortlist-t3"));
        await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
        expect(screen.getByTestId("roster-shortlist-t2").getAttribute("aria-pressed")).toBe("false");
        expect(counter("t1")).toBe("02 / 03");
        expect(screen.getByTestId("roster-shortlist-count").textContent).toBe("2 shortlisted");
        expect(api.post.mock.calls.map((c) => c[1].key).sort()).toEqual(["t1", "t3"]);
    });

    it("persists across a reload: the server's shortlist for this viewer is what the page starts from", async () => {
        await openRoster({ shortlisted: ["t1", "t3"] });
        expect(screen.getByTestId("roster-shortlist-t1").getAttribute("aria-pressed")).toBe("true");
        expect(screen.getByTestId("roster-shortlist-t2").getAttribute("aria-pressed")).toBe("false");
        expect(screen.getByTestId("roster-shortlist-t3").getAttribute("aria-pressed")).toBe("true");
        expect(screen.getByTestId("roster-shortlist-count").textContent).toBe("2 shortlisted");
        cleanup();
        payload = PAYLOAD({ shortlisted: ["t2"] });                                    // a different viewer / session
        await openRoster();
        expect(screen.getByTestId("roster-shortlist-t1").getAttribute("aria-pressed")).toBe("false");
        expect(screen.getByTestId("roster-shortlist-t2").getAttribute("aria-pressed")).toBe("true");
    });

    it("a failed save rolls that button back and tells the viewer", async () => {
        api.post.mockRejectedValueOnce({ response: { status: 500 } });
        await openRoster();
        const b = screen.getByTestId("roster-shortlist-t1");
        fireEvent.click(b);
        expect(b.getAttribute("aria-pressed")).toBe("true");
        await waitFor(() => expect(b.getAttribute("aria-pressed")).toBe("false"));
        expect(toast.error).toHaveBeenCalled();
        expect(screen.queryByTestId("roster-shortlist-count")).toBeNull();
    });

    it("a double tap while saving sends one request", async () => {
        let release;
        api.post.mockImplementationOnce(() => new Promise((res) => { release = () => res({ data: { ok: true } }); }));
        await openRoster();
        const b = screen.getByTestId("roster-shortlist-t1");
        fireEvent.click(b); fireEvent.click(b); fireEvent.click(b);
        expect(api.post).toHaveBeenCalledTimes(1);
        expect(b.getAttribute("aria-pressed")).toBe("true");
        await act(async () => { release(); });
        await waitFor(() => expect(b.getAttribute("aria-busy")).toBe("false"));
    });
});

describe("selected media only, and scale (50+ talents)", () => {
    it("only selected images are ever rendered, and only for sections near the viewport", async () => {
        render(<RosterView />);
        await screen.findByTestId("roster-sections");
        expect(document.querySelectorAll("main img").length).toBe(0);                  // nothing near yet -> no image mounted
        await setNear("roster-hero-t2", true);
        expect(screen.queryByTestId("roster-image-t1")).toBeNull();
        expect(heroSrc("t2")).toBe("https://img.test/j1.jpg");
        expect(document.body.innerHTML).not.toContain("j2.jpg");                       // other images of the talent are not in the DOM
        expect(document.body.innerHTML).not.toContain("t1a.jpg");
        await setNear("roster-hero-t2", false);                                        // scrolled far away -> unmounted again
        expect(screen.queryByTestId("roster-image-t2")).toBeNull();
    });

    it("a 60-talent roster mounts one image per NEAR section and never the other ~170", async () => {
        const many = Array.from({ length: 60 }, (_, i) => talent(i + 1, { name: `Person ${i + 1}`, images: [img(`p${i + 1}a`), img(`p${i + 1}b`), img(`p${i + 1}c`)] }));
        payload = PAYLOAD({ talents: many });
        render(<RosterView />);
        await screen.findByTestId("roster-sections");
        expect(screen.getAllByTestId(/^roster-talent-t\d+$/).length).toBe(60);         // all sections exist (vertical page)...
        expect(document.querySelectorAll("main img").length).toBe(0);                  // ...but no image until it is near
        for (const k of ["t10", "t11"]) await setNear(`roster-hero-${k}`, true);
        expect(document.querySelectorAll("main img").length).toBe(2);
        expect(document.querySelectorAll("main img[src*='p10a'], main img[src*='p11a']").length).toBe(2);
        const total = document.getElementsByTagName("*").length;
        expect(total).toBeLessThan(60 * 90);                                           // bounded, linear DOM
    });

    it("warms only the next/previous image of a NEAR talent, nothing for far talents", async () => {
        const made = [];
        const RealImage = window.Image;
        window.Image = function FakeImage() { return { set src(v) { made.push(v); } }; };
        try {
            render(<RosterView />);
            await screen.findByTestId("roster-sections");
            expect(made).toEqual([]);
            await setNear("roster-hero-t1", true);
            await waitFor(() => expect(made).toEqual(["https://img.test/t1b.jpg"]));      // only t1's next image
            next("t1");
            await waitFor(() => expect(made.sort()).toEqual(["https://img.test/t1a.jpg", "https://img.test/t1b.jpg", "https://img.test/t1c.jpg"]));   // now also the previous one
            expect(made.some((u) => /j\d|d1|t2/.test(u))).toBe(false);                     // no other talent's images
        } finally { window.Image = RealImage; }
    });

    it("without IntersectionObserver (old browsers) only the first few sections get images", async () => {
        delete globalThis.IntersectionObserver;
        const many = Array.from({ length: 30 }, (_, i) => talent(i + 1));
        payload = PAYLOAD({ talents: many });
        render(<RosterView />);
        await screen.findByTestId("roster-sections");
        await waitFor(() => expect(document.querySelectorAll("main img").length).toBe(3));
    });

    it("an empty roster shows a message instead of an empty page", async () => {
        payload = PAYLOAD({ talents: [], pdf_available: false });
        render(<RosterView />);
        expect(await screen.findByTestId("roster-empty")).toBeTruthy();
    });
});

describe("PDF button (unchanged behaviour)", () => {
    it("is in the header when the roster has images and points at the existing PDF endpoint; hidden otherwise", async () => {
        await openRoster();
        expect(screen.getByTestId("roster-download-pdf")).toBeTruthy();
        const assign = vi.fn();
        const orig = window.location;
        Object.defineProperty(window, "location", { configurable: true, value: { ...orig, assign, hash: orig.hash } });
        fireEvent.click(screen.getByTestId("roster-download-pdf"));
        Object.defineProperty(window, "location", { configurable: true, value: orig });
        expect(assign).toHaveBeenCalledWith("https://api.example.test/api/public/links/talentgram-x-bkb-abc/roster/pdf?token=viewer-token");
        cleanup();
        payload = PAYLOAD({ pdf_available: false });
        await openRoster();
        expect(screen.queryByTestId("roster-download-pdf")).toBeNull();
    });
});

describe("Allow PDF Download (server decides via pdf_available)", () => {
    it("ON: Download PDF is in the header", async () => {
        await openRoster({ pdf_available: true });
        const btn = screen.getByTestId("roster-download-pdf");
        expect(btn.textContent).toContain("Download PDF");
        expect(btn.getAttribute("aria-label")).toBe("Download PDF");
    });

    it("OFF: no Download PDF anywhere, no empty button area, and the rest of the page is unchanged", async () => {
        await openRoster({ pdf_available: false });
        expect(screen.queryByTestId("roster-download-pdf")).toBeNull();
        expect(screen.queryByText(/Download PDF|Preparing PDF/)).toBeNull();
        expect(screen.queryByLabelText(/PDF/)).toBeNull();
        const header = screen.getByTestId("roster-header-title").parentElement;
        expect(header.children.length).toBe(3);                                     // logo, title, right slot — same structure as ON
        expect(screen.getByTestId("roster-index-open")).toBeTruthy();               // the other header control is still there
        expect(within(screen.getByTestId("roster-sections")).getAllByRole("heading", { level: 2 }).length).toBe(3);
        fireEvent.click(screen.getByTestId("roster-next-t1"));                      // carousel + shortlist still work
        expect(screen.getByTestId("roster-counter-t1").textContent).toBe("02 / 03");
        fireEvent.click(screen.getByTestId("roster-shortlist-t1"));
        expect(screen.getByTestId("roster-shortlist-t1").getAttribute("aria-pressed")).toBe("true");
    });
});
