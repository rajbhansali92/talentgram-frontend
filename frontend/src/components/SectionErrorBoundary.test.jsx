import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import SectionErrorBoundary from "./SectionErrorBoundary";

function Bomb() {
    throw new Error("boom");
}

// React logs uncaught errors to the console even when a boundary catches
// them (expected, standard behavior) — silence that noise for these tests.
const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

afterEach(() => {
    cleanup();
    consoleErrorSpy.mockClear();
});

describe("SectionErrorBoundary", () => {
    it("renders children normally when there's no error", () => {
        const { getByText } = render(
            <SectionErrorBoundary label="test">
                <div>All good</div>
            </SectionErrorBoundary>
        );
        expect(getByText("All good")).toBeTruthy();
    });

    it("catches a render-time throw and shows the default fallback instead of crashing", () => {
        const { getByText } = render(
            <SectionErrorBoundary label="test">
                <Bomb />
            </SectionErrorBoundary>
        );
        expect(getByText("This section couldn't load.")).toBeTruthy();
    });

    it("renders a custom fallback when provided", () => {
        const { getByText } = render(
            <SectionErrorBoundary label="test" fallback={<span>Custom fallback</span>}>
                <Bomb />
            </SectionErrorBoundary>
        );
        expect(getByText("Custom fallback")).toBeTruthy();
    });

    it("logs the error with its label for debugging", () => {
        render(
            <SectionErrorBoundary label="comments">
                <Bomb />
            </SectionErrorBoundary>
        );
        expect(consoleErrorSpy).toHaveBeenCalledWith(
            expect.stringContaining("comments"),
            expect.any(Error),
            expect.anything()
        );
    });

    it("isolates failures: a crash in one boundary does not affect a sibling boundary", () => {
        const { getByText } = render(
            <div>
                <SectionErrorBoundary label="a">
                    <Bomb />
                </SectionErrorBoundary>
                <SectionErrorBoundary label="b">
                    <div>Sibling content still renders</div>
                </SectionErrorBoundary>
            </div>
        );
        expect(getByText("This section couldn't load.")).toBeTruthy();
        expect(getByText("Sibling content still renders")).toBeTruthy();
    });
});
