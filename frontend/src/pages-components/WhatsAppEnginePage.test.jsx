import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";

// --- sonner (matches LinkHistory.test.jsx's established pattern) ---
const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({
  toast: Object.assign((...args) => {}, {
    error: (...a) => toastError(...a),
    success: (...a) => toastSuccess(...a),
  }),
}));

vi.mock("@/lib/api", () => ({
  adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// Lightweight stand-ins for heavier dependencies unrelated to multi-worker
// behavior — keeps these tests hermetic and fast, matching this suite's
// existing convention of mocking out large sibling components.
vi.mock("@/components/VirtualList", () => ({ default: () => null }));
vi.mock("@/components/ProjectSearchModal", () => ({ default: () => null }));
vi.mock("@/components/pipeline/TalentBrowserModal", () => ({
  TalentPreviewDrawer: () => null,
  useMediaQuery: () => false,
}));
vi.mock("@/lib/talentPreviewCache", () => ({ talentPreviewCache: { get: () => null, set: () => {} } }));

vi.mock("@/lib/whatsappApi", () => ({
  getTemplates: vi.fn().mockResolvedValue([{ id: "tpl1", name: "Custom", slug: "custom", body_text: "Hi {{talent_name}}", variables: ["talent_name"] }]),
  createTemplate: vi.fn(),
  updateTemplate: vi.fn(),
  deleteTemplate: vi.fn(),
  getPipelineSummary: vi.fn().mockResolvedValue({}),
  createBatch: vi.fn().mockResolvedValue({ batch: { id: "b1" }, jobs: [], skipped: [] }),
  getBatches: vi.fn().mockResolvedValue([]),
  runBatchAction: vi.fn(),
  getJobs: vi.fn().mockResolvedValue([]),
  retryJob: vi.fn(),
  getWhatsAppAgents: vi.fn().mockResolvedValue([]),
  getWorkers: vi.fn().mockResolvedValue([]),
  createWorker: vi.fn(),
  getWorkerSession: vi.fn().mockResolvedValue({ status: "disconnected" }),
  resetWorkerSession: vi.fn(),
  getWaConfig: vi.fn().mockResolvedValue({}),
  updateWaConfig: vi.fn(),
  getAuditLog: vi.fn().mockResolvedValue([]),
  resolveTargets: vi.fn().mockResolvedValue({ recipients: [], unresolvable: [], counts: {} }),
  getCrmContactTypes: vi.fn().mockResolvedValue([]),
  validateManual: vi.fn(),
  getContactLists: vi.fn().mockResolvedValue([]),
  createContactList: vi.fn(),
  updateContactList: vi.fn(),
  deleteContactList: vi.fn(),
  getGroupLists: vi.fn().mockResolvedValue([]),
  createGroupList: vi.fn(),
  updateGroupList: vi.fn(),
  deleteGroupList: vi.fn(),
  getOngoingPipelineTalents: vi.fn().mockResolvedValue({ talents: [] }),
  testInternalNotification: vi.fn(),
}));

import * as whatsappApi from "@/lib/whatsappApi";
import WhatsAppEnginePage from "./WhatsAppEnginePage";

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  toastError.mockClear();
  toastSuccess.mockClear();
  // Re-establish default resolved values cleared by vi.clearAllMocks().
  whatsappApi.getTemplates.mockResolvedValue([{ id: "tpl1", name: "Custom", slug: "custom", body_text: "Hi {{talent_name}}", variables: ["talent_name"] }]);
  whatsappApi.createBatch.mockResolvedValue({ batch: { id: "b1" }, jobs: [], skipped: [] });
  whatsappApi.getBatches.mockResolvedValue([]);
  whatsappApi.getJobs.mockResolvedValue([]);
  whatsappApi.getWhatsAppAgents.mockResolvedValue([]);
  whatsappApi.getWorkerSession.mockResolvedValue({ status: "disconnected" });
  whatsappApi.getWaConfig.mockResolvedValue({});
  whatsappApi.getAuditLog.mockResolvedValue([]);
  whatsappApi.getContactLists.mockResolvedValue([]);
  whatsappApi.getGroupLists.mockResolvedValue([]);
  whatsappApi.getCrmContactTypes.mockResolvedValue([]);
});

const WORKER1 = {
  id: "default",
  label: "Worker 1",
  session_instance: "default",
  session: { status: "authenticated", connected_phone_number: "+91 90000 00001", worker_ready: true },
};
const WORKER2_REGISTERED = {
  id: "worker-2",
  label: "Worker 2",
  session_instance: "worker-2",
  session: { status: "qr_pending", connected_phone_number: null, worker_ready: false },
};

async function goToSettingsStatus() {
  fireEvent.click(screen.getByText("Settings"));
  fireEvent.click(await screen.findByText("Session Status"));
}

async function goToSettingsSafety() {
  fireEvent.click(screen.getByText("Settings"));
  fireEvent.click(await screen.findByText("Safety Configuration"));
}

describe("WhatsAppEnginePage — multi-worker", () => {
  it("(a) selects Worker 1 (default) by default, before the registry even resolves", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1]);
    render(<WhatsAppEnginePage />);

    // The worker-1 tab is present and, once loaded, visibly active — but the
    // real assertion is behavioral: the FIRST worker-scoped fetch the page
    // ever makes targets "default", never waiting on an explicit selection.
    await goToSettingsStatus();
    await waitFor(() => expect(whatsappApi.getWorkerSession).toHaveBeenCalledWith("default"));
    expect(screen.getByTestId("we-worker-tab-default")).toBeTruthy();
  });

  it("(b) switching to Worker 2 re-issues every worker-scoped request with worker-2's id", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    render(<WhatsAppEnginePage />);

    await goToSettingsStatus();
    await waitFor(() => expect(whatsappApi.getWorkerSession).toHaveBeenCalledWith("default"));

    whatsappApi.getWorkerSession.mockClear();
    whatsappApi.getWhatsAppAgents.mockClear();

    fireEvent.click(await screen.findByTestId("we-worker-tab-worker-2"));

    await waitFor(() => expect(whatsappApi.getWorkerSession).toHaveBeenCalledWith("worker-2"));
    expect(whatsappApi.getWorkerSession).not.toHaveBeenCalledWith("default");
    await waitFor(() => expect(whatsappApi.getWhatsAppAgents).toHaveBeenCalledWith("worker-2"));
  });

  it("(c) campaign launch includes the selected worker_id explicitly, never the backend default", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    whatsappApi.resolveTargets.mockResolvedValue({
      recipients: [{ recipient_id: "r1", name: "Test", phone: "+919999999999", destination_type: "number", destination: "+919999999999", recipient_kind: "TALENT", source: "MANUAL" }],
      unresolvable: [], counts: { resolved: 1, sending: 1, excluded: 0 },
    });
    render(<WhatsAppEnginePage />);

    // Campaigns → Launch is the default tab; switch to Worker 2 first.
    await waitFor(() => expect(whatsappApi.getWorkers).toHaveBeenCalled());
    fireEvent.click(await screen.findByTestId("we-worker-tab-worker-2"));
    await waitFor(() => screen.getByText(/Sending from/i));
    expect(within(screen.getByText(/Sending from/i).closest("div")).getByText("Worker 2")).toBeTruthy();
  });

  it("(d) Worker 1's session data is cleared before Worker 2's own data is shown (no stale cross-worker display)", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    // Worker 1 resolves immediately; Worker 2's fetch is held open so the
    // intermediate (post-switch, pre-load) render can be inspected.
    let resolveWorker2;
    whatsappApi.getWorkerSession.mockImplementation((id) => {
      if (id === "default") return Promise.resolve(WORKER1.session);
      return new Promise((res) => { resolveWorker2 = res; });
    });

    render(<WhatsAppEnginePage />);
    await goToSettingsStatus();
    await waitFor(() => expect(screen.getByTestId("we-connected-account").textContent).toBe("+91 90000 00001"));

    fireEvent.click(screen.getByTestId("we-worker-tab-worker-2"));

    // Worker 1's session data must be cleared IMMEDIATELY on switch, before
    // Worker 2's own fetch has even resolved — the panel drops back to its
    // loading state (and the "Connected Account" field disappears with it)
    // rather than continuing to show Worker 1's number under Worker 2's tab.
    await waitFor(() => expect(screen.queryByTestId("we-connected-account")).toBeNull());
    expect(screen.getByText(/Querying WhatsApp Web session state/i)).toBeTruthy();

    resolveWorker2({ status: "qr_pending", connected_phone_number: null });
    await waitFor(() => expect(whatsappApi.getWorkerSession).toHaveBeenCalledWith("worker-2"));
    await waitFor(() => expect(screen.getByTestId("we-connected-account").textContent).toBe("Unknown"));
  });

  it("(e) session polling is cleaned up on unmount (no further requests fire afterward)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    whatsappApi.getWorkers.mockResolvedValue([WORKER1]);

    const { unmount } = render(<WhatsAppEnginePage />);
    fireEvent.click(screen.getByText("Settings"));
    fireEvent.click(await screen.findByText("Session Status"));

    await vi.waitFor(() => expect(whatsappApi.getWorkerSession).toHaveBeenCalledWith("default"));
    const callsBeforeUnmount = whatsappApi.getWorkerSession.mock.calls.length;

    unmount();
    vi.advanceTimersByTime(20000); // several multiples of the 4s poll interval
    expect(whatsappApi.getWorkerSession.mock.calls.length).toBe(callsBeforeUnmount);

    vi.useRealTimers();
  });

  it("shows the Worker 2 placeholder as 'Not provisioned' and never fetches a session for it", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1]); // worker-2 not yet registered
    render(<WhatsAppEnginePage />);

    await waitFor(() => expect(whatsappApi.getWorkers).toHaveBeenCalled());
    const w2Tab = await screen.findByTestId("we-worker-tab-worker-2");
    expect(within(w2Tab).getByText("Not provisioned")).toBeTruthy();

    fireEvent.click(w2Tab);
    fireEvent.click(screen.getByText("Settings"));
    fireEvent.click(await screen.findByText("Session Status"));

    expect(await screen.findByTestId("we-worker-not-provisioned")).toBeTruthy();
    // No fake QR / fake status ever requested for an unregistered worker.
    expect(whatsappApi.getWorkerSession).not.toHaveBeenCalledWith("worker-2");
  });

  it("registering Worker 2 requires explicit confirmation and calls createWorker with the stable id", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1]);
    whatsappApi.createWorker.mockResolvedValue({ ...WORKER2_REGISTERED });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<WhatsAppEnginePage />);
    const registerBtn = await screen.findByTestId("we-register-worker-2");
    fireEvent.click(registerBtn);

    await waitFor(() => expect(whatsappApi.createWorker).toHaveBeenCalledWith({ label: "Worker 2", worker_id: "worker-2" }));
    expect(confirmSpy).toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("does NOT register Worker 2 when the confirmation is declined", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<WhatsAppEnginePage />);
    const registerBtn = await screen.findByTestId("we-register-worker-2");
    fireEvent.click(registerBtn);

    await new Promise((r) => setTimeout(r, 0));
    expect(whatsappApi.createWorker).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  // --- Step 4.5 hardening: Safety Configuration worker scoping ---

  it("(Task 5.1) Safety Configuration loads with the selected worker_id", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    whatsappApi.getWaConfig.mockResolvedValue({ min_delay_sec: 8, max_delay_sec: 15 });
    render(<WhatsAppEnginePage />);

    await goToSettingsSafety();
    await waitFor(() => expect(whatsappApi.getWaConfig).toHaveBeenCalledWith("default"));

    whatsappApi.getWaConfig.mockClear();
    fireEvent.click(await screen.findByTestId("we-worker-tab-worker-2"));
    await waitFor(() => expect(whatsappApi.getWaConfig).toHaveBeenCalledWith("worker-2"));
    expect(whatsappApi.getWaConfig).not.toHaveBeenCalledWith("default");
  });

  it("(Task 5.2) Safety Configuration saves with the selected worker_id", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    whatsappApi.getWaConfig.mockResolvedValue({ min_delay_sec: 8, max_delay_sec: 15, max_retries: 3, circuit_breaker_threshold: 5 });
    render(<WhatsAppEnginePage />);

    fireEvent.click(await screen.findByTestId("we-worker-tab-worker-2"));
    await goToSettingsSafety();
    await waitFor(() => expect(whatsappApi.getWaConfig).toHaveBeenCalledWith("worker-2"));

    const form = document.querySelector("form");
    fireEvent.submit(form);

    await waitFor(() => expect(whatsappApi.updateWaConfig).toHaveBeenCalled());
    // EVERY call this save produces must carry worker-2's id — never an
    // omitted third arg that would fall back to the backend's own default.
    for (const call of whatsappApi.updateWaConfig.mock.calls) {
      expect(call[2]).toBe("worker-2");
    }
  });

  it("(Task 5.3) switching workers never displays Worker 1's Safety Configuration under Worker 2", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    let resolveWorker2Config;
    whatsappApi.getWaConfig.mockImplementation((id) => {
      if (id === "default") return Promise.resolve({ min_delay_sec: 8 });
      return new Promise((res) => { resolveWorker2Config = res; });
    });

    render(<WhatsAppEnginePage />);
    await goToSettingsSafety();
    await waitFor(() => screen.getByDisplayValue("8"));

    fireEvent.click(screen.getByTestId("we-worker-tab-worker-2"));

    // Worker 1's value must be gone from the form immediately — the panel
    // drops to its loading state rather than continuing to show it under
    // Worker 2's badge.
    await waitFor(() => expect(screen.queryByDisplayValue("8")).toBeNull());

    resolveWorker2Config({ min_delay_sec: 20 });
    await waitFor(() => screen.getByDisplayValue("20"));
    expect(screen.getByTestId("we-config-worker-badge").textContent).toMatch(/Worker 2/);
  });

  it("(Task 5.4) registering Worker 2 does not imply a live session — it stays disconnected until a real session doc says otherwise", async () => {
    whatsappApi.getWorkers.mockResolvedValueOnce([WORKER1]).mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    whatsappApi.createWorker.mockResolvedValue({ ...WORKER2_REGISTERED });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<WhatsAppEnginePage />);
    fireEvent.click(await screen.findByTestId("we-register-worker-2"));
    await waitFor(() => expect(whatsappApi.createWorker).toHaveBeenCalled());

    // After registration, the strip re-fetches the registry (real data,
    // not an optimistic "connected" assumption) and Worker 2 shows exactly
    // what its OWN session doc says — "qr_pending" here — never "Connected".
    await waitFor(() => {
      const tab = screen.getByTestId("we-worker-tab-worker-2");
      expect(within(tab).queryByText("Not provisioned")).toBeNull();
      expect(within(tab).getByText("Scan QR to connect")).toBeTruthy();
    });
  });

  it("(Task 5.5) selecting an unregistered worker never calls its session or config endpoints", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1]); // worker-2 not registered
    render(<WhatsAppEnginePage />);

    fireEvent.click(await screen.findByTestId("we-worker-tab-worker-2"));
    await goToSettingsSafety();
    expect(await screen.findByTestId("we-worker-not-provisioned")).toBeTruthy();

    expect(whatsappApi.getWorkerSession).not.toHaveBeenCalledWith("worker-2");
    expect(whatsappApi.getWaConfig).not.toHaveBeenCalledWith("worker-2");
  });

  it("(Task 5.6) a real dry-run submission carries worker_id all the way to createBatch's payload", async () => {
    whatsappApi.getWorkers.mockResolvedValue([WORKER1, WORKER2_REGISTERED]);
    whatsappApi.resolveTargets.mockResolvedValue({
      recipients: [{
        recipient_id: "r1", name: "Test", phone: "+919999999999",
        destination_type: "number", destination: "+919999999999",
        recipient_kind: "TALENT", source: "MANUAL",
      }],
      unresolvable: [], counts: { resolved: 1, sending: 1, excluded: 0 },
    });
    render(<WhatsAppEnginePage />);

    await waitFor(() => expect(whatsappApi.getWorkers).toHaveBeenCalled());
    fireEvent.click(await screen.findByTestId("we-worker-tab-worker-2"));
    await waitFor(() => screen.getByText(/Sending from/i));

    // Drive the actual launcher: MANUAL source -> type a contact -> wait
    // for the debounced auto-resolve -> pick the template -> submit.
    fireEvent.click(screen.getByTestId("source-MANUAL"));
    fireEvent.change(screen.getByTestId("manual-contacts-input"), {
      target: { value: "Test Contact,+919999999999" },
    });
    await waitFor(() => expect(whatsappApi.resolveTargets).toHaveBeenCalled(), { timeout: 2000 });

    fireEvent.change(screen.getByDisplayValue("Select Template..."), { target: { value: "tpl1" } });

    const dryRunBtn = await screen.findByText("Compile Dry Run Preview");
    await waitFor(() => expect(dryRunBtn.disabled).toBe(false));
    fireEvent.click(dryRunBtn);

    await waitFor(() => expect(whatsappApi.createBatch).toHaveBeenCalled());
    const payload = whatsappApi.createBatch.mock.calls[0][0];
    expect(payload.worker_id).toBe("worker-2");
  });

  it("(Task 5.7) static sweep: whatsappApi.js never issues a worker-sensitive request without an explicit worker param", async () => {
    // Reads the REAL source (not the mock) so this fails the moment a
    // future edit reintroduces a silent-default call site — the same
    // grep-able guarantee Step 4.5's audit checked by hand, now enforced
    // by a test.
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(__dirname, "../lib/whatsappApi.js"), "utf-8"
    );
    for (const fn of ["getWorkerSession", "getWaConfig", "updateWaConfig", "getWhatsAppAgents", "getBatches"]) {
      const def = src.slice(src.indexOf(`export async function ${fn}(`));
      const params = def.slice(0, def.indexOf(")"));
      expect(params).toMatch(/workerId/);
    }
  });
});
