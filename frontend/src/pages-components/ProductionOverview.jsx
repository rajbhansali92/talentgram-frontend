/* ---------------------------------------------------------------------
 * Production Desk (global) — every project that has a LOCKED talent, in one operational view.
 *
 * This page is a LENS over the existing per-project Production Desk, not a second system:
 *   - the list comes from GET /production/overview, which builds each row with the very same
 *     backend helpers the project page uses (so a row's numbers always equal the project page's),
 *     and filters / sorts / paginates on the server;
 *   - expanding a row loads the existing GET /projects/{id}/production-desk (detail only when asked);
 *   - "Ask to Raise Invoice" and "Payment Follow-up" are the SAME shared actions the project page
 *     uses (@/lib/productionDesk) — the invoice request is talent-facing (talent rate only) and the
 *     follow-up is production-facing, reviewed before WhatsApp opens;
 *   - "Open Project" goes to the full project workspace, which is unchanged.
 * Filters live in the URL, so refresh, back/forward and shared links all work.
 * ------------------------------------------------------------------- */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import {
    formatCurrency, formatDate, formatClock, LocationLink, askTalentToRaiseInvoice,
    usePaymentFollowUp, PaymentRecipientSelect, FollowUpReviewDialog,
} from "@/lib/productionDesk";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import {
    Loader2, AlertTriangle, ChevronDown, ChevronUp, Search, X, SlidersHorizontal, ExternalLink,
    MessageCircle, Sun, CalendarClock, CheckCircle2, Film, Receipt, IndianRupee,
} from "lucide-react";

const DEFAULTS = { status: "active", sort: "priority" };
const FILTER_KEYS = ["q", "status", "stage", "shoot_state", "shoot_date", "payment", "attention", "client", "sort"];
const PAGE_SIZE = 20;

const STATUS_OPTIONS = [
    { value: "all", label: "All projects" },
    { value: "ongoing", label: "Ongoing" }, { value: "hold", label: "On hold" },
    { value: "locked", label: "Locked" }, { value: "complete", label: "Completed" },
];
const STAGE_OPTIONS = [
    { value: "not_started", label: "Not started" }, { value: "confirmed", label: "Confirmed" },
    { value: "shoot_scheduled", label: "Shoot scheduled" }, { value: "shoot_complete", label: "Shoot complete" },
    { value: "finance_closed", label: "Finance closed" },
];
const SHOOT_STATE_OPTIONS = [
    { value: "not_scheduled", label: "Not scheduled" }, { value: "scheduled", label: "Scheduled" },
    { value: "today", label: "Shooting today" }, { value: "completed", label: "Completed" },
];
const SHOOT_DATE_OPTIONS = [
    { value: "today", label: "Today" }, { value: "upcoming", label: "Upcoming" },
    { value: "past", label: "Past" }, { value: "none", label: "No shoot date" },
];
const PAYMENT_OPTIONS = [
    { value: "client_pending", label: "Client payment pending" }, { value: "client_partial", label: "Client partially received" },
    { value: "client_received", label: "Client fully received" }, { value: "talent_pending", label: "Talent payments pending" },
    { value: "talent_done", label: "Talent payments completed" },
];
const ATTENTION_OPTIONS = [{ value: "needs", label: "Needs attention" }, { value: "clear", label: "No issues" }];
const SORT_OPTIONS = [
    { value: "shoot", label: "Next shoot date" },
    { value: "outstanding", label: "Client outstanding" }, { value: "earnings", label: "Talentgram earnings" },
    { value: "name", label: "Project name" },
];

const dash = (v) => (v === null || v === undefined ? "—" : formatCurrency(v));

const SHOOT_LABEL = { not_scheduled: "Not scheduled", scheduled: "Scheduled", today: "Shooting today", completed: "Completed" };
const CLIENT_PAY = {
    pending: { label: "Pending", cls: "bg-amber-50 text-amber-800 border-amber-200" },
    partial: { label: "Partly received", cls: "bg-sky-50 text-sky-800 border-sky-200" },
    received: { label: "Received", cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
    unknown: { label: "No quote yet", cls: "bg-slate-50 text-slate-500 border-slate-200" },
};

function Pill({ cls, children, testId }) {
    return <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${cls}`} data-testid={testId}>{children}</span>;
}

function SummaryTile({ label, value, tone, testId }) {
    const toneCls = { warn: "text-amber-700", good: "text-emerald-700", hot: "text-red-600" }[tone] || "text-black/85";
    return (
        <div className="rounded-lg border border-black/[0.08] bg-white px-3 py-2.5 min-w-0">
            <div className="text-[10px] uppercase tracking-wide text-black/40 truncate">{label}</div>
            <div className={`mt-0.5 text-base font-semibold truncate ${toneCls}`} data-testid={testId}>{value}</div>
        </div>
    );
}

function FilterSelect({ label, value, onChange, options, testId, allLabel = "All" }) {
    return (
        <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-wide text-black/40 mb-1">{label}</div>
            <Select value={value || "__all"} onValueChange={(v) => onChange(v === "__all" ? "" : v)}>
                <SelectTrigger className="h-8 text-xs w-full" data-testid={testId}><SelectValue /></SelectTrigger>
                <SelectContent>
                    <SelectItem value="__all">{allLabel}</SelectItem>
                    {options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
                </SelectContent>
            </Select>
        </div>
    );
}

// ── One project's follow-up: pick the recipient, preview, confirm. Mounted only while open, so
//    contacts are fetched for the one project the user is acting on, never for every row. ─────
function FollowUpDialog({ project, onClose }) {
    const followup = usePaymentFollowUp(project.project_id, {
        onConfirmed: async () => {
            try {
                await adminApi.patch(`/projects/${project.project_id}/production-desk`, { last_follow_up_at: new Date().toISOString() });
            } catch (err) {
                toast.error(formatErrorDetail(err) || "Opened WhatsApp, but could not record the follow-up");
            }
            onClose();
        },
    });
    return (
        <>
            <Dialog open={!followup.preview} onOpenChange={(o) => { if (!o) onClose(); }}>
                <DialogContent className="max-w-md" data-testid="po-followup-picker">
                    <DialogHeader><DialogTitle>Payment follow-up · {project.brand_name}</DialogTitle></DialogHeader>
                    <div className="space-y-3 text-xs">
                        <div className="text-black/50">
                            Outstanding from the production: <span className="font-semibold text-black/80">{dash(project.client_payment.outstanding)}</span>
                        </div>
                        <div>
                            <div className="text-black/40 mb-1">Send follow-up to</div>
                            <PaymentRecipientSelect contacts={followup.contacts} value={followup.contactId} onChange={followup.setContactId} className="h-8 text-xs w-full" />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
                        <Button size="sm" onClick={followup.requestPreview} disabled={!followup.contactId} data-testid="po-followup-preview">Preview message</Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
            <FollowUpReviewDialog preview={followup.preview} onCancel={followup.cancel} onConfirm={followup.confirm} />
        </>
    );
}

// ── The expanded view: the existing per-project desk response, rendered compactly ───────────
function ProjectDetail({ project, onFollowUp }) {
    const [state, setState] = useState({ loading: true, data: null, error: false });
    useEffect(() => {
        let cancelled = false;
        adminApi.get(`/projects/${project.project_id}/production-desk`)
            .then(({ data }) => { if (!cancelled) setState({ loading: false, data, error: false }); })
            .catch((err) => { if (!cancelled) { setState({ loading: false, data: null, error: true }); toast.error(formatErrorDetail(err) || "Could not load project details"); } });
        return () => { cancelled = true; };
    }, [project.project_id]);

    if (state.loading) return <div className="flex items-center justify-center py-8 text-black/40 text-xs"><Loader2 className="h-4 w-4 animate-spin mr-2" /> Loading details…</div>;
    if (state.error || !state.data) return <div className="py-6 text-center text-xs text-black/40">Could not load this project's production details.</div>;

    const { locked_talents: talents, summary: s } = state.data;
    const days = talents.flatMap((t) => (t.shoot_days || []).map((d) => ({ ...d, talent: t.name })))
        .sort((a, b) => `${a.date}${a.call_time || ""}`.localeCompare(`${b.date}${b.call_time || ""}`));
    const openTasks = state.data.tasks?.pending?.length ?? 0;
    const overdueTasks = state.data.tasks?.overdue?.length ?? 0;

    return (
        <div className="space-y-4 text-xs" data-testid={`po-detail-${project.project_id}`}>
            <div className="flex flex-wrap items-center gap-2" data-testid={`po-actions-${project.project_id}`}>
                <Button asChild size="sm" className="h-8 text-xs">
                    <Link to={`/admin/projects/${project.project_id}?tab=production`} data-testid={`po-open-project-${project.project_id}`}><ExternalLink className="h-3 w-3 mr-1" /> Open Project</Link>
                </Button>
                <Button size="sm" variant="outline" className="h-8 text-xs" onClick={onFollowUp} data-testid={`po-followup-${project.project_id}`}>
                    <MessageCircle className="h-3 w-3 mr-1" /> Payment Follow-up
                </Button>
                <Button asChild size="sm" variant="outline" className="h-8 text-xs">
                    <Link to={`/admin/projects/${project.project_id}?tab=production`} data-testid={`po-edit-${project.project_id}`}>Edit production details</Link>
                </Button>
                <span className="text-black/40 ml-auto">{openTasks} open task{openTasks !== 1 ? "s" : ""}{overdueTasks ? ` · ${overdueTasks} overdue` : ""}</span>
            </div>

            <div>
                <div className="text-[10px] font-semibold uppercase tracking-wide text-black/40 mb-1.5">Locked talents</div>
                <div className="hidden lg:block overflow-x-auto rounded-md border border-black/[0.06]">
                    <table className="w-full text-xs">
                        <thead className="bg-slate-50/70 text-black/45">
                            <tr>{["Talent", "Talent rate", "Production quote", "Commission", "Spread", "OT", "Reimb.", "Talent net", "Payment", ""].map((h) => <th key={h} className="px-2.5 py-1.5 text-left font-medium whitespace-nowrap">{h}</th>)}</tr>
                        </thead>
                        <tbody>
                            {talents.map((t) => (
                                <tr key={t.talent_id} className="border-t border-black/[0.05]" data-testid={`po-talent-${t.talent_id}`}>
                                    <td className="px-2.5 py-1.5 font-medium text-black/80">{t.name}</td>
                                    <td className="px-2.5 py-1.5">{dash(t.talent_agreed_rate)}</td>
                                    <td className="px-2.5 py-1.5">{t.production_quote == null ? <span className="text-amber-700">Not entered</span> : formatCurrency(t.production_quote)}</td>
                                    <td className="px-2.5 py-1.5">{dash(t.commission_amount)}</td>
                                    <td className={`px-2.5 py-1.5 ${t.spread < 0 ? "text-red-600" : ""}`}>{dash(t.spread)}</td>
                                    <td className="px-2.5 py-1.5">{formatCurrency(t.extra_hours_total || 0)}</td>
                                    <td className="px-2.5 py-1.5">{formatCurrency(t.reimbursement_total || 0)}</td>
                                    <td className="px-2.5 py-1.5">{dash(t.talent_net_payable)}</td>
                                    <td className="px-2.5 py-1.5"><Pill cls={t.payment_status === "cleared" ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-amber-50 text-amber-800 border-amber-200"}>{t.payment_status === "cleared" ? "Cleared" : "Pending"}</Pill></td>
                                    <td className="px-2.5 py-1.5 text-right"><Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={() => askTalentToRaiseInvoice(project.project_id, t.talent_id)} data-testid={`po-invoice-${t.talent_id}`}><Receipt className="h-3 w-3 mr-1" /> Ask to Raise Invoice</Button></td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
                <div className="lg:hidden space-y-2">
                    {talents.map((t) => (
                        <div key={t.talent_id} className="rounded-md border border-black/[0.08] p-2.5" data-testid={`po-talent-mobile-${t.talent_id}`}>
                            <div className="flex items-center justify-between gap-2 mb-1.5">
                                <span className="font-medium text-black/80 truncate">{t.name}</span>
                                <Pill cls={t.payment_status === "cleared" ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-amber-50 text-amber-800 border-amber-200"}>{t.payment_status === "cleared" ? "Cleared" : "Pending"}</Pill>
                            </div>
                            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                                <div><span className="text-black/40">Rate </span>{dash(t.talent_agreed_rate)}</div>
                                <div><span className="text-black/40">Quote </span>{t.production_quote == null ? <span className="text-amber-700">Not entered</span> : formatCurrency(t.production_quote)}</div>
                                <div><span className="text-black/40">Commission </span>{dash(t.commission_amount)}</div>
                                <div><span className="text-black/40">Spread </span>{dash(t.spread)}</div>
                                <div><span className="text-black/40">OT </span>{formatCurrency(t.extra_hours_total || 0)}</div>
                                <div><span className="text-black/40">Reimb. </span>{formatCurrency(t.reimbursement_total || 0)}</div>
                            </div>
                            <Button size="sm" variant="outline" className="h-7 text-[11px] mt-2" onClick={() => askTalentToRaiseInvoice(project.project_id, t.talent_id)}><Receipt className="h-3 w-3 mr-1" /> Ask to Raise Invoice</Button>
                        </div>
                    ))}
                </div>
            </div>

            <div data-testid={`po-schedule-${project.project_id}`}>
                <div className="text-[10px] font-semibold uppercase tracking-wide text-black/40 mb-1.5">Shoot schedule</div>
                {days.length === 0 ? <div className="text-black/35 italic">No shoot days scheduled.</div> : (
                    <div className="space-y-1">
                        {days.map((d) => (
                            <div key={d.id} className="grid grid-cols-[auto_1fr] sm:grid-cols-[88px_1fr_110px_110px_1fr] gap-x-3 gap-y-0.5 rounded-md bg-slate-50/60 px-2.5 py-1.5 items-center">
                                <span className="font-medium text-black/75">{formatDate(d.date)}</span>
                                <span className="text-black/60 truncate">{d.talent}</span>
                                <span className="text-black/50 col-span-2 sm:col-span-1"><span className="text-black/35">Report </span>{formatClock(d.reporting_time)}</span>
                                <span className="text-black/50 col-span-2 sm:col-span-1"><span className="text-black/35">Call </span>{formatClock(d.call_time)}</span>
                                <span className="col-span-2 sm:col-span-1 min-w-0"><LocationLink name={d.location} mapUrl={d.location_map_url} address={d.location_address} /></span>
                            </div>
                        ))}
                    </div>
                )}
            </div>

            <div data-testid={`po-financials-${project.project_id}`}>
                <div className="text-[10px] font-semibold uppercase tracking-wide text-black/40 mb-1.5">Financial summary</div>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-1">
                    {[
                        ["Production quote", dash(project.money.production_quote)], ["Talent agreed rates", dash(s.talent_agreed_total)],
                        ["Commission", dash(s.commission_gross)], ["Additional spread", dash(s.spread_total)],
                        ["Overtime", dash(s.production_overtime_total)], ["Reimbursements", dash(s.production_reimbursements_total)],
                        ["Talentgram earnings", dash(s.talentgram_earnings_total)], ["Client total", dash(s.production_billable_total)],
                        ["Client received", dash(s.client_received_total)], ["Client outstanding", s.production_basis === "partial" ? "Incomplete" : dash(s.client_outstanding_total)],
                        ["Talent paid", dash(s.talent_paid_total)], ["Talent pending", dash(s.talent_pending_total)],
                    ].map(([k, v]) => (
                        <div key={k} className="flex items-baseline justify-between gap-2"><span className="text-black/45">{k}</span><span className="font-medium text-black/80">{v}</span></div>
                    ))}
                </div>
            </div>
        </div>
    );
}

// ── One project: a row on desktop, a stacked card below lg ─────────────────────────────────
function ProjectRow({ project: p, expanded, onToggle, onFollowUp }) {
    const shoot = p.shoot;
    const cp = CLIENT_PAY[p.client_payment.state] || CLIENT_PAY.unknown;
    const allPaid = p.talent_payment.total > 0 && p.talent_payment.cleared >= p.talent_payment.total;
    const shootCell = (tid) => (
        <div className="min-w-0">
            <div className={`text-xs ${shoot.state === "today" ? "font-semibold text-red-600" : "text-black/70"}`} data-testid={tid}>{SHOOT_LABEL[shoot.state]}</div>
            {shoot.next_date && <div className="text-[11px] text-black/45 truncate">{formatDate(shoot.next_date)}{shoot.next_location ? ` · ${shoot.next_location}` : ""}</div>}
        </div>
    );
    const attention = (tid) => p.attention_count > 0 && (
        <Pill cls="bg-amber-50 text-amber-800 border-amber-200" testId={tid}><AlertTriangle className="h-3 w-3 mr-0.5" />{p.attention_count}</Pill>
    );
    return (
        <div className={`rounded-lg border bg-white ${shoot.state === "today" ? "border-red-200" : "border-black/[0.08]"}`} data-testid={`po-row-${p.project_id}`}>
            {/* desktop */}
            <button type="button" onClick={onToggle} aria-expanded={expanded} className="hidden lg:grid w-full grid-cols-[minmax(0,2.2fr)_56px_minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.1fr)_70px_56px_28px] gap-x-3 items-center px-3 py-3 text-left hover:bg-slate-50/60" data-testid={`po-toggle-${p.project_id}`}>
                <div className="min-w-0">
                    <div className="text-sm font-semibold text-black/85 truncate" data-testid={`po-name-${p.project_id}`}>{p.brand_name}</div>
                    <div className="text-[11px] text-black/45 truncate">{p.client.label || "—"} <span className="capitalize">· {p.status}</span></div>
                </div>
                <div className="text-xs text-black/70" title="Locked talents">{p.locked_count}</div>
                {shootCell(`po-shoot-state-${p.project_id}`)}
                <div className="text-xs" data-testid={`po-quote-${p.project_id}`}>{dash(p.money.production_quote)}</div>
                <div className="text-xs">{dash(p.money.talent_payable_total)}</div>
                <div className="text-xs font-medium" data-testid={`po-earnings-${p.project_id}`}>{dash(p.money.earnings)}</div>
                <div className="min-w-0"><Pill cls={cp.cls} testId={`po-client-pay-${p.project_id}`}>{cp.label}</Pill>{p.client_payment.outstanding > 0 && <div className="text-[11px] text-black/45 mt-0.5 truncate">{formatCurrency(p.client_payment.outstanding)} due</div>}</div>
                <div className={`text-xs ${allPaid ? "text-emerald-700" : "text-black/70"}`} data-testid={`po-talent-pay-${p.project_id}`}>{p.talent_payment.cleared}/{p.talent_payment.total}</div>
                <div>{attention(`po-attention-${p.project_id}`)}</div>
                <div className="text-black/35">{expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}</div>
            </button>
            {/* mobile / tablet */}
            <button type="button" onClick={onToggle} aria-expanded={expanded} className="lg:hidden w-full text-left px-3 py-3" data-testid={`po-toggle-mobile-${p.project_id}`}>
                <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                        <div className="text-sm font-semibold text-black/85 truncate">{p.brand_name}</div>
                        <div className="text-[11px] text-black/45 truncate">{p.client.label || "—"} <span className="capitalize">· {p.status}</span> · {p.locked_count} locked</div>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">{attention(`po-attention-mobile-${p.project_id}`)}{expanded ? <ChevronUp className="h-4 w-4 text-black/35" /> : <ChevronDown className="h-4 w-4 text-black/35" />}</div>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
                    <div>{shootCell(`po-shoot-state-mobile-${p.project_id}`)}</div>
                    <div><span className="text-black/40 block">Client payment</span><Pill cls={cp.cls}>{cp.label}</Pill>{p.client_payment.outstanding > 0 && <span className="text-black/45"> · {formatCurrency(p.client_payment.outstanding)} due</span>}</div>
                    <div><span className="text-black/40 block">Production quote</span>{dash(p.money.production_quote)}</div>
                    <div><span className="text-black/40 block">Talentgram earnings</span><span className="font-medium">{dash(p.money.earnings)}</span></div>
                    <div><span className="text-black/40 block">Talent payments</span>{p.talent_payment.cleared}/{p.talent_payment.total} cleared</div>
                    <div><span className="text-black/40 block">Talent cost</span>{dash(p.money.talent_payable_total)}</div>
                </div>
            </button>
            {/* quick actions stay available without expanding */}
            <div className="flex flex-wrap items-center gap-2 px-3 pb-2.5 -mt-0.5">
                <Button asChild size="sm" variant="ghost" className="h-7 text-[11px] px-2"><Link to={`/admin/projects/${p.project_id}?tab=production`} data-testid={`po-open-${p.project_id}`}>Open Project →</Link></Button>
                <Button size="sm" variant="ghost" className="h-7 text-[11px] px-2" onClick={() => onFollowUp(p)} data-testid={`po-followup-quick-${p.project_id}`}><MessageCircle className="h-3 w-3 mr-1" /> Payment Follow-up</Button>
            </div>
            {expanded && <div className="border-t border-black/[0.06] px-3 py-3"><ProjectDetail project={p} onFollowUp={() => onFollowUp(p)} /></div>}
        </div>
    );
}

export default function ProductionOverview() {
    const [params, setParams] = useSearchParams();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(false);
    const [expandedId, setExpandedId] = useState(null);
    const [followUpFor, setFollowUpFor] = useState(null);
    const [filtersOpen, setFiltersOpen] = useState(false);
    const [showAllShoots, setShowAllShoots] = useState(false);
    const filters = useMemo(() => {
        const f = {};
        FILTER_KEYS.forEach((k) => { f[k] = params.get(k) || DEFAULTS[k] || ""; });
        f.page = Math.max(0, parseInt(params.get("page") || "0", 10) || 0);
        return f;
    }, [params]);
    const [searchText, setSearchText] = useState(filters.q);
    useEffect(() => { setSearchText(filters.q); }, [filters.q]);

    const setFilters = useCallback((patch) => {
        const next = new URLSearchParams(params);
        Object.entries(patch).forEach(([k, v]) => {
            if (v === "" || v === null || v === undefined || v === DEFAULTS[k] || (k === "page" && Number(v) === 0)) next.delete(k);
            else next.set(k, String(v));
        });
        if (!("page" in patch)) next.delete("page");           // any filter change starts from the first page
        setParams(next);
    }, [params, setParams]);

    // typing in the search box updates the URL (and so the query) after a short pause
    const debounceRef = useRef(null);
    const onSearch = (v) => {
        setSearchText(v);
        clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => setFilters({ q: v.trim() }), 350);
    };
    useEffect(() => () => clearTimeout(debounceRef.current), []);

    const requestId = useRef(0);
    const query = params.toString();
    useEffect(() => {
        const id = ++requestId.current;
        setLoading(true);
        const apiParams = { size: PAGE_SIZE, page: filters.page };
        FILTER_KEYS.forEach((k) => { if (filters[k]) apiParams[k] = filters[k]; });
        adminApi.get("/production/overview", { params: apiParams })
            .then(({ data: d }) => { if (id === requestId.current) { setData(d); setError(false); } })
            .catch((err) => { if (id === requestId.current) { setError(true); toast.error(formatErrorDetail(err) || "Could not load Production Desk"); } })
            .finally(() => { if (id === requestId.current) setLoading(false); });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [query]);

    const activeFilterCount = ["stage", "shoot_state", "shoot_date", "payment", "attention", "client"].filter((k) => filters[k]).length
        + (filters.status !== DEFAULTS.status ? 1 : 0) + (filters.q ? 1 : 0);
    const reset = () => { setSearchText(""); setParams(new URLSearchParams()); };

    const s = data?.summary;
    const shoots = data?.upcoming_shoots || [];
    const visibleShoots = showAllShoots ? shoots : shoots.slice(0, 5);
    const clientOptions = (data?.facets?.clients || []).map((c) => ({ value: c, label: c }));

    return (
        <div className="space-y-4 pb-16 max-w-full" data-testid="production-overview-root">
            <div className="flex items-end justify-between gap-3">
                <div className="min-w-0">
                    <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-black/90 flex items-center gap-2"><Film className="h-6 w-6 text-black/40 shrink-0" /> Production Desk</h1>
                    <p className="text-xs text-black/45 mt-1">Every project with a locked talent — what's shooting, what's owed, and what needs doing.</p>
                </div>
            </div>

            {s && (
                <div className="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-8 gap-2" data-testid="po-summary">
                    <SummaryTile label="Projects" value={s.projects} testId="po-sum-projects" />
                    <SummaryTile label="Locked talents" value={s.locked_talents} testId="po-sum-locked" />
                    <SummaryTile label="Shooting today" value={s.shooting_today} tone={s.shooting_today ? "hot" : undefined} testId="po-sum-today" />
                    <SummaryTile label="Upcoming shoots" value={s.upcoming_shoots} testId="po-sum-upcoming" />
                    <SummaryTile label="Client outstanding" value={formatCurrency(s.client_outstanding)} tone={s.client_outstanding ? "warn" : "good"} testId="po-sum-client-outstanding" />
                    <SummaryTile label="Talent pending" value={formatCurrency(s.talent_pending)} tone={s.talent_pending ? "warn" : "good"} testId="po-sum-talent-pending" />
                    <SummaryTile label="TG earnings" value={formatCurrency(s.earnings)} tone="good" testId="po-sum-earnings" />
                    <SummaryTile label="Needs attention" value={s.needs_attention} tone={s.needs_attention ? "warn" : "good"} testId="po-sum-attention" />
                </div>
            )}

            {/* filters */}
            <Card className="border-black/[0.08] shadow-none">
                <CardContent className="p-3 space-y-3">
                    <div className="flex items-center gap-2">
                        <div className="relative flex-1 min-w-0">
                            <Search className="h-3.5 w-3.5 text-black/30 absolute left-2.5 top-1/2 -translate-y-1/2" />
                            <Input value={searchText} onChange={(e) => onSearch(e.target.value)} placeholder="Search project or client…" className="h-8 text-xs pl-8" data-testid="po-search" />
                        </div>
                        <Button size="sm" variant="outline" className="h-8 text-xs lg:hidden" onClick={() => setFiltersOpen((v) => !v)} data-testid="po-filters-toggle">
                            <SlidersHorizontal className="h-3 w-3 mr-1" /> Filters{activeFilterCount ? ` (${activeFilterCount})` : ""}
                        </Button>
                        {activeFilterCount > 0 && <Button size="sm" variant="ghost" className="h-8 text-xs" onClick={reset} data-testid="po-reset"><X className="h-3 w-3 mr-1" /> Reset</Button>}
                    </div>
                    <div className={`${filtersOpen ? "grid" : "hidden"} lg:grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2`} data-testid="po-filters">
                        <FilterSelect label="Project status" value={filters.status === DEFAULTS.status ? "" : filters.status} onChange={(v) => setFilters({ status: v || "active" })} options={STATUS_OPTIONS} testId="po-f-status" allLabel="Active (not completed)" />
                        <FilterSelect label="Production stage" value={filters.stage} onChange={(v) => setFilters({ stage: v })} options={STAGE_OPTIONS} testId="po-f-stage" />
                        <FilterSelect label="Shoot" value={filters.shoot_state} onChange={(v) => setFilters({ shoot_state: v })} options={SHOOT_STATE_OPTIONS} testId="po-f-shoot-state" />
                        <FilterSelect label="Shoot date" value={filters.shoot_date} onChange={(v) => setFilters({ shoot_date: v })} options={SHOOT_DATE_OPTIONS} testId="po-f-shoot-date" />
                        <FilterSelect label="Payment" value={filters.payment} onChange={(v) => setFilters({ payment: v })} options={PAYMENT_OPTIONS} testId="po-f-payment" />
                        <FilterSelect label="Attention" value={filters.attention} onChange={(v) => setFilters({ attention: v })} options={ATTENTION_OPTIONS} testId="po-f-attention" />
                        <FilterSelect label="Client" value={filters.client} onChange={(v) => setFilters({ client: v })} options={clientOptions} testId="po-f-client" allLabel="All clients" />
                        <FilterSelect label="Sort" value={filters.sort === DEFAULTS.sort ? "" : filters.sort} onChange={(v) => setFilters({ sort: v || "priority" })} options={SORT_OPTIONS} testId="po-f-sort" allLabel="Most urgent first" />
                    </div>
                </CardContent>
            </Card>

            {/* upcoming shoots — straight from the talents' own shoot schedules */}
            {shoots.length > 0 && (
                <Card className="border-black/[0.08] shadow-none" data-testid="po-upcoming">
                    <CardContent className="p-3">
                        <div className="flex items-center justify-between mb-2">
                            <span className="text-[11px] font-semibold uppercase tracking-wide text-black/45 flex items-center gap-1.5"><CalendarClock className="h-3.5 w-3.5" /> Upcoming shoots</span>
                            {shoots.length > 5 && <button className="text-[11px] text-black/45 hover:text-black/70" onClick={() => setShowAllShoots((v) => !v)}>{showAllShoots ? "Show fewer" : `Show all (${shoots.length})`}</button>}
                        </div>
                        <div className="space-y-1">
                            {visibleShoots.map((u) => (
                                <Link key={`${u.project_id}-${u.talent_id}-${u.date}-${u.call_time}`} to={`/admin/projects/${u.project_id}?tab=production`}
                                    className="grid grid-cols-[auto_1fr] md:grid-cols-[84px_minmax(0,1.3fr)_minmax(0,1fr)_120px_120px_minmax(0,1.2fr)] gap-x-3 gap-y-0.5 rounded-md px-2 py-1.5 hover:bg-slate-50 text-xs items-center" data-testid="po-upcoming-item">
                                    <span className={`font-medium ${u.date === data.today ? "text-red-600" : "text-black/75"}`}>{u.date === data.today ? "Today" : formatDate(u.date)}</span>
                                    <span className="text-black/80 truncate">{u.brand_name}</span>
                                    <span className="text-black/55 truncate col-span-2 md:col-span-1">{u.talent_name}</span>
                                    <span className="text-black/50"><span className="text-black/35">Report </span>{formatClock(u.reporting_time)}</span>
                                    <span className="text-black/50"><span className="text-black/35">Call </span>{formatClock(u.call_time)}</span>
                                    <span className="text-black/55 truncate col-span-2 md:col-span-1">{u.location || "—"}</span>
                                </Link>
                            ))}
                        </div>
                    </CardContent>
                </Card>
            )}

            {/* the project list */}
            {loading && !data ? (
                <div className="flex items-center justify-center py-20 text-black/40 text-sm"><Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading Production Desk…</div>
            ) : error && !data ? (
                <div className="py-20 text-center text-sm text-black/40" data-testid="po-error">Could not load Production Desk.</div>
            ) : (
                <div className="space-y-2" data-testid="po-list">
                    <div className="hidden lg:grid grid-cols-[minmax(0,2.2fr)_56px_minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.1fr)_70px_56px_28px] gap-x-3 px-3 text-[10px] uppercase tracking-wide text-black/35">
                        <div>Project</div><div>Locked</div><div>Shoot</div><div>Prod. quote</div><div>Talent cost</div><div>TG earnings</div><div>Client payment</div><div>Talents</div><div>Alerts</div><div />
                    </div>
                    {(data?.items || []).length === 0 ? (
                        <div className="py-16 text-center text-sm text-black/40" data-testid="po-empty">
                            <CheckCircle2 className="h-5 w-5 mx-auto mb-2 text-black/25" />
                            {activeFilterCount ? "No projects match these filters." : "No projects with locked talent yet."}
                            {activeFilterCount > 0 && <div className="mt-2"><Button size="sm" variant="outline" className="h-8 text-xs" onClick={reset}>Clear filters</Button></div>}
                        </div>
                    ) : (
                        data.items.map((p) => (
                            <ProjectRow key={p.project_id} project={p} expanded={expandedId === p.project_id}
                                onToggle={() => setExpandedId((cur) => (cur === p.project_id ? null : p.project_id))}
                                onFollowUp={(proj) => setFollowUpFor(proj)} />
                        ))
                    )}
                    {data && data.pages > 1 && (
                        <div className="flex items-center justify-between pt-2 text-xs text-black/50" data-testid="po-pagination">
                            <span>Page {data.page + 1} of {data.pages} · {data.total} projects</span>
                            <div className="flex gap-2">
                                <Button size="sm" variant="outline" className="h-8 text-xs" disabled={data.page <= 0} onClick={() => setFilters({ page: data.page - 1 })} data-testid="po-prev">Previous</Button>
                                <Button size="sm" variant="outline" className="h-8 text-xs" disabled={!data.has_more} onClick={() => setFilters({ page: data.page + 1 })} data-testid="po-next">Next</Button>
                            </div>
                        </div>
                    )}
                </div>
            )}

            {followUpFor && <FollowUpDialog project={followUpFor} onClose={() => setFollowUpFor(null)} />}
        </div>
    );
}
