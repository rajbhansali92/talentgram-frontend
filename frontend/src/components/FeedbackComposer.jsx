import React, { useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { Mic, MessageCircle, Loader2, Send, ShieldCheck } from "lucide-react";
import VoiceRecorder from "@/components/VoiceRecorder";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

/**
 * Client-side feedback composer used inside the talent detail overlay.
 *
 * Flow:
 *  - Tab between TEXT and VOICE
 *  - On send → POST /public/links/{slug}/feedback (text) or
 *              POST /public/links/{slug}/feedback/voice (multipart)
 *  - Server stores it as `pending / admin_only`. The talent NEVER sees it
 *    until an admin approves it (relay rule).
 *
 * Props:
 *   slug          — link slug
 *   token         — viewer JWT
 *   talent        — { id }
 *   submission    — { id, project_id }
 *   onSent()      — optional callback after a successful send
 */
export default function FeedbackComposer({
    slug,
    token,
    talent,
    submission,
    onSent,
}) {
    const [tab, setTab] = useState("text");
    const [text, setText] = useState("");
    const [sending, setSending] = useState(false);

    const projectId = submission?.project_id;
    const submissionId = submission?.id;
    const talentId = talent?.id;
    const ready = Boolean(projectId && submissionId && talentId);

    const sendText = async () => {
        const t = text.trim();
        if (!t) {
            toast.error("Please write your feedback first");
            return;
        }
        if (!ready) {
            toast.error(
                "Feedback isn't available for this talent yet. Reach out to the team.",
            );
            return;
        }
        setSending(true);
        try {
            await axios.post(
                `${API}/public/links/${slug}/feedback`,
                {
                    talent_id: talentId,
                    submission_id: submissionId,
                    project_id: projectId,
                    text: t,
                },
                { headers: { Authorization: `Bearer ${token}` } },
            );
            setText("");
            toast.success("Sent for review — your team will moderate before sharing.");
            if (onSent) onSent();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to send");
        } finally {
            setSending(false);
        }
    };

    const sendVoice = async (blob) => {
        if (!ready) {
            toast.error("Feedback isn't available for this talent yet.");
            return;
        }
        setSending(true);
        try {
            const form = new FormData();
            form.append("talent_id", talentId);
            form.append("submission_id", submissionId);
            form.append("project_id", projectId);
            form.append("file", blob, "feedback.webm");
            await axios.post(
                `${API}/public/links/${slug}/feedback/voice`,
                form,
                {
                    headers: {
                        Authorization: `Bearer ${token}`,
                    },
                },
            );
            toast.success("Voice note sent for moderation.");
            if (onSent) onSent();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to upload voice note");
        } finally {
            setSending(false);
        }
    };

    return (
        <div
            className="border-t border-white/10 pt-6 mt-6"
            data-testid="feedback-composer"
        >
            <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
                <p className="eyebrow">Send Private Feedback</p>
                <p className="text-[10px] text-white/40 inline-flex items-center gap-1.5 tg-mono">
                    <ShieldCheck className="w-3 h-3" />
                    Moderated · talent only sees what your team approves
                </p>
            </div>

            <div className="flex gap-1 mb-4">
                <button
                    type="button"
                    onClick={() => setTab("text")}
                    data-testid="fb-tab-text"
                    className={`inline-flex items-center gap-1.5 px-3 py-2 text-[11px] tracking-widest uppercase rounded-sm border transition-all ${
                        tab === "text"
                            ? "bg-white text-black border-white"
                            : "border-white/15 text-white/60 hover:border-white/40 hover:text-white"
                    }`}
                >
                    <MessageCircle className="w-3.5 h-3.5" /> Text
                </button>
                <button
                    type="button"
                    onClick={() => setTab("voice")}
                    data-testid="fb-tab-voice"
                    className={`inline-flex items-center gap-1.5 px-3 py-2 text-[11px] tracking-widest uppercase rounded-sm border transition-all ${
                        tab === "voice"
                            ? "bg-white text-black border-white"
                            : "border-white/15 text-white/60 hover:border-white/40 hover:text-white"
                    }`}
                >
                    <Mic className="w-3.5 h-3.5" /> Voice
                </button>
            </div>

            {tab === "text" ? (
                <div className="space-y-3">
                    <textarea
                        value={text}
                        onChange={(e) => setText(e.target.value)}
                        rows={3}
                        maxLength={4000}
                        placeholder="Write a short note for your team to review…"
                        data-testid="fb-text-input"
                        className="w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none resize-none"
                    />
                    <div className="flex items-center justify-between gap-3">
                        <span className="text-[10px] text-white/30 tg-mono">
                            {text.length} / 4000
                        </span>
                        <button
                            type="button"
                            onClick={sendText}
                            disabled={sending || !text.trim()}
                            data-testid="fb-send-text-btn"
                            className="inline-flex items-center gap-2 px-4 py-2 bg-white text-black hover:opacity-90 rounded-sm text-xs disabled:opacity-40"
                        >
                            {sending ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                                <Send className="w-3.5 h-3.5" />
                            )}
                            Send for review
                        </button>
                    </div>
                </div>
            ) : (
                <VoiceRecorder onSend={sendVoice} sending={sending} />
            )}
        </div>
    );
}
