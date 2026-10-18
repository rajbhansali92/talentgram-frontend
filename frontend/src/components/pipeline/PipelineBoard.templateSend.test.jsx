import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() },
    isAdmin: () => true, getRole: () => "admin", getSubdomainUrl: (s) => `https://${s}.x`,
}));

vi.mock("./TalentBrowserModal", async (orig) => ({ ...(await orig()), default: () => null }));       // unrelated to this feature (reads localStorage on mount)

import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import PipelineBoard from "./PipelineBoard";

afterEach(cleanup);

const STAGES = ["ask_to_test", "approved", "hold", "shortlisted", "already_tested", "locked", "rejected", "not_available", "not_interested"];
const ROWS = STAGES.map((stage, i) => ({
    id: `row-${i}`, project_id: "proj-amazon", talent_id: `tal-${i}`, talent_name: `Talent ${i}`, stage, talent_phone: "911234500000",
}));
const W1 = { id: "default", label: "Worker 1", sending_enabled: true, session: { status: "authenticated" } };
const W2 = { id: "worker-2", label: "Worker 2", sending_enabled: true, session: { status: "authenticated" } };

beforeEach(() => {
    // jsdom has no matchMedia / (in this setup) localStorage; the board reads both for layout only.
    window.matchMedia = window.matchMedia || ((q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
    if (!globalThis.localStorage) vi.stubGlobal("localStorage", { getItem: () => null, setItem() {}, removeItem() {} });
    Object.values(adminApi).forEach((f) => f.mockReset?.());
    Object.values(toast).forEach((f) => f.mockReset?.());
    adminApi.get.mockImplementation((url) => {
        if (url === "/projects/proj-amazon/pipeline") return Promise.resolve({ data: { data: ROWS } });
        if (url === "/projects/proj-amazon") return Promise.resolve({ data: { id: "proj-amazon", slug: "amazon", brand_name: "Amazon" } });
        if (url === "/whatsapp/templates") return Promise.resolve({ data: [{ id: "t-cc", name: "Casting Call" }, { id: "t-fu", name: "Follow Up" }] });
        if (url === "/whatsapp/workers") return Promise.resolve({ data: [W1, W2] });
        return Promise.resolve({ data: {} });
    });
    adminApi.post.mockImplementation((url, body) => {
        if (url === "/whatsapp/casting-call/send") return Promise.resolve({ data: { success: true, errors: [], batches: [{ project_id: "proj-amazon", project_name: "Amazon", batch_id: "b1", queued: body.talent_ids.length, skipped: 0 }] } });
        return Promise.resolve({ data: {} });
    });
});

const open = async () => {
    render(<MemoryRouter><PipelineBoard projectId="proj-amazon" /></MemoryRouter>);
    await screen.findByTestId("pipeline-card-row-0");
};
const select = (...rows) => rows.forEach((i) => fireEvent.click(screen.getByLabelText(`Select Talent ${i}`)));
const sends = () => adminApi.post.mock.calls.filter((c) => c[0] === "/whatsapp/casting-call/send");

describe("Casting Pipeline — WhatsApp (template send) from the selected-talent toolbar", () => {
    it("no toolbar and no WhatsApp action with nothing selected", async () => {
        await open();
        expect(screen.queryByTestId("pipeline-bulk-bar")).toBeNull();
        expect(screen.queryByText("WhatsApp")).toBeNull();
    });

    it("appears for ONE selected talent and for several, whatever stage they are in", async () => {
        await open();
        select(0);
        expect(screen.getByText("WhatsApp")).toBeTruthy();
        select(3, 5, 6, 8);                                                  // shortlisted, locked, rejected, not_interested
        expect(screen.getByText("5 Selected")).toBeTruthy();
        expect(screen.getByText("WhatsApp")).toBeTruthy();
    });

    it("opens the existing template picker for a mixed-stage selection and sends ONLY those talents to THIS project", async () => {
        await open();
        select(0, 3, 5, 7);                                                  // ask_to_test, shortlisted, locked, not_available
        fireEvent.click(screen.getByText("WhatsApp"));
        const ctx = await screen.findByTestId("template-picker-context");
        expect(ctx.textContent).toBe("4 selected talents · Amazon");
        fireEvent.click(await screen.findByTestId("template-picker-option-t-cc"));
        fireEvent.click(await screen.findByTestId("send-via-worker-2"));
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(sends()).toHaveLength(1));
        const body = sends()[0][1];
        expect(body.project_ids).toEqual(["proj-amazon"]);                    // the real current project id — not a name, not a picker
        expect(body.talent_ids.sort()).toEqual(["tal-0", "tal-3", "tal-5", "tal-7"]);   // row ids translated to TALENT ids; nobody else
        expect(body).toMatchObject({ template_id: "t-cc", worker_id: "worker-2", apply_pipeline_move: false });
        expect(screen.queryByText(/add to project/i)).toBeNull();
    });

    it("the selection and the pipeline are untouched by a send (no stage-move / pipeline calls)", async () => {
        await open();
        select(1, 2);
        fireEvent.click(screen.getByText("WhatsApp"));
        fireEvent.click(await screen.findByTestId("template-picker-option-t-fu"));
        fireEvent.click(await screen.findByTestId("send-via-default"));
        fireEvent.click(screen.getByTestId("template-picker-send"));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith(expect.stringContaining("Queued 2 messages")));
        await waitFor(() => expect(screen.queryByTestId("template-picker-modal")).toBeNull());
        expect(screen.getByText("2 Selected")).toBeTruthy();                  // selection kept
        const urls = adminApi.post.mock.calls.map((c) => c[0]);
        expect(urls.some((u) => /pipeline\/(bulk-move|move|bulk-archive|bulk-delete)/.test(u))).toBe(false);
        expect(adminApi.put).not.toHaveBeenCalled(); expect(adminApi.delete).not.toHaveBeenCalled(); expect(adminApi.patch).not.toHaveBeenCalled();
    });

    it("Cancel closes the picker, keeps the selection, sends nothing", async () => {
        await open();
        select(0, 1);
        fireEvent.click(screen.getByText("WhatsApp"));
        fireEvent.click(await screen.findByTestId("template-picker-cancel"));
        expect(screen.queryByTestId("template-picker-modal")).toBeNull();
        expect(screen.getByText("2 Selected")).toBeTruthy();
        expect(sends()).toHaveLength(0);
    });

    it("the toolbar's WhatsApp action opens the template picker (it used to open a preview that referenced an undefined variable and crashed the board)", async () => {
        await open();
        select(0, 2);
        fireEvent.click(screen.getByText("WhatsApp"));
        expect(await screen.findByTestId("template-picker-modal")).toBeTruthy();
        expect(screen.queryByText("Bulk WhatsApp Preview")).toBeNull();
        expect(screen.getByText("2 Selected")).toBeTruthy();                  // the board is still alive
    });

    it("the other toolbar actions are unchanged", async () => {
        await open();
        select(0, 2);
        fireEvent.click(screen.getByText("Move Stage"));
        expect(screen.getByText(/Move 2 talents to/)).toBeTruthy();
        const bar = within(screen.getByTestId("pipeline-bulk-bar"));
        for (const t of ["Move Stage", "Create Submissions", "Labels", "Note", "WhatsApp", "Email", "Export", "Archive", "Delete"]) expect(bar.getByText(t)).toBeTruthy();
        fireEvent.click(bar.getByText("Note"));
        expect(await screen.findByText("Add Note in Bulk")).toBeTruthy();
    });
});
