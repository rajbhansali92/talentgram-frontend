import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn(), post: vi.fn() },
}));

// Force the feature flag ON for the page tests (it is OFF by default).
vi.mock("@/lib/simpleAssistant", async (importOriginal) => {
    const actual = await importOriginal();
    return { ...actual, SIMPLE_ASSISTANT_ENABLED: true };
});

import { adminApi } from "@/lib/api";
import SimpleAssistant from "./SimpleAssistant";

const OVERVIEW = {
    generated_at: "2026-09-06T00:00:00Z",
    greeting: "Good morning",
    user: { first_name: "Raj" },
    summary: { active_projects: 2, talents_in_pipeline: 5, tests_received: 1, follow_ups: 3 },
    stage_vocabulary: [],
    projects: [
        {
            id: "p1", name: "Google AI", character: "Lead 25-30", status: "ongoing",
            shoot_dates: "18 Sep", budget: "25000", medium_usage: "Digital", image_url: null,
            pipeline_total: 3, stage_counts: { locked: 1, ask_to_test: 2 },
            stage_summary: [{ key: "locked", label: "Locked", count: 1 }],
            tests_received: 1, follow_up_count: 1,
            talents: [{
                talent_id: "t1", name: "Ahana Pocha", age: 26, height: "5'6\"", gender: "female",
                photo_url: null, stage: "locked", stage_label: "Locked",
                is_follow_up: false, test_status: "received",
                media: { intro_video: { id: "m1", label: "Introduction", url: "https://cdn/intro.mp4", poster_url: null }, takes: [] },
            }],
            talents_truncated: false, talent_total: 3,
        },
    ],
};

const PLAN_RESPONSE = {
    conversation_id: "c1", state: "preview", intent: "mark_talent_status",
    message: "I would move 1 talent to Not Available in Google AI. Ready to make this change?",
    clarification: null, answer: null, requires_confirmation: true,
    plan: {
        intent: "mark_talent_status", summary: "…", project: { id: "p1", label: "Google AI" },
        requires_confirmation: true, executable: true, warnings: [],
        changes: [{
            op: "update", entity_type: "talent", entity_id: "t1", entity_label: "Ahana Pocha",
            field: "pipeline_stage", current_value: "shortlisted", current_label: "Shortlisted",
            proposed_value: "not_available", proposed_label: "Not available",
        }],
    },
    context: { v: 1, conversation_id: "c1", pending: { kind: "confirm_plan", plan_id: "sap_x", plan: {} }, sig: "abc" },
};

const EXECUTED_RESPONSE = {
    conversation_id: "c1", state: "executed", intent: "mark_talent_status",
    message: "Done.\nGoogle AI\nAhana Pocha — Not available\n1 of 1 change completed successfully.",
    plan: PLAN_RESPONSE.plan,
    result: {
        intent: "mark_talent_status", project: { id: "p1", label: "Google AI" },
        target_stage: "not_available", target_stage_label: "Not available",
        counts: { success: 1 },
        outcomes: [{
            talent_id: "t1", talent_label: "Ahana Pocha", status: "success",
            from: "shortlisted", to: "not_available", from_label: "Shortlisted", to_label: "Not available",
        }],
    },
    overview: { ...OVERVIEW, summary: { ...OVERVIEW.summary, tests_received: 9 } },
};

const STALE_RESPONSE = {
    conversation_id: "c1", state: "stale", intent: "mark_talent_status",
    message: "This action is no longer current. Ahana Pocha's status changed to Locked after I prepared the plan. Ask me again and I'll prepare a fresh plan.",
    plan: PLAN_RESPONSE.plan, need_refresh: true,
};

// ---- Phase 4A communication fixtures ----
const COMM_PLAN = {
    action_type: "send_media", talent: { id: "t_neha", label: "Neha Sharma" },
    project: { id: "p1", label: "Google AI" },
    submission_id: "sub1", destination: "Google AI Casting", destination_type: "whatsapp_group",
    destination_label: "Google AI Casting",
    media: [
        { media_id: "m_intro", category: "intro_video", label: "Intro Video", url: "https://cdn/intro.mp4", poster_url: null, kind: "video" },
        { media_id: "m_t2", category: "take", label: "Take 2", url: "https://cdn/t2.mp4", poster_url: null, kind: "video" },
    ],
    message: "Neha Sharma — Google AI", profile_link: null, warnings: [],
    requires_confirmation: true, executable: true,
};
const COMM_PREVIEW = {
    conversation_id: "c1", state: "preview", intent: "communication",
    message: "I would send as WhatsApp media to Google AI Casting. Nothing has been sent yet.",
    comm: COMM_PLAN, requires_confirmation: true,
    context: { v: 1, conversation_id: "c1", pending: { kind: "confirm_comm", plan_id: "sap_c", comm_plan: COMM_PLAN }, sig: "s" },
};
// ---- Phase 4B submission fixtures ----
const SUB_PLAN = {
    action_type: "attach_media", talent: { id: "t_ahana", label: "Ahana Pocha" },
    project: { id: "p1", label: "Google AI" }, submission_id: "sub1",
    submission_status: "pending", form_found: true,
    form: { name: "Ahana Pocha", height: "5'6\"", budget: "30000", availability: "Available" },
    existing_media: [{ category: "take", label: "Take 1", kind: "video", submission_media_id: "sm_t1", url: "https://cdn/t1.mp4" }],
    available_media: [{ category: "intro_video", label: "Intro Video", kind: "video", source_id: "lib_intro", url: "https://cdn/intro.mp4", poster_url: null }],
    missing: [], proposed: [{ category: "intro_video", label: "Intro Video", kind: "video", source_id: "lib_intro" }],
    warnings: [], requires_confirmation: true, executable: true,
};
const SUB_PREVIEW = {
    conversation_id: "c1", state: "preview", intent: "submission",
    message: "I would attach Intro Video to Ahana Pocha's Google AI submission. No changes have been made.",
    sub: SUB_PLAN, requires_confirmation: true,
    context: { v: 1, conversation_id: "c1", pending: { kind: "confirm_submission", plan_id: "sap_s", sub_plan: SUB_PLAN }, sig: "s" },
};
const SUB_ATTACHED = {
    conversation_id: "c1", state: "attached", intent: "submission",
    message: "Submission updated.\nGoogle AI\n✓ Intro Video — attached\n1 of 1 attached.",
    sub: SUB_PLAN,
    result: {
        action_type: "attach_media", project: { id: "p1", label: "Google AI" }, talent: { id: "t_ahana", label: "Ahana Pocha" },
        submission_id: "sub1", counts: { attached: 1 },
        outcomes: [{ label: "Intro Video", status: "attached" }],
    },
    overview: { ...OVERVIEW, summary: { ...OVERVIEW.summary, tests_received: 4 } },
};

// ---- Phase 4C ingest fixtures ----
const INGEST_PLAN = {
    action_type: "ingest_audition", talent: { id: "t_ahana", label: "Ahana Pocha" },
    project: { id: "p1", label: "Google AI" }, submission_id: "sub1", submission_status: "pending",
    form_found: true, form: { name: "Ahana Pocha" },
    existing_media: [{ category: "take", label: "Take 1", kind: "video", submission_media_id: "sm_t1", url: "https://cdn/t1.mp4" }],
    available_media: [], missing: [], proposed: [], warnings: [],
    requires_confirmation: true, executable: true,
    upload: {
        filename: "audition_take_2.mp4", size: 19_300_000, content_type: "video/mp4",
        sha256: "abc123", target_category: "take_2", target_label: "Take 2", replaces: false,
    },
};
const INGEST_PREVIEW = {
    conversation_id: "c1", state: "preview", intent: "submission",
    message: "Incoming audition for Ahana Pocha — Google AI. I would add it as Take 2 on the submission. Nothing has been uploaded yet.",
    sub: INGEST_PLAN, requires_confirmation: true,
    context: { v: 1, conversation_id: "c1", pending: { kind: "confirm_upload", plan_id: "sap_u", sub_plan: INGEST_PLAN }, sig: "s" },
};
const INGEST_UPLOADED = {
    conversation_id: "c1", state: "attached", intent: "submission",
    message: "Audition uploaded.\nGoogle AI\n✓ Take 2 — added on the submission\n1 of 1 uploaded.",
    sub: INGEST_PLAN,
    result: {
        action_type: "upload_audition", project: { id: "p1", label: "Google AI" },
        talent: { id: "t_ahana", label: "Ahana Pocha" }, submission_id: "sub1",
        counts: { uploaded: 1 }, outcomes: [{ label: "Take 2", status: "uploaded" }],
    },
    overview: { ...OVERVIEW, summary: { ...OVERVIEW.summary, tests_received: 9 } },
};

const COMM_QUEUED = {
    conversation_id: "c1", state: "queued", intent: "communication",
    message: "Intro Video — queued\nTake 2 — queued\n2 items queued to the WhatsApp Engine for Google AI Casting.",
    comm: COMM_PLAN,
    result: {
        action_type: "send_media", destination: "Google AI Casting", destination_type: "whatsapp_group",
        destination_label: "Google AI Casting",
        outcomes: [
            { label: "Intro Video", status: "queued", batch_id: "b1" },
            { label: "Take 2", status: "queued", batch_id: "b2" },
        ],
        counts: { queued: 2, failed: 0, total: 2 },
        status_token: "tok", batch_ids: ["b1", "b2"],
    },
    overview: { ...OVERVIEW, summary: { ...OVERVIEW.summary, follow_ups: 7 } },
};

const CLARIFY_RESPONSE = {
    conversation_id: "c1", state: "clarification", intent: "mark_talent_status",
    message: 'I found 3 talents named "Riya":', plan: null, answer: null, requires_confirmation: false,
    clarification: {
        kind: "talent", prompt: "Which one do you mean?",
        options: [
            { index: 1, id: "r1", label: "Riya Sharma", name: "Riya Sharma" },
            { index: 2, id: "r2", label: "Riya Mehta", name: "Riya Mehta" },
            { index: 3, id: "r3", label: "Riya Kapoor", name: "Riya Kapoor" },
        ],
    },
    context: { v: 1, conversation_id: "c1", pending: { kind: "clarify_talent", options: [] } },
};

// jsdom has no media stack.
beforeAll(() => {
    Object.defineProperty(window.HTMLMediaElement.prototype, "play", {
        configurable: true, value: () => Promise.resolve(),
    });
});
afterEach(cleanup);
beforeEach(() => {
    adminApi.get.mockReset();
    adminApi.post.mockReset();
    adminApi.get.mockResolvedValue({ data: OVERVIEW });
});

const renderPage = () =>
    render(
        <MemoryRouter initialEntries={["/admin/simple-assistant"]}>
            <SimpleAssistant />
        </MemoryRouter>,
    );

describe("SimpleAssistant — Phase 1 read-only Command Centre", () => {
    it("wakes up, greets by name, renders real project + talent data", async () => {
        renderPage();
        await waitFor(() => expect(screen.getByTestId("assistant-greeting").textContent).toContain("Good morning, Raj."));
        expect(screen.queryByTestId("assistant-projects")).not.toBeNull();
        expect(screen.getByText("Google AI")).toBeTruthy();
        expect(screen.getByText("Ahana Pocha")).toBeTruthy();
    });

    it("plays an existing video in-place; only ever issues GET reads for data", async () => {
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-play-intro"));
        fireEvent.click(screen.getByTestId("assistant-play-intro"));
        await waitFor(() => expect(screen.queryByTestId("assistant-media-dialog")).not.toBeNull());
        expect(screen.getByTestId("assistant-media-video").getAttribute("src")).toBe("https://cdn/intro.mp4");
    });
});

describe("SimpleAssistant — Phase 2 conversational command layer (dry-run)", () => {
    it("renders the command bar + quick commands", async () => {
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-bar"));
        expect(screen.getByTestId("assistant-command-input")).toBeTruthy();
        expect(screen.getAllByTestId("assistant-quick-command").length).toBeGreaterThan(0);
    });

    it("a quick command populates the input, it does not auto-send", async () => {
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-bar"));
        fireEvent.click(screen.getAllByTestId("assistant-quick-command")[0]);
        expect(screen.getByTestId("assistant-command-input").value.length).toBeGreaterThan(0);
        expect(adminApi.post).not.toHaveBeenCalled();
    });

    it("submitting a command previews an action plan with current → proposed", async () => {
        adminApi.post.mockResolvedValue({ data: PLAN_RESPONSE });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));

        fireEvent.change(screen.getByTestId("assistant-command-input"), {
            target: { value: "Mark Ahana unavailable for Google AI" },
        });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.queryByTestId("assistant-plan")).not.toBeNull());
        const plan = screen.getByTestId("assistant-plan");
        expect(within(plan).getByText("Ahana Pocha")).toBeTruthy();
        expect(within(plan).getByText("Shortlisted")).toBeTruthy();
        expect(within(plan).getByText("Not available")).toBeTruthy();
        expect(screen.getByTestId("assistant-turn-message").textContent).toContain("Ready to make this change");
        expect(screen.getByTestId("assistant-confirm")).toBeTruthy();
        expect(screen.getByTestId("assistant-cancel")).toBeTruthy();
        // submitting a command is preview-only — no confirm endpoint hit
        expect(adminApi.post.mock.calls[0][0]).toBe("/simple-assistant/command");
        expect(adminApi.post.mock.calls.map((c) => c[0])).not.toContain("/simple-assistant/command/confirm");
    });

    it("Confirm executes the plan and shows a per-talent outcome + refreshes the overview", async () => {
        adminApi.post
            .mockResolvedValueOnce({ data: PLAN_RESPONSE })
            .mockResolvedValueOnce({ data: EXECUTED_RESPONSE });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Mark Ahana unavailable for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-confirm"));

        fireEvent.click(screen.getByTestId("assistant-confirm"));
        // confirm posts to the execute endpoint with the signed context
        await waitFor(() => expect(adminApi.post.mock.calls[1][0]).toBe("/simple-assistant/command/confirm"));
        expect(adminApi.post.mock.calls[1][1].context).toEqual(PLAN_RESPONSE.context);

        await waitFor(() => expect(screen.queryByTestId("assistant-exec-result")).not.toBeNull());
        const res = screen.getByTestId("assistant-exec-result");
        expect(within(res).getByText("Ahana Pocha")).toBeTruthy();
        expect(within(res).getByText("Shortlisted → Not available")).toBeTruthy();
        // overview reflects the fresh state returned by the execute call
        await waitFor(() => {
            const chips = screen.getByTestId("assistant-summary").textContent;
            expect(chips).toContain("9");
        });
    });

    it("stale plan is rejected on Confirm and does not claim success", async () => {
        adminApi.post
            .mockResolvedValueOnce({ data: PLAN_RESPONSE })
            .mockResolvedValueOnce({ data: STALE_RESPONSE });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Mark Ahana unavailable for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-confirm"));

        fireEvent.click(screen.getByTestId("assistant-confirm"));
        await waitFor(() => expect(screen.queryByTestId("assistant-stale")).not.toBeNull());
        expect(screen.getByTestId("assistant-stale").textContent).toMatch(/no longer current/i);
        expect(screen.queryByTestId("assistant-exec-result")).toBeNull();
    });

    it("partial failure shows failed rows and never a bare 'Done'", async () => {
        const partial = {
            ...EXECUTED_RESPONSE,
            message: "I couldn't complete every change.\nGoogle AI\nAhana Pocha — Not available\nSana Khan — unchanged; status changed to Locked\n1 of 2 changes completed successfully.",
            result: {
                ...EXECUTED_RESPONSE.result,
                counts: { success: 1, stale: 1 },
                outcomes: [
                    EXECUTED_RESPONSE.result.outcomes[0],
                    { talent_id: "t3", talent_label: "Sana Khan", status: "stale", from: "locked", from_label: "Locked", reason: "status changed to Locked after I prepared the plan" },
                ],
            },
        };
        adminApi.post.mockResolvedValueOnce({ data: PLAN_RESPONSE }).mockResolvedValueOnce({ data: partial });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Mark them unavailable for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-confirm"));
        fireEvent.click(screen.getByTestId("assistant-confirm"));

        await waitFor(() => expect(screen.queryByTestId("assistant-exec-result")).not.toBeNull());
        expect(screen.getByTestId("assistant-turn-message").textContent).toContain("couldn't complete every change");
        const res = screen.getByTestId("assistant-exec-result");
        expect(within(res).getByText("Sana Khan")).toBeTruthy();
    });

    it("Cancel dismisses the plan without any confirm call", async () => {
        adminApi.post.mockResolvedValue({ data: PLAN_RESPONSE });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Mark Ahana unavailable for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-cancel"));

        fireEvent.click(screen.getByTestId("assistant-cancel"));
        await waitFor(() => expect(screen.getByTestId("assistant-turn-message").textContent).toMatch(/cancelled/i));
        expect(adminApi.post).toHaveBeenCalledTimes(1);
    });

    it("renders a clarification card and a pick continues the conversation", async () => {
        adminApi.post
            .mockResolvedValueOnce({ data: CLARIFY_RESPONSE })
            .mockResolvedValueOnce({ data: { ...PLAN_RESPONSE, message: "I would move 1 talent to Not Available in Google AI." } });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Mark Riya unavailable for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.queryByTestId("assistant-clarification")).not.toBeNull());
        const opts = screen.getAllByTestId("assistant-clarify-option");
        expect(opts.length).toBe(3);

        fireEvent.click(opts[1]); // "Riya Mehta"
        await waitFor(() => expect(screen.queryByTestId("assistant-plan")).not.toBeNull());
        // second call carried the round-tripped context
        expect(adminApi.post.mock.calls[1][1].context).toEqual(CLARIFY_RESPONSE.context);
        expect(adminApi.post.mock.calls[1][1].message).toBe("2");
    });

    it("shows a calm error turn when the command call fails", async () => {
        adminApi.post.mockResolvedValue({ data: undefined });
        adminApi.post.mockImplementation(async () => {
            throw Object.assign(new Error("x"), { response: { status: 502, data: { detail: "Could not process that command" } } });
        });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "do something" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => expect(screen.getByTestId("assistant-turn-message").textContent).toMatch(/could not process/i));
    });

    it("keeps the Phase 1 project overview rendering alongside the console", async () => {
        adminApi.post.mockResolvedValue({ data: PLAN_RESPONSE });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Mark Ahana unavailable for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-plan"));
        expect(screen.queryByTestId("assistant-projects")).not.toBeNull();
        // "Ahana Pocha" now appears in BOTH the plan card and the overview panel.
        expect(screen.getAllByText("Ahana Pocha").length).toBeGreaterThanOrEqual(2);
    });

    it("scopes all styling under .tg-assistant — no bare global selectors", async () => {
        const { ASSISTANT_CSS } = await import("@/components/simple-assistant/assistantStyles");
        // No bare element / document selectors at a rule boundary.
        const FORBIDDEN = /(?:^|[};])\s*(?:html|body|:root|\*|input|button|textarea|table|video|a|img|h1|div|span|section|article)\s*[,.:{[]/;
        expect(FORBIDDEN.test(ASSISTANT_CSS)).toBe(false);
        // Heavily namespaced.
        expect((ASSISTANT_CSS.match(/\.tg-assistant/g) || []).length).toBeGreaterThan(80);
        // Keyframe names are all prefixed so they can't shadow app keyframes.
        for (const m of ASSISTANT_CSS.matchAll(/@keyframes\s+([\w-]+)/g)) {
            expect(m[1].startsWith("tga-")).toBe(true);
        }
    });
});

describe("SimpleAssistant — Phase 4A communication + media", () => {
    it("previews a WhatsApp media send with talent / project / destination / media", async () => {
        adminApi.post.mockResolvedValue({ data: COMM_PREVIEW });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), {
            target: { value: "Send Neha's intro and audition take 2 to Google AI casting" },
        });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.queryByTestId("assistant-comm-preview")).not.toBeNull());
        const card = screen.getByTestId("assistant-comm-preview");
        expect(within(card).getByText("Neha Sharma")).toBeTruthy();
        expect(within(card).getByText("Google AI Casting")).toBeTruthy();
        expect(within(card).getByText("Intro Video")).toBeTruthy();
        expect(within(card).getByText("Take 2")).toBeTruthy();
        expect(screen.getByTestId("assistant-comm-confirm")).toBeTruthy();
        // preview is preview-only — no confirm/send endpoint hit
        expect(adminApi.post.mock.calls.map((c) => c[0])).not.toContain("/simple-assistant/command/confirm");
    });

    it("lets the user inspect existing media in-place before sending", async () => {
        adminApi.post.mockResolvedValue({ data: COMM_PREVIEW });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's intro to Google AI casting" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getAllByTestId("assistant-comm-media-play"));

        fireEvent.click(screen.getAllByTestId("assistant-comm-media-play")[0]);
        await waitFor(() => expect(screen.queryByTestId("assistant-comm-media-video")).not.toBeNull());
        expect(screen.getByTestId("assistant-comm-media-video").getAttribute("src")).toBe("https://cdn/intro.mp4");
    });

    it("Confirm Send hits the confirm endpoint with the signed context and shows the queued outcome", async () => {
        adminApi.post
            .mockResolvedValueOnce({ data: COMM_PREVIEW })
            .mockResolvedValueOnce({ data: COMM_QUEUED });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's intro to Google AI casting" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-comm-confirm"));

        fireEvent.click(screen.getByTestId("assistant-comm-confirm"));
        await waitFor(() => expect(adminApi.post.mock.calls[1][0]).toBe("/simple-assistant/command/confirm"));
        expect(adminApi.post.mock.calls[1][1].context).toEqual(COMM_PREVIEW.context);

        await waitFor(() => expect(screen.queryByTestId("assistant-comm-result")).not.toBeNull());
        const res = screen.getByTestId("assistant-comm-result");
        expect(within(res).getByText("Intro Video")).toBeTruthy();
        // overview refreshed from the execute response
        await waitFor(() => expect(screen.getByTestId("assistant-summary").textContent).toContain("7"));
    });

    it("reports a WhatsApp failure without claiming success", async () => {
        const failed = {
            conversation_id: "c1", state: "failed", intent: "communication",
            message: "I couldn't send Neha Sharma's Intro Video to Google AI Casting.\nReason: WhatsApp session not connected\nNo successful send was recorded.",
            comm: COMM_PLAN,
            result: { action_type: "send_media", outcomes: [{ label: "Intro Video", status: "failed", reason: "WhatsApp session not connected" }], counts: { queued: 0, failed: 1, total: 1 } },
        };
        adminApi.post.mockResolvedValueOnce({ data: COMM_PREVIEW }).mockResolvedValueOnce({ data: failed });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's intro to Google AI casting" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-comm-confirm"));
        fireEvent.click(screen.getByTestId("assistant-comm-confirm"));

        await waitFor(() => expect(screen.getByTestId("assistant-turn-message").textContent).toMatch(/no successful send was recorded/i));
        expect(screen.getByTestId("assistant-turn-message").textContent).not.toMatch(/\bqueued\b|\bDone\b/);
    });

    it("reports a partial media failure per file", async () => {
        const partial = {
            ...COMM_QUEUED, state: "partial",
            message: "Intro Video — queued\nTake 2 — failed\nI couldn't complete the entire set — 1 of 2 queued to WhatsApp.",
            result: {
                ...COMM_QUEUED.result, counts: { queued: 1, failed: 1, total: 2 },
                outcomes: [
                    { label: "Intro Video", status: "queued", batch_id: "b1" },
                    { label: "Take 2", status: "failed", reason: "media too large" },
                ],
            },
        };
        adminApi.post.mockResolvedValueOnce({ data: COMM_PREVIEW }).mockResolvedValueOnce({ data: partial });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's intro and take 2 to Google AI casting" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-comm-confirm"));
        fireEvent.click(screen.getByTestId("assistant-comm-confirm"));

        await waitFor(() => expect(screen.queryByTestId("assistant-comm-result")).not.toBeNull());
        const res = screen.getByTestId("assistant-comm-result");
        expect(within(res).getByText("media too large")).toBeTruthy();
        expect(screen.getByTestId("assistant-turn-message").textContent).toContain("couldn't complete the entire set");
    });

    it("previews a profile-link send with the existing generated URL", async () => {
        const linkPlan = {
            ...COMM_PLAN, action_type: "send_profile_link", media: [],
            profile_link: { slug: "neha", url: "https://links.talentgramagency.com/l/neha", title: "Neha Sharma" },
        };
        adminApi.post.mockResolvedValue({
            data: { ...COMM_PREVIEW, comm: linkPlan, message: "I would send the profile link to Google AI Casting. Nothing has been sent yet.",
                context: { ...COMM_PREVIEW.context, pending: { ...COMM_PREVIEW.context.pending, comm_plan: linkPlan } } },
        });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's profile to Google AI casting" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => expect(screen.queryByTestId("assistant-comm-link")).not.toBeNull());
        expect(screen.getByTestId("assistant-comm-link").textContent).toContain("links.talentgramagency.com/l/neha");
    });

    it("an attach-to-submission plan shows no send button (preview only)", async () => {
        const attachPlan = { ...COMM_PLAN, action_type: "attach_media", destination_type: "submission", executable: false,
            warnings: ["Attaching media to a submission isn't enabled in this phase — this is a preview only."] };
        adminApi.post.mockResolvedValue({
            data: { ...COMM_PREVIEW, comm: attachPlan, requires_confirmation: false, context: null,
                message: attachPlan.warnings[0] },
        });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Upload Neha's take 2 to her Google AI submission" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-comm-preview"));
        expect(screen.queryByTestId("assistant-comm-confirm")).toBeNull();
        expect(screen.getByTestId("assistant-comm-warnings").textContent).toMatch(/preview only/i);
    });
});

describe("SimpleAssistant — Phase 5 approved WhatsApp casting forwarding", () => {
    it("asks which WhatsApp destination when the command is ambiguous, then previews the picked one", async () => {
        const clar = {
            conversation_id: "c1", state: "clarification", intent: "communication",
            message: "I found two possible WhatsApp destinations for Neha Sharma. Which one should I use?",
            clarification: {
                kind: "destination", prompt: "I found two possible WhatsApp destinations for Neha Sharma. Which one should I use?",
                options: [
                    { index: 1, id: "project_casting_group", label: "Google AI Casting" },
                    { index: 2, id: "talent_own", label: "Neha Sharma x Talentgram" },
                ],
            },
            context: { v: 1, conversation_id: "c1", pending: { kind: "clarify_comm_destination", options: [] }, sig: "s" },
        };
        const picked = { ...COMM_PREVIEW, comm: { ...COMM_PLAN, destination: "Neha Sharma x Talentgram",
            destination_label: "Neha Sharma's WhatsApp group", destination_source: "talent_own" } };
        adminApi.post.mockResolvedValueOnce({ data: clar }).mockResolvedValueOnce({ data: picked });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's Take 2 to Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.getAllByTestId("assistant-clarify-option").length).toBe(2));
        fireEvent.click(screen.getAllByTestId("assistant-clarify-option")[1]);
        await waitFor(() => expect(screen.queryByTestId("assistant-comm-preview")).not.toBeNull());
        expect(screen.getByTestId("assistant-comm-preview").textContent).toContain("Neha Sharma's WhatsApp group");
        expect(adminApi.post.mock.calls[1][1].message).toBe("2");
    });

    it("reports a stale send plan and queues nothing", async () => {
        const stale = {
            conversation_id: "c1", state: "stale", intent: "communication",
            message: "The send plan is no longer current.\n\nTake 2 has changed since the preview was prepared.\n\nI've refreshed the submission — please review and confirm again.",
            comm: COMM_PLAN, need_refresh: true,
        };
        adminApi.post.mockResolvedValueOnce({ data: COMM_PREVIEW }).mockResolvedValueOnce({ data: stale });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Send Neha's intro and Take 2 to the Google AI casting group" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-comm-confirm"));
        fireEvent.click(screen.getByTestId("assistant-comm-confirm"));

        await waitFor(() => expect(screen.queryByTestId("assistant-comm-stale")).not.toBeNull());
        expect(screen.getByTestId("assistant-comm-stale").textContent).toMatch(/no longer current/i);
        expect(screen.queryByTestId("assistant-comm-result")).toBeNull();
    });
});

describe("SimpleAssistant — Phase 4B incoming audition / submission media", () => {
    it("shows a submission preview: form + existing + available media + proposed attachment", async () => {
        adminApi.post.mockResolvedValue({ data: SUB_PREVIEW });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Attach Ahana's intro to Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.queryByTestId("assistant-sub-preview")).not.toBeNull());
        const card = screen.getByTestId("assistant-sub-preview");
        expect(within(card).getAllByText("Ahana Pocha").length).toBeGreaterThan(0);
        expect(within(card).getByTestId("assistant-sub-form").textContent).toMatch(/5'6"/);
        expect(within(card).getByTestId("assistant-sub-existing").textContent).toContain("Take 1");
        expect(within(card).getByTestId("assistant-sub-available").textContent).toContain("Intro Video");
        expect(within(card).getByTestId("assistant-sub-proposed").textContent).toContain("Intro Video");
        expect(screen.getByTestId("assistant-sub-confirm")).toBeTruthy();
        // preview only — no confirm endpoint hit
        expect(adminApi.post.mock.calls.map((c) => c[0])).not.toContain("/simple-assistant/command/confirm");
    });

    it("lets the user inspect existing media inline before attaching", async () => {
        adminApi.post.mockResolvedValue({ data: SUB_PREVIEW });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Prepare Ahana's Google AI submission" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-available"));

        const btn = within(screen.getByTestId("assistant-sub-available")).getAllByRole("button")[0];
        fireEvent.click(btn);
        await waitFor(() => expect(screen.queryByTestId("assistant-sub-media-video")).not.toBeNull());
        expect(screen.getByTestId("assistant-sub-media-video").getAttribute("src")).toBe("https://cdn/intro.mp4");
    });

    it("Confirm Attach calls the confirm endpoint with the signed context and shows the result + refreshes overview", async () => {
        adminApi.post.mockResolvedValueOnce({ data: SUB_PREVIEW }).mockResolvedValueOnce({ data: SUB_ATTACHED });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Attach Ahana's intro to Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-confirm"));

        fireEvent.click(screen.getByTestId("assistant-sub-confirm"));
        await waitFor(() => expect(adminApi.post.mock.calls[1][0]).toBe("/simple-assistant/command/confirm"));
        expect(adminApi.post.mock.calls[1][1].context).toEqual(SUB_PREVIEW.context);

        await waitFor(() => expect(screen.queryByTestId("assistant-sub-result")).not.toBeNull());
        expect(within(screen.getByTestId("assistant-sub-result")).getByText("Intro Video")).toBeTruthy();
        await waitFor(() => expect(screen.getByTestId("assistant-summary").textContent).toContain("4"));
    });

    it("stale submission is reported and nothing is re-attached", async () => {
        const stale = {
            conversation_id: "c1", state: "stale", intent: "submission",
            message: "This submission changed after I prepared the plan — Intro Video is already attached. I've refreshed the submission; nothing was attached again.",
            sub: SUB_PLAN, need_refresh: true,
        };
        adminApi.post.mockResolvedValueOnce({ data: SUB_PREVIEW }).mockResolvedValueOnce({ data: stale });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Attach Ahana's intro to Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-confirm"));
        fireEvent.click(screen.getByTestId("assistant-sub-confirm"));

        await waitFor(() => expect(screen.queryByTestId("assistant-sub-stale")).not.toBeNull());
        expect(screen.getByTestId("assistant-sub-stale").textContent).toMatch(/already attached/i);
        expect(screen.queryByTestId("assistant-sub-result")).toBeNull();
    });

    it("an inspect-only submission plan shows no confirm button", async () => {
        const inspectPlan = { ...SUB_PLAN, action_type: "inspect_submission", proposed: [], requires_confirmation: false, executable: false };
        adminApi.post.mockResolvedValue({
            data: { ...SUB_PREVIEW, sub: inspectPlan, requires_confirmation: false, context: null,
                message: "Ahana Pocha — Google AI. Submission pending. Nothing missing." },
        });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Prepare Ahana's Google AI submission" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-preview"));
        expect(screen.queryByTestId("assistant-sub-confirm")).toBeNull();
        expect(screen.getByTestId("assistant-sub-existing")).toBeTruthy();
    });

    it("partial attach failure is reported per item", async () => {
        const partial = {
            ...SUB_ATTACHED, state: "partial",
            message: "Submission updated partially.\nGoogle AI\n✓ Intro Video — attached\n✕ Portfolio image — failed\n1 of 2 attached.",
            result: {
                ...SUB_ATTACHED.result, counts: { attached: 1, failed: 1 },
                outcomes: [
                    { label: "Intro Video", status: "attached" },
                    { label: "Portfolio image", status: "failed", reason: "not found on this talent's profile" },
                ],
            },
        };
        adminApi.post.mockResolvedValueOnce({ data: SUB_PREVIEW }).mockResolvedValueOnce({ data: partial });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Attach Ahana's intro and photos to Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-confirm"));
        fireEvent.click(screen.getByTestId("assistant-sub-confirm"));

        await waitFor(() => expect(screen.queryByTestId("assistant-sub-result")).not.toBeNull());
        expect(screen.getByTestId("assistant-turn-message").textContent).toContain("partially");
        expect(within(screen.getByTestId("assistant-sub-result")).getByText("not found on this talent's profile")).toBeTruthy();
    });
});

describe("SimpleAssistant — Phase 4C incoming audition file ingest", () => {
    const pickFile = () => {
        const file = new File([new Uint8Array(64)], "audition_take_2.mp4", { type: "video/mp4" });
        fireEvent.change(screen.getByTestId("assistant-file-input"), { target: { files: [file] } });
        return file;
    };

    it("shows the attached file as a chip and can remove it", async () => {
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-file-input"));
        pickFile();
        await waitFor(() => expect(screen.queryByTestId("assistant-file-chip")).not.toBeNull());
        expect(screen.getByTestId("assistant-file-chip").textContent).toContain("audition_take_2.mp4");
        fireEvent.click(screen.getByTestId("assistant-file-remove"));
        await waitFor(() => expect(screen.queryByTestId("assistant-file-chip")).toBeNull());
    });

    it("previews an INCOMING AUDITION plan (file_meta sent, no bytes, no upload endpoint)", async () => {
        adminApi.post.mockResolvedValue({ data: INGEST_PREVIEW });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        pickFile();
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Upload this as Ahana's Take 2 for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.queryByTestId("assistant-sub-upload")).not.toBeNull());
        expect(screen.getByTestId("assistant-upload-filename").textContent).toBe("audition_take_2.mp4");
        expect(screen.getByTestId("assistant-upload-target").textContent).toBe("Take 2");
        expect(screen.getByText("Confirm Upload")).toBeTruthy();

        // /command carried file_meta (name/size/type), never the file itself
        const body = adminApi.post.mock.calls[0][1];
        expect(adminApi.post.mock.calls[0][0]).toBe("/simple-assistant/command");
        expect(body.file_meta.filename).toBe("audition_take_2.mp4");
        expect(body.file_meta.size).toBe(64);
        expect(adminApi.post.mock.calls.map((c) => c[0])).not.toContain("/simple-assistant/command/upload");
    });

    it("Confirm Upload posts the bytes once to /command/upload and shows the result + refreshes overview", async () => {
        adminApi.post.mockResolvedValueOnce({ data: INGEST_PREVIEW }).mockResolvedValueOnce({ data: INGEST_UPLOADED });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        pickFile();
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Upload this as Ahana's Take 2 for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-confirm"));

        fireEvent.click(screen.getByTestId("assistant-sub-confirm"));
        await waitFor(() => expect(adminApi.post.mock.calls[1][0]).toBe("/simple-assistant/command/upload"));
        expect(adminApi.post.mock.calls[1][1] instanceof FormData).toBe(true);

        await waitFor(() => expect(screen.queryByTestId("assistant-sub-result")).not.toBeNull());
        expect(within(screen.getByTestId("assistant-sub-result")).getByText("Take 2")).toBeTruthy();
        await waitFor(() => expect(screen.getByTestId("assistant-summary").textContent).toContain("9"));
    });

    it("a non-executable ingest preview (bad file) shows no confirm button", async () => {
        const bad = {
            ...INGEST_PREVIEW,
            message: "I can't upload that yet. That file is 900 MB — the submission video limit is 200 MB.",
            requires_confirmation: false,
            sub: { ...INGEST_PLAN, executable: false, requires_confirmation: false,
                warnings: ["That file is 900 MB — the submission video limit is 200 MB."] },
            context: null,
        };
        adminApi.post.mockResolvedValue({ data: bad });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        pickFile();
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Upload this as Ahana's Take 2 for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-sub-preview"));
        expect(screen.queryByTestId("assistant-sub-confirm")).toBeNull();
        expect(screen.getByTestId("assistant-sub-warnings").textContent).toMatch(/200 MB/);
    });

    it("a command with no file just relays the backend's 'attach a file' message", async () => {
        adminApi.post.mockResolvedValue({
            data: { conversation_id: "c1", state: "blocked", intent: "submission",
                message: "I don't have a file attached to this command. Attach the audition video first, then ask me again." },
        });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Upload this as Ahana's Take 2 for Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => expect(screen.getByTestId("assistant-turn-message").textContent).toMatch(/attach the audition video/i));
        expect(adminApi.post.mock.calls.map((c) => c[0])).not.toContain("/simple-assistant/command/upload");
    });
});

describe("SimpleAssistant — Phase 6 WhatsApp delivery status (read-only)", () => {
    const STATUS_CARD = {
        conversation_id: "c1", state: "answer", intent: "whatsapp_status",
        message: "Ahana Pocha — Google AI: ✓ VERIFIED. The WhatsApp worker reports the message as sent and verified.",
        status: {
            plan_id: "sap_1", talent: { id: "t_ahana", label: "Ahana Pocha" },
            project: { id: "p1", label: "Google AI" }, destination: "Google AI Casting",
            media: [{ label: "Intro Video", status: "verified" }, { label: "Take 2", status: "sent" }],
            overall: "verified", overall_copy: "The WhatsApp worker reports the message as sent and verified.",
            jobs: [{ status: "verified" }, { status: "sent" }], job_count: 2,
            queued_at: "2026-09-06T11:42:00Z", sent_at: "2026-09-06T11:43:00Z", failure_reason: null, failed_count: 0,
        },
    };

    it("renders a WHATSAPP STATUS card from the existing engine's job state — no confirm/send endpoints", async () => {
        adminApi.post.mockResolvedValue({ data: STATUS_CARD });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Did Ahana's Google AI send go through?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));

        await waitFor(() => expect(screen.queryByTestId("assistant-status-card")).not.toBeNull());
        const card = screen.getByTestId("assistant-status-card");
        expect(card.textContent).toContain("Google AI Casting");
        expect(within(card).getByTestId("assistant-status-media").textContent).toContain("Intro Video");
        expect(within(card).getByTestId("assistant-status-overall").textContent).toContain("VERIFIED");
        const posted = adminApi.post.mock.calls.map((c) => c[0]);
        expect(posted).not.toContain("/simple-assistant/command/confirm");
        expect(posted).not.toContain("/simple-assistant/command/upload");
    });

    it("shows the failure reason verbatim with 'no automatic retry'", async () => {
        const failed = {
            ...STATUS_CARD,
            message: "Ahana Pocha — Google AI: ✕ FAILED. The send failed.\nReason: INVALID_DESTINATION: group not found\nNo automatic retry was performed.",
            status: { ...STATUS_CARD.status, overall: "failed", overall_copy: "The send failed.",
                failure_reason: "INVALID_DESTINATION: group not found", failed_count: 1,
                media: [{ label: "Intro Video", status: "failed" }] },
        };
        adminApi.post.mockResolvedValue({ data: failed });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "What happened to Ahana's Google AI casting message?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => expect(screen.queryByTestId("assistant-status-failure")).not.toBeNull());
        expect(screen.getByTestId("assistant-status-failure").textContent).toContain("INVALID_DESTINATION: group not found");
        expect(screen.getByTestId("assistant-status-failure").textContent).toMatch(/no automatic retry/i);
    });

    it("shows 'no reply yet' safely when nothing was captured (never a false negative)", async () => {
        adminApi.post.mockResolvedValue({
            data: { conversation_id: "c1", state: "answer", intent: "whatsapp_status",
                message: "I don't see a WhatsApp reply from Ahana Pocha about Google AI yet." },
        });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Did Ahana reply about Google AI?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => expect(screen.getByTestId("assistant-turn-message").textContent).toMatch(/don't see a whatsapp reply/i));
        expect(screen.getByTestId("assistant-turn-message").textContent).not.toMatch(/has not replied|hasn't replied/i);
        expect(screen.queryByTestId("assistant-inbound-card")).toBeNull();
    });
});

describe("SimpleAssistant — Phase 7 inbound WhatsApp reply capture (read-only)", () => {
    const REPLY = {
        conversation_id: "c1", state: "answer", intent: "whatsapp_status",
        message: 'Ahana Pocha — Google AI: "Yes, I\'m available."',
        inbound: {
            inbound_id: "inb_1", talent: { id: "t_ahana", label: "Ahana Pocha" }, talent_resolution: "resolved",
            sender_phone_masked: "+91 981XXX 01", sender_name: "Ahana",
            project: { id: "p1", label: "Google AI" }, project_resolution: "casting_group", project_candidates: [],
            group_name: "Google AI Casting", received_at: "2026-09-06T12:08:00Z",
            message_text: "Yes, I'm available.", source: "WhatsApp",
        },
    };

    it("renders a WHATSAPP REPLY card with the verbatim message and resolution ticks", async () => {
        adminApi.post.mockResolvedValue({ data: REPLY });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Did Ahana reply about Google AI?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => expect(screen.queryByTestId("assistant-inbound-card")).not.toBeNull());
        const card = screen.getByTestId("assistant-inbound-card");
        expect(within(card).getByTestId("assistant-inbound-talent").textContent).toBe("Ahana Pocha");
        expect(within(card).getByTestId("assistant-inbound-project").textContent).toBe("Google AI");
        expect(within(card).getByTestId("assistant-inbound-text").textContent).toContain("Yes, I'm available.");
        expect(card.textContent).toContain("✓ Talent");
        expect(card.textContent).toContain("✓ Project");
        // never interpreted
        expect(card.textContent).not.toMatch(/interested|negotiat|available:? yes/i);
    });

    it("shows an unresolved-sender card with the phone and 'no action was taken'", async () => {
        const unresolved = {
            ...REPLY, message: 'Inbound WhatsApp message from +91 981XXX 34 — talent unresolved. "I can do it."',
            inbound: { ...REPLY.inbound, talent: null, talent_resolution: "unresolved",
                sender_phone_masked: "+91 981XXX 34",
                project: null, project_resolution: "casting_group", message_text: "I can do it." },
        };
        adminApi.post.mockResolvedValue({ data: unresolved });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Show Ahana's latest reply" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-inbound-card"));
        const card = screen.getByTestId("assistant-inbound-card");
        expect(within(card).getByTestId("assistant-inbound-talent").textContent).toMatch(/unresolved/i);
        expect(card.textContent).toContain("+91 981XXX 34");
        expect(card.textContent).toMatch(/no action was taken/i);
    });

    it("shows an ambiguous-project card listing candidates without assigning one", async () => {
        const ambig = {
            ...REPLY, message: 'Reply from Ahana Pocha — project could not be determined reliably. "I can do it."',
            inbound: { ...REPLY.inbound, project: null, project_resolution: "ambiguous_project",
                project_candidates: [{ id: "p1", label: "Google AI" }, { id: "p2", label: "L'Oréal Glyco Bright" }],
                message_text: "I can do it." },
        };
        adminApi.post.mockResolvedValue({ data: ambig });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Did Ahana reply?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-inbound-candidates"));
        const cands = screen.getByTestId("assistant-inbound-candidates");
        expect(cands.textContent).toContain("Google AI");
        expect(cands.textContent).toContain("L'Oréal Glyco Bright");
        expect(cands.textContent).toMatch(/no project was assigned/i);
    });
});

describe("SimpleAssistant — Phase 8 read-only inbound intelligence", () => {
    const INTEL = {
        conversation_id: "c1", state: "answer", intent: "whatsapp_status",
        message: 'Ahana Pocha — Google AI: "What is the budget?"\nTopic: budget (high) · answerable. No message was sent.',
        intelligence: {
            inbound_id: "inb_1", talent: { id: "t_ahana", label: "Ahana Pocha" }, talent_resolution: "resolved",
            project: { id: "p1", label: "Google AI" }, project_resolution: "casting_group", project_candidates: [],
            received_at: "2026-09-06T12:08:00Z", message_text: "What is the budget?", source: "WhatsApp",
            topic: "budget", topic_confidence: "high",
            signals: { is_question: true, date: { resolution: "none" }, availability_signal: null, interest_signal: null, negotiation_request: false },
            project_knowledge: { field: "budget_per_day", value: "₹35,000", available: true, hidden_from_talent: false, label: "budget" },
            answerability: "answerable", proposed_response: "The budget for Google AI is ₹35,000.", next_step: null,
            needs_review: false, pipeline_mutation: "none", no_message_sent: true,
        },
    };

    it("renders an intelligence card with topic, project knowledge, proposed response and NO MESSAGE SENT", async () => {
        adminApi.post.mockResolvedValue({ data: INTEL });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "What is Ahana asking about Google AI?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-card"));
        const c = screen.getByTestId("assistant-intel-card");
        expect(within(c).getByTestId("assistant-intel-topic").textContent).toMatch(/budget/);
        expect(within(c).getByTestId("assistant-intel-knowledge").textContent).toContain("₹35,000");
        expect(within(c).getByTestId("assistant-intel-answerability").textContent).toBe("Answerable");
        expect(within(c).getByTestId("assistant-intel-proposed").textContent).toContain("The budget for Google AI is ₹35,000.");
        expect(within(c).getByTestId("assistant-intel-nosend").textContent).toContain("NO MESSAGE SENT");
        const posted = adminApi.post.mock.calls.map((x) => x[0]);
        expect(posted).not.toContain("/simple-assistant/command/confirm");
        expect(posted).not.toContain("/simple-assistant/command/upload");
    });

    it("shows 'Needs review' for a low-confidence classification", async () => {
        const low = {
            ...INTEL, message: 'Topic: availability (medium · Needs review) · partially_answerable. No message was sent.',
            intelligence: { ...INTEL.intelligence, topic: "availability", topic_confidence: "medium",
                answerability: "partially_answerable", needs_review: true,
                proposed_response: null, next_step: "Talent has stated an availability signal — no pipeline change was made." },
        };
        adminApi.post.mockResolvedValue({ data: low });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Analyze Ahana's latest message" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-confidence"));
        expect(screen.getByTestId("assistant-intel-confidence").textContent).toMatch(/needs review/i);
        expect(screen.getByTestId("assistant-intel-nextstep").textContent).toMatch(/no pipeline change/i);
        expect(screen.queryByTestId("assistant-intel-proposed")).toBeNull();
    });

    it("for a negotiation request shows the current budget but proposes nothing", async () => {
        const nego = {
            ...INTEL, message: 'Topic: budget (high) · partially_answerable. No message was sent.',
            intelligence: { ...INTEL.intelligence, topic: "budget",
                signals: { ...INTEL.intelligence.signals, negotiation_request: true },
                answerability: "partially_answerable", proposed_response: null,
                next_step: "Requires admin decision — no counteroffer was made." },
        };
        adminApi.post.mockResolvedValue({ data: nego });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "What is Ahana asking about Google AI?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-card"));
        expect(screen.getByTestId("assistant-intel-knowledge").textContent).toContain("₹35,000");
        expect(screen.queryByTestId("assistant-intel-proposed")).toBeNull();
        expect(screen.getByTestId("assistant-intel-nextstep").textContent).toMatch(/admin decision/i);
    });

    it("for an ambiguous project lists candidates and uses no project info", async () => {
        const ambig = {
            ...INTEL, message: "I found an inbound WhatsApp message from Ahana Pocha, but I cannot reliably determine the project.\n\nPossible projects:\n1. Google AI\n2. L'Oréal\n\nNo project information was used.",
            intelligence: { ...INTEL.intelligence, project: null, project_resolution: "ambiguous_project",
                project_candidates: [{ id: "p1", label: "Google AI" }, { id: "p2", label: "L'Oréal" }],
                topic: "budget", project_knowledge: { available: false, label: "budget" }, proposed_response: null },
        };
        adminApi.post.mockResolvedValue({ data: ambig });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Why did Ahana message?" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-candidates"));
        expect(screen.getByTestId("assistant-intel-candidates").textContent).toContain("Google AI");
        expect(screen.getByTestId("assistant-intel-candidates").textContent).toMatch(/no project information was used/i);
        expect(screen.queryByTestId("assistant-intel-proposed")).toBeNull();
    });
});

describe("SimpleAssistant — Phase 9 human-approved AI WhatsApp response", () => {
    const DRAFT = {
        conversation_id: "c1", state: "answer", intent: "whatsapp_status",
        message: "AI-drafted reply to Ahana Pocha — review, edit if you want, then Approve & Send. Nothing has been sent.",
        requires_confirmation: true,
        intelligence: {
            inbound_id: "inb_1", talent: { id: "t_ahana", label: "Ahana Pocha" }, talent_resolution: "resolved",
            project: { id: "p1", label: "Google AI" }, project_resolution: "casting_group",
            message_text: "What is the budget?", topic: "budget", topic_confidence: "high",
            project_knowledge: { label: "budget", value: "₹35,000", available: true, hidden_from_talent: false },
            answerability: "answerable", proposed_response: "The budget for Google AI is ₹35,000.",
            needs_review: false, no_message_sent: true,
            ai_draft: {
                text: "Hi Ahana, the budget for the Google AI project is ₹35,000. Let us know if you'd like to proceed.",
                mode: "informational", confidence: "high", grounded: true, needs_review: true,
                editable: true, no_message_sent: true, insufficient_information: false,
            },
        },
        context: { v: 1, conversation_id: "c1", pending: { kind: "confirm_ai_response", plan_id: "sap_ai", inbound_id: "inb_1", talent_id: "t_ahana" }, sig: "s" },
    };
    const QUEUED = {
        conversation_id: "c1", state: "queued", intent: "communication",
        message: 'Reply to Ahana Pocha queued to WhatsApp:\n\n"..."\n\n1 item queued.',
        result: { action_type: "approve_ai_response", outcomes: [{ label: "AI reply", status: "queued", batch_id: "b1" }], counts: { queued: 1, failed: 0, total: 1 }, batch_ids: ["b1"] },
        overview: { ...OVERVIEW, summary: { ...OVERVIEW.summary, tests_received: 7 } },
    };

    it("shows the AI draft with an editable box, 'Needs your approval', and NO MESSAGE SENT until approved", async () => {
        adminApi.post.mockResolvedValue({ data: DRAFT });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Draft a reply to Ahana about Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-draft"));
        const box = screen.getByTestId("assistant-intel-draft-text");
        expect(box.value).toContain("₹35,000");
        expect(screen.getByTestId("assistant-intel-draft").textContent).toMatch(/needs your approval/i);
        expect(screen.getByTestId("assistant-intel-nosend").textContent).toContain("NO MESSAGE SENT");
        expect(screen.getByTestId("assistant-intel-approve")).toBeTruthy();
        // preview only — no confirm endpoint hit yet
        expect(adminApi.post.mock.calls.map((c) => c[0])).not.toContain("/simple-assistant/command/confirm");
    });

    it("Approve & Send posts the edited text and the signed context, then shows 'queued'", async () => {
        adminApi.post.mockResolvedValueOnce({ data: DRAFT }).mockResolvedValueOnce({ data: QUEUED });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Draft a reply to Ahana" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-draft-text"));

        const box = screen.getByTestId("assistant-intel-draft-text");
        fireEvent.change(box, { target: { value: "Hi Ahana, the budget for this project is ₹35,000. Please confirm." } });
        fireEvent.click(screen.getByTestId("assistant-intel-approve"));

        await waitFor(() => expect(adminApi.post.mock.calls[1][0]).toBe("/simple-assistant/command/confirm"));
        expect(adminApi.post.mock.calls[1][1].final_text).toBe("Hi Ahana, the budget for this project is ₹35,000. Please confirm.");
        expect(adminApi.post.mock.calls[1][1].context).toEqual(DRAFT.context);
        await waitFor(() => expect(screen.queryByTestId("assistant-intel-sent")).not.toBeNull());
        expect(screen.getByTestId("assistant-intel-sent").textContent).toMatch(/queued to whatsapp/i);
    });

    it("a generation failure shows the reason and no approve button", async () => {
        const failed = {
            ...DRAFT, message: '"What are the payment terms?" — I couldn\'t generate a response (unavailable). No message was sent.',
            requires_confirmation: false, context: null,
            intelligence: { ...DRAFT.intelligence, ai_draft: { failed: true, reason: "The AI provider is not configured." } },
        };
        adminApi.post.mockResolvedValue({ data: failed });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Draft a reply to Ahana" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-draft-failed"));
        expect(screen.getByTestId("assistant-intel-draft-failed").textContent).toMatch(/no message was sent/i);
        expect(screen.queryByTestId("assistant-intel-approve")).toBeNull();
    });

    it("a stale-facts rejection at approval is shown and nothing is sent", async () => {
        const stale = {
            conversation_id: "c1", state: "stale", intent: "communication", need_refresh: true,
            message: "The project information changed since this response was drafted. Please review the response again.",
        };
        adminApi.post.mockResolvedValueOnce({ data: DRAFT }).mockResolvedValueOnce({ data: stale });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Draft a reply to Ahana" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-approve"));
        fireEvent.click(screen.getByTestId("assistant-intel-approve"));
        await waitFor(() => expect(screen.queryByTestId("assistant-intel-approve-blocked")).not.toBeNull());
        expect(screen.getByTestId("assistant-intel-approve-blocked").textContent).toMatch(/changed since this response was drafted/i);
        expect(screen.queryByTestId("assistant-intel-sent")).toBeNull();
    });
});

describe("SimpleAssistant — Phase 10 multi-intent card", () => {
    const MULTI = {
        conversation_id: "c1", state: "answer", intent: "whatsapp_status",
        message: 'Intents: interest, budget, shoot_date · partially_answerable. No message was sent.',
        requires_confirmation: true,
        intelligence: {
            inbound_id: "inb_1", talent: { id: "t_ahana", label: "Ahana Pocha" }, talent_resolution: "resolved",
            project: { id: "p1", label: "Google AI" }, project_resolution: "casting_group",
            message_text: "Hi, I'm interested. What is the budget and when is the shoot?",
            intents: [
                { topic: "interest", confidence: "high" },
                { topic: "budget", confidence: "high" },
                { topic: "shoot_date", confidence: "high" },
            ],
            topic: "budget", topic_confidence: "high",
            project_knowledge: {}, answerability: "answerable", proposed_response: null, needs_review: false,
            no_message_sent: true,
            project_knowledge_multi: { facts: { budget: "₹35,000", "shoot date": "15 September 2026" }, missing: [], hidden: [] },
            ai_draft: {
                text: "Hi Ahana, the budget for the project is ₹35,000 and the shoot is scheduled for 15 September 2026. Please let us know if you'd like to proceed.",
                mode: "informational", confidence: "high", grounded: true, needs_review: true,
                editable: true, no_message_sent: true, answered_intents: ["budget", "shoot date"], unanswered_intents: [],
            },
        },
        context: { v: 1, conversation_id: "c1", pending: { kind: "confirm_ai_response", plan_id: "sap_ai", inbound_id: "inb_1", talent_id: "t_ahana" }, sig: "s" },
    };

    it("lists every detected intent and every verified fact, plus a draft answering all of them", async () => {
        adminApi.post.mockResolvedValue({ data: MULTI });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Draft a reply to Ahana about Google AI" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-detected"));
        const det = screen.getByTestId("assistant-intel-detected").textContent;
        expect(det).toContain("interest");
        expect(det).toContain("budget");
        expect(det).toContain("shoot date");
        const ver = screen.getByTestId("assistant-intel-verified").textContent;
        expect(ver).toContain("₹35,000");
        expect(ver).toContain("15 September 2026");
        const box = screen.getByTestId("assistant-intel-draft-text");
        expect(box.value).toContain("₹35,000");
        expect(box.value).toContain("15 September 2026");
        expect(screen.getByTestId("assistant-intel-nosend").textContent).toContain("NO MESSAGE SENT");
    });

    it("shows a partial-answer state — one fact verified, one unavailable", async () => {
        const partial = {
            ...MULTI,
            intelligence: {
                ...MULTI.intelligence, answerability: "partially_answerable", needs_review: true,
                intents: [{ topic: "budget", confidence: "high" }, { topic: "payment", confidence: "high" }],
                project_knowledge_multi: { facts: { budget: "₹35,000" }, missing: ["payment terms"], hidden: [] },
                ai_draft: { ...MULTI.intelligence.ai_draft, text: "Hi Ahana, the budget is ₹35,000. I don't have the payment terms yet, but I'll check and get back to you." },
            },
        };
        adminApi.post.mockResolvedValue({ data: partial });
        renderPage();
        await waitFor(() => screen.getByTestId("assistant-command-input"));
        fireEvent.change(screen.getByTestId("assistant-command-input"), { target: { value: "Draft a reply to Ahana" } });
        fireEvent.click(screen.getByTestId("assistant-command-send"));
        await waitFor(() => screen.getByTestId("assistant-intel-verified"));
        const ver = screen.getByTestId("assistant-intel-verified").textContent;
        expect(ver).toContain("₹35,000");
        expect(ver).toMatch(/payment terms/i);
        expect(ver).toMatch(/not available in talentgram/i);
        expect(screen.getByTestId("assistant-intel-answerability").textContent).toMatch(/partially/i);
    });
});
