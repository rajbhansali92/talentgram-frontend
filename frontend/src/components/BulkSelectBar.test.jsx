import { describe, it, expect, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import BulkSelectBar from "./BulkSelectBar";

afterEach(cleanup);

describe("BulkSelectBar — Merge Different Emails action", () => {
    it("does not render the Merge Different Emails button when onMergeEmails is not passed (e.g. Projects/Links lists)", () => {
        render(<BulkSelectBar count={2} onMerge={vi.fn()} onDelete={vi.fn()} testid="bar" />);
        expect(screen.queryByTestId("bar-merge-emails")).toBeNull();
        // The existing Merge Talents button must still render unaffected.
        expect(screen.getByTestId("bar-merge")).toBeTruthy();
    });

    it("renders a separate, sibling button from Merge Talents when onMergeEmails IS passed, enabled only at count===2", () => {
        const onMerge = vi.fn();
        const onMergeEmails = vi.fn();
        const { rerender } = render(
            <BulkSelectBar count={2} onMerge={onMerge} onMergeEmails={onMergeEmails} onDelete={vi.fn()} testid="bar" />
        );
        const mergeBtn = screen.getByTestId("bar-merge");
        const mergeEmailsBtn = screen.getByTestId("bar-merge-emails");
        expect(mergeBtn.textContent).toContain("Merge Talents");
        expect(mergeEmailsBtn.textContent).toContain("Merge Different Emails");
        expect(mergeEmailsBtn.disabled).toBe(false);

        mergeEmailsBtn.click();
        expect(onMergeEmails).toHaveBeenCalledTimes(1);
        expect(onMerge).not.toHaveBeenCalled();

        rerender(<BulkSelectBar count={1} onMerge={onMerge} onMergeEmails={onMergeEmails} onDelete={vi.fn()} testid="bar" />);
        const disabledBtn = screen.getByTestId("bar-merge-emails");
        expect(disabledBtn.disabled).toBe(true);
        expect(disabledBtn.textContent).toContain("Select exactly 2");

        rerender(<BulkSelectBar count={3} onMerge={onMerge} onMergeEmails={onMergeEmails} onDelete={vi.fn()} testid="bar" />);
        expect(screen.getByTestId("bar-merge-emails").disabled).toBe(true);
    });
});
