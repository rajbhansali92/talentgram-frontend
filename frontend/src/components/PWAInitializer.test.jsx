import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import PWAInitializer from "./PWAInitializer";

vi.mock("next/navigation", () => ({
    usePathname: () => "/l/some-slug",
}));

const toastInfo = vi.fn();
vi.mock("sonner", () => ({
    toast: { info: (...args) => toastInfo(...args) },
}));

// A minimal EventTarget-backed fake for navigator.serviceWorker, so
// dispatching a real "controllerchange" event exercises PWAInitializer's
// actual listener wiring rather than a hand-rolled callback list.
function makeFakeServiceWorkerContainer({ hasControllerAtStart = false } = {}) {
    const target = new EventTarget();
    target.controller = hasControllerAtStart ? { scriptURL: "/sw.js" } : null;
    target.register = vi.fn().mockResolvedValue({ scope: "/" });
    return target;
}

beforeEach(() => {
    toastInfo.mockClear();
});
afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
});

describe("PWAInitializer — service worker registration + update detection", () => {
    it("registers the service worker on mount", () => {
        const sw = makeFakeServiceWorkerContainer();
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });

        render(<PWAInitializer />);

        expect(sw.register).toHaveBeenCalledWith("/sw.js");
    });

    it("does not show the update toast for the initial controllerchange (first-ever claim, no prior controller)", () => {
        const sw = makeFakeServiceWorkerContainer({ hasControllerAtStart: false });
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });

        render(<PWAInitializer />);
        sw.dispatchEvent(new Event("controllerchange"));

        expect(toastInfo).not.toHaveBeenCalled();
    });

    it("shows the update toast on a SECOND controllerchange after the initial claim", () => {
        const sw = makeFakeServiceWorkerContainer({ hasControllerAtStart: false });
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });

        render(<PWAInitializer />);
        sw.dispatchEvent(new Event("controllerchange")); // initial claim — suppressed
        sw.dispatchEvent(new Event("controllerchange")); // a real takeover — shown

        expect(toastInfo).toHaveBeenCalledTimes(1);
        expect(toastInfo.mock.calls[0][0]).toMatch(/new version/i);
    });

    it("shows the update toast immediately if a controller already existed at mount (returning session)", () => {
        const sw = makeFakeServiceWorkerContainer({ hasControllerAtStart: true });
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });

        render(<PWAInitializer />);
        sw.dispatchEvent(new Event("controllerchange"));

        expect(toastInfo).toHaveBeenCalledTimes(1);
    });

    it("never forces a reload itself — the toast only offers a user-triggered action", () => {
        const sw = makeFakeServiceWorkerContainer({ hasControllerAtStart: true });
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });
        const reloadSpy = vi.fn();
        Object.defineProperty(window, "location", { value: { reload: reloadSpy }, writable: true });

        render(<PWAInitializer />);
        sw.dispatchEvent(new Event("controllerchange"));

        expect(reloadSpy).not.toHaveBeenCalled();
        // The action handler exists and is caller-triggered, not auto-invoked.
        const options = toastInfo.mock.calls[0][1];
        expect(typeof options.action.onClick).toBe("function");
    });

    it("does not notify twice for repeated controllerchange events", () => {
        const sw = makeFakeServiceWorkerContainer({ hasControllerAtStart: true });
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });

        render(<PWAInitializer />);
        sw.dispatchEvent(new Event("controllerchange"));
        sw.dispatchEvent(new Event("controllerchange"));
        sw.dispatchEvent(new Event("controllerchange"));

        expect(toastInfo).toHaveBeenCalledTimes(1);
    });

    it("logs a structured JSON line for every controllerchange, including the suppressed initial claim", () => {
        const sw = makeFakeServiceWorkerContainer({ hasControllerAtStart: false });
        vi.stubGlobal("navigator", { ...navigator, serviceWorker: sw });
        const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});

        render(<PWAInitializer />);
        sw.dispatchEvent(new Event("controllerchange")); // initial claim
        sw.dispatchEvent(new Event("controllerchange")); // real takeover

        const logged = logSpy.mock.calls.map(([line]) => JSON.parse(line));
        expect(logged).toHaveLength(2);
        expect(logged[0]).toMatchObject({ source: "service_worker", event: "controllerchange", is_initial_claim: true });
        expect(logged[1]).toMatchObject({ source: "service_worker", event: "controllerchange", is_initial_claim: false });
        logSpy.mockRestore();
    });
});
