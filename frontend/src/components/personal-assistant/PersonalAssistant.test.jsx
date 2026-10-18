import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), success: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn() },
    getAdmin: () => ({ id: "u1", role: "admin" }),
}));

import { adminApi } from "@/lib/api";
import { setAssistantContext } from "@/lib/assistantContext";
import PersonalAssistant from "./PersonalAssistant";

afterEach(() => { cleanup(); vi.useRealTimers(); delete window.SpeechRecognition; delete window.webkitSpeechRecognition; setAssistantContext({ projectId: null, talentIds: [] }); });

const W1 = { id: "default", label: "Worker 1", ready: true, status: "Connected" };
const W2 = { id: "worker-2", label: "Worker 2", ready: true, status: "Connected" };
const WDOWN = { id: "w3", label: "Worker 3", ready: false, status: "Disconnected" };

const task = (over = {}) => ({ id: "t1", command_id: "c1", seq: 0, kind: "add", status: "awaiting_confirmation", title: "Add Angela to Amazon Now", summary: ["1 talent: Angela", "Into: Amazon Now"],
    params: { talents: [], projects: [] }, slots: [], depends_on: [], warnings: [], outcomes: [], batch_ids: [], error: null, updated_at: new Date().toISOString(), ...over });

let tasksOnServer; let workers;
function server({ access = true, onMessage } = {}) {
    tasksOnServer = [];
    workers = [W1, W2, WDOWN];
    sessionStorage.clear();
    adminApi.get.mockImplementation((url) => {
        if (url === "/personal-assistant/access") return Promise.resolve({ data: { allowed: access, role: "admin" } });
        if (url === "/personal-assistant/history") return Promise.resolve({ data: { messages: [] } });
        if (url === "/personal-assistant/tasks") return Promise.resolve({ data: { tasks: tasksOnServer } });
        if (url === "/personal-assistant/workers") return Promise.resolve({ data: { workers } });
        return Promise.reject(new Error("unexpected GET " + url));
    });
    adminApi.post.mockImplementation((url, body) => {
        if (url === "/personal-assistant/message") return onMessage ? onMessage(body) : Promise.resolve({ data: { command_id: "c1", reply: "Here's what I understood: 1 waiting for your OK.", tasks: [task()], unrecognised: [] } });
        return Promise.reject(new Error("unexpected POST " + url));
    });
}
beforeEach(() => { adminApi.get.mockReset(); adminApi.post.mockReset(); });

const mount = () => render(<MemoryRouter initialEntries={["/admin/projects/proj-9"]}><PersonalAssistant /></MemoryRouter>);
const open = async () => { fireEvent.click(await screen.findByTestId("pa-launcher")); await screen.findByTestId("pa-panel"); };
const type = (v) => fireEvent.change(screen.getByLabelText("Command"), { target: { value: v } });
const posts = (u) => adminApi.post.mock.calls.filter((c) => c[0].endsWith(u));

describe("floating entry point", () => {
    it("is hidden when the backend says this account is not allowed, and no panel code runs", async () => {
        server({ access: false });
        mount();
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledWith("/personal-assistant/access"));
        expect(screen.queryByTestId("pa-launcher")).toBeNull();
    });

    it("is shown for an allowed account, closed by default, and opens / minimises / closes on demand", async () => {
        server();
        mount();
        expect(await screen.findByTestId("pa-launcher")).toBeTruthy();
        expect(screen.queryByTestId("pa-panel")).toBeNull();                      // closed by default
        await open();
        expect(screen.queryByTestId("pa-launcher")).toBeNull();
        expect(screen.getByRole("dialog", { name: "Talentgram assistant" })).toBeTruthy();
        fireEvent.click(screen.getByTestId("pa-minimize"));
        expect(screen.queryByLabelText("Command")).toBeNull();                    // collapsed to the header bar
        fireEvent.click(screen.getByTestId("pa-minimize"));
        expect(screen.getByLabelText("Command")).toBeTruthy();
        fireEvent.click(screen.getByTestId("pa-close"));
        expect(screen.queryByTestId("pa-panel")).toBeNull();
        expect(screen.getByTestId("pa-launcher")).toBeTruthy();
    });

    it("asks the backend only once per session (cached), not on every page", async () => {
        server();
        const { unmount } = mount();
        await screen.findByTestId("pa-launcher");
        unmount();
        mount();
        await screen.findByTestId("pa-launcher");
        expect(adminApi.get.mock.calls.filter((c) => c[0] === "/personal-assistant/access")).toHaveLength(1);
    });

    it("Escape closes the panel; layout hooks for mobile are present (inset on small screens, 16px input to stop iOS zoom)", async () => {
        server();
        mount();
        await open();
        const panel = screen.getByTestId("pa-panel");
        expect(panel.className).toMatch(/left-2/);
        expect(panel.className).toMatch(/sm:w-\[400px\]/);
        expect(screen.getByLabelText("Command").className).toMatch(/text-base/);
        fireEvent.keyDown(panel, { key: "Escape" });
        expect(screen.queryByTestId("pa-panel")).toBeNull();
    });
});

describe("bulk bar overlap", () => {
    const bar = (top, right) => {
        const el = document.createElement("div");
        el.setAttribute("data-pa-dock", "bottom");
        el.getBoundingClientRect = () => ({ top, right, left: 0, bottom: top + 200, width: right, height: 200 });
        document.body.appendChild(el);
        return el;
    };

    it("lifts the launcher to sit above the MEASURED top of a docked bulk bar (any height), and drops back when the selection clears", async () => {
        server();
        Object.defineProperty(window, "innerHeight", { value: 800, configurable: true });
        Object.defineProperty(window, "innerWidth", { value: 375, configurable: true });
        const el = bar(552, 366);
        setAssistantContext({ projectId: null, talentIds: ["t-a"] });
        mount();
        const launcher = await screen.findByTestId("pa-launcher");
        await waitFor(() => expect(launcher.style.bottom).toBe("260px"));            // 800 − 552 + 12
        act(() => setAssistantContext({ projectId: null, talentIds: [] }));
        await waitFor(() => expect(screen.getByTestId("pa-launcher").style.bottom).toBe(""));
        el.remove();
    });

    it("ignores a bar that does not reach the launcher's side of a wide screen", async () => {
        server();
        Object.defineProperty(window, "innerWidth", { value: 1440, configurable: true });
        const el = bar(700, 900);
        setAssistantContext({ projectId: null, talentIds: ["t-a"] });
        mount();
        const launcher = await screen.findByTestId("pa-launcher");
        expect(launcher.style.bottom).toBe("");
        el.remove();
    });
});

describe("text commands", () => {
    it("sends the text with the page context (current project from the URL + the selection on screen) and shows the reply and the task", async () => {
        server();
        setAssistantContext({ projectId: null, talentIds: ["t-a", "t-b"] });
        mount();
        await open();
        expect(screen.getByText("2 selected talents on screen")).toBeTruthy();
        type("Add these selected talents to the current project");
        fireEvent.click(screen.getByTestId("pa-send"));
        await screen.findByTestId("pa-task-t1");
        const [, body] = posts("/message")[0];
        expect(body.text).toBe("Add these selected talents to the current project");
        expect(body.context).toMatchObject({ project_id: "proj-9", talent_ids: ["t-a", "t-b"] });
        expect(body.client_request_id.length).toBeGreaterThan(8);
        expect(screen.getByText(/waiting for your OK/)).toBeTruthy();
        expect(screen.getByText("Into: Amazon Now")).toBeTruthy();
    });

    it("a failed request is reported honestly and Retry reuses the SAME request id (the server dedupes)", async () => {
        let n = 0;
        server({ onMessage: () => (++n === 1 ? Promise.reject(new Error("net")) : Promise.resolve({ data: { command_id: "c1", reply: "ok", tasks: [], unrecognised: [] } })) });
        mount();
        await open();
        type("Find Angela");
        fireEvent.click(screen.getByTestId("pa-send"));
        expect(await screen.findByText(/couldn't get an answer from the server/i)).toBeTruthy();
        fireEvent.click(screen.getByTestId("pa-retry-send"));
        await waitFor(() => expect(posts("/message")).toHaveLength(2));
        expect(posts("/message")[1][1].client_request_id).toBe(posts("/message")[0][1].client_request_id);
    });

    it("does not send an empty command, and cannot be double-submitted while one is in flight", async () => {
        let finish;
        server({ onMessage: () => new Promise((r) => { finish = () => r({ data: { command_id: "c1", reply: "done", tasks: [], unrecognised: [] } }); }) });
        mount();
        await open();
        expect(screen.getByTestId("pa-send").disabled).toBe(true);
        type("Find Angela");
        fireEvent.click(screen.getByTestId("pa-send"));
        fireEvent.click(screen.getByTestId("pa-send"));
        fireEvent.keyDown(screen.getByLabelText("Command"), { key: "Enter" });
        expect(posts("/message")).toHaveLength(1);
        await act(async () => { finish(); });
    });
});

describe("clarification cards", () => {
    const amb = () => task({ id: "t2", kind: "lookup", status: "needs_clarification", title: "Profile · Angela Kumar",
        slots: [{ key: "talent:0", kind: "talent", phrase: "Angela Kumar", prompt: 'Which "Angela Kumar" did you mean?', options: [
            { id: "a1", label: "Angela Kumar", sublabel: "27y · 5'6\" · Mumbai, India", thumb: "https://x/a1.jpg" },
            { id: "a2", label: "Angela Kumar", sublabel: "31y · Delhi, India", thumb: null }] }] });

    it("shows each candidate with photo + identifying details; tapping one sends its ID, not its name", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1 needs your pick", tasks: [amb()], unrecognised: [] } }) });
        adminApi.post.mockImplementation((url, body) => {
            if (url === "/personal-assistant/message") return Promise.resolve({ data: { command_id: "c1", reply: "1 needs your pick", tasks: [amb()], unrecognised: [] } });
            if (url === "/personal-assistant/tasks/t2/clarify") return Promise.resolve({ data: { task: task({ id: "t2", kind: "lookup", status: "completed", title: "Profile · Angela Kumar", slots: [], result: { kind: "profile", cards: [{ id: "a2", label: "Angela Kumar", sublabel: "31y", thumb: null, profile_path: "/admin/talents/a2", media_link_available: false, photos: [] }] } }) } });
            return Promise.reject(new Error(url));
        });
        mount();
        await open();
        type("Show me Angela Kumar's profile");
        fireEvent.click(screen.getByTestId("pa-send"));
        tasksOnServer = [task({ id: "t2", kind: "lookup", status: "completed", title: "Profile · Angela Kumar", slots: [], result: { kind: "profile", cards: [{ id: "a2", label: "Angela Kumar", sublabel: "31y", thumb: null, profile_path: "/admin/talents/a2", media_link_available: false, photos: [] }] } })];
        const opt1 = await screen.findByTestId("pa-option-a1");
        expect(within(opt1).getByText("27y · 5'6\" · Mumbai, India")).toBeTruthy();
        expect(opt1.querySelector("img").getAttribute("src")).toBe("https://x/a1.jpg");
        fireEvent.click(screen.getByTestId("pa-option-a2"));
        await waitFor(() => expect(posts("/clarify")).toHaveLength(1));
        expect(posts("/clarify")[0][1]).toEqual({ slot: "talent:0", choice_id: "a2" });
        expect(await screen.findByTestId("pa-card-a2")).toBeTruthy();
        expect(screen.getByText("Completed")).toBeTruthy();
    });

    it("a typed correction continues the same task", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [{ ...amb(), slots: [{ key: "talent:0", kind: "talent", phrase: "Zzz", prompt: "I couldn't find a talent matching “Zzz”.", options: [] }] }], unrecognised: [] } }) });
        adminApi.post.mockImplementationOnce(() => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [{ ...amb(), slots: [{ key: "talent:0", kind: "talent", phrase: "Zzz", prompt: "I couldn't find a talent matching “Zzz”.", options: [] }] }], unrecognised: [] } }));
        adminApi.post.mockImplementation((url) => Promise.resolve({ data: { task: task({ id: "t2", status: "completed", kind: "lookup", slots: [], result: { cards: [] } }) } }));
        mount();
        await open();
        type("Show Zzz");
        fireEvent.click(screen.getByTestId("pa-send"));
        const slot = await screen.findByTestId("pa-slot-talent:0");
        fireEvent.change(within(slot).getByPlaceholderText(/type the name again/i), { target: { value: "Angela Kumar" } });
        fireEvent.click(within(slot).getByText("Use"));
        await waitFor(() => expect(posts("/clarify")[0][1]).toEqual({ slot: "talent:0", text: "Angela Kumar" }));
    });
});

describe("confirmation, workers and statuses", () => {
    const sendTask = (over = {}) => task({ id: "s1", kind: "send", title: "Send Casting Call to Angela for Amazon Now", summary: ["Template: Casting Call", "To 1 talent: Angela"], params: { talents: [], projects: [] }, ...over });
    const mountWithSend = async (extra = {}) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1 waiting", tasks: [sendTask(extra)], unrecognised: [] } }) });
        mount();
        await open();
        type("Send the casting call to Angela");
        fireEvent.click(screen.getByTestId("pa-send"));
        await screen.findByTestId("pa-task-s1");
        tasksOnServer = [sendTask(extra)];                                       // what a refresh would return
    };

    it("shows LIVE worker availability, disables unavailable ones, never pre-picks when two are connected, and needs a choice to confirm", async () => {
        await mountWithSend();
        const w1 = await screen.findByTestId("pa-worker-default");
        expect(w1.textContent).toContain("Connected");
        expect(screen.getByTestId("pa-worker-w3").disabled).toBe(true);
        expect(screen.getByTestId("pa-worker-w3").textContent).toContain("Disconnected");
        expect(screen.getByTestId("pa-confirm").disabled).toBe(true);           // two connected → the user chooses
        fireEvent.click(screen.getByTestId("pa-worker-worker-2"));
        expect(screen.getByTestId("pa-confirm").disabled).toBe(false);
        expect(posts("/confirm")).toHaveLength(0);                                 // nothing runs before the click
    });

    it("pre-selects only when exactly one worker is connected; a 'worker 2' hint from the command is honoured", async () => {
        await mountWithSend();
        await screen.findByTestId("pa-worker-default");
        cleanup();
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1", tasks: [sendTask()], unrecognised: [] } }) });
        workers = [W1, { ...W2, ready: false, status: "Scan QR" }];
        mount(); await open(); type("send"); fireEvent.click(screen.getByTestId("pa-send")); await screen.findByTestId("pa-task-s1");
        await waitFor(() => expect(screen.getByTestId("pa-worker-default").getAttribute("aria-checked")).toBe("true"));
        cleanup();
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1", tasks: [sendTask({ params: { talents: [], projects: [], worker_hint: "worker 2" } })], unrecognised: [] } }) });
        mount(); await open(); type("send"); fireEvent.click(screen.getByTestId("pa-send")); await screen.findByTestId("pa-task-s1");
        await waitFor(() => expect(screen.getByTestId("pa-worker-worker-2").getAttribute("aria-checked")).toBe("true"));
    });

    it("no connected worker: says so and offers no way to send", async () => {
        await mountWithSend();
        workers = [WDOWN];
        cleanup();
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1", tasks: [sendTask()], unrecognised: [] } }) });
        workers = [WDOWN];
        mount(); await open(); type("send"); fireEvent.click(screen.getByTestId("pa-send")); await screen.findByTestId("pa-task-s1");
        expect(await screen.findByTestId("pa-no-worker")).toBeTruthy();
        expect(screen.getByTestId("pa-confirm").disabled).toBe(true);
    });

    it("confirm sends the explicit worker once even with repeated clicks, then shows the server's persisted status (queued ≠ sent)", async () => {
        await mountWithSend();
        let finish;
        adminApi.post.mockImplementation((url) => (url.endsWith("/confirm") ? new Promise((r) => { finish = () => r({ data: { task: sendTask({ status: "queued", status_detail: "1 waiting for the worker", batch_ids: ["b1"] }) } }); }) : Promise.reject(new Error(url))));
        fireEvent.click(await screen.findByTestId("pa-worker-default"));
        const btn = screen.getByTestId("pa-confirm");
        fireEvent.click(btn); fireEvent.click(btn); fireEvent.click(btn);
        expect(posts("/confirm")).toHaveLength(1);
        expect(posts("/confirm")[0][1]).toEqual({ worker_id: "default" });
        await act(async () => { finish(); });
        expect(await screen.findByText("Queued")).toBeTruthy();
        expect(screen.getByTestId("pa-detail").textContent).toBe("1 waiting for the worker");
        expect(screen.queryByText("Completed")).toBeNull();                       // never claims delivery
    });

    it("server refusal (worker went offline) is shown on the card and the task stays confirmable", async () => {
        await mountWithSend();
        adminApi.post.mockImplementation((url) => Promise.reject({ response: { status: 409, data: { detail: "Worker 1 is not connected to WhatsApp right now. Choose a connected worker." } } }));
        fireEvent.click(await screen.findByTestId("pa-worker-default"));
        fireEvent.click(screen.getByTestId("pa-confirm"));
        expect(await screen.findByText(/not connected to WhatsApp right now/)).toBeTruthy();
        expect(screen.getByText("Awaiting confirmation")).toBeTruthy();
    });

    it("partial results list each recipient and why; failed sends offer Retry", async () => {
        await mountWithSend({ status: "partial", summary: [], status_detail: "1 sent · 1 failed", batch_ids: ["b1"],
            outcomes: [{ talent_id: "a", name: "Angela", status: "sent", verified: true }, { talent_id: "b", name: "Riya", status: "failed", error: "number not on WhatsApp" }],
            not_queued: [{ talent_id: "c", name: "Neha", status: "not_queued", reason: "No whatsapp_group_name and no phone number" }] });
        const rows = within(screen.getByTestId("pa-outcomes"));
        expect(rows.getByText(/sent ✓ verified/)).toBeTruthy();
        expect(rows.getByText(/number not on WhatsApp/)).toBeTruthy();
        expect(rows.getByText(/No whatsapp_group_name/)).toBeTruthy();
        expect(screen.getByText("Partly sent")).toBeTruthy();
        expect(screen.getByTestId("pa-retry")).toBeTruthy();
    });

    it("confirm-all appears for several waiting tasks, and dependent steps are labelled", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "2 waiting", tasks: [task({ id: "a1" }), task({ id: "s1", seq: 1, kind: "send", title: "Send", depends_on: ["a1"] })], unrecognised: [] } }) });
        mount(); await open(); type("add and send"); fireEvent.click(screen.getByTestId("pa-send"));
        await screen.findByTestId("pa-confirm-all");
        expect(screen.getByText("Runs after the step before it succeeds.")).toBeTruthy();
        fireEvent.click(await within(screen.getByTestId("pa-confirm-all")).findByTestId("pa-worker-default"));
        adminApi.post.mockImplementation((url) => Promise.resolve({ data: { results: {} } }));
        fireEvent.click(screen.getByText("Confirm all 2"));
        await waitFor(() => expect(posts("/commands/c1/confirm-all")).toHaveLength(1));
        expect(posts("/commands/c1/confirm-all")[0][1]).toEqual({ worker_id: "default" });
    });

    it("queued work is polled from the backend (status comes from persisted state, not the browser)", async () => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "queued", tasks: [sendTask({ status: "queued", summary: [] })], unrecognised: [] } }) });
        mount(); await open(); type("send"); fireEvent.click(screen.getByTestId("pa-send"));
        await screen.findByText("Queued");
        tasksOnServer = [sendTask({ status: "completed", summary: [], status_detail: "1 sent" })];
        await act(async () => { await vi.advanceTimersByTimeAsync(4200); });
        expect(await screen.findByText("Sent")).toBeTruthy();                      // a SEND that the backend reports finished reads "Sent", not just "Completed"
    });

    it("the Tasks tab lists persisted tasks after a refresh (history + tasks load from the backend)", async () => {
        server();
        adminApi.get.mockImplementation((url) => {
            if (url === "/personal-assistant/access") return Promise.resolve({ data: { allowed: true } });
            if (url === "/personal-assistant/history") return Promise.resolve({ data: { messages: [{ id: "m1", role: "user", text: "Add Angela" }, { id: "m2", role: "assistant", text: "1 waiting", command_id: "c1" }] } });
            if (url === "/personal-assistant/tasks") return Promise.resolve({ data: { tasks: [task({ status: "completed", summary: [] })] } });
            return Promise.resolve({ data: { workers: [] } });
        });
        mount(); await open();
        expect(await screen.findByText("Add Angela")).toBeTruthy();
        expect(await screen.findByTestId("pa-task-t1")).toBeTruthy();             // the card is back under its command
        fireEvent.click(screen.getByTestId("pa-tab-tasks"));
        expect(within(screen.getByTestId("pa-queue")).getByText("Add Angela to Amazon Now")).toBeTruthy();
    });
});

describe("honest wording and failure handling", () => {
    const send = (over = {}) => task({ id: "s1", kind: "send", title: "Send Casting Call to Angela", summary: [], params: { talents: [], projects: [] }, ...over });
    const show = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("send"); fireEvent.click(screen.getByTestId("pa-send")); await screen.findByTestId(`pa-task-${t.id}`);
    };

    it.each([["queued", "Queued"], ["running", "Sending"], ["completed", "Sent"], ["partial", "Partly sent"], ["failed", "Failed"], ["cancelled", "Cancelled"]])(
        "a WhatsApp send in state %s reads %s — never 'Completed' unless it was actually sent", async (status, label) => {
            await show(send({ status }));
            expect(within(screen.getByTestId("pa-task-s1")).getByTestId("pa-status").textContent).toBe(label);
        });

    it("an interrupted send reads Uncertain (not Failed) and offers no Retry, because it may already have gone out", async () => {
        await show(send({ status: "failed", uncertain: true, error: "This was interrupted — I can't tell whether it was queued. Check WhatsApp Engine → campaign history for this project first." }));
        const card = screen.getByTestId("pa-task-s1");
        expect(within(card).getByTestId("pa-status").textContent).toBe("Uncertain");
        expect(within(card).queryByTestId("pa-retry")).toBeNull();
        expect(within(card).getByRole("alert").textContent).toMatch(/campaign history/i);
    });

    it("a clean failure still reads Failed and still offers Retry; an uncertain ADD may be retried (idempotent)", async () => {
        await show(send({ status: "failed", batch_ids: ["b1"] }));
        expect(within(screen.getByTestId("pa-task-s1")).getByTestId("pa-status").textContent).toBe("Failed");
        expect(within(screen.getByTestId("pa-task-s1")).getByTestId("pa-retry")).toBeTruthy();
        cleanup();
        await show(task({ id: "a2", kind: "add", status: "failed", uncertain: true, summary: [] }));
        expect(within(screen.getByTestId("pa-task-a2")).getByTestId("pa-status").textContent).toBe("Uncertain");
        expect(within(screen.getByTestId("pa-task-a2")).getByTestId("pa-retry")).toBeTruthy();
    });

    it("non-send tasks keep the plain wording", async () => {
        await show(task({ id: "a1", kind: "add", status: "completed", summary: [] }));
        expect(within(screen.getByTestId("pa-task-a1")).getByTestId("pa-status").textContent).toBe("Completed");
    });

    it("shows which worker a send used", async () => {
        await show(send({ status: "queued", params: { talents: [], projects: [], worker_id: "worker-2", worker_label: "Worker 2" } }));
        expect(screen.getByTestId("pa-worker-used").textContent).toBe("Worker: Worker 2");
    });

    it("a confirm that times out says it may still have run, refreshes from the server, and does not double-run", async () => {
        await show(send({ status: "awaiting_confirmation", summary: ["Template: Casting Call"] }));
        tasksOnServer = [send({ status: "queued", status_detail: "1 waiting for the worker", batch_ids: ["b1"] })];     // the server DID queue it
        adminApi.post.mockImplementation((url) => Promise.reject(Object.assign(new Error("timeout of 45000ms exceeded"), { code: "ECONNABORTED" })));
        fireEvent.click(await screen.findByTestId("pa-worker-default"));
        fireEvent.click(screen.getByTestId("pa-confirm"));
        expect(await screen.findByText(/may still have run/i)).toBeTruthy();
        await waitFor(() => expect(within(screen.getByTestId("pa-task-s1")).getByTestId("pa-status").textContent).toBe("Queued"));   // truth comes from the persisted task
        expect(posts("/confirm")).toHaveLength(1);
    });

    it("every API call has a client-side timeout so nothing spins forever", async () => {
        await show(send({ status: "awaiting_confirmation", summary: [] }));
        const [, , cfg] = posts("/message")[0];
        expect(cfg).toEqual({ timeout: 45000 });
    });

    it("a repeat of an already-queued send is flagged and the button says so", async () => {
        await show(send({ status: "awaiting_confirmation", duplicate_risk: true, warnings: ["Already queued…"], summary: ["Template: Casting Call", "Already queued or sent in the last 24 h with this template for this project: Angela — sending again would message them twice."] }));
        expect(screen.getByTestId("pa-confirm").textContent).toContain("Send again anyway");
        expect(screen.getByTestId("pa-preview").textContent).toContain("would message them twice");
    });

    it("a share shows recipient, content and (after confirmation) worker before/after it runs", async () => {
        await show(task({ id: "sh1", kind: "share", status: "awaiting_confirmation", title: "Share media link of Angela with Casty Person",
            summary: ["Recipient: Casty Person (+91 ••••• 11122) — saved CRM contact", "Content: media link of 1 talent:", "  • Angela — their media download link (created when you confirm)"] }));
        const p = screen.getByTestId("pa-preview").textContent;
        expect(p).toContain("Casty Person"); expect(p).toContain("••••• 11122"); expect(p).toContain("media download link");
        expect(screen.getByTestId("pa-confirm").disabled).toBe(true);              // a worker must be chosen
    });

    it("a recipient question offers the saved contacts as tappable options and sends only the picked id", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1 needs your pick", unrecognised: [], tasks: [task({ id: "sh2", kind: "share", status: "needs_clarification", title: "Share profile of Angela with …",
            slots: [{ key: "recipient", kind: "recipient", phrase: "", prompt: "Which Casting Director? (saved CRM contacts)", options: [{ id: "crm:abc", label: "Priya Nair", sublabel: "Casting Director · +91 ••••• 11122" }] }] })] } }) });
        mount(); await open(); type("share"); fireEvent.click(screen.getByTestId("pa-send"));
        const opt = await screen.findByTestId("pa-option-crm:abc");
        expect(opt.textContent).toContain("Casting Director");
        adminApi.post.mockImplementation((url, body) => Promise.resolve({ data: { task: task({ id: "sh2", kind: "share", status: "awaiting_confirmation", slots: [] }) } }));
        fireEvent.click(opt);
        await waitFor(() => expect(posts("/clarify")[0][1]).toEqual({ slot: "recipient", choice_id: "crm:abc" }));
    });
});

describe("voice input", () => {
    it("says plainly when the browser has no speech recognition, and the text box still works", async () => {
        server();
        mount(); await open();
        expect(screen.getByTestId("pa-voice-unsupported")).toBeTruthy();
        expect(screen.getByTestId("pa-mic").disabled).toBe(true);
        type("Find Angela");
        expect(screen.getByTestId("pa-send").disabled).toBe(false);
    });

    function fakeRecognition() {
        const inst = {};
        window.webkitSpeechRecognition = function Rec() { Object.assign(this, { start: vi.fn(() => { inst.started = true; }), stop: vi.fn(() => this.onend?.()), abort: vi.fn() }); inst.rec = this; };
        return inst;
    }

    it("tap to listen shows the indicator, puts the words in the text box for review, and does NOT send by itself", async () => {
        server();
        const inst = fakeRecognition();
        mount(); await open();
        fireEvent.click(screen.getByTestId("pa-mic"));
        expect(inst.started).toBe(true);
        expect(screen.getByTestId("pa-listening")).toBeTruthy();
        expect(inst.rec.continuous).toBe(false);                                   // push-to-talk, never continuous
        act(() => { inst.rec.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: "show me angela kumar's profile" }], { isFinal: true })] }); });
        act(() => { inst.rec.onend(); });
        expect(screen.getByLabelText("Command").value).toBe("show me angela kumar's profile");
        expect(posts("/message")).toHaveLength(0);                                 // the user reviews / edits, then sends
        expect(screen.queryByTestId("pa-listening")).toBeNull();
    });

    it("tapping again stops listening", async () => {
        server();
        const inst = fakeRecognition();
        mount(); await open();
        fireEvent.click(screen.getByTestId("pa-mic"));
        fireEvent.click(screen.getByTestId("pa-mic"));
        expect(inst.rec.stop).toHaveBeenCalled();
    });

    it.each([["not-allowed", /blocked/i], ["no-speech", /didn't hear/i], ["network", /network/i], ["audio-capture", /No microphone/i]])("handles the %s error with a useful message and keeps the keyboard fallback", async (code, re) => {
        server();
        const inst = fakeRecognition();
        mount(); await open();
        fireEvent.click(screen.getByTestId("pa-mic"));
        act(() => { inst.rec.onerror({ error: code }); inst.rec.onend(); });
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(re);
        type("typed instead");
        expect(screen.getByLabelText("Command").value).toBe("typed instead");
    });
});
