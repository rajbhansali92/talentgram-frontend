import { describe, it, expect } from "vitest";
import { DedupStore, defaultDedupKey } from "./dedupStore";

describe("defaultDedupKey", () => {
    it("is stable regardless of key order in params/data", () => {
        const a = defaultDedupKey({ method: "get", url: "/x", params: { b: 1, a: 2 } });
        const b = defaultDedupKey({ method: "get", url: "/x", params: { a: 2, b: 1 } });
        expect(a).toBe(b);
    });

    it("differs across methods and urls", () => {
        const a = defaultDedupKey({ method: "get", url: "/x" });
        const b = defaultDedupKey({ method: "post", url: "/x" });
        const c = defaultDedupKey({ method: "get", url: "/y" });
        expect(a).not.toBe(b);
        expect(a).not.toBe(c);
    });

    it("differs when the body differs (still uniquely identifies distinct requests)", () => {
        const a = defaultDedupKey({ method: "post", url: "/x", data: { otp: "111111" } });
        const b = defaultDedupKey({ method: "post", url: "/x", data: { otp: "222222" } });
        expect(a).not.toBe(b);
    });

    it("never contains PII values from the request body, params, or headers", () => {
        const sensitive = {
            email: "priya@example.com",
            phone: "9999999999",
            alternate_contact_number: "8888888888",
            whatsapp_group_name: "Talentgram Crew",
            instagram_handle: "@priya",
            otp: "482913",
            token: "sk_live_verysecrettoken",
            authorization: "Bearer sk_live_verysecrettoken",
            custom_answers: { favorite_color: "blue", availability_note: "weekends only" },
            filename: "priya-headshot-final-v2.jpg",
        };

        const key = defaultDedupKey({
            method: "post",
            url: "/portal/otp/verify",
            params: { source: "signup" },
            data: sensitive,
        });

        for (const value of [
            sensitive.email,
            sensitive.phone,
            sensitive.alternate_contact_number,
            sensitive.whatsapp_group_name,
            sensitive.instagram_handle,
            sensitive.otp,
            sensitive.token,
            sensitive.authorization,
            sensitive.custom_answers.favorite_color,
            sensitive.custom_answers.availability_note,
            sensitive.filename,
        ]) {
            expect(key).not.toContain(value);
            expect(key.toLowerCase()).not.toContain(value.toLowerCase());
        }

        // Only structural info (method + path) and opaque hex fingerprints.
        expect(key).toMatch(/^post:\/portal\/otp\/verify:[0-9a-f]{8}:[0-9a-f]{8}$/);
    });

    it("produces no raw-value leakage even for a bare string/number body", () => {
        const key = defaultDedupKey({ method: "post", url: "/x", data: "482913" });
        expect(key).not.toContain("482913");
    });
});

describe("DedupStore", () => {
    it("returns null for an unknown key", () => {
        expect(new DedupStore().get("missing")).toBeNull();
    });

    it("a second dedupe-mode start for the same key does not abort the first", () => {
        const store = new DedupStore();
        const controller1 = new AbortController();
        store.start("k", { controller: controller1, promise: Promise.resolve(1), mode: "dedupe" });
        const controller2 = new AbortController();
        store.start("k", { controller: controller2, promise: Promise.resolve(2), mode: "dedupe" });
        expect(controller1.signal.aborted).toBe(false);
    });

    it("replace mode aborts the previous entry's controller", () => {
        const store = new DedupStore();
        const controller1 = new AbortController();
        store.start("k", { controller: controller1, promise: Promise.resolve(1), mode: "dedupe" });
        const controller2 = new AbortController();
        store.start("k", { controller: controller2, promise: Promise.resolve(2), mode: "replace" });
        expect(controller1.signal.aborted).toBe(true);
    });

    it("bumps the generation on every start for the same key", () => {
        const store = new DedupStore();
        const e1 = store.start("k", { controller: new AbortController(), promise: Promise.resolve() });
        const e2 = store.start("k", { controller: new AbortController(), promise: Promise.resolve() });
        expect(e2.generation).toBe(e1.generation + 1);
    });

    it("isStale is true once a newer generation has been started", () => {
        const store = new DedupStore();
        const e1 = store.start("k", { controller: new AbortController(), promise: Promise.resolve() });
        expect(store.isStale("k", e1.generation)).toBe(false);
        store.start("k", { controller: new AbortController(), promise: Promise.resolve() });
        expect(store.isStale("k", e1.generation)).toBe(true);
    });

    it("finish only clears the entry if the generation still matches", () => {
        const store = new DedupStore();
        const e1 = store.start("k", { controller: new AbortController(), promise: Promise.resolve() });
        const e2 = store.start("k", { controller: new AbortController(), promise: Promise.resolve() });
        store.finish("k", e1.generation); // stale finish, should not touch e2's entry
        expect(store.get("k").generation).toBe(e2.generation);
        store.finish("k", e2.generation);
        expect(store.get("k")).toBeNull();
    });

    it("cancel aborts and removes the entry", () => {
        const store = new DedupStore();
        const controller = new AbortController();
        store.start("k", { controller, promise: Promise.resolve() });
        store.cancel("k", "cancelled");
        expect(controller.signal.aborted).toBe(true);
        expect(store.get("k")).toBeNull();
    });

    it("cancelAll aborts and removes every entry", () => {
        const store = new DedupStore();
        const c1 = new AbortController();
        const c2 = new AbortController();
        store.start("a", { controller: c1, promise: Promise.resolve() });
        store.start("b", { controller: c2, promise: Promise.resolve() });
        store.cancelAll("navigation");
        expect(c1.signal.aborted).toBe(true);
        expect(c2.signal.aborted).toBe(true);
        expect(store.get("a")).toBeNull();
        expect(store.get("b")).toBeNull();
    });
});
