import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup, screen, act } from "@testing-library/react";
import PipelineColumn from "./PipelineColumn";

// Mobile P0 bug: every mobile stage starts COLLAPSED (see PipelineBoard's
// mobileExpandedStages, which starts {}), so the "Loading more candidates…"
// sentinel doesn't exist in the DOM on first mount. The IntersectionObserver
// effect used to depend only on [items.length, visibleLimit] — neither of
// which changes when a column is later expanded — so it attached NO observer
// on mobile, ever, and infinite scroll hung forever. Desktop columns start
// expanded (collapsedStages also starts {}, but its default reads the
// opposite way), so the sentinel was already present on mount and desktop
// never hit this.

vi.mock("./PipelineCard", () => ({
    default: ({ item }) => <div data-testid="pipeline-card">{item.id}</div>,
}));

// Deterministic, inspectable IntersectionObserver double.
class FakeIntersectionObserver {
    constructor(callback, options) {
        this.callback = callback;
        this.options = options;
        this.observedElements = [];
        FakeIntersectionObserver.instances.push(this);
    }
    observe(el) {
        this.observedElements.push(el);
    }
    disconnect() {
        this.disconnected = true;
    }
    // Test helper: simulate the sentinel scrolling into view.
    triggerIntersect() {
        this.callback([{ isIntersecting: true }]);
    }
}
FakeIntersectionObserver.instances = [];

const items = Array.from({ length: 25 }, (_, i) => ({ id: `talent-${i}` }));

beforeEach(() => {
    FakeIntersectionObserver.instances = [];
    global.IntersectionObserver = FakeIntersectionObserver;
});
afterEach(() => {
    cleanup();
    delete global.IntersectionObserver;
});

describe("PipelineColumn — infinite scroll attaches after mobile expand", () => {
    it("attaches no observer while collapsed (sentinel isn't rendered yet)", () => {
        render(<PipelineColumn stage="follow_up" items={items} bulkIds={new Set()} isCollapsed={true} />);
        expect(FakeIntersectionObserver.instances.length).toBe(0);
    });

    it("attaches an observer to the sentinel once the column is expanded", () => {
        const { rerender } = render(
            <PipelineColumn stage="follow_up" items={items} bulkIds={new Set()} isCollapsed={true} />
        );
        expect(FakeIntersectionObserver.instances.length).toBe(0);

        // Simulate the user tapping to expand the mobile-collapsed column —
        // items.length and visibleLimit are unchanged, only isCollapsed flips.
        rerender(<PipelineColumn stage="follow_up" items={items} bulkIds={new Set()} isCollapsed={false} />);

        expect(FakeIntersectionObserver.instances.length).toBe(1);
        const observer = FakeIntersectionObserver.instances[0];
        expect(observer.observedElements.length).toBe(1);
    });

    it("loads more candidates once the (now-attached) observer fires", () => {
        const { rerender } = render(
            <PipelineColumn stage="follow_up" items={items} bulkIds={new Set()} isCollapsed={true} />
        );
        rerender(<PipelineColumn stage="follow_up" items={items} bulkIds={new Set()} isCollapsed={false} />);

        expect(screen.getAllByTestId("pipeline-card").length).toBe(20); // initial visibleLimit

        act(() => {
            FakeIntersectionObserver.instances[0].triggerIntersect();
        });

        expect(screen.getAllByTestId("pipeline-card").length).toBe(25); // all 25 now visible
    });
});
