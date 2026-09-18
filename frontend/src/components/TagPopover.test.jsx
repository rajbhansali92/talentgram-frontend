import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import TagPopover from "./TagPopover";

// Multi-select fix (2026-09-18): clicking an existing tag used to persist
// it to the backend immediately, one API call per click. The required
// behaviour is a "pending selection" — clicking stages a tag locally
// (visually distinct, dashed amber chip), and "Save & Close" persists
// every staged tag in one action.

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));
vi.mock("sonner", () => ({
    toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/talentPreviewCache", () => ({
    talentPreviewCache: { invalidateTalent: vi.fn() },
}));

import { adminApi } from "@/lib/api";
import { toast } from "sonner";

afterEach(cleanup);

const ALL_TAGS = [
    { id: "t1", name: "actor 1" },
    { id: "t2", name: "actor 2" },
    { id: "t3", name: "actor 3" },
];

function setup(overrides = {}) {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    adminApi.delete.mockReset();
    toast.success.mockReset();
    toast.error.mockReset();
    adminApi.get.mockResolvedValue({ data: { tags: ALL_TAGS } });
    adminApi.post.mockResolvedValue({ data: {} });

    const onSave = vi.fn();
    const onClose = vi.fn();
    const talent = { id: "talent-1", name: "Dhanashree Parikh", tags: [], ...overrides.talent };
    render(<TagPopover talent={talent} onSave={onSave} onClose={onClose} />);
    return { onSave, onClose, talent };
}

describe("TagPopover — multi-select tag assignment", () => {
    it("stages multiple existing tags without an API call per click", async () => {
        setup();
        await waitFor(() => expect(screen.getByTestId("tag-option-t1")).toBeTruthy());

        fireEvent.click(screen.getByTestId("tag-option-t1"));
        fireEvent.click(screen.getByTestId("tag-option-t2"));

        // Nothing persisted yet — both selections are purely local/pending.
        expect(adminApi.post).not.toHaveBeenCalled();
        expect(screen.getByTestId("pending-tag-t1")).toBeTruthy();
        expect(screen.getByTestId("pending-tag-t2")).toBeTruthy();
        // Selected tags leave the pick-list (clearly distinguishable —
        // they now live in the pending chip row instead).
        expect(screen.queryByTestId("tag-option-t1")).toBeNull();
        expect(screen.queryByTestId("tag-option-t2")).toBeNull();
    });

    it("Save & Close persists every staged tag in one action", async () => {
        const { onSave, onClose } = setup();
        await waitFor(() => expect(screen.getByTestId("tag-option-t1")).toBeTruthy());

        fireEvent.click(screen.getByTestId("tag-option-t1"));
        fireEvent.click(screen.getByTestId("tag-option-t2"));
        fireEvent.click(screen.getByText("Save & Close"));

        await waitFor(() => expect(adminApi.post).toHaveBeenCalledTimes(2));
        expect(adminApi.post).toHaveBeenCalledWith("/talents/talent-1/tag/t1");
        expect(adminApi.post).toHaveBeenCalledWith("/talents/talent-1/tag/t2");
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        const [talentId, updatedTags] = onSave.mock.calls[0];
        expect(talentId).toBe("talent-1");
        expect(updatedTags.map(t => t.id).sort()).toEqual(["t1", "t2"]);
        await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    });

    it("never allows a tag to be staged twice — clicking again unstages it", async () => {
        setup();
        await waitFor(() => expect(screen.getByTestId("tag-option-t1")).toBeTruthy());

        fireEvent.click(screen.getByTestId("tag-option-t1"));
        expect(screen.getByTestId("pending-tag-t1")).toBeTruthy();

        // Clicking the pending chip's own remove button unstages it —
        // it must return to the pick-list, never end up duplicated.
        fireEvent.click(screen.getByTestId("pending-tag-t1").querySelector("button"));
        expect(screen.queryByTestId("pending-tag-t1")).toBeNull();
        expect(screen.getByTestId("tag-option-t1")).toBeTruthy();
    });

    it("keeps already-assigned tags intact and distinguishable from pending ones", async () => {
        setup({ talent: { tags: [{ id: "t3", name: "actor 3" }] } });
        await waitFor(() => expect(adminApi.get).toHaveBeenCalled());

        // Already-assigned tag shows immediately, never in the pick-list.
        expect(screen.getByText("actor 3")).toBeTruthy();
        expect(screen.queryByTestId("tag-option-t3")).toBeNull();

        fireEvent.click(screen.getByTestId("tag-option-t1"));
        // Assigned (solid) and pending (dashed) tags render as visually
        // distinct chip groups.
        expect(screen.getByTestId("pending-tags")).toBeTruthy();
        expect(screen.getByText("actor 3")).toBeTruthy();
    });

    it("preserves search filtering across pending and assigned tags", async () => {
        setup({ talent: { tags: [{ id: "t3", name: "actor 3" }] } });
        await waitFor(() => expect(screen.getByTestId("tag-option-t1")).toBeTruthy());

        fireEvent.click(screen.getByTestId("tag-option-t1")); // stage one
        const input = screen.getByPlaceholderText("Search or type new tag...");
        fireEvent.change(input, { target: { value: "actor 2" } });

        expect(screen.getByTestId("tag-option-t2")).toBeTruthy();
        expect(screen.queryByTestId("tag-option-t1")).toBeNull(); // staged, and doesn't match search anyway
    });

    it("Save & Close with nothing staged just closes, no API call", async () => {
        const { onClose } = setup();
        await waitFor(() => expect(screen.getByTestId("tag-option-t1")).toBeTruthy());

        fireEvent.click(screen.getByText("Save & Close"));
        expect(adminApi.post).not.toHaveBeenCalled();
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it("keeps a failed tag pending for retry and does not close the modal", async () => {
        const { onSave, onClose } = setup();
        await waitFor(() => expect(screen.getByTestId("tag-option-t1")).toBeTruthy());
        adminApi.post.mockImplementation((url) =>
            url.endsWith("/t1") ? Promise.reject(new Error("boom")) : Promise.resolve({ data: {} })
        );

        fireEvent.click(screen.getByTestId("tag-option-t1"));
        fireEvent.click(screen.getByTestId("tag-option-t2"));
        fireEvent.click(screen.getByText("Save & Close"));

        await waitFor(() => expect(adminApi.post).toHaveBeenCalledTimes(2));
        // t2 succeeded and is saved; t1 failed and stays pending for retry.
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        expect(onSave.mock.calls[0][1].map(t => t.id)).toEqual(["t2"]);
        expect(screen.getByTestId("pending-tag-t1")).toBeTruthy();
        expect(onClose).not.toHaveBeenCalled();
    });
});
