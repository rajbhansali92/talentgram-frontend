import React, { useEffect, useRef, useState } from "react";
import { Mic, StopCircle, Trash2, Loader2, Send } from "lucide-react";

const MAX_SEC = 60;

/**
 * Voice recorder for the moderated feedback composer.
 *  - Browser MediaRecorder API
 *  - 60 sec hard cap (auto-stop)
 *  - Preview via <audio> before send
 *  - `onSend(blob)` callback receives the recorded Blob
 *
 * No external mic-handling library — keeps the bundle small.
 */
export default function VoiceRecorder({ onSend, sending = false, disabled }) {
    const [recording, setRecording] = useState(false);
    const [seconds, setSeconds] = useState(0);
    const [blob, setBlob] = useState(null);
    const [previewUrl, setPreviewUrl] = useState(null);
    const [permError, setPermError] = useState(null);
    const recRef = useRef(null);
    const chunksRef = useRef([]);
    const timerRef = useRef(null);

    useEffect(() => {
        return () => {
            if (timerRef.current) clearInterval(timerRef.current);
            if (recRef.current && recRef.current.state !== "inactive") {
                try {
                    recRef.current.stop();
                } catch {}
            }
            if (previewUrl) URL.revokeObjectURL(previewUrl);
        };
    }, [previewUrl]);

    const start = async () => {
        if (disabled) return;
        setPermError(null);
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const mr = new MediaRecorder(stream);
            chunksRef.current = [];
            mr.ondataavailable = (e) => {
                if (e.data && e.data.size) chunksRef.current.push(e.data);
            };
            mr.onstop = () => {
                const b = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
                chunksRef.current = [];
                setBlob(b);
                setPreviewUrl(URL.createObjectURL(b));
                stream.getTracks().forEach((t) => t.stop());
            };
            recRef.current = mr;
            mr.start();
            setSeconds(0);
            setRecording(true);
            timerRef.current = setInterval(() => {
                setSeconds((s) => {
                    const next = s + 1;
                    if (next >= MAX_SEC) {
                        stop();
                    }
                    return next;
                });
            }, 1000);
        } catch (e) {
            setPermError(
                e?.name === "NotAllowedError"
                    ? "Microphone permission denied"
                    : "Could not access microphone",
            );
        }
    };

    const stop = () => {
        if (timerRef.current) {
            clearInterval(timerRef.current);
            timerRef.current = null;
        }
        if (recRef.current && recRef.current.state !== "inactive") {
            try {
                recRef.current.stop();
            } catch {}
        }
        setRecording(false);
    };

    const reset = () => {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        setBlob(null);
        setPreviewUrl(null);
        setSeconds(0);
    };

    const send = async () => {
        if (!blob || sending) return;
        await onSend(blob);
        reset();
    };

    const fmt = (s) => {
        const m = String(Math.floor(s / 60)).padStart(1, "0");
        const ss = String(s % 60).padStart(2, "0");
        return `${m}:${ss}`;
    };

    return (
        <div className="space-y-3" data-testid="voice-recorder">
            {permError && (
                <p className="text-[11px] text-[#FF3B30]">{permError}</p>
            )}
            {!blob && !recording && (
                <button
                    type="button"
                    onClick={start}
                    disabled={disabled}
                    data-testid="voice-record-start"
                    className="inline-flex items-center gap-2 px-4 py-2.5 border border-white/20 hover:border-white rounded-sm text-sm transition-all disabled:opacity-40"
                >
                    <Mic className="w-4 h-4" />
                    Record voice note
                </button>
            )}

            {recording && (
                <div className="flex items-center gap-3" data-testid="voice-recording-active">
                    <button
                        type="button"
                        onClick={stop}
                        className="w-10 h-10 rounded-full bg-[#FF3B30] hover:bg-[#FF3B30]/90 text-white flex items-center justify-center"
                    >
                        <StopCircle className="w-4 h-4" />
                    </button>
                    <div className="flex items-center gap-2">
                        <span className="w-2 h-2 rounded-full bg-[#FF3B30] animate-pulse" />
                        <span className="tg-mono text-xs text-white/70">
                            {fmt(seconds)} / {fmt(MAX_SEC)}
                        </span>
                    </div>
                </div>
            )}

            {blob && !recording && (
                <div className="space-y-3" data-testid="voice-preview">
                    <audio
                        src={previewUrl}
                        controls
                        className="w-full"
                        data-testid="voice-preview-audio"
                    />
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={send}
                            disabled={sending}
                            data-testid="voice-send-btn"
                            className="inline-flex items-center gap-2 px-4 py-2.5 bg-white text-black hover:opacity-90 rounded-sm text-sm transition-all disabled:opacity-40"
                        >
                            {sending ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                                <Send className="w-4 h-4" />
                            )}
                            Send for review
                        </button>
                        <button
                            type="button"
                            onClick={reset}
                            disabled={sending}
                            data-testid="voice-discard-btn"
                            className="inline-flex items-center gap-2 px-3 py-2.5 border border-white/15 hover:border-white/40 rounded-sm text-xs"
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                            Discard
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
