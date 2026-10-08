import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// The global Production Desk is a first-class sidebar destination: present in the nav config, rendered in
// the real sidebar, highlighted on its route, routed by AdminApp — without disturbing the Projects link.
vi.mock("@/lib/api", () => ({ getAdmin: () => ({ id: "a", role: "admin", name: "Admin", email: "a@x.com" }), clearAdminSession: vi.fn(), adminApi: { get: vi.fn(() => Promise.resolve({ data: {} })) } }));
vi.mock("@/components/Logo", () => ({ default: () => null }));
vi.mock("@/components/NotificationBell", () => ({ default: () => null }));
vi.mock("@/components/ActionQueuePanel", () => ({ default: () => null }));
if (typeof window.ResizeObserver === "undefined") window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
if (typeof window.localStorage === "undefined" || typeof window.localStorage.getItem !== "function") {
    const store = {};
    Object.defineProperty(window, "localStorage", { value: {
        getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; }, clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
    }, configurable: true });
}
if (typeof window.matchMedia !== "function") window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });

import AdminLayout, { NAV_ITEMS } from "@/components/AdminLayout";

afterEach(cleanup);

describe("Production Desk navigation", () => {
    it("is in the main nav, right after Projects, pointing at /admin/production", () => {
        const labels = NAV_ITEMS.base.map((i) => i.label);
        expect(labels).toContain("Production Desk");
        expect(labels.indexOf("Production Desk")).toBe(labels.indexOf("Projects") + 1);
        expect(NAV_ITEMS.base.find((i) => i.label === "Production Desk").to).toBe("/admin/production");
        expect(NAV_ITEMS.adminOnly.some((i) => i.label === "Production Desk")).toBe(false);      // visible to the team, not admin-only
    });

    it("renders in the sidebar and is the active item on /admin/production (Projects is not)", () => {
        render(
            <MemoryRouter initialEntries={["/admin/production"]}>
                <Routes><Route path="/admin" element={<AdminLayout />}><Route path="production" element={<div>page</div>} /></Route></Routes>
            </MemoryRouter>,
        );
        const sidebar = screen.getByTestId("admin-sidebar");
        const prod = within(sidebar).getByText("Production Desk").closest("a");
        const proj = within(sidebar).getByText("Projects").closest("a");
        expect(prod.getAttribute("href")).toBe("/admin/production");
        expect(prod.getAttribute("aria-current")).toBe("page");
        expect(proj.getAttribute("aria-current")).toBeNull();
    });

    it("a project route keeps Projects active and does not light up Production Desk", () => {
        render(
            <MemoryRouter initialEntries={["/admin/projects/abc"]}>
                <Routes><Route path="/admin" element={<AdminLayout />}><Route path="projects/:id" element={<div>page</div>} /></Route></Routes>
            </MemoryRouter>,
        );
        const sidebar = screen.getByTestId("admin-sidebar");
        expect(within(sidebar).getByText("Projects").closest("a").getAttribute("aria-current")).toBe("page");
        expect(within(sidebar).getByText("Production Desk").closest("a").getAttribute("aria-current")).toBeNull();
    });
});
