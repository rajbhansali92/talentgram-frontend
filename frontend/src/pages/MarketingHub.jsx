import React, { useCallback, useEffect, useState, useMemo, useRef } from "react";
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
import { 
    Plus, Loader2, Phone, Mail, Users as UsersIcon, MessageSquare, 
    Calendar, Building2, PhoneCall, Clock, TrendingUp, Users, Activity, 
    ChevronRight, Sparkles, Zap, Target, AlertCircle, Edit2, Share2, DollarSign, X, Check, ChevronDown
} from "lucide-react";

const CONTACT_TYPES = [
    { value: "brand_manager", label: "Brand Manager", group: "Brand & Marketing" },
    { value: "marketing_manager", label: "Marketing Manager", group: "Brand & Marketing" },
    { value: "influencer_marketing", label: "Influencer Marketing Manager", group: "Brand & Marketing" },
    { value: "creative_director", label: "Creative Director", group: "Brand & Marketing" },
    { value: "agency_producer", label: "Agency Producer", group: "Brand & Marketing" },

    { value: "casting_director", label: "Casting Director", group: "Casting" },
    { value: "casting_assistant", label: "Casting Assistant", group: "Casting" },
    { value: "casting_company", label: "Casting Company", group: "Casting" },

    { value: "producer", label: "Producer", group: "Production" },
    { value: "executive_producer", label: "Executive Producer", group: "Production" },
    { value: "production_house", label: "Production House", group: "Production" },
    { value: "line_producer", label: "Line Producer", group: "Production" },

    { value: "talent_agency", label: "Talent Agency", group: "Agency" },
    { value: "modeling_agency", label: "Modeling Agency", group: "Agency" },
    { value: "casting_agency", label: "Casting Agency", group: "Agency" }
];

// ============================================================================
// UTILITY FUNCTIONS - Centralized
// ============================================================================

const formatDate = (iso) => {
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

const formatDateTime = (iso) => {
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

const formatCurrency = (val) => {
    if (val === undefined || val === null) return "—";
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

const getDaysSinceContact = (date) => {
    if (!date) return null;
    const lastContact = new Date(date);
    const now = new Date();
    const diffTime = now - lastContact;
    if (diffTime < 0) return 0; // Future date edge case
    return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
};

const getRelationshipHealth = (client) => {
    const stage = client.stage || "lead";
    if (stage === "key_account") {
        return { status: "key_account", label: "Key Account", color: "text-[#B89B5E] bg-[#B89B5E]/8 border-[#B89B5E]/20", icon: Sparkles };
    }
    const days = getDaysSinceContact(client.last_contacted_date);
    if (days === null) {
        return { status: "inactive", label: "New Lead", color: "text-slate-500 bg-slate-50 border-slate-200/60", icon: Zap };
    }
    if (days <= 7) {
        return { status: "healthy", label: "Active", color: "text-[#5A7D5A] bg-[#5A7D5A]/8 border-[#5A7D5A]/20", icon: TrendingUp };
    }
    if (days <= 30) {
        return { status: "warming", label: "Engaged", color: "text-amber-700 bg-amber-50 border-amber-200/50", icon: Clock };
    }
    return { status: "cold", label: "Needs attention", color: "text-[#9E4A4A] bg-[#9E4A4A]/8 border-[#9E4A4A]/20", icon: Target };
};

const getMomentum = (lastContacted) => {
    const days = getDaysSinceContact(lastContacted);
    if (days === null) return "New relationship record";
    if (days <= 3) return "High momentum";
    if (days <= 7) return "Active conversation";
    if (days <= 14) return "Engaged";
    return "Needs follow-up outreach";
};

// ============================================================================
// Skeleton Loading Component
// ============================================================================

const ClientCardSkeleton = () => (
    <div className="bg-white border border-slate-200 rounded-2xl p-6 animate-pulse shadow-sm">
        <div className="flex items-start justify-between gap-6 flex-wrap">
            <div className="flex-1">
                <div className="flex items-center gap-3 mb-3">
                    <div className="h-7 w-48 bg-slate-200 rounded-lg" />
                    <div className="h-5 w-16 bg-slate-200 rounded-lg" />
                </div>
                <div className="flex gap-5 mb-3">
                    <div className="h-5 w-32 bg-slate-200 rounded" />
                    <div className="h-5 w-28 bg-slate-200 rounded" />
                </div>
                <div className="flex gap-5">
                    <div className="h-4 w-24 bg-slate-200 rounded" />
                    <div className="h-4 w-20 bg-slate-200 rounded" />
                </div>
            </div>
            <div className="w-4 h-4 bg-slate-200 rounded" />
        </div>
    </div>
);

// ============================================================================
// Error State Component
// ============================================================================

const ErrorState = ({ message, onRetry }) => (
    <div className="border-2 border-rose-100 bg-rose-50/30 rounded-2xl py-16 sm:py-20 text-center">
        <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-rose-50 border border-rose-200/50 mb-4">
            <AlertCircle className="w-6 h-6 text-[#9E4A4A]" />
        </div>
        <div className="text-[#9E4A4A] text-sm font-medium mb-2">Failed to load relationship ecosystem</div>
        <p className="text-slate-500 text-sm mb-4 max-w-xs mx-auto leading-relaxed">{message || "Please check network or backend credentials."}</p>
        <button
            onClick={onRetry}
            className="inline-flex items-center gap-2 px-5 py-2 bg-white border border-slate-200 hover:border-slate-300 rounded-xl text-sm text-slate-700 hover:text-slate-900 transition-colors shadow-sm"
        >
            <Loader2 className="w-3.5 h-3.5" />
            Retry Connection
        </button>
    </div>
);

// ============================================================================
// Empty State Component
// ============================================================================

const EmptyState = ({ hasSearch, hasFilters, onClearFilters, onAddClient }) => (
    <div className="border-2 border-dashed border-slate-200 rounded-2xl py-16 sm:py-20 text-center bg-slate-50/30">
        <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-slate-100/50 mb-4 border border-slate-200/30">
            <Users className="w-6 h-6 text-slate-400" />
        </div>
        {hasSearch || hasFilters ? (
            <>
                <div className="text-slate-800 text-sm font-medium mb-1">No matching clients found</div>
                <p className="text-slate-500 text-xs max-w-xs mx-auto">Try refining your fuzzy match filter, lifecycle stage selectors, or query terms.</p>
                <button
                    onClick={onClearFilters}
                    className="mt-4 text-xs font-semibold px-4 py-2 border border-slate-200 bg-white hover:border-slate-300 rounded-xl text-slate-600 hover:text-slate-800 shadow-sm transition-colors"
                >
                    Clear Search Filters
                </button>
            </>
        ) : (
            <>
                <div className="text-slate-800 text-sm font-medium mb-1">Ecosystem Empty</div>
                <p className="text-slate-500 text-xs max-w-xs mx-auto mb-5">Begin scaling your production relationships by cataloging your first corporate lead.</p>
                <button
                    onClick={onAddClient}
                    className="inline-flex items-center gap-1.5 bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-medium hover:bg-slate-800 transition-colors shadow-sm"
                >
                    <Plus className="w-3.5 h-3.5" /> Add First Client
                </button>
            </>
        )}
    </div>
);

// ============================================================================
// Field Input Component
// ============================================================================

const FieldInput = ({ label, value, onChange, required, placeholder, testId, autoFocus }) => (
    <label className="block">
        <div className="text-[10px] tracking-[0.08em] font-semibold text-slate-500 uppercase font-mono flex justify-between select-none">
            <span>{label}</span>
            {required && <span className="text-[#9E4A4A]">* Required</span>}
        </div>
        <input
            type="text"
            value={value || ""}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            required={required}
            autoFocus={autoFocus}
            data-testid={testId}
            className="mt-1.5 w-full bg-slate-50/40 rounded-xl border border-slate-200/80 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-2.5 px-4 text-[15px] sm:text-sm text-slate-900 placeholder:text-slate-400 transition-all duration-200 shadow-sm"
        />
    </label>
);

// ============================================================================
// MAIN COMPONENT
// ============================================================================

export default function MarketingHub() {
    const [clients, setClients] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [activeClient, setActiveClient] = useState(null);
    const [addOpen, setAddOpen] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [filterType, setFilterType] = useState("all");
    const [selectedContactType, setSelectedContactType] = useState("all");
    const [recentSearches, setRecentSearches] = useState([]);
    const [focusedIndex, setFocusedIndex] = useState(-1);

    const searchInputRef = useRef(null);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const { data } = await adminApi.get("/marketing/clients");
            const clientsData = Array.isArray(data) ? data : (data?.items || []);
            setClients(clientsData);
        } catch (e) {
            const errorMsg = e?.response?.data?.detail || "Failed to load clients";
            setError(errorMsg);
            toast.error(errorMsg);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    // Load recent searches on mount
    useEffect(() => {
        const stored = localStorage.getItem("tg_crm_recent_searches");
        if (stored) {
            try { setRecentSearches(JSON.parse(stored)); } catch (e) { console.error(e); }
        }
    }, []);

    // Save recent searches when queries match successfully
    const saveSearchQuery = useCallback((query) => {
        if (!query || query.trim().length < 2) return;
        const q = query.trim();
        setRecentSearches(prev => {
            const next = [q, ...prev.filter(x => x.toLowerCase() !== q.toLowerCase())].slice(0, 4);
            localStorage.setItem("tg_crm_recent_searches", JSON.stringify(next));
            return next;
        });
    }, []);

    // Filter logic including multi-field tags fuzzy match
    const filteredClients = useMemo(() => {
        return clients.filter(client => {
            const query = searchQuery.trim().toLowerCase();
            const tagsString = (client.tags || []).join(" ").toLowerCase();
            const matchesSearch = query === "" || 
                client.name?.toLowerCase().includes(query) ||
                client.company_name?.toLowerCase().includes(query) ||
                client.phone_number?.includes(query) ||
                client.email?.toLowerCase().includes(query) ||
                tagsString.includes(query);
            
            if (!matchesSearch) return false;
            
            if (selectedContactType !== "all" && client.contact_type !== selectedContactType) {
                return false;
            }
            
            if (filterType === "recent") {
                const days = getDaysSinceContact(client.last_contacted_date);
                return days !== null && days <= 7 && client.stage !== "key_account";
            }
            if (filterType === "dormant") {
                const days = getDaysSinceContact(client.last_contacted_date);
                return days === null || days > 30;
            }
            if (filterType === "high_value") {
                return client.stage === "key_account";
            }
            if (filterType === "lead") {
                return client.stage === "lead";
            }
            
            return true;
        });
    }, [clients, searchQuery, filterType, selectedContactType]);

    // Derived Statistics Dashboard
    const stats = useMemo(() => {
        const active = clients.filter(c => {
            const days = getDaysSinceContact(c.last_contacted_date);
            return days !== null && days <= 7 && c.stage !== "key_account";
        }).length;
        const dormant = clients.filter(c => {
            const days = getDaysSinceContact(c.last_contacted_date);
            return days === null || days > 30;
        }).length;
        const keyAccounts = clients.filter(c => c.stage === "key_account").length;
        return { active, dormant, keyAccounts, total: clients.length };
    }, [clients]);

    const contactTypeCounts = useMemo(() => {
        const counts = {};
        CONTACT_TYPES.forEach(t => {
            counts[t.value] = 0;
        });
        clients.forEach(c => {
            if (c.contact_type && counts[c.contact_type] !== undefined) {
                counts[c.contact_type]++;
            }
        });
        return counts;
    }, [clients]);

    // Handle arrow keys and CMD+K keyboard focus shortcuts
    useEffect(() => {
        const handleKeyDown = (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "k") {
                e.preventDefault();
                searchInputRef.current?.focus();
                setFocusedIndex(-1);
            } else if (e.key === "/") {
                const activeEl = document.activeElement;
                if (activeEl && ["INPUT", "TEXTAREA"].includes(activeEl.tagName)) return;
                e.preventDefault();
                searchInputRef.current?.focus();
                setFocusedIndex(-1);
            } else if (e.key === "ArrowDown" && filteredClients.length > 0) {
                e.preventDefault();
                setFocusedIndex(prev => Math.min(prev + 1, filteredClients.length - 1));
            } else if (e.key === "ArrowUp" && filteredClients.length > 0) {
                e.preventDefault();
                setFocusedIndex(prev => Math.max(prev - 1, 0));
            } else if (e.key === "Enter" && focusedIndex >= 0 && focusedIndex < filteredClients.length) {
                e.preventDefault();
                setActiveClient(filteredClients[focusedIndex]);
            }
        };

        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [filteredClients, focusedIndex]);

    // Handle search selection callbacks
    useEffect(() => {
        if (searchQuery) {
            const timer = setTimeout(() => saveSearchQuery(searchQuery), 3000);
            return () => clearTimeout(timer);
        }
    }, [searchQuery, saveSearchQuery]);

    const handleClientCreated = useCallback((newClient) => {
        setClients(prev => [newClient, ...prev]);
        setAddOpen(false);
        toast.success(`Client ${newClient.name} successfully registered.`);
    }, []);

    const handleClientUpdated = useCallback((updatedClient) => {
        setClients(prev => prev.map(c => c.id === updatedClient.id ? updatedClient : c));
        setActiveClient(updatedClient);
    }, []);

    const handleClientDeleted = useCallback((deletedId) => {
        setClients(prev => prev.filter(c => c.id !== deletedId));
        setActiveClient(null);
    }, []);

    const handleInteractionAdded = useCallback((updatedDate) => {
        if (!activeClient) return;
        const bumped = { 
            ...activeClient, 
            last_contacted_date: updatedDate, 
            interaction_count: (activeClient.interaction_count || 0) + 1 
        };
        setActiveClient(bumped);
        setClients(prev => {
            const next = prev.map(c => c.id === bumped.id ? bumped : c);
            next.sort((a, b) => {
                const da = new Date(a.last_contacted_date || 0).getTime();
                const db = new Date(b.last_contacted_date || 0).getTime();
                return db - da;
            });
            return next;
        });
    }, [activeClient]);

    const clearFilters = useCallback(() => {
        setFilterType("all");
        setSelectedContactType("all");
        setSearchQuery("");
        setFocusedIndex(-1);
    }, []);

    const hasActiveFilters = filterType !== "all" || selectedContactType !== "all" || searchQuery;

    // Premium Editorial UI Header
    return (
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10 bg-white min-h-screen relative" data-testid="marketing-hub-page">
            {/* Ambient executive light layout backdrop */}
            <div className="absolute inset-0 pointer-events-none overflow-hidden">
                <div className="absolute top-0 right-1/4 w-[380px] h-[380px] bg-slate-50/50 rounded-full blur-3xl" />
                <div className="absolute bottom-12 left-10 w-[380px] h-[380px] bg-[#5A7D5A]/3 rounded-full blur-3xl" />
            </div>

            {/* Header Dashboard Surface */}
            <div className="relative mb-8 sm:mb-12">
                <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-5 mb-8 sm:mb-10">
                    <div>
                        <h1 className="text-3xl sm:text-4xl font-light tracking-tight text-slate-900 font-display">
                            Client Intelligence
                        </h1>
                        <p className="text-sm text-slate-500 mt-2 font-mono tracking-tight flex items-center gap-1.5">
                            <Activity className="w-3.5 h-3.5 text-slate-400" />
                            Executive Relationship Operating System
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => setAddOpen(true)}
                        data-testid="marketing-add-client-btn"
                        className="shrink-0 inline-flex items-center gap-2 bg-slate-900 text-white px-5 py-3 rounded-2xl text-xs font-semibold hover:bg-slate-800 transition-all hover:shadow-md hover:scale-[1.01] active:scale-[0.98] whitespace-nowrap"
                    >
                        <Plus className="w-4 h-4" /> Add Corporate Client
                    </button>
                </div>

                {/* Dashboard Stats linked filters */}
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-5 mb-8 sm:mb-10 select-none">
                    {[
                        { id: "all", count: stats.total, label: "Total Accounts", desc: "Relationships cataloged", color: "hover:border-slate-400", activeBg: "bg-slate-50/50 border-slate-900 shadow-sm" },
                        { id: "recent", count: stats.active, label: "Active Pipeline", desc: "Contacted this week", color: "hover:border-[#5A7D5A]/40", activeBg: "bg-[#5A7D5A]/3 border-[#5A7D5A] shadow-sm" },
                        { id: "dormant", count: stats.dormant, label: "Needs Outreach", desc: "Inactive for > 30 days", color: "hover:border-[#9E4A4A]/40", activeBg: "bg-[#9E4A4A]/3 border-[#9E4A4A] shadow-sm" },
                        { id: "high_value", count: stats.keyAccounts, label: "Key Accounts", desc: "Flagged strategic partners", color: "hover:border-[#B89B5E]/40", activeBg: "bg-[#B89B5E]/3 border-[#B89B5E] shadow-sm" }
                    ].map((st) => {
                        const active = filterType === st.id;
                        return (
                            <div
                                key={st.id}
                                onClick={() => { setFilterType(st.id); setFocusedIndex(-1); }}
                                className={`cursor-pointer bg-white border ${active ? st.activeBg : `border-slate-200 ${st.color}`} rounded-2xl p-3.5 sm:p-5 transition-all duration-200 hover:shadow-sm`}
                            >
                                <div className="text-xl sm:text-2xl font-bold text-slate-950 mb-0.5">{st.count}</div>
                                <div className="text-[11px] font-semibold text-slate-800 tracking-tight">{st.label}</div>
                                <div className="text-[9.5px] text-slate-500 font-medium mt-0.5 leading-snug">{st.desc}</div>
                            </div>
                        );
                    })}
                </div>

                {/* Intelligent Search Input */}
                <div className="space-y-4 border-b border-slate-100 pb-6 sm:pb-7">
                    <div className="flex flex-col lg:flex-row lg:items-center gap-4 justify-between">
                        <div className="relative flex-1 max-w-full lg:max-w-lg">
                            <div className="absolute left-4 top-1/2 -translate-y-1/2">
                                <svg className="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                                </svg>
                            </div>
                            <input
                                ref={searchInputRef}
                                type="text"
                                placeholder="Search by name, company, phone, or tags... (CMD+K / /)"
                                value={searchQuery}
                                onChange={(e) => { setSearchQuery(e.target.value); setFocusedIndex(-1); }}
                                className="w-full pl-11 pr-10 py-3.5 bg-slate-50/50 border border-slate-200 rounded-2xl text-sm text-slate-700 placeholder:text-slate-400 focus:bg-white focus:border-slate-400 focus:ring-1 focus:ring-slate-400 focus:outline-none transition-all shadow-inner font-sans"
                            />
                            {searchQuery && (
                                <button
                                    onClick={() => { setSearchQuery(""); searchInputRef.current?.focus(); }}
                                    className="absolute right-3.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 transition-colors"
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            )}
                        </div>
                        <div className="flex items-center gap-1.5 text-xs text-slate-400 font-mono">
                            <span className="bg-slate-50 border border-slate-200 px-2 py-1 rounded">⌘K</span>
                            <span>to focus search</span>
                        </div>
                    </div>

                    {/* Local searches pills */}
                    {recentSearches.length > 0 && (
                        <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider font-mono">Recent:</span>
                            {recentSearches.map((s, idx) => (
                                <button
                                    key={`${s}-${idx}`}
                                    onClick={() => { setSearchQuery(s); setFocusedIndex(-1); }}
                                    className="inline-flex items-center gap-1 px-3 py-1 bg-slate-50 hover:bg-slate-100 border border-slate-200 text-xs text-slate-600 hover:text-slate-900 rounded-full transition-colors font-medium shadow-sm"
                                >
                                    {s}
                                </button>
                            ))}
                            <button
                                onClick={() => {
                                    localStorage.removeItem("tg_crm_recent_searches");
                                    setRecentSearches([]);
                                }}
                                className="text-[10px] text-[#9E4A4A] hover:underline font-medium font-mono"
                            >
                                Clear History
                            </button>
                        </div>
                    )}
                    {/* Contact Type filters strip */}
                    <div className="flex items-center gap-2 overflow-x-auto pb-2 scrollbar-none pt-2 border-t border-slate-50">
                        <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider font-mono shrink-0 select-none">Type:</span>
                        <button
                            type="button"
                            onClick={() => { setSelectedContactType("all"); setFocusedIndex(-1); }}
                            className={`inline-flex items-center gap-1.5 px-3 py-1 border rounded-full text-xs font-semibold whitespace-nowrap transition-all duration-200 ${
                                selectedContactType === "all"
                                    ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                    : "bg-white text-slate-600 border-slate-200 hover:border-slate-300"
                            }`}
                        >
                            All Types
                        </button>
                        {CONTACT_TYPES.map((t) => {
                            const count = contactTypeCounts[t.value] || 0;
                            const active = selectedContactType === t.value;
                            return (
                                <button
                                    key={t.value}
                                    type="button"
                                    onClick={() => { setSelectedContactType(active ? "all" : t.value); setFocusedIndex(-1); }}
                                    className={`inline-flex items-center gap-1.5 px-3 py-1 border rounded-full text-xs font-semibold whitespace-nowrap transition-all duration-200 ${
                                        active
                                            ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                            : "bg-white text-slate-600 border-slate-200 hover:border-slate-300"
                                    }`}
                                >
                                    <span>{t.label}</span>
                                    <span className={`text-[9px] px-1.5 py-0.5 rounded-full font-mono font-bold ${active ? "bg-white/20 text-white" : "bg-slate-100 text-slate-500"}`}>
                                        {count}
                                    </span>
                                </button>
                            );
                        })}
                    </div>
                </div>
            </div>

            {/* List with keyboard index highlights */}
            {loading ? (
                <div className="space-y-4" data-testid="marketing-loading">
                    <ClientCardSkeleton />
                    <ClientCardSkeleton />
                    <ClientCardSkeleton />
                </div>
            ) : error ? (
                <ErrorState message={error} onRetry={load} />
            ) : filteredClients.length === 0 ? (
                <EmptyState 
                    hasSearch={!!searchQuery} 
                    hasFilters={hasActiveFilters} 
                    onClearFilters={clearFilters}
                    onAddClient={() => setAddOpen(true)}
                />
            ) : (
                <div className="space-y-3" data-testid="marketing-clients-list">
                    {filteredClients.map((c, i) => {
                        const health = getRelationshipHealth(c);
                        const momentum = getMomentum(c.last_contacted_date);
                        const HealthIcon = health.icon;
                        const isFocused = focusedIndex === i;
                        const initial = c.name?.charAt(0) || "C";
                        
                        return (
                            <div
                                key={c.id}
                                onClick={() => { setActiveClient(c); setFocusedIndex(i); }}
                                data-testid={`marketing-client-row-${c.id}`}
                                className={`group bg-white border rounded-2xl p-4 sm:p-5 cursor-pointer transition-all duration-200 hover:shadow-md ${
                                    isFocused 
                                        ? "border-slate-900 ring-1 ring-slate-950 bg-slate-50/20" 
                                        : "border-slate-200 hover:border-slate-300"
                                }`}
                            >
                                <div className="flex items-start gap-3 sm:gap-5">
                                    {/* Glassmorphic Initial Avatar */}
                                    <div className="w-10 h-10 sm:w-12 sm:h-12 rounded-xl sm:rounded-2xl bg-slate-50 border border-slate-200 flex items-center justify-center font-display text-base sm:text-lg font-medium text-slate-800 shrink-0 shadow-sm">
                                        {initial}
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2.5 mb-1.5 flex-wrap">
                                            <h3 className="text-base sm:text-lg leading-tight font-semibold text-slate-900">
                                                {c.name}
                                            </h3>
                                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-[9px] font-bold uppercase tracking-wider border ${health.color}`}>
                                                <HealthIcon className="w-2.5 h-2.5" />
                                                {health.label}
                                            </span>
                                            {c.value && (
                                                <span className="inline-flex items-center gap-0.5 px-2 py-0.5 bg-[#B89B5E]/6 border border-[#B89B5E]/15 rounded-lg text-[9px] font-mono font-bold text-[#B89B5E]">
                                                    {formatCurrency(c.value)}
                                                </span>
                                            )}
                                        </div>

                                        <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-xs mb-2.5 text-slate-600">
                                            {c.company_name && (
                                                <div className="flex items-center gap-1">
                                                    <Building2 className="w-3.5 h-3.5 text-slate-400" />
                                                    <span>{c.company_name}</span>
                                                </div>
                                            )}
                                            {c.email && (
                                                <div className="flex items-center gap-1 font-mono">
                                                    <Mail className="w-3.5 h-3.5 text-slate-400" />
                                                    <span className="break-all">{c.email}</span>
                                                </div>
                                            )}
                                            {c.phone_number && (
                                                <div className="flex items-center gap-1 font-mono">
                                                    <PhoneCall className="w-3.5 h-3.5 text-slate-400" />
                                                    <span>{c.phone_number}</span>
                                                </div>
                                            )}
                                        </div>

                                        {/* Tags rendering */}
                                        {c.tags && c.tags.length > 0 && (
                                            <div className="flex gap-1.5 flex-wrap mb-3 font-mono">
                                                {c.tags.map(t => (
                                                    <span key={t} className="inline-block px-2 py-0.5 bg-slate-50 border border-slate-200/80 rounded-md text-[10px] text-slate-500">
                                                        #{t}
                                                    </span>
                                                ))}
                                            </div>
                                        )}

                                        <div className="flex flex-wrap gap-x-3.5 gap-y-1 text-[10px] text-slate-500 font-mono">
                                            <div className="flex items-center gap-1">
                                                <Calendar className="w-3.5 h-3.5 text-slate-400" />
                                                <span>Contact: {formatDate(c.last_contacted_date)}</span>
                                            </div>
                                            <div className="flex items-center gap-1">
                                                <TrendingUp className="w-3.5 h-3.5 text-slate-400" />
                                                <span>{momentum}</span>
                                            </div>
                                            {c.interaction_count > 0 && (
                                                <div className="flex items-center gap-1">
                                                    <MessageSquare className="w-3.5 h-3.5 text-slate-400" />
                                                    <span>{c.interaction_count} log{c.interaction_count !== 1 ? 's' : ''}</span>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                    <div className="shrink-0 flex items-center h-10 sm:h-12">
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
                onClientUpdated={handleClientUpdated}
                onClientDeleted={handleClientDeleted}
                onInteractionAdded={handleInteractionAdded}
            />
        </div>
    );
}

// ============================================================================
// ADD CLIENT DIALOG (UPGRADED)
// ============================================================================

function AddClientDialog({ open, onClose, onCreated }) {
    const [name, setName] = useState("");
    const [company, setCompany] = useState("");
    const [phone, setPhone] = useState("");
    const [email, setEmail] = useState("");
    const [stage, setStage] = useState("lead");
    const [value, setValue] = useState("");
    const [tags, setTags] = useState("");
    const [contactType, setContactType] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) {
            setName("");
            setCompany("");
            setPhone("");
            setEmail("");
            setStage("lead");
            setValue("");
            setTags("");
            setContactType("");
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
            const tagsList = tags.split(",").map(t => t.trim()).filter(Boolean);
            const valNum = value.trim() ? parseFloat(value) : null;
            const { data } = await adminApi.post("/marketing/clients", {
                name: name.trim(),
                company_name: company.trim() || null,
                phone_number: phone.trim() || null,
                email: email.trim() || null,
                stage: stage,
                value: valNum,
                tags: tagsList,
                contact_type: contactType || null
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
                className="bg-white border-slate-200 text-slate-900 sm:max-w-lg rounded-2xl shadow-xl overflow-hidden p-0"
                data-testid="marketing-add-client-dialog"
            >
                <div className="bg-slate-50 border-b border-slate-100 p-6">
                    <DialogHeader>
                        <DialogTitle className="text-2xl font-light tracking-tight text-slate-950 font-display">
                            Create Client Record
                        </DialogTitle>
                        <DialogDescription className="text-slate-500 text-xs">
                            Establish a new corporate relationship file inside your executive CRM.
                        </DialogDescription>
                    </DialogHeader>
                </div>
                <form onSubmit={submit} className="p-6 space-y-4">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <FieldInput
                            label="Name"
                            value={name}
                            onChange={setName}
                            required
                            placeholder="E.g. David Selznick"
                            testId="marketing-input-name"
                            autoFocus
                        />
                        <FieldInput
                            label="Company Name"
                            value={company}
                            onChange={setCompany}
                            placeholder="E.g. Metro-Goldwyn-Mayer"
                            testId="marketing-input-company"
                        />
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <FieldInput
                            label="Phone Number"
                            value={phone}
                            onChange={setPhone}
                            placeholder="E.g. +91 9999999999"
                            testId="marketing-input-phone"
                        />
                        <FieldInput
                            label="Email Address"
                            value={email}
                            onChange={setEmail}
                            placeholder="E.g. david@mgm.com"
                            testId="marketing-input-email"
                        />
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <label className="block">
                            <div className="text-[10px] tracking-[0.08em] font-semibold text-slate-500 uppercase font-mono mb-1.5 flex justify-between select-none">
                                <span>Lifecycle Stage</span>
                            </div>
                            <select
                                value={stage}
                                onChange={(e) => setStage(e.target.value)}
                                className="mt-1.5 w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:bg-white focus:border-slate-300 focus:outline-none transition-colors"
                            >
                                <option value="lead">New Lead</option>
                                <option value="active">Active partner</option>
                                <option value="key_account">Key Account (High Value)</option>
                            </select>
                        </label>
                        <FieldInput
                            label="Deal/Relationship Value (INR)"
                            value={value}
                            onChange={setValue}
                            placeholder="E.g. 500000"
                            testId="marketing-input-value"
                        />
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <label className="block">
                            <div className="text-[10px] tracking-[0.08em] font-semibold text-slate-500 uppercase font-mono mb-1.5 flex justify-between select-none">
                                <span>Contact Type</span>
                            </div>
                            <select
                                value={contactType}
                                onChange={(e) => setContactType(e.target.value)}
                                className="mt-1.5 w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:bg-white focus:border-slate-300 focus:outline-none transition-colors"
                            >
                                <option value="">Select Type (optional)</option>
                                <optgroup label="Brand & Marketing">
                                    <option value="brand_manager">Brand Manager</option>
                                    <option value="marketing_manager">Marketing Manager</option>
                                    <option value="influencer_marketing">Influencer Marketing Manager</option>
                                    <option value="creative_director">Creative Director</option>
                                    <option value="agency_producer">Agency Producer</option>
                                </optgroup>
                                <optgroup label="Casting">
                                    <option value="casting_director">Casting Director</option>
                                    <option value="casting_assistant">Casting Assistant</option>
                                    <option value="casting_company">Casting Company</option>
                                </optgroup>
                                <optgroup label="Production">
                                    <option value="producer">Producer</option>
                                    <option value="executive_producer">Executive Producer</option>
                                    <option value="production_house">Production House</option>
                                    <option value="line_producer">Line Producer</option>
                                </optgroup>
                                <optgroup label="Agency">
                                    <option value="talent_agency">Talent Agency</option>
                                    <option value="modeling_agency">Modeling Agency</option>
                                    <option value="casting_agency">Casting Agency</option>
                                </optgroup>
                            </select>
                        </label>
                        <FieldInput
                            label="Relationship Tags (Comma-separated)"
                            value={tags}
                            onChange={setTags}
                            placeholder="E.g. Producer, Mumbai"
                            testId="marketing-input-tags"
                        />
                    </div>

                    <DialogFooter className="pt-4 border-t border-slate-100 gap-3">
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={saving}
                            data-testid="marketing-add-cancel-btn"
                            className="px-4 py-2.5 text-sm font-semibold text-slate-600 hover:text-slate-900 transition-colors disabled:opacity-40"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={saving}
                            data-testid="marketing-add-submit-btn"
                            className="inline-flex items-center justify-center gap-2 bg-slate-900 text-white px-5 py-2.5 rounded-xl text-sm font-semibold hover:bg-slate-800 transition-colors disabled:opacity-50 min-w-36 shadow-sm"
                        >
                            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : "Save Client"}
                        </button>
                    </DialogFooter>
                </form>
            </DialogContent>
        </Dialog>
    );
}

// ============================================================================
// CLIENT DRAWER (UPGRADED EXECUTIVE RELATIONSHIP DASHBOARD)
// ============================================================================

const INTERACTION_TYPES = [
    { value: "call", label: "Call", icon: Phone },
    { value: "email", label: "Email", icon: Mail },
    { value: "meeting", label: "Meeting", icon: UsersIcon },
    { value: "whatsapp", label: "WhatsApp", icon: MessageSquare },
];

function ClientDrawer({ client, onClose, onClientUpdated, onClientDeleted, onInteractionAdded }) {
    const open = !!client;
    const [interactions, setInteractions] = useState([]);
    const [loadingList, setLoadingList] = useState(false);
    const [type, setType] = useState("call");
    const [notes, setNotes] = useState("");
    const [saving, setSaving] = useState(false);

    // Editing states
    const [isEditing, setIsEditing] = useState(false);
    const [editName, setEditName] = useState("");
    const [editCompany, setEditCompany] = useState("");
    const [editPhone, setEditPhone] = useState("");
    const [editEmail, setEditEmail] = useState("");
    const [editStage, setEditStage] = useState("lead");
    const [editValue, setEditValue] = useState("");
    const [editTags, setEditTags] = useState("");
    const [editContactType, setEditContactType] = useState("");
    const [updating, setUpdating] = useState(false);

    // Escape key listener to close drawer
    useEffect(() => {
        if (!open) return;
        const handleKeyDown = (e) => {
            if (e.key === "Escape") {
                onClose();
            }
        };
        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [open, onClose]);

    const handleArchive = async () => {
        if (!client) return;
        if (!window.confirm(`Are you sure you want to archive ${client.name}?`)) return;
        
        const requestUrl = `${adminApi.defaults.baseURL || ""}/marketing/clients/${client.id}/archive`;
        console.log("--- CRM ARCHIVE DIAGNOSTICS ---");
        console.log("Request URL:", requestUrl);
        console.log("Request Method: POST");
        
        setUpdating(true);
        try {
            await adminApi.post(`/marketing/clients/${client.id}/archive`);
            toast.success("Client record archived.");
            onClientDeleted(client.id);
        } catch (e) {
            console.error("Archive request error:", e);
            toast.error(e?.response?.data?.detail || "Failed to archive client");
        } finally {
            setUpdating(false);
        }
    };

    const handleDelete = async () => {
        if (!client) return;
        if (!window.confirm(`Are you sure you want to delete ${client.name}? This will perform a soft-delete.`)) return;
        
        const requestUrl = `${adminApi.defaults.baseURL || ""}/marketing/clients/${client.id}`;
        console.log("--- CRM DELETE DIAGNOSTICS ---");
        console.log("Request URL:", requestUrl);
        console.log("Request Method: DELETE");
        
        setUpdating(true);
        try {
            await adminApi.delete(`/marketing/clients/${client.id}`);
            toast.success("Client record deleted.");
            onClientDeleted(client.id);
        } catch (e) {
            console.error("Delete request error:", e);
            toast.error(e?.response?.data?.detail || "Failed to delete client");
        } finally {
            setUpdating(false);
        }
    };

    const loadInteractions = useCallback(async (cid) => {
        setLoadingList(true);
        try {
            const { data } = await adminApi.get(`/marketing/interactions/${cid}`);
            const interactionsData = Array.isArray(data) ? data : (data?.items || []);
            setInteractions(interactionsData);
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
            setIsEditing(false);
            return;
        }
        loadInteractions(client.id);

        // Populate edit values
        setEditName(client.name || "");
        setEditCompany(client.company_name || "");
        setEditPhone(client.phone_number || "");
        setEditEmail(client.email || "");
        setEditStage(client.stage || "lead");
        setEditValue(client.value !== undefined && client.value !== null ? String(client.value) : "");
        setEditTags((client.tags || []).join(", "));
        setEditContactType(client.contact_type || "");
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
            toast.success("Touchpoint logged successfully.");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to log interaction");
        } finally {
            setSaving(false);
        }
    };

    const submitUpdate = async (e) => {
        e.preventDefault();
        if (!client) return;
        if (!editName.trim()) {
            toast.error("Name is required");
            return;
        }
        setUpdating(true);
        try {
            const tagsList = editTags.split(",").map(t => t.trim()).filter(Boolean);
            const valNum = editValue.trim() ? parseFloat(editValue) : null;
            const { data } = await adminApi.put(`/marketing/clients/${client.id}`, {
                name: editName.trim(),
                company_name: editCompany.trim() || null,
                phone_number: editPhone.trim() || null,
                email: editEmail.trim() || null,
                stage: editStage,
                value: valNum,
                tags: tagsList,
                contact_type: editContactType || null
            });
            onClientUpdated(data);
            setIsEditing(false);
            toast.success("Client record updated successfully.");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to update client");
        } finally {
            setUpdating(false);
        }
    };

    const handleShare = () => {
        if (!client) return;
        // Construct standard WhatsApp outreach text
        const text = `Hi ${client.name}, hope you are doing well! Just wanted to share our latest premium curation packages. Let me know if anything stands out!`;
        const url = `https://wa.me/${(client.phone_number || "").replace(/[^0-9]/g, "")}?text=${encodeURIComponent(text)}`;
        window.open(url, "_blank");
    };

    const getInteractionIcon = (type) => {
        const found = INTERACTION_TYPES.find(t => t.value === type);
        return found?.icon || MessageSquare;
    };

    const daysSince = getDaysSinceContact(client?.last_contacted_date);
    const health = client ? getRelationshipHealth(client) : null;

    return (
        <Sheet open={open} onOpenChange={(v) => !v && onClose()}>
            <SheetContent
                side="right"
                className="w-full sm:max-w-2xl bg-white border-l border-slate-200 text-slate-900 overflow-y-auto shadow-2xl p-0 [&>button]:hidden"
                data-testid="marketing-client-drawer"
            >
                {client && (
                    <div className="h-full flex flex-col">
                        {/* Drawer Header */}
                        <div className="bg-gradient-to-b from-slate-50/50 to-white border-b border-slate-100 px-5 sm:px-6 py-6 sm:py-8 relative">
                            <SheetHeader className="space-y-2 pr-12">
                                <SheetTitle
                                    className="text-slate-950 text-2xl sm:text-3xl font-light tracking-tight font-display"
                                    data-testid="marketing-drawer-title"
                                >
                                    {client.name}
                                </SheetTitle>
                                <SheetDescription className="text-slate-500 text-sm sm:text-base font-mono flex items-center gap-2 flex-wrap">
                                    <span>{client.company_name || "Independent Account"}</span>
                                    {client.contact_type && (
                                        <>
                                            <span className="text-slate-300">•</span>
                                            <span className="bg-amber-50 text-amber-800 border border-amber-200/50 px-2 py-0.5 rounded-full text-xs font-semibold font-sans tracking-normal uppercase">
                                                {CONTACT_TYPES.find(t => t.value === client.contact_type)?.label || client.contact_type}
                                            </span>
                                        </>
                                    )}
                                </SheetDescription>
                            </SheetHeader>
                            <div className="absolute right-5 top-6 sm:top-8 flex items-center gap-2">
                                <button
                                    onClick={() => setIsEditing(!isEditing)}
                                    className="p-2 text-slate-400 hover:text-slate-800 border border-slate-200 hover:border-slate-300 rounded-xl transition-all shadow-sm bg-white"
                                    title="Edit Profile"
                                >
                                    {isEditing ? <Check className="w-4 h-4 text-[#5A7D5A]" /> : <Edit2 className="w-4 h-4" />}
                                </button>
                                <button
                                    type="button"
                                    onClick={onClose}
                                    data-testid="marketing-drawer-close-btn"
                                    className="p-2 text-slate-400 hover:text-slate-800 border border-slate-200 hover:border-slate-300 rounded-xl transition-all shadow-sm bg-white"
                                    title="Close Drawer"
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            </div>
                        </div>

                        {/* Scrollable Content */}
                        <div className="flex-1 overflow-y-auto px-5 sm:px-6 py-6 space-y-6 sm:space-y-8">
                            
                            {/* Inline Editing Form Toggle */}
                            {isEditing ? (
                                <form onSubmit={submitUpdate} className="bg-slate-50/50 border border-slate-200 rounded-2xl p-5 space-y-4 shadow-sm animate-in fade-in duration-200">
                                    <h4 className="text-xs font-mono font-semibold text-slate-500 uppercase tracking-wider mb-2">Edit Relationship File</h4>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <FieldInput label="Name" value={editName} onChange={setEditName} required />
                                        <FieldInput label="Company" value={editCompany} onChange={setEditCompany} />
                                    </div>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <FieldInput label="Phone" value={editPhone} onChange={setEditPhone} />
                                        <FieldInput label="Email" value={editEmail} onChange={setEditEmail} />
                                    </div>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <label className="block">
                                            <div className="text-[10px] tracking-[0.08em] font-semibold text-slate-500 uppercase font-mono mb-1.5 flex justify-between select-none">
                                                <span>Lifecycle Stage</span>
                                            </div>
                                            <select
                                                value={editStage}
                                                onChange={(e) => setEditStage(e.target.value)}
                                                className="mt-1.5 w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:bg-white focus:border-slate-300 focus:outline-none transition-colors"
                                            >
                                                <option value="lead">New Lead</option>
                                                <option value="active">Active partner</option>
                                                <option value="key_account">Key Account (High Value)</option>
                                            </select>
                                        </label>
                                        <label className="block">
                                            <div className="text-[10px] tracking-[0.08em] font-semibold text-slate-500 uppercase font-mono mb-1.5 flex justify-between select-none">
                                                <span>Contact Type</span>
                                            </div>
                                            <select
                                                value={editContactType}
                                                onChange={(e) => setEditContactType(e.target.value)}
                                                className="mt-1.5 w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:bg-white focus:border-slate-300 focus:outline-none transition-colors"
                                            >
                                                <option value="">Select Type (optional)</option>
                                                <optgroup label="Brand & Marketing">
                                                    <option value="brand_manager">Brand Manager</option>
                                                    <option value="marketing_manager">Marketing Manager</option>
                                                    <option value="influencer_marketing">Influencer Marketing Manager</option>
                                                    <option value="creative_director">Creative Director</option>
                                                    <option value="agency_producer">Agency Producer</option>
                                                </optgroup>
                                                <optgroup label="Casting">
                                                    <option value="casting_director">Casting Director</option>
                                                    <option value="casting_assistant">Casting Assistant</option>
                                                    <option value="casting_company">Casting Company</option>
                                                </optgroup>
                                                <optgroup label="Production">
                                                    <option value="producer">Producer</option>
                                                    <option value="executive_producer">Executive Producer</option>
                                                    <option value="production_house">Production House</option>
                                                    <option value="line_producer">Line Producer</option>
                                                </optgroup>
                                                <optgroup label="Agency">
                                                    <option value="talent_agency">Talent Agency</option>
                                                    <option value="modeling_agency">Modeling Agency</option>
                                                    <option value="casting_agency">Casting Agency</option>
                                                </optgroup>
                                            </select>
                                        </label>
                                    </div>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <FieldInput label="Deal Value (INR)" value={editValue} onChange={setEditValue} />
                                        <FieldInput label="Tags (comma-separated)" value={editTags} onChange={setEditTags} />
                                    </div>
                                    
                                    <div className="flex gap-2.5 justify-end pt-2">
                                        <button
                                            type="button"
                                            onClick={() => setIsEditing(false)}
                                            className="px-4 py-2 text-xs font-semibold text-slate-500 hover:text-slate-800"
                                        >
                                            Cancel
                                        </button>
                                        <button
                                            type="submit"
                                            disabled={updating}
                                            className="inline-flex items-center gap-1.5 bg-slate-950 text-white px-4 py-2 rounded-xl text-xs font-semibold hover:bg-slate-800 disabled:opacity-50 shadow-sm"
                                        >
                                            {updating && <Loader2 className="w-3 animate-spin" />}
                                            Save Changes
                                        </button>
                                    </div>
                                </form>
                            ) : (
                                /* Client Intelligence Summary */
                                <div className="space-y-4">
                                    <div className="flex items-center gap-2">
                                        <Sparkles className="w-4 h-4 text-slate-400" />
                                        <h3 className="text-xs font-mono font-semibold text-slate-400 uppercase tracking-wider">Relationship Scorecard</h3>
                                    </div>
                                    
                                    {/* Action Deck Bar (Prominent Apple/Linear-style Contact actions) */}
                                    {(() => {
                                        const showPhone = !!client.phone_number;
                                        const showEmail = !!client.email;
                                        const showWhatsApp = !!client.phone_number;
                                        
                                        const colsCount = (showPhone ? 1 : 0) + (showEmail ? 1 : 0) + (showWhatsApp ? 1 : 0);
                                        if (colsCount === 0) return null;
                                        
                                        const gridColsClass = colsCount === 3 ? "grid-cols-3" : colsCount === 2 ? "grid-cols-2" : "grid-cols-1";
                                        
                                        return (
                                            <div className={`grid ${gridColsClass} gap-2.5 bg-slate-50 border border-slate-200/60 rounded-2xl p-2.5`}>
                                                {showPhone && (
                                                    <a
                                                        href={`tel:${client.phone_number}`}
                                                        className="flex flex-col items-center justify-center gap-1.5 py-3.5 border border-slate-200/80 bg-white hover:bg-slate-50 rounded-xl text-[11px] font-semibold text-slate-800 shadow-sm transition-all duration-200 active:scale-[0.97]"
                                                    >
                                                        <Phone className="w-4 h-4 text-slate-600" />
                                                        <span>Call</span>
                                                    </a>
                                                )}
                                                {showEmail && (
                                                    <a
                                                        href={`mailto:${client.email}`}
                                                        className="flex flex-col items-center justify-center gap-1.5 py-3.5 border border-slate-200/80 bg-white hover:bg-slate-50 rounded-xl text-[11px] font-semibold text-slate-800 shadow-sm transition-all duration-200 active:scale-[0.97]"
                                                    >
                                                        <Mail className="w-4 h-4 text-slate-600" />
                                                        <span>Email</span>
                                                    </a>
                                                )}
                                                {showWhatsApp && (
                                                    <button
                                                        type="button"
                                                        onClick={handleShare}
                                                        className="flex flex-col items-center justify-center gap-1.5 py-3.5 border border-[#B89B5E]/30 bg-white hover:bg-[#B89B5E]/5 rounded-xl text-[11px] font-semibold text-[#B89B5E] shadow-sm transition-all duration-200 active:scale-[0.97]"
                                                    >
                                                        <MessageSquare className="w-4 h-4 text-[#B89B5E]" />
                                                        <span>WhatsApp</span>
                                                    </button>
                                                )}
                                            </div>
                                        );
                                    })()}

                                    {/* Mini Scorecard row */}
                                    <div className="grid grid-cols-3 gap-3">
                                        <div className="bg-slate-50/50 border border-slate-100 rounded-xl p-3.5 text-center">
                                            <div className="text-[10px] font-semibold font-mono text-slate-400 uppercase mb-1">Status</div>
                                            <div className="flex items-center justify-center">
                                                {health && (
                                                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[9px] font-bold uppercase tracking-wider border ${health.color}`}>
                                                        {health.label}
                                                    </span>
                                                )}
                                            </div>
                                        </div>
                                        <div className="bg-slate-50/50 border border-slate-100 rounded-xl p-3.5 text-center">
                                            <div className="text-[10px] font-semibold font-mono text-slate-400 uppercase mb-1">Logs</div>
                                            <div className="text-xs font-semibold text-slate-800">{client.interaction_count || 0} times</div>
                                        </div>
                                        <div className="bg-slate-50/50 border border-slate-100 rounded-xl p-3.5 text-center">
                                            <div className="text-[10px] font-semibold font-mono text-slate-400 uppercase mb-1">Value</div>
                                            <div className="text-xs font-mono font-bold text-[#B89B5E]">
                                                {client.value ? formatCurrency(client.value) : "—"}
                                            </div>
                                        </div>
                                    </div>

                                    {/* Relationship card details */}
                                    <div className="bg-slate-50/30 border border-slate-200/60 rounded-2xl p-4 space-y-2.5 text-xs">
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1 border-b border-slate-100 pb-2.5">
                                            <span className="text-slate-500 font-medium">Email Address</span>
                                            <span className="font-mono text-slate-800 font-semibold break-all">{client.email || "—"}</span>
                                        </div>
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1 border-b border-slate-100 pb-2.5">
                                            <span className="text-slate-500 font-medium">Phone Number</span>
                                            <span className="font-mono text-slate-800 font-semibold">{client.phone_number || "—"}</span>
                                        </div>
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1 border-b border-slate-100 pb-2.5">
                                            <span className="text-slate-500 font-medium">Tags Registered</span>
                                            <span className="font-mono text-slate-600 font-medium">
                                                {client.tags && client.tags.length > 0 ? client.tags.map(t => `#${t}`).join(" ") : "—"}
                                            </span>
                                        </div>
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                                            <span className="text-slate-500 font-medium">Last Contacted</span>
                                            <span className="text-slate-800 font-semibold">
                                                {formatDateTime(client.last_contacted_date)}
                                                {daysSince !== null && (
                                                    <span className="ml-1.5 text-slate-400 text-[10px] font-mono">({daysSince}d ago)</span>
                                                )}
                                            </span>
                                        </div>
                                    </div>

                                    {/* Relationship Management danger / archive controls */}
                                    <div className="border border-red-100 bg-red-50/20 rounded-2xl p-4 space-y-3">
                                        <div className="flex items-center gap-1.5 text-red-800 font-semibold text-xs select-none">
                                            <AlertCircle className="w-4 h-4 text-red-600" />
                                            <span>Relationship Management</span>
                                        </div>
                                        <p className="text-[11px] text-slate-500 leading-normal select-none">
                                            Manage the visibility of this contact record. Deleting or archiving will remove it from your active workspace.
                                        </p>
                                        <div className="flex gap-2">
                                            <button
                                                type="button"
                                                onClick={handleArchive}
                                                disabled={updating}
                                                data-testid="marketing-drawer-archive-btn"
                                                className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 border border-slate-200 hover:border-slate-300 rounded-xl text-xs font-semibold text-slate-700 bg-white hover:bg-slate-50 transition-colors shadow-sm disabled:opacity-40"
                                            >
                                                Archive Client
                                            </button>
                                            <button
                                                type="button"
                                                onClick={handleDelete}
                                                disabled={updating}
                                                data-testid="marketing-drawer-delete-btn"
                                                className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 border border-red-200 hover:border-red-300 rounded-xl text-xs font-semibold text-red-700 bg-white hover:bg-red-50/50 transition-colors shadow-sm disabled:opacity-40"
                                            >
                                                Delete Record
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* Log interaction form */}
                            <div className="space-y-4">
                                <div className="flex items-center gap-2">
                                    <MessageSquare className="w-4 h-4 text-slate-400" />
                                    <h3 className="text-xs font-mono font-semibold text-slate-400 uppercase tracking-wider">Log Communication</h3>
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
                                                    className={`inline-flex items-center gap-2 px-4 py-2 text-xs rounded-xl border transition-colors duration-150 font-medium ${
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
                                    <div className="relative">
                                        <textarea
                                            value={notes}
                                            onChange={(e) => setNotes(e.target.value)}
                                            placeholder="Input exact touchpoint comments or action items... (Maximum 4000 chars)"
                                            rows={3}
                                            maxLength={4000}
                                            data-testid="marketing-interaction-notes"
                                            className="w-full bg-slate-50/50 border border-slate-200 rounded-2xl px-4 py-3 text-sm text-slate-900 placeholder:text-slate-400 focus:bg-white focus:border-slate-300 focus:ring-1 focus:ring-slate-300 focus:outline-none transition-colors resize-none shadow-inner"
                                        />
                                        <div className="absolute right-3.5 bottom-3 text-[10px] font-mono text-slate-400">
                                            {notes.length}/4000
                                        </div>
                                    </div>
                                    <button
                                        type="submit"
                                        disabled={saving || !notes.trim()}
                                        data-testid="marketing-interaction-submit-btn"
                                        className="inline-flex items-center justify-center gap-2 bg-slate-900 text-white px-5 py-2.5 rounded-xl text-xs font-semibold hover:bg-slate-800 transition-colors disabled:opacity-50 shadow-sm min-w-36"
                                    >
                                        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : "Log Touchpoint"}
                                    </button>
                                </form>
                            </div>

                            {/* Interaction timeline */}
                            <div className="space-y-4 pb-6">
                                <div className="flex items-center gap-2">
                                    <Clock className="w-4 h-4 text-slate-400" />
                                    <h3 className="text-xs font-mono font-semibold text-slate-400 uppercase tracking-wider">
                                        Timeline Records ({interactions.length})
                                    </h3>
                                </div>
                                {loadingList ? (
                                    <div className="py-8 flex justify-center">
                                        <Loader2 className="w-5 h-5 animate-spin text-slate-300" />
                                    </div>
                                ) : interactions.length === 0 ? (
                                    <div
                                        className="text-xs text-slate-400 py-10 text-center border-2 border-dashed border-slate-200 rounded-2xl bg-slate-50/20"
                                        data-testid="marketing-history-empty"
                                    >
                                        <MessageSquare className="w-7 h-7 mx-auto mb-2 text-slate-300" />
                                        No interactions logged under this account.
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
                                                    <div className="absolute left-0 top-1.5 w-4 h-4 rounded-full bg-slate-900 border-2 border-white shadow-sm flex items-center justify-center" />
                                                    
                                                    <div className="bg-white border border-slate-200 rounded-2xl p-4 ml-2 shadow-[0_2px_10px_rgba(15,23,42,0.02)] hover:shadow-md transition-shadow duration-200">
                                                        <div className="flex flex-wrap items-center justify-between gap-2 mb-2 border-b border-slate-50 pb-2">
                                                            <div className="flex items-center gap-1.5">
                                                                <Icon className="w-3.5 h-3.5 text-slate-500" />
                                                                <span className="text-xs font-semibold text-slate-700 capitalize">
                                                                    {it.type}
                                                                </span>
                                                            </div>
                                                            <span className="font-mono text-[9px] text-slate-400">
                                                                {formatDateTime(it.created_at)}
                                                            </span>
                                                        </div>
                                                        {it.notes && (
                                                            <div className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap break-words">
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
