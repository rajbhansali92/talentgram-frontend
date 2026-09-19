import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, waitFor, screen, fireEvent } from "@testing-library/react";
import ProductionDesk from "./ProductionDesk";

// Production Desk V2 (2026-09-19): talent-level shoot scheduling, overtime,
// reimbursements surfaced per-talent, payment tranches, and a restructured
// checklist. Focused regression coverage for exactly what changed — the
// pre-existing kickback/crew/document/task flows are untouched and are
// covered by the backend's own extensive test_production_desk.py.

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/pipeline/TalentBrowserModal", () => ({
    TalentPreviewDrawer: () => null,
    useMediaQuery: () => false,
}));
vi.mock("@/lib/talentPreviewCache", () => ({
    talentPreviewCache: { getTalent: () => null, hydrateTalent: () => Promise.resolve({}) },
}));

// jsdom gap (pre-existing, unrelated to this change) — see the same shim
// used by MarketingHub.test.jsx / SubmissionPage.material.test.jsx.
if (typeof window.ResizeObserver === "undefined") {
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
}
if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = () => {};
}
if (typeof Element.prototype.hasPointerCapture !== "function") {
    Element.prototype.hasPointerCapture = () => false;
}

const TALENT = {
    talent_id: "t1",
    name: "Harshita K Chundawat",
    image_url: null,
    instagram_handle: null,
    phone: "+919999900000",
    budget_per_day: 50000,
    budget_total: 50000,
    budget_total_is_explicit: false,
    shooting_days: 1,
    commission_percent: 15,
    commission_amount: 7500,
    payment_status: "pending",
    shoot_days: [],
    extra_hours_total: 0,
    commissionable_amount: 50000,
    reimbursement_total: 0,
    invoice_amount: 42500,
    readings_rehearsals: [],
    costume_trial_at: null,
    costume_trial_location: null,
    costume_trial_map_url: null,
    fitting_status: "not_scheduled",
    look_test_status: "not_scheduled",
    grooming_requirements: null,
    special_instructions: null,
    shoot_status: "not_scheduled",
};

const BASE_DATA = {
    project: {
        id: "proj-1", brand_name: "Google AI", status: "ongoing", commission_percent: "15%",
        shoot_dates: "20th - 22nd Sept", pd_shoot_date: null, medium_usage: "", director: "",
        production_house: "", additional_details: "", competitive_brand_enabled: false,
        pd_production_budget_per_day: null, pd_production_budget_total: 50000, pd_shooting_days: 1,
        pd_confirmation_mail_received: true, pd_invoice_raised: true, pd_invoice_sent: true,
        pd_payment_in_received: false, pd_gst_component_received: false,
        pd_production_status: "not_started", pd_call_time: "8:00 AM", pd_shoot_location: "Mumbai",
        pd_shoot_notes: "", pd_production_contact: { client_id: "c1", name: "Mudita", phone_number: "+919000000000" },
        client_budget_lines: [], talent_budget_lines: [],
        pd_reporting_time: "7:00 AM", pd_shoot_status: "completed",
        pd_payment_terms: "within 15 days", pd_expected_payment_date: null,
        pd_last_follow_up_at: null, pd_next_follow_up_at: null, pd_payment_followup_status: "in_progress",
        pd_payment_followup_notes: "",
        pd_agreement_status: null,
        pd_shoot_dates_list: ["2026-09-20", "2026-09-21"],
        pd_invoice_raised_and_sent: true,
    },
    finance: { zoho_status: "not_connected" },
    locked_talents: [TALENT],
    summary: {
        locked_count: 1, shoot_days: 1, talent_budget_total: 50000, extra_hours_total: 0,
        reimbursements_total: 0, production_budget_total: 50000,
        total_talent_and_overtime_and_reimbursements: 50000,
        commission_gross: 7500, kickbacks_total: 0, commission_net: 7500,
        payments_cleared: 0, payments_total: 1, payments_pending_amount: 50000,
        tranches_total: 0, tranches_received_total: 0,
    },
    needs_attention: [],
    kickbacks: [],
    reimbursements: [],
    reimbursement_checklist_status: "n_a",
    tranches: [],
    crew: [],
    documents: [],
    tasks: { all: [], due_today: [], overdue: [], upcoming: [], pending: [] },
    today: { tasks: [], trials: [], shoots: [], project_shoot_today: false, payment_followup_due: false },
    upcoming: { tasks: [], trials: [], shoots: [], payment_followup: false },
};

function mockAdminApi(overrides = {}) {
    const get = vi.fn((url) => {
        if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
        if (url === "/marketing/clients") return Promise.resolve({ data: [] });
        return Promise.resolve({ data: {} });
    });
    const post = vi.fn(() => Promise.resolve({ data: BASE_DATA }));
    const patch = vi.fn(() => Promise.resolve({ data: BASE_DATA }));
    const del = vi.fn(() => Promise.resolve({ data: BASE_DATA }));
    return { get, post, patch, delete: del, put: vi.fn(), ...overrides };
}

vi.mock("@/lib/api", () => ({ get adminApi() { return globalThis.__mockAdminApi; } }));

beforeEach(() => {
    globalThis.__mockAdminApi = mockAdminApi();
});
afterEach(cleanup);

describe("ProductionDesk V2", () => {
    it("loads and renders the overview without crashing", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("production-desk-root")).toBeTruthy());
        expect(screen.getByTestId("pd-overview")).toBeTruthy();
    });

    it("Talent Preparation no longer shows Fitting / Look Test / Shoot Status", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-talent-prep")).toBeTruthy());
        const prep = screen.getByTestId("pd-talent-prep");
        expect(prep.textContent).not.toMatch(/Fitting/);
        expect(prep.textContent).not.toMatch(/Look Test/);
        expect(prep.textContent).not.toMatch(/Shoot Status/);
        expect(prep.textContent).toMatch(/Costume Trial/);
        expect(prep.textContent).toMatch(/Trial Location/);
    });

    it("Project Requirements & Usage section is removed", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("production-desk-root")).toBeTruthy());
        expect(screen.queryByTestId("pd-requirements")).toBeNull();
        expect(screen.queryByText("Project Requirements & Usage")).toBeNull();
    });

    it("renders the new Shooting Schedule, Readings & Rehearsals, and Talent Financials sections", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-shoot-schedule")).toBeTruthy());
        expect(screen.getByTestId("pd-readings-rehearsals")).toBeTruthy();
        expect(screen.getByTestId("pd-talent-financials")).toBeTruthy();
        expect(screen.getByTestId(`pd-financial-${TALENT.talent_id}`)).toBeTruthy();
    });

    it("Talent Financials shows the server-computed breakdown, not re-derived values", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId(`pd-financial-${TALENT.talent_id}`)).toBeTruthy());
        const card = screen.getByTestId(`pd-financial-${TALENT.talent_id}`);
        expect(card.textContent).toMatch(/50,000/); // fee + commissionable both 50000
        expect(card.textContent).toMatch(/7,500/); // commission
        expect(card.textContent).toMatch(/42,500/); // invoice amount
    });

    it("adding a shoot day posts the correct payload", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
        const card = screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`);
        fireEvent.click(screen.getAllByText("Add Date")[0]);
        const dateInput = card.querySelector('input[type="date"]');
        fireEvent.change(dateInput, { target: { value: "2026-09-21" } });
        fireEvent.click(screen.getByText("Save"));
        await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk/talents/t1/shoot-days",
            expect.objectContaining({ date: "2026-09-21" }),
        ));
    });

    it("'Use Project Dates' one-tap calls the seed endpoint", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId(`pd-use-project-dates-${TALENT.talent_id}`)).toBeTruthy());
        fireEvent.click(screen.getByTestId(`pd-use-project-dates-${TALENT.talent_id}`));
        await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk/talents/t1/shoot-days/use-project-dates",
        ));
    });

    it("checklist shows Agreement Signed, combined Invoice Raised & Sent, Reimbursement Out (N/A), and renamed Talent Payment Out", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-checklist")).toBeTruthy());
        expect(screen.getByTestId("pd-checklist-agreement")).toBeTruthy();
        const invoiceRow = screen.getByTestId("pd-checklist-invoice");
        expect(invoiceRow.textContent).toMatch(/Invoice Raised & Sent/);
        expect(invoiceRow.textContent).toMatch(/Complete/);
        expect(screen.queryByText("Invoice Raised", { exact: true })).toBeNull();
        expect(screen.queryByText("Invoice Sent", { exact: true })).toBeNull();
        const reimbRow = screen.getByTestId("pd-checklist-reimbursement-out");
        expect(reimbRow.textContent).toMatch(/N\/A/);
        expect(screen.getByTestId("pd-checklist-talent-payments").textContent).toMatch(/Talent Payment Out/);
    });

    it("toggling the combined invoice switch patches both underlying fields", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-checklist-invoice")).toBeTruthy());
        const invoiceRow = screen.getByTestId("pd-checklist-invoice");
        fireEvent.click(invoiceRow.querySelector('button[role="switch"]'));
        await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk",
            expect.objectContaining({ invoice_raised_and_sent: false }),
        ));
    });

    it("Payment Follow-up shows the Concerned Person picker and a WhatsApp Follow-up button", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-payment-followup")).toBeTruthy());
        expect(screen.getByText("Concerned Person (CRM contact)")).toBeTruthy();
        expect(screen.getByTestId("pd-whatsapp-followup-btn")).toBeTruthy();
    });

    it("WhatsApp Follow-up fetches the message and updates last_follow_up_at", async () => {
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                if (url === "/projects/proj-1/production-desk/payment-followup-message") {
                    return Promise.resolve({ data: { phone: "+919000000000", contact_name: "Mudita", message: "Hi Mudita,\n\nJust a gentle reminder..." } });
                }
                return Promise.resolve({ data: {} });
            }),
        });
        const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-whatsapp-followup-btn")).toBeTruthy());
        fireEvent.click(screen.getByTestId("pd-whatsapp-followup-btn"));
        await waitFor(() => expect(openSpy).toHaveBeenCalled());
        expect(openSpy.mock.calls[0][0]).toContain("https://wa.me/919000000000");
        await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk",
            expect.objectContaining({ last_follow_up_at: expect.any(String) }),
        ));
        openSpy.mockRestore();
    });

    it("Ask Talent to Raise Invoice fetches the message and opens WhatsApp without sending", async () => {
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                if (url === "/projects/proj-1/production-desk/talents/t1/invoice-message") {
                    return Promise.resolve({ data: { phone: "+919999900000", talent_name: "Harshita", message: "Hi Harshita,\n\nInvoice amount to Talentgram: ₹42,500", breakdown: {} } });
                }
                return Promise.resolve({ data: {} });
            }),
        });
        const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`)).toBeTruthy());
        fireEvent.click(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`));
        await waitFor(() => expect(openSpy).toHaveBeenCalled());
        expect(openSpy.mock.calls[0][0]).toContain("https://wa.me/919999900000");
        expect(decodeURIComponent(openSpy.mock.calls[0][0])).toContain("42,500");
        openSpy.mockRestore();
    });

    it("Payment Tranches section allows adding a tranche", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-tranches")).toBeTruthy());
        fireEvent.click(screen.getByText("Add Tranche"));
        fireEvent.change(screen.getByPlaceholderText("e.g. Signing"), { target: { value: "Signing" } });
        const amountInputs = screen.getByTestId("pd-tranches").querySelectorAll('input[type="number"]');
        fireEvent.change(amountInputs[0], { target: { value: "200000" } });
        fireEvent.click(screen.getAllByText("Add Tranche")[1]);
        await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk/tranches",
            expect.objectContaining({ name: "Signing", amount: 200000 }),
        ));
    });

    it("structured Shoot Dates list can add and remove dates", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-shoot-dates-list")).toBeTruthy());
        expect(screen.getByTestId("pd-shoot-date-chip-2026-09-20")).toBeTruthy();
        expect(screen.getByTestId("pd-shoot-date-chip-2026-09-21")).toBeTruthy();

        const list = screen.getByTestId("pd-shoot-dates-list");
        const removeBtn = screen.getByTestId("pd-shoot-date-chip-2026-09-20").querySelector("button");
        fireEvent.click(removeBtn);
        await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk",
            expect.objectContaining({ shoot_dates_list: ["2026-09-21"] }),
        ));
    });
});
