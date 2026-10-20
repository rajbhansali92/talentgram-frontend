import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Browser speech-to-text (Web Speech API) — no audio is recorded by us or sent to any service WE run or add.
 * Reality check, shown honestly in the UI: Chrome/Edge/Safari implement it by sending the audio to the BROWSER
 * VENDOR's own speech service (needs network, may be unavailable offline); Firefox has no support. When it
 * isn't available `supported` is false and the text box is the (fully working) fallback.
 *
 * TAP TO START, TAP TO STOP (2026-10-20). A session is the USER's intention to listen, not one browser recognition:
 *   idle → starting → listening → (user taps) processing → idle
 * The engine ending by itself (silence, a pause while thinking, a browser/service limit) is NOT the end of the session: while
 * the user still wants to listen the recognition is restarted — bounded and rate-limited — and every piece of text is kept.
 * Only these end a session: the user's tap, the panel closing/minimising, a hard maximum duration, or a failure that
 * cannot be recovered (permission, service, microphone, network). In every case the words captured so far are delivered.
 *
 * Transcript accumulation (why nothing is missing or doubled): a recognition's `results` list is read as a SNAPSHOT that is replaced on every event
 * (interim entries are drafts and are never kept once superseded; only FINAL entries that vanish from a later list are archived). When a recognition
 * ends its entries are committed once. The transcript is `reconcileTranscript()` of everything held — see that function for how engines that RESTATE
 * earlier text (cumulative entries, repeated entries, echoed tails) are collapsed while genuinely repeated speech ("no no") is preserved.
 *
 * The transcript is handed to the caller to PUT IN THE TEXT BOX exactly once per session — it is never executed or sent.
 *
 * Earlier fixes kept: the site is served with Permissions-Policy microphone=(self) (a policy of microphone=() made the browser
 * report the microphone as denied whatever the user allowed); this hook checks the document's policy and the browser's
 * permission state before and after a failure and reports what actually happened. Nothing here logs audio or transcripts;
 * `diagnostics` carries only codes/states.
 */
export const START_TIMEOUT_MS = 8000;            // the FIRST start only: the browser must report audio / a result within this
export const RESTART_START_TIMEOUT_MS = 4000;    // a restart that does not begin within this ends the session (text kept)
export const MAX_SESSION_MS = 120000;            // hard safety maximum for one session (configurable: `maxMs`)
export const STOP_GRACE_MS = 2500;               // after a stop request, wait this long for the engine's last words
export const MAX_RESTARTS = 40;                  // recognitions per session after the first
export const FAST_END_MS = 800;                  // a recognition that ends sooner than this counts as a "fast end"
export const MAX_FAST_ENDS = 3;                  // …and this many in a row mean the engine is not cooperating: stop restarting
export const RESTART_DELAY_MS = 150;             // base delay before a restart (grows with consecutive fast ends)

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
    repeated: () => "Some words may be repeated in what was heard — please check and correct the text before sending.",
    unknown: () => "Voice input stopped unexpectedly. Tap the mic to try again, or type.",
    unsupported: () => (touchDevice() ? "Voice input isn't available in this browser. Type your command, or tap the microphone key on your keyboard to dictate." : "Voice input isn't available in this browser. You can type instead."),
    insecure: () => "Voice input needs a secure (https) connection. You can type instead.",
    startFailed: () => "Voice input couldn't start on this device. You can type instead.",
    startThrew: () => "Voice input couldn't start. Wait a moment and tap the mic again, or type.",
    // the engine kept ending on its own — what was heard is kept
    keepsStopping: () => "The browser's speech recognition keeps stopping on its own here. I kept everything it heard — check it, then tap the mic to continue, or type.",
    restartFailed: () => "The browser wouldn't resume listening by itself (some phones need a tap to start each time). I kept everything it heard — tap the mic to continue, or type.",
    maxReached: (secs) => `Voice input stopped at the ${Math.round(secs / 60) >= 1 ? `${Math.round(secs / 60)}-minute` : `${secs}-second`} limit. I kept everything it heard — tap the mic to continue.`,
};

/** Which message explains a refused/denied microphone — decided from FACTS about this page, not assumed. */
export function refusalMessage({ permission, policy }) {
    if (policy === false) return MESSAGES.policy();
    if (permission === "denied") return MESSAGES.denied();
    if (permission === "granted") return MESSAGES.refused();
    if (permission === "prompt") return MESSAGES.promptDismissed();
    return MESSAGES.unknownDenied();
}

// Errors that cannot be recovered by trying again: the session ends (text kept) and says why.
const FATAL_MESSAGE = {
    "service-not-allowed": () => MESSAGES.serviceRefused(),
    "audio-capture": () => MESSAGES.noDevice(),
    network: () => MESSAGES.network(),
    "language-not-supported": () => MESSAGES.language(),
};

const normalize = (s) => (s || "").replace(/\s+/g, " ").trim();
const join = (parts) => normalize(parts.filter(Boolean).join(" "));

// ---------------------------------------------------------------------------------------------------- transcript reconciliation
// WHY a phrase appeared several times ("muft muft Kripa muft Kripa Trivedi muft Kripa Trivedi to approved in …"). Engines do not all follow the
// spec's "one entry per spoken segment". Some (Android Chrome, some desktop builds with `continuous`) deliver NEW entries that RESTATE everything
// said so far — "muft", "muft Kripa", "muft Kripa" again, "muft Kripa Trivedi", … — and some echo the tail of a finished entry as a separate
// entry ("… profile in Bangalore" + "in Bangalore"). Joining the entries printed the sentence once per restatement. The first fix compared each
// entry only with the PREVIOUS one, so a single repeated entry broke the chain and everything after it stayed doubled; it also committed interim
// text whenever the list shrank, which kept stale drafts.
//
// The model now: a recognition's entries are a SNAPSHOT (the latest `results` list, replaced as a whole — an interim entry is a draft and is never
// kept once superseded). Only FINAL entries that vanish from or change in a later list are archived (a final never changes in a real engine, so a
// changed one means the list restarted). The transcript is the reconciliation of everything held, in order:
//   1. an entry whose words are exactly the FIRST words of the next different entry, which is longer, is that entry's earlier draft → dropped. Seeing this proves the
//      engine is "restating", and only then:
//   2. identical neighbours from that recognition are one entry repeated, not speech → one kept.
//   3. an entry that is the proper TAIL of the entry right before it (≥2 words, or any length when it is still interim) is an echo → dropped.
// (2 and 3 never apply to an engine that has not shown it restates: there a repeated entry or tail is what the person said.)
// Everything else is kept as spoken: "no no", "very very good", the same sentence said twice (identical FINAL entries of an engine that does not
// restate), and a real second segment. Known, accepted limit: a genuine "yes" followed by a separate "yes please" reads as one restated entry.
const wordsOf = (s) => normalize(s).toLowerCase().replace(/[.,!?;:"“”]/g, "").split(" ").filter(Boolean);
const isPrefix = (a, b) => a.length > 0 && b.length > a.length && a.every((w, i) => w === b[i]);
const isTail = (a, b) => a.length > 0 && b.length > a.length && a.every((w, i) => w === b[b.length - a.length + i]);
const sameWords = (a, b) => a.length === b.length && a.every((w, i) => w === b[i]);

/** parts: [{text, final, rid}] in the order they were heard → { text, dropped, restating, suspect } */
export function reconcileTranscript(parts) {
    const list = (parts || []).map((p) => ({ ...p, text: normalize(p.text), w: wordsOf(p.text) })).filter((p) => p.w.length);
    const keep = list.map(() => true);
    const restatedRecs = new Set();
    let suspect = false;
    list.forEach((p, i) => {                                                    // 1. an earlier draft: the NEXT different entry restates it, longer
        const nxt = list.slice(i + 1).find((q) => !sameWords(q.w, p.w));        // (a repeated identical entry in between does not break the chain)
        if (nxt && isPrefix(p.w, nxt.w)) { keep[i] = false; restatedRecs.add(p.rid); }
    });
    const prevKept = (i) => { for (let k = i - 1; k >= 0; k -= 1) if (keep[k]) return k; return -1; };
    list.forEach((p, i) => {
        if (!keep[i]) return;
        const k = prevKept(i);
        if (k < 0) return;
        const q = list[k];
        if (sameWords(q.w, p.w) && p.rid === q.rid && restatedRecs.has(p.rid)) keep[k] = false;      // 2. the same entry repeated
        else if (p.rid === q.rid && isTail(p.w, q.w) && (p.w.length >= 2 || !p.final)) {
            if (restatedRecs.has(p.rid)) keep[i] = false;
            else suspect = true;                                                    // could be an echo or the person repeating themselves: keep, but warn
        }                                              // 3. an echoed tail
    });
    const kept = list.filter((_, i) => keep[i]);
    return { text: join(kept.map((p) => p.text)), dropped: list.length - kept.length, restating: restatedRecs.size > 0, suspect };
}

/** A transcript that still repeats a run of words three or more times is not trustworthy: say so (the text is kept either way). */
export function looksRepeated(text) {
    const w = wordsOf(text);
    for (let n = 2; n <= 6; n += 1) {
        const seen = new Map();
        for (let i = 0; i + n <= w.length; i += 1) {
            const key = w.slice(i, i + n).join(" ");
            const c = (seen.get(key) || 0) + 1;
            if (c >= 3) return true;
            seen.set(key, c);
        }
    }
    return false;
}

export function useSpeechInput({ lang = "en-IN", onFinal, maxMs = MAX_SESSION_MS } = {}) {
    const Ctor = getRecognitionCtor();
    const [phase, setPhase] = useState("idle");              // idle | starting | listening | processing
    const [interim, setInterim] = useState("");              // the words still being decided (display only)
    const [liveText, setLiveText] = useState("");            // everything captured so far, as it will be delivered (display only)
    const [error, setError] = useState(null);
    const [notice, setNotice] = useState(null);              // a non-error explanation (e.g. the maximum was reached)
    const [diagnostics, setDiagnostics] = useState(null);    // codes only — shown next to an error so a report says WHY
    const sessRef = useRef(null);                            // the ONE active session (null = none); late events find a different/none and are ignored
    const seqRef = useRef(0);
    const onFinalRef = useRef(onFinal);
    onFinalRef.current = onFinal;
    const maxMsRef = useRef(maxMs);
    maxMsRef.current = maxMs;

    const note = (d) => {
        setDiagnostics(d);
        try { console.debug("[voice]", d); } catch { /* console unavailable */ }       // codes/states only — never audio or words
    };
    const partsOf = (s) => [...s.committed, ...(s.rec ? [...s.rec.archive, ...s.rec.snap] : [])];
    const textOf = (s) => reconcileTranscript(partsOf(s)).text;
    const clearTimers = (s) => { Object.values(s.timers).forEach((t) => clearTimeout(t)); s.timers = {}; };
    const detach = (rec) => { if (rec) { rec.onresult = rec.onerror = rec.onend = rec.onstart = rec.onaudiostart = null; try { rec.abort?.(); } catch { /* already over */ } } };

    // The ONE place a session ends. Idempotent. Delivers the transcript (if any) exactly once, then returns to idle.
    const finalize = useCallback((s, { err = null, info = null, diag = null, deliver = true, silentEmpty = false } = {}) => {
        if (!s || s.finished) return;
        s.finished = true;
        clearTimers(s);
        const got = reconcileTranscript(partsOf(s));         // BEFORE the live recognition is detached: its entries are part of the transcript
        const text = got.text;
        if (s.rec) { s.rec.__done = true; detach(s.rec); s.rec = null; }
        if (sessRef.current === s) sessRef.current = null;
        setPhase("idle");
        setInterim("");
        setLiveText("");
        if (err) { setError(err); if (diag) note(diag); }
        // Shape of what the engine sent — counts only, never words — so a report can say whether it restated; a transcript that STILL repeats a run
        // of words is kept as heard, with a plain notice instead of a silent guess.
        if (text && (got.dropped || s.maxList > 1)) { if (!err) note({ stage: "transcript", events: s.events, maxEntries: s.maxList, collapsed: got.dropped, restating: got.restating }); }
        if (info) setNotice(info);
        else if (deliver && text && (got.suspect || looksRepeated(text))) setNotice(MESSAGES.repeated());
        if (deliver && text) onFinalRef.current?.(text);
        else if (deliver && !text && !err && !info && !silentEmpty) setError(MESSAGES.noSpeech());   // nothing recognised: say so, never silent
    }, []);

    // Recognition number `n` of the session. A fresh object each time: no handlers or state leak between recognitions.
    const launch = useCallback((s, isRestart) => {
        let rec;
        try { rec = new Ctor(); } catch { finalize(s, { err: MESSAGES.startFailed(), diag: { stage: "construct", result: "threw" } }); return; }
        rec.lang = lang;
        rec.interimResults = true;
        rec.continuous = true;                                // keep listening through pauses where the engine supports it
        rec.maxAlternatives = 1;
        s.rec = rec;
        s.lastBeganAt = 0;                                    // "how long did THIS recognition run" must not inherit the previous one's start
        s.recSeq += 1;
        rec.__seq = s.recSeq;
        rec.__done = false;
        const live = () => {
            const last = rec.snap[rec.snap.length - 1];
            setInterim(last && !last.final ? last.text : "");
            setLiveText(textOf(s));
        };
        const begun = () => {
            if (s.finished || s.rec !== rec) return;
            clearTimeout(s.timers.start);
            s.lastBeganAt = Date.now();
            s.everBegun = true;
            setPhase((p) => (p === "processing" ? p : "listening"));
        };
        rec.onstart = begun;
        rec.onaudiostart = begun;
        rec.snap = [];                                        // the latest `results` list: [{text, final, rid}] — replaced whole on every event
        rec.archive = [];                                     // FINAL entries that vanished from / changed in a later list (the list restarted)
        rec.onresult = (e) => {
            if (s.finished || s.rec !== rec || rec.__done) return;
            begun();
            const list = e.results || [];
            const next = [];
            for (let i = 0; i < list.length; i += 1) next.push({ text: normalize(list[i]?.[0]?.transcript), final: !!list[i]?.isFinal, rid: rec.__seq });
            // A final entry never changes in a real engine; if one is gone or different the list restarted: keep that final, drop interim drafts.
            rec.snap.forEach((p, i) => { if (p.final && p.text && (!next[i] || next[i].text !== p.text)) rec.archive.push(p); });
            rec.snap = next;
            s.events += 1; s.maxList = Math.max(s.maxList, list.length);
            live();
        };
        rec.onerror = (e) => {
            if (s.finished || s.rec !== rec || rec.__done) return;
            const code = e?.error || "unknown";
            s.lastError = code;
            if (code === "no-speech") { s.sawNoSpeech = true; return; }          // "nothing heard for a while" — not a failure while the user is listening
            if (code === "aborted") return;                                       // an end follows; handled there (restart or finish)
            const diag = { stage: "recognition", error: code, permission: s.perm, policy: s.policy };
            let msg;
            if (code === "not-allowed") msg = refusalMessage({ permission: s.perm, policy: s.policy });
            else if (FATAL_MESSAGE[code]) msg = FATAL_MESSAGE[code]();
            else msg = MESSAGES.unknown();
            // Synchronous on purpose: browsers fire `end` right after `error`, and the message must already be set by then.
            // Whatever was heard before the failure is kept and delivered; the message explains why it stopped.
            finalize(s, { err: msg, diag });
            if (code === "not-allowed") {                      // then refine the wording with the permission state AS IT IS NOW
                Promise.all([micPermission(), Promise.resolve(microphonePolicyAllowed())]).then(([now, pol]) => {
                    if (!sessRef.current && s.finished) { setError(refusalMessage({ permission: now, policy: pol })); note({ ...diag, permission: now, policy: pol, refined: true }); }
                });
            }
        };
        rec.onend = () => {
            if (s.finished || s.rec !== rec || rec.__done) return;
            rec.__done = true;                                // a repeated `end` for this recognition can never restart anything twice
            const lasted = s.lastBeganAt ? Date.now() - s.lastBeganAt : 0;
            // commit this recognition's words once; the next recognition starts from empty. If the next recognition replays the end of what
            // we already hold (an engine restating its previous result), the reconciliation keeps ONE copy.
            s.committed = [...s.committed, ...rec.archive, ...rec.snap];
            s.rec = null;
            setInterim(""); setLiveText(textOf(s));
            if (s.stopping) { finalize(s, { info: s.stopInfo }); return; }          // the user (or the maximum) asked to stop: this end IS the stop
            // The engine ended by itself. The user still wants to listen → restart, bounded and rate-limited.
            s.fastEnds = lasted < FAST_END_MS ? s.fastEnds + 1 : 0;
            if (s.fastEnds > MAX_FAST_ENDS) { finalize(s, { err: textOf(s) ? null : MESSAGES.keepsStopping(), info: textOf(s) ? MESSAGES.keepsStopping() : null, diag: { stage: "restart", result: "fast-ends", count: s.fastEnds } }); return; }
            if (s.restarts >= MAX_RESTARTS) { finalize(s, { info: MESSAGES.keepsStopping(), diag: { stage: "restart", result: "limit", restarts: s.restarts } }); return; }
            const delay = RESTART_DELAY_MS * (s.fastEnds + 1);
            clearTimeout(s.timers.restart);
            s.timers.restart = setTimeout(() => {
                if (s.finished || s.stopping || sessRef.current !== s) return;     // a stop/close during the delay cancels the restart
                s.restarts += 1;
                launch(s, true);
            }, delay);
        };
        try {
            rec.start();
        } catch {
            finalize(s, isRestart ? { info: MESSAGES.restartFailed(), diag: { stage: "restart", result: "threw" } } : { err: MESSAGES.startThrew(), diag: { stage: "start", result: "threw" } });
            return;
        }
        clearTimeout(s.timers.start);
        // A genuine failure to START is reported — but only until the browser first reports audio (or a result). Once a session has
        // begun, silence or thinking time never ends it.
        s.timers.start = setTimeout(() => {
            if (s.finished || s.rec !== rec || rec.__done) return;
            if (s.everBegun && !isRestart) return;
            finalize(s, isRestart ? { info: MESSAGES.restartFailed(), diag: { stage: "restart", result: "timeout" } }
                : { err: textOf(s) ? null : MESSAGES.startTimeout(), info: textOf(s) ? MESSAGES.restartFailed() : null, diag: { stage: "start", result: "timeout", permission: s.perm, policy: s.policy, ios: isIOS(), standalone: isStandaloneApp() } });
        }, isRestart ? RESTART_START_TIMEOUT_MS : START_TIMEOUT_MS);
    }, [Ctor, lang, finalize]);                              // eslint-disable-line react-hooks/exhaustive-deps

    // The user's intention to stop (a tap, the maximum, the panel closing with `keep`): finalize through the engine's last words.
    const requestStop = useCallback((s, { info = null, force = false } = {}) => {
        if (!s || s.finished) return;
        if (force || !s.rec) { finalize(s, { info }); return; }               // second tap while finishing / between restarts: deliver what we have NOW
        if (s.stopping) return;
        s.stopping = true;
        s.stopInfo = info;
        clearTimeout(s.timers.restart);
        setPhase("processing");
        const rec = s.rec;
        try { rec.stop(); } catch { /* already stopped */ }
        // a stop that never produces `end` (seen on mobile Safari) must not leave the UI waiting: force it, keep the words
        clearTimeout(s.timers.grace);
        s.timers.grace = setTimeout(() => {
            if (s.finished) return;
            finalize(s, { info: s.stopInfo });
        }, STOP_GRACE_MS);
    }, [finalize]);

    const start = useCallback(async () => {
        if (sessRef.current) return;
        setError(null); setNotice(null); setDiagnostics(null); setInterim(""); setLiveText("");
        if (!Ctor) { setError(MESSAGES.unsupported()); note({ stage: "start", result: "unsupported" }); return; }
        if (typeof window !== "undefined" && window.isSecureContext === false) { setError(MESSAGES.insecure()); note({ stage: "start", result: "insecure-context" }); return; }
        seqRef.current += 1;
        const s = { id: seqRef.current, finished: false, stopping: false, stopInfo: null, committed: [], rec: null, recSeq: 0, events: 0, maxList: 0,
            restarts: 0, fastEnds: 0, everBegun: false, lastBeganAt: 0, lastError: null, sawNoSpeech: false, timers: {}, perm: "unknown", policy: null };
        sessRef.current = s;                                  // a fresh object per session: nothing from a previous one can leak in
        setPhase("starting");
        s.policy = microphonePolicyAllowed();                // is the microphone even allowed for THIS DOCUMENT?
        s.perm = await micPermission();                       // and what does the browser say about THIS origin?
        if (s.finished || sessRef.current !== s) return;      // cancelled while we waited
        if (s.policy === false || s.perm === "denied") {
            finalize(s, { err: refusalMessage({ permission: s.perm, policy: s.policy }), diag: { stage: "preflight", result: "refused", permission: s.perm, policy: s.policy }, deliver: false });
            return;
        }
        // the hard maximum for the whole session — silence is never the stop mechanism
        s.timers.max = setTimeout(() => { if (sessRef.current === s) requestStop(s, { info: MESSAGES.maxReached(Math.round(maxMsRef.current / 1000)) }); }, maxMsRef.current);
        launch(s, false);
    }, [Ctor, launch, finalize, requestStop]);

    const stop = useCallback(() => {
        const s = sessRef.current;
        if (!s) return;
        if (!s.rec && !s.everBegun && s.recSeq === 0) { finalize(s, { deliver: false }); return; }   // cancelled during the permission check: nothing started
        requestStop(s, { force: s.stopping });                // a second tap while "finishing" delivers what is there right now
    }, [finalize, requestStop]);

    /** Close/minimise: end the session now but KEEP what was heard (it lands in the text box). `discard` drops it (unmount). */
    const abort = useCallback(({ discard = false } = {}) => {
        const s = sessRef.current;
        if (!s) return;
        finalize(s, { deliver: !discard, silentEmpty: true });
    }, [finalize]);

    const toggle = useCallback(() => {
        const run = !sessRef.current ? start() : stop();
        return Promise.resolve(run).catch(() => { abort({ discard: true }); setError(MESSAGES.unknown()); });     // never an unhandled rejection
    }, [start, stop, abort]);

    useEffect(() => () => { const s = sessRef.current; if (s) { s.finished = true; clearTimers(s); if (s.rec) { s.rec.__done = true; detach(s.rec); } sessRef.current = null; } }, []);

    return {
        supported: !!Ctor, phase, listening: phase !== "idle", interim, liveText, error, notice, diagnostics,
        start, stop, abort, toggle, clearError: () => { setError(null); setNotice(null); setDiagnostics(null); },
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
