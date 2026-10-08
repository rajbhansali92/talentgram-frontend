import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup, screen } from "@testing-library/react";
import { LocationLink, mapsSearchUrl } from "@/lib/productionDesk";

afterEach(cleanup);

describe("LocationLink — clickable Google Maps location, no API key needed", () => {
    it("opens the stored Maps URL of a selected place and shows its exact formatted address", () => {
        const stored = "https://www.google.com/maps/search/?api=1&query=19.05,72.83&query_place_id=ChIJabcdefghij";
        render(<LocationLink name="Mehboob Studio" address="Mehboob Studio, Bandra West, Mumbai 400050" mapUrl={stored} />);
        const a = screen.getByRole("link");
        expect(a.getAttribute("href")).toBe(stored);
        expect(a.getAttribute("target")).toBe("_blank");
        expect(a.textContent).toMatch(/Mehboob Studio, Bandra West, Mumbai 400050/);
    });

    it("with no stored URL, falls back to a Maps search for the address, else for the typed name", () => {
        const { unmount } = render(<LocationLink name="Film City" address="Film City Rd, Goregaon East, Mumbai" mapUrl={null} />);
        expect(screen.getByRole("link").getAttribute("href")).toBe("https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent("Film City, Film City Rd, Goregaon East, Mumbai"));
        unmount();
        render(<LocationLink name="Studio C, Andheri" mapUrl="" />);
        expect(screen.getByRole("link").getAttribute("href")).toBe("https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent("Studio C, Andheri"));
    });

    it("renders a dash for an empty location and never links text too short to search", () => {
        const { container, unmount } = render(<LocationLink name="" mapUrl={null} />);
        expect(container.textContent).toBe("—");
        unmount();
        render(<LocationLink name="TBD" mapUrl={null} />);
        expect(screen.getByRole("link")).toBeTruthy();                       // 3 characters is searchable
        expect(mapsSearchUrl("ab")).toBeNull();
        expect(mapsSearchUrl(undefined, null)).toBeNull();
    });
});
