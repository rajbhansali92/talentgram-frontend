import { describe, it, expect, vi, afterEach } from "vitest";
import React from "react";
import { render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

// The REAL page registry (adminRoutes) with every page component stubbed: this is about which URL serves which page, and who may see it.
vi.mock("@/lib/api", () => ({ isAdmin: () => globalThis.__admin === true, getAdmin: () => ({ role: globalThis.__admin ? "admin" : "team" }) }));
const { stub } = vi.hoisted(() => ({ stub: (name) => ({ default: () => globalThis.__h("div", { "data-testid": `page-${name}` }) }) }));
vi.mock("@/pages/Dashboard", () => stub("Dashboard"));
vi.mock("@/pages/TalentList", () => stub("TalentList"));
vi.mock("@/pages/TalentEdit", () => stub("TalentEdit"));
vi.mock("@/pages/ProjectList", () => stub("ProjectList"));
vi.mock("@/pages/ProjectEdit", () => stub("ProjectEdit"));
vi.mock("@/pages/ProductionOverview", () => stub("ProductionOverview"));
vi.mock("@/pages/SubmissionReviewCenter", () => stub("SubmissionReviewCenter"));
vi.mock("@/pages/LinkHistory", () => stub("LinkHistory"));
vi.mock("@/pages/LinkGenerator", () => stub("LinkGenerator"));
vi.mock("@/pages/LinkResults", () => stub("LinkResults"));
vi.mock("@/pages/Applications", () => stub("Applications"));
vi.mock("@/pages/UserManagement", () => stub("UserManagement"));
vi.mock("@/pages/AdminFeedback", () => stub("AdminFeedback"));
vi.mock("@/pages/MarketingHub", () => stub("MarketingHub"));
vi.mock("@/pages/NotificationsPage", () => stub("NotificationsPage"));
vi.mock("@/pages/WorkflowPage", () => stub("WorkflowPage"));
vi.mock("@/pages/StorageDashboard", () => stub("StorageDashboard"));
vi.mock("@/pages/WhatsAppEnginePage", () => stub("WhatsAppEnginePage"));
vi.mock("@/pages/SubmissionDiagnostics", () => stub("SubmissionDiagnostics"));
vi.mock("@/pages/ImportWizard", () => stub("ImportWizard"));
vi.mock("@/pages/CastingDesk", () => stub("CastingDesk"));
vi.mock("@/pages/SimpleAssistant", () => stub("SimpleAssistant"));

import { ADMIN_PAGE_ROUTES, ADMIN_PAGE_KEYS } from "@/components/workspace/adminRoutes";
import { ROUTE_META } from "@/components/workspace/routeMeta";
import { NAV_ITEMS } from "@/components/workspace/navConfig";
globalThis.__h = React.createElement;

afterEach(cleanup);
const Where = () => <span data-testid="where">{useLocation().pathname}</span>;
const at = (path, admin) => {
  globalThis.__admin = admin;
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {ADMIN_PAGE_ROUTES.map((r) => <Route key={r.abs} path={r.abs} element={r.element} />)}
        <Route path="*" element={<div data-testid="fallback" />} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
};

describe("the page registry", () => {
  it("has exactly one page for every route entry, in step with the metadata the tabs and launcher use", () => {
    expect(ADMIN_PAGE_ROUTES.map((r) => r.path)).toEqual(ROUTE_META.map((m) => m.path));
    expect([...ADMIN_PAGE_KEYS].sort()).toEqual(ROUTE_META.map((m) => m.path).sort());
    ADMIN_PAGE_ROUTES.forEach((r) => expect(r.element, r.abs).toBeTruthy());
  });

  it("serves every sidebar destination", () => {
    [...NAV_ITEMS.base, ...NAV_ITEMS.adminOnly].forEach((i) => {
      cleanup();
      at(i.to, true);
      expect(document.querySelector('[data-testid^="page-"]'), i.to).toBeTruthy();
    });
  });
});

describe("route guards (the menus hiding a page is only cosmetic)", () => {
  it.each(["/admin/users", "/admin/storage", "/admin/diagnostics", "/admin/imports"])("%s: an admin sees it, anyone else is sent to the Overview", (path) => {
    at(path, true);
    expect(screen.queryByTestId("page-Dashboard")).toBeNull();
    expect(document.querySelector('[data-testid^="page-"]')).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe(path);
    cleanup();
    at(path, false);
    expect(screen.getByTestId("where").textContent).toBe("/admin");
    expect(screen.getByTestId("page-Dashboard")).toBeTruthy();
  });

  it("pages every team member may open are not guarded", () => {
    at("/admin/projects/p1/submissions", false);
    expect(screen.getByTestId("page-SubmissionReviewCenter")).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/admin/projects/p1/submissions");
  });

  it("an admin-only route is guarded by its position in the shared list, so a NEW admin-only sidebar item is guarded automatically", () => {
    const guarded = ROUTE_META.filter((m) => m.adminOnly).map((m) => m.abs).sort();
    expect(guarded).toEqual(NAV_ITEMS.adminOnly.map((i) => i.to).sort());
  });
});
