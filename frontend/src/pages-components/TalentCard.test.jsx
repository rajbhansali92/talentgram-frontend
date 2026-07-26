import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TalentCard } from "./TalentList";

// Perf audit 2026-07-26: TalentCard used to swap its whole body between a
// <Link> (browse mode) and a <button> (selection mode). React can't
// reconcile across different element types at the same tree position, so
// entering/exiting selection mode forced a full unmount+remount of the
// entire card (image, tags, everything) for EVERY visible card at once —
// confirmed live on production via MutationObserver: ~40 remove+add DOM
// node pairs (one per card on the page) for a single checkbox click.
// Fixed by keeping the same <Link> always and toggling behavior via onClick.

afterEach(cleanup);

const talent = {
    id: "t1", name: "Test Talent", location: "Mumbai, India",
    image_url: null, media_count: 0, tags: [],
};

function renderCard(props = {}) {
    const onToggle = vi.fn();
    const onTagClick = vi.fn();
    const utils = render(
        <MemoryRouter>
            <TalentCard
                t={talent}
                checked={false}
                isSelectionMode={false}
                canBulkDelete={false}
                onToggle={onToggle}
                onTagClick={onTagClick}
                {...props}
            />
        </MemoryRouter>
    );
    return { ...utils, onToggle };
}

describe("TalentCard — stable element across selection-mode toggle", () => {
    it("renders the same <a> DOM node when isSelectionMode flips, not a new element", () => {
        const { container, rerender, onToggle } = renderCard({ isSelectionMode: false });
        const linkBefore = container.querySelector("a");
        expect(linkBefore).not.toBeNull();

        rerender(
            <MemoryRouter>
                <TalentCard
                    t={talent} checked={false} isSelectionMode={true}
                    canBulkDelete={false} onToggle={onToggle} onTagClick={() => {}}
                />
            </MemoryRouter>
        );

        const linkAfter = container.querySelector("a");
        expect(linkAfter).not.toBeNull();
        // The regression this closes: entering selection mode used to
        // replace the <a> with a <button> entirely (a different DOM node).
        expect(linkAfter).toBe(linkBefore);
        expect(container.querySelector("button.block.w-full.text-left")).toBeNull();
    });

    it("toggles selection instead of navigating when clicked in selection mode", () => {
        const { container, onToggle } = renderCard({ isSelectionMode: true });
        const link = container.querySelector("a");
        const evt = new MouseEvent("click", { bubbles: true, cancelable: true });
        const prevented = !link.dispatchEvent(evt);

        expect(onToggle).toHaveBeenCalledWith("t1");
        expect(prevented).toBe(true); // navigation was prevented
    });

    it("does not call onToggle when clicked outside selection mode (normal navigation)", () => {
        const { container, onToggle } = renderCard({ isSelectionMode: false });
        const link = container.querySelector("a");
        link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));

        expect(onToggle).not.toHaveBeenCalled();
    });
});
