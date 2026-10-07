/* ---------------------------------------------------------------------
 * ProductionDesk — post-lock operational workspace for a project.
 *
 * Everything here is a VIEW over data that already lives elsewhere:
 *   - Locked talents come from Casting Pipeline (stage === "locked").
 *     This component never creates a talent or a project-talent link;
 *     it only reads/annotates the existing `casting_pipeline` row via
 *     `backend/routers/production_desk.py`.
 *   - Kickback recipients, crew members, and the production contact are
 *     all existing CRM contacts (`/marketing/clients`) — no separate
 *     contacts list here, ever.
 *   - Documents (call sheet, agreement, invoice, GST/TDS, reimbursement
 *     bills, ...) are pushed onto the project's existing `materials[]`
 *     via the same upload endpoint the Project Details tab's Material
 *     Modal already uses.
 *   - No AI. No separate CRM. No separate finance system. This is a
 *     consolidated operational read/write surface over Casting Pipeline
 *     + CRM + the project's own fields, matching what the backend
 *     already computes and returns in one shot from
 *     GET /projects/{id}/production-desk.
 * ------------------------------------------------------------------- */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { TalentPreviewDrawer, useMediaQuery } from "@/components/pipeline/TalentBrowserModal";
import { talentPreviewCache } from "@/lib/talentPreviewCache";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import {
    Command, CommandInput, CommandList, CommandEmpty, CommandGroup, CommandItem,
} from "@/components/ui/command";
import {
    Select, SelectTrigger, SelectValue, SelectContent, SelectItem,
} from "@/components/ui/select";
import {
    Table, TableHeader, TableRow, TableHead, TableBody, TableCell,
} from "@/components/ui/table";
import {
    Loader2, AlertTriangle, Users, IndianRupee, CalendarDays, FileText,
    Plus, Trash2, Upload, ChevronsUpDown, ExternalLink, Receipt,
    ClipboardList, Wallet, UserPlus, Sun, CalendarClock, ListChecks,
    CheckCircle2, Circle, PhoneCall, MessageCircle, MapPin,
    Layers, X, Pencil, ChevronDown, ChevronUp, Phone, Mail,
    Building2, History,
} from "lucide-react";

// Same INR formatter MarketingHub already uses — no second money formatter.
const formatCurrency = (val) => {
    if (val === undefined || val === null || val === "") return "—";
    try {
        return new Intl.NumberFormat("en-IN", {
            style: "currency",
            currency: "INR",
            maximumFractionDigits: 0,
        }).format(val);
    } catch {
        return `₹${val}`;
    }
};

// due_at / pd_*_at fields are stored as full ISO datetimes (matching
// core._now()'s own shape); a plain <input type="date"> only round-trips
// the date part, so these convert at the UI boundary — noon UTC is the
// same default time the Management Agent's own date parsing uses.
// Business timezone: Asia/Kolkata (IST). Every date/time this screen shows or
// reads is the IST calendar value — never the browser's own timezone, so a
// viewer in another country (or a laptop clock set wrong) sees the same day
// the backend and the reminder worker use. Date-only fields are stored at
// noon UTC (= 17:30 IST, the same calendar date in both zones), so they can
// never slide across a day boundary in either direction.
const IST_TZ = "Asia/Kolkata";
const istDateParts = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: IST_TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);

const toDateInputValue = (iso) => {
    if (!iso) return "";
    try {
        if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
        return istDateParts(new Date(iso));
    } catch { return ""; }
};
const fromDateInputValue = (dateStr) => (dateStr ? `${dateStr}T12:00:00.000Z` : null);

const formatDate = (iso) => {
    if (!iso) return "—";
    try {
        // A bare YYYY-MM-DD (shoot-day dates) is a calendar date, not an instant.
        const d = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(`${iso}T12:00:00.000Z`) : new Date(iso);
        return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: IST_TZ });
    } catch {
        return iso;
    }
};

// Call / reporting times are stored as "HH:MM" (24h, IST wall-clock) and shown
// as "9:30 AM". Legacy free-text values ("9 AM", "morning") pass through as-is.
const formatClock = (v) => {
    if (!v) return "—";
    const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(String(v).trim());
    if (!m) return v;
    const h = Number(m[1]);
    return `${h % 12 || 12}:${m[2]} ${h >= 12 ? "PM" : "AM"}`;
};
// Input value for <input type="time">: only a canonical HH:MM round-trips; legacy text is shown read-only.
const clockInputValue = (v) => (v && /^([01]\d|2[0-3]):[0-5]\d$/.test(String(v).trim()) ? String(v).trim() : "");
const clockMinutes = (v) => {
    const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(String(v || "").trim());
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const reportingAfterCall = (reporting, call) => {
    const r = clockMinutes(reporting); const c = clockMinutes(call);
    return r !== null && c !== null && r > c;
};

const DOCUMENT_CATEGORIES = [
    { value: "client_confirmation", label: "Client Confirmation" },
    { value: "po", label: "Purchase Order" },
    { value: "agreement", label: "Agreement" },
    { value: "invoice", label: "Invoice" },
    { value: "call_sheet", label: "Call Sheet" },
    { value: "payment_proof", label: "Payment Proof" },
    { value: "gst_tds_document", label: "GST / TDS Document" },
];
const DOC_LABEL = Object.fromEntries(DOCUMENT_CATEGORIES.map((c) => [c.value, c.label]));

// Fallback ONLY (2026-09-27) — the real source is CRM's own admin-managed
// Contact Type lookup list (crm_contact_types via GET /marketing/contact-
// types, the same source Marketing Hub's own pickers use), fetched into
// `crewRoles` state below. This hardcoded list is used only if that fetch
// fails, so the Add Crew Member form never has zero role options.
const CREW_ROLES = [
    "Director", "Producer", "DOP", "Photographer", "Stylist", "Makeup",
    "Hair", "Production Manager", "Line Producer", "Client", "Casting",
    "Editor", "Other",
];

// Post-lock operational stage — purely informational (see backend
// production_desk.py's PRODUCTION_STATUS_OPTIONS docstring); does not
// replace or gate the project's own status field shown on Project Details.
const PRODUCTION_STATUS_OPTIONS = [
    { value: "not_started", label: "Not Started" },
    { value: "confirmed", label: "Confirmed" },
    { value: "shoot_scheduled", label: "Shoot Scheduled" },
    { value: "shoot_complete", label: "Shoot Complete" },
    { value: "finance_closed", label: "Finance Closed" },
];

// Matches backend production_desk.py's TRIAL_STATUS_OPTIONS /
// SHOOT_STATUS_OPTIONS / PAYMENT_FOLLOWUP_STATUSES exactly — a manually-
// set status, not computed from a parsed date (see that module's
// SHOOT_STATUS_OPTIONS docstring for why).
const TRIAL_STATUS_OPTIONS = ["not_scheduled", "scheduled", "completed"];
const SHOOT_STATUS_OPTIONS = ["not_scheduled", "scheduled", "today", "completed", "cancelled"];
const PAYMENT_FOLLOWUP_STATUSES = ["not_due", "due", "in_progress", "done"];

// V2 — Production Desk talent-level shooting/overtime/reimbursements.
// Matches backend production_desk.py's AGREEMENT_STATUS_OPTIONS /
// TRANCHE_INVOICE_STATUSES / TRANCHE_PAYMENT_STATUSES exactly.
const AGREEMENT_STATUS_OPTIONS = [
    { value: "done", label: "Done" },
    { value: "pending", label: "Pending" },
    { value: "n_a", label: "N/A" },
];
const TRANCHE_INVOICE_STATUSES = [
    { value: "pending", label: "Pending" },
    { value: "raised", label: "Raised" },
    { value: "raised_and_sent", label: "Raised & Sent" },
];
const TRANCHE_PAYMENT_STATUSES = [
    { value: "pending", label: "Pending" },
    { value: "received", label: "Received" },
];

// Opens the SAME wa.me deep-link pattern MarketingHub.jsx's own
// handleShare() already uses for one-off admin-triggered WhatsApp
// messages — the admin still taps Send inside WhatsApp themselves, so
// this never auto-sends anything (spec: "do not create a new WhatsApp
// sender/worker").
function openWhatsApp(phone, message) {
    const digits = (phone || "").replace(/[^0-9]/g, "");
    if (!digits) {
        toast.error("No phone number on file");
        return;
    }
    window.open(`https://wa.me/${digits}?text=${encodeURIComponent(message)}`, "_blank");
}

// Lightest-possible "clickable location" (spec section 7/29) — a name plus
// an optional Google Maps URL, never a maps-search integration.
function LocationLink({ name, mapUrl, address }) {
    if (!name && !mapUrl) return <span className="text-black/30">—</span>;
    const addr = address && address !== name ? address : null;
    if (mapUrl) {
        return (
            <a href={mapUrl} target="_blank" rel="noreferrer" className="inline-flex flex-col text-[#0c2340] hover:underline min-w-0" title={addr || undefined}>
                <span className="inline-flex items-center gap-1 min-w-0">
                    <MapPin className="h-3 w-3 shrink-0" />
                    <span className="truncate">{name || "View on map"}</span>
                </span>
                {addr && <span className="text-[10px] text-black/40 truncate pl-4">{addr}</span>}
            </a>
        );
    }
    return (
        <span className="inline-flex flex-col min-w-0">
            <span className="text-black/70 truncate">{name}</span>
            {addr && <span className="text-[10px] text-black/40 truncate">{addr}</span>}
        </span>
    );
}

// {projectId, enabled} for the server-side Google Maps place search (the Maps key
// never reaches the browser — see production_desk.py's /places/* endpoints).
// `enabled` mirrors the desk's `capabilities.places_search`, so an environment with no
// key never issues (or logs) a failing search request.
const PlacesContext = React.createContext(null);

// Location search/select. Two sources, one list:
//   1. Google Maps places (server-side proxy; needs GOOGLE_MAPS_API_KEY on the
//      backend) — picking one saves the REAL place: name, formatted address,
//      place id, latitude/longitude and the Maps URL.
//   2. Locations the studio has already used (known-locations) + free text — the
//      original behaviour, and the automatic fallback whenever Google search is
//      not configured or unavailable (the 503/502 is swallowed, never shown).
// onCommit(name, mapUrl, place) — `place` is only set for a Google selection.
function LocationPicker({ value, onCommit, knownLocations, placeholder, className, immediate }) {
    const places = React.useContext(PlacesContext);
    const projectId = places?.projectId;
    const placesEnabled = !!places?.enabled;
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState(value || "");
    const [googleResults, setGoogleResults] = useState([]);
    const googleOff = React.useRef(false);
    const sessionRef = React.useRef(null);
    if (!sessionRef.current) {
        sessionRef.current = (typeof crypto !== "undefined" && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()).slice(2);
    }

    useEffect(() => { setQuery(value || ""); }, [value]);

    useEffect(() => {
        const q = query.trim();
        if (!projectId || !placesEnabled || googleOff.current || q.length < 3 || !open) { setGoogleResults([]); return undefined; }
        let cancelled = false;
        const t = setTimeout(async () => {
            try {
                const { data } = await adminApi.get(`/projects/${projectId}/production-desk/places/search`, { params: { q, session: sessionRef.current } });
                if (!cancelled) setGoogleResults(Array.isArray(data?.results) ? data.results : []);
            } catch (err) {
                // 503 = not configured, 502 = Google unavailable: fall back to known locations quietly.
                if (!cancelled) { setGoogleResults([]); if (err?.response?.status === 503) googleOff.current = true; }
            }
        }, 300);
        return () => { cancelled = true; clearTimeout(t); };
    }, [query, open, projectId, placesEnabled]);

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        const list = knownLocations || [];
        if (!q) return list.slice(0, 15);
        return list.filter((l) => l.name.toLowerCase().includes(q)).slice(0, 15);
    }, [knownLocations, query]);

    const commit = (name, mapUrl, place) => {
        setQuery(name);
        setOpen(false);
        onCommit(name, mapUrl, place);
    };

    const pickGoogle = async (r) => {
        try {
            const { data } = await adminApi.get(`/projects/${projectId}/production-desk/places/${encodeURIComponent(r.place_id)}`, { params: { session: sessionRef.current } });
            sessionRef.current = String(Math.random()).slice(2);   // a Places session ends at the details call
            commit(data.name || r.name, data.maps_url || undefined, {
                address: data.address || r.address || null, place_id: data.place_id || r.place_id,
                lat: data.lat ?? null, lng: data.lng ?? null,
            });
        } catch {
            // Details failed — keep what the user picked as plain text rather than losing it.
            commit(r.name);
        }
    };

    const hasAny = filtered.length > 0 || googleResults.length > 0;
    return (
        <Popover open={open} onOpenChange={setOpen}>
            <PopoverTrigger asChild>
                <Input
                    type="text"
                    value={query}
                    placeholder={placeholder || "Search or type a location"}
                    className={className || "h-7 text-xs"}
                    onChange={(e) => {
                        setQuery(e.target.value);
                        setOpen(true);
                        // Add-form usage (no blur before "Save" is clicked) needs
                        // every keystroke synced to the parent's form state —
                        // detail-row usage stays blur/select-only to avoid a
                        // network PATCH per keystroke (matches every other text
                        // field in this file).
                        if (immediate) onCommit(e.target.value);
                    }}
                    onFocus={() => setOpen(true)}
                    onBlur={() => {
                        setTimeout(() => {
                            setOpen(false);
                            if (!immediate && query !== (value || "")) onCommit(query);
                        }, 150);
                    }}
                />
            </PopoverTrigger>
            {hasAny && (
                <PopoverContent className="w-72 p-0" align="start" onOpenAutoFocus={(e) => e.preventDefault()}>
                    <Command shouldFilter={false}>
                        <CommandList>
                            {googleResults.length > 0 && (
                                <CommandGroup heading="Google Maps">
                                    {googleResults.map((r) => (
                                        <CommandItem key={r.place_id} value={`g-${r.place_id}`} onSelect={() => pickGoogle(r)} className="text-xs cursor-pointer" data-testid="pd-place-result">
                                            <MapPin className="h-3 w-3 mr-1.5 text-[#0c2340] shrink-0" />
                                            <span className="min-w-0">
                                                <span className="block truncate">{r.name}</span>
                                                {r.address && <span className="block truncate text-[10px] text-black/40">{r.address}</span>}
                                            </span>
                                        </CommandItem>
                                    ))}
                                </CommandGroup>
                            )}
                            {filtered.length > 0 && (
                                <CommandGroup heading="Previously used">
                                    {filtered.map((l) => (
                                        <CommandItem
                                            key={l.name}
                                            value={l.name}
                                            onSelect={() => commit(l.name, l.map_url || undefined)}
                                            className="text-xs cursor-pointer"
                                        >
                                            <MapPin className="h-3 w-3 mr-1.5 text-black/30 shrink-0" />
                                            <span className="truncate">{l.name}</span>
                                        </CommandItem>
                                    ))}
                                </CommandGroup>
                            )}
                        </CommandList>
                    </Command>
                </PopoverContent>
            )}
        </Popover>
    );
}

// V2 polish (spec section 7-11) — the consistent Edit/Save/Cancel/Delete
// control cluster used by every editable record (costume trial, shoot day,
// reading/rehearsal, tranche) and editable section (Payment Follow-up).
// Normal browsing stays fully read-only; only this button ever flips a
// record into a mutable state. The Management Agent is unaffected — it
// still PATCHes the same endpoints directly, never through this UI state.
function EditControls({ editing, onEdit, onSave, onCancel, onDelete, saveDisabled, size = "sm", compact = false }) {
    const h = size === "sm" ? "h-6 text-[10px] px-2" : "h-7 text-xs";
    // Deleting a record needs a confirmation step (a stray tap must never
    // silently remove a shoot day / reading / tranche / crew member).
    const confirmDelete = () => { if (window.confirm("Delete this? This can't be undone.")) onDelete(); };
    if (!editing) {
        if (compact) {
            // Dense table rows (e.g. Shooting Schedule) — icon-only with a
            // tooltip/aria-label instead of the text button, so Actions
            // never needs more width than Date/Location/etc. and can't wrap.
            return (
                <div className="flex items-center gap-2 shrink-0">
                    <button onClick={onEdit} className="text-black/40 hover:text-[#0c2340]" title="Edit" aria-label="Edit">
                        <Pencil className="h-3.5 w-3.5" />
                    </button>
                    {onDelete && (
                        <button onClick={confirmDelete} className="text-black/40 hover:text-red-500" title="Delete" aria-label="Delete">
                            <Trash2 className="h-3.5 w-3.5" />
                        </button>
                    )}
                </div>
            );
        }
        return (
            <div className="flex items-center gap-1.5 shrink-0">
                <Button size="sm" variant="ghost" className={h} onClick={onEdit}>
                    <Pencil className="h-3 w-3 mr-1" /> Edit
                </Button>
                {onDelete && (
                    <button onClick={confirmDelete} className="text-black/30 hover:text-red-500" title="Delete" aria-label="Delete">
                        <Trash2 className="h-3 w-3" />
                    </button>
                )}
            </div>
        );
    }
    return (
        <div className="flex items-center gap-1.5 shrink-0">
            <Button size="sm" variant="ghost" className={h} onClick={onCancel}>Cancel</Button>
            <Button size="sm" className={h} onClick={onSave} disabled={saveDisabled}>Save</Button>
        </div>
    );
}

function SectionCard({ title, icon: Icon, right, children, testId }) {
    return (
        <Card className="border-black/[0.08] shadow-none" data-testid={testId}>
            {/* UI audit fix — a title + a multi-part `right` (e.g. a status
                label next to a button) used to compete for one row's width
                and wrap/crowd on mobile ("Commission & Kickbacks" title
                wrapping into 2 lines with "Zoho — Not Connected" and "Add
                Kickback" squeezed into what was left). Stacking below sm:
                gives each its own full-width line; sm:+ keeps the original
                single-row layout unchanged. */}
            <CardHeader className="flex flex-col lg:flex-row lg:items-center justify-between gap-2 space-y-0 py-3.5 px-4 border-b border-black/[0.06]">
                <CardTitle className="text-[13px] font-semibold text-black/80 flex items-center gap-2">
                    {Icon && <Icon className="h-3.5 w-3.5 text-black/40 shrink-0" />}
                    {title}
                </CardTitle>
                {right}
            </CardHeader>
            <CardContent className="p-4">{children}</CardContent>
        </Card>
    );
}

function StatPill({ label, value, tone }) {
    const toneClass = {
        neutral: "text-black/70",
        warn: "text-amber-700",
        good: "text-emerald-700",
    }[tone || "neutral"];
    return (
        <div className="flex flex-col gap-0.5 min-w-[110px]">
            <span className="text-[11px] uppercase tracking-wide text-black/40">{label}</span>
            <span className={`text-[15px] font-semibold ${toneClass}`}>{value}</span>
        </div>
    );
}

// V2 polish (spec sections 26-35) — Production Overview as a collapsible
// smart dashboard. Everything here is DERIVED from data the backend already
// computes and returns from GET .../production-desk (summary,
// needs_attention, today, upcoming, completed) — no second data source, no
// second reminder engine. Collapse state is a per-viewer convenience
// (localStorage), never sent to the server.
// ============================================================================
// Production Financials — the ONE place the two sides of the money are laid out.
//
//   Production / Client   what the production is quoted and owes (quote + overtime
//                         + reimbursements − received)
//   Talent (talent-facing) what each talent was told and is paid (agreed rate,
//                         commission on THAT rate, net payable)
//   Talentgram (internal)  commission + the spread between quote and talent rate
//
// Talent rate ≠ production quote. Commission is always % × talent rate (+ overtime), never
// on the quote; the spread (quote − rate) is Talentgram's own margin and exists only on
// internal surfaces — it is never part of an invoice or any message to a talent. Every number
// here comes from the server (`_financial_rollup` / `_talent_card`); nothing is re-derived.
// ============================================================================
const BASIS_NOTE = {
    per_talent: "Production quote = the sum of each locked talent's quote.",
    partial: "A production quote is entered for only some talents — production totals are incomplete.",
    project_budget: "No per-talent quotes yet — using the project-level production budget below.",
    none: "No production quote entered yet. Enter one per talent in Locked Talents.",
};

function FinLine({ label, value, tone, strong, testId }) {
    const toneCls = { warn: "text-amber-700", good: "text-emerald-700", bad: "text-red-600" }[tone] || "text-black/80";
    return (
        <div className={`flex items-baseline justify-between gap-3 text-xs ${strong ? "pt-1.5 mt-1 border-t border-black/[0.06]" : ""}`}>
            <span className={strong ? "font-semibold text-black/70" : "text-black/50"}>{label}</span>
            <span className={`${strong ? "font-bold" : "font-medium"} ${toneCls}`} data-testid={testId}>{value}</span>
        </div>
    );
}

function FinGroup({ title, tag, tagClass, children, testId }) {
    return (
        <div className="rounded-lg border border-black/[0.08] p-3" data-testid={testId}>
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-black/40">{title}</span>
                <span className={`text-[9px] font-semibold uppercase tracking-wide rounded px-1.5 py-0.5 border ${tagClass}`}>{tag}</span>
            </div>
            <div className="space-y-1">{children}</div>
        </div>
    );
}

const moneyOrDash = (v) => (v === null || v === undefined ? "—" : formatCurrency(v));

function ProductionFinancials({ project: p, talents, summary: s, onSaveProject }) {
    const [showLegacy, setShowLegacy] = useState(false);
    const billable = s.production_billable_total;
    const quoteBase = billable === null || billable === undefined ? null : billable - (s.production_overtime_total || 0) - (s.production_reimbursements_total || 0);
    const incomplete = s.production_basis === "partial";
    const missing = s.production_quotes_missing || 0;

    return (
        <SectionCard title="Production Financials" icon={Wallet} testId="pd-production-financials">
            <div className="space-y-4" data-testid="pd-production-budget">
                <p className="text-[11px] text-black/40 -mt-1" data-testid="pd-fin-basis-note">{BASIS_NOTE[s.production_basis] || ""}
                    {missing > 0 && s.production_basis !== "none" && s.production_basis !== "project_budget" && ` (${missing} talent${missing !== 1 ? "s" : ""} without a quote)`}
                </p>

                <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                    <FinGroup title="Production / Client" tag="Client" tagClass="bg-sky-50 text-sky-700 border-sky-200" testId="pd-fin-client">
                        <FinLine label="Production Quote" value={moneyOrDash(quoteBase)} testId="pd-fin-quote" />
                        <FinLine label="Overtime" value={formatCurrency(s.production_overtime_total || 0)} />
                        <FinLine label="Reimbursements" value={formatCurrency(s.production_reimbursements_total || 0)} />
                        <FinLine label="Total to bill production" value={moneyOrDash(billable)} strong testId="pd-fin-billable" />
                        <FinLine label="Received" value={formatCurrency(s.client_received_total || 0)} tone="good" />
                        <FinLine label="Outstanding" value={incomplete ? "Incomplete" : moneyOrDash(s.client_outstanding_total)} strong tone={s.client_outstanding_total > 0 ? "warn" : "good"} testId="pd-fin-outstanding" />
                        <div className="pt-2">
                            <Label className="text-[11px] text-black/40">Shooting Days</Label>
                            <InlineNumber value={p.pd_shooting_days} className="mt-1" onSave={(v) => onSaveProject({ shooting_days: v })} />
                        </div>
                    </FinGroup>

                    <FinGroup title="Talent" tag="Talent-facing" tagClass="bg-emerald-50 text-emerald-700 border-emerald-200" testId="pd-fin-talent">
                        <FinLine label="Locked talents" value={s.locked_count} />
                        <FinLine label="Talent agreed rates" value={formatCurrency(s.talent_agreed_total || 0)} testId="pd-fin-talent-rates" />
                        <FinLine label="Commission (on talent rate)" value={formatCurrency(s.commission_gross || 0)} />
                        <FinLine label="Overtime" value={formatCurrency(s.extra_hours_total || 0)} />
                        <FinLine label="Reimbursements" value={formatCurrency(s.reimbursements_total || 0)} />
                        <FinLine label="Total talent net payable" value={formatCurrency(s.talent_payable_total || 0)} strong testId="pd-fin-talent-payable" />
                    </FinGroup>

                    <FinGroup title="Talentgram" tag="Internal only" tagClass="bg-amber-50 text-amber-800 border-amber-200" testId="pd-fin-internal">
                        <FinLine label="Commission" value={formatCurrency(s.commission_gross || 0)} />
                        <FinLine label="Additional spread (quote − rate)" value={formatCurrency(s.spread_total || 0)} tone={s.spread_total < 0 ? "bad" : undefined} testId="pd-fin-spread" />
                        <FinLine label="Total Talentgram earnings" value={formatCurrency(s.talentgram_earnings_total || 0)} strong testId="pd-fin-earnings" />
                        {s.kickbacks_total > 0 && <FinLine label="After kickbacks" value={formatCurrency(s.talentgram_earnings_net_of_kickbacks || 0)} />}
                    </FinGroup>
                </div>

                {talents.length > 0 && (
                    <div data-testid="pd-fin-table">
                        <div className="hidden lg:block overflow-x-auto">
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead className="text-xs">Talent</TableHead>
                                        <TableHead className="text-xs">Talent Rate</TableHead>
                                        <TableHead className="text-xs">Production Quote</TableHead>
                                        <TableHead className="text-xs">Comm %</TableHead>
                                        <TableHead className="text-xs">Commission</TableHead>
                                        <TableHead className="text-xs">Spread</TableHead>
                                        <TableHead className="text-xs">OT</TableHead>
                                        <TableHead className="text-xs">Reimb.</TableHead>
                                        <TableHead className="text-xs">Talent Net</TableHead>
                                        <TableHead className="text-xs">TG Earning</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {talents.map((t) => (
                                        <TableRow key={t.talent_id} data-testid={`pd-fin-row-${t.talent_id}`}>
                                            <TableCell className="text-xs font-medium text-black/80">{t.name || "Untitled"}</TableCell>
                                            <TableCell className="text-xs">{moneyOrDash(t.talent_agreed_rate)}</TableCell>
                                            <TableCell className="text-xs">{t.production_quote === null || t.production_quote === undefined ? <span className="text-amber-700">Not entered</span> : formatCurrency(t.production_quote)}</TableCell>
                                            <TableCell className="text-xs">{t.commission_percent === null || t.commission_percent === undefined ? "—" : `${t.commission_percent}%`}</TableCell>
                                            <TableCell className="text-xs">{moneyOrDash(t.commission_amount)}</TableCell>
                                            <TableCell className={`text-xs ${t.spread < 0 ? "text-red-600" : ""}`}>{moneyOrDash(t.spread)}</TableCell>
                                            <TableCell className="text-xs">{formatCurrency(t.extra_hours_total || 0)}</TableCell>
                                            <TableCell className="text-xs">{formatCurrency(t.reimbursement_total || 0)}</TableCell>
                                            <TableCell className="text-xs">{moneyOrDash(t.talent_net_payable)}</TableCell>
                                            <TableCell className="text-xs font-semibold">{moneyOrDash(t.talentgram_earning)}</TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </div>
                        <div className="lg:hidden space-y-2">
                            {talents.map((t) => (
                                <div key={t.talent_id} className="rounded-md border border-black/[0.06] p-3" data-testid={`pd-fin-row-mobile-${t.talent_id}`}>
                                    <div className="text-xs font-medium text-black/80 mb-2">{t.name || "Untitled"}</div>
                                    <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
                                        <div><span className="text-black/40 block">Talent Rate</span>{moneyOrDash(t.talent_agreed_rate)}</div>
                                        <div><span className="text-black/40 block">Production Quote</span>{t.production_quote === null || t.production_quote === undefined ? <span className="text-amber-700">Not entered</span> : formatCurrency(t.production_quote)}</div>
                                        <div><span className="text-black/40 block">Commission</span>{moneyOrDash(t.commission_amount)}</div>
                                        <div><span className="text-black/40 block">Spread</span><span className={t.spread < 0 ? "text-red-600" : ""}>{moneyOrDash(t.spread)}</span></div>
                                        <div><span className="text-black/40 block">OT / Reimb.</span>{formatCurrency(t.extra_hours_total || 0)} / {formatCurrency(t.reimbursement_total || 0)}</div>
                                        <div><span className="text-black/40 block">Talent Net</span>{moneyOrDash(t.talent_net_payable)}</div>
                                        <div className="col-span-2 font-semibold"><span className="text-black/40 font-normal block">TG Earning</span>{moneyOrDash(t.talentgram_earning)}</div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                <div className="pt-1 border-t border-black/[0.06]">
                    <button className="text-[11px] text-black/40 hover:text-black/70 inline-flex items-center gap-1" onClick={() => setShowLegacy((v) => !v)} data-testid="pd-fin-legacy-toggle">
                        <History className="h-3 w-3" /> {showLegacy ? "Hide" : "Show"} project-level production budget
                    </button>
                    {showLegacy && (
                        <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-3" data-testid="pd-fin-legacy">
                            <p className="sm:col-span-2 text-[11px] text-black/35">
                                The single project-wide production budget from before per-talent quotes. It is used only while no talent has a quote; once quotes are entered they replace it.
                            </p>
                            <div>
                                <Label className="text-[11px] text-black/40">Budget / Day</Label>
                                <InlineNumber value={p.pd_production_budget_per_day} className="mt-1" onSave={(v) => onSaveProject({ production_budget_per_day: v })} />
                            </div>
                            <div>
                                <Label className="text-[11px] text-black/40">Total Budget</Label>
                                <InlineNumber value={p.pd_production_budget_total} className="mt-1" onSave={(v) => onSaveProject({ production_budget_total: v })} />
                            </div>
                        </div>
                    )}
                </div>

                {(p.client_budget_lines?.length > 0 || p.talent_budget_lines?.length > 0) && (
                    <div className="pt-2 border-t border-black/[0.06]" data-testid="pd-budget-reference">
                        <Label className="text-[11px] text-black/40">Budget Reference (from Project Details)</Label>
                        <div className="mt-1.5 space-y-1">
                            {p.client_budget_lines?.map((l, i) => (
                                <div key={`cb-${i}`} className="flex justify-between text-xs text-black/60">
                                    <span>{l.label || "Client Budget"}</span><span>{l.value}</span>
                                </div>
                            ))}
                            {p.talent_budget_lines?.map((l, i) => (
                                <div key={`tb-${i}`} className="flex justify-between text-xs text-black/60">
                                    <span>{l.label || "Talent Budget"}</span><span>{l.value}</span>
                                </div>
                            ))}
                        </div>
                    </div>
                )}
            </div>
        </SectionCard>
    );
}

function OverviewDashboard({ project: p, summary, needsAttention, today, upcoming, completed }) {
    const [collapsed, setCollapsed] = useState(() => {
        try { return localStorage.getItem("pd_overview_collapsed") === "1"; } catch { return false; }
    });
    const toggle = () => {
        setCollapsed((v) => {
            const next = !v;
            try { localStorage.setItem("pd_overview_collapsed", next ? "1" : "0"); } catch { /* per-viewer convenience only */ }
            return next;
        });
    };

    const todayItems = [];
    if (today.project_shoot_today) todayItems.push({ key: "proj-shoot", icon: "🎬", label: "Shoot is today" });
    today.shoot_days?.forEach((d) => todayItems.push({ key: `sd-${d.talent_id}-${d.date}`, icon: "🎬", label: `Shoot — ${d.talent_name}${d.location ? ` (${d.location})` : ""}` }));
    today.prep_events?.forEach((e) => todayItems.push({ key: `pe-${e.talent_id}-${e.date}`, icon: e.type === "rehearsal" ? "🎭" : "📖", label: `${e.type === "rehearsal" ? "Rehearsal" : "Reading"} — ${e.talent_name}` }));
    today.trials.forEach((c) => todayItems.push({ key: `trial-${c.talent_id}`, icon: "👗", label: `Costume trial — ${c.name}${c.costume_trial_location ? ` (${c.costume_trial_location})` : ""}` }));
    today.tasks.forEach((t) => todayItems.push({ key: `task-${t.id}`, icon: "✓", label: `${t.title}${t.talent_name ? ` (${t.talent_name})` : ""}` }));
    if (today.payment_followup_due) todayItems.push({ key: "pf", icon: "💰", label: "Payment follow-up due today", warn: true });

    const upcomingItems = [];
    upcoming.shoot_days?.forEach((d) => upcomingItems.push({ key: `sd-${d.talent_id}-${d.date}`, label: `${formatDate(d.date)} — ${d.talent_name} — Shoot${d.location ? ` — ${d.location}` : ""}` }));
    upcoming.prep_events?.forEach((e) => upcomingItems.push({ key: `pe-${e.talent_id}-${e.date}`, label: `${formatDate(e.date)} — ${e.talent_name} — ${e.type === "rehearsal" ? "Rehearsal" : "Reading"}${e.location ? ` — ${e.location}` : ""}` }));
    upcoming.trials.forEach((c) => upcomingItems.push({ key: `trial-${c.talent_id}`, label: `${formatDate(c.costume_trial_at)} — ${c.name} — Costume Trial` }));
    upcoming.tasks.forEach((t) => upcomingItems.push({ key: `task-${t.id}`, label: `${formatDate(t.due_at)} — ${t.title}${t.talent_name ? ` (${t.talent_name})` : ""}` }));
    if (upcoming.payment_followup) upcomingItems.push({ key: "pf", label: `${formatDate(p.pd_next_follow_up_at)} — Payment Follow-up` });

    const completedItems = [];
    completed?.reimbursements?.forEach((r) => completedItems.push({ key: `r-${r.id}`, label: `Reimbursement cleared — ${r.talent_name} — ${formatCurrency(r.amount)}` }));
    completed?.tranches?.forEach((t) => completedItems.push({ key: `t-${t.id}`, label: `Tranche received — ${t.name} — ${formatCurrency(t.amount)}` }));
    completed?.tasks?.forEach((t) => completedItems.push({ key: `task-${t.id}`, label: `Task completed — ${t.title}` }));

    const billable = summary.production_billable_total;
    const quoteBase = billable === null || billable === undefined ? null : billable - (summary.production_overtime_total || 0) - (summary.production_reimbursements_total || 0);
    const productionMetrics = [
        { label: "Production Quote", value: moneyOrDash(quoteBase) },
        { label: "Shoot Days", value: summary.shoot_days ?? "—" },
        { label: "Shoot Status", value: (p.pd_shoot_status || "not_scheduled").replace("_", " ") },
    ];
    const talentMetrics = [
        { label: "Locked Talents", value: summary.locked_count },
        { label: "Talent Agreed Rates", value: formatCurrency(summary.talent_agreed_total || 0) },
        { label: "Talent Net Payable", value: formatCurrency(summary.talent_payable_total || 0) },
    ];
    const earningsMetrics = [
        { label: "Commission", value: formatCurrency(summary.commission_gross || 0) },
        { label: "Additional Spread", value: formatCurrency(summary.spread_total || 0), tone: summary.spread_total < 0 ? "warn" : "neutral" },
        { label: "Total Earnings", value: formatCurrency(summary.talentgram_earnings_total || 0), tone: "good" },
        ...(summary.kickbacks_total > 0 ? [{ label: "After Kickbacks", value: formatCurrency(summary.talentgram_earnings_net_of_kickbacks || 0) }] : []),
    ];
    const paymentMetrics = [
        { label: "Client Total", value: moneyOrDash(billable) },
        { label: "Client Received", value: formatCurrency(summary.client_received_total || 0), tone: "good" },
        {
            label: "Client Outstanding",
            value: summary.production_basis === "partial" ? "Incomplete" : moneyOrDash(summary.client_outstanding_total),
            tone: summary.client_outstanding_total > 0 || summary.production_basis === "partial" ? "warn" : "good",
        },
        {
            label: "Client Payment",
            value: p.pd_payment_in_received ? "Received" : "Pending",
            tone: p.pd_payment_in_received ? "good" : "warn",
        },
        {
            label: "Talent Payment Out",
            value: `${summary.payments_cleared}/${summary.payments_total} Cleared`,
            tone: summary.payments_cleared === summary.payments_total && summary.payments_total > 0 ? "good" : "warn",
        },
        ...(summary.payments_pending_amount > 0
            ? [{ label: "Talent Pending Amount", value: formatCurrency(summary.payments_pending_amount), tone: "warn" }]
            : []),
    ];

    return (
        <Card className="border-black/[0.08] shadow-none" data-testid="pd-overview">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 py-3.5 px-4 border-b border-black/[0.06]">
                <div className="min-w-0">
                    <CardTitle className="text-[13px] font-semibold text-black/80 flex items-center gap-2">
                        <ClipboardList className="h-3.5 w-3.5 text-black/40" /> Production Desk
                    </CardTitle>
                    <div className="text-[11px] text-black/40 mt-0.5 flex flex-wrap items-center gap-x-2">
                        <span className="font-medium text-black/60 truncate">{p.brand_name}</span>
                        {p.status && <Badge variant="outline" className="text-[10px] capitalize">{p.status}</Badge>}
                        <Badge variant="outline" className="text-[10px] capitalize">{(p.pd_production_status || "not_started").replace("_", " ")}</Badge>
                        {p.production_house && <span className="truncate">· {p.production_house}</span>}
                    </div>
                </div>
                <button onClick={toggle} className="text-black/40 hover:text-black/70 p-1 shrink-0" data-testid="pd-overview-toggle">
                    {collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
                </button>
            </CardHeader>
            {!collapsed && (
                <CardContent className="p-4 space-y-4" data-testid="pd-overview-content">
                    {/* Needs Attention — the first thing an admin should see. */}
                    {needsAttention.length > 0 ? (
                        <div className="flex flex-col gap-1.5 rounded-lg bg-amber-50 border border-amber-200 px-3.5 py-3" data-testid="pd-needs-attention">
                            <div className="flex items-center gap-1.5 text-xs font-semibold text-amber-800">
                                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                                Needs Attention · {needsAttention.length}
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                                {needsAttention.map((item, i) => (
                                    <span key={i} className="inline-flex items-center rounded-md bg-white/70 border border-amber-200/80 px-2 py-1 text-[11px] text-amber-800">
                                        {item}
                                    </span>
                                ))}
                            </div>
                        </div>
                    ) : (
                        <div className="flex items-center gap-1.5 rounded-lg border border-black/[0.06] px-3.5 py-2.5 text-xs text-black/40" data-testid="pd-needs-attention-clear">
                            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600/70" />
                            All production checks are clear.
                        </div>
                    )}

                    {/* Key numbers — grouped by what decision they inform, not a
                        flat wall of equally-weighted metrics. */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
                        <MetricGroup title="Production" items={productionMetrics} />
                        <MetricGroup title="Talent" items={talentMetrics} />
                        <MetricGroup title="Talentgram" items={earningsMetrics} />
                        <MetricGroup title="Payments" items={paymentMetrics} />
                    </div>

                    {/* Schedule — one compact block; empty rows stay a single
                        muted line each instead of three large equal boxes. */}
                    <div className="rounded-lg border border-black/[0.06] divide-y divide-black/[0.05]">
                        <ScheduleRow testId="pd-today" icon={Sun} label="Today" items={todayItems} emptyText="Nothing scheduled" />
                        <ScheduleRow testId="pd-upcoming" icon={CalendarClock} label="Upcoming" items={upcomingItems} emptyText="Nothing upcoming" />
                        <ScheduleRow testId="pd-completed" icon={CheckCircle2} label="Completed" items={completedItems} emptyText="Nothing completed" />
                    </div>
                </CardContent>
            )}
        </Card>
    );
}

function MetricGroup({ title, items }) {
    const toneClass = {
        neutral: "text-black/80",
        warn: "text-amber-700",
        good: "text-emerald-700",
    };
    return (
        <div>
            <div className="text-[10px] font-semibold uppercase tracking-wide text-black/35 mb-1.5">{title}</div>
            <div className="space-y-1">
                {items.map((it) => (
                    <div key={it.label} className="flex items-baseline justify-between gap-3 text-xs">
                        <span className="text-black/45">{it.label}</span>
                        <span className={`font-semibold ${toneClass[it.tone || "neutral"]}`}>{it.value}</span>
                    </div>
                ))}
            </div>
        </div>
    );
}

// Compact schedule line — a label + inline items when there's content, a
// single muted line when there's nothing, never a large empty box.
function ScheduleRow({ testId, icon: Icon, label, items, emptyText }) {
    return (
        <div className="px-3.5 py-2.5" data-testid={testId}>
            <div className="flex items-baseline gap-2">
                <span className="text-[11px] font-medium text-black/50 uppercase tracking-wide flex items-center gap-1 shrink-0">
                    <Icon className="h-3 w-3" /> {label}
                </span>
                {items.length === 0 && <span className="text-xs text-black/35">{emptyText}</span>}
            </div>
            {items.length > 0 && (
                <div className="mt-1.5 space-y-1 text-xs">
                    {items.map((it) => (
                        <div key={it.key} className={it.warn ? "text-amber-700" : "text-black/75"}>
                            {it.icon ? `${it.icon} ` : ""}{it.label}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

// Inline number field that saves onBlur only when the value actually
// changed — avoids a PATCH storm while the admin is still typing.
function InlineNumber({ value, onSave, placeholder, prefix, className }) {
    const [local, setLocal] = useState(value ?? "");
    useEffect(() => setLocal(value ?? ""), [value]);
    return (
        <div className={`flex items-center gap-1 ${className || ""}`}>
            {prefix && <span className="text-black/30 text-xs">{prefix}</span>}
            <Input
                type="number"
                value={local}
                placeholder={placeholder}
                onChange={(e) => setLocal(e.target.value)}
                onBlur={() => {
                    const num = local === "" ? null : Number(local);
                    if (num !== (value ?? null)) onSave(num);
                }}
                className="h-7 text-xs px-1.5 border-black/10"
            />
        </div>
    );
}

// Searchable existing-CRM-contact picker with an inline "+ Add Contact"
// fallback that uses the SAME /marketing/clients creation endpoint the
// Marketing Hub uses — no second contacts table anywhere.
function ClientPicker({ clients, onPicked, onContactCreated, placeholder }) {
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState("");
    const [creating, setCreating] = useState(false);
    const [newName, setNewName] = useState("");
    const [newPhone, setNewPhone] = useState("");
    const [saving, setSaving] = useState(false);

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return clients.slice(0, 30);
        return clients.filter((c) => (c.name || "").toLowerCase().includes(q)
            || (c.phone_number || "").includes(q)
            || (c.company_name || "").toLowerCase().includes(q)).slice(0, 30);
    }, [clients, query]);

    const createContact = async () => {
        if (!newName.trim()) return;
        setSaving(true);
        try {
            const { data } = await adminApi.post("/marketing/clients", {
                name: newName.trim(),
                phone_number: newPhone.trim() || undefined,
            });
            onContactCreated?.(data);
            onPicked(data);
            setOpen(false);
            setCreating(false);
            setNewName("");
            setNewPhone("");
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not create contact");
        } finally {
            setSaving(false);
        }
    };

    return (
        <Popover open={open} onOpenChange={setOpen}>
            <PopoverTrigger asChild>
                <Button variant="outline" size="sm" className="h-8 justify-between text-xs font-normal w-full">
                    {placeholder || "Search CRM contacts…"}
                    <ChevronsUpDown className="h-3 w-3 opacity-40 ml-2" />
                </Button>
            </PopoverTrigger>
            <PopoverContent className="w-72 p-0" align="start">
                {!creating ? (
                    <Command shouldFilter={false}>
                        <CommandInput placeholder="Search contacts…" value={query} onValueChange={setQuery} className="text-xs" />
                        <CommandList>
                            <CommandEmpty className="py-4 text-center text-xs text-black/40">No contacts found.</CommandEmpty>
                            <CommandGroup>
                                {filtered.map((c) => (
                                    <CommandItem
                                        key={c.id || c._id}
                                        value={c.id || c._id}
                                        onSelect={() => { onPicked(c); setOpen(false); }}
                                        className="text-xs cursor-pointer"
                                    >
                                        <span className="font-medium">{c.name}</span>
                                        {c.phone_number && <span className="ml-2 text-black/40">{c.phone_number}</span>}
                                    </CommandItem>
                                ))}
                            </CommandGroup>
                        </CommandList>
                        <div className="border-t border-black/[0.06] p-1.5">
                            <Button variant="ghost" size="sm" className="w-full h-7 text-xs justify-start" onClick={() => setCreating(true)}>
                                <Plus className="h-3 w-3 mr-1.5" /> Add Contact
                            </Button>
                        </div>
                    </Command>
                ) : (
                    <div className="p-3 space-y-2">
                        <Input placeholder="Name" value={newName} onChange={(e) => setNewName(e.target.value)} className="h-8 text-xs" autoFocus />
                        <Input placeholder="Phone (optional)" value={newPhone} onChange={(e) => setNewPhone(e.target.value)} className="h-8 text-xs" />
                        <div className="flex gap-2 justify-end pt-1">
                            <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setCreating(false)}>Cancel</Button>
                            <Button size="sm" className="h-7 text-xs" disabled={!newName.trim() || saving} onClick={createContact}>
                                {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Save"}
                            </Button>
                        </div>
                    </div>
                )}
            </PopoverContent>
        </Popover>
    );
}

export default function ProductionDesk({ projectId, project }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [clients, setClients] = useState([]);
    const [crewRoles, setCrewRoles] = useState([]);
    const [knownLocations, setKnownLocations] = useState([]);
    const isMobile = useMediaQuery("(max-width: 767px)");
    const [quickViewTalent, setQuickViewTalent] = useState(null);
    const [kickbackDialog, setKickbackDialog] = useState(false);
    const [reimbursementDialog, setReimbursementDialog] = useState(false);
    const [crewDialog, setCrewDialog] = useState(false);
    const [uploadDialog, setUploadDialog] = useState(false);
    const [taskDialog, setTaskDialog] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data } = await adminApi.get(`/projects/${projectId}/production-desk`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Failed to load Production Desk");
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    useEffect(() => { load(); }, [load]);

    // CRM contacts, fetched once for both the Kickback and Crew pickers —
    // the existing /marketing/clients list endpoint (no server-side
    // search), filtered client-side, same convention Marketing Hub uses.
    useEffect(() => {
        adminApi.get("/marketing/clients").then(({ data }) => {
            setClients(Array.isArray(data) ? data : (data.items || []));
        }).catch(() => {});
    }, []);

    // Crew role options (2026-09-27) — the SAME admin-managed CRM Contact
    // Type lookup list used by Marketing Hub's own pickers
    // (crm_contact_types via GET /marketing/contact-types), so Crew role
    // selection here is never a second, hardcoded role list. Falls back to
    // the previous hardcoded CREW_ROLES only if this call fails, so the
    // form never renders with zero options.
    useEffect(() => {
        adminApi.get("/marketing/contact-types").then(({ data }) => {
            const labels = Array.isArray(data) ? data.map((t) => t.label).filter(Boolean) : [];
            setCrewRoles(labels.length > 0 ? labels : CREW_ROLES);
        }).catch(() => setCrewRoles(CREW_ROLES));
    }, []);

    // V2 — Known Locations (spec section 2): real, previously-used location
    // names + map URLs the studio has already typed in, across every
    // project/talent — see production_desk.py's known-locations endpoint
    // docstring for why this replaces a Maps/Places integration.
    useEffect(() => {
        adminApi.get(`/projects/${projectId}/production-desk/known-locations`).then(({ data }) => {
            setKnownLocations(Array.isArray(data.locations) ? data.locations : []);
        }).catch(() => {});
    }, [projectId]);

    const openQuickView = useCallback((card) => {
        const cached = talentPreviewCache.getTalent(card.talent_id);
        const initial = cached || {
            id: card.talent_id,
            name: card.name,
            image_url: card.image_url,
            instagram_handle: card.instagram_handle,
            phone: card.phone,
        };
        setQuickViewTalent(initial);
        if (Array.isArray(initial.media)) return;
        talentPreviewCache
            .hydrateTalent(card.talent_id, async () => {
                const { data } = await adminApi.get(`/talents/${card.talent_id}`);
                return data;
            })
            .then((full) => setQuickViewTalent((prev) => (prev && prev.id === card.talent_id ? full : prev)))
            .catch(() => {});
    }, []);

    const patchProject = useCallback(async (payload) => {
        try {
            const { data } = await adminApi.patch(`/projects/${projectId}/production-desk`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Update failed");
        }
    }, [projectId]);

    const patchTalent = useCallback(async (talentId, payload) => {
        try {
            const { data } = await adminApi.patch(`/projects/${projectId}/production-desk/talents/${talentId}`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Update failed");
        }
    }, [projectId]);

    const deleteKickback = useCallback(async (id) => {
        try {
            const { data } = await adminApi.delete(`/projects/${projectId}/production-desk/kickbacks/${id}`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not remove kickback");
        }
    }, [projectId]);

    const updateReimbursementStatus = useCallback(async (id, status) => {
        try {
            const { data } = await adminApi.patch(`/projects/${projectId}/production-desk/reimbursements/${id}`, { status });
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Update failed");
        }
    }, [projectId]);

    const deleteReimbursement = useCallback(async (id) => {
        try {
            const { data } = await adminApi.delete(`/projects/${projectId}/production-desk/reimbursements/${id}`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not remove reimbursement");
        }
    }, [projectId]);

    const deleteCrew = useCallback(async (id) => {
        try {
            const { data } = await adminApi.delete(`/projects/${projectId}/production-desk/crew/${id}`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not remove crew member");
        }
    }, [projectId]);

    // V2 — per-talent shoot-day schedule (spec sections 1-4).
    const addShootDay = useCallback(async (talentId, payload) => {
        try {
            const { data } = await adminApi.post(`/projects/${projectId}/production-desk/talents/${talentId}/shoot-days`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not add shoot day");
        }
    }, [projectId]);

    const updateShootDay = useCallback(async (talentId, dayId, payload) => {
        try {
            const { data } = await adminApi.patch(`/projects/${projectId}/production-desk/talents/${talentId}/shoot-days/${dayId}`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Update failed");
        }
    }, [projectId]);

    const deleteShootDay = useCallback(async (talentId, dayId) => {
        try {
            const { data } = await adminApi.delete(`/projects/${projectId}/production-desk/talents/${talentId}/shoot-days/${dayId}`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not remove shoot day");
        }
    }, [projectId]);

    // V2 — Readings & Rehearsals (spec section 8).
    const addReadingRehearsal = useCallback(async (talentId, payload) => {
        try {
            const { data } = await adminApi.post(`/projects/${projectId}/production-desk/talents/${talentId}/readings-rehearsals`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not add entry");
        }
    }, [projectId]);

    const updateReadingRehearsal = useCallback(async (talentId, entryId, payload) => {
        try {
            const { data } = await adminApi.patch(`/projects/${projectId}/production-desk/talents/${talentId}/readings-rehearsals/${entryId}`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Update failed");
        }
    }, [projectId]);

    const deleteReadingRehearsal = useCallback(async (talentId, entryId) => {
        try {
            const { data } = await adminApi.delete(`/projects/${projectId}/production-desk/talents/${talentId}/readings-rehearsals/${entryId}`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not remove entry");
        }
    }, [projectId]);

    // V2 — Payment Tranches (spec sections 19-20).
    const addTranche = useCallback(async (payload) => {
        try {
            const { data } = await adminApi.post(`/projects/${projectId}/production-desk/tranches`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not add tranche");
        }
    }, [projectId]);

    const updateTranche = useCallback(async (id, payload) => {
        try {
            const { data } = await adminApi.patch(`/projects/${projectId}/production-desk/tranches/${id}`, payload);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Update failed");
        }
    }, [projectId]);

    const deleteTranche = useCallback(async (id) => {
        try {
            const { data } = await adminApi.delete(`/projects/${projectId}/production-desk/tranches/${id}`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not remove tranche");
        }
    }, [projectId]);

    // V2 — WhatsApp one-tap actions (spec sections 17/18/23-26). The
    // backend computes the exact message/amount server-side (single
    // source of truth for the financial formula); this just opens the
    // SAME wa.me deep link every other one-off admin WhatsApp send in
    // this codebase already uses — see openWhatsApp() above.
    // Payment follow-up: pick WHO it goes to (CRM contacts tied to this project — crew, the saved
    // concerned person, and same-company contacts), then REVIEW the exact message and recipient in
    // a dialog; only the dialog's confirm button opens WhatsApp (the admin still taps Send there).
    const [followupContacts, setFollowupContacts] = useState([]);
    const [followupContactId, setFollowupContactId] = useState("");
    const [followupPreview, setFollowupPreview] = useState(null);
    const crewCount = data?.crew?.length ?? 0;
    const defaultContactId = data?.project?.pd_production_contact?.client_id || "";
    useEffect(() => {
        let cancelled = false;
        adminApi.get(`/projects/${projectId}/production-desk/payment-followup-contacts`).then(({ data: d }) => {
            if (cancelled) return;
            const list = Array.isArray(d?.contacts) ? d.contacts : [];
            setFollowupContacts(list);
            setFollowupContactId((cur) => {
                if (cur && list.some((c) => c.client_id === cur && c.has_phone)) return cur;
                const pick = list.find((c) => c.is_default && c.has_phone) || list.find((c) => c.has_phone);
                return pick ? pick.client_id : "";
            });
        }).catch(() => { if (!cancelled) setFollowupContacts([]); });
        return () => { cancelled = true; };
    }, [projectId, crewCount, defaultContactId]);

    const sendPaymentFollowUp = useCallback(async () => {
        try {
            const { data: msg } = await adminApi.get(
                `/projects/${projectId}/production-desk/payment-followup-message`,
                followupContactId ? { params: { contact_id: followupContactId } } : undefined,
            );
            setFollowupPreview(msg);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not build follow-up message");
        }
    }, [projectId, followupContactId]);

    const confirmPaymentFollowUp = useCallback(async () => {
        const msg = followupPreview;
        if (!msg) return;
        setFollowupPreview(null);
        openWhatsApp(msg.phone, msg.message);
        await patchProject({ last_follow_up_at: new Date().toISOString() });
    }, [followupPreview, patchProject]);

    // V2 polish (spec sections 22-25) — destination_type is "group" when
    // this talent has a whatsapp_group_name on file (the SAME field the
    // existing campaign engine already reads), but a wa.me link can only
    // open an individual chat, never a WhatsApp group — so the phone
    // number is still what actually opens, and the admin is told so
    // rather than silently sending to the wrong place.
    // destination_type === "group": the talent's WhatsApp GROUP is the real
    // destination now (spec: "the destination must actually be the talent's
    // associated WhatsApp group, not merely display the group name"). A
    // link can't open a WhatsApp group, so this sends through the existing
    // WhatsApp Engine (same batch/job/worker pipeline every other real send
    // in this app uses — see production_desk.py's send_talent_invoice_to_group)
    // — never silently: the admin must explicitly confirm the exact message
    // first, matching the same confirm-before-send pattern the WhatsApp
    // Engine's own broadcast page already uses.
    // destination_type === "phone": unchanged — opens wa.me with the
    // message pre-filled, admin reviews and sends manually inside WhatsApp.
    const askTalentToRaiseInvoice = useCallback(async (talentId) => {
        try {
            const { data } = await adminApi.get(`/projects/${projectId}/production-desk/talents/${talentId}/invoice-message`);
            if (data.destination_type === "group") {
                const confirmed = window.confirm(
                    `Send this invoice request to ${data.talent_name}'s WhatsApp group "${data.whatsapp_group_name}"?\n\n${data.message}`,
                );
                if (!confirmed) return;
                try {
                    await adminApi.post(`/projects/${projectId}/production-desk/talents/${talentId}/invoice-message/send-to-group`);
                    toast.success(`Sent to ${data.whatsapp_group_name}`);
                } catch (sendErr) {
                    toast.error(formatErrorDetail(sendErr) || "Could not send to the WhatsApp group");
                }
                return;
            }
            openWhatsApp(data.phone, data.message);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not build invoice request");
        }
    }, [projectId]);

    // Tasks — the SAME db.workflow_tasks the admin Workflow page and the
    // Management Agent read/write (routers/workflow.py). Not a
    // Production-Desk-only task store.
    const createTask = useCallback(async (payload) => {
        try {
            await adminApi.post("/workflow/tasks", { ...payload, project_id: projectId, project_name: data?.project?.brand_name || "" });
            await load();
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not add task");
        }
    }, [projectId, load, data]);

    const updateTaskStatus = useCallback(async (taskId, status) => {
        try {
            await adminApi.put(`/workflow/tasks/${taskId}`, { status });
            await load();
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not update task");
        }
    }, [load]);

    if (loading) {
        return (
            <div className="flex items-center justify-center py-24 text-black/40">
                <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading Production Desk…
            </div>
        );
    }
    if (!data) {
        return <div className="py-24 text-center text-black/40 text-sm">Could not load Production Desk.</div>;
    }

    const { project: p, locked_talents: talents, summary, needs_attention, kickbacks, reimbursements, reimbursement_checklist_status, tranches, crew, documents, finance, today, upcoming, tasks } = data;

    return (
        <PlacesContext.Provider value={{ projectId, enabled: !!data.capabilities?.places_search }}>
        <div className="space-y-4 pb-16" data-testid="production-desk-root">
            <OverviewDashboard project={p} summary={summary} needsAttention={needs_attention} today={today} upcoming={upcoming} completed={data.completed} />

            {/* Locked Talents */}
            <SectionCard title={`Locked Talents (${talents.length})`} icon={Users} testId="pd-locked-talents">
                {talents.length === 0 ? (
                    <div className="text-xs text-black/40 py-4 text-center">
                        <div>No locked talents yet.</div>
                        <div className="mt-0.5">Move a talent to <strong className="text-black/50">Locked</strong> in Casting Pipeline to add them here.</div>
                    </div>
                ) : (
                    <>
                        {/* Desktop/tablet — the full table. UI audit fix: this
                            used to be the ONLY layout at every width, forcing
                            a horizontal scroll-within-a-card on mobile
                            (Part 12: "if a table cannot reasonably fit, use
                            the mobile card representation"). */}
                        <div className="hidden lg:block overflow-x-auto">
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead className="text-xs">Talent</TableHead>
                                        <TableHead className="text-xs">Budget / Day</TableHead>
                                        <TableHead className="text-xs">Shoot Days</TableHead>
                                        <TableHead className="text-xs">Talent Rate (Total)</TableHead>
                                        <TableHead className="text-xs">Production Quote</TableHead>
                                        <TableHead className="text-xs">Commission %</TableHead>
                                        <TableHead className="text-xs">Commission ₹</TableHead>
                                        <TableHead className="text-xs">Payment</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {talents.map((t) => (
                                        <TableRow key={t.talent_id} data-testid={`pd-talent-row-${t.talent_id}`}>
                                            <TableCell>
                                                <button
                                                    className="flex items-center gap-2 text-left hover:underline"
                                                    onClick={() => openQuickView(t)}
                                                >
                                                    {t.image_url ? (
                                                        <img src={t.image_url} alt="" className="h-7 w-7 rounded-full object-cover" />
                                                    ) : (
                                                        <div className="h-7 w-7 rounded-full bg-black/[0.06] flex items-center justify-center text-[10px] text-black/40">
                                                            {(t.name || "?")[0]}
                                                        </div>
                                                    )}
                                                    <span className="text-xs font-medium text-black/80">{t.name || "Untitled"}</span>
                                                </button>
                                            </TableCell>
                                            <TableCell>
                                                <InlineNumber value={t.budget_per_day} placeholder="—" className="min-w-[92px]" onSave={(v) => patchTalent(t.talent_id, { budget_per_day: v })} />
                                            </TableCell>
                                            <TableCell>
                                                <InlineNumber value={t.shooting_days} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { shooting_days: v })} />
                                            </TableCell>
                                            <TableCell>
                                                <InlineNumber value={t.budget_total} placeholder="—" className="min-w-[92px]" onSave={(v) => patchTalent(t.talent_id, { budget_total: v })} />
                                            </TableCell>
                                            <TableCell>
                                                <InlineNumber value={t.production_quote} placeholder="—" className="min-w-[92px]" onSave={(v) => patchTalent(t.talent_id, { production_quote: v })} />
                                            </TableCell>
                                            <TableCell>
                                                <InlineNumber value={t.commission_percent} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { commission_percent: v })} />
                                            </TableCell>
                                            <TableCell className="text-xs text-black/60">{formatCurrency(t.commission_amount)}</TableCell>
                                            <TableCell>
                                                <Select value={t.payment_status} onValueChange={(v) => patchTalent(t.talent_id, { payment_status: v })}>
                                                    <SelectTrigger className={`h-7 text-xs w-[110px] ${t.payment_status === "cleared" ? "text-emerald-700" : "text-amber-700"}`}>
                                                        <SelectValue />
                                                    </SelectTrigger>
                                                    <SelectContent>
                                                        <SelectItem value="pending">Pending</SelectItem>
                                                        <SelectItem value="cleared">Cleared</SelectItem>
                                                    </SelectContent>
                                                </Select>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </div>
                        {/* Mobile/tablet — stacked cards, same editable fields. */}
                        <div className="lg:hidden space-y-2">
                            {talents.map((t) => (
                                <div key={t.talent_id} className="rounded-md border border-black/[0.06] p-3" data-testid={`pd-talent-row-mobile-${t.talent_id}`}>
                                    <button className="flex items-center gap-2 text-left hover:underline mb-2" onClick={() => openQuickView(t)}>
                                        {t.image_url ? (
                                            <img src={t.image_url} alt="" className="h-7 w-7 rounded-full object-cover" />
                                        ) : (
                                            <div className="h-7 w-7 rounded-full bg-black/[0.06] flex items-center justify-center text-[10px] text-black/40">{(t.name || "?")[0]}</div>
                                        )}
                                        <span className="text-xs font-medium text-black/80">{t.name || "Untitled"}</span>
                                    </button>
                                    <div className="grid grid-cols-2 gap-x-3 gap-y-2 text-[11px]">
                                        <div><span className="text-black/40 block mb-0.5">Budget / Day</span><InlineNumber value={t.budget_per_day} placeholder="—" className="min-w-[92px]" onSave={(v) => patchTalent(t.talent_id, { budget_per_day: v })} /></div>
                                        <div><span className="text-black/40 block mb-0.5">Shoot Days</span><InlineNumber value={t.shooting_days} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { shooting_days: v })} /></div>
                                        <div><span className="text-black/40 block mb-0.5">Talent Rate (Total)</span><InlineNumber value={t.budget_total} placeholder="—" className="min-w-[92px]" onSave={(v) => patchTalent(t.talent_id, { budget_total: v })} /></div>
                                        <div><span className="text-black/40 block mb-0.5">Production Quote</span><InlineNumber value={t.production_quote} placeholder="—" className="min-w-[92px]" onSave={(v) => patchTalent(t.talent_id, { production_quote: v })} /></div>
                                        <div><span className="text-black/40 block mb-0.5">Commission %</span><InlineNumber value={t.commission_percent} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { commission_percent: v })} /></div>
                                        <div><span className="text-black/40 block mb-0.5">Commission ₹</span><span className="text-black/60">{formatCurrency(t.commission_amount)}</span></div>
                                        <div>
                                            <span className="text-black/40 block mb-0.5">Payment</span>
                                            <Select value={t.payment_status} onValueChange={(v) => patchTalent(t.talent_id, { payment_status: v })}>
                                                <SelectTrigger className={`h-7 text-xs w-full ${t.payment_status === "cleared" ? "text-emerald-700" : "text-amber-700"}`}>
                                                    <SelectValue />
                                                </SelectTrigger>
                                                <SelectContent>
                                                    <SelectItem value="pending">Pending</SelectItem>
                                                    <SelectItem value="cleared">Cleared</SelectItem>
                                                </SelectContent>
                                            </Select>
                                        </div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </>
                )}
            </SectionCard>

            {/* V2 polish (spec section 2) — Talent Preparation now holds
                BOTH Costume Trial and Readings & Rehearsals for each talent
                in one place, instead of two separate cards. Additive
                fields on the SAME locked casting_pipeline row Locked
                Talents above reads; no second talent/project relationship.
                Fitting/Look Test/Shoot Status stay fully alive server-side
                (Management Agent has real NLU commands against them — see
                production_desk.py's _talent_card docstring), simply not
                shown here (spec section 6); Shoot Status lives in Shoot
                Details below, next to the actual per-day schedule. */}
            {talents.length > 0 && (
                <SectionCard title="Talent Preparation" icon={ListChecks} testId="pd-talent-prep">
                    <div className="space-y-4">
                        {talents.map((t) => (
                            <div key={t.talent_id} className="rounded-lg border border-black/[0.08] p-3" data-testid={`pd-prep-${t.talent_id}`}>
                                <div className="text-xs font-semibold text-black/80 mb-2">{t.name}</div>
                                <CostumeTrialBlock talent={t} knownLocations={knownLocations} onSave={(payload) => patchTalent(t.talent_id, payload)} />
                                <div className="mt-3 pt-3 border-t border-black/[0.06]">
                                    <TalentReadingsRehearsals
                                        talent={t}
                                        knownLocations={knownLocations}
                                        onAdd={(payload) => addReadingRehearsal(t.talent_id, payload)}
                                        onUpdate={(entryId, payload) => updateReadingRehearsal(t.talent_id, entryId, payload)}
                                        onDelete={(entryId) => deleteReadingRehearsal(t.talent_id, entryId)}
                                    />
                                </div>
                            </div>
                        ))}
                    </div>
                </SectionCard>
            )}

            {/* V2 — Talent Financials (spec sections 21-25): the exact
                Fee / Extra Hours / Commissionable / Commission /
                Reimbursements / Invoice Amount breakdown, all computed
                server-side in _talent_card — never re-derived here. */}
            {talents.length > 0 && (
                <SectionCard title="Talent Financials" icon={IndianRupee} testId="pd-talent-financials">
                    <div className="space-y-2">
                        {talents.map((t) => (
                            <TalentFinancialCard key={t.talent_id} talent={t} onAskInvoice={() => askTalentToRaiseInvoice(t.talent_id)} />
                        ))}
                    </div>
                </SectionCard>
            )}

            {/* Tasks — the SAME db.workflow_tasks the Management Agent and
                the admin Workflow page read/write. */}
            <SectionCard
                title={`Tasks (${tasks.pending.length})`}
                icon={ListChecks}
                right={<Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setTaskDialog(true)}><Plus className="h-3 w-3 mr-1" /> Add Task</Button>}
                testId="pd-tasks"
            >
                {tasks.pending.length === 0 ? (
                    <div className="text-xs text-black/40 py-3 text-center">No open tasks</div>
                ) : (
                    <div className="space-y-1.5">
                        {tasks.pending.map((t) => {
                            const isOverdue = tasks.overdue.some((o) => o.id === t.id);
                            return (
                                <div key={t.id} className="flex items-center justify-between gap-2 text-xs rounded-md border border-black/[0.06] px-3 py-2" data-testid={`pd-task-${t.id}`}>
                                    <button className="flex items-center gap-2 text-left flex-1 min-w-0" onClick={() => updateTaskStatus(t.id, "completed")}>
                                        <Circle className="h-3.5 w-3.5 text-black/30 shrink-0" />
                                        <span className="truncate">
                                            <span className="font-medium text-black/80">{t.title}</span>
                                            {t.talent_name && <span className="text-black/40"> · {t.talent_name}</span>}
                                        </span>
                                    </button>
                                    <div className="flex items-center gap-2 shrink-0">
                                        {t.priority && <Badge variant="outline" className="text-[10px]">{t.priority}</Badge>}
                                        <span className={isOverdue ? "text-red-600 font-medium" : "text-black/40"}>{formatDate(t.due_at)}</span>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}
            </SectionCard>

            <ProductionFinancials project={p} talents={talents} summary={summary} onSaveProject={patchProject} />

            {/* V2 polish (spec section 3/4/6) — Shoot Details now holds
                BOTH the project-level shoot info AND every locked talent's
                own independent shooting schedule, instead of two separate
                cards. One primary structured date list (spec section 4);
                the free-text legacy `shoot_dates` and the reminder-only
                `pd_shoot_date` are kept (never deleted — the reminder
                worker genuinely reads pd_shoot_date/pd_call_time/
                pd_reporting_time/pd_shoot_location, see
                services/production_reminder_worker.py) but folded into one
                small labeled "Legacy / Reminder" sub-area instead of
                looking like a third separate shoot-date UI. */}
            <SectionCard title="Shoot Details" icon={CalendarDays} testId="pd-shoot-details">
                <ShootDetailsProjectBlock project={p} knownLocations={knownLocations} onSave={patchProject} />
                {talents.length > 0 && (
                    <div className="mt-4 pt-4 border-t border-black/[0.06]">
                        <div className="text-[11px] font-medium text-black/50 uppercase tracking-wide mb-2">Talent Shooting Schedule</div>
                        <div className="space-y-4">
                            {talents.map((t) => (
                                <TalentShootSchedule
                                    key={t.talent_id}
                                    talent={t}
                                    knownLocations={knownLocations}
                                    onAdd={(payload) => addShootDay(t.talent_id, payload)}
                                    onUpdate={(dayId, payload) => updateShootDay(t.talent_id, dayId, payload)}
                                    onDelete={(dayId) => deleteShootDay(t.talent_id, dayId)}
                                />
                            ))}
                        </div>
                    </div>
                )}
            </SectionCard>

            {/* Payment Follow-up — operational tracking ONLY, not a
                Finance/accounting record. pd_payment_in_received (in
                Project Checklist below) stays the one "has it actually
                arrived" boolean; this is the working notes a manager
                keeps while chasing it. */}
            <SectionCard
                title="Payment Follow-up"
                icon={PhoneCall}
                testId="pd-payment-followup"
                right={
                    <Button size="sm" variant="outline" className="h-7 text-xs" onClick={sendPaymentFollowUp} data-testid="pd-whatsapp-followup-btn" title="Opens WhatsApp with the message pre-filled — you review and send it yourself">
                        <MessageCircle className="h-3 w-3 mr-1" /> Open WhatsApp Follow-up
                    </Button>
                }
            >
                <p className="text-[11px] text-black/35 mb-3 -mt-1">
                    Opens WhatsApp with the message pre-filled. Nothing is sent automatically — you review and tap Send yourself.
                </p>
                <PaymentFollowUpBlock project={p} clients={clients} onContactCreated={(c) => setClients((prev) => [c, ...prev])} onSave={patchProject}
                    contacts={followupContacts} selectedContactId={followupContactId} onSelectContact={setFollowupContactId} />
                <Dialog open={!!followupPreview} onOpenChange={(o) => { if (!o) setFollowupPreview(null); }}>
                    <DialogContent className="max-w-lg" data-testid="pd-followup-dialog">
                        <DialogHeader><DialogTitle>Review payment follow-up</DialogTitle></DialogHeader>
                        {followupPreview && (
                            <div className="space-y-3 text-xs">
                                <div data-testid="pd-followup-recipient-summary">
                                    <span className="text-black/40">To: </span>
                                    <span className="font-medium text-black/80">{followupPreview.contact_name || "—"}</span>
                                    <span className="text-black/40"> · {followupPreview.phone}</span>
                                </div>
                                {(followupPreview.warnings || []).map((w) => (
                                    <div key={w} className="rounded-md bg-amber-50 border border-amber-200 px-2.5 py-1.5 text-amber-800" data-testid="pd-followup-warning">{w}</div>
                                ))}
                                <pre className="whitespace-pre-wrap rounded-md border border-black/[0.08] bg-slate-50 p-3 text-[11px] text-black/75 max-h-72 overflow-auto font-sans" data-testid="pd-followup-message">{followupPreview.message}</pre>
                                <p className="text-[11px] text-black/40">Nothing is sent from here — this opens WhatsApp with the text pre-filled and you tap Send yourself.</p>
                            </div>
                        )}
                        <DialogFooter>
                            <Button variant="ghost" size="sm" onClick={() => setFollowupPreview(null)}>Cancel</Button>
                            <Button size="sm" onClick={confirmPaymentFollowUp} data-testid="pd-followup-confirm"><MessageCircle className="h-3 w-3 mr-1" /> Open WhatsApp</Button>
                        </DialogFooter>
                    </DialogContent>
                </Dialog>
            </SectionCard>

            {/* V2 — Payment Tranches / Billing Milestones (spec sections
                19-20): an operational billing tracker for long-format
                projects, not accounting. */}
            <PaymentTranchesSection
                tranches={tranches}
                onAdd={addTranche}
                onUpdate={updateTranche}
                onDelete={deleteTranche}
                totals={{ total: summary.tranches_total, received: summary.tranches_received_total }}
            />

            {/* Commission & Kickbacks */}
            <SectionCard
                title="Commission & Kickbacks"
                icon={IndianRupee}
                right={
                    <div className="flex items-center gap-3">
                        {/* Honest, static state — Zoho Books integration does not
                            exist yet (see backend/routers/production_desk.py
                            module docstring). Never claims a sync happened. */}
                        <span className="text-[11px] text-black/35" data-testid="pd-zoho-status">
                            Zoho — {finance?.zoho_status === "not_connected" ? "Not Connected" : finance?.zoho_status}
                        </span>
                        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setKickbackDialog(true)}><Plus className="h-3 w-3 mr-1" /> Add Kickback</Button>
                    </div>
                }
                testId="pd-commission"
            >
                <div className="flex flex-wrap gap-x-8 gap-y-3 mb-3">
                    <StatPill label="Commission %" value={p.commission_percent || "—"} />
                    <StatPill label="Gross Commission" value={formatCurrency(summary.commission_gross)} />
                    <StatPill label="Total Kickbacks" value={formatCurrency(summary.kickbacks_total)} tone={summary.kickbacks_total > 0 ? "warn" : "neutral"} />
                    <StatPill label="Net Commission" value={formatCurrency(summary.commission_net)} tone="good" />
                </div>
                {kickbacks.length > 0 && (
                    <div className="space-y-1.5">
                        {kickbacks.map((k) => (
                            <div key={k.id} className="flex items-center justify-between text-xs border-t border-black/[0.05] pt-1.5" data-testid={`pd-kickback-${k.id}`}>
                                <div className="flex items-center gap-2">
                                    <span className="font-medium text-black/70">{formatCurrency(k.amount)}</span>
                                    <span className="text-black/40">→ {k.recipient?.name || k.recipient_name || "Unnamed"}</span>
                                    {k.notes && <span className="text-black/30">({k.notes})</span>}
                                </div>
                                <button onClick={() => deleteKickback(k.id)} className="text-black/30 hover:text-red-500">
                                    <Trash2 className="h-3 w-3" />
                                </button>
                            </div>
                        ))}
                    </div>
                )}
            </SectionCard>

            {/* Reimbursements */}
            <SectionCard
                title="Reimbursements"
                icon={Receipt}
                right={<Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setReimbursementDialog(true)} disabled={talents.length === 0}><Plus className="h-3 w-3 mr-1" /> Add Reimbursement</Button>}
                testId="pd-reimbursements"
            >
                {reimbursements.length === 0 ? (
                    <div className="text-xs text-black/40 py-4 text-center">No reimbursements recorded.</div>
                ) : (
                    <div className="space-y-1.5">
                        {reimbursements.map((r) => (
                            <div key={r.id} className="flex items-center justify-between text-xs border-t border-black/[0.05] pt-1.5" data-testid={`pd-reimbursement-${r.id}`}>
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className="font-medium text-black/70">{formatCurrency(r.amount)}</span>
                                    <span className="text-black/50">{r.expense_type}</span>
                                    <span className="text-black/40">— {r.talent_name}</span>
                                    {r.date && <span className="text-black/30">{r.date}</span>}
                                    {!r.material_id && <Badge variant="outline" className="text-[10px] text-amber-700 border-amber-300">No bill</Badge>}
                                </div>
                                <div className="flex items-center gap-2">
                                    <Select value={r.status} onValueChange={(v) => updateReimbursementStatus(r.id, v)}>
                                        <SelectTrigger className={`h-6 text-[11px] w-[90px] ${r.status === "paid" ? "text-emerald-700" : "text-amber-700"}`}>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="pending">Pending</SelectItem>
                                            <SelectItem value="paid">Paid</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <button onClick={() => deleteReimbursement(r.id)} className="text-black/30 hover:text-red-500">
                                        <Trash2 className="h-3 w-3" />
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </SectionCard>

            {/* Project Checklist — lifecycle order: Agreement -> Confirmation
                -> Invoice Raised & Sent -> Client Payment -> GST -> Talent
                Reimbursement Out -> Talent Payment Out. The last two rows
                are derived, read-only (same project_reimbursements/Locked
                Talents data already computed elsewhere), never a second
                record. V2 (spec sections 12-14): Invoice Raised + Invoice
                Sent are now ONE toggle (still writes both underlying
                fields — kept alive for Management Agent's existing
                invoice-status commands, see production_desk.py); Agreement
                Signed added as its own three-way status; "Talent Payments"
                renamed "Talent Payment Out" (terminology only). */}
            <SectionCard
                title="Project Checklist"
                icon={ClipboardList}
                testId="pd-checklist"
                right={
                    <Select value={p.pd_production_status || "not_started"} onValueChange={(v) => patchProject({ production_status: v })}>
                        <SelectTrigger className="h-7 text-xs w-[150px]" data-testid="pd-production-status">
                            <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                            {PRODUCTION_STATUS_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
                        </SelectContent>
                    </Select>
                }
            >
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div className="flex items-center justify-between gap-2 rounded-md border border-black/[0.06] px-3 py-2.5" data-testid="pd-checklist-agreement">
                        <span className="text-xs text-black/70">Agreement Signed</span>
                        <Select value={p.pd_agreement_status || "pending"} onValueChange={(v) => patchProject({ agreement_status: v })}>
                            <SelectTrigger className={`h-7 text-[11px] w-[90px] ${p.pd_agreement_status === "done" ? "text-emerald-700" : p.pd_agreement_status === "n_a" ? "text-black/40" : "text-amber-700"}`}>
                                <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                                {AGREEMENT_STATUS_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
                            </SelectContent>
                        </Select>
                    </div>
                    {[
                        { key: "confirmation_mail_received", label: "Confirmation Mail Received", val: p.pd_confirmation_mail_received },
                    ].map((item) => (
                        <div key={item.key} className="flex items-center justify-between gap-2 rounded-md border border-black/[0.06] px-3 py-2.5">
                            <span className="text-xs text-black/70">{item.label}</span>
                            <div className="flex items-center gap-1.5">
                                <span className={`text-[11px] font-medium ${item.val ? "text-emerald-700" : "text-amber-700"}`}>{item.val ? "Complete" : "Pending"}</span>
                                <Switch checked={!!item.val} onCheckedChange={(v) => patchProject({ [item.key]: v })} />
                            </div>
                        </div>
                    ))}
                    <div className="flex items-center justify-between gap-2 rounded-md border border-black/[0.06] px-3 py-2.5" data-testid="pd-checklist-invoice">
                        <span className="text-xs text-black/70">Invoice Raised &amp; Sent</span>
                        <div className="flex items-center gap-1.5">
                            <span className={`text-[11px] font-medium ${p.pd_invoice_raised_and_sent ? "text-emerald-700" : "text-amber-700"}`}>{p.pd_invoice_raised_and_sent ? "Complete" : "Pending"}</span>
                            <Switch checked={!!p.pd_invoice_raised_and_sent} onCheckedChange={(v) => patchProject({ invoice_raised_and_sent: v })} />
                        </div>
                    </div>
                    {[
                        { key: "payment_in_received", label: "Client Payment In", val: p.pd_payment_in_received },
                        { key: "gst_component_received", label: "GST Component In", val: p.pd_gst_component_received },
                    ].map((item) => (
                        <div key={item.key} className="flex items-center justify-between gap-2 rounded-md border border-black/[0.06] px-3 py-2.5">
                            <span className="text-xs text-black/70">{item.label}</span>
                            <div className="flex items-center gap-1.5">
                                <span className={`text-[11px] font-medium ${item.val ? "text-emerald-700" : "text-amber-700"}`}>{item.val ? "Complete" : "Pending"}</span>
                                <Switch checked={!!item.val} onCheckedChange={(v) => patchProject({ [item.key]: v })} />
                            </div>
                        </div>
                    ))}
                    {/* Derived, read-only (spec section 11) — never a false
                        "Pending" when the project genuinely has no
                        reimbursements. */}
                    <div className="flex items-center justify-between gap-2 rounded-md border border-black/[0.06] px-3 py-2.5" data-testid="pd-checklist-reimbursement-out">
                        <span className="text-xs text-black/70">Talent Reimbursement Out</span>
                        <span className={`text-[11px] font-medium ${reimbursement_checklist_status === "complete" ? "text-emerald-700" : reimbursement_checklist_status === "n_a" ? "text-black/40" : "text-amber-700"}`}>
                            {reimbursement_checklist_status === "complete" ? "Complete" : reimbursement_checklist_status === "n_a" ? "N/A" : "Pending"}
                        </span>
                    </div>
                    {/* Derived, read-only — same summary.payments_* Locked
                        Talents already computes; not a second toggle/record. */}
                    <div className="flex items-center justify-between gap-2 rounded-md border border-black/[0.06] px-3 py-2.5" data-testid="pd-checklist-talent-payments">
                        <span className="text-xs text-black/70">Talent Payment Out</span>
                        <span className={`text-[11px] font-medium ${summary.payments_cleared === summary.payments_total && summary.payments_total > 0 ? "text-emerald-700" : "text-amber-700"}`}>
                            {summary.payments_cleared} / {summary.payments_total} Cleared
                        </span>
                    </div>
                </div>
            </SectionCard>

            {/* V2 polish (spec sections 12-14) — Crew, redesigned as
                compact CRM-linked contact cards with a peek popover
                (Name/Role/Company/Phone/Email) and Call/WhatsApp/Email
                actions shown only where the data actually exists. Never
                duplicates the CRM record — every field comes from the
                SAME client-ref lookup this endpoint already returns. */}
            <SectionCard
                title={`Crew (${crew.length})`}
                icon={UserPlus}
                right={<Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setCrewDialog(true)}><Plus className="h-3 w-3 mr-1" /> Add from CRM</Button>}
                testId="pd-crew"
            >
                {crew.length === 0 ? (
                    <div className="text-xs text-black/40 py-4 text-center">No crew added yet.</div>
                ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                        {crew.map((c) => <CrewCard key={c.id} crew={c} onDelete={() => deleteCrew(c.id)} />)}
                    </div>
                )}
            </SectionCard>

            {/* "Project Requirements & Usage" removed from this workspace
                (spec section 27) — not useful for day-to-day production
                ops. The underlying fields (medium_usage, director,
                production_house, competitive_brand_enabled,
                additional_details) are untouched and still fully visible
                on the Project Details tab; nothing was deleted, only this
                read-only Production Desk display of them. */}

            {/* Documents */}
            <SectionCard
                title={`Documents (${documents.length})`}
                icon={FileText}
                right={<Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setUploadDialog(true)}><Upload className="h-3 w-3 mr-1" /> Upload</Button>}
                testId="pd-documents"
            >
                {documents.length === 0 ? (
                    <div className="text-xs text-black/40 py-4 text-center">No documents uploaded yet.</div>
                ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                        {documents.map((m) => (
                            <a
                                key={m.id}
                                href={m.url}
                                target="_blank"
                                rel="noreferrer"
                                className="flex items-center justify-between text-xs rounded-md border border-black/[0.06] px-3 py-2 hover:bg-black/[0.02]"
                                data-testid={`pd-document-${m.id}`}
                            >
                                <div className="min-w-0">
                                    <div className="font-medium text-black/75 truncate">{m.original_filename || DOC_LABEL[m.category] || m.category}</div>
                                    <div className="text-black/40">{DOC_LABEL[m.category] || m.category}</div>
                                </div>
                                <ExternalLink className="h-3 w-3 text-black/30 shrink-0 ml-2" />
                            </a>
                        ))}
                    </div>
                )}
            </SectionCard>

            {/* Add Kickback dialog */}
            <Dialog open={kickbackDialog} onOpenChange={setKickbackDialog}>
                <DialogContent className="max-w-sm">
                    <DialogHeader><DialogTitle className="text-sm">Add Kickback</DialogTitle></DialogHeader>
                    <KickbackForm
                        clients={clients}
                        onContactCreated={(c) => setClients((prev) => [c, ...prev])}
                        onSubmit={async ({ amount, recipient }) => {
                            try {
                                const { data } = await adminApi.post(`/projects/${projectId}/production-desk/kickbacks`, {
                                    amount,
                                    recipient_client_id: recipient?.id || recipient?._id,
                                    recipient_name: recipient?.name,
                                });
                                setData(data);
                                setKickbackDialog(false);
                            } catch (err) {
                                toast.error(formatErrorDetail(err) || "Could not add kickback");
                            }
                        }}
                    />
                </DialogContent>
            </Dialog>

            {/* Add Reimbursement dialog */}
            <Dialog open={reimbursementDialog} onOpenChange={setReimbursementDialog}>
                <DialogContent className="max-w-sm">
                    <DialogHeader><DialogTitle className="text-sm">Add Reimbursement</DialogTitle></DialogHeader>
                    <ReimbursementForm
                        talents={talents}
                        onSubmit={async (form) => {
                            try {
                                const fd = new FormData();
                                fd.append("talent_id", form.talentId);
                                fd.append("expense_type", form.expenseType);
                                fd.append("amount", form.amount);
                                if (form.date) fd.append("date", form.date);
                                if (form.notes) fd.append("notes", form.notes);
                                if (form.file) fd.append("file", form.file);
                                const { data } = await adminApi.post(`/projects/${projectId}/production-desk/reimbursements`, fd, {
                                    headers: { "Content-Type": "multipart/form-data" },
                                });
                                setData(data);
                                setReimbursementDialog(false);
                            } catch (err) {
                                toast.error(formatErrorDetail(err) || "Could not add reimbursement");
                            }
                        }}
                    />
                </DialogContent>
            </Dialog>

            {/* Add Crew dialog */}
            <Dialog open={crewDialog} onOpenChange={setCrewDialog}>
                <DialogContent className="max-w-sm">
                    <DialogHeader><DialogTitle className="text-sm">Add Crew Member</DialogTitle></DialogHeader>
                    <CrewForm
                        clients={clients}
                        roles={crewRoles}
                        onContactCreated={(c) => setClients((prev) => [c, ...prev])}
                        onSubmit={async ({ contact, role }) => {
                            try {
                                const { data } = await adminApi.post(`/projects/${projectId}/production-desk/crew`, {
                                    client_id: contact.id || contact._id,
                                    role,
                                });
                                setData(data);
                                setCrewDialog(false);
                            } catch (err) {
                                toast.error(formatErrorDetail(err) || "Could not add crew member");
                            }
                        }}
                    />
                </DialogContent>
            </Dialog>

            {/* Upload document dialog — reuses the EXISTING project material upload endpoint */}
            <Dialog open={uploadDialog} onOpenChange={setUploadDialog}>
                <DialogContent className="max-w-sm">
                    <DialogHeader><DialogTitle className="text-sm">Upload Document</DialogTitle></DialogHeader>
                    <UploadDocumentForm
                        onSubmit={async ({ category, file }) => {
                            try {
                                const fd = new FormData();
                                fd.append("category", category);
                                fd.append("file", file);
                                await adminApi.post(`/projects/${projectId}/material`, fd, {
                                    headers: { "Content-Type": "multipart/form-data" },
                                });
                                await load();
                                setUploadDialog(false);
                            } catch (err) {
                                toast.error(formatErrorDetail(err) || "Upload failed");
                            }
                        }}
                    />
                </DialogContent>
            </Dialog>

            {/* Add Task dialog — writes to the SAME db.workflow_tasks the
                Management Agent and the admin Workflow page use. */}
            <Dialog open={taskDialog} onOpenChange={setTaskDialog}>
                <DialogContent className="max-w-sm">
                    <DialogHeader><DialogTitle className="text-sm">Add Task</DialogTitle></DialogHeader>
                    <AddTaskForm
                        talents={talents}
                        onSubmit={async (payload) => {
                            await createTask(payload);
                            setTaskDialog(false);
                        }}
                    />
                </DialogContent>
            </Dialog>

            {quickViewTalent && (
                <TalentPreviewDrawer talent={quickViewTalent} onClose={() => setQuickViewTalent(null)} isMobile={isMobile} />
            )}
        </div>
        </PlacesContext.Provider>
    );
}

// ============================================================================
// V2 polish (spec section 3/6/7) — the project-level half of the merged
// Shoot Details card. Shoot DATES live only in each talent's own schedule
// (the project-level date list was removed as a duplicate); the project
// block keeps the project-wide defaults (call/reporting time, location,
// status, notes, and the legacy/reminder fields) behind section-level
// Edit/Save/Cancel so normal browsing can't accidentally change them.
// ============================================================================
// V2 polish (spec sections 12-14) — one crew member, as a compact
// CRM-linked card. Clicking the name opens a "peek" popover with the
// contact's own CRM fields (no second contact record — everything comes
// from the crew.contact object the backend already resolves). Action
// buttons only render when the underlying data actually exists.
function CrewCard({ crew: c, onDelete }) {
    const contact = c.contact || {};
    const phone = contact.phone_number;
    const digits = (phone || "").replace(/[^0-9]/g, "");

    return (
        <div className="rounded-md border border-black/[0.06] px-3 py-2" data-testid={`pd-crew-${c.id}`}>
            <div className="flex items-start justify-between gap-2">
                <Popover>
                    <PopoverTrigger asChild>
                        <button className="text-left min-w-0" data-testid={`pd-crew-peek-trigger-${c.id}`}>
                            <div className="text-xs font-medium text-black/75 hover:underline truncate">{contact.name || "Unnamed"}</div>
                            <div className="text-[11px] text-black/40">{c.role}</div>
                        </button>
                    </PopoverTrigger>
                    <PopoverContent className="w-64 text-xs" align="start" data-testid={`pd-crew-peek-${c.id}`}>
                        <div className="font-semibold text-black/80 text-sm mb-1">{contact.name || "Unnamed"}</div>
                        <div className="space-y-1 text-black/60">
                            <div>Role: {c.role}</div>
                            {contact.company_name && <div className="flex items-center gap-1"><Building2 className="h-3 w-3" /> {contact.company_name}</div>}
                            {phone && <div className="flex items-center gap-1"><Phone className="h-3 w-3" /> {phone}</div>}
                            {contact.email && <div className="flex items-center gap-1"><Mail className="h-3 w-3" /> {contact.email}</div>}
                            {contact.contact_type && <div>Type: {contact.contact_type}</div>}
                        </div>
                        <a href="/admin/marketing" className="mt-2 inline-flex items-center gap-1 text-[#0c2340] hover:underline text-[11px]">
                            <ExternalLink className="h-3 w-3" /> Open CRM
                        </a>
                    </PopoverContent>
                </Popover>
                <button onClick={onDelete} className="text-black/30 hover:text-red-500 shrink-0"><Trash2 className="h-3 w-3" /></button>
            </div>
            {(phone || contact.email) && (
                <div className="flex items-center gap-2 mt-2">
                    {phone && (
                        <a href={`tel:${digits}`} className="inline-flex items-center gap-1 text-[11px] text-black/60 hover:text-[#0c2340] border border-black/[0.08] rounded px-2 py-1" data-testid={`pd-crew-call-${c.id}`}>
                            <Phone className="h-3 w-3" /> Call
                        </a>
                    )}
                    {phone && (
                        <button
                            onClick={() => window.open(`https://wa.me/${digits}`, "_blank")}
                            className="inline-flex items-center gap-1 text-[11px] text-black/60 hover:text-[#0c2340] border border-black/[0.08] rounded px-2 py-1"
                            data-testid={`pd-crew-whatsapp-${c.id}`}
                        >
                            <MessageCircle className="h-3 w-3" /> WhatsApp
                        </button>
                    )}
                    {contact.email && (
                        <a href={`mailto:${contact.email}`} className="inline-flex items-center gap-1 text-[11px] text-black/60 hover:text-[#0c2340] border border-black/[0.08] rounded px-2 py-1" data-testid={`pd-crew-email-${c.id}`}>
                            <Mail className="h-3 w-3" /> Email
                        </a>
                    )}
                </div>
            )}
        </div>
    );
}

// V2 polish (spec section 9) — Payment Follow-up, section-level
// Edit/Save/Cancel exactly as illustrated: Payment Terms / Expected Date /
// Concerned Person / Status shown read-only, Edit flips the whole block to
// inputs, Cancel restores exactly what the server last returned.
function PaymentFollowUpBlock({ project: p, clients, onContactCreated, onSave, contacts = [], selectedContactId = "", onSelectContact = () => {} }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(null);
    const [pickedContact, setPickedContact] = useState(null);

    const startEdit = () => {
        setDraft({
            payment_terms: p.pd_payment_terms || "", status: p.pd_payment_followup_status || "not_due",
            expected_date: toDateInputValue(p.pd_expected_payment_date), next_follow_up: toDateInputValue(p.pd_next_follow_up_at),
            notes: p.pd_payment_followup_notes || "",
        });
        setPickedContact(null);
        setEditing(true);
    };
    const save = () => {
        const payload = {
            payment_terms: draft.payment_terms, payment_followup_status: draft.status,
            expected_payment_date: fromDateInputValue(draft.expected_date),
            payment_followup_notes: draft.notes,
        };
        const newNextFollowUp = fromDateInputValue(draft.next_follow_up);
        if (newNextFollowUp !== p.pd_next_follow_up_at) {
            payload.next_follow_up_at = newNextFollowUp;
            payload.last_follow_up_at = new Date().toISOString();
        }
        if (pickedContact) payload.production_contact_client_id = pickedContact.id || pickedContact._id;
        onSave(payload);
        setEditing(false);
        setDraft(null);
    };

    return (
        <div data-testid="pd-payment-followup-block">
            {!editing ? (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                    <div><span className="text-black/40 block">Payment Terms</span><span className="text-black/70">{p.pd_payment_terms || "—"}</span></div>
                    <div><span className="text-black/40 block">Expected Date</span><span className="text-black/70">{formatDate(p.pd_expected_payment_date)}</span></div>
                    <div><span className="text-black/40 block">Concerned Person</span><span className="text-black/70">{p.pd_production_contact?.name || "—"}</span></div>
                    <div className="sm:col-span-2" data-testid="pd-followup-recipient">
                        <span className="text-black/40 block mb-1">Send follow-up to</span>
                        {contacts.length === 0 ? (
                            <span className="text-black/35">No CRM contacts on this project yet — set a Concerned Person below or add someone under Crew.</span>
                        ) : (
                            <Select value={selectedContactId || undefined} onValueChange={onSelectContact}>
                                <SelectTrigger className="h-8 text-xs w-full sm:w-[360px]" data-testid="pd-followup-recipient-select"><SelectValue placeholder="Choose who to follow up with" /></SelectTrigger>
                                <SelectContent>
                                    {contacts.map((c) => (
                                        <SelectItem key={c.client_id} value={c.client_id} disabled={!c.has_phone} data-testid={`pd-followup-contact-${c.client_id}`}>
                                            {c.name || "Unnamed"}{c.role ? ` · ${c.role}` : (c.designation ? ` · ${c.designation}` : "")}{c.company_name ? ` · ${c.company_name}` : ""}{!c.has_phone ? " (no phone)" : ""}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        )}
                    </div>
                    <div><span className="text-black/40 block">Status</span><Badge variant="outline" className="text-[10px] capitalize">{(p.pd_payment_followup_status || "not_due").replace("_", " ")}</Badge></div>
                    <div><span className="text-black/40 block">Next Follow-up</span><span className="text-black/70">{formatDate(p.pd_next_follow_up_at)}</span></div>
                    {p.pd_payment_followup_notes && <div className="sm:col-span-2"><span className="text-black/40 block">Notes</span><span className="text-black/70 whitespace-pre-wrap">{p.pd_payment_followup_notes}</span></div>}
                    {p.pd_last_follow_up_at && <div className="sm:col-span-2 text-black/40">Last followed up: {formatDate(p.pd_last_follow_up_at)}</div>}
                    <div className="sm:col-span-2"><EditControls editing={false} onEdit={startEdit} /></div>
                </div>
            ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div className="sm:col-span-2">
                        <Label className="text-[11px] text-black/40">Concerned Person (CRM contact)</Label>
                        <div className="mt-1">
                            <ClientPicker
                                clients={clients}
                                placeholder={pickedContact?.name || p.pd_production_contact?.name || "Search CRM contacts…"}
                                onContactCreated={onContactCreated}
                                onPicked={setPickedContact}
                            />
                        </div>
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Payment Terms</Label>
                        <Input value={draft.payment_terms} onChange={(e) => setDraft((d) => ({ ...d, payment_terms: e.target.value }))} placeholder="e.g. 50% advance, 50% on delivery" className="h-8 text-xs mt-1" />
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Status</Label>
                        <Select value={draft.status} onValueChange={(v) => setDraft((d) => ({ ...d, status: v }))}>
                            <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                            <SelectContent>
                                {PAYMENT_FOLLOWUP_STATUSES.map((o) => <SelectItem key={o} value={o}>{o.replace("_", " ")}</SelectItem>)}
                            </SelectContent>
                        </Select>
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Expected Date</Label>
                        <Input type="date" value={draft.expected_date} onChange={(e) => setDraft((d) => ({ ...d, expected_date: e.target.value }))} className="h-8 text-xs mt-1" />
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Next Follow-up</Label>
                        <Input type="date" value={draft.next_follow_up} onChange={(e) => setDraft((d) => ({ ...d, next_follow_up: e.target.value }))} className="h-8 text-xs mt-1" />
                    </div>
                    <div className="sm:col-span-2">
                        <Label className="text-[11px] text-black/40">Notes</Label>
                        <Textarea value={draft.notes} onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))} rows={2} className="text-xs mt-1" />
                    </div>
                    <div className="sm:col-span-2 flex justify-end">
                        <EditControls editing onSave={save} onCancel={() => { setEditing(false); setDraft(null); }} />
                    </div>
                </div>
            )}
        </div>
    );
}

function ShootDetailsProjectBlock({ project: p, knownLocations, onSave }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(null);
    const [showLegacy, setShowLegacy] = useState(false);

    const startEdit = () => {
        setDraft({
            call_time: p.pd_call_time || "", reporting_time: p.pd_reporting_time || "",
            location: p.pd_shoot_location || "", status: p.pd_shoot_status || "not_scheduled",
            notes: p.pd_shoot_notes || "", shoot_dates: p.shoot_dates || "",
            shoot_date: p.pd_shoot_date || "",
        });
        setEditing(true);
    };
    const save = () => {
        onSave({
            call_time: draft.call_time, reporting_time: draft.reporting_time,
            shoot_location: draft.location, shoot_status: draft.status, shoot_notes: draft.notes,
            shoot_dates: draft.shoot_dates, shoot_date: draft.shoot_date || null,
        });
        setEditing(false);
        setDraft(null);
    };

    return (
        <div>
            {/* Shoot dates live ONLY in the Talent Shooting Schedule below — the project-level
                date list that used to sit here duplicated it and was removed. These are the
                project-wide DEFAULTS (shown on reminders and in the Management Agent). */}
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-[11px] font-medium text-black/50 uppercase tracking-wide">Project Shoot Info (defaults)</span>
                <EditControls editing={editing} onEdit={startEdit} onSave={save} onCancel={() => { setEditing(false); setDraft(null); }} />
            </div>
            {!editing ? (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2 text-xs">
                    <div><span className="text-black/40 block">Call Time</span><span className="text-black/70">{p.pd_call_time || "—"}</span></div>
                    <div><span className="text-black/40 block">Reporting Time</span><span className="text-black/70">{p.pd_reporting_time || "—"}</span></div>
                    <div><span className="text-black/40 block">Location</span><LocationLink name={p.pd_shoot_location} mapUrl={null} /></div>
                    <div><span className="text-black/40 block">Status</span><Badge variant="outline" className="text-[10px] capitalize">{(p.pd_shoot_status || "not_scheduled").replace("_", " ")}</Badge></div>
                    {p.pd_shoot_notes && <div className="col-span-2 sm:col-span-4"><span className="text-black/40 block">Notes</span><span className="text-black/70 whitespace-pre-wrap">{p.pd_shoot_notes}</span></div>}
                </div>
            ) : (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    <div><Label className="text-[10px]">Call Time</Label><Input value={draft.call_time} onChange={(e) => setDraft((d) => ({ ...d, call_time: e.target.value }))} placeholder="e.g. 8:00 AM" className="h-7 text-xs" /></div>
                    <div><Label className="text-[10px]">Reporting Time</Label><Input value={draft.reporting_time} onChange={(e) => setDraft((d) => ({ ...d, reporting_time: e.target.value }))} placeholder="e.g. 7:00 AM" className="h-7 text-xs" /></div>
                    <div>
                        <Label className="text-[10px]">Location</Label>
                        <LocationPicker value={draft.location} knownLocations={knownLocations} className="h-7 text-xs" immediate onCommit={(name) => setDraft((d) => ({ ...d, location: name }))} />
                    </div>
                    <div>
                        <Label className="text-[10px]">Status</Label>
                        <Select value={draft.status} onValueChange={(v) => setDraft((d) => ({ ...d, status: v }))}>
                            <SelectTrigger className="h-7 text-xs"><SelectValue /></SelectTrigger>
                            <SelectContent>{SHOOT_STATUS_OPTIONS.map((o) => <SelectItem key={o} value={o}>{o.replace("_", " ")}</SelectItem>)}</SelectContent>
                        </Select>
                    </div>
                    <div className="col-span-2 sm:col-span-4"><Label className="text-[10px]">Notes</Label><Textarea value={draft.notes} onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))} rows={2} className="text-xs" /></div>
                    <div className="col-span-2 sm:col-span-4 pt-2 border-t border-black/[0.06]">
                        <Label className="text-[10px] text-black/40">Legacy / Reminder fields</Label>
                        <div className="grid grid-cols-2 gap-2 mt-1">
                            <div>
                                <Label className="text-[10px]" title="Free-text shooting-date summary used on submission forms and the client link.">Shooting Dates (free text)</Label>
                                <Input value={draft.shoot_dates} onChange={(e) => setDraft((d) => ({ ...d, shoot_dates: e.target.value }))} placeholder="e.g. 26th - 27th August" className="h-7 text-xs" />
                            </div>
                            <div>
                                <Label className="text-[10px]" title="Used only by the automated shoot-reminder worker.">Reminder Date</Label>
                                <Input type="date" value={draft.shoot_date} onChange={(e) => setDraft((d) => ({ ...d, shoot_date: e.target.value }))} className="h-7 text-xs" />
                            </div>
                        </div>
                    </div>
                </div>
            )}
            {!editing && (p.shoot_dates || p.pd_shoot_date) && (
                <button className="mt-2 text-[11px] text-black/35 hover:text-black/60 inline-flex items-center gap-1" onClick={() => setShowLegacy((v) => !v)}>
                    <History className="h-3 w-3" /> {showLegacy ? "Hide" : "Show"} legacy/reminder fields
                </button>
            )}
            {!editing && showLegacy && (
                <div className="mt-1.5 text-[11px] text-black/50 space-y-0.5">
                    {p.shoot_dates && <div>Shooting Dates (free text): {p.shoot_dates}</div>}
                    {p.pd_shoot_date && <div>Reminder Date: {p.pd_shoot_date}</div>}
                </div>
            )}
        </div>
    );
}

// ============================================================================
// V2 — Per-talent Shooting Schedule (spec sections 1-4). Cards, not a
// table, so this stays usable on mobile. Each row's own extra-hours
// figure here is a DISPLAY-only recomputation using the exact same
// formula the backend already applied to compute extra_hours_total — the
// authoritative number always comes from the server on save.
// ============================================================================
function _dayExtraHours(perDay, agreed, actual) {
    const a = Number(agreed);
    const act = Number(actual);
    if (!perDay || !a || a <= 0 || actual === null || actual === undefined || actual === "" || Number.isNaN(act)) return { hours: 0, amount: 0 };
    const extra = Math.max(0, act - a);
    return { hours: extra, amount: (perDay / a) * extra };
}

const SHOOT_SCHEDULE_COLUMNS = ["Date", "Call Time", "Reporting Time", "Location", "Agreed Basis", "Actual Hours", "Extra Hours", "Extra Amount", "Status", "Actions"];
// Explicit per-column widths (not equal grid-cols-N) — Actions and Location
// need real room; a short numeric column doesn't. This exact template is
// shared by the header AND every row (view + edit) so nothing can drift out
// of alignment, and Actions gets enough width to never wrap (the original
// UI defect: a 10-label header over a 9-cell row, with Actions crammed into
// the same cell as the Status badge).
const SHOOT_SCHEDULE_GRID = "lg:grid-cols-[1fr_0.85fr_0.95fr_1.15fr_0.8fr_0.8fr_0.75fr_0.85fr_0.9fr_0.95fr]";

// V2 polish (spec section 4/7) — one shoot day, explicit Edit/Save/Cancel.
// Renders BOTH a desktop grid-row (proper labeled columns, not
// placeholder-only cells) and a mobile stacked card from the SAME local
// state, so editing never desyncs between breakpoints.
//
// The Talent Shooting Schedule is the ONE place shoot dates live. Times are
// IST wall-clock "HH:MM" (a time picker — never a typed free-text string),
// reporting cannot be later than call, and a location picked through Google
// Maps keeps its address, place id, coordinates and Maps link.
const EMPTY_PLACE = { location_address: "", location_place_id: "", location_lat: null, location_lng: null };

// Merges a LocationPicker commit into a draft: a Google selection stores the whole place;
// typing different text over a previous place drops that place's address/coordinates/link
// (they would otherwise stay attached to words that no longer describe them).
function applyLocationCommit(d, name, mapUrl, place) {
    if (place) {
        return {
            ...d, location: name, location_map_url: mapUrl || "",
            location_address: place.address || "", location_place_id: place.place_id || "",
            location_lat: place.lat ?? null, location_lng: place.lng ?? null,
        };
    }
    if (name !== d.location && d.location_place_id) {
        return { ...d, location: name, location_map_url: mapUrl || "", ...EMPTY_PLACE };
    }
    return { ...d, location: name, location_map_url: mapUrl && !d.location_map_url ? mapUrl : d.location_map_url };
}

// A legacy free-text time ("9 AM") can't populate a time picker; leaving the picker blank must
// leave that stored text UNCHANGED, never silently wipe it — so an untouched legacy value is
// simply omitted from the PATCH.
function timeField(key, draftValue, original) {
    if (draftValue) return { [key]: draftValue };
    if (original && !clockInputValue(original)) return {};   // legacy text, untouched
    return { [key]: null };
}

function ClockInput({ value, onChange, legacy, className, testId }) {
    return (
        <div>
            <Input type="time" value={value} onChange={(e) => onChange(e.target.value)} className={className || "h-7 text-[11px]"} data-testid={testId} />
            {legacy && !value && <div className="text-[10px] text-black/35 mt-0.5 truncate" title="Saved as free text — pick a time to replace it">Saved: {legacy}</div>}
        </div>
    );
}

function ShootDayRow({ day, perDay, knownLocations, onUpdate, onDelete }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(null);
    const extra = _dayExtraHours(perDay, day.agreed_hours, day.actual_hours);

    const startEdit = () => {
        setDraft({
            date: day.date || "", call_time: clockInputValue(day.call_time), reporting_time: clockInputValue(day.reporting_time),
            location: day.location || "", location_map_url: day.location_map_url || "",
            location_address: day.location_address || "", location_place_id: day.location_place_id || "",
            location_lat: day.location_lat ?? null, location_lng: day.location_lng ?? null,
            notes: day.notes || "",
            agreed_hours: day.agreed_hours ?? "", actual_hours: day.actual_hours ?? "",
            shoot_status: day.shoot_status || "scheduled",
        });
        setEditing(true);
    };
    const badTimes = draft ? reportingAfterCall(draft.reporting_time, draft.call_time) : false;
    const save = () => {
        if (badTimes) return;
        onUpdate({
            date: draft.date,
            ...timeField("call_time", draft.call_time, day.call_time),
            ...timeField("reporting_time", draft.reporting_time, day.reporting_time),
            location: draft.location || null, location_map_url: draft.location_map_url || null,
            location_address: draft.location_address || null, location_place_id: draft.location_place_id || null,
            location_lat: draft.location_lat, location_lng: draft.location_lng,
            notes: draft.notes || null,
            agreed_hours: draft.agreed_hours === "" ? null : Number(draft.agreed_hours),
            actual_hours: draft.actual_hours === "" ? null : Number(draft.actual_hours),
            shoot_status: draft.shoot_status,
        });
        setEditing(false);
        setDraft(null);
    };
    const cancel = () => { setEditing(false); setDraft(null); };

    if (editing) {
        return (
            <div className={`grid grid-cols-2 ${SHOOT_SCHEDULE_GRID} gap-x-2 gap-y-2 lg:items-end bg-white border border-[#0c2340]/20 rounded-md p-2.5`} data-testid={`pd-shoot-day-${day.id}`}>
                <div><Label className="text-[10px] lg:hidden">Date</Label><Input type="date" value={draft.date} onChange={(e) => setDraft((d) => ({ ...d, date: e.target.value }))} className="h-7 text-[11px]" /></div>
                <div><Label className="text-[10px] lg:hidden">Call Time (IST)</Label><ClockInput value={draft.call_time} legacy={day.call_time && !clockInputValue(day.call_time) ? day.call_time : ""} onChange={(v) => setDraft((d) => ({ ...d, call_time: v }))} testId={`pd-day-call-${day.id}`} /></div>
                <div><Label className="text-[10px] lg:hidden">Reporting Time (IST)</Label><ClockInput value={draft.reporting_time} legacy={day.reporting_time && !clockInputValue(day.reporting_time) ? day.reporting_time : ""} onChange={(v) => setDraft((d) => ({ ...d, reporting_time: v }))} testId={`pd-day-reporting-${day.id}`} /></div>
                <div>
                    <Label className="text-[10px] lg:hidden">Location</Label>
                    <LocationPicker
                        value={draft.location}
                        knownLocations={knownLocations}
                        className="h-7 text-[11px]"
                        immediate
                        onCommit={(name, mapUrl, place) => setDraft((d) => applyLocationCommit(d, name, mapUrl, place))}
                    />
                    {draft.location_address && <div className="text-[10px] text-black/40 truncate mt-0.5" title={draft.location_address}>{draft.location_address}</div>}
                </div>
                <div><Label className="text-[10px] lg:hidden">Agreed Basis (hrs)</Label><Input type="number" value={draft.agreed_hours} onChange={(e) => setDraft((d) => ({ ...d, agreed_hours: e.target.value }))} className="h-7 text-[11px]" /></div>
                <div><Label className="text-[10px] lg:hidden">Actual Hours</Label><Input type="number" value={draft.actual_hours} onChange={(e) => setDraft((d) => ({ ...d, actual_hours: e.target.value }))} className="h-7 text-[11px]" /></div>
                <div className="text-[11px] text-black/40"><span className="lg:hidden text-black/40 mr-1">Extra:</span>{extra.hours > 0 ? `${extra.hours}h` : "—"}</div>
                <div className="text-[11px] text-black/40"><span className="lg:hidden text-black/40 mr-1">Extra Amt:</span>{extra.hours > 0 ? formatCurrency(extra.amount) : "—"}</div>
                <div>
                    <Label className="text-[10px] lg:hidden">Status</Label>
                    <Select value={draft.shoot_status} onValueChange={(v) => setDraft((d) => ({ ...d, shoot_status: v }))}>
                        <SelectTrigger className="h-7 text-[11px]"><SelectValue /></SelectTrigger>
                        <SelectContent>{SHOOT_STATUS_OPTIONS.map((o) => <SelectItem key={o} value={o}>{o.replace("_", " ")}</SelectItem>)}</SelectContent>
                    </Select>
                </div>
                <div className="col-span-2 lg:col-span-1 flex lg:justify-end items-center gap-1.5">
                    <EditControls editing onSave={save} onCancel={cancel} saveDisabled={badTimes} />
                </div>
                <div className="col-span-2 lg:col-span-full">
                    <Label className="text-[10px]">Notes</Label>
                    <Input value={draft.notes} onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))} placeholder="Anything the talent should know for this day" className="h-7 text-[11px]" />
                    {badTimes && <div className="text-[11px] text-red-600 mt-1" data-testid={`pd-day-time-error-${day.id}`}>Reporting time cannot be later than call time.</div>}
                </div>
            </div>
        );
    }

    return (
        <>
            {/* Desktop — labeled grid row, matches the header below */}
            <div className={`hidden lg:grid ${SHOOT_SCHEDULE_GRID} gap-x-2 items-center text-[11px] bg-slate-50/60 rounded-md px-2 py-2.5`} data-testid={`pd-shoot-day-${day.id}`}>
                <div className="font-medium text-black/70 truncate">{formatDate(day.date)} <span className="text-black/30 font-normal">{(day.date || "").slice(0, 4)}</span></div>
                <div className="text-black/60 truncate">{formatClock(day.call_time)}</div>
                <div className="text-black/60 truncate">{formatClock(day.reporting_time)}</div>
                <div className="text-black/60 truncate min-w-0"><LocationLink name={day.location} mapUrl={day.location_map_url} address={day.location_address} /></div>
                <div className="text-black/60">{day.agreed_hours ?? "—"}</div>
                <div className="text-black/60">{day.actual_hours ?? "—"}</div>
                <div className={extra.hours > 0 ? "text-amber-700 font-medium" : "text-black/30"}>{extra.hours > 0 ? `${extra.hours}h` : "—"}</div>
                <div className={extra.hours > 0 ? "text-amber-700 font-medium truncate" : "text-black/30"}>{extra.hours > 0 ? formatCurrency(extra.amount) : "—"}</div>
                <div><Badge variant="outline" className="text-[10px] capitalize">{(day.shoot_status || "scheduled").replace("_", " ")}</Badge></div>
                <div className="flex items-center justify-end">
                    <EditControls editing={false} onEdit={startEdit} onDelete={onDelete} compact />
                </div>
                {day.notes && <div className="col-span-full text-[10px] text-black/45 pt-1 truncate" title={day.notes}>Note: {day.notes}</div>}
            </div>
            {/* Mobile — stacked card, same data, same state */}
            <div className="lg:hidden rounded-md border border-black/[0.08] p-2.5 text-[11px] space-y-1.5" data-testid={`pd-shoot-day-mobile-${day.id}`}>
                <div className="flex items-center justify-between">
                    <span className="font-semibold text-black/80">{formatDate(day.date)} {(day.date || "").slice(0, 4)}</span>
                    <EditControls editing={false} onEdit={startEdit} onDelete={onDelete} />
                </div>
                <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-black/60">
                    <div><span className="text-black/40">Call:</span> {formatClock(day.call_time)}</div>
                    <div><span className="text-black/40">Reporting:</span> {formatClock(day.reporting_time)}</div>
                    <div className="col-span-2"><span className="text-black/40">Location:</span> <LocationLink name={day.location} mapUrl={day.location_map_url} address={day.location_address} /></div>
                    <div><span className="text-black/40">Agreed:</span> {day.agreed_hours ?? "—"}h</div>
                    <div><span className="text-black/40">Actual:</span> {day.actual_hours ?? "—"}h</div>
                    {extra.hours > 0 && (
                        <div className="col-span-2 text-amber-700 font-medium">Extra: {extra.hours}h · {formatCurrency(extra.amount)}</div>
                    )}
                    {day.notes && <div className="col-span-2 text-black/50">Note: {day.notes}</div>}
                </div>
                <Badge variant="outline" className="text-[10px] capitalize">{(day.shoot_status || "scheduled").replace("_", " ")}</Badge>
            </div>
        </>
    );
}

const EMPTY_DAY_FORM = {
    date: "", call_time: "", reporting_time: "", location: "", location_map_url: "",
    location_address: "", location_place_id: "", location_lat: null, location_lng: null,
    notes: "", agreed_hours: "", actual_hours: "",
};

function TalentShootSchedule({ talent, knownLocations, onAdd, onUpdate, onDelete }) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState(EMPTY_DAY_FORM);
    const badTimes = reportingAfterCall(form.reporting_time, form.call_time);

    const submit = () => {
        if (!form.date || badTimes) return;
        onAdd({
            date: form.date,
            call_time: form.call_time || null,
            reporting_time: form.reporting_time || null,
            location: form.location || null,
            location_map_url: form.location_map_url || null,
            location_address: form.location_address || null,
            location_place_id: form.location_place_id || null,
            location_lat: form.location_lat,
            location_lng: form.location_lng,
            notes: form.notes || null,
            agreed_hours: form.agreed_hours === "" ? null : Number(form.agreed_hours),
            actual_hours: form.actual_hours === "" ? null : Number(form.actual_hours),
        });
        setForm(EMPTY_DAY_FORM);
        setAdding(false);
    };

    return (
        <div className="rounded-lg border border-black/[0.08] p-3" data-testid={`pd-shoot-schedule-${talent.talent_id}`}>
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-xs font-semibold text-black/80">{talent.name}</span>
                <Button size="sm" variant="outline" className="h-6 text-[10px] px-2" onClick={() => setAdding((v) => !v)} data-testid={`pd-add-shoot-day-${talent.talent_id}`}>
                    <Plus className="h-3 w-3 mr-1" /> Add Date
                </Button>
            </div>

            {talent.shoot_days.length === 0 && !adding && (
                <div className="text-[11px] text-black/30 italic py-2">No shoot days scheduled.</div>
            )}

            {talent.shoot_days.length > 0 && (
                <div className={`hidden lg:grid ${SHOOT_SCHEDULE_GRID} gap-x-2 px-2 pb-1 text-[10px] uppercase tracking-wide text-black/35`}>
                    {SHOOT_SCHEDULE_COLUMNS.map((c, i) => (
                        <div key={c} className={i === SHOOT_SCHEDULE_COLUMNS.length - 1 ? "text-right" : ""}>{c}</div>
                    ))}
                </div>
            )}
            <div className="space-y-1.5">
                {talent.shoot_days.map((d) => (
                    <ShootDayRow
                        key={d.id}
                        day={d}
                        perDay={talent.budget_per_day}
                        knownLocations={knownLocations}
                        onUpdate={(payload) => onUpdate(d.id, payload)}
                        onDelete={() => onDelete(d.id)}
                    />
                ))}
            </div>

            {adding && (
                <div className="mt-2 grid grid-cols-2 lg:grid-cols-4 gap-1.5 items-end bg-white border border-black/[0.06] rounded-md p-2" data-testid={`pd-shoot-day-form-${talent.talent_id}`}>
                    <div><Label className="text-[10px]">Date</Label><Input type="date" value={form.date} onChange={(e) => setForm((f) => ({ ...f, date: e.target.value }))} className="h-7 text-[11px]" data-testid={`pd-form-date-${talent.talent_id}`} /></div>
                    <div><Label className="text-[10px]">Call Time (IST)</Label><ClockInput value={form.call_time} onChange={(v) => setForm((f) => ({ ...f, call_time: v }))} testId={`pd-form-call-${talent.talent_id}`} /></div>
                    <div><Label className="text-[10px]">Reporting (IST)</Label><ClockInput value={form.reporting_time} onChange={(v) => setForm((f) => ({ ...f, reporting_time: v }))} testId={`pd-form-reporting-${talent.talent_id}`} /></div>
                    <div>
                        <Label className="text-[10px]">Location</Label>
                        <LocationPicker
                            value={form.location}
                            knownLocations={knownLocations}
                            className="h-7 text-[11px]"
                            immediate
                            onCommit={(name, mapUrl, place) => setForm((f) => applyLocationCommit(f, name, mapUrl, place))}
                        />
                        {form.location_address && <div className="text-[10px] text-black/40 truncate mt-0.5" title={form.location_address}>{form.location_address}</div>}
                    </div>
                    <div><Label className="text-[10px]">Agreed Hours (basis)</Label><Input type="number" value={form.agreed_hours} onChange={(e) => setForm((f) => ({ ...f, agreed_hours: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Actual Hours</Label><Input type="number" value={form.actual_hours} onChange={(e) => setForm((f) => ({ ...f, actual_hours: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div className="col-span-2"><Label className="text-[10px]">Notes</Label><Input value={form.notes} onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div className="col-span-2 lg:col-span-4 flex items-center justify-between gap-1.5">
                        <span className="text-[11px] text-red-600" data-testid={`pd-form-time-error-${talent.talent_id}`}>{badTimes ? "Reporting time cannot be later than call time." : ""}</span>
                        <span className="flex gap-1.5">
                            <Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={() => setAdding(false)}>Cancel</Button>
                            <Button size="sm" className="h-7 text-[11px]" disabled={!form.date || badTimes} onClick={submit}>Save</Button>
                        </span>
                    </div>
                </div>
            )}
        </div>
    );
}

// ============================================================================
// V2 polish (spec section 2) — Costume Trial, now Date + Time + Location,
// explicit Edit/Save/Cancel (spec section 7). Normal browsing is
// read-only; only "Edit" makes the fields mutable, and Cancel discards
// any unsaved change and restores exactly what the server last returned.
// ============================================================================
function CostumeTrialBlock({ talent, knownLocations, onSave }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(null);

    const startEdit = () => {
        setDraft({
            date: toDateInputValue(talent.costume_trial_at),
            time: talent.costume_trial_time || "",
            location: talent.costume_trial_location || "",
            location_map_url: talent.costume_trial_map_url || "",
        });
        setEditing(true);
    };

    const save = () => {
        onSave({
            costume_trial_at: fromDateInputValue(draft.date),
            costume_trial_time: draft.time || null,
            costume_trial_location: draft.location || null,
            costume_trial_map_url: draft.location_map_url || null,
        });
        setEditing(false);
        setDraft(null);
    };

    const hasAny = talent.costume_trial_at || talent.costume_trial_time || talent.costume_trial_location;

    return (
        <div data-testid={`pd-costume-trial-${talent.talent_id}`}>
            <div className="flex items-center justify-between gap-2 mb-1.5">
                <span className="text-[11px] font-medium text-black/50 uppercase tracking-wide">Costume Trial</span>
                <EditControls editing={editing} onEdit={startEdit} onSave={save} onCancel={() => { setEditing(false); setDraft(null); }} />
            </div>
            {!editing ? (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-black/70">
                    <span>{formatDate(talent.costume_trial_at)}</span>
                    {talent.costume_trial_time && <span>{talent.costume_trial_time}</span>}
                    <LocationLink name={talent.costume_trial_location} mapUrl={talent.costume_trial_map_url} />
                    {!hasAny && <span className="text-black/30 italic">Not scheduled</span>}
                </div>
            ) : (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
                    <div><Label className="text-[10px]">Date</Label><Input type="date" value={draft.date} onChange={(e) => setDraft((d) => ({ ...d, date: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Time</Label><Input value={draft.time} onChange={(e) => setDraft((d) => ({ ...d, time: e.target.value }))} placeholder="e.g. 4:00 PM" className="h-7 text-[11px]" /></div>
                    <div>
                        <Label className="text-[10px]">Location</Label>
                        <LocationPicker
                            value={draft.location}
                            knownLocations={knownLocations}
                            className="h-7 text-[11px]"
                            immediate
                            onCommit={(name, mapUrl) => setDraft((d) => ({ ...d, location: name, location_map_url: mapUrl && !d.location_map_url ? mapUrl : d.location_map_url }))}
                        />
                    </div>
                    <div><Label className="text-[10px]">Map URL</Label><Input value={draft.location_map_url} onChange={(e) => setDraft((d) => ({ ...d, location_map_url: e.target.value }))} placeholder="Optional" className="h-7 text-[11px]" /></div>
                </div>
            )}
        </div>
    );
}

// ============================================================================
// V2 — Readings & Rehearsals (spec section 8/38). Explicit per-row
// Edit/Save/Cancel/Delete, reusing the same PATCH endpoint the Management
// Agent could also call directly.
// ============================================================================
function ReadingRehearsalRow({ entry, knownLocations, onUpdate, onDelete }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(null);

    const startEdit = () => {
        setDraft({
            type: entry.type, date: entry.date || "", time: entry.time || "",
            location: entry.location || "", location_map_url: entry.location_map_url || "", notes: entry.notes || "",
        });
        setEditing(true);
    };
    const save = () => {
        onUpdate({
            type: draft.type, date: draft.date || null, time: draft.time || null,
            location: draft.location || null, location_map_url: draft.location_map_url || null, notes: draft.notes || null,
        });
        setEditing(false);
        setDraft(null);
    };

    if (editing) {
        return (
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-1.5 items-end bg-white border border-black/[0.08] rounded-md p-2" data-testid={`pd-reading-${entry.id}`}>
                <div>
                    <Label className="text-[10px]">Type</Label>
                    <Select value={draft.type} onValueChange={(v) => setDraft((d) => ({ ...d, type: v }))}>
                        <SelectTrigger className="h-7 text-[11px]"><SelectValue /></SelectTrigger>
                        <SelectContent>
                            <SelectItem value="reading">Reading</SelectItem>
                            <SelectItem value="rehearsal">Rehearsal</SelectItem>
                        </SelectContent>
                    </Select>
                </div>
                <div><Label className="text-[10px]">Date</Label><Input type="date" value={draft.date} onChange={(e) => setDraft((d) => ({ ...d, date: e.target.value }))} className="h-7 text-[11px]" /></div>
                <div><Label className="text-[10px]">Time</Label><Input value={draft.time} onChange={(e) => setDraft((d) => ({ ...d, time: e.target.value }))} placeholder="e.g. 4 PM" className="h-7 text-[11px]" /></div>
                <div>
                    <Label className="text-[10px]">Location</Label>
                    <LocationPicker
                        value={draft.location}
                        knownLocations={knownLocations}
                        className="h-7 text-[11px]"
                        immediate
                        onCommit={(name, mapUrl) => setDraft((d) => ({ ...d, location: name, location_map_url: mapUrl && !d.location_map_url ? mapUrl : d.location_map_url }))}
                    />
                </div>
                <div><Label className="text-[10px]">Notes</Label><Input value={draft.notes} onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))} className="h-7 text-[11px]" /></div>
                <div className="col-span-2 sm:col-span-5 flex justify-end gap-1.5">
                    <EditControls editing onSave={save} onCancel={() => { setEditing(false); setDraft(null); }} />
                </div>
            </div>
        );
    }

    return (
        <div className="flex items-center justify-between text-[11px] bg-slate-50/60 rounded-md px-2 py-1.5" data-testid={`pd-reading-${entry.id}`}>
            <div className="flex items-center gap-2 flex-wrap">
                <Badge variant="outline" className="text-[10px] capitalize">{entry.type}</Badge>
                {entry.date && <span className="text-black/60">{entry.date}</span>}
                {entry.time && <span className="text-black/50">{entry.time}</span>}
                {entry.location && (
                    entry.location_map_url ? (
                        <a href={entry.location_map_url} target="_blank" rel="noreferrer" className="text-black/50 hover:text-[#0c2340] hover:underline inline-flex items-center gap-0.5">
                            · <MapPin className="h-2.5 w-2.5" />{entry.location}
                        </a>
                    ) : <span className="text-black/50">· {entry.location}</span>
                )}
                {entry.notes && <span className="text-black/40">({entry.notes})</span>}
            </div>
            <EditControls editing={false} onEdit={startEdit} onDelete={onDelete} />
        </div>
    );
}

function TalentReadingsRehearsals({ talent, knownLocations, onAdd, onUpdate, onDelete }) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState({ type: "reading", date: "", time: "", location: "", location_map_url: "", notes: "" });

    const submit = () => {
        onAdd({
            type: form.type, date: form.date || null, time: form.time || null,
            location: form.location || null, location_map_url: form.location_map_url || null, notes: form.notes || null,
        });
        setForm({ type: "reading", date: "", time: "", location: "", location_map_url: "", notes: "" });
        setAdding(false);
    };

    return (
        <div data-testid={`pd-readings-${talent.talent_id}`}>
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-[11px] font-medium text-black/50 uppercase tracking-wide">Readings &amp; Rehearsals</span>
                <Button size="sm" variant="outline" className="h-6 text-[10px] px-2" onClick={() => setAdding((v) => !v)}>
                    <Plus className="h-3 w-3 mr-1" /> Add
                </Button>
            </div>
            {talent.readings_rehearsals.length === 0 && !adding && (
                <div className="text-[11px] text-black/30 italic py-1">No readings or rehearsals scheduled.</div>
            )}
            <div className="space-y-1">
                {talent.readings_rehearsals.map((e) => (
                    <ReadingRehearsalRow
                        key={e.id}
                        entry={e}
                        knownLocations={knownLocations}
                        onUpdate={(payload) => onUpdate(e.id, payload)}
                        onDelete={() => onDelete(e.id)}
                    />
                ))}
            </div>
            {adding && (
                <div className="mt-2 grid grid-cols-2 sm:grid-cols-5 gap-1.5 items-end bg-white border border-black/[0.06] rounded-md p-2">
                    <div>
                        <Label className="text-[10px]">Type</Label>
                        <Select value={form.type} onValueChange={(v) => setForm((f) => ({ ...f, type: v }))}>
                            <SelectTrigger className="h-7 text-[11px]"><SelectValue /></SelectTrigger>
                            <SelectContent>
                                <SelectItem value="reading">Reading</SelectItem>
                                <SelectItem value="rehearsal">Rehearsal</SelectItem>
                            </SelectContent>
                        </Select>
                    </div>
                    <div><Label className="text-[10px]">Date</Label><Input type="date" value={form.date} onChange={(e) => setForm((f) => ({ ...f, date: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Time</Label><Input value={form.time} onChange={(e) => setForm((f) => ({ ...f, time: e.target.value }))} placeholder="e.g. 4 PM" className="h-7 text-[11px]" /></div>
                    <div>
                        <Label className="text-[10px]">Location</Label>
                        <LocationPicker
                            value={form.location}
                            knownLocations={knownLocations}
                            className="h-7 text-[11px]"
                            immediate
                            onCommit={(name, mapUrl) => setForm((f) => ({ ...f, location: name, location_map_url: mapUrl && !f.location_map_url ? mapUrl : f.location_map_url }))}
                        />
                    </div>
                    <div><Label className="text-[10px]">Notes</Label><Input value={form.notes} onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div className="col-span-2 sm:col-span-5 flex justify-end gap-1.5">
                        <Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={() => setAdding(false)}>Cancel</Button>
                        <Button size="sm" className="h-7 text-[11px]" onClick={submit}>Save</Button>
                    </div>
                </div>
            )}
        </div>
    );
}

// ============================================================================
// V2 — Talent Financials (spec sections 21-25). Every number here comes
// straight from the server's already-computed _talent_card fields — no
// arithmetic happens in this component.
// ============================================================================
function TalentFinancialCard({ talent, onAskInvoice }) {
    return (
        <div className="rounded-lg border border-black/[0.08] p-3" data-testid={`pd-financial-${talent.talent_id}`}>
            {/* UI audit fix — the talent name used to truncate to "ZZ…" on
                mobile/tablet because it shared one row with the (long)
                invoice button; stacking below lg: gives the name its own
                line at every width narrower than genuine desktop. */}
            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-2 mb-2">
                <span className="text-xs font-semibold text-black/80">{talent.name}</span>
                <Button
                    size="sm" variant="outline" className="h-6 text-[10px] px-2 self-start lg:self-auto shrink-0"
                    onClick={onAskInvoice}
                    data-testid={`pd-ask-invoice-${talent.talent_id}`}
                    title={talent.whatsapp_group_name
                        ? `Sends to the "${talent.whatsapp_group_name}" WhatsApp group — you'll be asked to confirm the message first`
                        : "Opens WhatsApp with the message pre-filled — you review and send it yourself"}
                >
                    <MessageCircle className="h-3 w-3 mr-1" /> Ask to Raise Invoice
                    <span className="text-black/40 ml-1" data-testid={`pd-whatsapp-destination-${talent.talent_id}`}>
                        · WhatsApp{talent.whatsapp_group_name ? " Group" : ""}
                    </span>
                </Button>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1.5 text-[11px]">
                <div className="flex justify-between sm:block"><span className="text-black/40">Talent Fee</span><span className="text-black/70 font-medium">{formatCurrency(talent.budget_total)}</span></div>
                <div className="flex justify-between sm:block"><span className="text-black/40">Extra Hours</span><span className="text-black/70 font-medium">{formatCurrency(talent.extra_hours_total)}</span></div>
                <div className="flex justify-between sm:block"><span className="text-black/40">Commissionable</span><span className="text-black/70 font-medium">{formatCurrency(talent.commissionable_amount)}</span></div>
                <div className="flex justify-between sm:block"><span className="text-black/40">Commission ({talent.commission_percent ?? "—"}%)</span><span className="text-black/70 font-medium">{formatCurrency(talent.commission_amount)}</span></div>
                <div className="flex justify-between sm:block"><span className="text-black/40">Reimbursements</span><span className="text-black/70 font-medium">{formatCurrency(talent.reimbursement_total)}</span></div>
                <div className="flex justify-between sm:block"><span className="text-[#0c2340] font-semibold">Invoice Amount</span><span className="text-[#0c2340] font-bold">{formatCurrency(talent.invoice_amount)}</span></div>
            </div>
        </div>
    );
}

// ============================================================================
// V2 — Payment Tranches / Billing Milestones (spec sections 19-20). An
// operational billing tracker, deliberately not an accounting system.
// ============================================================================
// V2 polish (spec section 7/9) — one payment tranche, explicit
// Edit/Save/Cancel/Delete (previously every field committed on blur —
// risky for a financial record someone could accidentally edit while
// scrolling/clicking).
function TrancheRow({ tranche: t, index, onUpdate, onDelete }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(null);

    const startEdit = () => {
        setDraft({
            name: t.name, amount: String(t.amount ?? ""), trigger: t.trigger || "",
            invoice_status: t.invoice_status, invoice_date: t.invoice_date || "", invoice_number: t.invoice_number || "",
            payment_status: t.payment_status, payment_date: t.payment_date || "", notes: t.notes || "",
        });
        setEditing(true);
    };
    const save = () => {
        onUpdate({
            name: draft.name.trim() || t.name, amount: draft.amount === "" ? t.amount : Number(draft.amount),
            trigger: draft.trigger || null, invoice_status: draft.invoice_status, invoice_date: draft.invoice_date || null,
            invoice_number: draft.invoice_number || null, payment_status: draft.payment_status,
            payment_date: draft.payment_date || null, notes: draft.notes || null,
        });
        setEditing(false);
        setDraft(null);
    };

    if (editing) {
        return (
            <div className="rounded-md border border-[#0c2340]/20 bg-white p-2.5" data-testid={`pd-tranche-${t.id}`}>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                    <div><Label className="text-[10px]">Name / Milestone</Label><Input value={draft.name} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} className="h-7 text-xs" /></div>
                    <div><Label className="text-[10px]">Amount</Label><Input type="number" value={draft.amount} onChange={(e) => setDraft((d) => ({ ...d, amount: e.target.value }))} className="h-7 text-xs" /></div>
                    <div><Label className="text-[10px]">Trigger</Label><Input value={draft.trigger} onChange={(e) => setDraft((d) => ({ ...d, trigger: e.target.value }))} className="h-7 text-xs" /></div>
                    <div>
                        <Label className="text-[10px]">Invoice Status</Label>
                        <Select value={draft.invoice_status} onValueChange={(v) => setDraft((d) => ({ ...d, invoice_status: v }))}>
                            <SelectTrigger className="h-7 text-xs"><SelectValue /></SelectTrigger>
                            <SelectContent>{TRANCHE_INVOICE_STATUSES.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
                        </Select>
                    </div>
                    <div><Label className="text-[10px]">Invoice Date</Label><Input type="date" value={draft.invoice_date} onChange={(e) => setDraft((d) => ({ ...d, invoice_date: e.target.value }))} className="h-7 text-xs" /></div>
                    <div><Label className="text-[10px]">Invoice #</Label><Input value={draft.invoice_number} onChange={(e) => setDraft((d) => ({ ...d, invoice_number: e.target.value }))} className="h-7 text-xs" /></div>
                    <div>
                        <Label className="text-[10px]">Payment Status</Label>
                        <Select value={draft.payment_status} onValueChange={(v) => setDraft((d) => ({ ...d, payment_status: v }))}>
                            <SelectTrigger className="h-7 text-xs"><SelectValue /></SelectTrigger>
                            <SelectContent>{TRANCHE_PAYMENT_STATUSES.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
                        </Select>
                    </div>
                    <div><Label className="text-[10px]">Payment Date</Label><Input type="date" value={draft.payment_date} onChange={(e) => setDraft((d) => ({ ...d, payment_date: e.target.value }))} className="h-7 text-xs" /></div>
                    <div><Label className="text-[10px]">Notes</Label><Input value={draft.notes} onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))} className="h-7 text-xs" /></div>
                </div>
                <div className="flex justify-end mt-2">
                    <EditControls editing onSave={save} onCancel={() => { setEditing(false); setDraft(null); }} />
                </div>
            </div>
        );
    }

    return (
        <div className="rounded-md border border-black/[0.06] p-2.5" data-testid={`pd-tranche-${t.id}`}>
            <div className="flex items-center justify-between gap-2 mb-1.5">
                <span className="text-xs font-semibold text-black/80">{index + 1}. {t.name}</span>
                <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-black/70">{formatCurrency(t.amount)}</span>
                    <EditControls editing={false} onEdit={startEdit} onDelete={onDelete} />
                </div>
            </div>
            {t.trigger && <div className="text-[11px] text-black/40 mb-1">Trigger: {t.trigger}</div>}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
                <span className={t.invoice_status === "raised_and_sent" ? "text-emerald-700" : "text-amber-700"}>
                    Invoice: {TRANCHE_INVOICE_STATUSES.find((o) => o.value === t.invoice_status)?.label || t.invoice_status}
                    {t.invoice_date && ` (${formatDate(t.invoice_date)})`}
                </span>
                {t.invoice_number && <span className="text-black/50">Inv# {t.invoice_number}</span>}
                <span className={t.payment_status === "received" ? "text-emerald-700" : "text-amber-700"}>
                    Payment: {TRANCHE_PAYMENT_STATUSES.find((o) => o.value === t.payment_status)?.label || t.payment_status}
                    {t.payment_date && ` (${formatDate(t.payment_date)})`}
                </span>
            </div>
            {t.notes && <div className="text-[11px] text-black/40 mt-1">Notes: {t.notes}</div>}
        </div>
    );
}

function PaymentTranchesSection({ tranches, onAdd, onUpdate, onDelete, totals }) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState({ name: "", amount: "", trigger: "", invoice_number: "", invoice_date: "", payment_date: "", notes: "" });

    const submit = () => {
        if (!form.name.trim() || !form.amount) return;
        onAdd({
            name: form.name.trim(),
            amount: Number(form.amount),
            trigger: form.trigger || null,
            invoice_number: form.invoice_number || null,
            invoice_date: form.invoice_date || null,
            payment_date: form.payment_date || null,
            notes: form.notes || null,
        });
        setForm({ name: "", amount: "", trigger: "", invoice_number: "", invoice_date: "", payment_date: "", notes: "" });
        setAdding(false);
    };

    return (
        <SectionCard
            title="Payment Tranches"
            icon={Layers}
            right={<Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setAdding((v) => !v)}><Plus className="h-3 w-3 mr-1" /> Add Tranche</Button>}
            testId="pd-tranches"
        >
            {tranches.length > 0 && (
                <div className="flex flex-wrap gap-x-8 gap-y-2 mb-3 text-xs">
                    <span className="text-black/40">Total: <span className="text-black/80 font-semibold">{formatCurrency(totals.total)}</span></span>
                    <span className="text-black/40">Received: <span className="text-emerald-700 font-semibold">{formatCurrency(totals.received)}</span></span>
                    <span className="text-black/40">Outstanding: <span className="text-amber-700 font-semibold">{formatCurrency((totals.total || 0) - (totals.received || 0))}</span></span>
                </div>
            )}
            {tranches.length === 0 && !adding ? (
                <div className="text-xs text-black/40 py-4 text-center">No payment tranches yet.</div>
            ) : (
                <div className="space-y-2">
                    {tranches.map((t, i) => (
                        <TrancheRow key={t.id} tranche={t} index={i} onUpdate={(payload) => onUpdate(t.id, payload)} onDelete={() => onDelete(t.id)} />
                    ))}
                </div>
            )}
            {adding && (
                <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 items-end bg-slate-50/60 border border-black/[0.06] rounded-md p-3">
                    <div><Label className="text-[10px]">Name / Milestone</Label><Input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Signing" className="h-8 text-xs" autoFocus /></div>
                    <div><Label className="text-[10px]">Amount</Label><Input type="number" value={form.amount} onChange={(e) => setForm((f) => ({ ...f, amount: e.target.value }))} className="h-8 text-xs" /></div>
                    <div><Label className="text-[10px]">Trigger (optional)</Label><Input value={form.trigger} onChange={(e) => setForm((f) => ({ ...f, trigger: e.target.value }))} placeholder="e.g. On shoot commencement" className="h-8 text-xs" /></div>
                    <div><Label className="text-[10px]">Invoice Date (optional)</Label><Input type="date" value={form.invoice_date} onChange={(e) => setForm((f) => ({ ...f, invoice_date: e.target.value }))} className="h-8 text-xs" /></div>
                    <div><Label className="text-[10px]">Invoice # (optional)</Label><Input value={form.invoice_number} onChange={(e) => setForm((f) => ({ ...f, invoice_number: e.target.value }))} className="h-8 text-xs" /></div>
                    <div><Label className="text-[10px]">Payment Date (optional)</Label><Input type="date" value={form.payment_date} onChange={(e) => setForm((f) => ({ ...f, payment_date: e.target.value }))} className="h-8 text-xs" /></div>
                    <div className="sm:col-span-3"><Label className="text-[10px]">Notes (optional)</Label><Input value={form.notes} onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))} className="h-8 text-xs" /></div>
                    <div className="sm:col-span-3 flex justify-end gap-1.5">
                        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setAdding(false)}>Cancel</Button>
                        <Button size="sm" className="h-7 text-xs" disabled={!form.name.trim() || !form.amount} onClick={submit}>Add Tranche</Button>
                    </div>
                </div>
            )}
        </SectionCard>
    );
}

function KickbackForm({ clients, onContactCreated, onSubmit }) {
    const [amount, setAmount] = useState("");
    const [recipient, setRecipient] = useState(null);
    const [notes, setNotes] = useState("");
    const [saving, setSaving] = useState(false);
    return (
        <div className="space-y-3">
            <div><Label className="text-xs">Amount</Label><Input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} className="h-8 text-xs mt-1" /></div>
            <div>
                <Label className="text-xs">Recipient</Label>
                <div className="mt-1">
                    <ClientPicker clients={clients} onContactCreated={onContactCreated} placeholder={recipient?.name || "Search CRM contacts…"} onPicked={setRecipient} />
                </div>
            </div>
            <div><Label className="text-xs">Notes</Label><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} className="text-xs mt-1" /></div>
            <DialogFooter>
                <Button
                    size="sm"
                    disabled={!amount || !recipient || saving}
                    onClick={async () => { setSaving(true); await onSubmit({ amount: Number(amount), recipient, notes }); setSaving(false); }}
                >
                    {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Add Kickback"}
                </Button>
            </DialogFooter>
        </div>
    );
}

function ReimbursementForm({ talents, onSubmit }) {
    const [talentId, setTalentId] = useState(talents[0]?.talent_id || "");
    const [expenseType, setExpenseType] = useState("");
    const [amount, setAmount] = useState("");
    const [date, setDate] = useState("");
    const [notes, setNotes] = useState("");
    const [file, setFile] = useState(null);
    const [saving, setSaving] = useState(false);
    return (
        <div className="space-y-3">
            <div>
                <Label className="text-xs">Talent</Label>
                <Select value={talentId} onValueChange={setTalentId}>
                    <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {talents.map((t) => <SelectItem key={t.talent_id} value={t.talent_id}>{t.name}</SelectItem>)}
                    </SelectContent>
                </Select>
            </div>
            <div><Label className="text-xs">Expense Type / Reason</Label><Input value={expenseType} onChange={(e) => setExpenseType(e.target.value)} placeholder="e.g. Travel, Food" className="h-8 text-xs mt-1" /></div>
            <div className="grid grid-cols-2 gap-2">
                <div><Label className="text-xs">Amount</Label><Input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} className="h-8 text-xs mt-1" /></div>
                <div><Label className="text-xs">Date</Label><Input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="h-8 text-xs mt-1" /></div>
            </div>
            <div><Label className="text-xs">Notes</Label><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} className="text-xs mt-1" /></div>
            <div>
                <Label className="text-xs">Bill / Receipt (optional)</Label>
                <Input type="file" onChange={(e) => setFile(e.target.files?.[0] || null)} className="h-8 text-xs mt-1" />
            </div>
            <DialogFooter>
                <Button
                    size="sm"
                    disabled={!talentId || !expenseType || !amount || saving}
                    onClick={async () => { setSaving(true); await onSubmit({ talentId, expenseType, amount, date, notes, file }); setSaving(false); }}
                >
                    {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Add Reimbursement"}
                </Button>
            </DialogFooter>
        </div>
    );
}

function CrewForm({ clients, roles, onContactCreated, onSubmit }) {
    const [contact, setContact] = useState(null);
    const roleOptions = roles && roles.length > 0 ? roles : CREW_ROLES;
    const [role, setRole] = useState(roleOptions[0]);
    // The contact-types fetch is async — if it resolves after this form's
    // first render, adopt its first option instead of staying on the
    // fallback list's default (only while the admin hasn't picked yet).
    useEffect(() => {
        if (roles && roles.length > 0 && !roles.includes(role)) setRole(roles[0]);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [roles]);
    const [saving, setSaving] = useState(false);
    return (
        <div className="space-y-3">
            <div>
                <Label className="text-xs">Contact</Label>
                <div className="mt-1"><ClientPicker clients={clients} onContactCreated={onContactCreated} placeholder={contact?.name || "Search CRM contacts…"} onPicked={setContact} /></div>
            </div>
            <div>
                <Label className="text-xs">Role</Label>
                <Select value={role} onValueChange={setRole}>
                    <SelectTrigger className="h-8 text-xs mt-1" data-testid="pd-crew-role-select"><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {roleOptions.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
                    </SelectContent>
                </Select>
            </div>
            <DialogFooter>
                <Button size="sm" disabled={!contact || saving} onClick={async () => { setSaving(true); await onSubmit({ contact, role }); setSaving(false); }}>
                    {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Add Crew Member"}
                </Button>
            </DialogFooter>
        </div>
    );
}

function UploadDocumentForm({ onSubmit }) {
    const [category, setCategory] = useState(DOCUMENT_CATEGORIES[0].value);
    const [file, setFile] = useState(null);
    const [saving, setSaving] = useState(false);
    return (
        <div className="space-y-3">
            <div>
                <Label className="text-xs">Category</Label>
                <Select value={category} onValueChange={setCategory}>
                    <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {DOCUMENT_CATEGORIES.map((c) => <SelectItem key={c.value} value={c.value}>{c.label}</SelectItem>)}
                    </SelectContent>
                </Select>
            </div>
            <div>
                <Label className="text-xs">File</Label>
                <Input type="file" onChange={(e) => setFile(e.target.files?.[0] || null)} className="h-8 text-xs mt-1" />
            </div>
            <DialogFooter>
                <Button size="sm" disabled={!file || saving} onClick={async () => { setSaving(true); await onSubmit({ category, file }); setSaving(false); }}>
                    {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Upload"}
                </Button>
            </DialogFooter>
        </div>
    );
}

function AddTaskForm({ talents, onSubmit }) {
    const [title, setTitle] = useState("");
    const [dueDate, setDueDate] = useState("");
    const [priority, setPriority] = useState("normal");
    const [talentId, setTalentId] = useState("none");
    const [saving, setSaving] = useState(false);
    return (
        <div className="space-y-3">
            <div>
                <Label className="text-xs">Task</Label>
                <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Get call sheet" className="h-8 text-xs mt-1" autoFocus />
            </div>
            <div className="grid grid-cols-2 gap-2">
                <div>
                    <Label className="text-xs">Due Date</Label>
                    <Input type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} className="h-8 text-xs mt-1" />
                </div>
                <div>
                    <Label className="text-xs">Priority</Label>
                    <Select value={priority} onValueChange={setPriority}>
                        <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                        <SelectContent>
                            <SelectItem value="low">Low</SelectItem>
                            <SelectItem value="normal">Normal</SelectItem>
                            <SelectItem value="high">High</SelectItem>
                        </SelectContent>
                    </Select>
                </div>
            </div>
            {talents.length > 0 && (
                <div>
                    <Label className="text-xs">Talent (optional)</Label>
                    <Select value={talentId} onValueChange={setTalentId}>
                        <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                        <SelectContent>
                            <SelectItem value="none">None</SelectItem>
                            {talents.map((t) => <SelectItem key={t.talent_id} value={t.talent_id}>{t.name}</SelectItem>)}
                        </SelectContent>
                    </Select>
                </div>
            )}
            <DialogFooter>
                <Button
                    size="sm"
                    disabled={!title.trim() || saving}
                    onClick={async () => {
                        setSaving(true);
                        await onSubmit({
                            title: title.trim(),
                            category: "project",
                            due_at: fromDateInputValue(dueDate),
                            priority,
                            talent_id: talentId === "none" ? null : talentId,
                        });
                        setSaving(false);
                    }}
                >
                    {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Add Task"}
                </Button>
            </DialogFooter>
        </div>
    );
}
