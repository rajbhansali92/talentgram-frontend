import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TalentListRow } from "./TalentList";

afterEach(cleanup);

// The row exactly as GET /talents returns it: counters computed server-side, no media[].
const row = (over = {}) => ({
    id: "t1", name: "Sana Shalini", location: "Bengaluru, India", image_url: null, tags: [], age: 32, height: "5'4\"",
    phone: "+919999999999", instagram_handle: "sanashalini",
    media_count: 7, image_count: 6, video_count: 1, ...over,
});

function renderRow(t) {
    return render(
        <MemoryRouter>
            <TalentListRow t={t} checked={false} isSelectionMode={false} canBulkDelete={false}
                onToggle={vi.fn()} onTagClick={vi.fn()} onProjectsClick={vi.fn()} onQuickView={vi.fn()} />
        </MemoryRouter>,
    );
}
const counters = (c) => ({
    images: [...c.querySelectorAll("span")].map((s) => s.textContent.trim()).filter((x) => x.startsWith("📷")),
    videos: [...c.querySelectorAll("span")].map((s) => s.textContent.trim()).filter((x) => x.startsWith("🎥")),
});

describe("Global Talent list row counters", () => {
    it("shows the Introduction Video as 1 (was always 0) and the true image count", () => {
        const { container } = renderRow(row());
        const c = counters(container);
        expect(c.videos.length).toBeGreaterThan(0);
        expect(c.videos.every((x) => x === "🎥 1")).toBe(true);
        expect(c.images.every((x) => x === "📷 6")).toBe(true);
    });
    it("a talent with images but no Introduction Video shows 🎥 0 and keeps the image count", () => {
        const { container } = renderRow(row({ media_count: 2, image_count: 2, video_count: 0 }));
        const c = counters(container);
        expect(c.videos.every((x) => x === "🎥 0")).toBe(true);
        expect(c.images.every((x) => x === "📷 2")).toBe(true);
    });
    it("a talent with no media at all shows 0 / 0 (no false video count)", () => {
        const { container } = renderRow(row({ media_count: 0, image_count: 0, video_count: 0 }));
        const c = counters(container);
        expect(c.videos.every((x) => x === "🎥 0") && c.images.every((x) => x === "📷 0")).toBe(true);
    });
    it("row actions are all still rendered (layout untouched)", () => {
        const { container } = renderRow(row());
        const text = container.textContent;
        for (const label of ["Tags", "Projects", "View", "Edit", "WhatsApp"]) expect(text).toContain(label);
    });
});
