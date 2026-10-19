import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Browser speech-to-text (Web Speech API) — no audio is recorded by us or sent to any service WE run or add.
 * Reality check, shown honestly in the UI: Chrome/Edge/Safari implement it by sending the audio to the BROWSER
 * VENDOR's own speech service (needs network, may be unavailable offline); Firefox has no support. When it
 * isn't available `supported` is false and the text box is the (fully working) fallback.
 *
 * Push-to-talk only: starts when the user taps the mic, never in the background, stops on a second tap / silence.
 * The transcript is handed back to the caller to PUT IN THE TEXT BOX — it is never executed automatically.
 *
 * Lifecycle (2026-10-19 — the old version could leave "Listening…" up for ever and called every refusal "blocked"):
 *   idle → starting (mic requested) → listening (browser says audio is flowing) → idle
 *   • "starting" is bounded: no `start` event within START_TIMEOUT_MS → abort, say so, back to idle (iOS Safari can sit
 *     on the permission sheet or never fire anything at all)
 *   • listening is bounded (MAX_LISTEN_MS), and a stop that never produces an `end` event is forced after STOP_GRACE_MS —
 *     the words heard so far (final, else the last interim) are still delivered, exactly once
 *   • "not-allowed" is classified with the Permissions API for THIS site: denied → "blocked for <site>"; granted → the browser
 *     allowed the site but refused speech (OS microphone setting / another app / speech service); unknown → says both
 *   • closing the panel, minimising it or unmounting aborts the session
 */
export const START_TIMEOUT_MS = 8000;
export const MAX_LISTEN_MS = 30000;
export const STOP_GRACE_MS = 2500;

const siteName = () => (typeof window !== "undefined" && window.location?.host) || "this site";

const ERROR_TEXT = {
    "service-not-allowed": () => "The browser's speech service refused to start on this device. You can type instead.",
    "no-speech": () => "I didn't hear anything. Tap the mic and try again, or type.",
    "audio-capture": () => "No working microphone was found (or another app is using it). Check it is plugged in and free, or type instead.",
    network: () => "Speech recognition needs a network connection right now. You can type instead.",
    aborted: () => null,
    "language-not-supported": () => "This browser doesn't support that language for voice. You can type instead.",
};

export function getRecognitionCtor() {
    if (typeof window === "undefined") return null;
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

async function micPermission() {
    try {
        if (typeof navigator === "undefined" || !navigator.permissions?.query) return "unknown";
        const s = await navigator.permissions.query({ name: "microphone" });
        return s?.state || "unknown";
    } catch {
        return "unknown";                                   // Safari / Firefox: the query for "microphone" isn't supported
    }
}

function blockedMessage(state) {
    const host = siteName();
    if (state === "denied") return `The microphone is blocked for ${host}. Browser permissions are per website — allow it for ${host} (the lock icon in the address bar → Site settings), then tap the mic again. Or type instead.`;
    if (state === "granted") return `The browser says the microphone is allowed for ${host}, but speech recognition was still refused. Check your computer's microphone privacy setting for this browser, that no other app or tab is using the microphone, and try again — or type instead.`;
    return `The browser refused microphone access for ${host}. Allow it for this exact site in the browser's site settings (permissions are per website), or type instead.`;
}

const touchDevice = () => typeof navigator !== "undefined" && (navigator.maxTouchPoints || 0) > 0;

export function useSpeechInput({ lang = "en-IN", onFinal } = {}) {
    const Ctor = getRecognitionCtor();
    const [phase, setPhase] = useState("idle");              // idle | starting | listening
    const [interim, setInterim] = useState("");
    const [error, setError] = useState(null);
    const recRef = useRef(null);
    const sessionRef = useRef(0);                            // bumps on every start/finish: late events from an old session are ignored
    const timers = useRef({});
    const onFinalRef = useRef(onFinal);
    onFinalRef.current = onFinal;

    const clearTimers = () => { Object.values(timers.current).forEach((t) => clearTimeout(t)); timers.current = {}; };

    // The ONE place a session ends. Idempotent: events that arrive afterwards (onend after our forced finish) do nothing.
    const finish = useCallback((session, { text = "", err = null } = {}) => {
        if (sessionRef.current !== session) return;
        sessionRef.current += 1;
        clearTimers();
        const rec = recRef.current;
        recRef.current = null;
        if (rec) { rec.onresult = rec.onerror = rec.onend = rec.onstart = rec.onaudiostart = null; try { rec.abort?.(); } catch { /* already over */ } }
        setPhase("idle");
        setInterim("");
        if (err) setError(err);
        const t = (text || "").trim();
        if (t) onFinalRef.current?.(t);
    }, []);

    const abort = useCallback(() => {
        sessionRef.current += 1;                              // discard everything the old session might still say
        clearTimers();
        const rec = recRef.current;
        recRef.current = null;
        if (rec) { rec.onresult = rec.onerror = rec.onend = rec.onstart = rec.onaudiostart = null; try { rec.abort?.(); } catch { /* ignore */ } }
        setPhase("idle");
        setInterim("");
    }, []);

    const stop = useCallback(() => {
        const rec = recRef.current;
        if (!rec) return;
        const session = sessionRef.current;
        try { rec.stop(); } catch { /* already stopped */ }
        // a stop that never produces `end` (seen on mobile Safari) must not leave the UI listening: force it, keep the words
        clearTimeout(timers.current.grace);
        timers.current.grace = setTimeout(() => finish(session, { text: rec.__heard?.() || "" }), STOP_GRACE_MS);
    }, [finish]);

    const start = useCallback(async () => {
        if (recRef.current || phase !== "idle") return;
        setError(null);
        setInterim("");
        if (!Ctor) { setError(touchDevice() ? "Voice input isn't available in this browser. Type your command, or tap the microphone key on your keyboard to dictate." : "Voice input isn't available in this browser. You can type instead."); return; }
        if (typeof window !== "undefined" && window.isSecureContext === false) { setError("Voice input needs a secure (https) connection. You can type instead."); return; }
        const session = sessionRef.current + 1;
        sessionRef.current = session;
        setPhase("starting");
        const perm = await micPermission();                   // never rely on the site's setting being what we think: ask the browser
        if (sessionRef.current !== session) return;           // cancelled while we waited
        if (perm === "denied") { sessionRef.current += 1; setPhase("idle"); setError(blockedMessage("denied")); return; }
        let rec;
        try {
            rec = new Ctor();
        } catch {
            sessionRef.current += 1; setPhase("idle");
            setError("Voice input couldn't start on this device. You can type instead.");
            return;
        }
        rec.lang = lang;
        rec.interimResults = true;
        rec.continuous = false;
        rec.maxAlternatives = 1;
        let finalText = "";
        let interimText = "";
        rec.__heard = () => (finalText || interimText).trim();
        const begun = () => { if (sessionRef.current === session) { clearTimeout(timers.current.start); setPhase("listening"); } };
        rec.onstart = begun;
        rec.onaudiostart = begun;
        rec.onresult = (e) => {
            if (sessionRef.current !== session) return;
            begun();
            // rebuild from the whole result list each time: some engines resend earlier results, which would otherwise duplicate words
            let f = ""; let i2 = "";
            for (let i = 0; i < e.results.length; i += 1) {
                const r = e.results[i];
                if (r.isFinal) f += r[0].transcript; else i2 += r[0].transcript;
            }
            finalText = f;
            interimText = i2;
            setInterim(i2);
        };
        rec.onerror = (e) => {
            if (sessionRef.current !== session) return;
            const code = e?.error;
            let msg;
            if (code === "not-allowed") msg = blockedMessage(perm);
            else if (ERROR_TEXT[code]) msg = ERROR_TEXT[code]();
            else msg = "Voice input stopped unexpectedly. You can type instead.";
            // Synchronous on purpose: browsers fire `end` right after `error`, and the message must already be set by then.
            // Whatever was heard before the failure is kept; the message (if any) explains why it stopped.
            finish(session, { text: code === "not-allowed" || code === "service-not-allowed" ? "" : rec.__heard(), err: msg });
            if (code === "not-allowed") {                      // then refine the wording with the permission state AS IT IS NOW
                micPermission().then((now) => { if (sessionRef.current === session + 1 && !recRef.current) setError(blockedMessage(now)); });
            }
        };
        rec.onend = () => finish(session, { text: rec.__heard() });
        recRef.current = rec;
        try {
            rec.start();
        } catch {
            finish(session, { err: "Voice input couldn't start. Wait a moment and tap the mic again, or type." });
            return;
        }
        timers.current.start = setTimeout(() => finish(session, { text: rec.__heard(), err: rec.__heard() ? null : "The microphone didn't start. Check the browser's permission prompt, then tap the mic again — or type instead." }), START_TIMEOUT_MS);
        timers.current.max = setTimeout(() => stop(), MAX_LISTEN_MS);
    }, [Ctor, lang, phase, finish, stop]);

    const toggle = useCallback(() => (phase === "idle" ? start() : phase === "starting" && !recRef.current ? abort() : stop()), [phase, start, stop, abort]);

    useEffect(() => () => { sessionRef.current += 1; clearTimers(); try { recRef.current?.abort?.(); } catch { /* ignore */ } recRef.current = null; }, []);

    return {
        supported: !!Ctor, phase, listening: phase !== "idle", interim, error,
        start, stop, abort, toggle, clearError: () => setError(null),
    };
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
