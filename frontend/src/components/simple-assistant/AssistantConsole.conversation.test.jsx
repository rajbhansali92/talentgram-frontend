import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, within } from "@testing-library/react";

import AssistantConsole from "./AssistantConsole";

afterEach(cleanup);

const noop = () => {};

function renderTurns(turns) {
    return render(
        <AssistantConsole turns={turns} busy={false} onConfirm={noop} onCancel={noop} onPick={noop} />,
    );
}

describe("AssistantConsole — Phase 12 conversation continuity", () => {
    it("renders the read-only conversation answer card, scoped + bounded", () => {
        renderTurns([{
            id: "t1",
            user: "Show Ahana's recent conversation about Google AI",
            response: {
                state: "answer",
                message: "Recent conversation with Ahana Pocha about Google AI (2 messages):",
                answer: {
                    kind: "conversation",
                    talent: "Ahana Pocha",
                    project: "Google AI",
                    window_hours: 168,
                    truncated: true,
                    messages: [
                        { direction: "in", timestamp: "2026-09-05T10:00:00Z", message_text: "Is this for 15 September?", source: "whatsapp_worker" },
                        { direction: "out", timestamp: "2026-09-05T10:05:00Z", message_text: "Yes, the shoot is on 15 September.", source: "whatsapp_engine" },
                    ],
                },
            },
        }]);
        const card = screen.getByTestId("assistant-conversation");
        expect(within(card).getByText(/scoped to Google AI/)).toBeTruthy();
        expect(within(card).getByText(/older messages not shown/)).toBeTruthy();
        const msgs = within(card).getAllByTestId("assistant-conversation-msg");
        expect(msgs).toHaveLength(2);
        expect(msgs[0].getAttribute("data-direction")).toBe("in");
        expect(msgs[1].getAttribute("data-direction")).toBe("out");
        expect(card.textContent).toContain("Is this for 15 September?");
    });

    it("shows RECENT CONVERSATION inside the intelligence card as non-authoritative context", () => {
        renderTurns([{
            id: "t2",
            user: "Draft a reply to Ahana about Google AI",
            response: {
                state: "answer",
                message: "AI-drafted reply — review, edit, then Approve & Send.",
                intelligence: {
                    inbound_id: "inb_1",
                    talent: { id: "t_ahana", label: "Ahana Pocha" },
                    project: { id: "p_g", label: "Google AI" },
                    message_text: "Okay, and what's the budget?",
                    topic: "budget",
                    topic_confidence: "high",
                    answerability: "answerable",
                    intents: [{ topic: "budget", confidence: "high" }],
                    project_knowledge_multi: { facts: { budget: "₹35,000" }, missing: [], hidden: [] },
                    recent_conversation: {
                        window_hours: 168,
                        truncated: false,
                        messages: [
                            { direction: "in", timestamp: "2026-09-05T09:00:00Z", message_text: "Is this for 15 September?" },
                        ],
                    },
                    ai_draft: {
                        text: "Hi Ahana, the budget for Google AI is ₹35,000.",
                        mode: "informational", confidence: "high",
                        grounded: true, needs_review: true, editable: true, no_message_sent: true,
                        answered_intents: ["budget"], unanswered_intents: [],
                    },
                },
                context: { conversation_id: "c1", sig: "x" },
            },
        }]);
        const convo = screen.getByTestId("assistant-intel-conversation");
        expect(convo.textContent).toMatch(/not authoritative/i);
        expect(convo.textContent).toContain("Is this for 15 September?");
        expect(screen.getByTestId("assistant-intel-conversation-msg").getAttribute("data-direction")).toBe("in");
        // the draft itself still renders for human approval
        expect(screen.getByTestId("assistant-intel-draft-text")).toBeTruthy();
    });

    it("omits the conversation block entirely when there is no history", () => {
        renderTurns([{
            id: "t3",
            user: "Draft a reply to Ahana about Google AI",
            response: {
                state: "answer", message: "AI-drafted reply.",
                intelligence: {
                    inbound_id: "inb_2",
                    talent: { id: "t_ahana", label: "Ahana Pocha" },
                    project: { id: "p_g", label: "Google AI" },
                    message_text: "What's the budget?",
                    topic: "budget", topic_confidence: "high", answerability: "answerable",
                    intents: [{ topic: "budget", confidence: "high" }],
                    project_knowledge_multi: { facts: { budget: "₹35,000" }, missing: [], hidden: [] },
                    ai_draft: {
                        text: "Hi Ahana, the budget is ₹35,000.", mode: "informational", confidence: "high",
                        grounded: true, needs_review: true, editable: true, no_message_sent: true,
                    },
                },
                context: { conversation_id: "c1", sig: "x" },
            },
        }]);
        expect(screen.queryByTestId("assistant-intel-conversation")).toBeNull();
    });
});
