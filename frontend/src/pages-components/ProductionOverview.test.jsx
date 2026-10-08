import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, waitFor, screen, fireEvent, within, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import ProductionOverview from "./ProductionOverview";

// Global Production Desk — a lens over the per-project desk. The list comes from /production/overview
// (server-side filtering/sorting/pagination); expanding a row loads the existing project desk; the
// invoice request and payment follow-up are the SAME shared actions the project page uses.

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));
if (typeof window.ResizeObserver === "undefined") {
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
}
if (typeof Element.prototype.scrollIntoView !== "function") Element.prototype.scrollIntoView = () => {};
if (typeof Element.prototype.hasPointerCapture !== "function") Element.prototype.hasPointerCapture = () => false;

const row = (over = {}) => ({
    project_id: "p1", brand_name: "Hotel Brand Shoot", status: "ongoing", stage: "not_started",
    client: { label: "ABC Films", contact_name: "Priya" }, locked_count: 2,
    shoot: { state: "today", next_date: "2026-10-08", next_location: "Mehboob Studio", next_map_url: null, first_date: "2026-10-08", last_date: "2026-10-09", days_count: 2 },
    money: { production_quote: 85000, production_basis: "per_talent", production_total: 90000, talent_agreed_total: 80000, talent_payable_total: 68000, commission: 12000, spread: 5000, overtime: 3000, reimbursements: 2000, earnings: 17000 },
    client_payment: { state: "pending", total: 90000, received: 0, outstanding: 90000 },
    talent_payment: { cleared: 0, total: 2, paid: 0, pending: 68000 },
    attention: ["Confirmation mail pending", "Invoice not raised", "Client payment pending", "GST component pending", "Call sheet missing"], attention_count: 5,
    ...over,
});
const ROW_B = row({
    project_id: "p2", brand_name: "Project B", client: { label: "XYZ", contact_name: null }, locked_count: 3,
    shoot: { state: "scheduled", next_date: "2026-10-10", next_location: "Film City", next_map_url: null, first_date: "2026-10-10", last_date: "2026-10-10", days_count: 1 },
    money: { production_quote: 200000, production_basis: "per_talent", production_total: 200000, talent_agreed_total: 170000, talent_payable_total: 144500, commission: 25500, spread: 30000, overtime: 0, reimbursements: 0, earnings: 55500 },
    client_payment: { state: "received", total: 200000, received: 200000, outstanding: 0 },
    talent_payment: { cleared: 2, total: 3, paid: 96000, pending: 48500 }, attention: ["Call sheet missing"], attention_count: 1,
});
const OVERVIEW = {
    items: [row(), ROW_B], data: [], total: 2, page: 0, size: 20, limit: 20, pages: 1, has_more: false, today: "2026-10-08",
    summary: { projects: 2, locked_talents: 5, shooting_today: 1, upcoming_shoots: 1, client_outstanding: 90000, talent_pending: 116500, earnings: 72500, needs_attention: 2 },
    upcoming_shoots: [
        { project_id: "p1", brand_name: "Hotel Brand Shoot", talent_id: "t1", talent_name: "Talent A", date: "2026-10-08", reporting_time: "08:00", call_time: "09:00", location: "Mehboob Studio", location_address: null, map_url: null, status: "scheduled" },
        { project_id: "p2", brand_name: "Project B", talent_id: "t9", talent_name: "Talent Z", date: "2026-10-10", reporting_time: "10:00", call_time: "11:00", location: "Film City", location_address: null, map_url: null, status: "scheduled" },
    ],
    facets: { clients: ["ABC Films", "XYZ"] },
};
const DESK = {
    project: { id: "p1", brand_name: "Hotel Brand Shoot" },
    locked_talents: [
        { talent_id: "t1", name: "Talent A", talent_agreed_rate: 50000, production_quote: 60000, commission_amount: 7500, spread: 10000, extra_hours_total: 3000, reimbursement_total: 0, talent_net_payable: 45500, payment_status: "pending",
          shoot_days: [{ id: "d1", date: "2026-10-08", reporting_time: "08:00", call_time: "09:00", location: "Mehboob Studio", location_address: "Bandra West, Mumbai", location_map_url: "https://maps.google.com/?cid=1" }] },
        { talent_id: "t2", name: "Talent B", talent_agreed_rate: 30000, production_quote: null, commission_amount: 4500, spread: null, extra_hours_total: 0, reimbursement_total: 2000, talent_net_payable: 27500, payment_status: "cleared", shoot_days: [] },
    ],
    summary: { talent_agreed_total: 80000, commission_gross: 12000, spread_total: 5000, production_overtime_total: 3000, production_reimbursements_total: 2000, talentgram_earnings_total: 17000,
               production_billable_total: 90000, client_received_total: 0, client_outstanding_total: 90000, talent_paid_total: 27500, talent_pending_total: 45500, production_basis: "per_talent" },
    tasks: { pending: [{ id: "k1" }], overdue: [] },
};

function makeApi(overrides = {}) {
    const calls = [];
    const get = vi.fn((url, cfg) => {
        calls.push([url, cfg?.params]);
        if (url === "/production/overview") return Promise.resolve({ data: overrides.overview ? overrides.overview(cfg?.params) : OVERVIEW });
        if (url === "/projects/p1/production-desk") return Promise.resolve({ data: DESK });
        if (url === "/projects/p1/production-desk/payment-followup-contacts") {
            return Promise.resolve({ data: { contacts: [
                { client_id: "c1", name: "Priya", role: null, company_name: "ABC Films", has_phone: true, is_default: true },
                { client_id: "c2", name: "Anil Accounts", role: "Producer", company_name: "ABC Films", has_phone: true, is_default: false },
            ] } });
        }
        if (url === "/projects/p1/production-desk/payment-followup-message") {
            return Promise.resolve({ data: { phone: "+919200000001", contact_id: cfg?.params?.contact_id || "c1", contact_name: "Priya", message: "Hi Priya,\n\nOutstanding: ₹90,000", warnings: [] } });
        }
        if (url === "/projects/p1/production-desk/talents/t1/invoice-message") {
            return Promise.resolve({ data: { destination_type: "phone", phone: "+919100000001", talent_name: "Talent A", message: "Talent Fee: ₹50,000" } });
        }
        return Promise.resolve({ data: {} });
    });
    return { get, post: vi.fn(() => Promise.resolve({ data: {} })), patch: vi.fn(() => Promise.resolve({ data: {} })), calls };
}
vi.mock("@/lib/api", () => ({ get adminApi() { return globalThis.__api; } }));

function LocationProbe() { const l = useLocation(); return <div data-testid="loc">{l.pathname}{l.search}</div>; }
function renderAt(url = "/admin/production") {
    return render(
        <MemoryRouter initialEntries={[url]}>
            <Routes><Route path="/admin/production" element={<><ProductionOverview /><LocationProbe /></>} /></Routes>
        </MemoryRouter>,
    );
}
const overviewCalls = () => globalThis.__api.calls.filter(([u]) => u === "/production/overview").map(([, p]) => p);

beforeEach(() => {
    globalThis.__api = makeApi();
    if (window.confirm && window.confirm.mockRestore) window.confirm.mockRestore();
    vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(cleanup);

describe("Global Production Desk", () => {
    it("loads the active default view from the server and shows every project with its key figures", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-row-p1")).toBeTruthy());
        expect(overviewCalls()[0]).toEqual({ size: 20, page: 0, status: "active", sort: "priority" });   // the active, most-urgent-first default
        const r1 = screen.getByTestId("po-row-p1").textContent;
        expect(r1).toMatch(/Hotel Brand Shoot/); expect(r1).toMatch(/ABC Films/);
        expect(screen.getByTestId("po-quote-p1").textContent).toBe("₹85,000");
        expect(screen.getByTestId("po-earnings-p1").textContent).toBe("₹17,000");
        expect(screen.getByTestId("po-client-pay-p1").textContent).toBe("Pending");
        expect(screen.getByTestId("po-talent-pay-p1").textContent).toBe("0/2");
        expect(screen.getByTestId("po-attention-p1").textContent).toBe("5");
        expect(screen.getByTestId("po-shoot-state-p1").textContent).toBe("Shooting today");
        expect(screen.getByTestId("po-client-pay-p2").textContent).toBe("Received");
        // the order is the server's (most urgent first), not re-sorted here
        const names = [...document.querySelectorAll('[data-testid^="po-name-"]')].map((e) => e.textContent);
        expect(names).toEqual(["Hotel Brand Shoot", "Project B"]);
    });

    it("shows the header summary exactly as the server computed it", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-summary")).toBeTruthy());
        expect(screen.getByTestId("po-sum-projects").textContent).toBe("2");
        expect(screen.getByTestId("po-sum-today").textContent).toBe("1");
        expect(screen.getByTestId("po-sum-client-outstanding").textContent).toBe("₹90,000");
        expect(screen.getByTestId("po-sum-talent-pending").textContent).toBe("₹1,16,500");
        expect(screen.getByTestId("po-sum-earnings").textContent).toBe("₹72,500");
        expect(screen.getByTestId("po-sum-attention").textContent).toBe("2");
    });

    it("lists upcoming shoots with date, report/call time and location, each linking to the project's production tab", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-upcoming")).toBeTruthy());
        const items = screen.getAllByTestId("po-upcoming-item");
        expect(items).toHaveLength(2);
        expect(items[0].textContent).toMatch(/Today/); expect(items[0].textContent).toMatch(/8:00 AM/); expect(items[0].textContent).toMatch(/9:00 AM/); expect(items[0].textContent).toMatch(/Mehboob Studio/);
        expect(items[1].getAttribute("href")).toBe("/admin/projects/p2?tab=production");
    });

    it("filters live in the URL: a deep link restores them and sends them to the server", async () => {
        renderAt("/admin/production?payment=client_pending&q=hotel&status=all&sort=earnings&page=0");
        await waitFor(() => expect(overviewCalls().length).toBeGreaterThan(0));
        expect(overviewCalls()[0]).toEqual({ size: 20, page: 0, payment: "client_pending", q: "hotel", status: "all", sort: "earnings" });
        expect(screen.getByTestId("po-search").value).toBe("hotel");
        expect(screen.getByTestId("po-reset")).toBeTruthy();                           // active filters can be cleared in one click
    });

    it("typing in search updates the URL and re-queries the server (debounced), and Reset clears everything", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-row-p1")).toBeTruthy());
        fireEvent.change(screen.getByTestId("po-search"), { target: { value: "hotel" } });
        await waitFor(() => expect(overviewCalls().some((p) => p.q === "hotel")).toBe(true), { timeout: 3000 });
        expect(screen.getByTestId("loc").textContent).toBe("/admin/production?q=hotel");
        fireEvent.click(screen.getByTestId("po-reset"));
        await waitFor(() => expect(screen.getByTestId("loc").textContent).toBe("/admin/production"));
        expect(screen.getByTestId("po-search").value).toBe("");
    });

    it("pages through results on the server", async () => {
        globalThis.__api = makeApi({ overview: (p) => ({ ...OVERVIEW, items: [p.page === 1 ? ROW_B : row()], total: 21, page: p.page, pages: 2, has_more: p.page === 0 }) });
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-pagination")).toBeTruthy());
        expect(screen.getByTestId("po-pagination").textContent).toMatch(/Page 1 of 2/);
        expect(screen.getByTestId("po-prev").disabled).toBe(true);
        fireEvent.click(screen.getByTestId("po-next"));
        await waitFor(() => expect(screen.getByTestId("po-row-p2")).toBeTruthy());
        expect(overviewCalls().at(-1)).toEqual({ size: 20, page: 1, status: "active", sort: "priority" });
        expect(screen.getByTestId("loc").textContent).toBe("/admin/production?page=1");
        expect(screen.getByTestId("po-next").disabled).toBe(true);
    });

    it("shows an empty state (with a way out) when nothing matches, and a different one when no project is locked", async () => {
        globalThis.__api = makeApi({ overview: () => ({ ...OVERVIEW, items: [], total: 0, pages: 0, summary: { ...OVERVIEW.summary, projects: 0 }, upcoming_shoots: [] }) });
        renderAt("/admin/production?q=zzz");
        await waitFor(() => expect(screen.getByTestId("po-empty")).toBeTruthy());
        expect(screen.getByTestId("po-empty").textContent).toMatch(/No projects match these filters/);
        cleanup();
        globalThis.__api = makeApi({ overview: () => ({ ...OVERVIEW, items: [], total: 0, pages: 0, upcoming_shoots: [] }) });
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-empty")).toBeTruthy());
        expect(screen.getByTestId("po-empty").textContent).toMatch(/No projects with locked talent yet/);
    });

    it("renders both the desktop row and the mobile card for every project (no squeezed table on phones)", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-row-p1")).toBeTruthy());
        expect(screen.getByTestId("po-toggle-p1").className).toMatch(/hidden lg:grid/);
        expect(screen.getByTestId("po-toggle-mobile-p1").className).toMatch(/lg:hidden/);
        expect(screen.getByTestId("po-toggle-mobile-p1").textContent).toMatch(/Hotel Brand Shoot/);
        expect(screen.getByTestId("po-toggle-mobile-p1").textContent).toMatch(/2 locked/);
    });

    it("loads project detail only when a row is expanded, with the talents, schedule and financial summary", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-row-p1")).toBeTruthy());
        expect(globalThis.__api.calls.some(([u]) => u === "/projects/p1/production-desk")).toBe(false);      // nothing loaded up front
        fireEvent.click(screen.getByTestId("po-toggle-p1"));
        await waitFor(() => expect(screen.getByTestId("po-detail-p1")).toBeTruthy());
        const t1 = screen.getByTestId("po-talent-t1").textContent;
        expect(t1).toMatch(/Talent A/); expect(t1).toMatch(/₹50,000/); expect(t1).toMatch(/₹60,000/); expect(t1).toMatch(/₹7,500/); expect(t1).toMatch(/₹10,000/); expect(t1).toMatch(/₹3,000/); expect(t1).toMatch(/Pending/);
        const t2 = screen.getByTestId("po-talent-t2").textContent;
        expect(t2).toMatch(/Not entered/); expect(t2).toMatch(/Cleared/);
        const sched = screen.getByTestId("po-schedule-p1");
        expect(sched.textContent).toMatch(/8:00 AM/); expect(sched.textContent).toMatch(/9:00 AM/); expect(sched.textContent).toMatch(/Bandra West, Mumbai/);
        expect(sched.querySelector('a[href="https://maps.google.com/?cid=1"]')).toBeTruthy();
        const fin = screen.getByTestId("po-financials-p1").textContent;
        for (const v of ["₹85,000", "₹80,000", "₹12,000", "₹5,000", "₹17,000", "₹90,000", "₹27,500", "₹45,500"]) expect(fin).toContain(v);
        fireEvent.click(screen.getByTestId("po-toggle-p1"));                              // collapses again
        expect(screen.queryByTestId("po-detail-p1")).toBeNull();
    });

    it("Open Project goes to the existing project workspace, from the row and from the expanded view", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-open-p1")).toBeTruthy());
        expect(screen.getByTestId("po-open-p1").getAttribute("href")).toBe("/admin/projects/p1?tab=production");
        fireEvent.click(screen.getByTestId("po-toggle-p1"));
        await waitFor(() => expect(screen.getByTestId("po-open-project-p1")).toBeTruthy());
        expect(screen.getByTestId("po-open-project-p1").getAttribute("href")).toBe("/admin/projects/p1?tab=production");
        expect(screen.getByTestId("po-edit-p1").getAttribute("href")).toBe("/admin/projects/p1?tab=production");
    });

    it("Ask to Raise Invoice uses the shared talent-facing action (the talent's own invoice message, opened for review in WhatsApp)", async () => {
        const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-row-p1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("po-toggle-p1"));
        await waitFor(() => expect(screen.getByTestId("po-invoice-t1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("po-invoice-t1"));
        await waitFor(() => expect(openSpy).toHaveBeenCalled());
        expect(globalThis.__api.get).toHaveBeenCalledWith("/projects/p1/production-desk/talents/t1/invoice-message");
        expect(openSpy.mock.calls[0][0]).toContain("https://wa.me/919100000001");
        expect(decodeURIComponent(openSpy.mock.calls[0][0])).toContain("Talent Fee: ₹50,000");
        openSpy.mockRestore();
    });

    it("Payment Follow-up: choose the recipient, review the message, and only the confirm opens WhatsApp for that contact", async () => {
        const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-followup-quick-p1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("po-followup-quick-p1"));                      // available on the row, without expanding
        await waitFor(() => expect(screen.getByTestId("po-followup-picker")).toBeTruthy());
        expect(screen.getByTestId("po-followup-picker").textContent).toMatch(/₹90,000/);   // production-side outstanding
        await waitFor(() => expect(screen.getByTestId("pd-followup-recipient-select").textContent).toMatch(/Priya/));   // default preselected
        fireEvent.click(screen.getByTestId("po-followup-preview"));
        await waitFor(() => expect(screen.getByTestId("pd-followup-dialog")).toBeTruthy());
        expect(globalThis.__api.get).toHaveBeenCalledWith("/projects/p1/production-desk/payment-followup-message", { params: { contact_id: "c1" } });
        expect(screen.getByTestId("pd-followup-recipient-summary").textContent).toMatch(/Priya/);
        expect(screen.getByTestId("pd-followup-message").textContent).toMatch(/Outstanding: ₹90,000/);
        expect(openSpy).not.toHaveBeenCalled();                                            // nothing is sent/opened before confirming
        fireEvent.click(screen.getByTestId("pd-followup-confirm"));
        await waitFor(() => expect(openSpy).toHaveBeenCalled());
        expect(openSpy.mock.calls[0][0]).toContain("https://wa.me/919200000001");
        await waitFor(() => expect(globalThis.__api.patch).toHaveBeenCalledWith("/projects/p1/production-desk", expect.objectContaining({ last_follow_up_at: expect.any(String) })));
        openSpy.mockRestore();
    });

    it("cancelling the follow-up sends nothing and records nothing", async () => {
        const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-followup-quick-p1")).toBeTruthy());
        fireEvent.click(screen.getByTestId("po-followup-quick-p1"));
        await waitFor(() => expect(screen.getByTestId("po-followup-preview")).toBeTruthy());
        await waitFor(() => expect(screen.getByTestId("pd-followup-recipient-select").textContent).toMatch(/Priya/));
        fireEvent.click(screen.getByTestId("po-followup-preview"));
        await waitFor(() => expect(screen.getByTestId("pd-followup-dialog")).toBeTruthy());
        fireEvent.click(within(screen.getByTestId("pd-followup-dialog")).getByText("Cancel"));
        await waitFor(() => expect(screen.queryByTestId("pd-followup-dialog")).toBeNull());
        expect(openSpy).not.toHaveBeenCalled();
        expect(globalThis.__api.patch).not.toHaveBeenCalled();
        openSpy.mockRestore();
    });

    it("does not load follow-up contacts for any project until someone acts on it", async () => {
        renderAt();
        await waitFor(() => expect(screen.getByTestId("po-row-p1")).toBeTruthy());
        expect(globalThis.__api.calls.some(([u]) => u.includes("payment-followup-contacts"))).toBe(false);
    });
});
