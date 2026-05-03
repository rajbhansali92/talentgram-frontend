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
import { Plus, Loader2 } from "lucide-react";

/**
 * MarketingHub — lightweight CRM dashboard.
 *
 * Scope is intentionally minimal (per spec): one table, one drawer on
 * row click, one placeholder "+ Add Client" button. Hooks into the
 * `/api/marketing/clients` endpoint that v38j shipped.
 *
 * Out of scope for this iteration:
 *   • Add-client modal (button is a placeholder)
 *   • Interaction-logging UI inside the drawer (drawer just shows the
 *     client's stored fields today; interactions API is wired and ready
 *     but will be rendered in a follow-up)
 *   • Search / filter / pagination (the endpoint returns all rows —
 *     fine for <500 clients; add pagination when volume grows)
 */
export default function MarketingHub() {
    const [clients, setClients] = useState([]);
    const [loading, setLoading] = useState(true);
    const [activeClient, setActiveClient] = useState(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/marketing/clients");
            setClients(Array.isArray(data) ? data : []);
        } catch (e) {
            toast.error(
                e?.response?.data?.detail || "Failed to load clients",
            );
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
            const d = new Date(iso);
            return d.toLocaleDateString(undefined, {
                year: "numeric",
                month: "short",
                day: "numeric",
            });
        } catch {
            return "—";
        }
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
                    onClick={() =>
                        toast.message("Add-client form coming soon.", {
                            description:
                                "POST /api/marketing/clients is already live — wiring up the modal next.",
                        })
                    }
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
                                    <td className="px-4 py-3 font-medium">
                                        {c.name}
                                    </td>
                                    <td className="px-4 py-3 text-white/70">
                                        {c.company_name || "—"}
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

            <ClientDrawer
                client={activeClient}
                onClose={() => setActiveClient(null)}
            />
        </div>
    );
}

/**
 * ClientDrawer — side sheet that slides in when a table row is clicked.
 * Kept in this file for now (tiny surface, per spec "keep UI simple").
 * When the drawer grows beyond ~100 LoC we'll extract it to its own
 * component file.
 */
function ClientDrawer({ client, onClose }) {
    const open = !!client;
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
                className="w-full sm:max-w-md bg-[#0a0a0a] border-l border-white/10 text-white"
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
                    <div className="mt-6 space-y-5 text-sm">
                        <DetailRow label="Phone" value={client.phone_number} />
                        <DetailRow
                            label="Created"
                            value={fmt(client.created_at)}
                        />
                        <DetailRow
                            label="Last Contacted"
                            value={fmt(client.last_contacted_date)}
                        />
                        <DetailRow label="Client ID" value={client.id} mono />
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
