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
 * Root cause of the 2026-10 "microphone is blocked" report (found 2026-10-20): the site was served with
 * `Permissions-Policy: microphone=()`, which turns the microphone OFF FOR THE DOCUMENT ITSELF. The browser then answers
 * "denied" to the Permissions API, getUserMedia and SpeechRecognition no matter what the user allows in Chrome's site
 * settings. The header is fixed (next.config.js); this hook now also CHECKS the document's policy first, so that situation
 * is reported as what it is instead of as the user's setting.
 *
 * Lifecycle:  idle → starting (mic requested) → listening (browser says audio is flowing) → processing (stop asked, waiting
 * for the engine's last words) → idle.  Failure is "idle + error" — always recoverable with another tap.
 *   • "starting" is bounded: no `start` event within START_TIMEOUT_MS → abort, say so, back to idle (iOS Safari can sit on the
 *     permission sheet or never fire anything at all)
 *   • listening is bounded (MAX_LISTEN_MS), and a stop that never produces an `end` event is forced after STOP_GRACE_MS —
 *     the words heard so far (final, else the last interim) are still delivered, exactly once
 *   • every error code has its own message; "not-allowed" is classified with the document's permissions policy AND the
 *     browser's permission state for THIS site, never assumed to be the user's setting
 *   • closing the panel, minimising it or unmounting aborts the session; events from an old session are ignored
 * Nothing here logs audio or transcripts; `diagnostics` carries only codes/states.
 */
export const START_TIMEOUT_MS = 8000;
export const MAX_LISTEN_MS = 30000;
export const STOP_GRACE_MS = 2500;

const siteName = () => (typeof window !== "undefined" && window.location?.host) || "this site";
const ua = () => (typeof navigator !== "undefined" ? navigator.userAgent || "" : "");

export const isIOS = () => /iPad|iPhone|iPod/.test(ua()) || (typeof navigator !== "undefined" && navigator.platform === "MacIntel" && (navigator.maxTouchPoints || 0) > 1);
export const isStandaloneApp = () => {
    try { return (typeof navigator !== "undefined" && navigator.standalone === true) || (typeof window !== "undefined" && !!window.matchMedia?.("(display-mode: standalone)")?.matches); } catch { return false; }
};
const touchDevice = () => typeof navigator !== "undefined" && (navigator.maxTouchPoints || 0) > 0;

export function getRecognitionCtor() {
    if (typeof window === "undefined") return null;
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

/** Does the page's own Permissions-Policy allow the microphone? true | false | null (browser can't say — Safari/Firefox). */
export function microphonePolicyAllowed() {
    try {
        const pp = typeof document !== "undefined" ? (document.permissionsPolicy || document.featurePolicy) : null;
        return pp?.allowsFeature ? !!pp.allowsFeature("microphone") : null;
    } catch {
        return null;
    }
}

/** The browser's permission state for THIS origin: granted | denied | prompt | unknown (no microphone query support). */
export async function micPermission() {
    try {
        if (typeof navigator === "undefined" || !navigator.permissions?.query) return "unknown";
        const s = await navigator.permissions.query({ name: "microphone" });
        return s?.state || "unknown";
    } catch {
        return "unknown";                                   // Safari / Firefox: the query for "microphone" isn't supported
    }
}

// ---------------------------------------------------------------------------------------------------- messages
const IOS_SPEECH_HELP = "On iPhone/iPad: allow the microphone for Safari (Settings › Safari › Microphone), turn on Dictation (Settings › General › Keyboard › Enable Dictation), or tap the microphone key on your keyboard.";

export const MESSAGES = {
    policy: () => "Voice input is switched off by this site's own security settings, not by your browser — that's a setup problem on our side and it needs fixing. You can type instead.",
    denied: () => `The microphone is blocked for ${siteName()}. In the browser's site settings (lock icon in the address bar) allow the microphone for exactly ${siteName()}, then tap the mic again — or type instead.`,
    // The page may use the microphone and the browser says the site IS allowed, yet recognition refused: not a "grant permission" problem.
    refused: () => `The microphone is allowed for ${siteName()}, but the browser's speech recognition was still refused. Check your device's microphone/speech privacy setting for this browser, that nothing else is using the microphone, and the network — or type instead.`,
    promptDismissed: () => `The browser didn't get microphone permission for ${siteName()} (the prompt was dismissed or blocked). Tap the mic and choose Allow — or type instead.`,
    unknownDenied: () => `The browser refused microphone access for ${siteName()}. Allow it for this exact site in the browser's site settings, or type instead.`,
    serviceRefused: () => (isIOS() ? `The speech service refused to start. ${IOS_SPEECH_HELP}` : "The browser's speech service refused to start on this device. You can type instead."),
    noSpeech: () => "I didn't hear anything. Tap the mic and try again, or type.",
    noDevice: () => "No working microphone was found (or another app is using it). Check it is connected and free, or type instead.",
    network: () => "Speech recognition needs a network connection right now (the browser's speech service is online). You can type instead.",
    language: () => "This browser doesn't support that language for voice. You can type instead.",
    startTimeout: () => (isIOS() && isStandaloneApp()
        ? "Voice input doesn't start inside a Home Screen app. Open this page in Safari, or tap the microphone key on your keyboard."
        : isIOS() ? `The microphone didn't start. ${IOS_SPEECH_HELP}`
            : "The microphone didn't start. Answer the browser's permission prompt if one is showing, then tap the mic again — or type instead."),
    unknown: () => "Voice input stopped unexpectedly. Tap the mic to try again, or type.",
    unsupported: () => (touchDevice() ? "Voice input isn't available in this browser. Type your command, or tap the microphone key on your keyboard to dictate." : "Voice input isn't available in this browser. You can type instead."),
    insecure: () => "Voice input needs a secure (https) connection. You can type instead.",
    startFailed: () => "Voice input couldn't start on this device. You can type instead.",
    startThrew: () => "Voice input couldn't start. Wait a moment and tap the mic again, or type.",
};

/** Which message explains a refused/denied microphone — decided from FACTS about this page, not assumed. */
export function refusalMessage({ permission, policy }) {
    if (policy === false) return MESSAGES.policy();
    if (permission === "denied") return MESSAGES.denied();
    if (permission === "granted") return MESSAGES.refused();
    if (permission === "prompt") return MESSAGES.promptDismissed();
    return MESSAGES.unknownDenied();
}

const ERROR_MESSAGE = {
    "service-not-allowed": () => MESSAGES.serviceRefused(),
    "no-speech": () => MESSAGES.noSpeech(),
    "audio-capture": () => MESSAGES.noDevice(),
    network: () => MESSAGES.network(),
    aborted: () => null,
    "language-not-supported": () => MESSAGES.language(),
};

export function useSpeechInput({ lang = "en-IN", onFinal } = {}) {
    const Ctor = getRecognitionCtor();
    const [phase, setPhase] = useState("idle");              // idle | starting | listening | processing
    const [interim, setInterim] = useState("");
    const [error, setError] = useState(null);
    const [diagnostics, setDiagnostics] = useState(null);    // codes only — shown next to an error so a report says WHY
    const recRef = useRef(null);
    const sessionRef = useRef(0);                            // bumps on every start/finish: late events from an old session are ignored
    const timers = useRef({});
    const onFinalRef = useRef(onFinal);
    onFinalRef.current = onFinal;

    const clearTimers = () => { Object.values(timers.current).forEach((t) => clearTimeout(t)); timers.current = {}; };
    const detach = (rec) => { if (rec) { rec.onresult = rec.onerror = rec.onend = rec.onstart = rec.onaudiostart = null; try { rec.abort?.(); } catch { /* already over */ } } };
    const note = (d) => {
        setDiagnostics(d);
        try { console.debug("[voice]", d); } catch { /* console unavailable */ }       // codes/states only — never audio or words
    };

    // The ONE place a session ends. Idempotent: events that arrive afterwards (onend after our forced finish) do nothing.
    const finish = useCallback((session, { text = "", err = null, diag = null } = {}) => {
        if (sessionRef.current !== session) return;
        sessionRef.current += 1;
        clearTimers();
        const rec = recRef.current;
        recRef.current = null;
        detach(rec);
        setPhase("idle");
        setInterim("");
        if (err) { setError(err); if (diag) note(diag); }
        const t = (text || "").trim();
        if (t) onFinalRef.current?.(t);
    }, []);

    const abort = useCallback(() => {
        sessionRef.current += 1;                              // discard everything the old session might still say
        clearTimers();
        const rec = recRef.current;
        recRef.current = null;
        detach(rec);
        setPhase("idle");
        setInterim("");
    }, []);

    const stop = useCallback(() => {
        const rec = recRef.current;
        if (!rec) return;
        const session = sessionRef.current;
        setPhase("processing");                               // the engine is finishing the last words
        try { rec.stop(); } catch { /* already stopped */ }
        // a stop that never produces `end` (seen on mobile Safari) must not leave the UI waiting: force it, keep the words
        clearTimeout(timers.current.grace);
        timers.current.grace = setTimeout(() => finish(session, { text: rec.__heard?.() || "" }), STOP_GRACE_MS);
    }, [finish]);

    const start = useCallback(async () => {
        if (recRef.current || phase !== "idle") return;
        setError(null);
        setDiagnostics(null);
        setInterim("");
        if (!Ctor) { setError(MESSAGES.unsupported()); note({ stage: "start", result: "unsupported" }); return; }
        if (typeof window !== "undefined" && window.isSecureContext === false) { setError(MESSAGES.insecure()); note({ stage: "start", result: "insecure-context" }); return; }
        const session = sessionRef.current + 1;
        sessionRef.current = session;
        setPhase("starting");
        const policy = microphonePolicyAllowed();             // is the microphone even allowed for THIS DOCUMENT?
        const perm = await micPermission();                   // and what does the browser say about THIS origin?
        if (sessionRef.current !== session) return;           // cancelled while we waited
        if (policy === false || perm === "denied") {
            sessionRef.current += 1;
            setPhase("idle");
            setError(refusalMessage({ permission: perm, policy }));
            note({ stage: "preflight", result: "refused", permission: perm, policy });
            return;
        }
        let rec;
        try {
            rec = new Ctor();
        } catch {
            sessionRef.current += 1; setPhase("idle");
            setError(MESSAGES.startFailed()); note({ stage: "construct", result: "threw" });
            return;
        }
        rec.lang = lang;
        rec.interimResults = true;
        rec.continuous = false;
        rec.maxAlternatives = 1;
        let finalText = "";
        let interimText = "";
        rec.__heard = () => (finalText || interimText).trim();
        const begun = () => { if (sessionRef.current === session) { clearTimeout(timers.current.start); setPhase((p) => (p === "processing" ? p : "listening")); } };
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
            const code = e?.error || "unknown";
            const diag = { stage: "recognition", error: code, permission: perm, policy };
            let msg;
            if (code === "not-allowed") msg = refusalMessage({ permission: perm, policy });
            else if (ERROR_MESSAGE[code]) msg = ERROR_MESSAGE[code]();
            else msg = MESSAGES.unknown();
            // Synchronous on purpose: browsers fire `end` right after `error`, and the message must already be set by then.
            // Whatever was heard before the failure is kept; the message (if any) explains why it stopped.
            finish(session, { text: code === "not-allowed" || code === "service-not-allowed" ? "" : rec.__heard(), err: msg, diag });
            if (code === "not-allowed") {                      // then refine the wording with the permission state AS IT IS NOW
                Promise.all([micPermission(), Promise.resolve(microphonePolicyAllowed())]).then(([now, pol]) => {
                    if (sessionRef.current === session + 1 && !recRef.current) { setError(refusalMessage({ permission: now, policy: pol })); note({ ...diag, permission: now, policy: pol, refined: true }); }
                });
            }
        };
        rec.onend = () => finish(session, { text: rec.__heard() });
        recRef.current = rec;
        try {
            rec.start();
        } catch {
            finish(session, { err: MESSAGES.startThrew(), diag: { stage: "start", result: "threw" } });
            return;
        }
        timers.current.start = setTimeout(() => {
            const heard = rec.__heard();
            finish(session, { text: heard, err: heard ? null : MESSAGES.startTimeout(), diag: { stage: "start", result: "timeout", permission: perm, policy, ios: isIOS(), standalone: isStandaloneApp() } });
        }, START_TIMEOUT_MS);
        timers.current.max = setTimeout(() => stop(), MAX_LISTEN_MS);
    }, [Ctor, lang, phase, finish, stop]);

    const toggle = useCallback(() => {
        if (phase === "processing") return undefined;          // already finishing — a second tap must not restart or double-stop
        const run = phase === "idle" ? start() : phase === "starting" && !recRef.current ? abort() : stop();
        return Promise.resolve(run).catch(() => { abort(); setError(MESSAGES.unknown()); });     // never an unhandled rejection
    }, [phase, start, stop, abort]);

    useEffect(() => () => { sessionRef.current += 1; clearTimers(); detach(recRef.current); recRef.current = null; }, []);

    return {
        supported: !!Ctor, phase, listening: phase !== "idle", interim, error, diagnostics,
        start, stop, abort, toggle, clearError: () => { setError(null); setDiagnostics(null); },
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
