import React from "react";
import { Mic, MessageSquare } from "lucide-react";

/**
 * Extracted verbatim from SubmissionPage.jsx's FeedbackRow (Phase 3 item 2 —
 * Project Detail Submission Summary). SubmissionPage.jsx itself is
 * unmodified (explicitly out of scope for this task) and keeps its own
 * copy; this is the same component made available to a second consumer
 * (ProjectDetail.jsx) instead of being re-implemented there.
 */
function timeAgo(iso) {
    if (!iso) return "";
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return "";
    const diff = (Date.now() - ts) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

export default function FeedbackRow({ fb }) {
    const isVoice = fb.type === "voice";
    return (
        <div
            className="bg-white/60 border border-[#eaeaea] rounded-2xl p-5 shadow-[0_1px_2px_rgba(0,0,0,0.02)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.06)]"
            data-testid={`talent-feedback-${fb.id}`}
        >
            <div className="flex items-center justify-between gap-3 mb-3">
                <span className="inline-flex items-center gap-1.5 text-[10px] tracking-[0.2em] uppercase font-mono text-[#333333]">
                    {isVoice ? (
                        <Mic className="w-3 h-3" />
                    ) : (
                        <MessageSquare className="w-3 h-3" />
                    )}
                    {isVoice ? "Voice" : "Text"}
                </span>
                <span className="text-[10px] font-mono text-[#333333]">
                    Received {timeAgo(fb.approved_at || fb.created_at)}
                </span>
            </div>
            {isVoice ? (
                <audio
                    src={fb.content_url}
                    controls
                    className="w-full"
                    data-testid={`talent-feedback-audio-${fb.id}`}
                />
            ) : (
                <p
                    className="text-[13px] leading-relaxed text-[#111111] whitespace-pre-wrap"
                    data-testid={`talent-feedback-text-${fb.id}`}
                >
                    {fb.text}
                </p>
            )}
        </div>
    );
}
