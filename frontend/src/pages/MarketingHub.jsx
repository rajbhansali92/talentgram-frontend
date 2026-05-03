import React, { useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import {
    Sheet,
    SheetContent,
    SheetHeader,
    SheetTitle,
    SheetDescription,
} from "@/components/ui/sheet";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
    DialogFooter,
} from "@/components/ui/dialog";
import { Plus, Loader2, Phone, Mail, Users as UsersIcon, MessageSquare } from "lucide-react";

/**
 * MarketingHub — lightweight CRM dashboard.
 * Backed by /api/marketing/{clients,interactions}.
 */
export default function MarketingHub() {
    const [clients, setClients] = useState([]);
    const [loading, setLoading] = useState(true);
    const [activeClient, setActiveClient] = useState(null);
    const [addOpen, setAddOpen] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/marketing/clients");
            setClients(Array.isArray(data) ? data : []);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to load clients");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const fmtDate = (iso) => {
        if (!iso) return "—";
        try {
            return new Date(iso).toLocaleDateString(undefined, {
                year: "numeric",
                month: "short",
                day: "numeric",
            });
        } catch {
            return "—";
        }
    };

    const handleClientCreated = (newClient) => {
        setClients((prev) => [newClient, ...prev]);
        setAddOpen(false);
        toast.success(`${newClient.name} added`);
    };

    const handleInteractionAdded = (updatedDate) => {
        // Bump the active client's last_contacted_date locally and re-sort.
        if (!activeClient) return;
        const bumped = { ...activeClient, last_contacted_date: updatedDate };
        setActiveClient(bumped);
        setClients((prev) => {
            const next = prev.map((c) => (c.id === bumped.id ? bumped : c));
            next.sort((a, b) => {
                const da = new Date(a.last_contacted_date || 0).getTime();
                const db = new Date(b.last_contacted_date || 0).getTime();
                return db - da;
            });
            return next;
        });
    };

    return (
        <div
            className="max-w-6xl mx-auto px-6 py-8"
            data-testid="marketing-hub-page"
        >
            <div className="flex items-center justify-between mb-6">
                <div>
                    <h1 className="font-display text-3xl tracking-tight">
                        Marketing Hub
                    </h1>
                    <p className="text-sm text-white/60 mt-1">
                        Lightweight CRM — track clients and touchpoints.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={() => setAddOpen(true)}
                    data-testid="marketing-add-client-btn"
                    className="inline-flex items-center gap-2 bg-white text-black px-4 py-2.5 rounded-sm text-xs tracking-wide hover:opacity-90 transition-opacity"
                >
                    <Plus className="w-3.5 h-3.5" />
                    Add Client
                </button>
            </div>

            {loading ? (
                <div
                    className="py-20 flex justify-center"
                    data-testid="marketing-loading"
                >
                    <Loader2 className="w-5 h-5 animate-spin text-white/40" />
                </div>
            ) : clients.length === 0 ? (
                <div
                    className="border border-dashed border-white/10 py-16 text-center text-white/50 text-sm"
                    data-testid="marketing-empty"
                >
                    No clients yet — click "+ Add Client" to create your first record.
                </div>
            ) : (
                <div
                    className="border border-white/10 overflow-hidden"
                    data-testid="marketing-clients-table"
                >
                    <table className="w-full text-sm">
                        <thead className="bg-white/[0.03] text-[11px] tracking-widest uppercase text-white/50">
                            <tr>
                                <th className="text-left px-4 py-3 font-medium">Name</th>
                                <th className="text-left px-4 py-3 font-medium">Company</th>
                                <th className="text-left px-4 py-3 font-medium">Phone</th>
                                <th className="text-left px-4 py-3 font-medium">Last Contacted</th>
                            </tr>
                        </thead>
                        <tbody>
                            {clients.map((c) => (
                                <tr
                                    key={c.id}
                                    onClick={() => setActiveClient(c)}
                                    data-testid={`marketing-client-row-${c.id}`}
                                    className="border-t border-white/10 cursor-pointer hover:bg-white/[0.04] transition-colors"
                                >
                                    <td className="px-4 py-3 font-medium">{c.name}</td>
                                    <td className="px-4 py-3 text-white/70">
                                        {c.company_name || "—"}
                                    </td>
                                    <td className="px-4 py-3 text-white/60 tg-mono text-xs">
                                        {c.phone_number || "—"}
                                    </td>
                                    <td className="px-4 py-3 text-white/60 tg-mono text-xs">
                                        {fmtDate(c.last_contacted_date)}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            <AddClientDialog
                open={addOpen}
                onClose={() => setAddOpen(false)}
                onCreated={handleClientCreated}
            />

            <ClientDrawer
                client={activeClient}
                onClose={() => setActiveClient(null)}
                onInteractionAdded={handleInteractionAdded}
            />
        </div>
    );
}

/* --------------------------------------------------------------------- */
/* Add Client dialog                                                     */
/* --------------------------------------------------------------------- */
function AddClientDialog({ open, onClose, onCreated }) {
    const [name, setName] = useState("");
    const [company, setCompany] = useState("");
    const [phone, setPhone] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) {
            setName("");
            setCompany("");
            setPhone("");
            setSaving(false);
        }
    }, [open]);

    const submit = async (e) => {
        e.preventDefault();
        if (!name.trim()) {
            toast.error("Client name is required");
            return;
        }
        setSaving(true);
        try {
            const { data } = await adminApi.post("/marketing/clients", {
                name: name.trim(),
                company_name: company.trim() || null,
                phone_number: phone.trim() || null,
            });
            onCreated(data);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to create client");
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
            <DialogContent
                className="bg-[#0a0a0a] border-white/10 text-white sm:max-w-md"
                data-testid="marketing-add-client-dialog"
            >
                <DialogHeader>
                    <DialogTitle className="font-display text-2xl tracking-tight">
                        Add Client
                    </DialogTitle>
                    <DialogDescription className="text-white/55">
                        Create a new CRM record. Only name is required.
                    </DialogDescription>
                </DialogHeader>
                <form onSubmit={submit} className="space-y-4 pt-2">
                    <FieldInput
                        label="Name"
                        value={name}
                        onChange={setName}
                        required
                        testId="marketing-input-name"
                        autoFocus
                    />
                    <FieldInput
                        label="Company"
                        value={company}
                        onChange={setCompany}
                        testId="marketing-input-company"
                    />
                    <FieldInput
                        label="Phone"
                        value={phone}
                        onChange={setPhone}
                        testId="marketing-input-phone"
                    />
                    <DialogFooter className="pt-2">
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={saving}
                            data-testid="marketing-add-cancel-btn"
                            className="px-4 py-2 text-xs text-white/60 hover:text-white transition-colors disabled:opacity-40"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={saving}
                            data-testid="marketing-add-submit-btn"
                            className="inline-flex items-center gap-2 bg-white text-black px-4 py-2 rounded-sm text-xs tracking-wide hover:opacity-90 transition-opacity disabled:opacity-50"
                        >
                            {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                            Save Client
                        </button>
                    </DialogFooter>
                </form>
            </DialogContent>
        </Dialog>
    );
}

function FieldInput({ label, value, onChange, required, testId, autoFocus }) {
    return (
        <label className="block">
            <div className="text-[10px] tracking-[0.2em] uppercase text-white/40 mb-1.5">
                {label}
                {required && <span className="text-white/60"> *</span>}
            </div>
            <input
                type="text"
                value={value}
                onChange={(e) => onChange(e.target.value)}
                required={required}
                autoFocus={autoFocus}
                data-testid={testId}
                className="w-full bg-white/[0.03] border border-white/10 px-3 py-2.5 text-sm text-white/90 focus:border-white/30 focus:outline-none transition-colors rounded-sm"
            />
        </label>
    );
}

/* --------------------------------------------------------------------- */
/* Client drawer with interaction log                                    */
/* --------------------------------------------------------------------- */
const INTERACTION_TYPES = [
    { value: "call", label: "Call", icon: Phone },
    { value: "email", label: "Email", icon: Mail },
    { value: "meeting", label: "Meeting", icon: UsersIcon },
    { value: "whatsapp", label: "WhatsApp", icon: MessageSquare },
];

function ClientDrawer({ client, onClose, onInteractionAdded }) {
    const open = !!client;
    const [interactions, setInteractions] = useState([]);
    const [loadingList, setLoadingList] = useState(false);
    const [type, setType] = useState("call");
    const [notes, setNotes] = useState("");
    const [saving, setSaving] = useState(false);

    const loadInteractions = useCallback(async (cid) => {
        setLoadingList(true);
        try {
            const { data } = await adminApi.get(`/marketing/interactions/${cid}`);
            setInteractions(Array.isArray(data) ? data : []);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to load interactions");
        } finally {
            setLoadingList(false);
        }
    }, []);

    useEffect(() => {
        if (!client) {
            setInteractions([]);
            setNotes("");
            setType("call");
            return;
        }
        loadInteractions(client.id);
    }, [client, loadInteractions]);

    const submitInteraction = async (e) => {
        e.preventDefault();
        if (!client) return;
        setSaving(true);
        try {
            const { data } = await adminApi.post("/marketing/interactions", {
                client_id: client.id,
                type,
                notes: notes.trim() || null,
            });
            setInteractions((prev) => [data, ...prev]);
            setNotes("");
            onInteractionAdded(data.created_at);
            toast.success("Interaction logged");
        } catch (err) {
            toast.error(
                err?.response?.data?.detail || "Failed to log interaction",
            );
        } finally {
            setSaving(false);
        }
    };

    const fmt = (iso) => {
        if (!iso) return "—";
        try {
            return new Date(iso).toLocaleString(undefined, {
                year: "numeric",
                month: "short",
                day: "numeric",
                hour: "numeric",
                minute: "2-digit",
            });
        } catch {
            return "—";
        }
    };

    return (
        <Sheet open={open} onOpenChange={(v) => !v && onClose()}>
            <SheetContent
                side="right"
                className="w-full sm:max-w-lg bg-[#0a0a0a] border-l border-white/10 text-white overflow-y-auto"
                data-testid="marketing-client-drawer"
            >
                <SheetHeader>
                    <SheetTitle
                        className="text-white font-display text-2xl tracking-tight"
                        data-testid="marketing-drawer-title"
                    >
                        {client?.name || ""}
                    </SheetTitle>
                    <SheetDescription className="text-white/55">
                        {client?.company_name || "No company listed"}
                    </SheetDescription>
                </SheetHeader>

                {client && (
                    <div className="mt-6 space-y-6 text-sm">
                        <div className="grid grid-cols-2 gap-4">
                            <DetailRow label="Phone" value={client.phone_number} />
                            <DetailRow
                                label="Last Contacted"
                                value={fmt(client.last_contacted_date)}
                            />
                        </div>

                        {/* Log interaction form */}
                        <form
                            onSubmit={submitInteraction}
                            className="border border-white/10 p-4 space-y-3"
                            data-testid="marketing-log-interaction-form"
                        >
                            <div className="text-[10px] tracking-[0.2em] uppercase text-white/40">
                                Log Interaction
                            </div>
                            <div className="flex flex-wrap gap-2">
                                {INTERACTION_TYPES.map((t) => {
                                    const Icon = t.icon;
                                    const active = type === t.value;
                                    return (
                                        <button
                                            key={t.value}
                                            type="button"
                                            onClick={() => setType(t.value)}
                                            data-testid={`marketing-type-${t.value}`}
                                            className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-sm border transition-colors ${
                                                active
                                                    ? "bg-white text-black border-white"
                                                    : "border-white/15 text-white/70 hover:border-white/40"
                                            }`}
                                        >
                                            <Icon className="w-3 h-3" />
                                            {t.label}
                                        </button>
                                    );
                                })}
                            </div>
                            <textarea
                                value={notes}
                                onChange={(e) => setNotes(e.target.value)}
                                placeholder="Notes (optional)"
                                rows={3}
                                data-testid="marketing-interaction-notes"
                                className="w-full bg-white/[0.03] border border-white/10 px-3 py-2 text-sm text-white/90 focus:border-white/30 focus:outline-none rounded-sm resize-none"
                            />
                            <button
                                type="submit"
                                disabled={saving}
                                data-testid="marketing-interaction-submit-btn"
                                className="inline-flex items-center gap-2 bg-white text-black px-3 py-2 rounded-sm text-xs tracking-wide hover:opacity-90 disabled:opacity-50"
                            >
                                {saving && (
                                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                )}
                                Log Touchpoint
                            </button>
                        </form>

                        {/* Interaction history */}
                        <div>
                            <div className="text-[10px] tracking-[0.2em] uppercase text-white/40 mb-2">
                                History ({interactions.length})
                            </div>
                            {loadingList ? (
                                <div className="py-6 flex justify-center">
                                    <Loader2 className="w-4 h-4 animate-spin text-white/40" />
                                </div>
                            ) : interactions.length === 0 ? (
                                <div
                                    className="text-xs text-white/40 py-4 text-center border border-dashed border-white/10"
                                    data-testid="marketing-history-empty"
                                >
                                    No touchpoints logged yet.
                                </div>
                            ) : (
                                <ul
                                    className="space-y-2"
                                    data-testid="marketing-history-list"
                                >
                                    {interactions.map((it) => (
                                        <li
                                            key={it.id}
                                            className="border border-white/10 px-3 py-2.5"
                                            data-testid={`marketing-history-${it.id}`}
                                        >
                                            <div className="flex items-center justify-between gap-3">
                                                <span className="text-[10px] tracking-widest uppercase text-white/55">
                                                    {it.type}
                                                </span>
                                                <span className="tg-mono text-[10px] text-white/40">
                                                    {fmt(it.created_at)}
                                                </span>
                                            </div>
                                            {it.notes && (
                                                <div className="text-xs text-white/75 mt-1.5 whitespace-pre-wrap">
                                                    {it.notes}
                                                </div>
                                            )}
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </div>
                    </div>
                )}
            </SheetContent>
        </Sheet>
    );
}

function DetailRow({ label, value, mono = false }) {
    return (
        <div>
            <div className="text-[10px] tracking-[0.2em] uppercase text-white/40 mb-1">
                {label}
            </div>
            <div
                className={`text-white/85 ${mono ? "tg-mono text-xs break-all" : ""}`}
            >
                {value || "—"}
            </div>
        </div>
    );
}
