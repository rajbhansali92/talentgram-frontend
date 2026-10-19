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
import { liveReply } from "./AssistantPanel";

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
        if (url === "/personal-assistant/access") return Promise.resolve({ data: { allowed: access } });
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

describe("a question about a talent's projects shows the answer, not a profile", () => {
    const card = (over = {}) => ({
        id: "tal-1", label: "Angela Kumar", sublabel: "28y · Mumbai", thumb: "http://x/t.jpg", profile_path: "/admin/talents/tal-1",
        portfolio_url: "https://folio.example/angela", video_url: "https://video.example/angela", photo_count: 2,
        photos: [{ thumb: "http://x/p1.jpg", url: "http://x/p1.jpg" }, { thumb: "http://x/p2.jpg", url: "http://x/p2.jpg" }],
        history: { projects: [{ project_id: "p1", project: "Amazon Now", stage: "Approved", since: "2026-09-01" }, { project_id: "p2", project: "Giva", stage: "Follow Up", since: "2026-09-02" }],
                   submissions: [{ project_id: "p1", project: "Amazon Now", status: "submitted", date: "2026-09-03" }] },
        tested_projects: [], ...over });
    const lookup = (kind, c, title) => task({ id: "q1", kind: "lookup", status: "completed", title, summary: [], result: { kind, cards: [c] } });
    const show = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("q"); fireEvent.click(screen.getByTestId("pa-send")); return screen.findByTestId("pa-task-q1");
    };

    it("lists the projects she is marked Already Tested for, without the photo strip, portfolio, video or share prompt", async () => {
        const el = await show(lookup("tested", card({ tested_projects: [{ project: "Amazon Now" }, { project: "Giva" }], submitted_projects: [] }), "Projects tested · Angela Kumar"));
        expect(el.getAttribute("aria-label")).toBe("Projects tested · Angela Kumar");
        expect(within(el).getByTestId("pa-tested-list").textContent).toBe("Tested for 2 projects: Amazon Now, Giva");
        expect(within(el).queryByTestId("pa-photos")).toBeNull();
        expect(within(el).queryByText(/Portfolio/)).toBeNull();
        expect(within(el).queryByText(/Intro video/)).toBeNull();
        expect(within(el).queryByText(/Share this/)).toBeNull();
        expect(within(el).getByText(/Open profile/)).toBeTruthy();                                // still easy to reach her profile
    });

    it("an EMPTY tested answer says so explicitly and lists the projects she has submitted for instead of going blank", async () => {
        const el = await show(lookup("tested", card({ submitted_projects: [{ project: "Amazon Now", status: "submitted", date: "2026-09-03" }, { project: "Giva", status: "updated", date: "2026-09-02" }] }), "Projects tested · Angela Kumar"));
        expect(within(el).getByTestId("pa-tested-none").textContent).toBe("No project is marked “Already Tested” for Angela Kumar.");
        expect(within(el).getByTestId("pa-submitted-head").textContent).toBe("Has submitted for 2 projects:");
        expect(el.textContent).toContain("Giva");
        expect(el.textContent).toContain("updated · 2026-09-02");
        expect(within(el).queryByText(/Not in any project yet/)).toBeNull();
        expect(within(el).queryByTestId("pa-history")).toBeNull();                                // not the long pipeline list for a tested question
        expect(el.textContent).toContain("“Tested” means the project's pipeline stage is Already Tested");
    });

    it("no tested projects and no submissions → both facts stated, never an empty card", async () => {
        const el = await show(lookup("tested", card({ submitted_projects: [] }), "Projects tested · Angela Kumar"));
        expect(within(el).getByTestId("pa-tested-none")).toBeTruthy();
        expect(within(el).getByTestId("pa-submitted-none").textContent).toBe("No submissions on record either.");
    });

    it("'projects she is in' keeps its pipeline list but is also not a profile; a PROFILE lookup still shows the media", async () => {
        const inProj = await show(lookup("projects", card(), "Projects of Angela Kumar"));
        expect(within(inProj).getByTestId("pa-history").textContent).toContain("Amazon Now");
        expect(within(inProj).queryByTestId("pa-photos")).toBeNull();
        cleanup();
        const prof = await show(lookup("profile", card({ history: undefined }), "Profile · Angela Kumar"));
        expect(within(prof).getByTestId("pa-photos")).toBeTruthy();
        expect(within(prof).getByText(/Portfolio/)).toBeTruthy();
        expect(within(prof).getByText(/Intro video/)).toBeTruthy();
        expect(within(prof).getByText(/Share this/)).toBeTruthy();
    });
});

describe("send preview destinations", () => {
    const send = (over = {}) => task({ id: "d1", kind: "send", status: "awaiting_confirmation", title: "Send Casting Call to Rivka, Gita, Nolan", params: { talents: [], projects: [], template_name: "Casting Call" }, ...over });
    const show = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("send"); fireEvent.click(screen.getByTestId("pa-send")); await screen.findByTestId(`pa-task-${t.id}`);
    };

    it("lists each recipient with a masked number or the WhatsApp group, and flags one with no destination as skipped", async () => {
        await show(send({ summary: [
            "Template: Casting Call", "To 3 talents: Rivka, Gita, Nolan",
            "  • Rivka → WhatsApp 91 ••••• 00000", "  • Gita → WhatsApp group “Gita Casting Group”", "  • Nolan → no WhatsApp number or group on file — will be skipped",
            "Project: Streaming Now", "Sent through the existing WhatsApp queue — pick the worker below."] }));
        const preview = screen.getByTestId("pa-preview");
        expect(preview.textContent).toContain("Rivka → WhatsApp 91 ••••• 00000");
        expect(preview.textContent).toContain("Gita → WhatsApp group “Gita Casting Group”");
        expect(preview.textContent).not.toMatch(/\d{10,}/);                                    // never a full number
        const skipped = [...preview.querySelectorAll("li")].find((li) => li.textContent.includes("Nolan →"));
        expect(skipped.className).toContain("text-amber-700");                                  // visibly flagged
        expect(screen.getByTestId("pa-confirm").disabled).toBe(true);                           // still needs a worker chosen first
    });

    it("a send with nowhere to go is shown as failed with the reason and offers no Confirm", async () => {
        await show(send({ status: "failed", summary: [], error: "That talent has no WhatsApp number or group on file, so there is nowhere to send this. Add one on the talent's profile first." }));
        const card = screen.getByTestId("pa-task-d1");
        expect(within(card).getByTestId("pa-status").textContent).toBe("Failed");
        expect(within(card).getByRole("alert").textContent).toMatch(/no WhatsApp number or group on file/);
        expect(within(card).queryByTestId("pa-confirm")).toBeNull();
    });
});

describe("Tasks tab worker naming", () => {
    const showTasks = async (rows) => {
        server();
        tasksOnServer = rows;
        mount(); await open();
        fireEvent.click(screen.getByTestId("pa-tab-tasks"));
        return screen.findByTestId("pa-queue");
    };
    const row = (over) => task({ id: "w1", kind: "send", status: "failed", title: "Send Casting Call to Angela", summary: [], ...over });

    it("shows the worker's friendly label saved with the task and does not ask the registry", async () => {
        const q = await showTasks([row({ params: { talents: [], projects: [], worker_id: "worker-2", worker_label: "Worker 2" } })]);
        expect(q.textContent).toContain("Worker: Worker 2");
        expect(adminApi.get).not.toHaveBeenCalledWith("/personal-assistant/workers");
    });

    it("names a task saved before labels existed from the live worker registry instead of the raw id", async () => {
        const q = await showTasks([row({ params: { talents: [], projects: [], worker_id: "default" } })]);
        await waitFor(() => expect(q.textContent).toContain("Worker: Worker 1"));
        expect(q.textContent).not.toContain("Worker: default");
    });

    it("falls back to the raw id (never a guess) when the registry cannot be read", async () => {
        server();
        tasksOnServer = [row({ params: { talents: [], projects: [], worker_id: "default" } })];
        const base = adminApi.get.getMockImplementation();
        adminApi.get.mockImplementation((url) => (url === "/personal-assistant/workers" ? Promise.reject(new Error("down")) : base(url)));
        mount(); await open();
        fireEvent.click(screen.getByTestId("pa-tab-tasks"));
        const q = await screen.findByTestId("pa-queue");
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledWith("/personal-assistant/workers"));
        expect(q.textContent).toContain("Worker: default");
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
        fireEvent.click(within(slot).getByText("Search"));
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

    it("several waiting actions get ONE plan with an approval; dependent steps are labelled, and the worker is sent with it", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "2 waiting", tasks: [task({ id: "a1", title: "Add Angela" }), task({ id: "s1", seq: 1, kind: "send", title: "Send", depends_on: ["a1"] })], unrecognised: [] } }) });
        mount(); await open(); type("add and send"); fireEvent.click(screen.getByTestId("pa-send"));
        const plan = await screen.findByTestId("pa-plan");
        expect(within(plan).getByTestId("pa-plan-steps").textContent).toMatch(/Send — after step 1, only if it succeeds/);
        expect(screen.queryAllByTestId("pa-confirm")).toHaveLength(0);                       // the chained steps are approved only through the plan
        fireEvent.click(await within(plan).findByTestId("pa-worker-default"));
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { results: {} } }));
        fireEvent.click(within(plan).getByTestId("pa-plan-approve"));
        await waitFor(() => expect(posts("/commands/c1/confirm-all")).toHaveLength(1));
        expect(posts("/commands/c1/confirm-all")[0][1]).toEqual({ worker_id: "default" });
    });

    it("the approval carries the fingerprint of exactly what was shown (one task and a whole plan)", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "2", tasks: [task({ id: "m1", kind: "move", fingerprint: "fp-m1" }), task({ id: "m2", seq: 1, kind: "move", fingerprint: "fp-m2" })], unrecognised: [] } }) });
        mount(); await open(); type("two moves"); fireEvent.click(screen.getByTestId("pa-send"));
        const plan = await screen.findByTestId("pa-plan");
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { results: {} } }));
        fireEvent.click(within(plan).getByTestId("pa-plan-approve"));
        await waitFor(() => expect(posts("/commands/c1/confirm-all")).toHaveLength(1));
        expect(posts("/commands/c1/confirm-all")[0][1]).toEqual({ fingerprints: { m1: "fp-m1", m2: "fp-m2" } });
        const one = task({ id: "m3", command_id: "c9", kind: "move", fingerprint: "fp-m3" });
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c9", reply: "1", tasks: [one], unrecognised: [] } }) });
        cleanup(); mount(); await open(); type("one move"); fireEvent.click(screen.getByTestId("pa-send"));
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: { ...one, status: "completed" } } }));
        fireEvent.click(await screen.findByTestId("pa-confirm"));
        await waitFor(() => expect(posts("/tasks/m3/confirm")).toHaveLength(1));
        expect(posts("/tasks/m3/confirm")[0][1]).toEqual({ fingerprint: "fp-m3" });
    });

    it("if the plan changed after it was shown, NOTHING ran: the revised action is shown with the reason, ready for a fresh approval", async () => {
        const a = task({ id: "s1", kind: "share", fingerprint: "old", title: "Share media link of Angela with the Amazon Now WhatsApp group", summary: ["Recipient: WhatsApp group “OLD GROUP” of Amazon Now"] });
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1", tasks: [a], unrecognised: [] } }) });
        mount(); await open(); type("share"); fireEvent.click(screen.getByTestId("pa-send"));
        await screen.findByTestId("pa-approve-head");
        const revised = { ...a, fingerprint: "new", summary: ["Recipient: WhatsApp group “NEW GROUP” of Amazon Now"] };
        tasksOnServer = [revised];                                                              // the server saved the revised task
        adminApi.post.mockImplementation(() => Promise.reject({ response: { status: 409, data: { detail: { changed: true, message: "Something changed since this was planned, so I did not run it.", tasks: [revised] } } } }));
        fireEvent.click(await within(await screen.findByTestId("pa-task-s1")).findByTestId("pa-worker-default"));
        fireEvent.click(screen.getByTestId("pa-confirm"));
        expect((await screen.findAllByText(/did not run it/)).length).toBeGreaterThan(0);
        expect(screen.getByTestId("pa-preview").textContent).toMatch(/NEW GROUP/);
        expect(screen.getByTestId("pa-preview").textContent).not.toMatch(/OLD GROUP/);
        expect(screen.getByTestId("pa-confirm")).toBeTruthy();                                 // still waiting for a fresh approval
    });

    it("tells 'who do you mean' (identity only) apart from 'approve this action' — and the header status follows the CURRENT request", async () => {
        const stale = task({ id: "old1", command_id: "c0", kind: "lookup", status: "needs_clarification", created_at: "2026-10-19T08:00:00Z", title: "Look up", summary: [], slots: [{ key: "talent:0", kind: "talent", prompt: "Which?", options: [] }] });
        const mv = task({ id: "mv1", command_id: "c1", kind: "move", status: "needs_clarification", created_at: "2026-10-19T09:00:00Z", title: "Move Soumya", summary: [],
            slots: [{ key: "talent:0", kind: "talent", prompt: "Which Soumya?", options: [{ id: "t-1", label: "Soumya Shukla", sublabel: "27y" }] }] });
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1 needs your pick", tasks: [mv], unrecognised: [] } }) });
        tasksOnServer = [stale];
        mount(); await open();
        await waitFor(() => expect(screen.getByTestId("pa-state").textContent).toMatch(/Needs your pick/));
        type("Move Soumya Shukla to Approved in Amazon Now"); fireEvent.click(screen.getByTestId("pa-send"));
        expect(await screen.findByTestId("pa-identify-head")).toBeTruthy();
        expect(screen.getByTestId("pa-identify-head").textContent).toMatch(/Who do you mean\?.*nothing happens until you approve/);
        expect(screen.queryByTestId("pa-approve-head")).toBeNull();
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: { ...mv, status: "awaiting_confirmation", fingerprint: "f", slots: [], summary: ["1 talent: Soumya Shukla", "To Approved in Amazon Now"] } } }));
        adminApi.get.mockImplementation((url) => Promise.resolve({ data: url.endsWith("/tasks") ? { tasks: [{ ...mv, status: "awaiting_confirmation", slots: [] }, stale] } : url.endsWith("/workers") ? { workers } : { messages: [] } }));
        fireEvent.click(await screen.findByTestId("pa-option-t-1"));
        const head = await screen.findByTestId("pa-approve-head");
        expect(head.textContent).toMatch(/Approve this action\?.*Nothing has been changed yet/);
        expect(screen.queryByTestId("pa-identify-head")).toBeNull();
        // the old, unanswered lookup no longer decides the header: this request is waiting for OK, the old one is a quiet note
        expect(screen.getByTestId("pa-state").textContent).toBe("Waiting for your OK · 1 older request still open");
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

describe("entity resolution cards (2026-10-19)", () => {
    const slot = (over = {}) => ({ key: "talent:0", kind: "talent", phrase: "Vidushi Tiwari", prompt: 'Which "Vidushi Tiwari" did you mean?', options: [
        { id: "v1", label: "Viidushi Tiwari", sublabel: "26y · 5'5\" · Mumbai, India · @viid", thumb: null, best: true, match: "close" }], ...over });
    const moveTask = (over = {}) => task({ id: "m1", kind: "move", status: "needs_clarification", title: "Move ? to Hold in Sebamed", source_text: "Move Vidushi Tiwari to Hold in Sebamed", slots: [slot()], ...over });
    const start = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "Here's what I understood: 1 needs your pick.", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("Move Vidushi Tiwari to Hold in Sebamed"); fireEvent.click(screen.getByTestId("pa-send"));
        return screen.findByTestId("pa-slot-talent:0");
    };

    it("shows the actual database candidate with its details, flags the best match, echoes the original command, and never shows an empty 'Use' button", async () => {
        const el = await start(moveTask());
        expect(within(el).getByText("Viidushi Tiwari")).toBeTruthy();
        expect(within(el).getByText(/26y · 5'5" · Mumbai, India · @viid/)).toBeTruthy();
        expect(within(el).getByTestId("pa-best-v1").textContent).toBe("Best match");
        expect(within(el).getByTestId("pa-command-echo").textContent).toContain("Move Vidushi Tiwari to Hold in Sebamed");
        expect(within(el).queryByText("Use")).toBeNull();
        expect(within(el).getByText("Search")).toBeTruthy();
    });

    it("tapping a card marks it Selected at once and sends only its ID; the stale 'needs your pick' line becomes 'waiting for your OK' when the server says so", async () => {
        const el = await start(moveTask());
        let release;
        const pending = new Promise((r) => { release = r; });                                // the save is "in flight" until we say so (no timing luck)
        adminApi.post.mockImplementation((url) => {
            if (url === "/personal-assistant/tasks/m1/clarify") return pending.then(() => ({ data: { task: moveTask({ status: "awaiting_confirmation", title: "Move Viidushi Tiwari to Hold in Sebamed", slots: [], summary: ["1 talent: Viidushi Tiwari", "To Hold in Sebamed"] }) } }));
            return Promise.reject(new Error(url));
        });
        tasksOnServer = [moveTask({ status: "awaiting_confirmation", title: "Move Viidushi Tiwari to Hold in Sebamed", slots: [], summary: ["1 talent: Viidushi Tiwari", "To Hold in Sebamed"] })];
        fireEvent.click(within(el).getByTestId("pa-option-v1"));
        expect(within(el).getByTestId("pa-option-v1").getAttribute("aria-pressed")).toBe("true");     // visibly Selected while it is being saved
        await waitFor(() => expect(posts("/clarify")).toHaveLength(1));
        release();
        expect(posts("/clarify")[0][1]).toEqual({ slot: "talent:0", choice_id: "v1" });
        await waitFor(() => expect(screen.getByTestId("pa-reply").textContent).toBe("Here's what I understood: 1 waiting for your OK."));
        expect(screen.queryByText(/needs your pick/i)).toBeNull();                         // nothing stale left on screen
        expect(screen.getAllByTestId("pa-confirm")).toHaveLength(1);                         // exactly ONE approval for the action
    });

    it("a typed correction is a SEARCH: the server's cards come back with a 'closest matches' note — typing alone never selects", async () => {
        const el = await start(moveTask({ slots: [slot({ options: [], prompt: 'I couldn\'t find anyone matching "Vdsh Tw". Check the spelling, or search with part of the name.' })] }));
        expect(within(el).getByTestId("pa-no-match").textContent).toMatch(/try another spelling/i);
        adminApi.post.mockImplementation((url) => Promise.resolve({ data: { task: moveTask() } }));
        tasksOnServer = [moveTask()];
        fireEvent.change(within(el).getByPlaceholderText(/type the name again/i), { target: { value: "Vidushi Tiwari" } });
        fireEvent.click(within(el).getByText("Search"));
        await waitFor(() => expect(posts("/clarify")[0][1]).toEqual({ slot: "talent:0", text: "Vidushi Tiwari" }));
        await waitFor(() => expect(screen.getByTestId("pa-search-note").textContent).toMatch(/closest matches for “Vidushi Tiwari”/i));
        expect(screen.getByTestId("pa-option-v1")).toBeTruthy();
        expect(screen.getByTestId("pa-task-m1").textContent).toMatch(/Needs your pick/);       // still unresolved until a card is tapped
    });

    it("project cards show their real status (Ongoing / Complete) and the proposed one is flagged; nothing is chosen for the user", async () => {
        const t = task({ id: "p1", kind: "send", status: "needs_clarification", title: "Send Casting Call", source_text: "send the casting for sebamed", slots: [{ key: "project:0", kind: "project", phrase: "Sibamed Trend", prompt: 'Which project did you mean by "Sibamed Trend"?', options: [
            { id: "pr1", label: "Sebamed - Trend film", sublabel: "Ongoing", status: "ongoing", best: true },
            { id: "pr2", label: "Sebamed - Old film", sublabel: "Complete", status: "complete" }] }] });
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("x"); fireEvent.click(screen.getByTestId("pa-send"));
        const el = await screen.findByTestId("pa-slot-project:0");
        expect(within(within(el).getByTestId("pa-option-pr1")).getByText("Ongoing")).toBeTruthy();
        expect(within(within(el).getByTestId("pa-option-pr2")).getByText("Complete")).toBeTruthy();
        expect(within(el).getByTestId("pa-best-pr1")).toBeTruthy();
        expect(within(el).queryByTestId("pa-best-pr2")).toBeNull();
        expect(screen.queryAllByTestId("pa-confirm")).toHaveLength(0);
    });
});

describe("combined 'add to the pipeline, then send' plan (2026-10-19)", () => {
    const add = (over = {}) => task({ id: "a1", seq: 0, kind: "add", status: "awaiting_confirmation", title: "First: add Kripa Trivedi to Sebamed - Trend film",
        summary: ["Step 1 of 2 — only the missing pipeline entries are created:", "  • Kripa Trivedi → Sebamed - Trend film (not in the pipeline yet)"], params: { talents: [], projects: [], prereq_for: "s1" }, ...over });
    const send = (over = {}) => task({ id: "s1", seq: 1, kind: "send", status: "awaiting_confirmation", title: "Send Casting Call to Kripa Trivedi for Sebamed - Trend film", depends_on: ["a1"],
        summary: ["Step 2 of 2 — runs only after step 1 succeeds. Not in the pipeline yet: Kripa Trivedi"], params: { talents: [], projects: [], prereq_task: "a1", strict_dependency: true }, ...over });
    const begin = async (tasks) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "Here's what I understood: 2 waiting for your OK.", tasks, unrecognised: [] } }) });
        mount(); await open(); type("send the casting"); fireEvent.click(screen.getByTestId("pa-send"));
        return screen.findByTestId("pa-plan");
    };

    it("shows ONE combined approval that lists both steps in order, with the worker choice, and no separate Confirm buttons on the two cards", async () => {
        const plan = await begin([add(), send()]);
        const steps = within(plan).getAllByRole("listitem").map((l) => l.textContent);
        expect(steps[0]).toMatch(/First: add Kripa Trivedi/);
        expect(steps[1]).toMatch(/Send Casting Call.*after step 1, only if it succeeds/);
        expect(await within(plan).findByTestId("pa-workers")).toBeTruthy();
        expect(screen.queryAllByTestId("pa-confirm")).toHaveLength(0);
        expect(within(plan).getByTestId("pa-plan-approve").disabled).toBe(true);                // a worker must be chosen (two are connected: never guessed)
        expect(screen.getByText(/Not in the pipeline yet: Kripa Trivedi/)).toBeTruthy();         // what is missing is spelled out
    });

    it("approving sends ONE plan approval; the server runs step 1 first and step 2 only after step 1 succeeds", async () => {
        const plan = await begin([add(), send()]);
        adminApi.post.mockImplementation((url) => {
            if (url.endsWith("/commands/c1/confirm-all")) return Promise.resolve({ data: { results: { a1: { task: add({ status: "completed" }) }, s1: { task: send({ status: "queued" }) } } } });
            return Promise.reject(new Error(url));
        });
        fireEvent.click(await within(plan).findByTestId("pa-worker-default"));
        fireEvent.click(within(plan).getByTestId("pa-plan-approve"));
        await waitFor(() => expect(posts("/commands/c1/confirm-all")).toHaveLength(1));
        expect(posts("/commands/c1/confirm-all")[0][1]).toEqual({ worker_id: "default" });
        expect(posts("/tasks/a1/confirm")).toHaveLength(0);                                    // ordering is the server's job now, not a browser loop
        expect(posts("/tasks/s1/confirm")).toHaveLength(0);
    });

    it("if step 1 only partly worked, the server does not run the send: the reason is shown on its card and the user is told it was not run", async () => {
        const plan = await begin([add(), send()]);
        const { toast } = await import("sonner");
        toast.error.mockClear();
        tasksOnServer = [add({ status: "partial", error: "Couldn't add everyone to: Sebamed - Trend film" }), send()];
        adminApi.post.mockImplementation((url) => {
            if (url.endsWith("/commands/c1/confirm-all")) return Promise.resolve({ data: { results: {
                a1: { task: add({ status: "partial", error: "Couldn't add everyone to: Sebamed - Trend film" }) },
                s1: { task: send(), error: "Step 1 only partly succeeded, so this send did not run." } } } });
            return Promise.reject(new Error(url));
        });
        fireEvent.click(await within(plan).findByTestId("pa-worker-default"));
        fireEvent.click(within(plan).getByTestId("pa-plan-approve"));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/not run/i)));
        expect((await screen.findAllByText(/this send did not run/)).length).toBeGreaterThan(0);
        expect(posts("/tasks/s1/confirm")).toHaveLength(0);
    });

    it("Cancel plan cancels the whole command in ONE request and sends nothing", async () => {
        const plan = await begin([add(), send()]);
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { tasks: [], cancelled: 2, still_running: 0 } }));
        fireEvent.click(within(plan).getByTestId("pa-plan-cancel"));
        await waitFor(() => expect(posts("/commands/c1/cancel")).toHaveLength(1));
        expect(posts("/confirm")).toHaveLength(0);
        expect(posts("/confirm-all")).toHaveLength(0);
    });

    it("after step 1 has failed or partly worked the plan banner is gone: the send keeps its own Confirm (with worker) and the add shows Retry + per-project outcomes", async () => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", unrecognised: [], tasks: [
            add({ status: "partial", error: "Couldn't add everyone to: Sebamed - Trend film. What was added stays added; retry to finish the rest.", outcomes: [{ name: "Kripa Trivedi", project: "Sebamed - Testimonial film", status: "added" }, { name: "Kripa Trivedi", project: "Sebamed - Trend film", status: "failed", reason: "boom" }] }), send()] } }) });
        mount(); await open(); type("x"); fireEvent.click(screen.getByTestId("pa-send"));
        await screen.findByTestId("pa-retry");
        expect(screen.queryByTestId("pa-plan")).toBeNull();
        const outcomes = screen.getByTestId("pa-outcomes").textContent;
        expect(outcomes).toMatch(/Sebamed - Testimonial film.*added/);
        expect(outcomes).toMatch(/Sebamed - Trend film.*failed — boom/);
        expect(screen.getAllByTestId("pa-confirm")).toHaveLength(1);                         // the (still blocked on the server) send's own Confirm
    });
});

describe("media kit card (2026-10-19)", () => {
    const kit = (over = {}) => ({ id: "tal-1", label: "Angela Kumar", sublabel: "28y", thumb: null, profile_path: "/admin/talents/tal-1", portfolio_url: null, video_url: null, photos: [], photo_count: 0,
        media_link_requested: true, media_link_available: true, media_link: "https://talentgram.in/talent-media/abc123", media_link_reason: null,
        media_contents: { indian: 5, western: 3, portfolio: 2, video: true, total: 11 }, ...over });
    const show = async (c) => {
        const t = task({ id: "q1", kind: "lookup", status: "completed", title: "Media link · Angela Kumar", summary: [], result: { kind: "media_link", cards: [c] } });
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "x", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("q"); fireEvent.click(screen.getByTestId("pa-send")); return screen.findByTestId("pa-task-q1");
    };

    it("shows the link with Open and Copy, explains what it contains, and offers Share on WhatsApp", async () => {
        const el = await show(kit());
        const link = within(el).getByTestId("pa-media-link");
        expect(link.getAttribute("href")).toBe("https://talentgram.in/talent-media/abc123");
        expect(link.getAttribute("target")).toBe("_blank");
        expect(within(el).getByLabelText("Open media link")).toBeTruthy();
        expect(within(el).getByLabelText("Copy link")).toBeTruthy();
        expect(within(el).getByTestId("pa-media-contents").textContent).toBe("Opens a page where anyone with the link can view and download: 5 Indian look · 3 Western look · 2 portfolio · intro video.");
        fireEvent.click(within(el).getByTestId("pa-share-whatsapp"));
        expect(screen.getByLabelText("Command").value).toBe("Send Angela Kumar's media link to ");     // the destination is named by the user, then previewed and approved
        expect(adminApi.post.mock.calls.filter((c) => /confirm|message/.test(c[0])).length).toBe(1);   // only the original message; sharing sent nothing
    });

    it("says plainly when the link is off or the profile has no media (no invented link)", async () => {
        const off = await show(kit({ media_link: null, media_link_available: false, media_link_reason: "The media link is switched off for this talent (or the profile is archived/merged), so there is no link to give out." }));
        expect(within(off).getByTestId("pa-media-reason").textContent).toMatch(/switched off/);
        expect(within(off).queryByTestId("pa-media-link")).toBeNull();
        expect(within(off).queryByTestId("pa-share-whatsapp")).toBeNull();
        cleanup();
        const bare = await show(kit({ media_contents: { indian: 0, western: 0, portfolio: 0, video: false, total: 0 }, media_link_reason: "The link works, but this talent has no photos or video on their profile yet, so it would open an empty page." }));
        expect(within(bare).getByTestId("pa-media-reason").textContent).toMatch(/no photos or video/);
    });
});

describe("destination group evidence (2026-10-19)", () => {
    const share = (access) => task({ id: "s1", kind: "share", fingerprint: "fp", title: "Share media link of Angela with the Amazon Now WhatsApp group", summary: ["Recipient: WhatsApp group “GRP” of Amazon Now"],
        preview: { group: "GRP", worker_access: access }, params: { talents: [], projects: [], recipient: { kind: "project_group" } } });
    const begin = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("share"); fireEvent.click(screen.getByTestId("pa-send"));
        const card = await screen.findByTestId("pa-task-s1");
        fireEvent.click(await within(card).findByTestId("pa-worker-default"));
        return card;
    };

    it("a worker never seen sending to the group needs an explicit 'send anyway' tick before Confirm is possible, and the tick is sent", async () => {
        const card = await begin(share({ default: { state: "unknown", label: "Worker 1" }, "worker-2": { state: "unknown", label: "Worker 2" } }));
        expect(within(card).getByTestId("pa-group-ack").textContent).toMatch(/never been seen sending to “GRP”/);
        expect(within(card).getByTestId("pa-confirm").disabled).toBe(true);
        fireEvent.click(within(card).getByRole("checkbox"));
        expect(within(card).getByTestId("pa-confirm").disabled).toBe(false);
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: { ...share({}), status: "queued" } } }));
        fireEvent.click(within(card).getByTestId("pa-confirm"));
        await waitFor(() => expect(posts("/tasks/s1/confirm")).toHaveLength(1));
        expect(posts("/tasks/s1/confirm")[0][1]).toEqual({ worker_id: "default", fingerprint: "fp", ack_unverified: true });
    });

    it("a verified worker needs no tick; a worker that could not find the group is held and cannot be confirmed", async () => {
        const ok = await begin(share({ default: { state: "verified", label: "Worker 1" } }));
        expect(within(ok).queryByTestId("pa-group-ack")).toBeNull();
        expect(within(ok).getByTestId("pa-confirm").disabled).toBe(false);
        cleanup();
        const held = await begin(share({ default: { state: "failed_before", label: "Worker 1" } }));
        expect(within(held).getByTestId("pa-group-blocked").textContent).toMatch(/could not find the group/);
        expect(within(held).getByTestId("pa-confirm").disabled).toBe(true);
    });
});

describe("name candidates (2026-10-19)", () => {
    const cards = (opts, kind = "talent") => task({ id: "m1", kind: "move", status: "needs_clarification", title: "Move Doha Kumar to Approved", summary: [],
        slots: [{ key: "talent:0", kind, prompt: 'Which "Doha Kumar" did you mean?', options: opts }] });
    const show = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("move doha"); fireEvent.click(screen.getByTestId("pa-send")); return screen.findByTestId("pa-task-m1");
    };

    it("a name that only SOUNDS like what was said is labelled, never flagged as the best match", async () => {
        const el = await show(cards([{ id: "t1", label: "Disha Kumar", sublabel: "27y", match: "sounds_like" }, { id: "t2", label: "Dasha Kumar", sublabel: "29y", match: "sounds_like" }]));
        expect(within(el).getByTestId("pa-sounds-t1").textContent).toBe("Sounds similar");
        expect(within(el).getByTestId("pa-sounds-t2")).toBeTruthy();
        expect(within(el).queryByTestId("pa-best-t1")).toBeNull();
        expect(within(el).getByTestId("pa-identify-head").textContent).toMatch(/Who do you mean\?/);
        expect(adminApi.post.mock.calls.filter((c) => /confirm|clarify/.test(c[0]))).toHaveLength(0);          // showing the candidates changed nothing
    });

    it("a same-named talent already in the named project is proposed as 'In this project', and a looks-like stage is proposed first", async () => {
        const el = await show(cards([{ id: "p2", label: "Priya Nair", sublabel: "In Sebamed's pipeline · Shortlisted · 33y", match: "exact", best: true, in_project: true }, { id: "p1", label: "Priya Nair", sublabel: "24y", match: "exact" }]));
        expect(within(el).getByTestId("pa-best-p2").textContent).toBe("In this project");
        expect(within(el).queryByTestId("pa-best-p1")).toBeNull();
        cleanup();
        const st = await show(task({ id: "m1", kind: "move", status: "needs_clarification", title: "Move", summary: [],
            slots: [{ key: "stage", kind: "stage", prompt: 'Which pipeline stage did you mean by "Improved"?', options: [{ id: "approved", label: "Approved", sublabel: "", best: true }, { id: "hold", label: "Hold", sublabel: "" }] }] }));
        expect(within(st).getByTestId("pa-best-approved").textContent).toBe("Best match");
        expect(within(st).getByTestId("pa-identify-head").textContent).toMatch(/Which stage\?/);
    });
});

describe("`update` command: values, file selection, approval and upload (2026-10-20)", () => {
    const media = (over = {}) => ({ category: "video", label: "Introduction video", op: "replace", existing: 1, existing_names: ["old.mp4"], files: [], ...over });
    const upd = (over = {}) => task({ id: "u1", kind: "update", status: "needs_clarification", title: "Update Angela: introduction video", summary: [], fingerprint: "fp1",
        params: { talents: [{ id: "t1", label: "Angela", state: "resolved" }], projects: [], updates: { fields: [], media: [media()] } },
        slots: [{ key: "files:video", kind: "files", category: "video", multiple: false, accept: ".mp4,.mov", max_mb: 200, prompt: "Choose the video for Introduction video." }], ...over });
    const file = (name, size = 1000, type = "video/mp4") => { const f = new File(["x"], name, { type }); Object.defineProperty(f, "size", { value: size }); return f; };
    const begin = async (t) => {
        server({ onMessage: () => Promise.resolve({ data: { command_id: "c1", reply: "1 needs your pick", tasks: [t], unrecognised: [] } }) });
        mount(); await open(); type("update angela's introduction video"); fireEvent.click(screen.getByTestId("pa-send"));
        return screen.findByTestId(`pa-task-${t.id}`);
    };
    const manifestOf = (c) => c[1].files;

    it("a missing value is typed in, sent as text, and the file slot only accepts approved formats and sizes", async () => {
        const card = await begin(upd({ slots: [{ key: "value:age", kind: "value", prompt: "What should Age be?", options: [] }] }));
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: upd({ status: "awaiting_confirmation", slots: [], summary: ["Age: 22 → 24"] }) } }));
        fireEvent.change(within(card).getByLabelText("What should Age be?"), { target: { value: "24" } });
        fireEvent.click(within(card).getByTestId("pa-value-ok-value:age"));
        await waitFor(() => expect(posts("/tasks/u1/clarify")).toHaveLength(1));
        expect(posts("/tasks/u1/clarify")[0][1]).toEqual({ slot: "value:age", text: "24" });
    });

    it("choosing a file shows its name and size, rejects the wrong type or size, and sends only a LIST (no file bytes) for the approval", async () => {
        const card = await begin(upd());
        const input = within(card).getByTestId("pa-file-input-video");
        fireEvent.change(input, { target: { files: [file("notes.pdf", 100, "application/pdf")] } });
        expect(within(card).getByTestId("pa-file-problem").textContent).toMatch(/isn't an accepted format/);
        fireEvent.change(input, { target: { files: [file("big.mp4", 300 * 1024 * 1024)] } });
        expect(within(card).getByTestId("pa-file-problem").textContent).toMatch(/too large — the limit is 200 MB/);
        fireEvent.change(input, { target: { files: [file("a.mp4"), file("b.mp4")] } });
        expect(within(card).getByTestId("pa-file-problem").textContent).toMatch(/single file/);
        expect(posts("/clarify")).toHaveLength(0);
        fireEvent.change(input, { target: { files: [file("intro.mp4", 5 * 1048576)] } });
        expect(within(card).getByTestId("pa-file-chosen").textContent).toMatch(/intro\.mp4.*5\.0 MB/);
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: upd({ status: "awaiting_confirmation", slots: [], summary: ["Introduction video: REPLACES old.mp4 with intro.mp4"] }) } }));
        fireEvent.click(within(card).getByTestId("pa-files-use-video"));
        await waitFor(() => expect(posts("/tasks/u1/clarify")).toHaveLength(1));
        expect(posts("/tasks/u1/clarify")[0][1]).toEqual({ slot: "files:video", files: [{ name: "intro.mp4", size: 5 * 1048576, type: "video/mp4" }] });
        expect((await screen.findByTestId("pa-preview")).textContent).toMatch(/REPLACES old\.mp4 with intro\.mp4/);
    });

    it("files attached BEFORE the command are used only when the user taps Use, and only for a compatible slot", async () => {
        const card = await begin(upd());
        fireEvent.change(screen.getByTestId("pa-attach"), { target: { files: [file("pic.jpg", 500, "image/jpeg"), file("movie.mp4", 900, "video/mp4")] } });
        expect(screen.getByTestId("pa-attached").textContent).toMatch(/pic\.jpg.*movie\.mp4/);
        const use = within(card).getByTestId("pa-files-attached-video");
        expect(use.textContent).toMatch(/Use 1 attached file \(movie\.mp4\)/);              // the picture is not offered for a video slot
        expect(posts("/clarify")).toHaveLength(0);                                              // attaching alone assigned nothing
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: upd({ status: "awaiting_confirmation", slots: [], summary: ["x"] }) } }));
        fireEvent.click(use);
        await waitFor(() => expect(posts("/tasks/u1/clarify")).toHaveLength(1));
        expect(manifestOf(posts("/tasks/u1/clarify")[0])).toEqual([{ name: "movie.mp4", size: 900, type: "video/mp4" }]);
        await waitFor(() => expect(screen.getByTestId("pa-attached").textContent).not.toMatch(/movie\.mp4/));
        expect(screen.getByTestId("pa-attached").textContent).toMatch(/pic\.jpg/);
    });

    it("approving applies the fields, then uploads each approved file through the existing API and shows what the server read back", async () => {
        const card0 = await begin(upd());
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: upd({ status: "awaiting_confirmation", slots: [], summary: ["Introduction video: REPLACES old.mp4 with intro.mp4"],
            params: { talents: [{ id: "t1", label: "Angela" }], projects: [], updates: { fields: [], media: [media({ files: [{ key: "intro.mp4|1000", name: "intro.mp4", size: 1000, type: "video/mp4", state: "pending" }] })] } } }) } }));
        fireEvent.change(within(card0).getByTestId("pa-file-input-video"), { target: { files: [file("intro.mp4", 1000)] } });
        fireEvent.click(within(card0).getByTestId("pa-files-use-video"));
        const card = await screen.findByTestId("pa-task-u1");
        await within(card).findByText("Approve changes");
        const running = upd({ status: "running", slots: [], params: { talents: [{ id: "t1", label: "Angela" }], projects: [], updates: { fields: [], media: [media({ files: [{ key: "intro.mp4|1000", name: "intro.mp4", size: 1000, type: "video/mp4", state: "pending" }] })] } } });
        const finished = upd({ status: "completed", slots: [], outcomes: [{ name: "Angela — Introduction video", status: "uploaded", detail: "intro.mp4" }],
            params: { talents: [{ id: "t1", label: "Angela" }], projects: [], updates: { fields: [], media: [media({ files: [{ key: "intro.mp4|1000", name: "intro.mp4", size: 1000, type: "video/mp4", state: "uploaded" }] })] } } });
        adminApi.post.mockImplementation((url) => Promise.resolve({ data: { task: url.endsWith("/media") ? finished : running } }));
        fireEvent.click(within(card).getByTestId("pa-confirm"));
        await waitFor(() => expect(posts("/tasks/u1/media")).toHaveLength(1));
        const [url, body, cfg] = posts("/tasks/u1/media")[0];
        expect(body instanceof FormData && body.get("category") === "video" && body.get("key") === "intro.mp4|1000" && body.get("file").name === "intro.mp4").toBe(true);
        expect(cfg.timeout).toBeGreaterThan(60000);
        expect(posts("/tasks/u1/confirm")[0][1]).toEqual({ fingerprint: "fp1" });
        const out = await screen.findByTestId("pa-outcomes");
        expect(out.textContent).toMatch(/Angela — Introduction video \(intro\.mp4\).*uploaded/);
        expect(screen.getByText("Completed")).toBeTruthy();
    });

    it("a failed upload is shown with its reason and can be retried; after a reload the same file must be selected again, and only an approved file is accepted", async () => {
        const stuck = upd({ status: "partial", slots: [], outcomes: [{ name: "Angela — Introduction video", status: "failed", reason: "The upload failed — nothing was added to the profile.", detail: "intro.mp4" }],
            params: { talents: [{ id: "t1", label: "Angela" }], projects: [], updates: { fields: [], media: [media({ files: [{ key: "intro.mp4|1000", name: "intro.mp4", size: 1000, type: "video/mp4", state: "failed", error: "x" }] })] } } });
        const card = await begin(stuck);
        expect(within(card).getByTestId("pa-outcomes").textContent).toMatch(/intro\.mp4.*failed — The upload failed/);
        expect(within(card).getByTestId("pa-upload-status").textContent).toMatch(/Select the same file again/);    // this tab no longer holds the file
        expect(within(card).queryByTestId("pa-confirm")).toBeNull();
        expect(within(card).queryByTestId("pa-retry")).toBeNull();                                                  // no generic Retry that would re-run the whole update
        const { toast } = await import("sonner");
        toast.error.mockClear();
        fireEvent.change(within(card).getByTestId("pa-reselect"), { target: { files: [file("other.mp4", 1000)] } });
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/None of those files is on the approved list/)));
        expect(posts("/media")).toHaveLength(0);
        adminApi.post.mockImplementation(() => Promise.resolve({ data: { task: { ...stuck, status: "completed", outcomes: [{ name: "Angela — Introduction video", status: "uploaded", detail: "intro.mp4" }] } } }));
        fireEvent.change(within(card).getByTestId("pa-reselect"), { target: { files: [file("intro.mp4", 1000)] } });
        await waitFor(() => expect(posts("/tasks/u1/media")).toHaveLength(1));
    });
});

describe("live reply line", () => {
    const t = (status) => ({ status });
    it("is recomputed from the tasks' current statuses, so it never says 'needs your pick' after the pick", () => {
        const saved = "Here's what I understood: 1 needs your pick.";
        expect(liveReply(saved, [t("needs_clarification")])).toBe("Here's what I understood: 1 needs your pick.");
        expect(liveReply(saved, [t("awaiting_confirmation")])).toBe("Here's what I understood: 1 waiting for your OK.");
        expect(liveReply(saved, [t("queued"), t("awaiting_confirmation")])).toBe("Here's what I understood: 1 waiting for your OK, 1 in progress.");
        expect(liveReply(saved, [t("completed")])).toBe("All done — see below.");
        expect(liveReply(saved, [t("completed"), t("failed")])).toBe("Finished — some steps need attention, see below.");
        expect(liveReply(saved, [t("cancelled"), t("cancelled")])).toBe("Cancelled — nothing was run or changed.");
        expect(liveReply(saved, [t("completed"), t("cancelled")])).toBe("Done — the cancelled steps were not run.");
    });
    it("keeps the 'I didn't understand' part and leaves a message with no tasks untouched", () => {
        expect(liveReply("Here's what I understood: 1 needs your pick. I didn't understand: “blah”.", [t("completed")])).toBe("All done — see below. I didn't understand: “blah”.");
        expect(liveReply("I couldn't turn that into an action.", [])).toBe("I couldn't turn that into an action.");
    });
});


// ================================================================================================================================
// Voice input — TAP TO START, TAP TO STOP (2026-10-20). A session is the user's intention to listen; the browser's recognition
// ending by itself (silence, a pause, a service limit) must not end it, and every word captured must arrive exactly once.
// All browser behaviour here is MOCKED (SpeechRecognition, Permissions API, timers): deterministic, but not proof of real devices.
// ================================================================================================================================
describe("voice input — tap to start, tap to stop", () => {
    const setPolicy = (allowed) => Object.defineProperty(document, "featurePolicy", { configurable: true, value: allowed === undefined ? undefined : { allowsFeature: (f) => (f === "microphone" ? allowed : true) } });
    const setPerm = (state) => Object.defineProperty(navigator, "permissions", { configurable: true, value: state ? { query: vi.fn(() => Promise.resolve({ state })) } : undefined });
    const setUA = (ua, extra = {}) => { Object.defineProperty(navigator, "userAgent", { configurable: true, value: ua }); Object.entries(extra).forEach(([k, v]) => Object.defineProperty(navigator, k, { configurable: true, value: v })); };
    const REAL_UA = navigator.userAgent;
    const reset = () => { setPolicy(undefined); setPerm(undefined); Object.defineProperty(window, "isSecureContext", { configurable: true, value: true }); Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 0 }); setUA(REAL_UA, { standalone: undefined }); };
    beforeEach(reset);
    afterEach(() => { reset(); vi.useRealTimers(); });

    // every recognition object the app creates is recorded: a restart makes a NEW one (inst.all[1], …)
    function fakeRecognition() {
        const inst = { all: [], get rec() { return this.all[this.all.length - 1]; } };
        window.webkitSpeechRecognition = function Rec() { Object.assign(this, { start: vi.fn(), stop: vi.fn(), abort: vi.fn() }); inst.all.push(this); };
        return inst;
    }
    const results = (...parts) => ({ resultIndex: 0, results: parts.map(([t, isFinal]) => Object.assign([{ transcript: t }], { isFinal })) });
    const mic = () => screen.getByTestId("pa-mic");
    const tap = async () => { fireEvent.click(mic()); await act(async () => {}); };
    const begin = async (inst) => { await tap(); act(() => { inst.rec.onstart(); }); };               // tap → the engine reports audio
    const hear = (rec, ...parts) => act(() => { rec.onresult(results(...parts)); });
    const engineEnds = (rec) => act(() => { rec.onend?.(); });
    const input = () => screen.getByLabelText("Command").value;
    // timers: mount with REAL timers (RTL waits), then fake ones only for the voice clocks
    const mountFake = async () => { server(); const inst = fakeRecognition(); mount(); await open(); vi.useFakeTimers(); return inst; };
    const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

    // ------------------------------------------------------------------------------------------------ 1. the toggle
    it("first tap starts listening and the button says so; the second tap stops it, the engine's last words arrive, and the text lands in the box", async () => {
        server(); const inst = fakeRecognition(); mount(); await open();
        expect(mic().getAttribute("aria-label")).toBe("Start voice input");
        await tap();
        expect(inst.all).toHaveLength(1);
        expect(inst.rec.start).toHaveBeenCalledTimes(1);
        expect(inst.rec.continuous).toBe(true);                                         // keeps listening through pauses where supported
        expect(screen.getByTestId("pa-listening").textContent).toMatch(/starting microphone/i);
        act(() => { inst.rec.onstart(); });
        expect(mic().getAttribute("aria-label")).toBe("Listening — tap to stop");
        expect(mic().getAttribute("aria-pressed")).toBe("true");
        expect(screen.getByTestId("pa-state").textContent).toMatch(/Listening — tap the mic to stop/);
        hear(inst.rec, ["show me angela kumar's profile", true]);
        fireEvent.click(mic());                                                           // the intentional stop
        expect(inst.rec.stop).toHaveBeenCalledTimes(1);
        expect(mic().getAttribute("aria-label")).toMatch(/Finishing transcript/);
        expect(mic().disabled).toBe(false);                                               // never a button that cannot be used to stop
        expect(input()).toBe("");                                                         // nothing yet: still waiting for the engine's last words
        engineEnds(inst.rec);
        expect(input()).toBe("show me angela kumar's profile");
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(mic().getAttribute("aria-label")).toBe("Start voice input");
        expect(posts("/message")).toHaveLength(0);                                        // never sent by itself
    });

    it("a second tap while it is finishing delivers what has been captured right away (it can always be stopped)", async () => {
        server(); const inst = fakeRecognition(); mount(); await open();
        await begin(inst);
        hear(inst.rec, ["add neha to pepsi", true]);
        fireEvent.click(mic());                                                           // stop requested; the engine is slow to say `end`
        fireEvent.click(mic());                                                           // impatient second tap
        expect(input()).toBe("add neha to pepsi");
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        engineEnds(inst.rec);                                                             // the engine's late end changes nothing
        expect(input()).toBe("add neha to pepsi");
    });

    it("tapping while it is still starting cancels it cleanly; the next tap starts one fresh session", async () => {
        let release;
        Object.defineProperty(navigator, "permissions", { configurable: true, value: { query: () => new Promise((r) => { release = () => r({ state: "granted" }); }) } });
        server(); const inst = fakeRecognition(); mount(); await open();
        fireEvent.click(mic());                                                           // starting… (waiting on the permission query)
        fireEvent.click(mic());                                                           // cancel
        await act(async () => { release(); });
        expect(inst.all).toHaveLength(0);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        Object.defineProperty(navigator, "permissions", { configurable: true, value: undefined });
        await tap();
        expect(inst.all).toHaveLength(1);
    });

    // ------------------------------------------------------------------------------------------------ 2. pauses
    it("a long pause never ends an active session: minutes of silence → still listening, nothing stopped, no error, nothing restarted", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["add these talents", true]);
        await advance(90_000);                                                            // the user is thinking for a minute and a half
        expect(screen.getByTestId("pa-listening")).toBeTruthy();
        expect(inst.rec.stop).not.toHaveBeenCalled();
        expect(inst.all).toHaveLength(1);
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();
        expect(mic().getAttribute("aria-label")).toBe("Listening — tap to stop");
        expect(screen.getByTestId("pa-live-text").textContent).toBe("add these talents");     // and the live preview still shows what was captured
    });

    it("the start watchdog only reports a genuine failure to START: once audio has begun it can never end the session", async () => {
        const inst = await mountFake();
        await begin(inst);                                                                // onstart arrived
        await advance(20_000);                                                            // well past the 8 s start limit
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();
        expect(screen.getByTestId("pa-listening")).toBeTruthy();
    });

    // ------------------------------------------------------------------------------------------------ 3-4. accumulation
    it("several result events add up to ONE complete transcript with no missing or repeated phrase", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["add Kripa Trivedi", true]);
        hear(inst.rec, ["add Kripa Trivedi", true], ["and Neha Rao", true]);                // the list grows (finals stay)
        hear(inst.rec, ["add Kripa Trivedi", true], ["and Neha Rao", true], ["to Sebamed Trend film", true]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("add Kripa Trivedi and Neha Rao to Sebamed Trend film");
    });

    it("interim results are replaced by their final, never repeated beside it", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["send the", false]);
        expect(screen.getByTestId("pa-live-text").textContent).toBe("send the");
        hear(inst.rec, ["send the casting", false]);
        hear(inst.rec, ["send the casting for sebamed", false]);
        hear(inst.rec, ["send the casting for sebamed", true], ["trend film", false]);       // final arrives; the next interim trails it
        expect(screen.getByTestId("pa-live-text").textContent).toBe("send the casting for sebamed trend film");
        hear(inst.rec, ["send the casting for sebamed", true], ["trend film", true]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("send the casting for sebamed trend film");
    });

    it("words that were only ever interim when the user stops are still kept", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["move vidushi to hold", false]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("move vidushi to hold");
    });

    it("existing typed text is kept and the transcript is appended to it", async () => {
        server(); const inst = fakeRecognition(); mount(); await open();
        type("Move");
        await begin(inst);
        hear(inst.rec, ["Vidushi Tiwari to hold", true]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("Move Vidushi Tiwari to hold");
    });

    // ------------------------------------------------------------------------------------------------ 5-7. unexpected end → bounded restart
    it("the engine ending by itself while the user is still listening restarts it, and everything said before AND after the restart is combined", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["add Kripa Trivedi", true]);
        await advance(5_000);                                                              // a healthy stretch…
        const first = inst.rec;
        engineEnds(first);                                                                 // …then the browser's service ends the recognition on its own
        expect(input()).toBe("");                                                          // the session is NOT over: nothing delivered yet
        expect(screen.getByTestId("pa-listening")).toBeTruthy();
        await advance(300);                                                                // after the short restart delay
        expect(inst.all).toHaveLength(2);                                                  // a NEW recognition object
        expect(inst.rec.start).toHaveBeenCalledTimes(1);
        expect(inst.rec).not.toBe(first);
        act(() => { inst.rec.onstart(); });
        hear(inst.rec, ["to Sebamed Trend film", true]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("add Kripa Trivedi to Sebamed Trend film");                    // earlier words kept, later words appended, once each
    });

    it("an intentional stop never restarts: after the stop, the engine's end finishes the session and nothing new is created", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["list ask to test", true]);
        fireEvent.click(mic());
        engineEnds(inst.rec);
        await advance(10_000);
        expect(inst.all).toHaveLength(1);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(input()).toBe("list ask to test");
    });

    it("a stop during the restart delay cancels the restart and still delivers the text", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["find angela", true]);
        await advance(3_000);
        engineEnds(inst.rec);                                                              // engine ended; a restart is pending…
        fireEvent.click(mic());                                                            // …and the user taps stop before it happens
        await advance(5_000);
        expect(inst.all).toHaveLength(1);
        expect(input()).toBe("find angela");
        expect(screen.queryByTestId("pa-listening")).toBeNull();
    });

    it("repeated `end` events for one recognition cannot create a restart loop (exactly one restart)", async () => {
        const inst = await mountFake();
        await begin(inst);
        await advance(3_000);
        const first = inst.rec;
        act(() => { first.onend?.(); first.onend?.(); first.onend?.(); });
        await advance(2_000);
        expect(inst.all).toHaveLength(2);                                                  // not 4
        act(() => { first.onend?.(); });                                                   // a late duplicate from the OLD recognition
        await advance(2_000);
        expect(inst.all).toHaveLength(2);
    });

    it("an engine that keeps ending instantly is given up on after a few tries (bounded, rate-limited) and what was heard is kept with an honest message", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["move kripa to hold", true]);
        for (let i = 0; i < 12 && screen.queryByTestId("pa-listening"); i += 1) {         // every recognition begins and dies at once
            engineEnds(inst.rec);
            await advance(1_500);
            if (inst.all.length > i + 1 && screen.queryByTestId("pa-listening")) act(() => { inst.rec.onstart(); });
        }
        expect(inst.all.length).toBeLessThanOrEqual(6);                                    // 1 + at most MAX_FAST_ENDS restarts, never a flood
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(input()).toBe("move kripa to hold");
        expect(screen.getByTestId("pa-voice-notice").textContent).toMatch(/keeps stopping.*kept everything/i);
        expect(mic().disabled).toBe(false);
    });

    it("a browser that refuses to resume by itself (restart throws) ends the session with the text kept and a tap-to-continue message", async () => {
        server(); fakeRecognition();
        let made = 0;
        window.webkitSpeechRecognition = function Rec() { made += 1; this.start = made === 1 ? vi.fn() : () => { throw new Error("NotAllowedError: needs a user gesture"); }; this.stop = vi.fn(); this.abort = vi.fn(); window.__rec = this; };
        mount(); await open();
        vi.useFakeTimers();
        await tap(); act(() => { window.__rec.onstart(); });
        hear(window.__rec, ["add riya to pepsi", true]);
        await advance(3_000);
        engineEnds(window.__rec);
        await advance(500);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(input()).toBe("add riya to pepsi");
        expect(screen.getByTestId("pa-voice-notice").textContent).toMatch(/tap the mic to continue/i);
    });

    it("a restart that never begins ends the session within a few seconds (text kept) instead of hanging", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["share her profile", true]);
        await advance(3_000);
        engineEnds(inst.rec);
        await advance(300);
        expect(inst.all).toHaveLength(2);                                                  // restarted… but the new one never reports `start`
        await advance(4_100);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(input()).toBe("share her profile");
        expect(screen.getByTestId("pa-voice-notice")).toBeTruthy();
    });

    // ------------------------------------------------------------------------------------------------ 8. hard maximum
    it("the hard maximum duration stops the session, delivers the captured text, and says it was the limit (not an error)", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["a very long command about everyone", true]);
        await advance(119_000);
        expect(screen.getByTestId("pa-listening")).toBeTruthy();                           // not before the maximum
        await advance(1_500);                                                              // 120 s reached
        expect(inst.rec.stop).toHaveBeenCalled();
        expect(mic().getAttribute("aria-label")).toMatch(/Finishing transcript/);
        engineEnds(inst.rec);
        expect(input()).toBe("a very long command about everyone");
        expect(screen.getByTestId("pa-voice-notice").textContent).toMatch(/2-minute limit.*kept everything/i);
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();
        expect(mic().disabled).toBe(false);
    });

    it("if the engine never answers the maximum's stop request, the session is forced shut after a moment with the text kept", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["keep this", true]);
        await advance(120_100);
        await advance(2_600);                                                              // the engine never says `end`
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(input()).toBe("keep this");
    });

    it("a session that ends with nothing recognised says so and returns to idle (silence during the session is not an error by itself)", async () => {
        const inst = await mountFake();
        await begin(inst);
        await advance(12_000);                                                             // 12 s of nothing: the engine reports no-speech and ends…
        act(() => { inst.rec.onerror({ error: "no-speech" }); });
        engineEnds(inst.rec);
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();                         // …that alone does not end or fail the session
        await advance(500);
        expect(inst.all).toHaveLength(2);                                                  // it was quietly restarted
        act(() => { inst.rec.onstart(); });
        fireEvent.click(mic()); engineEnds(inst.rec);                                      // the user finally taps stop with nothing said
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/didn't hear anything/i);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(mic().disabled).toBe(false);
        expect(input()).toBe("");
    });

    // ------------------------------------------------------------------------------------------------ 9. errors
    it.each([["network", /network connection/i], ["audio-capture", /No working microphone/i], ["language-not-supported", /language/i], ["weird-new-code", /stopped unexpectedly/i]])("a %s failure ends the session with its own message, keeps and delivers the words already captured, and leaves the mic usable", async (code, re) => {
        server(); const inst = fakeRecognition(); mount(); await open();
        type("typed before");
        await begin(inst);
        hear(inst.rec, ["move kripa", true]);
        act(() => { inst.rec.onerror({ error: code }); inst.rec.onend?.(); });
        const msg = screen.getByTestId("pa-voice-error").textContent;
        expect(msg).toMatch(re);
        expect(msg).not.toMatch(/permission|blocked|allow/i);                             // unrelated failures are not "permission blocked"
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(input()).toBe("typed before move kripa");                                   // typed text + what was heard: nothing lost
        expect(mic().disabled).toBe(false);
        expect(mic().getAttribute("data-state")).toBe("failed");
    });

    it("an aborted recognition is not fatal: the session continues (restart) and keeps what was heard", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["add riya", true]);
        act(() => { inst.rec.onerror({ error: "aborted" }); });
        engineEnds(inst.rec);
        await advance(500);
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();
        expect(inst.all).toHaveLength(2);
        act(() => { inst.rec.onstart(); });
        hear(inst.rec, ["to pepsi", true]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("add riya to pepsi");
    });

    // ------------------------------------------------------------------------------------------------ 10. close / minimise / stale callbacks
    it.each([["minimising", "pa-minimize"], ["closing", "pa-close"]])("%s the panel stops recognition, detaches its handlers, keeps the captured text in the box, and ignores every late event", async (_n, buttonId) => {
        server(); const inst = fakeRecognition(); mount(); await open();
        await begin(inst);
        hear(inst.rec, ["move vidushi tiwari", true]);
        const rec = inst.rec;
        const late = { onresult: rec.onresult, onerror: rec.onerror, onend: rec.onend, onstart: rec.onstart };
        fireEvent.click(screen.getByTestId(buttonId));
        expect(rec.abort).toHaveBeenCalled();
        expect(rec.onresult).toBeNull(); expect(rec.onend).toBeNull(); expect(rec.onerror).toBeNull();
        act(() => {                                                                           // stale callbacks from the old recognition
            late.onresult(results(["INTRUDER words", true]));
            late.onerror({ error: "network" });
            late.onend();
            late.onstart();
        });
        if (buttonId === "pa-minimize") fireEvent.click(screen.getByTestId("pa-minimize")); else fireEvent.click(await screen.findByTestId("pa-launcher"));
        await screen.findByTestId("pa-panel");
        expect(input()).toBe("move vidushi tiwari");                                          // delivered once, no intruder, no error
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        await tap();                                                                          // reopening allows a fresh session
        expect(inst.all).toHaveLength(2);
    });

    it("unmounting while listening aborts the session; late events touch nothing and nothing throws or warns about state updates", async () => {
        const spy = vi.spyOn(console, "error").mockImplementation(() => {});
        server(); const inst = fakeRecognition(); const { unmount } = mount(); await open();
        await begin(inst);
        const rec = inst.rec;
        const h = { onresult: rec.onresult, onerror: rec.onerror, onend: rec.onend };
        unmount();
        expect(rec.abort).toHaveBeenCalled();
        act(() => { h.onresult?.(results(["late words", true])); h.onerror?.({ error: "network" }); h.onend?.(); });
        expect(spy.mock.calls.filter((c) => /unmounted|not wrapped in act/i.test(String(c[0])))).toHaveLength(0);
        spy.mockRestore();
    });

    // ------------------------------------------------------------------------------------------------ 11-12. isolation + once + never sent
    it("one session's words can never leak into the next: handlers and text start from nothing every time", async () => {
        server(); const inst = fakeRecognition(); mount(); await open();
        await begin(inst);
        hear(inst.rec, ["first command about pepsi", true]);
        const old = inst.rec;
        const oldHandlers = { onresult: old.onresult, onend: old.onend };
        fireEvent.click(mic()); engineEnds(old);
        expect(input()).toBe("first command about pepsi");
        type("");                                                                              // the user clears the box
        await begin(inst);
        expect(inst.all).toHaveLength(2);
        expect(inst.rec).not.toBe(old);
        act(() => { oldHandlers.onresult?.(results(["STALE words", true])); oldHandlers.onend?.(); });   // the old recognition speaks again
        hear(inst.rec, ["second command", true]);
        fireEvent.click(mic()); engineEnds(inst.rec);
        expect(input()).toBe("second command");                                                // no "first…", no "STALE…"
    });

    it("the finalized text is delivered exactly once (extra end/stop events add nothing) and is never sent by itself — the user still has to press Send", async () => {
        const inst = await mountFake();
        await begin(inst);
        hear(inst.rec, ["send the casting call to the selected talents", true]);
        const rec = inst.rec;
        fireEvent.click(mic());
        act(() => { rec.onend(); rec.onend?.(); rec.onend?.(); });
        fireEvent.click(mic()); fireEvent.click(mic());                                         // taps after it is over start new sessions — they must not re-deliver
        const afterStops = input();
        expect(afterStops.startsWith("send the casting call to the selected talents")).toBe(true);
        expect(afterStops.split("send the casting call").length - 1).toBe(1);
        expect(posts("/message")).toHaveLength(0);
        vi.useRealTimers();
        const box = screen.getByLabelText("Command");
        fireEvent.change(box, { target: { value: "send the casting call to the selected talents" } });
        fireEvent.click(screen.getByTestId("pa-send"));
        await waitFor(() => expect(posts("/message")).toHaveLength(1));
        expect(posts("/message")[0][1].text).toBe("send the casting call to the selected talents");
    });

    // ------------------------------------------------------------------------------------------------ permission/policy/platform (kept from b5a55a8)
    it("says plainly when the browser has no speech recognition, and the text box still works", async () => {
        server(); mount(); await open();
        expect(screen.getByTestId("pa-voice-unsupported")).toBeTruthy();
        expect(mic().disabled).toBe(true);
        type("Find Angela");
        expect(screen.getByTestId("pa-send").disabled).toBe(false);
    });

    it("a touch device without speech recognition is pointed at the keyboard's own dictation key", async () => {
        Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 5 });
        server(); mount(); await open();
        expect(screen.getByTestId("pa-voice-unsupported").textContent).toMatch(/microphone key on your keyboard/i);
    });

    it("a page whose security policy disables the microphone is reported as THAT (a site setup problem), never attempted, never blamed on the user's setting", async () => {
        setPolicy(false); setPerm("denied");
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        const msg = screen.getByTestId("pa-voice-error").textContent;
        expect(inst.all).toHaveLength(0);
        expect(msg).toMatch(/security settings/i);
        expect(msg).not.toMatch(/lock icon|blocked for/i);
        expect(screen.getByTestId("pa-voice-diag").textContent).toMatch(/page policy blocked by site policy/);
        expect(mic().disabled).toBe(false);
    });

    it("the browser reporting the microphone DENIED for this site never starts recognition and names the site", async () => {
        setPolicy(true); setPerm("denied");
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        const msg = screen.getByTestId("pa-voice-error").textContent;
        expect(inst.all).toHaveLength(0);
        expect(msg).toContain(window.location.host);
        expect(msg).toMatch(/site settings/i);
        expect(screen.getByTestId("pa-voice-diag").textContent).toMatch(/browser permission denied · page policy allowed/);
    });

    it("policy allows + Chrome says Allow + recognition still refused → NOT 'grant permission again': the browser/OS refused speech", async () => {
        setPolicy(true); setPerm("granted");
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        expect(inst.all).toHaveLength(1);
        act(() => { inst.rec.onerror({ error: "not-allowed" }); inst.rec.onend?.(); });
        const msg = screen.getByTestId("pa-voice-error").textContent;
        expect(msg).toMatch(/is allowed for/i);
        expect(msg).toMatch(/still refused/i);
        expect(msg).not.toMatch(/lock icon|choose allow|blocked for/i);
        expect(screen.getByTestId("pa-voice-diag").textContent).toMatch(/error not-allowed · browser permission granted · page policy allowed/);
    });

    it("permission 'prompt' when recognition fails with not-allowed → the prompt was dismissed/blocked: tap again and choose Allow", async () => {
        setPolicy(true); setPerm("prompt");
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        act(() => { inst.rec.onerror({ error: "not-allowed" }); inst.rec.onend?.(); });
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/choose Allow/i);
    });

    it("the wording is refined with the permission state AFTER the failure (user clicked Block in the prompt → now 'denied')", async () => {
        setPolicy(true);
        let state = "prompt";
        Object.defineProperty(navigator, "permissions", { configurable: true, value: { query: () => Promise.resolve({ state }) } });
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        state = "denied";
        act(() => { inst.rec.onerror({ error: "not-allowed" }); inst.rec.onend?.(); });
        await waitFor(() => expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/blocked for/i));
    });

    it("not-allowed with an unknown permission state (Safari/Firefox have no microphone query) asks for the site setting for THIS site only", async () => {
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        act(() => { inst.rec.onerror({ error: "not-allowed" }); inst.rec.onend?.(); });
        const msg = screen.getByTestId("pa-voice-error").textContent;
        expect(msg).toContain(window.location.host);
        expect(msg).toMatch(/exact site/i);
    });

    it("a first start that never reports audio is reported within the start limit (genuine failure to START), idle again, mic usable", async () => {
        const inst = await mountFake();
        fireEvent.click(mic()); await act(async () => { await vi.advanceTimersByTimeAsync(5); });
        expect(screen.getByTestId("pa-listening")).toBeTruthy();
        await advance(8_100);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/didn't start/i);
        expect(inst.rec.abort).toHaveBeenCalled();
        expect(mic().disabled).toBe(false);
    });

    it("iPhone Safari: a start that never fires gets iPhone-specific guidance (microphone, Dictation, keyboard key)", async () => {
        setUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1", { maxTouchPoints: 5 });
        await mountFake();
        fireEvent.click(mic()); await act(async () => { await vi.advanceTimersByTimeAsync(5); });
        await advance(8_100);
        const msg = screen.getByTestId("pa-voice-error").textContent;
        expect(msg).toMatch(/Settings › Safari › Microphone/);
        expect(msg).toMatch(/Enable Dictation/);
        expect(msg).toMatch(/keyboard/i);
        expect(screen.getByTestId("pa-voice-diag").textContent).toMatch(/iOS/);
    });

    it("iPhone Home-Screen app: says voice doesn't start there and to open Safari instead", async () => {
        setUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148", { maxTouchPoints: 5, standalone: true });
        await mountFake();
        fireEvent.click(mic()); await act(async () => { await vi.advanceTimersByTimeAsync(5); });
        await advance(8_100);
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/Home Screen app.*Safari/i);
    });

    it("service-not-allowed points iPhone users at Dictation / the keyboard key; Android gets the generic service message", async () => {
        setUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1", { maxTouchPoints: 5 });
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        act(() => { inst.rec.onerror({ error: "service-not-allowed" }); inst.rec.onend?.(); });
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/Enable Dictation/);
        cleanup();
        setUA("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36", { maxTouchPoints: 5 });
        server(); const inst2 = fakeRecognition(); mount(); await open();
        await tap();
        act(() => { inst2.rec.onerror({ error: "service-not-allowed" }); inst2.rec.onend?.(); });
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/speech service refused/i);
        expect(screen.getByTestId("pa-voice-error").textContent).not.toMatch(/Dictation/);
    });

    it("an insecure (non-https) page says why voice can't work; a recognition that throws on construction or on start leaves the UI idle with a message", async () => {
        Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        expect(inst.all).toHaveLength(0);
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/secure/i);
        cleanup(); Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
        server(); window.webkitSpeechRecognition = function Rec() { this.start = () => { throw new Error("InvalidStateError"); }; this.abort = vi.fn(); this.stop = vi.fn(); };
        mount(); await open();
        await tap();
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/couldn't start/i);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
        cleanup(); server(); window.webkitSpeechRecognition = function Rec() { throw new Error("boom"); };
        mount(); await open();
        await tap();
        expect(screen.getByTestId("pa-voice-error").textContent).toMatch(/couldn't start/i);
        expect(screen.queryByTestId("pa-listening")).toBeNull();
    });

    it("diagnostics carry only codes — never the words that were spoken", async () => {
        const dbg = vi.spyOn(console, "debug").mockImplementation(() => {});
        server(); const inst = fakeRecognition(); mount(); await open();
        await begin(inst);
        hear(inst.rec, ["secret talent phone number", true]);
        act(() => { inst.rec.onerror({ error: "network" }); inst.rec.onend?.(); });
        const logged = JSON.stringify(dbg.mock.calls);
        expect(logged).toMatch(/network/);
        expect(logged).not.toMatch(/secret|phone number/);
        expect(screen.getByTestId("pa-voice-diag").textContent).not.toMatch(/secret/);
        dbg.mockRestore();
    });

    it("every failure after a failed session leaves a clean slate: a new tap clears the old message and starts again", async () => {
        server(); const inst = fakeRecognition(); mount(); await open();
        await tap();
        act(() => { inst.rec.onerror({ error: "network" }); inst.rec.onend?.(); });
        expect(screen.getByTestId("pa-voice-error")).toBeTruthy();
        await tap();
        expect(screen.queryByTestId("pa-voice-error")).toBeNull();
        expect(inst.all).toHaveLength(2);
    });
});
