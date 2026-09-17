import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, cleanup } from "@testing-library/react";

// Mock the transport wrapper directly. Its contract is "always resolves to
// { data } or { error }, never rejects" — so the test never creates a
// rejected promise (which the vitest/jsdom combo mis-attributes as unhandled).
vi.mock("@/lib/simpleAssistant", async (importOriginal) => ({
    ...(await importOriginal()),
    fetchAssistantOverview: vi.fn(),
}));

import { fetchAssistantOverview } from "@/lib/simpleAssistant";
import { useAssistantOverview } from "./useAssistantOverview";

afterEach(cleanup);
beforeEach(() => fetchAssistantOverview.mockReset());

describe("useAssistantOverview", () => {
    it("loads the read-only overview", async () => {
        fetchAssistantOverview.mockResolvedValue({ data: { summary: { active_projects: 1 }, projects: [] } });
        const { result } = renderHook(() => useAssistantOverview());

        expect(result.current.loading).toBe(true);
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.data.summary.active_projects).toBe(1);
        expect(result.current.error).toBeNull();
    });

    it("surfaces a calm error string instead of throwing", async () => {
        fetchAssistantOverview.mockResolvedValue({
            error: { response: { status: 500, data: { detail: "boom" } } },
        });
        const { result } = renderHook(() => useAssistantOverview());

        await waitFor(() => expect(result.current.error).toBe("boom"));
        expect(result.current.loading).toBe(false);
        expect(result.current.data).toBeNull();
    });

    it("maps a 404 to the 'not enabled' message", async () => {
        fetchAssistantOverview.mockResolvedValue({ error: { response: { status: 404 } } });
        const { result } = renderHook(() => useAssistantOverview());
        await waitFor(() => expect(result.current.error).toMatch(/not enabled/));
    });

    it("falls back to a generic message when the error has no detail", async () => {
        fetchAssistantOverview.mockResolvedValue({ error: { message: "Network Error" } });
        const { result } = renderHook(() => useAssistantOverview());
        await waitFor(() => expect(result.current.error).toBe("Could not reach Talentgram."));
    });
});
