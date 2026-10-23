// Production Desk — GST breakdown, instalment ledger and payment ageing (presentation only).
//
// Every figure here is the server's: `receivable` on a tranche and `payable` on a talent card come from
// backend/production_money.py. Nothing is calculated, rounded or defaulted in the browser — a value the
// server could not establish arrives as null and is shown as "—" / "unavailable", never guessed.
import React, { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatDate, formatMoneyExact } from "@/lib/productionDesk";

const dateOrNote = (iso) => (iso ? formatDate(iso) : "date not recorded");

export const money = (v) => formatMoneyExact(v);

export const GST_TREATMENT_OPTIONS = [
    { value: "unset", label: "Not recorded" },
    { value: "none", label: "No GST" },
    { value: "exclusive", label: "GST added on top" },
    { value: "inclusive", label: "GST included in amount" },
];
const DUE_BASIS = { due_date: "due date", payment_terms: "from invoice date + payment terms", expected_date: "from expected payment date" };

// Same id for a retry of the same attempt (double-click, flaky network); a fresh one after it succeeded.
const newRequestId = () =>
    (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `r${Date.now()}${Math.random().toString(36).slice(2, 10)}`).replace(/[^A-Za-z0-9_-]/g, "");

const STATE_CLS = {
    paid: "bg-emerald-50 text-emerald-700 border-emerald-200",
    partially_paid: "bg-amber-50 text-amber-800 border-amber-200",
    unpaid: "bg-slate-50 text-slate-600 border-slate-200",
    unknown: "bg-slate-50 text-slate-500 border-slate-200",
};

export function StatusChip({ view, testId }) {
    if (!view) return null;
    const cls = view.overdue && view.state !== "paid" ? "bg-red-50 text-red-700 border-red-200" : STATE_CLS[view.state] || STATE_CLS.unknown;
    return (
        <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${cls}`} data-testid={testId}>
            {view.status}{view.overdue && view.days_overdue > 0 ? ` · ${view.days_overdue}d` : ""}
        </span>
    );
}

export function gstTreatmentLabel(tax) {
    if (!tax) return "";
    if (tax.state === "none") return "No GST";
    if (tax.state === "exclusive") return `GST ${tax.rate}% added on top`;
    if (tax.state === "inclusive") return `GST ${tax.rate}% included`;
    if (tax.state === "incomplete") return "GST rate missing";
    return "GST not recorded";
}

// Client invoice: base / GST / total, received (and the GST part of it where established), outstanding, ageing.
export function ReceivableSummary({ view: v, testId }) {
    if (!v) return null;
    const tax = v.tax || {};
    const hasGst = tax.state === "exclusive" || tax.state === "inclusive";
    return (
        <div className="mt-1.5 space-y-0.5 text-[11px]" data-testid={testId}>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-black/60">
                <StatusChip view={v} testId={testId && `${testId}-status`} />
                <span data-testid={testId && `${testId}-tax`} className={tax.state === "not_recorded" || tax.state === "incomplete" ? "text-black/40" : ""}>
                    {hasGst
                        ? `Base ${money(tax.base)} + GST ${tax.rate}% ${money(tax.gst)} = ${money(v.invoice_total)}${tax.state === "inclusive" ? " (GST included in amount)" : ""}`
                        : gstTreatmentLabel(tax)}
                </span>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-black/50">
                <span>Received <span className="font-semibold text-emerald-700" data-testid={testId && `${testId}-received`}>{money(v.received)}</span>
                    {hasGst && v.received_gst !== null && v.received_gst !== undefined && <span className="text-black/40"> (GST part {money(v.received_gst)})</span>}
                </span>
                <span>Outstanding <span className={`font-semibold ${v.outstanding > 0 ? "text-amber-700" : "text-emerald-700"}`} data-testid={testId && `${testId}-outstanding`}>{money(v.outstanding)}</span>
                    {hasGst && v.outstanding > 0 && v.outstanding_gst > 0 && <span className="text-black/40"> (incl. GST {money(v.outstanding_gst)})</span>}
                </span>
                {v.instalments > 0 && <span>{v.instalments} payment{v.instalments !== 1 ? "s" : ""}: {dateOrNote(v.first_payment_date)}{v.last_payment_date && v.last_payment_date !== v.first_payment_date ? ` → ${formatDate(v.last_payment_date)}` : ""}</span>}
            </div>
            <AgeingLine view={v} testId={testId && `${testId}-ageing`} />
        </div>
    );
}

export function AgeingLine({ view: v, testId }) {
    if (!v) return null;
    let text;
    if (v.due_date) {
        text = `Due ${formatDate(v.due_date)} (${DUE_BASIS[v.due_basis] || "due date"})`;
        if (v.state === "paid") text += " · settled";
        else if (v.overdue) text += ` · ${v.days_overdue} day${v.days_overdue !== 1 ? "s" : ""} overdue`;
    } else {
        text = "Due date unavailable";
        if (v.state !== "paid" && v.days_since_invoice != null) text += ` · ${v.days_since_invoice} day${v.days_since_invoice !== 1 ? "s" : ""} since invoice date`;
    }
    return <div className={`${v.overdue && v.state !== "paid" ? "text-red-600 font-medium" : "text-black/40"}`} data-testid={testId}>{text}</div>;
}

// One instalment row: view, correct, void. Voided instalments stay visible (struck through) so the history is auditable.
function PaymentRow({ p, onEdit, onVoid, testId }) {
    const [editing, setEditing] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [busy, setBusy] = useState(false);
    const [d, setD] = useState({});
    const voided = p.status === "voided";
    const start = () => { setD({ amount: String(p.amount), date: p.date || "", reference: p.reference || "", notes: p.notes || "", gst_amount: p.gst_amount == null ? "" : String(p.gst_amount) }); setEditing(true); };
    const save = async () => {
        if (busy) return;
        setBusy(true);
        const ok = await onEdit(p.id, {
            amount: Number(d.amount), date: d.date || null, reference: d.reference || null, notes: d.notes || null,
            gst_amount: d.gst_amount === "" ? null : Number(d.gst_amount),
        });
        setBusy(false);
        if (ok) setEditing(false);
    };
    if (editing) {
        return (
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-1.5 items-end rounded border border-black/[0.08] p-2" data-testid={testId}>
                <div><Label className="text-[10px]">Amount</Label><Input type="number" min="0" step="0.01" value={d.amount} onChange={(e) => setD({ ...d, amount: e.target.value })} className="h-7 text-xs" data-testid={`${testId}-amount`} /></div>
                <div><Label className="text-[10px]">Date</Label><Input type="date" value={d.date} onChange={(e) => setD({ ...d, date: e.target.value })} className="h-7 text-xs" /></div>
                <div><Label className="text-[10px]">GST part (optional)</Label><Input type="number" min="0" step="0.01" value={d.gst_amount} onChange={(e) => setD({ ...d, gst_amount: e.target.value })} className="h-7 text-xs" /></div>
                <div><Label className="text-[10px]">Reference</Label><Input value={d.reference} onChange={(e) => setD({ ...d, reference: e.target.value })} className="h-7 text-xs" /></div>
                <div className="flex gap-1 justify-end">
                    <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setEditing(false)}>Cancel</Button>
                    <Button size="sm" className="h-7 text-xs" disabled={busy || !(Number(d.amount) > 0)} onClick={save} data-testid={`${testId}-save`}>Save</Button>
                </div>
                <div className="col-span-2 sm:col-span-5"><Input value={d.notes} onChange={(e) => setD({ ...d, notes: e.target.value })} placeholder="Notes (optional)" className="h-7 text-xs" /></div>
            </div>
        );
    }
    return (
        <div className={`flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 text-[11px] ${voided ? "line-through text-black/30" : "text-black/60"}`} data-testid={testId}>
            <span>
                <span className="font-semibold">{money(p.amount)}</span>
                {" · "}{p.date ? formatDate(p.date) : (p.legacy ? "date not recorded (earlier record)" : "date not recorded")}
                {p.gst_amount != null && <span className="text-black/40"> · GST {money(p.gst_amount)}</span>}
                {p.reference && <span className="text-black/40"> · Ref {p.reference}</span>}
                {p.notes && <span className="text-black/40"> · {p.notes}</span>}
                {(p.revisions || []).length > 0 && !voided && <span className="text-black/30"> · corrected</span>}
                {voided && <span> (voided)</span>}
            </span>
            {!voided && !p.legacy && (
                <span className="flex items-center gap-1 no-underline">
                    <button className="text-[10px] text-black/40 hover:text-black/70 underline" onClick={start} data-testid={`${testId}-edit`}>Correct</button>
                    {confirming ? (
                        <>
                            <button className="text-[10px] text-red-600 font-semibold underline" disabled={busy} onClick={async () => { setBusy(true); await onVoid(p.id); setBusy(false); setConfirming(false); }} data-testid={`${testId}-void-confirm`}>Confirm void</button>
                            <button className="text-[10px] text-black/40 underline" onClick={() => setConfirming(false)}>Keep</button>
                        </>
                    ) : (
                        <button className="text-[10px] text-black/40 hover:text-red-600 underline" onClick={() => setConfirming(true)} data-testid={`${testId}-void`}>Void</button>
                    )}
                </span>
            )}
        </div>
    );
}

// Instalment ledger + "Record payment". The request id is created once per attempt and re-sent on a retry, so a
// double-click or a repeated submit after a network error can never record the same payment twice.
export function PaymentLedger({ payments, outstanding, allowGst, canRecord = true, onAdd, onEdit, onVoid, testId }) {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const [f, setF] = useState({ amount: "", date: "", reference: "", notes: "", gst_amount: "" });
    const rid = useRef(null);
    const list = payments || [];
    const openForm = () => {
        rid.current = rid.current || newRequestId();
        setF({ amount: outstanding > 0 ? String(outstanding) : "", date: "", reference: "", notes: "", gst_amount: "" });
        setOpen(true);
    };
    const submit = async () => {
        if (busy || !(Number(f.amount) > 0)) return;
        setBusy(true);
        const ok = await onAdd({
            amount: Number(f.amount), date: f.date || null, reference: f.reference || null, notes: f.notes || null,
            gst_amount: f.gst_amount === "" ? null : Number(f.gst_amount), request_id: rid.current,
        });
        setBusy(false);
        if (ok) { rid.current = null; setOpen(false); }
    };
    return (
        <div className="mt-2 space-y-1" data-testid={testId}>
            {list.length > 0 && (
                <div className="space-y-0.5 rounded border border-black/[0.06] bg-slate-50/50 px-2 py-1.5" data-testid={`${testId}-list`}>
                    {list.map((p) => <PaymentRow key={p.id} p={p} onEdit={onEdit} onVoid={onVoid} testId={`${testId}-row-${p.id}`} />)}
                </div>
            )}
            {canRecord && !open && (
                <Button size="sm" variant="outline" className="h-6 text-[10px] px-2" onClick={openForm} data-testid={`${testId}-record`}>Record payment</Button>
            )}
            {open && (
                <div className="grid grid-cols-2 sm:grid-cols-5 gap-1.5 items-end rounded border border-[#0c2340]/20 bg-white p-2" data-testid={`${testId}-form`}>
                    <div><Label className="text-[10px]">Amount</Label><Input type="number" min="0" step="0.01" value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} className="h-7 text-xs" data-testid={`${testId}-amount`} autoFocus /></div>
                    <div><Label className="text-[10px]">Date</Label><Input type="date" value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} className="h-7 text-xs" data-testid={`${testId}-date`} /></div>
                    {allowGst
                        ? <div><Label className="text-[10px]">GST part (optional)</Label><Input type="number" min="0" step="0.01" value={f.gst_amount} onChange={(e) => setF({ ...f, gst_amount: e.target.value })} className="h-7 text-xs" /></div>
                        : <div className="hidden sm:block" />}
                    <div><Label className="text-[10px]">Reference</Label><Input value={f.reference} onChange={(e) => setF({ ...f, reference: e.target.value })} placeholder="UTR / cheque" className="h-7 text-xs" /></div>
                    <div className="flex gap-1 justify-end">
                        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setOpen(false)}>Cancel</Button>
                        <Button size="sm" className="h-7 text-xs" disabled={busy || !(Number(f.amount) > 0)} onClick={submit} data-testid={`${testId}-submit`}>{busy ? "Saving…" : "Save"}</Button>
                    </div>
                    <div className="col-span-2 sm:col-span-5"><Input value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} placeholder="Notes (optional)" className="h-7 text-xs" /></div>
                </div>
            )}
        </div>
    );
}

// Tax + due-date inputs shared by the tranche add/edit forms. "unset" = nothing recorded (never assumed).
export function TaxFields({ value, onChange, withDue = true }) {
    const taxable = value.gst_treatment === "exclusive" || value.gst_treatment === "inclusive";
    return (
        <>
            <div>
                <Label className="text-[10px]">GST</Label>
                <Select value={value.gst_treatment || "unset"} onValueChange={(v) => onChange({ ...value, gst_treatment: v, gst_rate: v === "exclusive" || v === "inclusive" ? value.gst_rate : "" })}>
                    <SelectTrigger className="h-7 text-xs" data-testid="pd-gst-treatment"><SelectValue /></SelectTrigger>
                    <SelectContent>{GST_TREATMENT_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
                </Select>
            </div>
            {taxable && (
                <div><Label className="text-[10px]">GST rate %</Label><Input type="number" min="0" max="100" step="0.01" value={value.gst_rate ?? ""} onChange={(e) => onChange({ ...value, gst_rate: e.target.value })} className="h-7 text-xs" data-testid="pd-gst-rate" /></div>
            )}
            {withDue && (
                <div><Label className="text-[10px]">Due date</Label><Input type="date" value={value.due_date || ""} onChange={(e) => onChange({ ...value, due_date: e.target.value })} className="h-7 text-xs" data-testid="pd-due-date" /></div>
            )}
        </>
    );
}

// Payload helper: what to send for the tax fields ("unset" clears, a taxable treatment needs the rate).
export function taxPayload(v) {
    const treatment = v.gst_treatment && v.gst_treatment !== "unset" ? v.gst_treatment : null;
    const taxable = treatment === "exclusive" || treatment === "inclusive";
    return { gst_treatment: treatment, gst_rate: taxable && v.gst_rate !== "" && v.gst_rate != null ? Number(v.gst_rate) : null, due_date: v.due_date || null };
}
export const taxMissingRate = (v) => (v.gst_treatment === "exclusive" || v.gst_treatment === "inclusive") && (v.gst_rate === "" || v.gst_rate == null);

// ---- Talent side ---------------------------------------------------------------------------------------------
const TALENT_GST_NOTE = {
    valid: null,
    no_gst: "No GST on this talent's invoice",
    invoice_missing: "GST treatment set but no GST invoice number recorded — GST not added to the payable",
    incomplete: "GST rate missing — GST not added to the payable",
};

// Talent payable with GST: base fee, GST invoice reference, GST amount, total payable, paid to date, balance, ageing,
// instalment ledger. Talent GST is independent of commission and of the client's GST.
export function TalentPayableBlock({ talent: t, onSaveGst, onAdd, onEdit, onVoid }) {
    const p = t.payable;
    const [editing, setEditing] = useState(false);
    const [d, setD] = useState({});
    if (!p) return null;
    const id = t.talent_id;
    const start = () => {
        setD({ gst_treatment: t.gst_treatment || "unset", gst_rate: t.gst_rate ?? "", gst_invoice_number: t.gst_invoice_number || "", gst_invoice_date: t.gst_invoice_date || "", due_date: t.payment_due_date || "" });
        setEditing(true);
    };
    const save = async () => {
        const tax = taxPayload(d);
        const ok = await onSaveGst(id, {
            gst_treatment: tax.gst_treatment, gst_rate: tax.gst_rate, payment_due_date: tax.due_date,
            gst_invoice_number: d.gst_invoice_number || null, gst_invoice_date: d.gst_invoice_date || null,
        });
        if (ok) setEditing(false);
    };
    const note = TALENT_GST_NOTE[p.gst_state];
    return (
        <div className="mt-3 rounded-lg border border-black/[0.08] p-3" data-testid={`pd-payable-${id}`}>
            <div className="flex items-center justify-between gap-2 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-black/40">Talent payment · payable</span>
                <span className="flex items-center gap-2">
                    <StatusChip view={p} testId={`pd-payable-${id}-status`} />
                    {!editing && <button className="text-[10px] text-black/40 hover:text-black/70 underline" onClick={start} data-testid={`pd-payable-${id}-edit-gst`}>GST &amp; due date</button>}
                </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-x-4 gap-y-1 text-[11px]">
                <div><div className="text-black/40">Agreed fee (net)</div><div className="font-medium" data-testid={`pd-payable-${id}-base`}>{money(p.net_payable)}</div></div>
                <div><div className="text-black/40">GST{p.gst_state === "valid" && p.rate != null ? ` ${p.rate}%` : ""}</div><div className="font-medium" data-testid={`pd-payable-${id}-gst`}>{p.gst_state === "valid" ? money(p.gst) : "—"}</div></div>
                <div><div className="text-black/40">Total payable</div><div className="font-semibold" data-testid={`pd-payable-${id}-total`}>{money(p.total_payable)}</div></div>
                <div><div className="text-black/40">Paid to date</div><div className="font-medium text-emerald-700" data-testid={`pd-payable-${id}-paid`}>{money(p.paid)}</div></div>
                <div><div className="text-black/40">Remaining</div><div className={`font-semibold ${p.balance > 0 ? "text-amber-700" : "text-emerald-700"}`} data-testid={`pd-payable-${id}-balance`}>{money(p.balance)}</div></div>
            </div>
            <div className="mt-1 space-y-0.5 text-[11px] text-black/40">
                {p.gst_state === "valid" && <div data-testid={`pd-payable-${id}-gst-invoice`}>GST invoice {p.gst_invoice_number}{p.gst_invoice_date ? ` · ${formatDate(p.gst_invoice_date)}` : " · date not recorded"}{p.treatment === "inclusive" ? " · GST included in the agreed amount" : ""}</div>}
                {note && <div className={p.gst_state === "invoice_missing" || p.gst_state === "incomplete" ? "text-amber-700" : ""} data-testid={`pd-payable-${id}-gst-note`}>{note}</div>}
                {p.instalments > 0 && <div>{p.instalments} payment{p.instalments !== 1 ? "s" : ""}: {dateOrNote(p.first_payment_date)}{p.last_payment_date && p.last_payment_date !== p.first_payment_date ? ` → ${formatDate(p.last_payment_date)}` : ""}</div>}
                <AgeingLine view={p} testId={`pd-payable-${id}-ageing`} />
            </div>
            {editing && (
                <div className="mt-2 grid grid-cols-2 sm:grid-cols-5 gap-1.5 items-end rounded border border-[#0c2340]/20 bg-white p-2" data-testid={`pd-payable-${id}-gst-form`}>
                    <TaxFields value={d} onChange={setD} />
                    <div><Label className="text-[10px]">GST invoice #</Label><Input value={d.gst_invoice_number} onChange={(e) => setD({ ...d, gst_invoice_number: e.target.value })} className="h-7 text-xs" data-testid={`pd-payable-${id}-gst-invoice-number`} /></div>
                    <div><Label className="text-[10px]">GST invoice date</Label><Input type="date" value={d.gst_invoice_date} onChange={(e) => setD({ ...d, gst_invoice_date: e.target.value })} className="h-7 text-xs" /></div>
                    <div className="flex gap-1 justify-end col-span-2 sm:col-span-1">
                        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setEditing(false)}>Cancel</Button>
                        <Button size="sm" className="h-7 text-xs" disabled={taxMissingRate(d)} onClick={save} data-testid={`pd-payable-${id}-gst-save`}>Save</Button>
                    </div>
                </div>
            )}
            <PaymentLedger
                testId={`pd-payable-${id}-ledger`}
                payments={t.payments} outstanding={p.balance} allowGst={p.gst_state === "valid"} canRecord={p.balance > 0}
                onAdd={(payload) => onAdd(id, payload)} onEdit={(pid, payload) => onEdit(id, pid, payload)} onVoid={(pid) => onVoid(id, pid)}
            />
        </div>
    );
}
