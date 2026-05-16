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
import { Plus, Loader2, Phone, Mail, Users as UsersIcon, MessageSquare, Calendar, Building2, PhoneCall, Clock, TrendingUp, Users, Activity, ChevronRight, Sparkles, Zap, Target } from "lucide-react";

/**
 * MarketingHub — enterprise-grade CRM dashboard.
 * Backed by /api/marketing/{clients,interactions}.
 */
export default function MarketingHub() {
    const [clients, setClients] = useState([]);
    const [loading, setLoading] = useState(true);
    const [activeClient, setActiveClient] = useState(null);
    const [addOpen, setAddOpen] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [filterType, setFilterType] = useState("all");

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

    const getDaysSinceContact = (date) => {
        if (!date) return null;
        const lastContact = new Date(date);
        const now = new Date();
        const diffTime = Math.abs(now - lastContact);
        const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
        return diffDays;
    };

    const getRelationshipHealth = (lastContacted) => {
        const days = getDaysSinceContact(lastContacted);
        if (!days) return { status: "inactive", label: "No contact", color: "text-rose-600 bg-rose-50", icon: Zap };
        if (days <= 7) return { status: "healthy", label: "Active", color: "text-emerald-700 bg-emerald-50", icon: TrendingUp };
        if (days <= 30) return { status: "warming", label: "Engaged", color: "text-amber-700 bg-amber-50", icon: Clock };
        return { status: "cold", label: "Needs attention", color: "text-slate-500 bg-slate-50", icon: Target };
    };

    const getMomentum = (lastContacted) => {
        const days = getDaysSinceContact(lastContacted);
        if (!days) return "No activity";
        if (days <= 3) return "High momentum";
        if (days <= 7) return "Active";
        if (days <= 14) return "Warming";
        return "Needs follow-up";
    };

    const filteredClients = clients.filter(client => {
        const matchesSearch = client.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
                             (client.company_name?.toLowerCase() || "").includes(searchQuery.toLowerCase());
        
        if (!matchesSearch) return false;
        
        if (filterType === "recent") {
            const days = getDaysSinceContact(client.last_contacted_date);
            return days !== null && days <= 7;
        }
        if (filterType === "dormant") {
            const days = getDaysSinceContact(client.last_contacted_date);
            return days === null || days > 30;
        }
        
        return true;
    });

    const handleClientCreated = (newClient) => {
        setClients((prev) => [newClient, ...prev]);
        setAddOpen(false);
        toast.success(`${newClient.name} added`);
    };

    const handleInteractionAdded = (updatedDate) => {
        if (!activeClient) return;
        const bumped = { ...activeClient, last_contacted_date: updatedDate, interaction_count: (activeClient.interaction_count || 0) + 1 };
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

    const activeClientsCount = clients.filter(c => {
        const days = getDaysSinceContact(c.last_contacted_date);
        return days !== null && days <= 7;
    }).length;
    
    const dormantClientsCount = clients.filter(c => {
        const days = getDaysSinceContact(c.last_contacted_date);
        return days === null || days > 30;
    }).length;

    return (
        <div
            className="max-w-6xl mx-auto px-6 py-10 bg-white min-h-screen"
            data-testid="marketing-hub-page"
        >
            {/* Ambient atmospheric layer */}
            <div className="fixed inset-0 pointer-events-none overflow-hidden">
                <div className="absolute -top-96 -right-96 w-[600px] h-[600px] bg-slate-100/30 rounded-full blur-3xl" />
                <div className="absolute -bottom-96 -left-96 w-[600px] h-[600px] bg-emerald-50/20 rounded-full blur-3xl" />
            </div>

            {/* Executive Dashboard Header */}
            <div className="relative mb-12">
                <div className="flex items-center justify-between gap-6 flex-wrap mb-10">
                    <div className="min-w-0">
                        <h1 className="text-4xl font-light tracking-tight text-slate-900">
                            Client Intelligence
                        </h1>
                        <p className="text-base text-slate-500 mt-2 font-light">
                            Executive relationship operating system
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => setAddOpen(true)}
                        data-testid="marketing-add-client-btn"
                        className="shrink-0 inline-flex items-center gap-2 bg-slate-900 text-white px-5 py-2.5 rounded-2xl text-sm font-medium hover:bg-slate-800 transition-all duration-200 hover:shadow-lg hover:scale-[1.02] active:scale-[0.98] whitespace-nowrap"
                    >
                        <Plus className="w-4 h-4" />
                        <span className="hidden sm:inline">Add Client</span>
                        <span className="sm:hidden">Add</span>
                    </button>
                </div>

                {/* Executive Dashboard Cards - Asymmetric hierarchy */}
                <div className="grid grid-cols-1 md:grid-cols-4 gap-5 mb-10">
                    <div className="md:col-span-2 bg-gradient-to-br from-slate-50 to-white border border-slate-200 rounded-2xl p-6 shadow-sm hover:shadow-md transition-all duration-300">
                        <div className="flex items-center justify-between mb-4">
                            <div className="flex items-center gap-2">
                                <div className="w-8 h-8 rounded-xl bg-emerald-100 flex items-center justify-center">
                                    <Activity className="w-4 h-4 text-emerald-600" />
                                </div>
                                <span className="text-xs font-mono text-slate-400">Active Pipeline</span>
                            </div>
                            <Sparkles className="w-4 h-4 text-emerald-400" />
                        </div>
                        <div className="text-4xl font-light text-slate-900 mb-1">{activeClientsCount}</div>
                        <div className="text-sm text-slate-500">Active relationships · Last 7 days</div>
                        <div className="mt-3 h-px bg-slate-200 rounded-full overflow-hidden">
                            <div className="h-full bg-slate-300 rounded-full" style={{ width: `${(activeClientsCount / (clients.length || 1)) * 100}%` }} />
                        </div>
                    </div>
                    
                    <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm hover:shadow-md transition-all duration-300">
                        <div className="flex items-center gap-2 mb-3">
                            <Users className="w-4 h-4 text-slate-400" />
                            <span className="text-xs font-mono text-slate-400">Total</span>
                        </div>
                        <div className="text-2xl font-light text-slate-900 mb-1">{clients.length}</div>
                        <div className="text-xs text-slate-500">Active clients in ecosystem</div>
                    </div>
                    
                    <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm hover:shadow-md transition-all duration-300">
                        <div className="flex items-center gap-2 mb-3">
                            <Clock className="w-4 h-4 text-amber-500" />
                            <span className="text-xs font-mono text-slate-400">Needs attention</span>
                        </div>
                        <div className="text-2xl font-light text-slate-900 mb-1">{dormantClientsCount}</div>
                        <div className="text-xs text-slate-500">Dormant · Requires outreach</div>
                    </div>
                </div>

                {/* Search and Filter Bar - Refined spacing */}
                <div className="flex flex-col lg:flex-row lg:items-center gap-5 justify-between border-b border-slate-100 pb-7">
                    <div className="relative flex-1 max-w-md">
                        <div className="absolute left-4 top-1/2 -translate-y-1/2">
                            <svg className="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                            </svg>
                        </div>
                        <input
                            type="text"
                            placeholder="Search clients or companies..."
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            className="w-full pl-11 pr-4 py-3 bg-slate-50 border border-slate-200/70 rounded-xl text-sm text-slate-700 placeholder:text-slate-400 focus:bg-white focus:border-slate-300 focus:ring-1 focus:ring-slate-300 focus:outline-none transition-all shadow-inner"
                        />
                    </div>
                    <div className="flex gap-2">
                        {[
                            { id: "all", label: "All Relationships" },
                            { id: "recent", label: "Active" },
                            { id: "dormant", label: "Needs Attention" }
                        ].map((filter) => (
                            <button
                                key={filter.id}
                                onClick={() => setFilterType(filter.id)}
                                className={`px-5 py-2 text-sm rounded-full transition-all duration-200 font-medium ${
                                    filterType === filter.id 
                                        ? "bg-slate-900 text-white shadow-sm" 
                                        : "text-slate-500 hover:bg-slate-50"
                                }`}
                            >
                                {filter.label}
                            </button>
                        ))}
                    </div>
                </div>
            </div>

            {/* Client Relationship List */}
            {loading ? (
                <div
                    className="py-20 flex justify-center"
                    data-testid="marketing-loading"
                >
                    <Loader2 className="w-6 h-6 animate-spin text-slate-300" />
                </div>
            ) : filteredClients.length === 0 ? (
                <div
                    className="border-2 border-dashed border-slate-200 rounded-2xl py-20 text-center"
                    data-testid="marketing-empty"
                >
                    <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-slate-50 mb-4">
                        <Users className="w-6 h-6 text-slate-300" />
                    </div>
                    <div className="text-slate-400 text-sm mb-2">No clients found</div>
                    <p className="text-slate-500 text-sm">Click "Add Client" to create your first relationship record.</p>
                </div>
            ) : (
                <div className="space-y-3 animate-in fade-in duration-500" data-testid="marketing-clients-list">
                    {filteredClients.map((c, idx) => {
                        const health = getRelationshipHealth(c.last_contacted_date);
                        const daysSince = getDaysSinceContact(c.last_contacted_date);
                        const momentum = getMomentum(c.last_contacted_date);
                        const HealthIcon = health.icon;
                        return (
                            <div
                                key={c.id}
                                onClick={() => setActiveClient(c)}
                                data-testid={`marketing-client-row-${c.id}`}
                                className="group bg-white border border-slate-200 rounded-2xl p-6 cursor-pointer hover:border-slate-300 hover:shadow-lg transition-all duration-300 hover:-translate-y-0.5"
                                style={{ animationDelay: `${idx * 50}ms` }}
                            >
                                <div className="flex items-start justify-between gap-6 flex-wrap">
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-3 mb-3 flex-wrap">
                                            <h3 className="text-[22px] leading-tight font-medium text-slate-900 group-hover:text-slate-700 transition-colors">
                                                {c.name}
                                            </h3>
                                            <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-lg text-xs font-medium ${health.color}`}>
                                                <HealthIcon className="w-3 h-3" />
                                                {health.label}
                                            </span>
                                        </div>
                                        <div className="flex flex-wrap gap-5 text-sm mb-3">
                                            {c.company_name && (
                                                <div className="flex items-center gap-1.5 text-slate-400">
                                                    <Building2 className="w-3.5 h-3.5" />
                                                    <span>{c.company_name}</span>
                                                </div>
                                            )}
                                            {c.phone_number && (
                                                <div className="flex items-center gap-1.5 text-slate-400">
                                                    <PhoneCall className="w-3.5 h-3.5" />
                                                    <span className="font-mono text-xs">{c.phone_number}</span>
                                                </div>
                                            )}
                                        </div>
                                        <div className="flex flex-wrap gap-5 text-xs">
                                            <div className="flex items-center gap-1.5 text-slate-400">
                                                <Calendar className="w-3.5 h-3.5" />
                                                <span>Last contact: {fmtDate(c.last_contacted_date)}</span>
                                            </div>
                                            <div className="flex items-center gap-1.5 text-slate-400">
                                                <TrendingUp className="w-3.5 h-3.5" />
                                                <span>{momentum}</span>
                                            </div>
                                            {c.interaction_count > 0 && (
                                                <div className="flex items-center gap-1.5 text-slate-400">
                                                    <MessageSquare className="w-3.5 h-3.5" />
                                                    <span>{c.interaction_count} touchpoint{c.interaction_count !== 1 ? 's' : ''}</span>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2 text-sm">
                                        <ChevronRight className="w-4 h-4 text-slate-300 group-hover:text-slate-500 transition-colors" />
                                    </div>
                                </div>
                            </div>
                        );
                    })}
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
/* Add Client dialog - Premium Enterprise                                */
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
                className="bg-white border-slate-200 text-slate-900 sm:max-w-md rounded-2xl shadow-xl"
                data-testid="marketing-add-client-dialog"
            >
                <DialogHeader>
                    <DialogTitle className="text-2xl font-light tracking-tight text-slate-900">
                        Add Client
                    </DialogTitle>
                    <DialogDescription className="text-slate-500">
                        Create a new CRM record. Only name is required.
                    </DialogDescription>
                </DialogHeader>
                <form onSubmit={submit} className="space-y-5 pt-4">
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
                    <DialogFooter className="pt-4 gap-3">
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={saving}
                            data-testid="marketing-add-cancel-btn"
                            className="px-4 py-2 text-sm text-slate-600 hover:text-slate-900 transition-colors disabled:opacity-40"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={saving}
                            data-testid="marketing-add-submit-btn"
                            className="inline-flex items-center gap-2 bg-slate-900 text-white px-5 py-2 rounded-xl text-sm font-medium hover:bg-slate-800 transition-all duration-200 disabled:opacity-50"
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
            <div className="text-xs font-medium text-slate-600 mb-1.5">
                {label}
                {required && <span className="text-slate-400 ml-0.5">*</span>}
            </div>
            <input
                type="text"
                value={value}
                onChange={(e) => onChange(e.target.value)}
                required={required}
                autoFocus={autoFocus}
                data-testid={testId}
                className="w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:bg-white focus:border-slate-300 focus:ring-1 focus:ring-slate-300 focus:outline-none transition-all"
            />
        </label>
    );
}

/* --------------------------------------------------------------------- */
/* Client drawer - Executive relationship intelligence panel            */
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

    const getInteractionIcon = (type) => {
        const found = INTERACTION_TYPES.find(t => t.value === type);
        const Icon = found?.icon || MessageSquare;
        return Icon;
    };

    const getDaysSinceContact = (date) => {
        if (!date) return null;
        const lastContact = new Date(date);
        const now = new Date();
        const diffTime = Math.abs(now - lastContact);
        const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
        return diffDays;
    };

    const daysSince = getDaysSinceContact(client?.last_contacted_date);

    return (
        <Sheet open={open} onOpenChange={(v) => !v && onClose()}>
            <SheetContent
                side="right"
                className="w-full sm:max-w-2xl bg-white border-l border-slate-200 text-slate-900 overflow-y-auto shadow-2xl p-0"
                data-testid="marketing-client-drawer"
            >
                {client && (
                    <div className="h-full flex flex-col">
                        {/* Drawer Header - With cinematic atmosphere */}
                        <div className="bg-gradient-to-b from-slate-50/80 to-white border-b border-slate-100 px-6 py-8">
                            <SheetHeader className="space-y-2">
                                <SheetTitle
                                    className="text-slate-900 text-3xl font-light tracking-tight"
                                    data-testid="marketing-drawer-title"
                                >
                                    {client.name}
                                </SheetTitle>
                                <SheetDescription className="text-slate-500 text-base">
                                    {client.company_name || "Independent relationship"}
                                </SheetDescription>
                            </SheetHeader>
                        </div>

                        {/* Scrollable Content */}
                        <div className="flex-1 overflow-y-auto px-6 py-6 space-y-8">
                            {/* Client Intelligence Section */}
                            <div className="space-y-4">
                                <div className="flex items-center gap-2">
                                    <Sparkles className="w-4 h-4 text-slate-400" />
                                    <h3 className="text-xs font-mono text-slate-400">Relationship Intelligence</h3>
                                </div>
                                <div className="grid grid-cols-2 gap-4">
                                    <div className="bg-slate-50 rounded-xl p-4">
                                        <div className="text-xs text-slate-500 mb-1">Primary Contact</div>
                                        <div className="flex items-center gap-2">
                                            <Phone className="w-3.5 h-3.5 text-slate-400" />
                                            <span className="text-sm font-mono text-slate-700">
                                                {client.phone_number || "Not provided"}
                                            </span>
                                        </div>
                                    </div>
                                    <div className="bg-slate-50 rounded-xl p-4">
                                        <div className="text-xs text-slate-500 mb-1">Last Engagement</div>
                                        <div className="flex items-center gap-2">
                                            <Calendar className="w-3.5 h-3.5 text-slate-400" />
                                            <span className="text-sm text-slate-700">
                                                {fmt(client.last_contacted_date)}
                                                {daysSince && (
                                                    <span className="ml-2 text-xs text-slate-400">({daysSince} days ago)</span>
                                                )}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                                <div className="bg-slate-50 rounded-xl p-4">
                                    <div className="text-xs text-slate-500 mb-2">Engagement Frequency</div>
                                    <div className="flex items-center justify-between">
                                        <span className="text-sm text-slate-700">{client.interaction_count || 0} total interactions</span>
                                        <div className="h-px flex-1 max-w-32 bg-slate-200 rounded-full overflow-hidden ml-3">
                                            <div className="h-full bg-slate-400 rounded-full" style={{ width: `${Math.min((client.interaction_count || 0) * 20, 100)}%` }} />
                                        </div>
                                    </div>
                                </div>
                            </div>

                            {/* Log interaction form - Refined hierarchy */}
                            <div className="space-y-4">
                                <div className="flex items-center gap-2">
                                    <MessageSquare className="w-4 h-4 text-slate-400" />
                                    <h3 className="text-xs font-mono text-slate-400">Quick Action</h3>
                                </div>
                                <form
                                    onSubmit={submitInteraction}
                                    className="space-y-4"
                                    data-testid="marketing-log-interaction-form"
                                >
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
                                                    className={`inline-flex items-center gap-2 px-4 py-2 text-sm rounded-xl border transition-all duration-200 ${
                                                        active
                                                            ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                            : "bg-white text-slate-600 border-slate-200 hover:border-slate-300 hover:bg-slate-50"
                                                    }`}
                                                >
                                                    <Icon className="w-3.5 h-3.5" />
                                                    {t.label}
                                                </button>
                                            );
                                        })}
                                    </div>
                                    <textarea
                                        value={notes}
                                        onChange={(e) => setNotes(e.target.value)}
                                        placeholder="Add notes about this interaction..."
                                        rows={3}
                                        data-testid="marketing-interaction-notes"
                                        className="w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:bg-white focus:border-slate-300 focus:ring-1 focus:ring-slate-300 focus:outline-none resize-none transition-all"
                                    />
                                    <button
                                        type="submit"
                                        disabled={saving}
                                        data-testid="marketing-interaction-submit-btn"
                                        className="inline-flex items-center gap-2 bg-slate-900 text-white px-5 py-2.5 rounded-xl text-sm font-medium hover:bg-slate-800 transition-all duration-200 disabled:opacity-50 shadow-sm"
                                    >
                                        {saving && (
                                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                        )}
                                        Log Touchpoint
                                    </button>
                                </form>
                            </div>

                            {/* Interaction timeline - Refined visuals */}
                            <div className="space-y-4">
                                <div className="flex items-center gap-2">
                                    <Clock className="w-4 h-4 text-slate-400" />
                                    <h3 className="text-xs font-mono text-slate-400">
                                        Communication Timeline ({interactions.length})
                                    </h3>
                                </div>
                                {loadingList ? (
                                    <div className="py-8 flex justify-center">
                                        <Loader2 className="w-5 h-5 animate-spin text-slate-300" />
                                    </div>
                                ) : interactions.length === 0 ? (
                                    <div
                                        className="text-sm text-slate-400 py-8 text-center border-2 border-dashed border-slate-200 rounded-xl"
                                        data-testid="marketing-history-empty"
                                    >
                                        <MessageSquare className="w-8 h-8 mx-auto mb-2 text-slate-300" />
                                        No touchpoints logged yet.
                                    </div>
                                ) : (
                                    <div className="space-y-3" data-testid="marketing-history-list">
                                        {interactions.map((it, idx) => {
                                            const Icon = getInteractionIcon(it.type);
                                            return (
                                                <div
                                                    key={it.id}
                                                    className="relative pl-6 pb-4 last:pb-0"
                                                    data-testid={`marketing-history-${it.id}`}
                                                >
                                                    {idx < interactions.length - 1 && (
                                                        <div className="absolute left-2 top-5 bottom-0 w-px bg-slate-200" />
                                                    )}
                                                    <div className="absolute left-0 top-1 w-4 h-4 rounded-full bg-slate-700 border-2 border-white shadow-sm" />
                                                    
                                                    <div className="bg-white border border-slate-200 rounded-xl p-4 ml-2 shadow-[0_2px_12px_rgba(15,23,42,0.03)] hover:shadow-md transition-shadow duration-200">
                                                        <div className="flex items-center justify-between gap-3 mb-2">
                                                            <div className="flex items-center gap-2">
                                                                <Icon className="w-3.5 h-3.5 text-slate-500" />
                                                                <span className="text-xs font-medium text-slate-600">
                                                                    {it.type}
                                                                </span>
                                                            </div>
                                                            <span className="font-mono text-[10px] text-slate-400">
                                                                {fmt(it.created_at)}
                                                            </span>
                                                        </div>
                                                        {it.notes && (
                                                            <div className="text-sm text-slate-700 mt-2 whitespace-pre-wrap">
                                                                {it.notes}
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                )}
            </SheetContent>
        </Sheet>
    );
}
