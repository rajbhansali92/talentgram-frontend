import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup, fireEvent } from "@testing-library/react";
import OfflinePage from "./page";

let reloadSpy;

beforeEach(() => {
    reloadSpy = vi.fn();
    Object.defineProperty(window, "location", { value: { reload: reloadSpy }, writable: true });
});
afterEach(() => {
    cleanup();
});

describe("OfflinePage — reconnect recovery", () => {
    it("does not reload on mount", () => {
        render(<OfflinePage />);
        expect(reloadSpy).not.toHaveBeenCalled();
    });

    it("reloads automatically when the browser reports it's back online", () => {
        render(<OfflinePage />);
        fireEvent(window, new Event("online"));
        expect(reloadSpy).toHaveBeenCalledTimes(1);
    });

    it("still supports the manual Retry Connection button", () => {
        const { getByText } = render(<OfflinePage />);
        fireEvent.click(getByText("Retry Connection"));
        expect(reloadSpy).toHaveBeenCalledTimes(1);
    });

    it("removes the online listener on unmount (no reload after unmount)", () => {
        const { unmount } = render(<OfflinePage />);
        unmount();
        fireEvent(window, new Event("online"));
        expect(reloadSpy).not.toHaveBeenCalled();
    });
});
