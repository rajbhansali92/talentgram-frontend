import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, screen, fireEvent } from "@testing-library/react";
import WorkLinksDisplay, { parseStoredWorkLink, getLinkMeta } from "./WorkLinksDisplay";

const LINKS = ["Reel || https://youtube.com/watch?v=abc123", "https://instagram.com/harshita"];

afterEach(() => {
    cleanup();
});

describe("parseStoredWorkLink", () => {
    it("splits a labeled entry on ' || '", () => {
        expect(parseStoredWorkLink("Reel || https://youtube.com/x")).toEqual({
            label: "Reel",
            url: "https://youtube.com/x",
        });
    });
    it("treats a bare URL as unlabeled", () => {
        expect(parseStoredWorkLink("https://instagram.com/x")).toEqual({ label: "", url: "https://instagram.com/x" });
    });
});

describe("WorkLinksDisplay — variant=\"list\" (used by ClientView)", () => {
    it("shows the full URL as visible text, not just the domain", () => {
        render(<WorkLinksDisplay links={LINKS} variant="list" />);
        // Domain-only subtitle still present…
        expect(screen.getByText("youtube.com")).toBeTruthy();
        // …AND the full URL is now visible as its own text, not just in href/title.
        expect(screen.getByTestId("work-link-url-0").textContent).toBe("https://youtube.com/watch?v=abc123");
        expect(screen.getByTestId("work-link-url-1").textContent).toBe("https://instagram.com/harshita");
    });

    it("the Open button and href are unchanged", () => {
        render(<WorkLinksDisplay links={LINKS} variant="list" />);
        const openLinks = screen.getAllByText("Open");
        expect(openLinks[0].closest("a").getAttribute("href")).toBe("https://youtube.com/watch?v=abc123");
        expect(openLinks[0].closest("a").getAttribute("rel")).toContain("noopener");
        expect(openLinks[0].closest("a").getAttribute("target")).toBe("_blank");
    });

    it("has a labeled copy-link button per row", () => {
        render(<WorkLinksDisplay links={LINKS} variant="list" />);
        const copyButtons = screen.getAllByLabelText("Copy link");
        expect(copyButtons.length).toBe(2);
    });

    describe("copy-link button", () => {
        beforeEach(() => {
            vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
        });
        afterEach(() => {
            vi.unstubAllGlobals();
        });

        it("copies the full URL to the clipboard on click", async () => {
            render(<WorkLinksDisplay links={LINKS} variant="list" />);
            const copyButtons = screen.getAllByLabelText("Copy link");
            fireEvent.click(copyButtons[0]);
            await Promise.resolve();
            expect(navigator.clipboard.writeText).toHaveBeenCalledWith("https://youtube.com/watch?v=abc123");
        });
    });
});

describe("WorkLinksDisplay — variant=\"cards\" (used elsewhere: TalentEdit, SubmissionPage, LinkResults) — unchanged", () => {
    it("does not render a visible full-URL line or a copy button (list-only additions)", () => {
        render(<WorkLinksDisplay links={LINKS} variant="cards" />);
        expect(screen.queryByTestId("work-link-url-0")).toBeNull();
        expect(screen.queryByLabelText("Copy link")).toBeNull();
    });

    it("still shows domain, label, and a working Open link", () => {
        render(<WorkLinksDisplay links={LINKS} variant="cards" />);
        expect(screen.getByText("Reel")).toBeTruthy();
        expect(screen.getByText("youtube.com")).toBeTruthy();
        const openLinks = screen.getAllByText("Open");
        expect(openLinks[0].closest("a").getAttribute("href")).toBe("https://youtube.com/watch?v=abc123");
    });
});
