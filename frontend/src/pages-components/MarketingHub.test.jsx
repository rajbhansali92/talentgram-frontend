import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, waitFor, screen, fireEvent } from "@testing-library/react";
import MarketingHub from "./MarketingHub";

// CRM talent-industry adaptation (2026-09-19): the generic corporate-sales
// fields (Lifecycle Stage / Deal Value / free-text Company / Relationship
// Tags in the primary form) were replaced with a Talentgram-specific
// information architecture — Production House/Company (admin-managed
// directory), Contact Type (admin-managed lookup), Designation, and
// Relationship Status. Focused regression coverage for exactly that change;
// the pre-existing CRUD/bulk-action/interaction-logging surface is covered
// by the backend's test_marketing.py and is unmodified here.

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/CommTimeline", () => ({ default: () => null }));

// jsdom in this project's vitest environment doesn't provide window.localStorage
// at all (a pre-existing test-environment gap, unrelated to this change) — shim
// it in-memory, scoped to just this file, rather than touching global config.
class MemoryStorage {
    constructor() { this.store = {}; }
    getItem(k) { return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null; }
    setItem(k, v) { this.store[k] = String(v); }
    removeItem(k) { delete this.store[k]; }
    clear() { this.store = {}; }
}
Object.defineProperty(window, "localStorage", { value: new MemoryStorage(), writable: true });

// cmdk (the Command palette LookupPicker uses) requires ResizeObserver,
// which jsdom doesn't implement — same category of pre-existing gap as
// localStorage above. A no-op stub is all cmdk needs to mount.
if (typeof window.ResizeObserver === "undefined") {
    window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
}
if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = () => {};
}
if (typeof Element.prototype.hasPointerCapture !== "function") {
    Element.prototype.hasPointerCapture = () => false;
}

const EXISTING_CLIENT = {
    id: "client-1",
    name: "Rahul Mehta",
    company_name: "Footloose Production",
    company_id: "co-1",
    phone_number: "+919876543210",
    email: "rahul@footloose.com",
    tags: [],
    stage: "active",
    value: null,
    contact_type: "producer",
    designation: "Line Producer",
    notes: null,
    created_at: "2026-09-01T00:00:00Z",
    last_contacted_date: "2026-09-18T00:00:00Z",
    archived: false,
    deleted: false,
};

const COMPANIES = [{ id: "co-1", name: "Footloose Production", active: true }];
const CONTACT_TYPES_FROM_API = [
    { id: "ct-1", value: "producer", label: "Producer", group: "Production", active: true },
    { id: "ct-2", value: "casting_director", label: "Casting Director", group: "Casting", active: true },
];

function mockAdminApi({ isAdminUser = true } = {}) {
    const get = vi.fn((url) => {
        if (url === "/marketing/clients") return Promise.resolve({ data: [EXISTING_CLIENT] });
        if (url === "/marketing/companies") return Promise.resolve({ data: COMPANIES });
        if (url === "/marketing/contact-types") return Promise.resolve({ data: CONTACT_TYPES_FROM_API });
        return Promise.resolve({ data: [] });
    });
    const post = vi.fn(() => Promise.resolve({ data: { ...EXISTING_CLIENT, id: "new-client" } }));
    return { get, post, put: vi.fn(), delete: vi.fn() };
}

let mockIsAdmin = true;
vi.mock("@/lib/api", () => ({
    get adminApi() { return globalThis.__mockAdminApi; },
    isAdmin: () => mockIsAdmin,
}));

beforeEach(() => {
    mockIsAdmin = true;
    globalThis.__mockAdminApi = mockAdminApi();
});
afterEach(cleanup);

describe("MarketingHub — CRM talent-industry adaptation", () => {
    it("loads existing clients without crashing and renders the new fields", async () => {
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        expect(screen.getByText("Rahul Mehta")).toBeTruthy();
        expect(screen.getByText("Footloose Production")).toBeTruthy();
        expect(screen.getByText("Line Producer")).toBeTruthy();
    });

    it("renamed the page to the Talentgram relationships concept, not generic corporate CRM language", async () => {
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        expect(screen.getByText("Talentgram Relationships")).toBeTruthy();
        expect(screen.queryByText("Client Intelligence")).toBeNull();
    });

    it("Contact Type filter pills are driven by the fetched list, not a hardcoded constant", async () => {
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        expect(screen.getByText("Casting Director")).toBeTruthy();
        // A type that exists in the old hardcoded CONTACT_TYPES const but was
        // NOT returned by the mocked GET /contact-types must not appear —
        // proves the pills come from the fetch, not the fallback constant.
        expect(screen.queryByText("Modeling Agency")).toBeNull();
    });

    it("Add Contact dialog shows the new field set — no Deal Value, no Lifecycle Stage, no free-text Company", async () => {
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        fireEvent.click(screen.getByTestId("marketing-add-client-btn"));
        await waitFor(() => expect(screen.getByTestId("marketing-add-client-dialog")).toBeTruthy());

        expect(screen.getByText("Create Industry Contact")).toBeTruthy();
        expect(screen.getByText("Production House / Company")).toBeTruthy();
        expect(screen.getByText("Contact Type")).toBeTruthy();
        expect(screen.getByText("Designation / Role")).toBeTruthy();
        expect(screen.getByText("Relationship Status")).toBeTruthy();
        expect(screen.getByText("Notes")).toBeTruthy();

        expect(screen.queryByText("Deal/Relationship Value (INR)")).toBeNull();
        expect(screen.queryByText("Deal Value (INR)")).toBeNull();
        expect(screen.queryByText("Lifecycle Stage")).toBeNull();
        expect(screen.queryByText("Relationship Tags (Comma-separated)")).toBeNull();
        expect(screen.queryByTestId("marketing-input-value")).toBeNull();
        expect(screen.queryByTestId("marketing-input-tags")).toBeNull();
    });

    it("Relationship Status dropdown offers the new values, keeping Key Account for backward compatibility", async () => {
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        fireEvent.click(screen.getByTestId("marketing-add-client-btn"));
        await waitFor(() => expect(screen.getByTestId("marketing-input-stage")).toBeTruthy());

        const select = screen.getByTestId("marketing-input-stage");
        const optionLabels = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
        expect(optionLabels).toEqual([
            "New Contact", "Active Relationship", "Project-Based", "Past Contact", "Dormant", "Key Account",
        ]);
    });

    it("a non-admin user does not see the '+ Add' / 'Manage' lookup-management affordances", async () => {
        mockIsAdmin = false;
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        expect(screen.queryByTestId("marketing-manage-contact-types-btn")).toBeNull();

        fireEvent.click(screen.getByTestId("marketing-add-client-btn"));
        await waitFor(() => expect(screen.getByTestId("marketing-add-client-dialog")).toBeTruthy());
        fireEvent.click(screen.getByTestId("marketing-input-company"));
        await waitFor(() => expect(screen.getByPlaceholderText("Search company...")).toBeTruthy());
        expect(screen.queryByText("Add")).toBeNull();
        expect(screen.queryByText("Manage")).toBeNull();
    });

    it("creating a contact posts company_id/contact_type/designation/notes, not free-text company or tags/value", async () => {
        render(<MarketingHub />);
        await waitFor(() => expect(screen.getByTestId("marketing-clients-list")).toBeTruthy());
        fireEvent.click(screen.getByTestId("marketing-add-client-btn"));
        await waitFor(() => expect(screen.getByTestId("marketing-add-client-dialog")).toBeTruthy());

        fireEvent.change(screen.getByTestId("marketing-input-name"), { target: { value: "New Test Contact" } });

        fireEvent.click(screen.getByTestId("marketing-input-company"));
        await waitFor(() => expect(document.querySelector("[cmdk-item]")).toBeTruthy());
        // "Footloose Production" also appears in the existing client's list
        // row — disambiguate to the picker's own option (cmdk marks each
        // CommandItem with a [cmdk-item] attribute).
        const companyOption = Array.from(document.querySelectorAll("[cmdk-item]")).find((el) => el.textContent.includes("Footloose Production"));
        expect(companyOption).toBeTruthy();
        fireEvent.click(companyOption);

        fireEvent.click(screen.getByTestId("marketing-add-submit-btn"));

        await waitFor(() => expect(globalThis.__mockAdminApi.post).toHaveBeenCalled());
        const [url, payload] = globalThis.__mockAdminApi.post.mock.calls[0];
        expect(url).toBe("/marketing/clients");
        expect(payload.name).toBe("New Test Contact");
        expect(payload.company_id).toBe("co-1");
        expect(payload).not.toHaveProperty("value");
        expect(payload).not.toHaveProperty("tags");
        expect(payload).toHaveProperty("designation");
        expect(payload).toHaveProperty("notes");
    });
});
