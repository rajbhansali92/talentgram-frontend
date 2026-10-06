import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, fireEvent, within } from "@testing-library/react";

vi.mock("next/navigation", () => ({
    useParams: () => ({ slug: "test-slug" }),
    useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
    usePathname: () => "/l/test-slug",
    useSearchParams: () => new URLSearchParams(),
}));
vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), info: vi.fn() }) }));

import { TalentDetail } from "./ClientView";

afterEach(() => {
    cleanup();
});

// The fixture values are test data only — the component renders whatever it is given.
const PROJECT_BUDGET = [
    { project_id: "p1", lines: [{ label: "Budget", value: "BUDGET-LINE-X" }], talent_budget: [], budget_per_day: "" },
];
const SHOOT_DATES = [{ project_id: "p1", shoot_dates: "SHOOT-DATES-X" }];

function makeTalent(over = {}) {
    return {
        id: "t1",
        project_id: "p1",
        name: "Test Talent",
        media: [],
        availability: { status: "yes" },
        budget: { status: "custom", value: "COUNTER-X" },
        competitive_brand: "COMPETITIVE-BRAND-X",
        work_links: ["Reel || https://youtube.com/watch?v=abc123"],
        ...over,
    };
}

function renderDetail(talent = makeTalent(), linkOver = {}) {
    const noop = () => {};
    return render(
        <TalentDetail
            talent={talent}
            talents={[talent]}
            link={{ visibility: { budget: true, availability: true, work_links: true }, ...linkOver }}
            slug="test-slug"
            projectBudget={PROJECT_BUDGET}
            projectShootDates={SHOOT_DATES}
            viewerAction={null}
            viewerActions={{}}
            reviewedIds={new Set()}
            isReviewed={false}
            onMarkReviewed={noop}
            onClose={noop}
            onNavigate={noop}
            setAction={noop}
            commentDraft=""
            setCommentDraft={noop}
            saveComment={noop}
            logDownload={noop}
            onShare={noop}
            saveVoiceNote={noop}
            sendingVoice={false}
        />,
    );
}

// TalentDetail renders the metadata twice: an always-visible, non-collapsible copy inside the
// `md:hidden` mobile block, and the "Availability & Budget" ACCORDION in the desktop right panel.
// jsdom applies no CSS, so both are in the DOM — these tests scope to the accordion itself
// (header testid + its panel, found by the id CollapsibleSection gives it).
const header = () => screen.getByTestId("availability-budget-section");
const panel = () => document.getElementById("availability-budget-section-panel");
const inPanel = () => within(panel());

describe("ClientView — Availability & Budget accordion", () => {
    it("is expanded on first render, with its contents visible and no click needed", () => {
        renderDetail();
        expect(header().getAttribute("aria-expanded")).toBe("true");
        expect(panel()).not.toBeNull();
        const p = inPanel();
        expect(p.getByTestId("client-availability")).toBeTruthy();
        expect(p.getByTestId("client-availability-status").textContent).toBe("Available");
        expect(p.getByTestId("client-budget")).toBeTruthy();
        expect(p.getByText("Counter Budget")).toBeTruthy();
        expect(p.getByText("COUNTER-X")).toBeTruthy();
        // "Status" row of the counter-offer block
        expect(p.getByText("Status")).toBeTruthy();
        expect(p.getByText("Counter-Offer")).toBeTruthy();
        expect(p.getByTestId("client-competitive-brand").textContent).toContain("COMPETITIVE-BRAND-X");
    });

    it("omits Competitive Brand when the talent has none (still open, nothing else changes)", () => {
        renderDetail(makeTalent({ competitive_brand: "" }));
        expect(header().getAttribute("aria-expanded")).toBe("true");
        expect(inPanel().queryByTestId("client-competitive-brand")).toBeNull();
        expect(inPanel().getByTestId("client-budget")).toBeTruthy();
    });

    it("collapses when the client clicks the header, stays collapsed, and re-expands on a second click", () => {
        renderDetail();
        fireEvent.click(header());
        expect(header().getAttribute("aria-expanded")).toBe("false");
        expect(panel()).toBeNull();

        fireEvent.click(header());
        expect(header().getAttribute("aria-expanded")).toBe("true");
        expect(panel()).not.toBeNull();
        expect(inPanel().getByTestId("client-budget")).toBeTruthy();
        expect(inPanel().getByTestId("client-availability")).toBeTruthy();
    });

    it("does not force itself open again: a parent re-render keeps a manually collapsed section collapsed", () => {
        const { rerender } = renderDetail();
        fireEvent.click(header());
        expect(header().getAttribute("aria-expanded")).toBe("false");

        const t = makeTalent();
        const noop = () => {};
        rerender(
            <TalentDetail
                talent={t}
                talents={[t]}
                link={{ visibility: { budget: true, availability: true, work_links: true } }}
                slug="test-slug"
                projectBudget={PROJECT_BUDGET}
                projectShootDates={SHOOT_DATES}
                viewerAction={null}
                viewerActions={{}}
                reviewedIds={new Set()}
                isReviewed={false}
                onMarkReviewed={noop}
                onClose={noop}
                onNavigate={noop}
                setAction={noop}
                commentDraft="typing a comment"
                setCommentDraft={noop}
                saveComment={noop}
                logDownload={noop}
                onShare={noop}
                saveVoiceNote={noop}
                sendingVoice={false}
            />,
        );
        expect(header().getAttribute("aria-expanded")).toBe("false");
        expect(panel()).toBeNull();
    });

    it("leaves the other accordions as they were: Work Links closed by default, Your Decision open", () => {
        renderDetail();
        const work = screen.getByTestId("work-links-section");
        expect(work.getAttribute("aria-expanded")).toBe("false");
        expect(document.getElementById("work-links-section-panel")).toBeNull();
        expect(screen.getByTestId("decision-section").getAttribute("aria-expanded")).toBe("true");

        // They still toggle independently of Availability & Budget.
        fireEvent.click(work);
        expect(work.getAttribute("aria-expanded")).toBe("true");
        expect(header().getAttribute("aria-expanded")).toBe("true");
        fireEvent.click(screen.getByTestId("decision-section"));
        expect(screen.getByTestId("decision-section").getAttribute("aria-expanded")).toBe("false");
        expect(header().getAttribute("aria-expanded")).toBe("true");
    });
});
