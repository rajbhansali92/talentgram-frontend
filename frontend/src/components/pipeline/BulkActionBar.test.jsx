import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, fireEvent, screen, within } from "@testing-library/react";
import BulkActionBar from "./BulkActionBar";

// Root cause: the "Move Stage" dropdown was rendered as a CSS-absolute child
// of the horizontally-scrolling actions row (overflow-x-auto). Per the CSS
// spec, overflow-x and overflow-y are coupled — any axis set to something
// other than "visible" forces the OTHER axis to compute as "auto" too, so
// the dropdown was ALWAYS clipped to zero visible area, on every browser,
// desktop and mobile alike. The menu existed in the DOM and its click
// handlers worked when invoked directly — it was just never visible or
// reachable by a real click, making the button look completely dead.
// Fixed by rendering the menu via a portal into document.body.

afterEach(cleanup);

function setup(overrides = {}) {
    const onMove = vi.fn().mockResolvedValue(undefined);
    render(
        <BulkActionBar
            count={2}
            onClear={() => {}}
            onMove={onMove}
            onLabel={() => {}}
            onNote={() => {}}
            onDelete={() => {}}
            onExport={() => {}}
            onWhatsApp={() => {}}
            onEmail={() => {}}
            onArchive={() => {}}
            {...overrides}
        />
    );
    return { onMove };
}

describe("BulkActionBar — Move Stage dropdown visibility", () => {
    it("renders the stage menu into document.body, not inside the scrolling actions row", () => {
        setup();
        fireEvent.click(screen.getByText("Move Stage"));

        const menu = screen.getByText(/Move 2 talents to/i).parentElement;
        // The old bug: menu was a descendant of the overflow-x-auto row,
        // which per the CSS overflow-x/y coupling rule silently clipped it
        // to zero visible area no matter what CSS was applied to it.
        const scrollingRow = document.querySelector(".overflow-x-auto");
        expect(scrollingRow.contains(menu)).toBe(false);
        // It must still be a real, present, reachable element (in body).
        expect(document.body.contains(menu)).toBe(true);
    });

    it("calls onMove with the selected stage when a menu item is clicked", async () => {
        const { onMove } = setup();
        fireEvent.click(screen.getByText("Move Stage"));

        const menu = screen.getByText(/Move 2 talents to/i).parentElement;
        fireEvent.click(within(menu).getByText("SHORTLISTED"));

        expect(onMove).toHaveBeenCalledWith("shortlisted");
    });

    it("closes the menu on outside click without breaking the portal wiring", () => {
        setup();
        fireEvent.click(screen.getByText("Move Stage"));
        expect(screen.queryByText(/Move 2 talents to/i)).not.toBe(null);

        fireEvent.mouseDown(document.body);

        expect(screen.queryByText(/Move 2 talents to/i)).toBe(null);
    });
});
