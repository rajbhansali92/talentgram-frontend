import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), warning: vi.fn(), success: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ adminApi: { get: vi.fn() } }));

import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { pdfErrorMessage, usePdfDownload } from "./pdfDownload";

beforeEach(() => {
    adminApi.get.mockReset(); toast.error.mockClear(); toast.warning.mockClear();
    URL.createObjectURL = vi.fn(() => "blob:x"); URL.revokeObjectURL = vi.fn();
});

describe("usePdfDownload", () => {
    it("ignores a second click while one build is running (no duplicate generation)", async () => {
        let resolve;
        adminApi.get.mockImplementation(() => new Promise((r) => { resolve = r; }));
        const { result } = renderHook(() => usePdfDownload());
        act(() => { result.current.download("L1", "Roster"); });
        act(() => { result.current.download("L1", "Roster"); });
        expect(adminApi.get).toHaveBeenCalledTimes(1);
        expect(result.current.busy).toBe(true);
        await act(async () => { resolve({ data: new Blob(["%PDF"]), headers: {} }); });
        await waitFor(() => expect(result.current.busy).toBe(false));
        // and a later click works again
        adminApi.get.mockResolvedValue({ data: new Blob(["%PDF"]), headers: {} });
        await act(async () => { await result.current.download("L1", "Roster"); });
        expect(adminApi.get).toHaveBeenCalledTimes(2);
    });

    it("warns when the server left images out, and stays quiet when the PDF is complete", async () => {
        adminApi.get.mockResolvedValue({ data: new Blob(["%PDF"]), headers: { "x-roster-images-total": "10", "x-roster-images-included": "7" } });
        const { result } = renderHook(() => usePdfDownload());
        await act(async () => { await result.current.download("L1", "R"); });
        expect(toast.warning).toHaveBeenCalledWith(expect.stringContaining("3 of 10 images"), expect.anything());
        toast.warning.mockClear();
        adminApi.get.mockResolvedValue({ data: new Blob(["%PDF"]), headers: { "x-roster-images-total": "10", "x-roster-images-included": "10" } });
        await act(async () => { await result.current.download("L1", "R"); });
        expect(toast.warning).not.toHaveBeenCalled();
        expect(toast.error).not.toHaveBeenCalled();
    });

    it("reports a specific error and frees the button on failure", async () => {
        adminApi.get.mockRejectedValue({ response: { status: 500 } });
        const { result } = renderHook(() => usePdfDownload());
        await act(async () => { await result.current.download("L1", "R"); });
        expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("Couldn't generate the PDF"), expect.anything());
        expect(result.current.busy).toBe(false);
    });

    it("explains the common failure modes", () => {
        expect(pdfErrorMessage({ response: { status: 404 } })).toMatch(/no available talents/);
        expect(pdfErrorMessage({ response: { status: 403 } })).toMatch(/permission/);
        expect(pdfErrorMessage({})).toMatch(/connection dropped/);
    });
});
