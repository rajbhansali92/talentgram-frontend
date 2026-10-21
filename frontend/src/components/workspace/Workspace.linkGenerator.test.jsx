import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, within, cleanup, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// The REAL Generate Link page, kept alive in a tab while another page is open. Only the page registry is stubbed for the OTHER routes.
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig();
  return { ...actual, getAdmin: () => ({ id: "u1", role: "admin", name: "A" }), isAdmin: () => true, clearAdminSession: vi.fn(), adminApi: { get: vi.fn(), post: vi.fn(), put: vi.fn() } };
});
vi.mock("sonner", () => ({ toast: Object.assign(() => {}, { error: vi.fn(), success: vi.fn() }) }));
vi.mock("@/components/Logo", () => ({ default: () => null }));
vi.mock("@/components/NotificationBell", () => ({ default: () => null }));
vi.mock("@/components/ActionQueuePanel", () => ({ default: () => null }));
vi.mock("@/components/personal-assistant/PersonalAssistant", () => ({ default: () => null }));
vi.mock("@/components/workspace/adminRoutes", async () => {
  const React = await import("react");
  const { ROUTE_META } = await import("@/components/workspace/routeMeta");
  const LinkGenerator = (await import("@/pages/LinkGenerator")).default;
  const other = (m) => React.createElement("div", { "data-testid": `other-${m.path || "overview"}` }, m.title);
  const ADMIN_PAGE_ROUTES = ROUTE_META.map((m) => ({ path: m.path, abs: m.abs, element: m.path === "links/new" ? React.createElement(LinkGenerator) : other(m) }));
  return { ADMIN_PAGE_ROUTES, ADMIN_PAGE_KEYS: [], RequireAdmin: ({ children }) => children };
});
if (typeof window.ResizeObserver === "undefined") window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
if (typeof window.IntersectionObserver === "undefined") window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } };
if (typeof window.localStorage === "undefined" || typeof window.localStorage.getItem !== "function") {
  const store = {};
  Object.defineProperty(window, "localStorage", { value: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }, clear: () => { Object.keys(store).forEach((k) => delete store[k]); } }, configurable: true });
}
if (typeof window.matchMedia !== "function") window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });

import { adminApi } from "@/lib/api";
import AdminLayout from "@/components/AdminLayout";

const TALENTS = ["Anya", "Bela", "Chitra", "Dev"].map((n, i) => ({ id: `t${i + 1}`, name: n, email: `${n}@x.com`, media: [], tags: [] }));
function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/admin/links/new"]}>
      <Routes><Route path="/admin" element={<AdminLayout />}><Route index element={null} /><Route path="*" element={null} /></Route></Routes>
    </MemoryRouter>,
  );
}
const pick = (id) => fireEvent.click(within(screen.getByTestId(`select-talent-${id}`)).getAllByRole("button")[0]);       // the card's own button
const sidebar = (name) => within(screen.getByTestId("admin-sidebar")).getByText(name).closest("a");
const activeTab = () => screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true")?.textContent;

beforeEach(() => {
  sessionStorage.clear();
  adminApi.get.mockReset(); adminApi.post.mockReset();
  adminApi.get.mockImplementation((u) => Promise.resolve({ data: u === "/talents" ? TALENTS : [] }));
  adminApi.post.mockResolvedValue({ data: { id: "L1" } });
});
afterEach(cleanup);

describe("Generate Link keeps its work while you use another tab", () => {
  it("the chosen link type, the title, the search, the selected talents and their ORDER survive switching away and back", async () => {
    renderApp();
    fireEvent.click(await screen.findByTestId("mode-pick-individual"));
    await screen.findByTestId("talent-select-grid");
    await waitFor(() => expect(screen.getByTestId("select-talent-t3")).toBeTruthy());
    fireEvent.change(screen.getByTestId("link-title-input"), { target: { value: "Talentgram x Nike" } });
    pick("t3");                          // chosen in this order: Chitra, then Anya
    pick("t1");
    expect(screen.getByTestId("selection-count").textContent).toBe("2 talents selected");

    fireEvent.click(sidebar("Talents"));                                               // go and do something else …
    expect(activeTab()).toBe("Talents");
    expect(screen.getByTestId("other-talents")).toBeTruthy();
    fireEvent.click(sidebar("Projects"));
    fireEvent.click(screen.getByRole("tab", { name: "Generate Link" }));               // … and come back

    expect(screen.getByTestId("selection-count").textContent).toBe("2 talents selected");
    expect(screen.getByTestId("link-title-input").value).toBe("Talentgram x Nike");
    pick("t4");                           // keep working where you left off
    expect(screen.getByTestId("selection-count").textContent).toBe("3 talents selected");
    fireEvent.click(screen.getByTestId("generate-link-btn"));
    await waitFor(() => expect(adminApi.post).toHaveBeenCalledTimes(1));
    expect(adminApi.post.mock.calls[0][0]).toBe("/links");
    expect(adminApi.post.mock.calls[0][1].talent_ids).toEqual(["t3", "t1", "t4"]);       // the order they were chosen in
  });

  it("the page is not reloaded on the way back (no second load of the talent list)", async () => {
    renderApp();
    fireEvent.click(await screen.findByTestId("mode-pick-individual"));
    await screen.findByTestId("talent-select-grid");
    const loads = () => adminApi.get.mock.calls.filter((c) => c[0] === "/talents").length;
    const before = loads();
    fireEvent.click(sidebar("Talents"));
    fireEvent.click(screen.getByRole("tab", { name: "Generate Link" }));
    await act(async () => {});
    expect(loads()).toBeLessThanOrEqual(before + 1);                                    // paused / resumed, not rebuilt: the grid is still there
    expect(screen.getByTestId("talent-select-grid")).toBeTruthy();
  });

  it("closing the Generate Link tab discards that page for good", async () => {
    renderApp();
    fireEvent.click(await screen.findByTestId("mode-pick-individual"));
    await screen.findByTestId("talent-select-grid");
    fireEvent.click(sidebar("Talents"));
    expect(screen.getByTestId("talent-select-grid")).toBeTruthy();                      // alive while its tab is open
    fireEvent.click(screen.getByLabelText("Close Generate Link"));
    expect(screen.queryByTestId("talent-select-grid")).toBeNull();                      // gone once closed
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Talents"]);
  });
});
