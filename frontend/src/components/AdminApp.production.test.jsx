import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import React from "react";
import { render, cleanup, screen, waitFor } from "@testing-library/react";

// Route wiring: /admin/production resolves to the global Production Desk page through AdminApp's router
// (direct URL access / refresh), while the existing project routes are untouched. Layout, auth gate and
// every other page are stubbed; only the routing table is under test.
vi.mock("@/components/ProtectedRoute", () => ({ default: ({ children }) => children }));
vi.mock("@/components/AdminLayout", async () => {
    const { Outlet } = await import("react-router-dom");
    return { default: () => <div data-testid="layout"><Outlet /></div> };
});
// vi.mock factories are hoisted above module-level consts, so the stub builder is hoisted too and creates its
// element lazily (at render time) through React.createElement exposed on globalThis below.
const { stub } = vi.hoisted(() => ({ stub: (name) => ({ default: () => globalThis.__h("div", { "data-testid": `page-${name}` }) }) }));
vi.mock("@/pages/AdminLogin", () => stub("login"));
vi.mock("@/pages/Dashboard", () => stub("dashboard"));
vi.mock("@/pages/TalentList", () => stub("talents"));
vi.mock("@/pages/TalentEdit", () => stub("talent-edit"));
vi.mock("@/pages/ProjectList", () => stub("projects"));
vi.mock("@/pages/ProjectEdit", () => stub("project-edit"));
vi.mock("@/pages/ProductionOverview", () => stub("production"));
vi.mock("@/pages/SubmissionReviewCenter", () => stub("submissions"));
vi.mock("@/pages/LinkHistory", () => stub("links"));
vi.mock("@/pages/LinkGenerator", () => stub("link-gen"));
vi.mock("@/pages/LinkResults", () => stub("link-results"));
vi.mock("@/pages/Applications", () => stub("applications"));
vi.mock("@/pages/UserManagement", () => stub("users"));
vi.mock("@/pages/AdminFeedback", () => stub("feedback"));
vi.mock("@/pages/MarketingHub", () => stub("marketing"));
vi.mock("@/pages/NotificationsPage", () => stub("notifications"));
vi.mock("@/pages/WorkflowPage", () => stub("workflow"));
vi.mock("@/pages/StorageDashboard", () => stub("storage"));
vi.mock("@/pages/WhatsAppEnginePage", () => stub("whatsapp"));
vi.mock("@/pages/SubmissionDiagnostics", () => stub("diagnostics"));
vi.mock("@/pages/ImportWizard", () => stub("imports"));
vi.mock("@/pages/CastingDesk", () => stub("casting-desk"));
vi.mock("@/pages/SimpleAssistant", () => stub("assistant"));

import AdminApp from "@/components/AdminApp";
globalThis.__h = React.createElement;

afterEach(cleanup);
const open = (path) => { window.history.pushState({}, "", path); return render(<AdminApp />); };

describe("AdminApp routing", () => {
    it("serves the global Production Desk at /admin/production (direct URL access)", async () => {
        open("/admin/production");
        await waitFor(() => expect(screen.getByTestId("page-production")).toBeTruthy());
        expect(screen.getByTestId("layout")).toBeTruthy();
    });

    it("keeps query-string filters on the route", async () => {
        open("/admin/production?payment=client_pending&page=1");
        await waitFor(() => expect(screen.getByTestId("page-production")).toBeTruthy());
        expect(window.location.search).toBe("?payment=client_pending&page=1");
    });

    it("leaves the existing project routes exactly as they were", async () => {
        open("/admin/projects/abc123");
        await waitFor(() => expect(screen.getByTestId("page-project-edit")).toBeTruthy());
        cleanup();
        open("/admin/projects");
        await waitFor(() => expect(screen.getByTestId("page-projects")).toBeTruthy());
        cleanup();
        open("/admin/projects/abc123/submissions");
        await waitFor(() => expect(screen.getByTestId("page-submissions")).toBeTruthy());
    });

    it("an unknown admin path still falls back to the dashboard", async () => {
        open("/admin/does-not-exist");
        await waitFor(() => expect(screen.getByTestId("page-dashboard")).toBeTruthy());
    });
});
