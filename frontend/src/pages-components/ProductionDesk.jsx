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
    Layers, X,
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
const toDateInputValue = (iso) => {
    if (!iso) return "";
    try { return new Date(iso).toISOString().slice(0, 10); } catch { return ""; }
};
const fromDateInputValue = (dateStr) => (dateStr ? `${dateStr}T12:00:00.000Z` : null);

const formatDate = (iso) => {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
    } catch {
        return iso;
    }
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
function LocationLink({ name, mapUrl }) {
    if (!name && !mapUrl) return <span className="text-black/30">—</span>;
    if (mapUrl) {
        return (
            <a href={mapUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[#0c2340] hover:underline">
                <MapPin className="h-3 w-3 shrink-0" />
                <span className="truncate">{name || "View on map"}</span>
            </a>
        );
    }
    return <span className="text-black/70">{name}</span>;
}

function SectionCard({ title, icon: Icon, right, children, testId }) {
    return (
        <Card className="border-black/[0.08] shadow-none" data-testid={testId}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 py-3.5 px-4 border-b border-black/[0.06]">
                <CardTitle className="text-[13px] font-semibold text-black/80 flex items-center gap-2">
                    {Icon && <Icon className="h-3.5 w-3.5 text-black/40" />}
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

    const useProjectDatesForTalent = useCallback(async (talentId) => {
        try {
            const { data } = await adminApi.post(`/projects/${projectId}/production-desk/talents/${talentId}/shoot-days/use-project-dates`);
            setData(data);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not use project dates");
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
    const sendPaymentFollowUp = useCallback(async () => {
        try {
            const { data } = await adminApi.get(`/projects/${projectId}/production-desk/payment-followup-message`);
            openWhatsApp(data.phone, data.message);
            await patchProject({ last_follow_up_at: new Date().toISOString() });
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not build follow-up message");
        }
    }, [projectId]);

    const askTalentToRaiseInvoice = useCallback(async (talentId) => {
        try {
            const { data } = await adminApi.get(`/projects/${projectId}/production-desk/talents/${talentId}/invoice-message`);
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
        <div className="space-y-4 pb-16" data-testid="production-desk-root">
            {/* Overview */}
            <SectionCard title="Overview" icon={ClipboardList} testId="pd-overview">
                <div className="flex flex-wrap gap-x-8 gap-y-4">
                    <StatPill label="Locked Talents" value={summary.locked_count} />
                    <StatPill label="Shoot Days" value={summary.shoot_days ?? "—"} />
                    <StatPill label="Talent Budget" value={formatCurrency(summary.talent_budget_total)} />
                    <StatPill label="Production Budget" value={formatCurrency(summary.production_budget_total)} />
                    <StatPill label="TG Commission (Net)" value={formatCurrency(summary.commission_net)} />
                    <StatPill
                        label="Payments"
                        value={`${summary.payments_cleared}/${summary.payments_total} Cleared`}
                        tone={summary.payments_cleared === summary.payments_total && summary.payments_total > 0 ? "good" : "warn"}
                    />
                    {summary.payments_pending_amount > 0 && (
                        <StatPill label="Pending Amount" value={formatCurrency(summary.payments_pending_amount)} tone="warn" />
                    )}
                </div>

                {needs_attention.length > 0 && (
                    <div className="mt-4 flex items-start gap-2 rounded-md bg-amber-50 border border-amber-200 px-3 py-2.5" data-testid="pd-needs-attention">
                        <AlertTriangle className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
                        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-amber-800">
                            {needs_attention.map((item, i) => <span key={i}>{item}</span>)}
                        </div>
                    </div>
                )}
            </SectionCard>

            {/* Today / Upcoming — derived from Locked Talents' Talent
                Preparation fields, workflow_tasks, and the payment
                follow-up fields below; not a second data source. */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <SectionCard title="Today" icon={Sun} testId="pd-today">
                    <div className="space-y-1.5 text-xs">
                        {today.project_shoot_today && <div className="text-black/80">🎬 Shoot is today</div>}
                        {today.shoots.map((c) => <div key={`shoot-${c.talent_id}`} className="text-black/80">🎬 Shoot today — {c.name}</div>)}
                        {today.trials.map((c) => (
                            <div key={`trial-${c.talent_id}`} className="text-black/80">
                                👗 Costume trial — {c.name}{c.costume_trial_location ? ` (${c.costume_trial_location})` : ""}
                            </div>
                        ))}
                        {today.tasks.map((t) => (
                            <div key={t.id} className="text-black/80">✓ {t.title}{t.talent_name ? ` (${t.talent_name})` : ""}</div>
                        ))}
                        {today.payment_followup_due && <div className="text-amber-700">💰 Payment follow-up due today</div>}
                        {!today.project_shoot_today && today.shoots.length === 0 && today.trials.length === 0 && today.tasks.length === 0 && !today.payment_followup_due && (
                            <div className="text-black/40 py-2 text-center">Nothing scheduled today.</div>
                        )}
                    </div>
                </SectionCard>

                <SectionCard title="Upcoming" icon={CalendarClock} testId="pd-upcoming">
                    <div className="space-y-1.5 text-xs">
                        {upcoming.shoots.map((c) => <div key={`up-shoot-${c.talent_id}`} className="text-black/70">🎬 Shoot scheduled — {c.name}</div>)}
                        {upcoming.trials.map((c) => (
                            <div key={`up-trial-${c.talent_id}`} className="text-black/70">👗 Costume trial — {c.name} ({formatDate(c.costume_trial_at)})</div>
                        ))}
                        {upcoming.tasks.map((t) => (
                            <div key={t.id} className="text-black/70">✓ {t.title}{t.talent_name ? ` (${t.talent_name})` : ""} — due {formatDate(t.due_at)}</div>
                        ))}
                        {upcoming.payment_followup && <div className="text-black/70">💰 Payment follow-up — {formatDate(p.pd_next_follow_up_at)}</div>}
                        {upcoming.shoots.length === 0 && upcoming.trials.length === 0 && upcoming.tasks.length === 0 && !upcoming.payment_followup && (
                            <div className="text-black/40 py-2 text-center">Nothing upcoming.</div>
                        )}
                    </div>
                </SectionCard>
            </div>

            {/* Locked Talents */}
            <SectionCard title={`Locked Talents (${talents.length})`} icon={Users} testId="pd-locked-talents">
                {talents.length === 0 ? (
                    <div className="text-xs text-black/40 py-6 text-center">
                        No talents are locked on this project yet. Move a talent to <strong>Locked</strong> in Casting Pipeline for it to appear here.
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead className="text-xs">Talent</TableHead>
                                    <TableHead className="text-xs">Budget / Day</TableHead>
                                    <TableHead className="text-xs">Shoot Days</TableHead>
                                    <TableHead className="text-xs">Total Budget</TableHead>
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
                                            <InlineNumber value={t.budget_per_day} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { budget_per_day: v })} />
                                        </TableCell>
                                        <TableCell>
                                            <InlineNumber value={t.shooting_days} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { shooting_days: v })} />
                                        </TableCell>
                                        <TableCell>
                                            <InlineNumber value={t.budget_total} placeholder="—" onSave={(v) => patchTalent(t.talent_id, { budget_total: v })} />
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
                )}
            </SectionCard>

            {/* Talent Preparation — additive fields on the SAME locked
                casting_pipeline row Locked Talents above reads; no second
                talent/project relationship. V2: Fitting/Look Test/Shoot
                Status were REMOVED from this UI (spec section 6) — Fitting/
                Look Test stay fully alive server-side (Management Agent has
                real, working NLU commands and readiness checks against
                them — see production_desk.py's _talent_card docstring),
                simply not shown here; Shoot Status moved into the new
                Shooting Schedule section below, where it belongs with the
                actual per-day schedule. */}
            {talents.length > 0 && (
                <SectionCard title="Talent Preparation" icon={ListChecks} testId="pd-talent-prep">
                    <div className="overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead className="text-xs">Talent</TableHead>
                                    <TableHead className="text-xs">Costume Trial</TableHead>
                                    <TableHead className="text-xs">Trial Location</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {talents.map((t) => (
                                    <TableRow key={t.talent_id} data-testid={`pd-prep-row-${t.talent_id}`}>
                                        <TableCell className="text-xs font-medium text-black/80">{t.name}</TableCell>
                                        <TableCell>
                                            <Input
                                                type="date"
                                                defaultValue={toDateInputValue(t.costume_trial_at)}
                                                className="h-7 text-xs w-[130px]"
                                                onBlur={(e) => {
                                                    const iso = fromDateInputValue(e.target.value);
                                                    if (iso !== t.costume_trial_at) patchTalent(t.talent_id, { costume_trial_at: iso });
                                                }}
                                            />
                                        </TableCell>
                                        <TableCell>
                                            <div className="flex items-center gap-1.5">
                                                <Input
                                                    defaultValue={t.costume_trial_location || ""}
                                                    placeholder="Location"
                                                    className="h-7 text-xs w-[130px]"
                                                    onBlur={(e) => { if (e.target.value !== (t.costume_trial_location || "")) patchTalent(t.talent_id, { costume_trial_location: e.target.value }); }}
                                                />
                                                <Input
                                                    defaultValue={t.costume_trial_map_url || ""}
                                                    placeholder="Map URL (optional)"
                                                    className="h-7 text-xs w-[150px]"
                                                    onBlur={(e) => { if (e.target.value !== (t.costume_trial_map_url || "")) patchTalent(t.talent_id, { costume_trial_map_url: e.target.value }); }}
                                                />
                                                {t.costume_trial_map_url && (
                                                    <a href={t.costume_trial_map_url} target="_blank" rel="noreferrer" className="text-black/30 hover:text-[#0c2340] shrink-0">
                                                        <MapPin className="h-3.5 w-3.5" />
                                                    </a>
                                                )}
                                            </div>
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </div>
                </SectionCard>
            )}

            {/* V2 — Shooting Schedule (spec sections 1-4): per-talent,
                independently-manageable shoot-day records. Cards, not a
                giant table, so this stays usable on mobile (spec section
                31). "Use Project Dates" is the explicit, admin-tapped
                one-tap seed from Shoot Details' own structured dates below
                — never automatic, never overwrites the project's dates. */}
            {talents.length > 0 && (
                <SectionCard title="Shooting Schedule" icon={CalendarClock} testId="pd-shoot-schedule">
                    <div className="space-y-4">
                        {talents.map((t) => (
                            <TalentShootSchedule
                                key={t.talent_id}
                                talent={t}
                                projectShootDates={p.pd_shoot_dates_list || []}
                                onAdd={(payload) => addShootDay(t.talent_id, payload)}
                                onUpdate={(dayId, payload) => updateShootDay(t.talent_id, dayId, payload)}
                                onDelete={(dayId) => deleteShootDay(t.talent_id, dayId)}
                                onUseProjectDates={() => useProjectDatesForTalent(t.talent_id)}
                            />
                        ))}
                    </div>
                </SectionCard>
            )}

            {/* V2 — Readings & Rehearsals (spec section 8), under Talent
                Preparation conceptually but its own card for scannability. */}
            {talents.length > 0 && (
                <SectionCard title="Readings & Rehearsals" icon={Users} testId="pd-readings-rehearsals">
                    <div className="space-y-4">
                        {talents.map((t) => (
                            <TalentReadingsRehearsals
                                key={t.talent_id}
                                talent={t}
                                onAdd={(payload) => addReadingRehearsal(t.talent_id, payload)}
                                onDelete={(entryId) => deleteReadingRehearsal(t.talent_id, entryId)}
                            />
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
                    <div className="text-xs text-black/40 py-4 text-center">No open tasks.</div>
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

            {/* Production Budget & Shoot Details */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <SectionCard title="Production Budget" icon={Wallet} testId="pd-production-budget">
                    <div className="space-y-3">
                        <div>
                            <Label className="text-[11px] text-black/40">Budget / Day</Label>
                            <InlineNumber value={p.pd_production_budget_per_day} className="mt-1" onSave={(v) => patchProject({ production_budget_per_day: v })} />
                        </div>
                        <div>
                            <Label className="text-[11px] text-black/40">Total Budget</Label>
                            <InlineNumber value={p.pd_production_budget_total} className="mt-1" onSave={(v) => patchProject({ production_budget_total: v })} />
                        </div>
                        <div>
                            <Label className="text-[11px] text-black/40">Number of Shooting Days</Label>
                            <InlineNumber value={p.pd_shooting_days} className="mt-1" onSave={(v) => patchProject({ shooting_days: v })} />
                        </div>
                        {/* V2 (spec section 21) — auto-aggregated, never manually
                            typed. A SEPARATE total from the manual line above,
                            which stays exactly as it was. */}
                        <div className="pt-3 border-t border-black/[0.06] space-y-1.5" data-testid="pd-budget-auto-breakdown">
                            <Label className="text-[11px] text-black/40">Auto-Calculated (Talent + Overtime + Reimbursements)</Label>
                            <div className="flex justify-between text-xs text-black/60"><span>Talent Fees</span><span>{formatCurrency(summary.talent_budget_total)}</span></div>
                            <div className="flex justify-between text-xs text-black/60"><span>Extra Hours (Overtime)</span><span>{formatCurrency(summary.extra_hours_total)}</span></div>
                            <div className="flex justify-between text-xs text-black/60"><span>Reimbursements</span><span>{formatCurrency(summary.reimbursements_total)}</span></div>
                            <div className="flex justify-between text-xs font-semibold text-black/80 pt-1 border-t border-black/[0.05]"><span>Total</span><span>{formatCurrency(summary.total_talent_and_overtime_and_reimbursements)}</span></div>
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

                <SectionCard title="Shoot Details" icon={CalendarDays} testId="pd-shoot-details">
                    <div className="space-y-2.5 text-xs">
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Shooting Dates</span>
                            <Input defaultValue={p.shoot_dates || ""} placeholder="e.g. 26th - 27th August" className="h-7 text-xs max-w-[220px]" onBlur={(e) => { if (e.target.value !== (p.shoot_dates || "")) patchProject({ shoot_dates: e.target.value }); }} />
                        </div>
                        {/* V2 (spec section 4) — the proper structured multi-date
                            picker: add/remove ISO dates, stored structurally.
                            This is what a locked talent's own schedule can be
                            seeded from ("Use Project Dates" in Shooting
                            Schedule above) — independent of the free-text
                            field above and the single reminder-only date
                            below. */}
                        <div>
                            <span className="text-black/40 block mb-1">Shoot Dates (structured)</span>
                            <ShootDatesList dates={p.pd_shoot_dates_list || []} onChange={(dates) => patchProject({ shoot_dates_list: dates })} />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0" title="A single date used only to schedule shoot reminders — independent of the free-text Shooting Dates above.">Shoot Date (reminders)</span>
                            <Input type="date" defaultValue={p.pd_shoot_date || ""} className="h-7 text-xs max-w-[160px]" onBlur={(e) => { if (e.target.value !== (p.pd_shoot_date || "")) patchProject({ shoot_date: e.target.value || null }); }} />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Call Time</span>
                            <Input defaultValue={p.pd_call_time || ""} placeholder="e.g. 8:00 AM" className="h-7 text-xs max-w-[160px]" onBlur={(e) => { if (e.target.value !== (p.pd_call_time || "")) patchProject({ call_time: e.target.value }); }} />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Reporting Time</span>
                            <Input defaultValue={p.pd_reporting_time || ""} placeholder="e.g. 7:00 AM" className="h-7 text-xs max-w-[160px]" onBlur={(e) => { if (e.target.value !== (p.pd_reporting_time || "")) patchProject({ reporting_time: e.target.value }); }} />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Location</span>
                            <Input defaultValue={p.pd_shoot_location || ""} placeholder="Shoot location" className="h-7 text-xs max-w-[220px]" onBlur={(e) => { if (e.target.value !== (p.pd_shoot_location || "")) patchProject({ shoot_location: e.target.value }); }} />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Shoot Status</span>
                            <Select value={p.pd_shoot_status} onValueChange={(v) => patchProject({ shoot_status: v })}>
                                <SelectTrigger className="h-7 text-xs w-[130px]"><SelectValue /></SelectTrigger>
                                <SelectContent>
                                    {SHOOT_STATUS_OPTIONS.map((o) => <SelectItem key={o} value={o}>{o.replace("_", " ")}</SelectItem>)}
                                </SelectContent>
                            </Select>
                        </div>
                        {/* Production Contact moved to Payment Follow-up as
                            "Concerned Person" (spec section 16) — same
                            underlying pd_production_contact_client_id field,
                            just homed where it's actually used. Multiple
                            production contacts (spec section 28) already
                            exist as the Crew section further down — reused,
                            not duplicated here. */}
                        <div>
                            <span className="text-black/40 block mb-1">Notes</span>
                            <Textarea defaultValue={p.pd_shoot_notes || ""} rows={2} className="text-xs" onBlur={(e) => { if (e.target.value !== (p.pd_shoot_notes || "")) patchProject({ shoot_notes: e.target.value }); }} />
                        </div>
                    </div>
                </SectionCard>
            </div>

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
                    <Button size="sm" variant="outline" className="h-7 text-xs" onClick={sendPaymentFollowUp} data-testid="pd-whatsapp-followup-btn">
                        <MessageCircle className="h-3 w-3 mr-1" /> WhatsApp Follow-up
                    </Button>
                }
            >
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div className="sm:col-span-2">
                        <Label className="text-[11px] text-black/40">Concerned Person (CRM contact)</Label>
                        <div className="mt-1">
                            <ClientPicker
                                clients={clients}
                                placeholder={p.pd_production_contact?.name || "Search CRM contacts…"}
                                onContactCreated={(c) => setClients((prev) => [c, ...prev])}
                                onPicked={(c) => patchProject({ production_contact_client_id: c.id || c._id })}
                            />
                        </div>
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Payment Terms</Label>
                        <Input defaultValue={p.pd_payment_terms || ""} placeholder="e.g. 50% advance, 50% on delivery" className="h-8 text-xs mt-1" onBlur={(e) => { if (e.target.value !== (p.pd_payment_terms || "")) patchProject({ payment_terms: e.target.value }); }} />
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Status</Label>
                        <Select value={p.pd_payment_followup_status} onValueChange={(v) => patchProject({ payment_followup_status: v })}>
                            <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                            <SelectContent>
                                {PAYMENT_FOLLOWUP_STATUSES.map((o) => <SelectItem key={o} value={o}>{o.replace("_", " ")}</SelectItem>)}
                            </SelectContent>
                        </Select>
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Expected Date</Label>
                        <Input type="date" defaultValue={toDateInputValue(p.pd_expected_payment_date)} className="h-8 text-xs mt-1"
                            onBlur={(e) => { const iso = fromDateInputValue(e.target.value); if (iso !== p.pd_expected_payment_date) patchProject({ expected_payment_date: iso }); }} />
                    </div>
                    <div>
                        <Label className="text-[11px] text-black/40">Next Follow-up</Label>
                        <Input type="date" defaultValue={toDateInputValue(p.pd_next_follow_up_at)} className="h-8 text-xs mt-1"
                            onBlur={(e) => { const iso = fromDateInputValue(e.target.value); if (iso !== p.pd_next_follow_up_at) patchProject({ next_follow_up_at: iso, last_follow_up_at: new Date().toISOString() }); }} />
                    </div>
                    <div className="sm:col-span-2">
                        <Label className="text-[11px] text-black/40">Notes</Label>
                        <Textarea defaultValue={p.pd_payment_followup_notes || ""} rows={2} className="text-xs mt-1" onBlur={(e) => { if (e.target.value !== (p.pd_payment_followup_notes || "")) patchProject({ payment_followup_notes: e.target.value }); }} />
                    </div>
                </div>
                {p.pd_last_follow_up_at && (
                    <div className="mt-3 text-[11px] text-black/40">Last followed up: {formatDate(p.pd_last_follow_up_at)}</div>
                )}
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

            {/* Crew */}
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
                        {crew.map((c) => (
                            <div key={c.id} className="flex items-center justify-between text-xs rounded-md border border-black/[0.06] px-3 py-2" data-testid={`pd-crew-${c.id}`}>
                                <div>
                                    <div className="font-medium text-black/75">{c.contact?.name || "Unnamed"}</div>
                                    <div className="text-black/40">{c.role}</div>
                                </div>
                                <button onClick={() => deleteCrew(c.id)} className="text-black/30 hover:text-red-500">
                                    <Trash2 className="h-3 w-3" />
                                </button>
                            </div>
                        ))}
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
    );
}

// ============================================================================
// V2 — Structured multi-date shoot picker (spec section 4). Add/remove
// ISO dates, stored structurally — no free text, no calendar-grid widget
// (deliberately the lightest implementation that satisfies "select one,
// select multiple, add/remove, see selected clearly").
// ============================================================================
function ShootDatesList({ dates, onChange }) {
    const [newDate, setNewDate] = useState("");
    const sorted = [...dates].sort();
    const addDate = () => {
        if (!newDate || dates.includes(newDate)) return;
        onChange([...dates, newDate].sort());
        setNewDate("");
    };
    const removeDate = (d) => onChange(dates.filter((x) => x !== d));
    return (
        <div data-testid="pd-shoot-dates-list">
            <div className="flex flex-wrap gap-1.5 mb-2">
                {sorted.length === 0 && <span className="text-black/30 italic">No dates set</span>}
                {sorted.map((d) => (
                    <span key={d} className="inline-flex items-center gap-1 px-2 py-1 bg-slate-50 border border-black/[0.08] rounded-full text-[11px] text-black/70" data-testid={`pd-shoot-date-chip-${d}`}>
                        {d}
                        <button type="button" onClick={() => removeDate(d)} className="text-black/30 hover:text-red-500"><X className="h-3 w-3" /></button>
                    </span>
                ))}
            </div>
            <div className="flex items-center gap-1.5">
                <Input type="date" value={newDate} onChange={(e) => setNewDate(e.target.value)} className="h-7 text-xs w-[150px]" />
                <Button size="sm" variant="outline" className="h-7 text-xs" onClick={addDate} disabled={!newDate}>
                    <Plus className="h-3 w-3 mr-1" /> Add Date
                </Button>
            </div>
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

function TalentShootSchedule({ talent, projectShootDates, onAdd, onUpdate, onDelete, onUseProjectDates }) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState({ date: "", call_time: "", reporting_time: "", location: "", location_map_url: "", agreed_hours: "", actual_hours: "" });

    const submit = () => {
        if (!form.date) return;
        onAdd({
            date: form.date,
            call_time: form.call_time || null,
            reporting_time: form.reporting_time || null,
            location: form.location || null,
            location_map_url: form.location_map_url || null,
            agreed_hours: form.agreed_hours === "" ? null : Number(form.agreed_hours),
            actual_hours: form.actual_hours === "" ? null : Number(form.actual_hours),
        });
        setForm({ date: "", call_time: "", reporting_time: "", location: "", location_map_url: "", agreed_hours: "", actual_hours: "" });
        setAdding(false);
    };

    return (
        <div className="rounded-lg border border-black/[0.08] p-3" data-testid={`pd-shoot-schedule-${talent.talent_id}`}>
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-xs font-semibold text-black/80">{talent.name}</span>
                <div className="flex items-center gap-1.5">
                    {projectShootDates.length > 0 && (
                        <Button size="sm" variant="ghost" className="h-6 text-[10px] px-2" onClick={onUseProjectDates} data-testid={`pd-use-project-dates-${talent.talent_id}`}>
                            Use Project Dates
                        </Button>
                    )}
                    <Button size="sm" variant="outline" className="h-6 text-[10px] px-2" onClick={() => setAdding((v) => !v)}>
                        <Plus className="h-3 w-3 mr-1" /> Add Date
                    </Button>
                </div>
            </div>

            {talent.shoot_days.length === 0 && !adding && (
                <div className="text-[11px] text-black/30 italic py-2">No shoot days scheduled.</div>
            )}

            <div className="space-y-1.5">
                {talent.shoot_days.map((d) => {
                    const extra = _dayExtraHours(talent.budget_per_day, d.agreed_hours, d.actual_hours);
                    return (
                        <div key={d.id} className="grid grid-cols-2 sm:grid-cols-6 gap-1.5 items-center text-[11px] bg-slate-50/60 rounded-md p-2" data-testid={`pd-shoot-day-${d.id}`}>
                            <div className="font-medium text-black/70">{d.date}</div>
                            <Input defaultValue={d.call_time || ""} placeholder="Call" className="h-6 text-[11px]" onBlur={(e) => { if (e.target.value !== (d.call_time || "")) onUpdate(d.id, { call_time: e.target.value }); }} />
                            <Input defaultValue={d.reporting_time || ""} placeholder="Reporting" className="h-6 text-[11px]" onBlur={(e) => { if (e.target.value !== (d.reporting_time || "")) onUpdate(d.id, { reporting_time: e.target.value }); }} />
                            <Input defaultValue={d.location || ""} placeholder="Location" className="h-6 text-[11px]" onBlur={(e) => { if (e.target.value !== (d.location || "")) onUpdate(d.id, { location: e.target.value }); }} />
                            <div className="flex items-center gap-1">
                                <Input type="number" defaultValue={d.agreed_hours ?? ""} placeholder="Basis h" className="h-6 text-[11px] w-14" onBlur={(e) => { const v = e.target.value === "" ? null : Number(e.target.value); if (v !== d.agreed_hours) onUpdate(d.id, { agreed_hours: v }); }} />
                                <Input type="number" defaultValue={d.actual_hours ?? ""} placeholder="Actual h" className="h-6 text-[11px] w-14" onBlur={(e) => { const v = e.target.value === "" ? null : Number(e.target.value); if (v !== d.actual_hours) onUpdate(d.id, { actual_hours: v }); }} />
                            </div>
                            <div className="flex items-center justify-between gap-1">
                                <div className="flex flex-col">
                                    <Select value={d.shoot_status || "scheduled"} onValueChange={(v) => onUpdate(d.id, { shoot_status: v })}>
                                        <SelectTrigger className="h-6 text-[10px] w-[92px]"><SelectValue /></SelectTrigger>
                                        <SelectContent>
                                            {SHOOT_STATUS_OPTIONS.map((o) => <SelectItem key={o} value={o}>{o.replace("_", " ")}</SelectItem>)}
                                        </SelectContent>
                                    </Select>
                                    {extra.hours > 0 && (
                                        <span className="text-amber-700 mt-0.5">+{extra.hours}h · {formatCurrency(extra.amount)}</span>
                                    )}
                                </div>
                                <button onClick={() => onDelete(d.id)} className="text-black/30 hover:text-red-500 shrink-0"><Trash2 className="h-3 w-3" /></button>
                            </div>
                        </div>
                    );
                })}
            </div>

            {adding && (
                <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-1.5 items-end bg-white border border-black/[0.06] rounded-md p-2">
                    <div><Label className="text-[10px]">Date</Label><Input type="date" value={form.date} onChange={(e) => setForm((f) => ({ ...f, date: e.target.value }))} className="h-7 text-[11px]" autoFocus /></div>
                    <div><Label className="text-[10px]">Call Time</Label><Input value={form.call_time} onChange={(e) => setForm((f) => ({ ...f, call_time: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Reporting</Label><Input value={form.reporting_time} onChange={(e) => setForm((f) => ({ ...f, reporting_time: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Location</Label><Input value={form.location} onChange={(e) => setForm((f) => ({ ...f, location: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Agreed Hours (basis)</Label><Input type="number" value={form.agreed_hours} onChange={(e) => setForm((f) => ({ ...f, agreed_hours: e.target.value }))} placeholder="e.g. 12" className="h-7 text-[11px]" /></div>
                    <div><Label className="text-[10px]">Actual Hours</Label><Input type="number" value={form.actual_hours} onChange={(e) => setForm((f) => ({ ...f, actual_hours: e.target.value }))} className="h-7 text-[11px]" /></div>
                    <div className="col-span-2 flex justify-end gap-1.5">
                        <Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={() => setAdding(false)}>Cancel</Button>
                        <Button size="sm" className="h-7 text-[11px]" disabled={!form.date} onClick={submit}>Save</Button>
                    </div>
                </div>
            )}
        </div>
    );
}

// ============================================================================
// V2 — Readings & Rehearsals (spec section 8).
// ============================================================================
function TalentReadingsRehearsals({ talent, onAdd, onDelete }) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState({ type: "reading", date: "", time: "", location: "", notes: "" });

    const submit = () => {
        onAdd({
            type: form.type, date: form.date || null, time: form.time || null,
            location: form.location || null, notes: form.notes || null,
        });
        setForm({ type: "reading", date: "", time: "", location: "", notes: "" });
        setAdding(false);
    };

    return (
        <div className="rounded-lg border border-black/[0.08] p-3" data-testid={`pd-readings-${talent.talent_id}`}>
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-xs font-semibold text-black/80">{talent.name}</span>
                <Button size="sm" variant="outline" className="h-6 text-[10px] px-2" onClick={() => setAdding((v) => !v)}>
                    <Plus className="h-3 w-3 mr-1" /> Add
                </Button>
            </div>
            {talent.readings_rehearsals.length === 0 && !adding && (
                <div className="text-[11px] text-black/30 italic py-1">No readings or rehearsals scheduled.</div>
            )}
            <div className="space-y-1">
                {talent.readings_rehearsals.map((e) => (
                    <div key={e.id} className="flex items-center justify-between text-[11px] bg-slate-50/60 rounded-md px-2 py-1.5" data-testid={`pd-reading-${e.id}`}>
                        <div className="flex items-center gap-2 flex-wrap">
                            <Badge variant="outline" className="text-[10px] capitalize">{e.type}</Badge>
                            {e.date && <span className="text-black/60">{e.date}</span>}
                            {e.time && <span className="text-black/50">{e.time}</span>}
                            {e.location && <span className="text-black/50">· {e.location}</span>}
                            {e.notes && <span className="text-black/40">({e.notes})</span>}
                        </div>
                        <button onClick={() => onDelete(e.id)} className="text-black/30 hover:text-red-500 shrink-0"><Trash2 className="h-3 w-3" /></button>
                    </div>
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
                    <div><Label className="text-[10px]">Location</Label><Input value={form.location} onChange={(e) => setForm((f) => ({ ...f, location: e.target.value }))} className="h-7 text-[11px]" /></div>
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
            <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-xs font-semibold text-black/80">{talent.name}</span>
                <Button size="sm" variant="outline" className="h-6 text-[10px] px-2" onClick={onAskInvoice} data-testid={`pd-ask-invoice-${talent.talent_id}`}>
                    <MessageCircle className="h-3 w-3 mr-1" /> Ask Talent to Raise Invoice
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
function PaymentTranchesSection({ tranches, onAdd, onUpdate, onDelete, totals }) {
    const [adding, setAdding] = useState(false);
    const [form, setForm] = useState({ name: "", amount: "", trigger: "" });

    const submit = () => {
        if (!form.name.trim() || !form.amount) return;
        onAdd({ name: form.name.trim(), amount: Number(form.amount), trigger: form.trigger || null });
        setForm({ name: "", amount: "", trigger: "" });
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
                        <div key={t.id} className="rounded-md border border-black/[0.06] p-2.5" data-testid={`pd-tranche-${t.id}`}>
                            <div className="flex items-center justify-between gap-2 mb-1.5">
                                <span className="text-xs font-semibold text-black/80">{i + 1}. {t.name}</span>
                                <div className="flex items-center gap-2">
                                    <span className="text-xs font-semibold text-black/70">{formatCurrency(t.amount)}</span>
                                    <button onClick={() => onDelete(t.id)} className="text-black/30 hover:text-red-500"><Trash2 className="h-3 w-3" /></button>
                                </div>
                            </div>
                            {t.trigger && <div className="text-[11px] text-black/40 mb-1.5">{t.trigger}</div>}
                            <div className="flex flex-wrap items-center gap-2">
                                <Select value={t.invoice_status} onValueChange={(v) => onUpdate(t.id, { invoice_status: v })}>
                                    <SelectTrigger className={`h-6 text-[10px] w-[110px] ${t.invoice_status === "raised_and_sent" ? "text-emerald-700" : "text-amber-700"}`}><SelectValue /></SelectTrigger>
                                    <SelectContent>
                                        {TRANCHE_INVOICE_STATUSES.map((o) => <SelectItem key={o.value} value={o.value}>Invoice: {o.label}</SelectItem>)}
                                    </SelectContent>
                                </Select>
                                <Select value={t.payment_status} onValueChange={(v) => onUpdate(t.id, { payment_status: v })}>
                                    <SelectTrigger className={`h-6 text-[10px] w-[110px] ${t.payment_status === "received" ? "text-emerald-700" : "text-amber-700"}`}><SelectValue /></SelectTrigger>
                                    <SelectContent>
                                        {TRANCHE_PAYMENT_STATUSES.map((o) => <SelectItem key={o.value} value={o.value}>Payment: {o.label}</SelectItem>)}
                                    </SelectContent>
                                </Select>
                            </div>
                        </div>
                    ))}
                </div>
            )}
            {adding && (
                <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 items-end bg-slate-50/60 border border-black/[0.06] rounded-md p-3">
                    <div><Label className="text-[10px]">Name / Milestone</Label><Input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Signing" className="h-8 text-xs" autoFocus /></div>
                    <div><Label className="text-[10px]">Amount</Label><Input type="number" value={form.amount} onChange={(e) => setForm((f) => ({ ...f, amount: e.target.value }))} className="h-8 text-xs" /></div>
                    <div><Label className="text-[10px]">Trigger (optional)</Label><Input value={form.trigger} onChange={(e) => setForm((f) => ({ ...f, trigger: e.target.value }))} placeholder="e.g. On shoot commencement" className="h-8 text-xs" /></div>
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

function CrewForm({ clients, onContactCreated, onSubmit }) {
    const [contact, setContact] = useState(null);
    const [role, setRole] = useState(CREW_ROLES[0]);
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
                    <SelectTrigger className="h-8 text-xs mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {CREW_ROLES.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
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
