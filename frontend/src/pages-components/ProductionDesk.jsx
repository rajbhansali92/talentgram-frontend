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
    ClipboardList, Wallet, UserPlus,
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

    const { project: p, locked_talents: talents, summary, needs_attention, kickbacks, reimbursements, crew, documents, finance } = data;

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
                        <div className="flex justify-between"><span className="text-black/40">Shooting Dates</span><span className="text-black/70">{p.shoot_dates || "—"}</span></div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Call Time</span>
                            <Input defaultValue={p.pd_call_time || ""} placeholder="e.g. 8:00 AM" className="h-7 text-xs max-w-[160px]" onBlur={(e) => { if (e.target.value !== (p.pd_call_time || "")) patchProject({ call_time: e.target.value }); }} />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-black/40 shrink-0">Location</span>
                            <Input defaultValue={p.pd_shoot_location || ""} placeholder="Shoot location" className="h-7 text-xs max-w-[220px]" onBlur={(e) => { if (e.target.value !== (p.pd_shoot_location || "")) patchProject({ shoot_location: e.target.value }); }} />
                        </div>
                        <div>
                            <span className="text-black/40 block mb-1">Production Contact</span>
                            <ClientPicker
                                clients={clients}
                                placeholder={p.pd_production_contact?.name || "Search CRM contacts…"}
                                onContactCreated={(c) => setClients((prev) => [c, ...prev])}
                                onPicked={(c) => patchProject({ production_contact_client_id: c.id || c._id })}
                            />
                        </div>
                        <div>
                            <span className="text-black/40 block mb-1">Notes</span>
                            <Textarea defaultValue={p.pd_shoot_notes || ""} rows={2} className="text-xs" onBlur={(e) => { if (e.target.value !== (p.pd_shoot_notes || "")) patchProject({ shoot_notes: e.target.value }); }} />
                        </div>
                    </div>
                </SectionCard>
            </div>

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

            {/* Project Checklist */}
            <SectionCard title="Project Checklist" icon={ClipboardList} testId="pd-checklist">
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    {[
                        { key: "confirmation_mail_received", label: "Confirmation Mail Received", val: p.pd_confirmation_mail_received },
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

            {/* Project Requirements / Usage — display only, reuses existing Project Details fields */}
            <SectionCard title="Project Requirements & Usage" icon={FileText} testId="pd-requirements">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                    <div><span className="text-black/40 block">Medium / Usage</span><span className="text-black/70">{p.medium_usage || "—"}</span></div>
                    <div><span className="text-black/40 block">Director</span><span className="text-black/70">{p.director || "—"}</span></div>
                    <div><span className="text-black/40 block">Production House</span><span className="text-black/70">{p.production_house || "—"}</span></div>
                    <div><span className="text-black/40 block">Competitive Brand Restriction</span><span className="text-black/70">{p.competitive_brand_enabled ? "Yes" : "No"}</span></div>
                    {p.additional_details && (
                        <div className="sm:col-span-2"><span className="text-black/40 block">Additional Details</span><span className="text-black/70 whitespace-pre-wrap">{p.additional_details}</span></div>
                    )}
                </div>
            </SectionCard>

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

            {quickViewTalent && (
                <TalentPreviewDrawer talent={quickViewTalent} onClose={() => setQuickViewTalent(null)} isMobile={isMobile} />
            )}
        </div>
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
