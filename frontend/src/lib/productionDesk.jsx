/* ---------------------------------------------------------------------
 * Shared Production Desk building blocks — formatters (IST-aware), the location link, and the
 * WhatsApp actions. Used by BOTH the project-level desk (ProductionDesk.jsx) and the global
 * Production Desk (ProductionOverview.jsx) so there is one implementation of each.
 * ------------------------------------------------------------------- */
import React, { useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { MapPin, MessageCircle } from "lucide-react";

// Same INR formatter MarketingHub already uses — no second money formatter.
export const formatCurrency = (val) => {
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
export const IST_TZ = "Asia/Kolkata";
export const istDateParts = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: IST_TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);

export const toDateInputValue = (iso) => {
    if (!iso) return "";
    try {
        if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
        return istDateParts(new Date(iso));
    } catch { return ""; }
};
export const fromDateInputValue = (dateStr) => (dateStr ? `${dateStr}T12:00:00.000Z` : null);

export const formatDate = (iso) => {
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
export const formatClock = (v) => {
    if (!v) return "—";
    const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(String(v).trim());
    if (!m) return v;
    const h = Number(m[1]);
    return `${h % 12 || 12}:${m[2]} ${h >= 12 ? "PM" : "AM"}`;
};
// Input value for <input type="time">: only a canonical HH:MM round-trips; legacy text is shown read-only.
export const clockInputValue = (v) => (v && /^([01]\d|2[0-3]):[0-5]\d$/.test(String(v).trim()) ? String(v).trim() : "");
export const clockMinutes = (v) => {
    const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(String(v || "").trim());
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
export const reportingAfterCall = (reporting, call) => {
    const r = clockMinutes(reporting); const c = clockMinutes(call);
    return r !== null && c !== null && r > c;
};

// Opens the SAME wa.me deep-link pattern MarketingHub.jsx's own
// handleShare() already uses for one-off admin-triggered WhatsApp
// messages — the admin still taps Send inside WhatsApp themselves, so
// this never auto-sends anything (spec: "do not create a new WhatsApp
// sender/worker").
export function openWhatsApp(phone, message) {
    const digits = (phone || "").replace(/[^0-9]/g, "");
    if (!digits) {
        toast.error("No phone number on file");
        return;
    }
    window.open(`https://wa.me/${digits}?text=${encodeURIComponent(message)}`, "_blank");
}

// Clickable location: the stored Maps URL when a place was selected, otherwise a plain Google Maps
// *search* link built from the text that was typed (the Maps URLs API needs no key and no billing).
export const mapsSearchUrl = (...parts) => {
    const q = parts.filter(Boolean).join(", ").trim();
    return q.length >= 3 ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(q)}` : null;
};

export function LocationLink({ name, mapUrl, address }) {
    if (!name && !mapUrl) return <span className="text-black/30">—</span>;
    const addr = address && address !== name ? address : null;
    mapUrl = mapUrl || mapsSearchUrl(name, addr);
    if (mapUrl) {
        return (
            <a href={mapUrl} target="_blank" rel="noreferrer" className="inline-flex flex-col max-w-full text-[#0c2340] hover:underline min-w-0" title={addr || undefined}>
                <span className="inline-flex items-center gap-1 min-w-0 max-w-full">
                    <MapPin className="h-3 w-3 shrink-0" />
                    <span className="truncate">{name || "View on map"}</span>
                </span>
                {addr && <span className="text-[10px] text-black/40 truncate pl-4 max-w-full">{addr}</span>}
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


// ── Production actions shared by the project-level desk and the global Production Desk ─────────
// There is ONE implementation of each action: the project page and the global page both call these,
// so the invoice request, the follow-up recipient list, the review step and the "nothing is sent
// automatically" guarantee cannot drift apart.

// "Ask to Raise Invoice" — talent-facing: the message is built by the backend from the TALENT's agreed
// rate only (never the production quote). A WhatsApp-group destination needs an explicit confirmation of
// the exact text; a phone destination opens WhatsApp pre-filled and the admin taps Send themselves.
export async function askTalentToRaiseInvoice(projectId, talentId) {
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
}

// Payment follow-up (production-facing): who to send it to (the project's own CRM contacts), the
// generated message, and the review step. Nothing opens until `confirm()` — the admin then taps Send
// inside WhatsApp themselves.
export function usePaymentFollowUp(projectId, { defaultContactId = "", refreshKey = 0, onConfirmed } = {}) {
    const [contacts, setContacts] = useState([]);
    const [contactId, setContactId] = useState("");
    const [preview, setPreview] = useState(null);

    useEffect(() => {
        let cancelled = false;
        adminApi.get(`/projects/${projectId}/production-desk/payment-followup-contacts`).then(({ data: d }) => {
            if (cancelled) return;
            const list = Array.isArray(d?.contacts) ? d.contacts : [];
            setContacts(list);
            setContactId((cur) => {
                if (cur && list.some((c) => c.client_id === cur && c.has_phone)) return cur;
                const pick = list.find((c) => c.is_default && c.has_phone) || list.find((c) => c.has_phone);
                return pick ? pick.client_id : "";
            });
        }).catch(() => { if (!cancelled) setContacts([]); });
        return () => { cancelled = true; };
    }, [projectId, refreshKey, defaultContactId]);

    const requestPreview = useCallback(async () => {
        try {
            const { data: msg } = await adminApi.get(
                `/projects/${projectId}/production-desk/payment-followup-message`,
                contactId ? { params: { contact_id: contactId } } : undefined,
            );
            setPreview(msg);
        } catch (err) {
            toast.error(formatErrorDetail(err) || "Could not build follow-up message");
        }
    }, [projectId, contactId]);

    const confirm = useCallback(async () => {
        const msg = preview;
        if (!msg) return;
        setPreview(null);
        openWhatsApp(msg.phone, msg.message);
        if (onConfirmed) await onConfirmed();
    }, [preview, onConfirmed]);

    const cancel = useCallback(() => setPreview(null), []);
    return { contacts, contactId, setContactId, preview, requestPreview, confirm, cancel };
}

export function PaymentRecipientSelect({ contacts, value, onChange, className }) {
    if (!contacts.length) {
        return <span className="text-black/35 text-xs">No CRM contacts on this project yet — set a Concerned Person or add someone under Crew.</span>;
    }
    return (
        <Select value={value || undefined} onValueChange={onChange}>
            <SelectTrigger className={className || "h-8 text-xs w-full sm:w-[360px]"} data-testid="pd-followup-recipient-select"><SelectValue placeholder="Choose who to follow up with" /></SelectTrigger>
            <SelectContent>
                {contacts.map((c) => (
                    <SelectItem key={c.client_id} value={c.client_id} disabled={!c.has_phone} data-testid={`pd-followup-contact-${c.client_id}`}>
                        {c.name || "Unnamed"}{c.role ? ` · ${c.role}` : (c.designation ? ` · ${c.designation}` : "")}{c.company_name ? ` · ${c.company_name}` : ""}{!c.has_phone ? " (no phone)" : ""}
                    </SelectItem>
                ))}
            </SelectContent>
        </Select>
    );
}

export function FollowUpReviewDialog({ preview, onCancel, onConfirm }) {
    return (
        <Dialog open={!!preview} onOpenChange={(o) => { if (!o) onCancel(); }}>
            <DialogContent className="max-w-lg" data-testid="pd-followup-dialog">
                <DialogHeader><DialogTitle>Review payment follow-up</DialogTitle></DialogHeader>
                {preview && (
                    <div className="space-y-3 text-xs">
                        <div data-testid="pd-followup-recipient-summary">
                            <span className="text-black/40">To: </span>
                            <span className="font-medium text-black/80">{preview.contact_name || "—"}</span>
                            <span className="text-black/40"> · {preview.phone}</span>
                        </div>
                        {(preview.warnings || []).map((w) => (
                            <div key={w} className="rounded-md bg-amber-50 border border-amber-200 px-2.5 py-1.5 text-amber-800" data-testid="pd-followup-warning">{w}</div>
                        ))}
                        <pre className="whitespace-pre-wrap rounded-md border border-black/[0.08] bg-slate-50 p-3 text-[11px] text-black/75 max-h-72 overflow-auto font-sans" data-testid="pd-followup-message">{preview.message}</pre>
                        <p className="text-[11px] text-black/40">Nothing is sent from here — this opens WhatsApp with the text pre-filled and you tap Send yourself.</p>
                    </div>
                )}
                <DialogFooter>
                    <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
                    <Button size="sm" onClick={onConfirm} data-testid="pd-followup-confirm"><MessageCircle className="h-3 w-3 mr-1" /> Open WhatsApp</Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
