import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, within } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useParams: () => ({ slug: "talentgram-x-bkb-abc" }) }));
vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), success: vi.fn() }) }));
vi.mock("@/components/Logo", () => ({ default: (p) => <img alt="Talentgram" data-testid="brand-logo" data-size={p.size} className={p.className} /> }));
vi.mock("@/lib/api", () => ({
    api: { get: vi.fn(), post: vi.fn() },
    API: "https://api.example.test/api",
    IMAGE_URL: (m) => m.url,
    getViewerToken: () => "viewer-token",
    saveViewerToken: vi.fn(),
}));

import { api } from "@/lib/api";
import RosterView from "./RosterView";

const img = (id, ratio = 0.75) => ({ id, url: `https://img.test/${id}.jpg`, category: "indian", ratio });
const placedInfo = (items) => items.map((it, i) => ({ ...it, x: 14 + i * 40, y: 240, w: 38, lines: [it.value] }));
const INFO_FULL = [
    { key: "age", label: "Age", value: "25" },
    { key: "location", label: "Location", value: "Mumbai, India" },
    { key: "instagram", label: "Instagram", value: "@ak_official", href: "https://www.instagram.com/ak_official/" },
];
const PAYLOAD = {
    title: "Talentgram X BKB", subtitle: "Selected Talent — October 2026", fields: {},
    page: { w: 210, h: 297, margin: 14 }, pdf_available: true,
    talents: [
        { key: "t1", index: 1, name: "Angela K", info: INFO_FULL, video: { url: "https://v.test/a.mp4" }, images: [img("a1"), img("a2")],
          pages: [{ kind: "hero", template: "portrait", rule_y: 236, video_y: 259, hero_h: 176, name_pt: 28, info: placedInfo(INFO_FULL),
                    slots: [{ media_id: "a1", x: 14, y: 54, w: 120, h: 176, fit: "contain", focal: [0.5, 0.5] }, { media_id: "a2", x: 138, y: 54, w: 58, h: 77, fit: "contain", focal: [0.5, 0.5] }] }] },
        { key: "t2", index: 2, name: "Jasmine S", info: [{ key: "age", label: "Age", value: "27" }, { key: "skills", label: "Skills", value: "Actor, Model", block: true }],
          video: null, images: [img("j1")],
          pages: [{ kind: "hero", template: "portrait", rule_y: 236, video_y: null, hero_h: 176, name_pt: 28,
                    info: placedInfo([{ key: "age", label: "Age", value: "27" }]),
                    slots: [{ media_id: "j1", x: 14, y: 54, w: 182, h: 176, fit: "contain", focal: [0.5, 0.5] }] },
                   { kind: "info", template: "info", slots: [], info: [{ key: "skills", label: "Skills", value: "Actor, Model", x: 14, y: 58, w: 182, lines: ["Actor, Model"] }] }] },
    ],
};

function setViewport(desktop) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
        matches: desktop, media: q, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
    }));
}

beforeEach(() => {
    api.get.mockReset();
    api.get.mockImplementation((url) => {
        if (url.endsWith("/meta")) return Promise.resolve({ data: { link_type: "roster", title: PAYLOAD.title, subtitle: PAYLOAD.subtitle } });
        return Promise.resolve({ data: PAYLOAD });
    });
});
afterEach(cleanup);

describe("RosterView", () => {
    it("cover: larger logo, title, subtitle, Instagram icon + handle, clickable, and no website footer", async () => {
        setViewport(true);
        render(<RosterView />);
        const cover = await screen.findByTestId("roster-cover");
        const logo = within(cover).getByTestId("brand-logo");
        expect(Number(logo.getAttribute("data-size"))).toBeGreaterThanOrEqual(92);   // header logo is 26; cover is a hero
        expect(screen.getByTestId("roster-title").textContent).toBe("Talentgram X BKB");
        expect(screen.getByTestId("roster-subtitle").textContent).toContain("Selected Talent");
        const links = screen.getAllByTestId("roster-instagram");
        expect(links.length).toBe(2);   // cover + page footer: the same element
        for (const a of links) {
            expect(a.getAttribute("href")).toBe("https://www.instagram.com/talentgram.agency/");
            expect(a.textContent).toContain("@talentgram.agency");
            expect(a.querySelector("svg")).toBeTruthy();
            expect(a.getAttribute("target")).toBe("_blank");
        }
        expect(document.body.textContent.toLowerCase()).not.toContain("talentgramagency.com");
    });

    it("desktop: draws the server-planned metadata, only shows the video button for talents that have one", async () => {
        setViewport(true);
        render(<RosterView />);
        await screen.findByTestId("roster-talent-t1");
        const t1 = screen.getByTestId("roster-talent-t1");
        for (const k of ["age", "location", "instagram"]) expect(within(t1).getByTestId(`roster-info-${k}`)).toBeTruthy();
        expect(within(t1).getByTestId("roster-info-instagram").querySelector("a").getAttribute("href")).toBe("https://www.instagram.com/ak_official/");
        expect(within(t1).getByTestId("roster-video-t1").getAttribute("href")).toBe("https://v.test/a.mp4");
        const t2 = screen.getByTestId("roster-talent-t2");
        expect(within(t2).queryByTestId("roster-video-t2")).toBeNull();            // no empty button for a missing video
        expect(within(t2).queryByTestId("roster-info-instagram")).toBeNull();      // field not shown -> not drawn
        expect(within(t2).getByTestId("roster-info-skills")).toBeTruthy();          // information page content
        expect(within(t2).getAllByTestId(/^roster-page-t2-/).length).toBe(2);
        expect(document.body.textContent).not.toMatch(/undefined|N\/A|null/);
    });

    it("mobile: natural stack, block fields span the width, no video link when missing", async () => {
        setViewport(false);
        render(<RosterView />);
        await screen.findByTestId("roster-talent-t1");
        expect(screen.queryAllByTestId(/^roster-page-/).length).toBe(0);
        const t2 = screen.getByTestId("roster-talent-t2");
        expect(within(t2).queryByTestId("roster-video-t2")).toBeNull();
        const skills = within(t2).getByText("Skills").closest("div");
        expect(skills.className).toContain("col-span-2");
        expect(within(screen.getByTestId("roster-talent-t1")).getByTestId("roster-video-t1")).toBeTruthy();
        // nav names come straight from the server (First + initial)
        expect(screen.getByTestId("roster-nav-t1").textContent).toContain("Angela K");
        expect(screen.getByTestId("roster-nav-t2").textContent).toContain("Jasmine S");
        expect(document.body.textContent).not.toMatch(/Kumar|Singh/);
    });

    it("download PDF button is shown only when the roster has images", async () => {
        setViewport(true);
        api.get.mockImplementation((url) => Promise.resolve({ data: url.endsWith("/meta") ? { link_type: "roster", title: "T" } : { ...PAYLOAD, pdf_available: false } }));
        render(<RosterView />);
        await screen.findByTestId("roster-talent-t1");
        expect(screen.queryByTestId("roster-download-pdf")).toBeNull();
    });
});
