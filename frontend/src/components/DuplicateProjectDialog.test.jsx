import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const toastSuccess = vi.fn();
vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { success: (...a) => toastSuccess(...a), error: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ adminApi: { post: vi.fn() } }));

import { adminApi } from "@/lib/api";
import DuplicateProjectDialog from "./DuplicateProjectDialog";

afterEach(cleanup);
beforeEach(() => { adminApi.post.mockReset(); toastSuccess.mockReset(); });

const PROJECT = { id: "p-1", brand_name: "Baskin Robbins Film 3" };
const setup = (props = {}) => {
    const onCancel = vi.fn(); const onCreated = vi.fn();
    render(<DuplicateProjectDialog open project={PROJECT} onCancel={onCancel} onCreated={onCreated} {...props} />);
    return { onCancel, onCreated };
};

describe("DuplicateProjectDialog", () => {
    it("shows the original name, defaults the copy to '<name> (Copy)' and explains what is and isn't copied", () => {
        setup();
        const dlg = screen.getByTestId("duplicate-project-dialog");
        expect(dlg.textContent).toMatch(/Baskin Robbins Film 3/);
        expect(screen.getByTestId("duplicate-project-name").value).toBe("Baskin Robbins Film 3 (Copy)");
        expect(dlg.textContent).toMatch(/No talents, casting pipeline, submissions or other activity are copied/);
        expect(dlg.textContent).toMatch(/original is not changed/);
    });

    it("renders nothing while closed", () => {
        render(<DuplicateProjectDialog open={false} project={PROJECT} onCancel={() => {}} onCreated={() => {}} />);
        expect(screen.queryByTestId("duplicate-project-dialog")).toBeNull();
    });

    it("Cancel closes without calling the server", () => {
        const { onCancel } = setup();
        fireEvent.click(screen.getByTestId("duplicate-project-cancel"));
        expect(onCancel).toHaveBeenCalledTimes(1);
        expect(adminApi.post).not.toHaveBeenCalled();
    });

    it("sends the chosen name and a request id, then reports success and hands the new project over", async () => {
        adminApi.post.mockResolvedValue({ data: { id: "new-9", brand_name: "Film 4" } });
        const { onCreated } = setup();
        fireEvent.change(screen.getByTestId("duplicate-project-name"), { target: { value: "  Film 4  " } });
        fireEvent.click(screen.getByTestId("duplicate-project-confirm"));
        await waitFor(() => expect(onCreated).toHaveBeenCalledWith({ id: "new-9", brand_name: "Film 4" }));
        expect(adminApi.post).toHaveBeenCalledWith("/projects/p-1/duplicate", { name: "Film 4", request_id: expect.stringMatching(/.{8,}/) });
        expect(toastSuccess).toHaveBeenCalledWith('Duplicated as "Film 4"');
    });

    it("shows a loading state and ignores repeated clicks: one request, however many taps", async () => {
        let release;
        adminApi.post.mockImplementation(() => new Promise((res) => { release = () => res({ data: { id: "n", brand_name: "X" } }); }));
        setup();
        const btn = screen.getByTestId("duplicate-project-confirm");
        fireEvent.click(btn); fireEvent.click(btn); fireEvent.click(btn);
        expect(adminApi.post).toHaveBeenCalledTimes(1);
        expect(btn.textContent).toMatch(/Duplicating/); expect(btn.disabled).toBe(true);
        expect(screen.getByTestId("duplicate-project-cancel").disabled).toBe(true);        // cannot back out mid-flight
        expect(screen.getByTestId("duplicate-project-name").disabled).toBe(true);
        release();
        await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
    });

    it("shows the server's error, creates nothing, and a retry reuses the SAME request id", async () => {
        adminApi.post.mockRejectedValueOnce({ response: { data: { detail: "Project not found" } } }).mockResolvedValue({ data: { id: "n", brand_name: "X" } });
        const { onCreated } = setup();
        fireEvent.click(screen.getByTestId("duplicate-project-confirm"));
        expect((await screen.findByTestId("duplicate-project-error")).textContent).toBe("Project not found");
        expect(onCreated).not.toHaveBeenCalled();
        expect(screen.getByTestId("duplicate-project-confirm").disabled).toBe(false);       // can try again
        fireEvent.click(screen.getByTestId("duplicate-project-confirm"));
        await waitFor(() => expect(onCreated).toHaveBeenCalled());
        expect(adminApi.post.mock.calls[1][1].request_id).toBe(adminApi.post.mock.calls[0][1].request_id);
    });

    it("falls back to a generic message when the failure has no detail", async () => {
        adminApi.post.mockRejectedValue(new Error("network"));
        setup();
        fireEvent.click(screen.getByTestId("duplicate-project-confirm"));
        expect((await screen.findByTestId("duplicate-project-error")).textContent).toMatch(/Nothing was created/);
    });

    it("an empty or over-long name cannot be submitted", () => {
        setup();
        const input = screen.getByTestId("duplicate-project-name"), btn = screen.getByTestId("duplicate-project-confirm");
        fireEvent.change(input, { target: { value: "   " } });
        expect(btn.disabled).toBe(true);
        fireEvent.change(input, { target: { value: "x".repeat(201) } });
        expect(btn.disabled).toBe(true);
        fireEvent.change(input, { target: { value: "ok" } });
        expect(btn.disabled).toBe(false);
    });

    it("Enter submits and Escape cancels", async () => {
        adminApi.post.mockResolvedValue({ data: { id: "n", brand_name: "X" } });
        const { onCancel, onCreated } = setup();
        fireEvent.keyDown(window, { key: "Escape" });
        expect(onCancel).toHaveBeenCalledTimes(1);
        fireEvent.keyDown(screen.getByTestId("duplicate-project-name"), { key: "Enter" });
        await waitFor(() => expect(onCreated).toHaveBeenCalled());
    });
});
