import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useParams: () => ({ token: "tok-abc" }) }));
vi.mock("@/components/Logo", () => ({ default: () => null }));
vi.mock("@/components/HlsVideo", () => ({ default: (p) => <video data-testid={p["data-testid"]} src={p.src} /> }));
vi.mock("@/lib/api", () => ({
    api: { get: vi.fn() },
    API: "https://api.example.test/api",
    IMAGE_URL: (m) => m.url,
}));

import { api } from "@/lib/api";
import TalentMediaPage from "./TalentMediaPage";

const view = {
    talent: { name: "Jasmine S" },
    total: 2,
    sections: [{ key: "indian", label: "Indian Look Images", items: [{ id: "m1", url: "https://cdn.test/1.jpg", filename: "x.jpg" }] }],
    video: { id: "v1", url: "https://cdn.test/v.m3u8", poster_url: null, filename: "v.mp4" },
};
const httpError = (status) => Object.assign(new Error(`HTTP ${status}`), { response: { status } });

beforeEach(() => { api.get.mockReset(); });
afterEach(cleanup);

describe("Talent media page — link states", () => {
    it("shows the media, with the same download buttons, while the link is live", async () => {
        api.get.mockResolvedValue({ data: view });
        render(<TalentMediaPage />);
        expect((await screen.findByTestId("talent-media-name")).textContent).toBe("Jasmine S");
        expect(screen.getByTestId("download-image-btn").getAttribute("href")).toBe("https://api.example.test/api/public/talent-media/tok-abc/file/m1");
        expect(screen.getByTestId("download-video-btn").getAttribute("href")).toBe("https://api.example.test/api/public/talent-media/tok-abc/file/v1");
        expect(screen.getByTestId("download-all-btn").textContent).toContain("Download All (2)");
        expect(screen.queryByTestId("talent-media-expired")).toBeNull();
    });

    it("an expired link (410) shows the expired page and asks for a new link — no media, no download buttons", async () => {
        api.get.mockRejectedValue(httpError(410));
        render(<TalentMediaPage />);
        const page = await screen.findByTestId("talent-media-expired");
        expect(page.textContent).toContain("This link has expired");
        expect(page.textContent).toMatch(/ask for a new link/i);
        expect(screen.queryByTestId("download-all-btn")).toBeNull();
        expect(screen.queryByTestId("download-image-btn")).toBeNull();
        expect(screen.queryByTestId("talent-media-error")).toBeNull();
    });

    it("an off / invalid link (404) keeps the existing unavailable message, distinct from expired", async () => {
        api.get.mockRejectedValue(httpError(404));
        render(<TalentMediaPage />);
        expect((await screen.findByTestId("talent-media-error")).textContent).toContain("This link isn’t available");
        expect(screen.queryByTestId("talent-media-expired")).toBeNull();
    });

    it("a network/server failure still offers Try again, not the expired page", async () => {
        api.get.mockRejectedValue(httpError(500));
        render(<TalentMediaPage />);
        expect(await screen.findByRole("button", { name: "Try again" })).not.toBeNull();
        expect(screen.queryByTestId("talent-media-expired")).toBeNull();
    });

    it("Download All re-checks the link first: if it expired while the page was open, the expired page replaces the media and nothing is downloaded", async () => {
        api.get.mockResolvedValueOnce({ data: view }).mockRejectedValueOnce(httpError(410));
        const assign = vi.fn();
        const original = window.location;
        Object.defineProperty(window, "location", { configurable: true, value: { ...original, assign } });
        try {
            render(<TalentMediaPage />);
            fireEvent.click(await screen.findByTestId("download-all-btn"));
            expect(await screen.findByTestId("talent-media-expired")).not.toBeNull();
            expect(assign).not.toHaveBeenCalled();
        } finally {
            Object.defineProperty(window, "location", { configurable: true, value: original });
        }
    });
});
