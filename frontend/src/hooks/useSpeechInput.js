import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Browser speech-to-text (Web Speech API) — no audio is recorded by us or sent to any service WE run or add.
 * Reality check, shown honestly in the UI: Chrome/Edge/Safari implement it by sending the audio to the BROWSER
 * VENDOR's own speech service (needs network, may be unavailable offline); Firefox has no support. When it
 * isn't available `supported` is false and the text box is the (fully working) fallback.
 *
 * Push-to-talk only: starts when the user taps the mic, never in the background, stops on a second tap / silence.
 * The transcript is handed back to the caller to PUT IN THE TEXT BOX — it is never executed automatically.
 */
const ERROR_TEXT = {
    "not-allowed": "Microphone access was blocked. Allow it in the browser's site settings, or type instead.",
    "service-not-allowed": "Speech recognition isn't allowed on this device or browser. You can type instead.",
    "no-speech": "I didn't hear anything. Tap the mic and try again, or type.",
    "audio-capture": "No microphone was found.",
    network: "Speech recognition needs a network connection right now. You can type instead.",
    aborted: null,
    "language-not-supported": "This browser doesn't support that language for voice. You can type instead.",
};

export function getRecognitionCtor() {
    if (typeof window === "undefined") return null;
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

export function useSpeechInput({ lang = "en-IN", onFinal } = {}) {
    const Ctor = getRecognitionCtor();
    const [listening, setListening] = useState(false);
    const [interim, setInterim] = useState("");
    const [error, setError] = useState(null);
    const recRef = useRef(null);
    const onFinalRef = useRef(onFinal);
    onFinalRef.current = onFinal;

    const stop = useCallback(() => {
        try { recRef.current?.stop(); } catch { /* already stopped */ }
    }, []);

    const start = useCallback(() => {
        if (!Ctor || recRef.current) return;
        setError(null);
        setInterim("");
        let rec;
        try {
            rec = new Ctor();
        } catch {
            setError("Voice input couldn't start on this device. You can type instead.");
            return;
        }
        rec.lang = lang;
        rec.interimResults = true;
        rec.continuous = false;
        rec.maxAlternatives = 1;
        let finalText = "";
        rec.onresult = (e) => {
            let interimText = "";
            for (let i = e.resultIndex; i < e.results.length; i += 1) {
                const r = e.results[i];
                if (r.isFinal) finalText += r[0].transcript;
                else interimText += r[0].transcript;
            }
            setInterim(interimText);
        };
        rec.onerror = (e) => {
            const msg = ERROR_TEXT[e.error];
            if (msg !== null) setError(msg || "Voice input stopped unexpectedly. You can type instead.");
        };
        rec.onend = () => {
            recRef.current = null;
            setListening(false);
            setInterim("");
            const text = finalText.trim();
            if (text) onFinalRef.current?.(text);
        };
        recRef.current = rec;
        try {
            rec.start();
            setListening(true);
        } catch {
            recRef.current = null;
            setError("Voice input couldn't start. You can type instead.");
        }
    }, [Ctor, lang]);

    const toggle = useCallback(() => (listening ? stop() : start()), [listening, start, stop]);

    useEffect(() => () => { try { recRef.current?.abort(); } catch { /* ignore */ } recRef.current = null; }, []);

    return { supported: !!Ctor, listening, interim, error, start, stop, toggle, clearError: () => setError(null) };
}

/** Optional spoken confirmations. Off unless the user turns it on; never blocks anything. */
export function speak(text) {
    try {
        if (typeof window === "undefined" || !window.speechSynthesis || !text) return;
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(String(text).slice(0, 240));
        u.rate = 1.05;
        window.speechSynthesis.speak(u);
    } catch { /* speech output is a nicety */ }
}
export const speechOutputSupported = () => typeof window !== "undefined" && !!window.speechSynthesis;
