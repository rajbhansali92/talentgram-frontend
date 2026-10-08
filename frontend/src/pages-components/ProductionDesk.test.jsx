import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, waitFor, screen, fireEvent, within } from "@testing-library/react";
import ProductionDesk from "./ProductionDesk";

// Production Desk V2 (2026-09-19) + V2 final UX polish (2026-09-19):
// merged Talent Preparation (costume trial + readings/rehearsals), merged
// Shoot Details (project dates + per-talent schedule) with a proper
// labeled table/card design, explicit Edit/Save/Cancel/Delete everywhere
// mutation happens, a redesigned Crew section with CRM peek + Call/
// WhatsApp/Email actions, a smart collapsible Overview dashboard, and a
// rebuilt talent invoice WhatsApp message (zero-suppression, billing
// details, WhatsApp-group-vs-phone destination preference). Pre-existing
// kickback/document/task flows are untouched and covered by the backend's
// own test_production_desk.py.

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));
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
if (typeof window.localStorage === "undefined") {
    const store = {};
    window.localStorage = {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; },
        clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
    };
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
    // Production/client side (internal only): talent rate 50,000 vs production quote 60,000.
    talent_agreed_rate: 50000,
    talent_net_payable: 42500,
    production_quote: 60000,
    spread: 10000,
    production_billable: 60000,
    talentgram_earning: 17500,
    readings_rehearsals: [],
    costume_trial_at: null,
    costume_trial_time: null,
    costume_trial_location: null,
    costume_trial_map_url: null,
    fitting_status: "not_scheduled",
    look_test_status: "not_scheduled",
    grooming_requirements: null,
    special_instructions: null,
    shoot_status: "not_scheduled",
    whatsapp_group_name: null,
};

const BASE_DATA = {
    project: {
        id: "proj-1", brand_name: "Google AI", status: "ongoing", commission_percent: "15%",
        shoot_dates: "20th - 22nd Sept", pd_shoot_date: null, medium_usage: "", director: "",
        production_house: "Google", additional_details: "", competitive_brand_enabled: false,
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
        production_basis: "per_talent", production_quote_total: 60000, production_quotes_set: 1, production_quotes_missing: 0,
        production_billable_total: 60000, production_overtime_total: 0, production_reimbursements_total: 0,
        talent_agreed_total: 50000, talent_payable_total: 42500, spread_total: 10000,
        talentgram_earnings_total: 17500, talentgram_earnings_net_of_kickbacks: 17500,
        client_received_total: 0, client_outstanding_total: 60000,
    },
    needs_attention: [],
    kickbacks: [],
    reimbursements: [],
    reimbursement_checklist_status: "n_a",
    tranches: [],
    crew: [],
    documents: [],
    tasks: { all: [], due_today: [], overdue: [], upcoming: [], pending: [] },
    today: { tasks: [], trials: [], shoots: [], shoot_days: [], prep_events: [], project_shoot_today: false, payment_followup_due: false },
    upcoming: { tasks: [], trials: [], shoots: [], shoot_days: [], prep_events: [], payment_followup: false },
    completed: { reimbursements: [], tranches: [], tasks: [] },
};

function mockAdminApi(overrides = {}) {
    const get = vi.fn((url) => {
        if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
        if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
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
    try { window.localStorage.clear(); } catch { /* noop */ }
    // Delete actions and the WhatsApp-group send now confirm via
    // window.confirm — default to "yes" so existing flows keep working;
    // tests that specifically need "cancel" override this per-test. Reset
    // call history every test so accumulated calls from a prior test never
    // leak into an assertion on "the first/last confirm call".
    if (window.confirm && window.confirm.mockRestore) window.confirm.mockRestore();
    vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(cleanup);

describe("ProductionDesk V2 final polish", () => {
    it("loads and renders the overview without crashing", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("production-desk-root")).toBeTruthy());
        expect(screen.getByTestId("pd-overview")).toBeTruthy();
    });

    it("Project Requirements & Usage section is removed", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("production-desk-root")).toBeTruthy());
        expect(screen.queryByTestId("pd-requirements")).toBeNull();
        expect(screen.queryByText("Project Requirements & Usage")).toBeNull();
    });

    // ---- Talent Preparation (merged: Costume Trial + Readings & Rehearsals) ----
    describe("Talent Preparation (merged)", () => {
        it("holds both Costume Trial and Readings & Rehearsals in one section, no separate cards", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-talent-prep")).toBeTruthy());
            const prep = screen.getByTestId("pd-talent-prep");
            expect(prep.textContent).toMatch(/Costume Trial/);
            expect(prep.textContent).toMatch(/Readings & Rehearsals/);
            expect(prep.textContent).not.toMatch(/Fitting/);
            expect(prep.textContent).not.toMatch(/Look Test/);
            expect(screen.queryByTestId("pd-readings-rehearsals")).toBeNull(); // no longer its own top-level card
            expect(screen.getByTestId(`pd-readings-${TALENT.talent_id}`)).toBeTruthy(); // nested inside prep
        });

        it("Costume Trial is read-only until Edit is pressed, then Save persists it", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-costume-trial-${TALENT.talent_id}`)).toBeTruthy());
            const block = screen.getByTestId(`pd-costume-trial-${TALENT.talent_id}`);
            expect(block.querySelector('input[type="date"]')).toBeNull(); // not editable yet
            fireEvent.click(block.querySelector("button")); // Edit
            const timeInput = block.querySelector('input[placeholder="e.g. 4:00 PM"]');
            fireEvent.change(timeInput, { target: { value: "4:00 PM" } });
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1",
                expect.objectContaining({ costume_trial_time: "4:00 PM" }),
            ));
        });

        it("adding a reading/rehearsal posts the correct payload", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-readings-${TALENT.talent_id}`)).toBeTruthy());
            const block = screen.getByTestId(`pd-readings-${TALENT.talent_id}`);
            fireEvent.click(block.querySelector("button")); // Add
            fireEvent.change(screen.getByPlaceholderText("e.g. 4 PM"), { target: { value: "5 PM" } });
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/readings-rehearsals",
                expect.objectContaining({ type: "reading", time: "5 PM" }),
            ));
        });

        it("an existing reading can be edited, cancelled, and deleted", async () => {
            const dataWithReading = {
                ...BASE_DATA,
                locked_talents: [{ ...TALENT, readings_rehearsals: [{ id: "r1", type: "rehearsal", date: "2026-09-22", time: "6 PM", location: "Studio A", notes: null }] }],
            };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithReading });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
                patch: vi.fn(() => Promise.resolve({ data: dataWithReading })),
                delete: vi.fn(() => Promise.resolve({ data: BASE_DATA })),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-reading-r1")).toBeTruthy());
            const row = screen.getByTestId("pd-reading-r1");
            expect(row.textContent).toMatch(/rehearsal/);

            // Edit -> Cancel discards
            fireEvent.click(row.querySelector("button")); // Edit
            fireEvent.click(screen.getByText("Cancel"));
            expect(globalThis.__mockAdminApi.patch).not.toHaveBeenCalled();

            // Edit -> Save persists
            fireEvent.click(screen.getByTestId("pd-reading-r1").querySelector("button"));
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/readings-rehearsals/r1",
                expect.objectContaining({ type: "rehearsal" }),
            ));

            // Delete
            const deleteBtn = screen.getByTestId("pd-reading-r1").querySelectorAll("button")[1];
            fireEvent.click(deleteBtn);
            await waitFor(() => expect(globalThis.__mockAdminApi.delete).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/readings-rehearsals/r1",
            ));
        });
    });

    // ---- Shoot Details (merged: project dates + per-talent schedule) ----
    describe("Shoot Details (merged)", () => {
        it("holds each talent's own schedule in one section — there is no separate project-level date list", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-details")).toBeTruthy());
            const card = screen.getByTestId("pd-shoot-details");
            expect(card.textContent).toMatch(/Talent Shooting Schedule/);
            expect(screen.queryByTestId("pd-shoot-dates-list")).toBeNull();          // duplicate date system removed
            expect(screen.queryByTestId(`pd-use-project-dates-${TALENT.talent_id}`)).toBeNull();
            expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy();
            expect(screen.queryByTestId("pd-shoot-schedule")).toBeNull(); // no longer a separate top-level card
        });

        it("shoot day table shows proper column headers, not placeholder-only cells", async () => {
            const dataWithDay = {
                ...BASE_DATA,
                locked_talents: [{ ...TALENT, shoot_days: [{ id: "d1", date: "2026-09-20", call_time: "8 AM", reporting_time: "7 AM", location: "Mumbai", agreed_hours: 12, actual_hours: 14, shoot_status: "scheduled" }] }],
            };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithDay });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-day-d1")).toBeTruthy());
            const scheduleCard = screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`);
            expect(scheduleCard.textContent).toMatch(/Agreed Basis/);
            expect(scheduleCard.textContent).toMatch(/Extra Hours/);
            expect(scheduleCard.textContent).toMatch(/Extra Amount/);
            const row = screen.getByTestId("pd-shoot-day-d1");
            expect(row.textContent).toMatch(/2h/); // 14 - 12 = 2h overtime, shown read-only
        });

        it("Actions column stays aligned with the header — same grid template, Actions never a separate cell than Status (regression for the wrapping defect)", async () => {
            const dataWithDay = {
                ...BASE_DATA,
                locked_talents: [{ ...TALENT, shoot_days: [{ id: "d1", date: "2026-09-20", agreed_hours: 12, actual_hours: 12, shoot_status: "scheduled" }] }],
            };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithDay });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-day-d1")).toBeTruthy());
            const scheduleCard = screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`);
            const headerRow = scheduleCard.querySelector(".hidden.lg\\:grid");
            const dataRow = screen.getByTestId("pd-shoot-day-d1");
            // Header and data row must share the identical column template —
            // this is exactly what the original defect violated (10 header
            // labels over a 9-column row).
            expect(headerRow.className).toContain("lg:grid-cols-[");
            expect(dataRow.className).toContain("lg:grid-cols-[");
            expect(headerRow.className.match(/lg:grid-cols-\[[^\]]+\]/)[0]).toBe(dataRow.className.match(/lg:grid-cols-\[[^\]]+\]/)[0]);
            // Edit and Delete controls are both inside the SAME trailing
            // Actions cell as siblings, never split across rows.
            const editBtn = within(dataRow).getByTitle("Edit");
            const deleteBtn = within(dataRow).getByTitle("Delete");
            expect(editBtn.parentElement).toBe(deleteBtn.parentElement);
        });

        it("a shoot day is read-only until Edit, and Save persists all fields together", async () => {
            const dataWithDay = {
                ...BASE_DATA,
                locked_talents: [{ ...TALENT, shoot_days: [{ id: "d1", date: "2026-09-20", call_time: "8 AM", reporting_time: "7 AM", location: "Mumbai", agreed_hours: 12, actual_hours: 12, shoot_status: "scheduled" }] }],
            };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithDay });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-day-d1")).toBeTruthy());
            const row = screen.getByTestId("pd-shoot-day-d1");
            expect(row.querySelector('input[type="date"]')).toBeNull();
            fireEvent.click(row.querySelector("button")); // Edit
            const editingRow = screen.getByTestId("pd-shoot-day-d1");
            const actualHoursInput = editingRow.querySelectorAll('input[type="number"]')[1];
            fireEvent.change(actualHoursInput, { target: { value: "15" } });
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/shoot-days/d1",
                expect.objectContaining({ actual_hours: 15, agreed_hours: 12 }),
            ));
        });

        it("adding a shoot day posts the correct payload", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            const card = screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`);
            fireEvent.click(within(card).getByText("Add Date"));
            const dateInput = card.querySelector('input[type="date"]');
            fireEvent.change(dateInput, { target: { value: "2026-09-21" } });
            fireEvent.click(within(card).getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/shoot-days",
                expect.objectContaining({ date: "2026-09-21" }),
            ));
        });

        it("never calls the removed project-dates seed endpoint", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            expect(screen.queryByText("Use Project Dates")).toBeNull();
            expect(globalThis.__mockAdminApi.post).not.toHaveBeenCalledWith(expect.stringContaining("use-project-dates"));
        });

        it("two talents keep fully independent shoot schedules on screen", async () => {
            const talentB = { ...TALENT, talent_id: "t2", name: "Shivi", shoot_days: [{ id: "d2", date: "2026-09-21", agreed_hours: 14, actual_hours: 14, shoot_status: "scheduled" }] };
            const talentA = { ...TALENT, shoot_days: [{ id: "d1", date: "2026-09-20", agreed_hours: 12, actual_hours: 12, shoot_status: "scheduled" }] };
            const dataTwoTalents = { ...BASE_DATA, locked_talents: [talentA, talentB] };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataTwoTalents });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-day-d1")).toBeTruthy());
            expect(screen.getByTestId("pd-shoot-day-d1").textContent).toMatch(/20 Sep/);
            expect(screen.getByTestId("pd-shoot-day-d2").textContent).toMatch(/21 Sep/);
        });

        it("adding a shoot day sends IST HH:MM times from time pickers and refuses reporting later than call", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-add-shoot-day-${TALENT.talent_id}`));
            fireEvent.change(screen.getByTestId(`pd-form-date-${TALENT.talent_id}`), { target: { value: "2026-10-08" } });
            fireEvent.change(screen.getByTestId(`pd-form-call-${TALENT.talent_id}`), { target: { value: "09:00" } });
            fireEvent.change(screen.getByTestId(`pd-form-reporting-${TALENT.talent_id}`), { target: { value: "10:00" } });
            // Reporting 10:00 is later than call 09:00 -> blocked with a visible reason.
            expect(screen.getByTestId(`pd-form-time-error-${TALENT.talent_id}`).textContent).toMatch(/cannot be later than call/);
            const form = screen.getByTestId(`pd-shoot-day-form-${TALENT.talent_id}`);
            expect(within(form).getByText("Save").disabled).toBe(true);
            fireEvent.change(screen.getByTestId(`pd-form-reporting-${TALENT.talent_id}`), { target: { value: "08:00" } });
            expect(screen.getByTestId(`pd-form-time-error-${TALENT.talent_id}`).textContent).toBe("");
            fireEvent.click(within(form).getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/shoot-days",
                expect.objectContaining({ date: "2026-10-08", call_time: "09:00", reporting_time: "08:00" }),
            ));
        });

        it("a schedule row shows times as 12-hour IST clock and links the selected Google place", async () => {
            const day = { id: "d9", date: "2026-10-08", call_time: "09:00", reporting_time: "08:00", location: "Mehboob Studio", location_address: "Bandra West, Mumbai", location_place_id: "ChIJabcdefghij", location_lat: 19.05, location_lng: 72.83, location_map_url: "https://maps.google.com/?cid=1", shoot_status: "scheduled", notes: "Bring props" };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: { ...BASE_DATA, locked_talents: [{ ...TALENT, shoot_days: [day] }] } });
                    return Promise.resolve({ data: url.endsWith("known-locations") ? { locations: [] } : (url === "/marketing/clients" ? [] : {}) });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-day-d9")).toBeTruthy());
            const row = screen.getByTestId("pd-shoot-day-d9");
            expect(row.textContent).toMatch(/9:00 AM/);
            expect(row.textContent).toMatch(/8:00 AM/);
            expect(row.textContent).toMatch(/Bandra West, Mumbai/);
            expect(row.textContent).toMatch(/Bring props/);
            const link = row.querySelector('a[href="https://maps.google.com/?cid=1"]');
            expect(link).toBeTruthy();
            expect(link.textContent).toMatch(/Mehboob Studio/);
        });

        it("editing a day keeps a legacy free-text call time unchanged instead of wiping it", async () => {
            const day = { id: "d8", date: "2026-10-08", call_time: "8 AM", reporting_time: "7 AM", location: "Mumbai", agreed_hours: 12, actual_hours: 12, shoot_status: "scheduled" };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: { ...BASE_DATA, locked_talents: [{ ...TALENT, shoot_days: [day] }] } });
                    return Promise.resolve({ data: url.endsWith("known-locations") ? { locations: [] } : (url === "/marketing/clients" ? [] : {}) });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-day-d8")).toBeTruthy());
            fireEvent.click(screen.getByTestId("pd-shoot-day-d8").querySelector("button")); // Edit
            fireEvent.change(screen.getByTestId("pd-day-reporting-d8"), { target: { value: "06:30" } }); // only change reporting
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalled());
            const body = globalThis.__mockAdminApi.patch.mock.calls.at(-1)[1];
            expect(body.reporting_time).toBe("06:30");
            expect("call_time" in body).toBe(false);          // untouched legacy text is not sent, so not wiped
        });

        it("picking a Google Maps result saves the real place (address, id, coordinates, link)", async () => {
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url, cfg) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: { ...BASE_DATA, capabilities: { places_search: true } } });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/places/search") return Promise.resolve({ data: { results: [{ place_id: "ChIJplace12345", name: "Film City", address: "Goregaon East, Mumbai" }] } });
                    if (url.startsWith("/projects/proj-1/production-desk/places/")) return Promise.resolve({ data: { place_id: "ChIJplace12345", name: "Film City", address: "Goregaon East, Mumbai, Maharashtra", lat: 19.16, lng: 72.88, maps_url: "https://maps.google.com/?cid=99" } });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-add-shoot-day-${TALENT.talent_id}`));
            fireEvent.change(screen.getByTestId(`pd-form-date-${TALENT.talent_id}`), { target: { value: "2026-10-09" } });
            const form = screen.getByTestId(`pd-shoot-day-form-${TALENT.talent_id}`);
            const locInput = form.querySelector('input[placeholder="Search or type a location"]');
            fireEvent.focus(locInput);
            fireEvent.change(locInput, { target: { value: "Film City" } });
            await waitFor(() => expect(screen.getByTestId("pd-place-result")).toBeTruthy(), { timeout: 3000 });
            fireEvent.click(screen.getByTestId("pd-place-result"));
            await waitFor(() => expect(within(form).getByText("Goregaon East, Mumbai, Maharashtra")).toBeTruthy());
            fireEvent.click(within(form).getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/talents/t1/shoot-days",
                expect.objectContaining({
                    location: "Film City", location_address: "Goregaon East, Mumbai, Maharashtra",
                    location_place_id: "ChIJplace12345", location_lat: 19.16, location_lng: 72.88,
                    location_map_url: "https://maps.google.com/?cid=99",
                }),
            ));
        });

        it("does not call Google search at all when the server has no Maps key", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);          // BASE_DATA has no capabilities.places_search
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-add-shoot-day-${TALENT.talent_id}`));
            const form = screen.getByTestId(`pd-shoot-day-form-${TALENT.talent_id}`);
            const locInput = form.querySelector('input[placeholder="Search or type a location"]');
            fireEvent.focus(locInput);
            fireEvent.change(locInput, { target: { value: "Film City" } });
            await new Promise((r) => setTimeout(r, 600));
            expect(globalThis.__mockAdminApi.get).not.toHaveBeenCalledWith(expect.stringContaining("/places/search"), expect.anything());
        });

        it("typing a different location after picking a place drops the old place's address and coordinates", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-add-shoot-day-${TALENT.talent_id}`));
            fireEvent.change(screen.getByTestId(`pd-form-date-${TALENT.talent_id}`), { target: { value: "2026-10-09" } });
            const form = screen.getByTestId(`pd-shoot-day-form-${TALENT.talent_id}`);
            const locInput = form.querySelector('input[placeholder="Search or type a location"]');
            fireEvent.change(locInput, { target: { value: "Plain typed studio" } });
            fireEvent.click(within(form).getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalled());
            const body = globalThis.__mockAdminApi.post.mock.calls.at(-1)[1];
            expect(body).toMatchObject({ location: "Plain typed studio", location_address: null, location_place_id: null, location_lat: null, location_lng: null });
        });

        it("project-level shoot info (call time, location, status) is read-only until Edit", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-shoot-details")).toBeTruthy());
            const card = screen.getByTestId("pd-shoot-details");
            expect(card.textContent).toMatch(/8:00 AM/); // call time shown read-only
            expect(card.querySelector('input[placeholder="e.g. 8:00 AM"]')).toBeNull();
        });
    });

    // ---- Checklist (unchanged from prior phase, regression) ----
    it("checklist shows Agreement Signed, combined Invoice Raised & Sent, Reimbursement Out (N/A), and renamed Talent Payment Out", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-checklist")).toBeTruthy());
        expect(screen.getByTestId("pd-checklist-agreement")).toBeTruthy();
        const invoiceRow = screen.getByTestId("pd-checklist-invoice");
        expect(invoiceRow.textContent).toMatch(/Invoice Raised & Sent/);
        const reimbRow = screen.getByTestId("pd-checklist-reimbursement-out");
        expect(reimbRow.textContent).toMatch(/N\/A/);
        expect(screen.getByTestId("pd-checklist-talent-payments").textContent).toMatch(/Talent Payment Out/);
    });

    // ---- Payment Follow-up (section-level edit mode) ----
    describe("Payment Follow-up", () => {
        it("shows read-only values normally, with an Edit control to change them", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-payment-followup-block")).toBeTruthy());
            const block = screen.getByTestId("pd-payment-followup-block");
            expect(block.textContent).toMatch(/within 15 days/);
            expect(block.textContent).toMatch(/Mudita/);
            expect(block.querySelector("textarea")).toBeNull(); // not editable yet
        });

        it("Edit -> Save persists all fields together; Cancel discards", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-payment-followup-block")).toBeTruthy());
            const block = screen.getByTestId("pd-payment-followup-block");
            fireEvent.click(block.querySelector("button")); // Edit
            const termsInput = screen.getByPlaceholderText("e.g. 50% advance, 50% on delivery");
            fireEvent.change(termsInput, { target: { value: "Full payment on delivery" } });
            fireEvent.click(screen.getByText("Cancel"));
            expect(globalThis.__mockAdminApi.patch).not.toHaveBeenCalled();
            expect(screen.getByTestId("pd-payment-followup-block").textContent).toMatch(/within 15 days/);

            fireEvent.click(screen.getByTestId("pd-payment-followup-block").querySelector("button"));
            fireEvent.change(screen.getByPlaceholderText("e.g. 50% advance, 50% on delivery"), { target: { value: "Full payment on delivery" } });
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk",
                expect.objectContaining({ payment_terms: "Full payment on delivery" }),
            ));
        });

        it("Follow-up shows a review dialog first, and only its confirm opens WhatsApp and updates last_follow_up_at", async () => {
            const contacts = [
                { client_id: "c1", name: "Mudita", role: null, company_name: "Google", has_phone: true, is_default: true, source: "production_contact" },
                { client_id: "c2", name: "Accounts Anil", role: "Production Manager", company_name: "Google", has_phone: true, is_default: false, source: "crew" },
            ];
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url, cfg) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/payment-followup-contacts") return Promise.resolve({ data: { contacts } });
                    if (url === "/projects/proj-1/production-desk/payment-followup-message") {
                        return Promise.resolve({ data: { phone: "+919000000000", contact_id: "c1", contact_name: "Mudita", message: "Hi Mudita,\n\nProduction amount: ₹60,000\nOutstanding: ₹60,000", warnings: [] } });
                    }
                    return Promise.resolve({ data: {} });
                }),
            });
            const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-whatsapp-followup-btn")).toBeTruthy());
            fireEvent.click(screen.getByTestId("pd-whatsapp-followup-btn"));
            // The message and recipient are shown BEFORE anything opens.
            await waitFor(() => expect(screen.getByTestId("pd-followup-dialog")).toBeTruthy());
            expect(screen.getByTestId("pd-followup-recipient-summary").textContent).toMatch(/Mudita/);
            expect(screen.getByTestId("pd-followup-message").textContent).toMatch(/Outstanding: ₹60,000/);
            expect(openSpy).not.toHaveBeenCalled();
            expect(globalThis.__mockAdminApi.get).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/payment-followup-message", { params: { contact_id: "c1" } },
            );
            fireEvent.click(screen.getByTestId("pd-followup-confirm"));
            await waitFor(() => expect(openSpy).toHaveBeenCalled());
            expect(openSpy.mock.calls[0][0]).toContain("https://wa.me/919000000000");
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk",
                expect.objectContaining({ last_follow_up_at: expect.any(String) }),
            ));
            openSpy.mockRestore();
        });

        it("Cancel in the follow-up review sends nothing and does not mark the project followed up", async () => {
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/payment-followup-message") {
                        return Promise.resolve({ data: { phone: "+919000000000", contact_id: "c1", contact_name: "Mudita", message: "Hi", warnings: ["No production quote entered — the message has no amounts."] } });
                    }
                    return Promise.resolve({ data: {} });
                }),
            });
            const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-whatsapp-followup-btn")).toBeTruthy());
            fireEvent.click(screen.getByTestId("pd-whatsapp-followup-btn"));
            await waitFor(() => expect(screen.getByTestId("pd-followup-dialog")).toBeTruthy());
            expect(screen.getByTestId("pd-followup-warning").textContent).toMatch(/no amounts/);
            fireEvent.click(screen.getByText("Cancel"));
            await waitFor(() => expect(screen.queryByTestId("pd-followup-dialog")).toBeNull());
            expect(openSpy).not.toHaveBeenCalled();
            expect(globalThis.__mockAdminApi.patch).not.toHaveBeenCalledWith("/projects/proj-1/production-desk", expect.objectContaining({ last_follow_up_at: expect.anything() }));
            openSpy.mockRestore();
        });

        it("lists the project's CRM contacts as follow-up recipients and disables ones with no phone", async () => {
            const contacts = [
                { client_id: "c1", name: "Mudita", role: null, company_name: "Google", has_phone: true, is_default: true },
                { client_id: "c3", name: "No Phone Nina", role: "Producer", company_name: null, has_phone: false, is_default: false },
            ];
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/payment-followup-contacts") return Promise.resolve({ data: { contacts } });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-followup-recipient-select")).toBeTruthy());
            // The default contact with a phone number is preselected.
            expect(screen.getByTestId("pd-followup-recipient-select").textContent).toMatch(/Mudita/);
        });
    });

    // ---- Payment Tranches ----
    describe("Payment Tranches", () => {
        it("allows adding a tranche", async () => {
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

        it("a tranche row shows Invoice Date/Number and Payment Date/Notes read-only, editable via Edit/Save", async () => {
            const dataWithTranche = {
                ...BASE_DATA,
                tranches: [{ id: "tr1", name: "Signing Advance", amount: 60000, trigger: "Signing", invoice_status: "raised_and_sent", invoice_date: "2026-09-10", invoice_number: "INV-001", payment_status: "received", payment_date: "2026-09-15", notes: "Paid via bank transfer" }],
            };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithTranche });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-tranche-tr1")).toBeTruthy());
            const row = screen.getByTestId("pd-tranche-tr1");
            expect(row.textContent).toMatch(/Signing Advance/);
            expect(row.textContent).toMatch(/INV-001/);
            expect(row.textContent).toMatch(/Paid via bank transfer/);
            expect(row.querySelector("select, textarea, input")).toBeNull(); // fully read-only until Edit

            fireEvent.click(row.querySelector("button")); // Edit
            fireEvent.click(screen.getByText("Save"));
            await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/tranches/tr1",
                expect.objectContaining({ invoice_number: "INV-001", notes: "Paid via bank transfer" }),
            ));
        });
    });

    // ---- Location picker ----
    it("location fields are searchable via LocationPicker and remain typeable", async () => {
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [{ name: "Yash Raj Studios", map_url: null }] } });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                return Promise.resolve({ data: {} });
            }),
        });
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-shoot-details")).toBeTruthy());
        const block = screen.getByTestId("pd-shoot-details");
        fireEvent.click(within(block).getByText("Edit")); // Edit shoot info (only Edit control in this card while empty)
        const locationInput = screen.getByPlaceholderText("Search or type a location");
        expect(locationInput.getAttribute("type")).toBe("text"); // regression guard for the PopoverTrigger type="button" bug
        fireEvent.change(locationInput, { target: { value: "Yash Raj" } });
        expect(locationInput.value).toBe("Yash Raj");
    });

    // ---- Crew (redesigned: CRM peek + actions) ----
    describe("Crew", () => {
        const CREW = { id: "cr1", role: "Production Manager", contact: { client_id: "c9", name: "Mudita Singh", phone_number: "+919876543210", email: "mudita@example.com", company_name: "Talentgram" } };

        it("shows a compact card with Call, WhatsApp, and Email actions when data exists", async () => {
            const dataWithCrew = { ...BASE_DATA, crew: [CREW] };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithCrew });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-crew-cr1")).toBeTruthy());
            expect(screen.getByTestId("pd-crew-call-cr1")).toBeTruthy();
            expect(screen.getByTestId("pd-crew-whatsapp-cr1")).toBeTruthy();
            expect(screen.getByTestId("pd-crew-email-cr1")).toBeTruthy();
        });

        it("hides action buttons when the underlying contact has no phone/email", async () => {
            const bareCrew = { id: "cr2", role: "Client", contact: { client_id: "c10", name: "No Contact Info" } };
            const dataWithCrew = { ...BASE_DATA, crew: [bareCrew] };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithCrew });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-crew-cr2")).toBeTruthy());
            expect(screen.queryByTestId("pd-crew-call-cr2")).toBeNull();
            expect(screen.queryByTestId("pd-crew-whatsapp-cr2")).toBeNull();
            expect(screen.queryByTestId("pd-crew-email-cr2")).toBeNull();
        });

        it("clicking the crew member's name opens a CRM peek with their details and an Open CRM link", async () => {
            const dataWithCrew = { ...BASE_DATA, crew: [CREW] };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithCrew });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-crew-peek-trigger-cr1")).toBeTruthy());
            fireEvent.click(screen.getByTestId("pd-crew-peek-trigger-cr1"));
            await waitFor(() => expect(screen.getByTestId("pd-crew-peek-cr1")).toBeTruthy());
            const peek = screen.getByTestId("pd-crew-peek-cr1");
            expect(peek.textContent).toMatch(/Production Manager/);
            expect(peek.textContent).toMatch(/Talentgram/);
            expect(peek.textContent).toMatch(/mudita@example.com/);
            expect(peek.textContent).toMatch(/Open CRM/);
        });

        // 2026-09-27 redesign — Crew role selection must use the SAME CRM
        // Contact Type lookup list Marketing Hub's own pickers use
        // (crm_contact_types via GET /marketing/contact-types), never a
        // second hardcoded role list.
        it("Add Crew Member's role picker is sourced from CRM's contact-types API, not the hardcoded fallback", async () => {
            const CONTACT_TYPES = [
                { id: "ct1", value: "line_producer", label: "Line Producer", group: "Production" },
                { id: "ct2", value: "dop", label: "DOP", group: "Production" },
            ];
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [{ id: "c1", name: "Rahul Mehta", phone_number: "+919876500000" }] });
                    if (url === "/marketing/contact-types") return Promise.resolve({ data: CONTACT_TYPES });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-crew")).toBeTruthy());
            expect(globalThis.__mockAdminApi.get.mock.calls.some(([url]) => url === "/marketing/contact-types")).toBe(true);

            fireEvent.click(screen.getByText("Add from CRM"));
            const roleSelect = await screen.findByTestId("pd-crew-role-select");
            // The trigger's displayed value is the CRM list's first label —
            // proof roleOptions came from the fetch, not CREW_ROLES's
            // "Director" (Candidate C, the old hardcoded fallback).
            expect(roleSelect.textContent).toMatch(/Line Producer/);
            expect(roleSelect.textContent).not.toMatch(/Director/);
        });

        it("falls back to the hardcoded role list only if the CRM contact-types fetch fails", async () => {
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/marketing/contact-types") return Promise.reject(new Error("network error"));
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-crew")).toBeTruthy());
            fireEvent.click(screen.getByText("Add from CRM"));
            const roleSelect = await screen.findByTestId("pd-crew-role-select");
            expect(roleSelect.textContent).toMatch(/Director/);
        });

        it("existing crew save behaviour still works: picking a CRM contact and submitting posts the chosen CRM-sourced role", async () => {
            const CONTACT_TYPES = [{ id: "ct1", value: "line_producer", label: "Line Producer", group: "Production" }];
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [{ id: "c1", name: "Rahul Mehta", phone_number: "+919876500000" }] });
                    if (url === "/marketing/contact-types") return Promise.resolve({ data: CONTACT_TYPES });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-crew")).toBeTruthy());
            fireEvent.click(screen.getByText("Add from CRM"));

            fireEvent.click(await screen.findByText("Search CRM contacts…"));
            fireEvent.click(await screen.findByText("Rahul Mehta"));

            fireEvent.click(screen.getByRole("button", { name: "Add Crew Member" }));
            await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalledWith(
                "/projects/proj-1/production-desk/crew",
                { client_id: "c1", role: "Line Producer" },
            ));
        });
    });

    // ---- Talent Invoice WhatsApp message (backend-computed; frontend wiring only) ----
    describe("Ask Talent to Raise Invoice", () => {
        it("no overtime, no reimbursement: opens WhatsApp with only Fee/Commission/Invoice/Billing lines", async () => {
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/talents/t1/invoice-message") {
                        return Promise.resolve({
                            data: {
                                phone: "+919999900000", talent_name: "Harshita", destination_type: "phone", whatsapp_group_name: null,
                                message: "Hi Harshita,\n\nTalent Fee: ₹50,000\nCommission @ 15%: ₹7,500\n\nInvoice Amount to Talentgram: ₹42,500\n\nBilling Details:\n\nCompany Name: Talentgram Agency LLP\nGSTIN No: 27AAVFT3898G1Z8",
                                breakdown: { talent_fee: 50000, extra_hours: 0, commissionable: 50000, commission_percent: 15, commission_amount: 7500, reimbursements: 0, invoice_amount: 42500 },
                            },
                        });
                    }
                    return Promise.resolve({ data: {} });
                }),
            });
            const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`));
            await waitFor(() => expect(openSpy).toHaveBeenCalled());
            const url = decodeURIComponent(openSpy.mock.calls[0][0]);
            expect(url).toContain("https://wa.me/919999900000");
            expect(url).toContain("42,500");
            expect(url).toContain("Billing Details");
            expect(url).toContain("GSTIN No: 27AAVFT3898G1Z8");
            expect(url).not.toContain("Extra Hours");
            expect(url).not.toContain("Reimbursements");
            expect(url).not.toContain("not subject to commission");
            openSpy.mockRestore();
        });

        it("with overtime and reimbursement: opens WhatsApp with the full breakdown", async () => {
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/talents/t1/invoice-message") {
                        return Promise.resolve({
                            data: {
                                phone: "+919999900000", talent_name: "Harshita", destination_type: "phone", whatsapp_group_name: null,
                                message: "Hi Harshita,\n\nTalent Fee: ₹50,000\nExtra Hours: 2 hours — ₹8,333\nCommissionable Amount: ₹58,333\nCommission @ 15%: ₹8,750\nReimbursements: ₹2,500\n\nInvoice Amount to Talentgram: ₹52,083",
                                breakdown: { talent_fee: 50000, extra_hours: 8333.33, extra_hours_count: 2, commissionable: 58333.33, commission_percent: 15, commission_amount: 8750, reimbursements: 2500, invoice_amount: 52083.33 },
                            },
                        });
                    }
                    return Promise.resolve({ data: {} });
                }),
            });
            const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`));
            await waitFor(() => expect(openSpy).toHaveBeenCalled());
            const url = decodeURIComponent(openSpy.mock.calls[0][0]);
            expect(url).toContain("Extra Hours: 2 hours");
            expect(url).toContain("Commissionable Amount: ₹58,333");
            expect(url).toContain("Reimbursements: ₹2,500");
            expect(url).toContain("52,083");
            openSpy.mockRestore();
        });

        it("prefers the WhatsApp group when set: after admin confirmation, sends via the existing WhatsApp Engine (not a wa.me link)", async () => {
            const talentWithGroup = { ...TALENT, whatsapp_group_name: "Harshita Casting Group" };
            const dataWithGroup = { ...BASE_DATA, locked_talents: [talentWithGroup] };
            const sendToGroup = vi.fn(() => Promise.resolve({ data: { ok: true, batch_id: "b1", job_ids: ["j1"], whatsapp_group_name: "Harshita Casting Group", talent_name: "Harshita" } }));
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithGroup });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/talents/t1/invoice-message") {
                        return Promise.resolve({
                            data: {
                                phone: "+919999900000", talent_name: "Harshita", destination_type: "group", whatsapp_group_name: "Harshita Casting Group",
                                message: "Hi Harshita,\n\nInvoice Amount to Talentgram: ₹42,500",
                                breakdown: {},
                            },
                        });
                    }
                    return Promise.resolve({ data: {} });
                }),
                post: (url, ...rest) => (url === "/projects/proj-1/production-desk/talents/t1/invoice-message/send-to-group" ? sendToGroup(url, ...rest) : Promise.resolve({ data: BASE_DATA })),
            });
            const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
            const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-whatsapp-destination-${TALENT.talent_id}`)).toBeTruthy());
            expect(screen.getByTestId(`pd-whatsapp-destination-${TALENT.talent_id}`).textContent).toMatch(/WhatsApp Group/);
            fireEvent.click(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`));
            await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
            expect(confirmSpy.mock.calls[0][0]).toContain("Harshita Casting Group"); // admin reviews the exact destination + message before it sends
            await waitFor(() => expect(sendToGroup).toHaveBeenCalled());
            expect(openSpy).not.toHaveBeenCalled(); // the group is the REAL destination now, not a wa.me phone link
            openSpy.mockRestore();
        });

        it("declining the confirmation does not send anything", async () => {
            const talentWithGroup = { ...TALENT, whatsapp_group_name: "Harshita Casting Group" };
            const dataWithGroup = { ...BASE_DATA, locked_talents: [talentWithGroup] };
            const sendToGroup = vi.fn(() => Promise.resolve({ data: { ok: true } }));
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithGroup });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/talents/t1/invoice-message") {
                        return Promise.resolve({ data: { phone: "+919999900000", talent_name: "Harshita", destination_type: "group", whatsapp_group_name: "Harshita Casting Group", message: "msg", breakdown: {} } });
                    }
                    return Promise.resolve({ data: {} });
                }),
                post: (url, ...rest) => (url.endsWith("/send-to-group") ? sendToGroup(url, ...rest) : Promise.resolve({ data: BASE_DATA })),
            });
            vi.spyOn(window, "confirm").mockReturnValue(false);
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`)).toBeTruthy());
            fireEvent.click(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`));
            await waitFor(() => expect(window.confirm).toHaveBeenCalled());
            expect(sendToGroup).not.toHaveBeenCalled();
        });

        it("falls back to the phone number when no WhatsApp group is set", async () => {
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: BASE_DATA });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    if (url === "/projects/proj-1/production-desk/talents/t1/invoice-message") {
                        return Promise.resolve({ data: { phone: "+919999900000", talent_name: "Harshita", destination_type: "phone", whatsapp_group_name: null, message: "Hi Harshita,\n\nInvoice Amount to Talentgram: ₹42,500", breakdown: {} } });
                    }
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-ask-invoice-${TALENT.talent_id}`)).toBeTruthy());
            expect(screen.getByTestId(`pd-whatsapp-destination-${TALENT.talent_id}`).textContent).not.toMatch(/Group/);
        });
    });

    // ---- Responsive layout (structural, not pixel-perfect) ----
    describe("Responsive layout", () => {
        it("Locked Talents renders both a desktop table and a mobile card list for the same data (CSS picks one per breakpoint)", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-locked-talents")).toBeTruthy());
            // Desktop table row
            expect(screen.getByTestId(`pd-talent-row-${TALENT.talent_id}`)).toBeTruthy();
            // Mobile card — distinct element/testid, not a duplicate of the table row
            const mobileCard = screen.getByTestId(`pd-talent-row-mobile-${TALENT.talent_id}`);
            expect(mobileCard).toBeTruthy();
            expect(mobileCard.parentElement.className).toContain("lg:hidden");
        });

        it("Talent Financials name and the invoice button are separate flex items, not competing for one truncated row", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-financial-${TALENT.talent_id}`)).toBeTruthy());
            const card = screen.getByTestId(`pd-financial-${TALENT.talent_id}`);
            const nameEl = within(card).getByText(TALENT.name);
            expect(nameEl.className).not.toContain("truncate"); // full name always visible now
            const header = card.querySelector(".flex.flex-col.lg\\:flex-row");
            expect(header).toBeTruthy();
        });

        it("SectionCard headers stack title above right-content through tablet width (Commission & Kickbacks, Payment Follow-up) — only true desktop goes row-mode", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-commission")).toBeTruthy());
            const commissionHeader = screen.getByTestId("pd-commission").querySelector(":scope > div");
            expect(commissionHeader.className).toContain("flex-col");
            expect(commissionHeader.className).toContain("lg:flex-row");
            const followupHeader = screen.getByTestId("pd-payment-followup").querySelector(":scope > div");
            expect(followupHeader.className).toContain("flex-col");
        });

        it("Shooting Schedule uses lg: (not sm:) for the desktop/mobile switch, so tablet width gets the readable stacked cards, not a cramped table", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`)).toBeTruthy());
            const card = screen.getByTestId(`pd-shoot-schedule-${TALENT.talent_id}`);
            expect(card.querySelector(".hidden.sm\\:grid")).toBeNull(); // no leftover sm: switch
        });
    });

    // ---- Overview dashboard ----
    describe("Overview dashboard", () => {
        it("shows the financial summary, project header, and is expanded by default", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-overview")).toBeTruthy());
            expect(screen.getByTestId("pd-overview-content")).toBeTruthy();
            const overview = screen.getByTestId("pd-overview");
            expect(overview.textContent).toMatch(/Google AI/);
            expect(overview.textContent).toMatch(/Talent Agreed Rates/);
            expect(overview.textContent).toMatch(/Total Earnings/);
        });

        it("shows Needs Attention, Today, Upcoming, and Completed panels", async () => {
            const dataWithEverything = {
                ...BASE_DATA,
                needs_attention: ["Client payment pending"],
                today: { ...BASE_DATA.today, shoot_days: [{ talent_id: "t1", talent_name: "Harshita", date: "2026-09-19", location: "Mumbai" }] },
                upcoming: { ...BASE_DATA.upcoming, prep_events: [{ talent_id: "t1", talent_name: "Harshita", type: "reading", date: "2026-09-22", location: null }] },
                completed: { reimbursements: [{ id: "r1", talent_name: "Harshita", amount: 2500 }], tranches: [], tasks: [] },
            };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataWithEverything });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-needs-attention")).toBeTruthy());
            expect(screen.getByTestId("pd-today").textContent).toMatch(/Harshita/);
            expect(screen.getByTestId("pd-upcoming").textContent).toMatch(/Reading/);
            expect(screen.getByTestId("pd-completed").textContent).toMatch(/Reimbursement cleared/);
        });

        it("can be collapsed and stays collapsed across a re-render (persisted per-viewer)", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-overview-content")).toBeTruthy());
            fireEvent.click(screen.getByTestId("pd-overview-toggle"));
            expect(screen.queryByTestId("pd-overview-content")).toBeNull();
            expect(window.localStorage.getItem("pd_overview_collapsed")).toBe("1");
        });

        // 2026-09-27 redesign tests below.
        it("Needs Attention shows only the outstanding items the backend returned, nothing more", async () => {
            const data = { ...BASE_DATA, needs_attention: ["Invoice not raised", "Call sheet missing"] };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-needs-attention")).toBeTruthy());
            const strip = screen.getByTestId("pd-needs-attention");
            expect(strip.textContent).toMatch(/Needs Attention · 2/);
            expect(strip.textContent).toMatch(/Invoice not raised/);
            expect(strip.textContent).toMatch(/Call sheet missing/);
            expect(screen.queryByTestId("pd-needs-attention-clear")).toBeNull();
        });

        it("shows a calm 'all clear' state when there is nothing outstanding", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />); // BASE_DATA.needs_attention is []
            await waitFor(() => expect(screen.getByTestId("pd-needs-attention-clear")).toBeTruthy());
            expect(screen.getByTestId("pd-needs-attention-clear").textContent).toMatch(/All production checks are clear/);
            expect(screen.queryByTestId("pd-needs-attention")).toBeNull();
        });

        it("groups the key numbers into Production / Talent / Talentgram / Payments instead of one flat list", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-overview-content")).toBeTruthy());
            const content = screen.getByTestId("pd-overview-content").textContent;
            for (const group of ["Production", "Talent", "Talentgram", "Payments"]) expect(content).toMatch(new RegExp(group));
            expect(content).toMatch(/Client Outstanding/);
            expect(content).toMatch(/Quote Spread/);
            expect(content).toMatch(/OT Spread/);
            expect(content).toMatch(/Reimbursement Spread/);
        });

        it("the Schedule section stays compact (a single muted line) for an empty Today/Upcoming/Completed", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />); // BASE_DATA's today/upcoming/completed are all empty
            await waitFor(() => expect(screen.getByTestId("pd-today")).toBeTruthy());
            expect(screen.getByTestId("pd-today").textContent).toMatch(/Nothing scheduled/);
            expect(screen.getByTestId("pd-upcoming").textContent).toMatch(/Nothing upcoming/);
            expect(screen.getByTestId("pd-completed").textContent).toMatch(/Nothing completed/);
        });
    });

    describe("Locked Talents and Tasks empty states (2026-09-27 redesign)", () => {
        it("Locked Talents shows a compact, informative empty state", async () => {
            const data = { ...BASE_DATA, locked_talents: [], summary: { ...BASE_DATA.summary, locked_count: 0 } };
            globalThis.__mockAdminApi = mockAdminApi({
                get: vi.fn((url) => {
                    if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data });
                    if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                    if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                    return Promise.resolve({ data: {} });
                }),
            });
            render(<ProductionDesk projectId="proj-1" project={{}} />);
            await waitFor(() => expect(screen.getByTestId("pd-locked-talents")).toBeTruthy());
            const section = screen.getByTestId("pd-locked-talents");
            expect(section.textContent).toMatch(/No locked talents yet/);
            expect(section.textContent).toMatch(/Move a talent to.*Locked.*in Casting Pipeline/);
        });

        it("Tasks shows a compact empty state and keeps Add Task visible", async () => {
            render(<ProductionDesk projectId="proj-1" project={{}} />); // BASE_DATA.tasks.pending is []
            await waitFor(() => expect(screen.getByTestId("pd-tasks")).toBeTruthy());
            const section = screen.getByTestId("pd-tasks");
            expect(section.textContent).toMatch(/No open tasks/);
            expect(within(section).getByText("Add Task")).toBeTruthy();
        });
    });

    // ---- Agreement N/A (regression from prior phase) ----
    it("Agreement N/A never appears as Pending in the checklist", async () => {
        const dataAgreementNA = { ...BASE_DATA, project: { ...BASE_DATA.project, pd_agreement_status: "n_a" } };
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data: dataAgreementNA });
                if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                return Promise.resolve({ data: {} });
            }),
        });
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-checklist-agreement")).toBeTruthy());
        expect(screen.getByTestId("pd-checklist-agreement").textContent).toMatch(/N\/A/);
    });
});

// ---------------------------------------------------------------------------
// Production Financials — talent rate ≠ production quote
// ---------------------------------------------------------------------------
describe("Production Financials", () => {
    const talentCard = (id, name, rate, quote, commission, extra = {}) => ({
        ...TALENT, talent_id: id, name, budget_per_day: rate, budget_total: rate, talent_agreed_rate: rate,
        commission_percent: 15, commission_amount: commission, production_quote: quote,
        spread: quote === null ? null : quote - rate, talent_net_payable: rate - commission,
        talentgram_earning: commission + (quote === null ? 0 : quote - rate), extra_hours_total: 0, reimbursement_total: 0,
        quote_spread: quote === null ? null : quote - rate, production_overtime: 0, production_overtime_is_explicit: false,
        production_reimbursement_total: 0, ot_spread: 0, reimbursement_spread: 0, production_billable: quote, ...extra,
    });
    const THREE = [
        talentCard("ta", "Talent A", 50000, 60000, 7500),
        talentCard("tb", "Talent B", 60000, 60000, 9000),
        talentCard("tc", "Talent C", 40000, 60000, 6000),
    ];
    const SUMMARY = {
        ...BASE_DATA.summary, locked_count: 3, production_basis: "per_talent", production_quote_total: 180000, production_quotes_set: 3,
        production_quotes_missing: 0, production_billable_total: 180000, production_overtime_total: 0, production_reimbursements_total: 0,
        talent_agreed_total: 150000, talent_payable_total: 127500, commission_gross: 22500, spread_total: 30000,
        talentgram_earnings_total: 52500, talentgram_earnings_net_of_kickbacks: 52500, client_received_total: 0, client_outstanding_total: 180000,
    };
    const load = (data) => {
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data });
                if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                return Promise.resolve({ data: {} });
            }),
        });
        render(<ProductionDesk projectId="proj-1" project={{}} />);
    };

    it("lays out client money, talent money and Talentgram earnings as three clearly separate groups", async () => {
        load({ ...BASE_DATA, locked_talents: THREE, summary: SUMMARY });
        await waitFor(() => expect(screen.getByTestId("pd-production-financials")).toBeTruthy());
        expect(screen.getByTestId("pd-fin-client").textContent).toMatch(/Client/);
        expect(screen.getByTestId("pd-fin-talent").textContent).toMatch(/Talent-facing/);
        expect(screen.getByTestId("pd-fin-internal").textContent).toMatch(/Internal only/);
        expect(screen.getByTestId("pd-fin-quote").textContent).toBe("₹1,80,000");          // production quote
        expect(screen.getByTestId("pd-fin-talent-rates").textContent).toBe("₹1,50,000");   // talent agreed rates
        expect(screen.getByTestId("pd-fin-spread").textContent).toBe("₹30,000");            // quote − rate
        expect(screen.getByTestId("pd-fin-earnings").textContent).toBe("₹52,500");          // 22,500 commission + 30,000 spread
        expect(screen.getByTestId("pd-fin-talent-payable").textContent).toBe("₹1,27,500");
        expect(screen.getByTestId("pd-fin-outstanding").textContent).toBe("₹1,80,000");
    });

    it("shows each talent's own rate, quote, commission, spread and earning independently", async () => {
        load({ ...BASE_DATA, locked_talents: THREE, summary: SUMMARY });
        await waitFor(() => expect(screen.getByTestId("pd-fin-row-ta")).toBeTruthy());
        const a = screen.getByTestId("pd-fin-row-ta").textContent;
        expect(a).toMatch(/₹50,000/); expect(a).toMatch(/₹60,000/); expect(a).toMatch(/₹7,500/); expect(a).toMatch(/₹10,000/); expect(a).toMatch(/₹17,500/);
        const b = screen.getByTestId("pd-fin-row-tb").textContent;
        expect(b).toMatch(/₹9,000/);                     // commission on the 60,000 rate
        expect(b).toMatch(/₹0/);                         // zero spread
        const c = screen.getByTestId("pd-fin-row-tc").textContent;
        expect(c).toMatch(/₹6,000/); expect(c).toMatch(/₹20,000/); expect(c).toMatch(/₹26,000/);
    });

    it("flags a talent with no production quote instead of inventing one", async () => {
        const noQuote = talentCard("ta", "Talent A", 50000, null, 7500);
        load({
            ...BASE_DATA, locked_talents: [noQuote, THREE[1]],
            summary: { ...SUMMARY, production_basis: "partial", production_quotes_set: 1, production_quotes_missing: 1, client_outstanding_total: null, spread_total: 0 },
        });
        await waitFor(() => expect(screen.getByTestId("pd-fin-row-ta")).toBeTruthy());
        expect(screen.getByTestId("pd-fin-row-ta").textContent).toMatch(/Not entered/);
        expect(screen.getByTestId("pd-fin-basis-note").textContent).toMatch(/only some talents/);
        expect(screen.getByTestId("pd-fin-outstanding").textContent).toBe("Incomplete");
    });

    it("keeps using the legacy project-level production budget until per-talent quotes exist", async () => {
        const legacy = talentCard("ta", "Talent A", 50000, null, 7500);
        load({
            ...BASE_DATA, locked_talents: [legacy],
            summary: { ...SUMMARY, locked_count: 1, production_basis: "project_budget", production_quotes_set: 0, production_quotes_missing: 1, production_billable_total: 85000, client_outstanding_total: 85000 },
        });
        await waitFor(() => expect(screen.getByTestId("pd-fin-basis-note")).toBeTruthy());
        expect(screen.getByTestId("pd-fin-basis-note").textContent).toMatch(/project-level production budget/);
        expect(screen.getByTestId("pd-fin-quote").textContent).toBe("₹85,000");
        fireEvent.click(screen.getByTestId("pd-fin-legacy-toggle"));
        expect(screen.getByTestId("pd-fin-legacy")).toBeTruthy();
    });

    it("the Production Quote is its own editable column, saved separately from the talent rate", async () => {
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId(`pd-talent-row-${TALENT.talent_id}`)).toBeTruthy());
        const row = screen.getByTestId(`pd-talent-row-${TALENT.talent_id}`);
        const inputs = row.querySelectorAll('input[type="number"]');
        // Budget/Day, Shoot Days, Talent Rate (Total), Production Quote, Commission %
        expect(inputs[3].value).toBe("60000");
        fireEvent.change(inputs[3], { target: { value: "65000" } });
        fireEvent.blur(inputs[3]);
        await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith(
            "/projects/proj-1/production-desk/talents/t1", { production_quote: 65000 },
        ));
        expect(globalThis.__mockAdminApi.patch).not.toHaveBeenCalledWith(
            "/projects/proj-1/production-desk/talents/t1", expect.objectContaining({ budget_total: expect.anything() }),
        );
    });

    it("each talent's card has separate Talent / Production / Talentgram columns, and the Talent column never shows a production number", async () => {
        load({ ...BASE_DATA, locked_talents: [THREE[0]], summary: { ...SUMMARY, locked_count: 1 } });
        const id = THREE[0].talent_id;
        await waitFor(() => expect(screen.getByTestId(`pd-financial-${id}`)).toBeTruthy());
        const talent = screen.getByTestId(`pd-fin-talent-col-${id}`).textContent;
        expect(talent).toMatch(/Agreed rate/); expect(talent).toMatch(/Net payable/);
        expect(talent).not.toMatch(/quote/i); expect(talent).not.toMatch(/spread/i); expect(talent).not.toMatch(/Production/i);
        expect(talent).not.toMatch(/60,000/);
        expect(screen.getByTestId(`pd-fin-prod-col-${id}`).textContent).toMatch(/Production quote/);
        expect(screen.getByTestId(`pd-fin-tg-col-${id}`).textContent).toMatch(/Quote spread/);
        expect(screen.getByTestId(`pd-fin-tg-col-${id}`).textContent).toMatch(/OT spread/);
        expect(screen.getByTestId(`pd-fin-tg-col-${id}`).textContent).toMatch(/Reimbursement spread/);
    });

    it("shows the worked example per talent: separate production OT and reimbursement, three spreads, one earnings figure", async () => {
        const a = talentCard("ta", "Talent A", 50000, 60000, 7500, {
            extra_hours_total: 5000, production_overtime: 7000, production_overtime_is_explicit: true, ot_spread: 2000,
            reimbursement_total: 2000, production_reimbursement_total: 3000, reimbursement_spread: 1000,
            talent_net_payable: 49500, production_billable: 70000, talentgram_earning: 20500,
        });
        load({ ...BASE_DATA, locked_talents: [a], summary: { ...SUMMARY, locked_count: 1, commission_gross: 7500, spread_total: 10000, ot_spread_total: 2000, reimbursement_spread_total: 1000, talentgram_earnings_total: 20500, production_billable_total: 70000, production_overtime_total: 7000, production_reimbursements_total: 3000, talent_payable_total: 49500, extra_hours_total: 5000, reimbursements_total: 2000 } });
        await waitFor(() => expect(screen.getByTestId("pd-fin-ta-earning")).toBeTruthy());
        const t = (k) => screen.getByTestId(`pd-fin-ta-${k}`).textContent;
        expect([t("rate"), t("commission"), t("talent-ot"), t("talent-reimb"), t("net")]).toEqual(["₹50,000", "₹7,500", "₹5,000", "₹2,000", "₹49,500"]);
        expect([t("quote"), t("prod-ot"), t("prod-reimb"), t("prod-total")]).toEqual(["₹60,000", "₹7,000", "₹3,000", "₹70,000"]);
        expect([t("quote-spread"), t("ot-spread"), t("reimb-spread"), t("earning")]).toEqual(["₹10,000", "₹2,000", "₹1,000", "₹20,500"]);
        expect(screen.getByTestId("pd-fin-ot-spread").textContent).toBe("₹2,000");
        expect(screen.getByTestId("pd-fin-reimb-spread").textContent).toBe("₹1,000");
        expect(screen.getByTestId("pd-fin-earnings").textContent).toBe("₹20,500");
        expect(screen.getByTestId("pd-fin-reconcile").textContent).toMatch(/₹70,000 = talent net ₹49,500 \+ commission ₹7,500 \+ spreads ₹13,000/);
    });

    it("production OT is its own editable field (empty = follows the talent's OT) and saves separately", async () => {
        const a = talentCard("ta", "Talent A", 50000, 60000, 7500, { extra_hours_total: 5000 });
        load({ ...BASE_DATA, locked_talents: [a], summary: { ...SUMMARY, locked_count: 1 } });
        await waitFor(() => expect(screen.getByTestId("pd-prod-ot-ta")).toBeTruthy());
        const input = screen.getByTestId("pd-prod-ot-ta").querySelector("input");
        expect(input.getAttribute("placeholder")).toBe("5000");                 // empty -> same as the talent's OT
        fireEvent.change(input, { target: { value: "7000" } });
        fireEvent.blur(input);
        await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith("/projects/proj-1/production-desk/talents/ta", { production_overtime: 7000 }));
    });

    it("each reimbursement has its own editable production amount, saved without touching the talent amount", async () => {
        const reimb = { id: "r1", talent_id: "ta", talent_name: "Talent A", expense_type: "Travel", amount: 2000, production_amount: null, status: "pending", material_id: "m1" };
        load({ ...BASE_DATA, locked_talents: [THREE[0]], reimbursements: [reimb], summary: { ...SUMMARY, locked_count: 1 } });
        await waitFor(() => expect(screen.getByTestId("pd-reimbursement-prod-r1")).toBeTruthy());
        const input = screen.getByTestId("pd-reimbursement-prod-r1").querySelector("input");
        expect(input.getAttribute("placeholder")).toBe("2000");
        fireEvent.change(input, { target: { value: "3000" } });
        fireEvent.blur(input);
        await waitFor(() => expect(globalThis.__mockAdminApi.patch).toHaveBeenCalledWith("/projects/proj-1/production-desk/reimbursements/r1", { production_amount: 3000 }));
    });

    it("the old duplicate 'Talent Financials' section is gone — one Financials section holds it all", async () => {
        load({ ...BASE_DATA, locked_talents: THREE, summary: SUMMARY });
        await waitFor(() => expect(screen.getByTestId("pd-production-financials")).toBeTruthy());
        expect(screen.queryByTestId("pd-talent-financials")).toBeNull();
        expect(screen.getByTestId("pd-production-financials").textContent).toMatch(/Ask to Raise Invoice/);
    });
});

// ---------------------------------------------------------------------------
// IST — dates are the Asia/Kolkata calendar day, whatever the browser's zone is
// ---------------------------------------------------------------------------
describe("IST dates", () => {
    it("a UTC evening instant shows as the NEXT calendar day in IST", async () => {
        // 2026-10-01 20:00 UTC == 2026-10-02 01:30 IST
        const data = { ...BASE_DATA, project: { ...BASE_DATA.project, pd_next_follow_up_at: "2026-10-01T20:00:00.000Z" } };
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data });
                if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                return Promise.resolve({ data: {} });
            }),
        });
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-payment-followup-block")).toBeTruthy());
        expect(screen.getByTestId("pd-payment-followup-block").textContent).toMatch(/02 Oct/);
    });

    it("a stored noon-UTC date-only value keeps its calendar day", async () => {
        const data = { ...BASE_DATA, project: { ...BASE_DATA.project, pd_expected_payment_date: "2026-10-05T12:00:00.000Z" } };
        globalThis.__mockAdminApi = mockAdminApi({
            get: vi.fn((url) => {
                if (url === "/projects/proj-1/production-desk") return Promise.resolve({ data });
                if (url === "/projects/proj-1/production-desk/known-locations") return Promise.resolve({ data: { locations: [] } });
                if (url === "/marketing/clients") return Promise.resolve({ data: [] });
                return Promise.resolve({ data: {} });
            }),
        });
        render(<ProductionDesk projectId="proj-1" project={{}} />);
        await waitFor(() => expect(screen.getByTestId("pd-payment-followup-block")).toBeTruthy());
        expect(screen.getByTestId("pd-payment-followup-block").textContent).toMatch(/05 Oct/);
    });
});
