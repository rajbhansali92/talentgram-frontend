// The transcript the speech hook delivers, for the result streams real engines send. These are deterministic simulations of event shapes —
// they prove the reconciliation logic, NOT that a particular phone or laptop produces exactly these events (that needs a real device).
import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useSpeechInput, reconcileTranscript, looksRepeated } from "@/hooks/useSpeechInput";

let insts;
class FakeRec {
    constructor() { insts.push(this); this.calls = []; }
    start() { this.calls.push("start"); setTimeout(() => this.onstart?.(), 0); }
    stop() { this.calls.push("stop"); setTimeout(() => this.onend?.(), 0); }
    abort() { this.calls.push("abort"); }
}
const entry = ([t, f]) => Object.assign([{ transcript: t }], { isFinal: !!f });
const ev = (list, resultIndex = 0) => ({ resultIndex, results: list.map(entry) });

async function session(events, { stopAfter = true } = {}) {
    insts = [];
    window.SpeechRecognition = FakeRec;
    const delivered = [];
    const { result } = renderHook(() => useSpeechInput({ onFinal: (t) => delivered.push(t) }));
    await act(async () => { await result.current.start(); await vi.advanceTimersByTimeAsync(10); });
    for (const e of events) await act(async () => { insts[insts.length - 1].onresult?.(e); await vi.advanceTimersByTimeAsync(40); });
    if (stopAfter) await act(async () => { result.current.stop(); await vi.advanceTimersByTimeAsync(3000); });
    return { delivered, result };
}

beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(navigator, "permissions", { value: { query: async () => ({ state: "granted" }) }, configurable: true });
});
afterEach(() => { vi.useRealTimers(); delete window.SpeechRecognition; });

const MUFT = ["muft", "muft Kripa", "muft Kripa", "muft Kripa Trivedi", "muft Kripa Trivedi", "muft Kripa Trivedi to", "muft Kripa Trivedi to approved in", "muft Kripa Trivedi to approved in", "muft Kripa Trivedi to approved in Amazon now"];
const MUFT_FINAL = "muft Kripa Trivedi to approved in Amazon now";

describe("the reported failures (production screenshots, 2026-10-20)", () => {
    it("cumulative entries, some repeated, all flagged final → the sentence once", async () => {
        const { delivered } = await session(MUFT.map((_, i) => ev(MUFT.slice(0, i + 1).map((t) => [t, true]), i)));
        expect(delivered).toEqual([MUFT_FINAL]);
    });
    it("the same stream with the newest entry still interim and resultIndex 0", async () => {
        const { delivered } = await session(MUFT.map((_, i) => ev(MUFT.slice(0, i + 1).map((t, k, a) => [t, k < a.length - 1]), 0)));
        expect(delivered).toEqual([MUFT_FINAL]);
    });
    it("every event is a list of [earlier finals…, one new interim]", async () => {
        const { delivered } = await session(MUFT.map((t, i) => ev([...MUFT.slice(0, i).map((x) => [x, true]), [t, false]], i)));
        expect(delivered).toEqual([MUFT_FINAL]);
    });
    it("a lone tail entry with no restating chain before it is ambiguous: kept as heard, and the user is told to check", async () => {
        const { delivered, result } = await session([
            ev([["show me Angela Kumar's profile in Bangalore", true]]),
            ev([["show me Angela Kumar's profile in Bangalore", true], ["in Bangalore", true]], 1),
        ]);
        expect(delivered).toEqual(["show me Angela Kumar's profile in Bangalore in Bangalore"]);
        expect(result.current.notice).toMatch(/may be repeated/i);
    });
    it("the screenshot's second sentence: growing restatements then a tail echo", async () => {
        const c = ["show", "show me", "show me", "show me", "show me Angela", "show me Angela Kumar's profile", "show me Angela Kumar's profile in Bangalore"];
        const events = c.map((_, i) => ev(c.slice(0, i + 1).map((t) => [t, true]), i));
        events.push(ev([...c.map((t) => [t, true]), ["in Bangalore", true]], c.length));
        const { delivered } = await session(events);
        expect(delivered).toEqual(["show me Angela Kumar's profile in Bangalore"]);
    });
});

describe("interim versus final, resultIndex and list shapes", () => {
    it("interim segmentation that changes between events is replaced, never accumulated", async () => {
        const { delivered } = await session([
            ev([["show", false]]), ev([["show", false], [" me", false]]), ev([["show me Angela", false]]),
            ev([["show me", false], [" Angela Kumar's", false]]), ev([["show me Angela Kumar's profile", true]]),
        ]);
        expect(delivered).toEqual(["show me Angela Kumar's profile"]);
    });
    it("standard engine: finals append as new segments, the interim tail is replaced by its final", async () => {
        const { delivered } = await session([
            ev([["move Angela Kumar", false]]), ev([["move Angela Kumar to approved", true]]),
            ev([["move Angela Kumar to approved", true], ["in Amazon", false]], 1),
            ev([["move Angela Kumar to approved", true], ["in Amazon Now", true]], 1),
        ]);
        expect(delivered).toEqual(["move Angela Kumar to approved in Amazon Now"]);
    });
    it("every event carries only the newest entry (list restarts): earlier finals are kept once", async () => {
        const { delivered } = await session([ev([["move Angela", true]]), ev([["to approved", true]]), ev([["in Amazon", true]])]);
        expect(delivered).toEqual(["move Angela to approved in Amazon"]);
    });
    it("a list that shrinks drops interim drafts but keeps its finals", async () => {
        const { delivered } = await session([
            ev([["call Angela", true], ["now", false], ["please", false]], 1), ev([["call Angela", true], ["now please", false]], 1), ev([["now please", true]]),
        ]);
        expect(delivered).toEqual(["call Angela now please"]);
    });
    it("the same event delivered twice changes nothing", async () => {
        const e = ev([["move Angela", true], ["to approved", false]], 1);
        const { delivered } = await session([e, e, e]);
        expect(delivered).toEqual(["move Angela to approved"]);
    });
});

describe("genuine repetition is preserved", () => {
    it("'no, no' as two finals of an engine that does not restate", async () => {
        const { delivered } = await session([ev([["no", true]]), ev([["no", true], ["no", true]], 1)]);
        expect(delivered).toEqual(["no no"]);
    });
    it("'very very good' inside one entry", async () => {
        const { delivered } = await session([ev([["very very good", true]])]);
        expect(delivered).toEqual(["very very good"]);
    });
    it("the same sentence said twice as two separate finals", async () => {
        const { delivered } = await session([ev([["move Angela to approved", true]]), ev([["move Angela to approved", true], ["move Angela to approved", true]], 1)]);
        expect(delivered).toEqual(["move Angela to approved move Angela to approved"]);
    });
    it("a restating engine still delivers a genuinely repeated word ('no' then 'no no')", async () => {
        const { delivered } = await session([ev([["no", false]]), ev([["no", false]]), ev([["no no", false]]), ev([["no no", true]])]);
        expect(delivered).toEqual(["no no"]);
    });
    it("a long sentence, a pause, then a second sentence", async () => {
        const { delivered } = await session([
            ev([["move", false]]), ev([["move Angela Kumar to approved in Amazon Now", true]]),
            ev([["move Angela Kumar to approved in Amazon Now", true], ["then show me", false]], 1),
            ev([["move Angela Kumar to approved in Amazon Now", true], ["then show me her profile", true]], 1),
        ]);
        expect(delivered).toEqual(["move Angela Kumar to approved in Amazon Now then show me her profile"]);
    });
});

describe("restarts, stale callbacks and the stop", () => {
    it("the engine ends in a pause, restarts, and replays what it already said → one copy", async () => {
        insts = []; window.SpeechRecognition = FakeRec;
        const delivered = [];
        const { result } = renderHook(() => useSpeechInput({ onFinal: (t) => delivered.push(t) }));
        await act(async () => { await result.current.start(); await vi.advanceTimersByTimeAsync(10); });
        await act(async () => { insts[0].onresult(ev([["move Angela Kumar", true]])); await vi.advanceTimersByTimeAsync(5000); });
        await act(async () => { insts[0].onend(); await vi.advanceTimersByTimeAsync(400); });
        expect(insts.length).toBe(2);
        await act(async () => { insts[1].onstart?.(); insts[1].onresult(ev([["move Angela Kumar to approved", true]])); await vi.advanceTimersByTimeAsync(40); });
        await act(async () => { result.current.stop(); await vi.advanceTimersByTimeAsync(3000); });
        expect(delivered).toEqual(["move Angela Kumar to approved"]);
    });
    it("a result from the previous recognition arriving during the next one is ignored", async () => {
        insts = []; window.SpeechRecognition = FakeRec;
        const delivered = [];
        const { result } = renderHook(() => useSpeechInput({ onFinal: (t) => delivered.push(t) }));
        await act(async () => { await result.current.start(); await vi.advanceTimersByTimeAsync(10); });
        await act(async () => { insts[0].onresult(ev([["move Angela", true]])); await vi.advanceTimersByTimeAsync(5000); });
        const stale = insts[0].onresult;
        await act(async () => { insts[0].onend(); await vi.advanceTimersByTimeAsync(400); });
        await act(async () => { insts[1].onstart?.(); insts[1].onresult(ev([["to approved", true]])); stale?.(ev([["SHOULD NOT APPEAR", true]])); insts[0].onresult?.(ev([["ALSO NOT", true]])); await vi.advanceTimersByTimeAsync(40); });
        await act(async () => { result.current.stop(); await vi.advanceTimersByTimeAsync(3000); });
        expect(delivered).toEqual(["move Angela to approved"]);
    });
    it("a result arriving after the stop was delivered cannot change or re-deliver anything", async () => {
        const { delivered } = await session([ev([["move Angela", true]])]);
        expect(delivered).toEqual(["move Angela"]);
        await act(async () => { insts[0].onresult?.(ev([["late words", true]])); await vi.advanceTimersByTimeAsync(3000); });
        expect(delivered).toEqual(["move Angela"]);
    });
    it("a result event pending at the moment of the tap is still included once, then nothing more", async () => {
        insts = []; window.SpeechRecognition = FakeRec;
        const delivered = [];
        const { result } = renderHook(() => useSpeechInput({ onFinal: (t) => delivered.push(t) }));
        await act(async () => { await result.current.start(); await vi.advanceTimersByTimeAsync(10); });
        await act(async () => { insts[0].onresult(ev([["move Angela", false]])); });
        await act(async () => { result.current.stop(); insts[0].onresult(ev([["move Angela Kumar", true]])); await vi.advanceTimersByTimeAsync(3000); });
        expect(delivered).toEqual(["move Angela Kumar"]);
    });
    it("nothing meaningful heard → nothing delivered", async () => {
        const { delivered } = await session([ev([["", false]])]);
        expect(delivered).toEqual([]);
    });
});

describe("reconcileTranscript / looksRepeated", () => {
    it("is case and punctuation insensitive when matching a restated draft", () => {
        expect(reconcileTranscript([{ text: "Move Angela,", final: true, rid: 1 }, { text: "move angela kumar", final: true, rid: 1 }]).text).toBe("move angela kumar");
    });
    it("does not treat a different segment as a draft", () => {
        expect(reconcileTranscript([{ text: "call Angela", final: true, rid: 1 }, { text: "call Kumar", final: true, rid: 1 }]).text).toBe("call Angela call Kumar");
    });
    it("a single-word echo that is final is kept (could be a real stutter)", () => {
        expect(reconcileTranscript([{ text: "move to approved", final: true, rid: 1 }, { text: "approved", final: true, rid: 1 }]).text).toBe("move to approved approved");
    });
    it("flags a transcript that still repeats a run of words 3+ times, but not a plain 'no no no'", () => {
        expect(looksRepeated("Kripa Trivedi move Kripa Trivedi now Kripa Trivedi")).toBe(true);
        expect(looksRepeated("no no no")).toBe(false);
        expect(looksRepeated("move Angela to approved in Amazon Now")).toBe(false);
    });
});

// ---- seeded fuzz: random utterances × engine behaviours → the transcript must equal what was said --------------------------------------
function rng(seed) { let a = seed; return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
const VOCAB = ["move", "Angela", "Kumar", "to", "approved", "in", "Amazon", "Now", "show", "me", "profile", "call", "Priya", "Singh", "tomorrow", "embassy", "one", "send", "casting"];
function utterance(r) {
    const n = 3 + Math.floor(r() * 7); const w = [];
    while (w.length < n) { const x = VOCAB[Math.floor(r() * VOCAB.length)]; if (!w.includes(x)) w.push(x); }    // distinct words: no accidental "restatement" of the speaker's own words (genuine repeats have their own tests)
    return w;
}
// each behaviour turns the spoken words into the events an engine might send
const BEHAVIOURS = {
    spec(w, r) { const ev_ = []; let i = 0; const finals = []; while (i < w.length) { const take = 1 + Math.floor(r() * 3); const seg = w.slice(i, i + take).join(" "); ev_.push(ev([...finals.map((t) => [t, true]), [seg, false]], finals.length)); finals.push(seg); ev_.push(ev(finals.map((t) => [t, true]), finals.length - 1)); i += take; } return ev_; },
    cumulativeAllFinal(w, r) { const out = []; const entries = []; for (let i = 1; i <= w.length; i += 1) { const t = w.slice(0, i).join(" "); entries.push(t); if (r() < 0.4) entries.push(t); out.push(ev(entries.map((x) => [x, true]), entries.length - 1)); } return out; },
    cumulativeInterimInPlace(w, r) { const out = []; for (let i = 1; i <= w.length; i += 1) { const t = w.slice(0, i).join(" "); out.push(ev([[t, i === w.length]])); if (r() < 0.4) out.push(ev([[t, i === w.length]])); } return out; },
    cumulativeWithTailEcho(w) { const out = []; const entries = []; for (let i = 1; i <= w.length; i += 1) { entries.push(w.slice(0, i).join(" ")); out.push(ev(entries.map((x) => [x, true]), entries.length - 1)); } if (w.length >= 4) out.push(ev([...entries.map((x) => [x, true]), [w.slice(-2).join(" "), false]], entries.length)); return out; },
    reSegmentingInterim(w, r) { const out = []; for (let i = 1; i <= w.length; i += 1) { const cut = Math.floor(r() * i); const parts = cut > 0 ? [w.slice(0, cut).join(" "), w.slice(cut, i).join(" ")] : [w.slice(0, i).join(" ")]; out.push(ev(parts.filter(Boolean).map((t) => [t, i === w.length]), 0)); } return out; },
    newestEntryOnly(w, r) { const out = []; let i = 0; while (i < w.length) { const take = 1 + Math.floor(r() * 3); out.push(ev([[w.slice(i, i + take).join(" "), true]])); i += take; } return out; },
};
describe("fuzz: whatever shape the engine uses, each spoken word is delivered once", () => {
    for (const [name, make] of Object.entries(BEHAVIOURS)) {
        it(`${name} (60 random utterances)`, async () => {
            for (let seed = 1; seed <= 60; seed += 1) {
                const r = rng(seed * 7919 + name.length);
                const said = utterance(r);
                const { delivered } = await session(make(said, r));
                expect(delivered, `seed ${seed}: ${said.join(" ")}`).toEqual([said.join(" ")]);
            }
        }, 60000);
    }
});
