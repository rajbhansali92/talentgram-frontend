import React, { useCallback, useEffect, useState, useMemo, useRef } from "react";
import { adminApi, isAdmin } from "@/lib/api";
import CommTimeline from "@/components/CommTimeline";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
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
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import {
    Command, CommandInput, CommandList, CommandEmpty, CommandGroup, CommandItem,
} from "@/components/ui/command";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
    Plus, Loader2, Phone, Mail, Users as UsersIcon, MessageSquare,
    Calendar, Building2, PhoneCall, Clock, TrendingUp, Users, Activity,
    ChevronRight, Sparkles, Zap, Target, AlertCircle, Edit2, Share2, DollarSign, X, Check, ChevronDown, Settings2
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
// ^ Used only as the initial render fallback for the `contactTypes` state
// below, so the filter pills aren't empty for one frame before the
// GET /marketing/contact-types fetch resolves — the backend seeds these
// exact 15 as defaults on first boot (routers/marketing.py), so this stays
// in sync with real data, not a second source of truth.

// Relationship Status — replaces the old generic "Lifecycle Stage" concept.
// `key_account` is kept (not part of the new 5) because the existing "Key
// Accounts" dashboard stat/filter already depends on it and removing it
// would destroy that working feature and any already-flagged contact's
// status; the stage field itself is untouched (still `clients.stage`),
// only the presented label set changes (2026-09-19).
const RELATIONSHIP_STATUSES = [
    { value: "lead", label: "New Contact" },
    { value: "active", label: "Active Relationship" },
    { value: "project_based", label: "Project-Based" },
    { value: "past_contact", label: "Past Contact" },
    { value: "dormant", label: "Dormant" },
    { value: "key_account", label: "Key Account" },
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
        return { status: "inactive", label: "New Lead", color: "text-[#333333] bg-slate-50 border-[#eaeaea]/60", icon: Zap };
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
    <div className="bg-white border border-[#eaeaea] rounded-2xl p-6 animate-pulse shadow-sm">
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
        <p className="text-[#333333] text-sm mb-4 max-w-xs mx-auto leading-relaxed">{message || "Please check network or backend credentials."}</p>
        <button
            onClick={onRetry}
            className="inline-flex items-center gap-2 px-5 py-2 bg-white border border-[#eaeaea] hover:border-[#d4d4d4] rounded-xl text-sm text-[#111111] hover:text-[#111111] transition-colors shadow-sm"
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
    <div className="border-2 border-dashed border-[#eaeaea] rounded-2xl py-16 sm:py-20 text-center bg-slate-50/30">
        <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-slate-100/50 mb-4 border border-[#eaeaea]/30">
            <Users className="w-6 h-6 text-[#333333]" />
        </div>
        {hasSearch || hasFilters ? (
            <>
                <div className="text-[#111111] text-sm font-medium mb-1">No matching clients found</div>
                <p className="text-[#333333] text-xs max-w-xs mx-auto">Try refining your search, relationship status filter, or contact type.</p>
                <button
                    onClick={onClearFilters}
                    className="mt-4 text-xs font-semibold px-4 py-2 border border-[#eaeaea] bg-white hover:border-[#d4d4d4] rounded-xl text-[#222222] hover:text-[#111111] shadow-sm transition-colors"
                >
                    Clear Search Filters
                </button>
            </>
        ) : (
            <>
                <div className="text-[#111111] text-sm font-medium mb-1">No industry contacts yet</div>
                <p className="text-[#333333] text-xs max-w-xs mx-auto mb-5">Start building your production house, casting, and brand relationships.</p>
                <button
                    onClick={onAddClient}
                    className="inline-flex items-center gap-1.5 bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-medium hover:bg-slate-800 transition-colors shadow-sm"
                >
                    <Plus className="w-3.5 h-3.5" /> Add First Contact
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
        <div className="text-[10px] tracking-[0.08em] font-semibold text-[#333333] uppercase font-mono flex justify-between select-none">
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
            className="mt-1.5 w-full bg-slate-50/40 rounded-xl border border-[#eaeaea]/80 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-2.5 px-4 text-[15px] sm:text-sm text-[#111111] placeholder:text-[#333333] transition-all duration-200 shadow-sm"
        />
    </label>
);

const FieldTextarea = ({ label, value, onChange, placeholder, testId, rows = 2 }) => (
    <label className="block">
        <div className="text-[10px] tracking-[0.08em] font-semibold text-[#333333] uppercase font-mono flex justify-between select-none">
            <span>{label}</span>
        </div>
        <textarea
            value={value || ""}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            rows={rows}
            data-testid={testId}
            className="mt-1.5 w-full bg-slate-50/40 rounded-xl border border-[#eaeaea]/80 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-2.5 px-4 text-[15px] sm:text-sm text-[#111111] placeholder:text-[#333333] transition-all duration-200 shadow-sm resize-none"
        />
    </label>
);

// ============================================================================
// Lookup Picker — searchable select + inline "+ Add" (admin-only), reusing
// the exact Popover/Command pattern ProductionDesk.jsx already uses for its
// CRM-contact picker, so Company and Contact Type get a consistent,
// familiar searchable-select instead of a bespoke dropdown (2026-09-19 CRM
// talent-industry adaptation). `items` is a flat [{key, label, group?}]
// list; `itemKey` is the currently selected key (company id, or contact-type
// slug). Non-admins simply don't see the "+ Add" footer — creation is admin-
// gated server-side too (current_admin on POST /companies, /contact-types).
// ============================================================================
function LookupPicker({ label, items, itemKey, onSelect, onCreate, onManage, isAdminUser, placeholder, entityName, testId, groupLabel }) {
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState("");
    const [creating, setCreating] = useState(false);
    const [newLabel, setNewLabel] = useState("");
    const [saving, setSaving] = useState(false);

    const selected = items.find((i) => i.key === itemKey);

    const grouped = useMemo(() => {
        const groups = new Map();
        for (const item of items) {
            const g = item.group || groupLabel || "";
            if (!groups.has(g)) groups.set(g, []);
            groups.get(g).push(item);
        }
        return Array.from(groups.entries());
    }, [items, groupLabel]);

    const create = async () => {
        if (!newLabel.trim()) return;
        setSaving(true);
        try {
            const created = await onCreate(newLabel.trim());
            onSelect(created.key);
            setOpen(false);
            setCreating(false);
            setNewLabel("");
        } catch (err) {
            toast.error(formatErrorDetail(err, `Failed to add ${entityName}`));
        } finally {
            setSaving(false);
        }
    };

    return (
        <label className="block">
            <div className="text-[10px] tracking-[0.08em] font-semibold text-[#333333] uppercase font-mono mb-1.5 select-none">
                <span>{label}</span>
            </div>
            <Popover open={open} onOpenChange={(v) => { setOpen(v); if (!v) setCreating(false); }}>
                <PopoverTrigger asChild>
                    <button
                        type="button"
                        data-testid={testId}
                        className="w-full flex items-center justify-between bg-slate-50 border border-[#eaeaea] rounded-xl px-4 py-2.5 text-sm text-[#111111] hover:border-[#d4d4d4] transition-colors"
                    >
                        <span className={selected ? "" : "text-[#333333]"}>{selected ? selected.label : (placeholder || `Select ${entityName}...`)}</span>
                        <ChevronDown className="w-3.5 h-3.5 text-[#333333] shrink-0" />
                    </button>
                </PopoverTrigger>
                <PopoverContent className="w-72 p-0" align="start">
                    {!creating ? (
                        <Command shouldFilter={false}>
                            <CommandInput placeholder={`Search ${entityName.toLowerCase()}...`} value={query} onValueChange={setQuery} className="text-xs" />
                            <CommandList>
                                <CommandEmpty className="py-4 text-center text-xs text-black/40">No {entityName.toLowerCase()} found.</CommandEmpty>
                                <CommandItem
                                    value="__none__"
                                    onSelect={() => { onSelect(null); setOpen(false); }}
                                    className="text-xs cursor-pointer text-[#333333] italic"
                                >
                                    None
                                </CommandItem>
                                {grouped.map(([groupName, groupItems]) => {
                                    const filtered = groupItems.filter((i) => !query.trim() || i.label.toLowerCase().includes(query.trim().toLowerCase()));
                                    if (filtered.length === 0) return null;
                                    return (
                                        <CommandGroup key={groupName || "_"} heading={groupName || undefined}>
                                            {filtered.map((item) => (
                                                <CommandItem
                                                    key={item.key}
                                                    value={item.key}
                                                    onSelect={() => { onSelect(item.key); setOpen(false); }}
                                                    className="text-xs cursor-pointer"
                                                >
                                                    {item.label}
                                                </CommandItem>
                                            ))}
                                        </CommandGroup>
                                    );
                                })}
                            </CommandList>
                            {isAdminUser && (onCreate || onManage) && (
                                <div className="border-t border-black/[0.06] p-1.5 flex gap-1">
                                    {onCreate && (
                                        <Button variant="ghost" size="sm" className="flex-1 h-7 text-xs justify-start" onClick={() => { setCreating(true); setNewLabel(query); }}>
                                            <Plus className="h-3 w-3 mr-1.5" /> Add
                                        </Button>
                                    )}
                                    {onManage && (
                                        <Button variant="ghost" size="sm" className="flex-1 h-7 text-xs justify-start" onClick={() => { setOpen(false); onManage(); }}>
                                            <Settings2 className="h-3 w-3 mr-1.5" /> Manage
                                        </Button>
                                    )}
                                </div>
                            )}
                        </Command>
                    ) : (
                        <div className="p-3 space-y-2">
                            <Input placeholder={`New ${entityName.toLowerCase()} name`} value={newLabel} onChange={(e) => setNewLabel(e.target.value)} className="h-8 text-xs" autoFocus />
                            <div className="flex gap-2 justify-end pt-1">
                                <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setCreating(false)}>Cancel</Button>
                                <Button size="sm" className="h-7 text-xs" disabled={!newLabel.trim() || saving} onClick={create}>
                                    {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Save"}
                                </Button>
                            </div>
                        </div>
                    )}
                </PopoverContent>
            </Popover>
        </label>
    );
}

// ============================================================================
// Manage Lookup Dialog — admin-only rename/deactivate/reactivate for the
// Company directory and Contact Type list. Deliberately reuses the existing
// Dialog component (same one AddClientDialog uses) instead of a new settings
// page/route — this is the smallest surface that satisfies "admins can add/
// rename/deactivate/reactivate" without inventing new admin infrastructure.
// ============================================================================
function ManageLookupDialog({ open, onClose, title, items, onRename, onDeactivate, onReactivate }) {
    const [editingKey, setEditingKey] = useState(null);
    const [editValue, setEditValue] = useState("");
    const [busyKey, setBusyKey] = useState(null);

    useEffect(() => {
        if (!open) { setEditingKey(null); setEditValue(""); }
    }, [open]);

    const startEdit = (item) => { setEditingKey(item.id); setEditValue(item.label); };

    const saveEdit = async (item) => {
        if (!editValue.trim()) return;
        setBusyKey(item.id);
        try {
            await onRename(item.id, editValue.trim());
            setEditingKey(null);
        } catch (err) {
            toast.error(formatErrorDetail(err, "Failed to rename"));
        } finally {
            setBusyKey(null);
        }
    };

    const toggleActive = async (item) => {
        setBusyKey(item.id);
        try {
            if (item.active) await onDeactivate(item.id);
            else await onReactivate(item.id);
        } catch (err) {
            toast.error(formatErrorDetail(err, "Failed to update"));
        } finally {
            setBusyKey(null);
        }
    };

    return (
        <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
            <DialogContent className="bg-white border-[#eaeaea] text-[#111111] sm:max-w-md rounded-2xl shadow-xl" data-testid="marketing-manage-lookup-dialog">
                <DialogHeader>
                    <DialogTitle className="text-lg font-display text-slate-950">{title}</DialogTitle>
                    <DialogDescription className="text-[#333333] text-xs">
                        Rename or deactivate entries used across all industry contacts.
                    </DialogDescription>
                </DialogHeader>
                <div className="max-h-96 overflow-y-auto space-y-2 py-2">
                    {items.length === 0 && <p className="text-xs text-[#333333] italic py-4 text-center">Nothing here yet.</p>}
                    {items.map((item) => (
                        <div key={item.id} data-testid={`marketing-manage-lookup-row-${item.id}`} className={`flex items-center gap-2 border border-[#eaeaea] rounded-xl px-3 py-2 ${!item.active ? "opacity-50" : ""}`}>
                            {editingKey === item.id ? (
                                <>
                                    <input
                                        value={editValue}
                                        onChange={(e) => setEditValue(e.target.value)}
                                        className="flex-1 text-xs border border-[#eaeaea] rounded-lg px-2 py-1 outline-none focus:border-[#d4d4d4]"
                                        autoFocus
                                    />
                                    <button type="button" onClick={() => saveEdit(item)} disabled={busyKey === item.id} className="text-xs font-semibold text-[#5A7D5A]">Save</button>
                                    <button type="button" onClick={() => setEditingKey(null)} className="text-xs text-[#333333]">Cancel</button>
                                </>
                            ) : (
                                <>
                                    <span className="flex-1 text-xs text-[#111111] truncate">{item.label}</span>
                                    <button type="button" onClick={() => startEdit(item)} className="text-[#333333] hover:text-[#111111]"><Edit2 className="w-3.5 h-3.5" /></button>
                                    <button
                                        type="button"
                                        onClick={() => toggleActive(item)}
                                        disabled={busyKey === item.id}
                                        className={`text-[10px] font-semibold px-2 py-1 rounded-lg border ${item.active ? "text-red-700 border-red-200 hover:bg-red-50" : "text-[#5A7D5A] border-[#5A7D5A]/30 hover:bg-[#5A7D5A]/5"}`}
                                    >
                                        {item.active ? "Deactivate" : "Reactivate"}
                                    </button>
                                </>
                            )}
                        </div>
                    ))}
                </div>
                <DialogFooter>
                    <button type="button" onClick={onClose} className="px-4 py-2 text-xs font-semibold text-[#333333] hover:text-[#111111]">Close</button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

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

    // Bulk / Multi-Select States
    const [selectedIds, setSelectedIds] = useState(new Set());
    const [isSelectionMode, setIsSelectionMode] = useState(false);
    const [bulkArchiveOpen, setBulkArchiveOpen] = useState(false);
    const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
    const [bulkDeleteConfirmInput, setBulkDeleteConfirmInput] = useState("");
    const [bulkTagOpen, setBulkTagOpen] = useState(false);
    const [selectedBulkTags, setSelectedBulkTags] = useState([]);
    const [newBulkTagInput, setNewBulkTagInput] = useState("");
    const [updating, setUpdating] = useState(false);

    // Company directory + Contact Type lookup lists (2026-09-19 CRM
    // talent-industry adaptation) — admin-managed via ManageLookupDialog,
    // selected via LookupPicker. Initial fallback keeps the Contact Type
    // filter pills populated for one frame before the fetch resolves.
    const [companies, setCompanies] = useState([]);
    const [contactTypes, setContactTypes] = useState(CONTACT_TYPES.map((t) => ({ ...t, active: true })));
    const [manageCompaniesOpen, setManageCompaniesOpen] = useState(false);
    const [manageTypesOpen, setManageTypesOpen] = useState(false);
    // Separate from `companies`/`contactTypes` above (those stay
    // active-only, feeding LookupPicker + the filter pills) — the Manage
    // dialog needs inactive entries too, so it gets its own state rather
    // than leaking deactivated items into every picker.
    const [managedCompanies, setManagedCompanies] = useState([]);
    const [managedContactTypes, setManagedContactTypes] = useState([]);
    const isAdminUser = isAdmin();

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

    const loadCompanies = useCallback(async (includeInactive = false) => {
        try {
            const { data } = await adminApi.get("/marketing/companies", { params: includeInactive ? { include_inactive: true } : {} });
            return Array.isArray(data) ? data : [];
        } catch (e) {
            toast.error(formatErrorDetail(e, "Failed to load companies"));
            return [];
        }
    }, []);

    const loadContactTypes = useCallback(async (includeInactive = false) => {
        try {
            const { data } = await adminApi.get("/marketing/contact-types", { params: includeInactive ? { include_inactive: true } : {} });
            return Array.isArray(data) ? data : [];
        } catch (e) {
            toast.error(formatErrorDetail(e, "Failed to load contact types"));
            return [];
        }
    }, []);

    useEffect(() => {
        load();
        loadCompanies().then((data) => { if (data.length) setCompanies(data); });
        loadContactTypes().then((data) => { if (data.length) setContactTypes(data); });
    }, [load, loadCompanies, loadContactTypes]);

    const handleCompanyCreate = useCallback(async (name) => {
        const { data } = await adminApi.post("/marketing/companies", { name });
        setCompanies((prev) => (prev.some((c) => c.id === data.id) ? prev : [...prev, data].sort((a, b) => a.name.localeCompare(b.name))));
        return { key: data.id, label: data.name };
    }, []);

    const handleContactTypeCreate = useCallback(async (label) => {
        const { data } = await adminApi.post("/marketing/contact-types", { label });
        setContactTypes((prev) => (prev.some((t) => t.value === data.value) ? prev : [...prev, data]));
        return { key: data.value, label: data.label };
    }, []);

    // Refresh BOTH the active-only picker list and the manage dialog's
    // full (incl. inactive) list — a rename/deactivate/reactivate must be
    // reflected in each independently.
    const refreshCompanies = useCallback(async () => {
        const [activeOnly, all] = await Promise.all([loadCompanies(false), loadCompanies(true)]);
        setCompanies(activeOnly);
        setManagedCompanies(all);
    }, [loadCompanies]);

    const refreshContactTypes = useCallback(async () => {
        const [activeOnly, all] = await Promise.all([loadContactTypes(false), loadContactTypes(true)]);
        setContactTypes(activeOnly);
        setManagedContactTypes(all);
    }, [loadContactTypes]);

    const openManageCompanies = () => { refreshCompanies(); setManageCompaniesOpen(true); };
    const openManageContactTypes = () => { refreshContactTypes(); setManageTypesOpen(true); };

    const renameCompany = async (id, name) => { await adminApi.put(`/marketing/companies/${id}`, { name }); await refreshCompanies(); };
    const deactivateCompany = async (id) => { await adminApi.post(`/marketing/companies/${id}/deactivate`); await refreshCompanies(); };
    const reactivateCompany = async (id) => { await adminApi.post(`/marketing/companies/${id}/reactivate`); await refreshCompanies(); };

    const renameContactType = async (id, label) => { await adminApi.put(`/marketing/contact-types/${id}`, { label }); await refreshContactTypes(); };
    const deactivateContactType = async (id) => { await adminApi.post(`/marketing/contact-types/${id}/deactivate`); await refreshContactTypes(); };
    const reactivateContactType = async (id) => { await adminApi.post(`/marketing/contact-types/${id}/reactivate`); await refreshContactTypes(); };

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
        contactTypes.forEach(t => {
            counts[t.value] = 0;
        });
        clients.forEach(c => {
            if (c.contact_type && counts[c.contact_type] !== undefined) {
                counts[c.contact_type]++;
            }
        });
        return counts;
    }, [clients, contactTypes]);

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

    // Bulk / Multi-Select Handlers and Helpers (Moved below filteredClients to avoid temporal dead zone crashes)
    const toggleSelect = useCallback((id) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) {
                next.delete(id);
            } else {
                next.add(id);
            }
            return next;
        });
    }, []);

    const clearSelection = useCallback(() => {
        setSelectedIds(new Set());
        setIsSelectionMode(false);
    }, []);

    const selectAllVisible = useCallback(() => {
        const visibleIds = filteredClients.map(c => c.id);
        const allSelected = visibleIds.every(id => selectedIds.has(id));
        
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (allSelected) {
                visibleIds.forEach(id => next.delete(id));
            } else {
                visibleIds.forEach(id => next.add(id));
            }
            return next;
        });
    }, [filteredClients, selectedIds]);

    const isAllVisibleSelected = useMemo(() => {
        if (filteredClients.length === 0) return false;
        return filteredClients.every(c => selectedIds.has(c.id));
    }, [filteredClients, selectedIds]);

    const isAnyVisibleSelected = useMemo(() => {
        return filteredClients.some(c => selectedIds.has(c.id));
    }, [filteredClients, selectedIds]);

    const handleBulkArchive = async () => {
        const idsArray = Array.from(selectedIds);
        setUpdating(true);
        try {
            await adminApi.post("/marketing/clients/bulk-archive", { ids: idsArray });
            toast.success(`${idsArray.length} contact(s) archived.`);
            setClients(prev => prev.filter(c => !selectedIds.has(c.id)));
            clearSelection();
            setBulkArchiveOpen(false);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to archive contacts");
        } finally {
            setUpdating(false);
        }
    };

    const handleBulkDelete = async () => {
        if (bulkDeleteConfirmInput !== "DELETE") return;
        const idsArray = Array.from(selectedIds);
        setUpdating(true);
        try {
            await adminApi.post("/marketing/clients/bulk-delete", { ids: idsArray });
            toast.success(`${idsArray.length} contact(s) deleted.`);
            setClients(prev => prev.filter(c => !selectedIds.has(c.id)));
            clearSelection();
            setBulkDeleteOpen(false);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to delete contacts");
        } finally {
            setUpdating(false);
        }
    };

    const allExistingTags = useMemo(() => {
        const set = new Set();
        clients.forEach(c => {
            (c.tags || []).forEach(t => set.add(t));
        });
        const defaults = ["Brand Manager", "Agency", "Producer", "Casting Director", "Creative Director", "Influencer Manager"];
        defaults.forEach(t => set.add(t));
        return Array.from(set).sort();
    }, [clients]);

    const handleBulkTag = async () => {
        const idsArray = Array.from(selectedIds);
        const tagsToApply = [...selectedBulkTags];
        const newTag = newBulkTagInput.trim();
        if (newTag) {
            tagsToApply.push(newTag);
        }
        if (tagsToApply.length === 0) {
            toast.error("Please select or enter at least one tag.");
            return;
        }
        setUpdating(true);
        try {
            await adminApi.post("/marketing/clients/bulk-tag", { ids: idsArray, tags: tagsToApply });
            toast.success(`Tags assigned to ${idsArray.length} contact(s).`);
            setClients(prev => prev.map(c => {
                if (selectedIds.has(c.id)) {
                    const merged = Array.from(new Set([...(c.tags || []), ...tagsToApply]));
                    return { ...c, tags: merged };
                }
                return c;
            }));
            clearSelection();
            setBulkTagOpen(false);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to assign tags");
        } finally {
            setUpdating(false);
        }
    };

    useEffect(() => {
        if (!bulkDeleteOpen) {
            setBulkDeleteConfirmInput("");
        }
    }, [bulkDeleteOpen]);

    useEffect(() => {
        if (!bulkTagOpen) {
            setSelectedBulkTags([]);
            setNewBulkTagInput("");
        }
    }, [bulkTagOpen]);

    // Touch event variables for mobile long press
    const touchTimeoutRef = useRef({});
    const touchStartCoordsRef = useRef({});

    const handleTouchStart = (e, id) => {
        const touch = e.touches[0];
        touchStartCoordsRef.current[id] = { x: touch.clientX, y: touch.clientY };
        
        if (touchTimeoutRef.current[id]) {
            clearTimeout(touchTimeoutRef.current[id]);
        }
        
        touchTimeoutRef.current[id] = setTimeout(() => {
            setIsSelectionMode(true);
            toggleSelect(id);
            if (navigator.vibrate) {
                navigator.vibrate(50);
            }
        }, 500);
    };

    const handleTouchMove = (e, id) => {
        const touch = e.touches[0];
        const start = touchStartCoordsRef.current[id];
        if (start) {
            const diffX = Math.abs(touch.clientX - start.x);
            const diffY = Math.abs(touch.clientY - start.y);
            if (diffX > 10 || diffY > 10) {
                if (touchTimeoutRef.current[id]) {
                    clearTimeout(touchTimeoutRef.current[id]);
                    touchTimeoutRef.current[id] = null;
                }
            }
        }
    };

    const handleTouchEnd = (id) => {
        if (touchTimeoutRef.current[id]) {
            clearTimeout(touchTimeoutRef.current[id]);
            touchTimeoutRef.current[id] = null;
        }
    };

    const handleCardClick = (e, client, index) => {
        if (e.target.closest("input[type=checkbox]")) {
            return;
        }
        if (selectedIds.size > 0 || isSelectionMode) {
            e.preventDefault();
            e.stopPropagation();
            toggleSelect(client.id);
        } else {
            setActiveClient(client);
            setFocusedIndex(index);
        }
    };

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
                        <h1 className="text-3xl sm:text-4xl font-light tracking-tight text-[#111111] font-display">
                            Talentgram Relationships
                        </h1>
                        <p className="text-sm text-[#333333] mt-2 font-mono tracking-tight flex items-center gap-1.5">
                            <Activity className="w-3.5 h-3.5 text-[#333333]" />
                            Industry Contacts — production houses, casting, brands
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => setAddOpen(true)}
                        data-testid="marketing-add-client-btn"
                        className="shrink-0 inline-flex items-center gap-2 bg-slate-900 text-white px-5 py-3 rounded-2xl text-xs font-semibold hover:bg-slate-800 transition-all hover:shadow-md hover:scale-[1.01] active:scale-[0.98] whitespace-nowrap"
                    >
                        <Plus className="w-4 h-4" /> Add Industry Contact
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
                                className={`cursor-pointer bg-white border ${active ? st.activeBg : `border-[#eaeaea] ${st.color}`} rounded-2xl p-3.5 sm:p-5 transition-all duration-200 hover:shadow-sm`}
                            >
                                <div className="text-xl sm:text-2xl font-bold text-slate-950 mb-0.5">{st.count}</div>
                                <div className="text-[11px] font-semibold text-[#111111] tracking-tight">{st.label}</div>
                                <div className="text-[9.5px] text-[#333333] font-medium mt-0.5 leading-snug">{st.desc}</div>
                            </div>
                        );
                    })}
                </div>

                {/* Intelligent Search Input */}
                <div className="space-y-4 border-b border-slate-100 pb-6 sm:pb-7">
                    <div className="flex flex-col lg:flex-row lg:items-center gap-4 justify-between">
                        <div className="relative flex-1 max-w-full lg:max-w-lg">
                            <div className="absolute left-4 top-1/2 -translate-y-1/2">
                                <svg className="w-4 h-4 text-[#333333]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                                </svg>
                            </div>
                            <input
                                ref={searchInputRef}
                                type="text"
                                placeholder="Search by name, company, phone, or tags... (CMD+K / /)"
                                value={searchQuery}
                                onChange={(e) => { setSearchQuery(e.target.value); setFocusedIndex(-1); }}
                                className="w-full pl-11 pr-10 py-3.5 bg-slate-50/50 border border-[#eaeaea] rounded-2xl text-sm text-[#111111] placeholder:text-[#333333] focus:bg-white focus:border-slate-400 focus:ring-1 focus:ring-slate-400 focus:outline-none transition-all shadow-inner font-sans"
                            />
                            {searchQuery && (
                                <button
                                    onClick={() => { setSearchQuery(""); searchInputRef.current?.focus(); }}
                                    className="absolute right-3.5 top-1/2 -translate-y-1/2 text-[#333333] hover:text-[#222222] transition-colors"
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            )}
                        </div>
                        <div className="flex items-center gap-1.5 text-xs text-[#333333] font-mono">
                            <span className="bg-slate-50 border border-[#eaeaea] px-2 py-1 rounded">⌘K</span>
                            <span>to focus search</span>
                        </div>
                    </div>

                    {/* Local searches pills */}
                    {recentSearches.length > 0 && (
                        <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[10px] font-semibold text-[#333333] uppercase tracking-wider font-mono">Recent:</span>
                            {recentSearches.map((s, idx) => (
                                <button
                                    key={`${s}-${idx}`}
                                    onClick={() => { setSearchQuery(s); setFocusedIndex(-1); }}
                                    className="inline-flex items-center gap-1 px-3 py-1 bg-slate-50 hover:bg-slate-100 border border-[#eaeaea] text-xs text-[#222222] hover:text-[#111111] rounded-full transition-colors font-medium shadow-sm"
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
                        <span className="text-[10px] font-semibold text-[#333333] uppercase tracking-wider font-mono shrink-0 select-none">Type:</span>
                        <button
                            type="button"
                            onClick={() => { setSelectedContactType("all"); setFocusedIndex(-1); }}
                            className={`inline-flex items-center gap-1.5 px-3 py-1 border rounded-full text-xs font-semibold whitespace-nowrap transition-all duration-200 ${
                                selectedContactType === "all"
                                    ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                    : "bg-white text-[#222222] border-[#eaeaea] hover:border-[#d4d4d4]"
                            }`}
                        >
                            All Types
                        </button>
                        {contactTypes.map((t) => {
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
                                            : "bg-white text-[#222222] border-[#eaeaea] hover:border-[#d4d4d4]"
                                    }`}
                                >
                                    <span>{t.label}</span>
                                    <span className={`text-[9px] px-1.5 py-0.5 rounded-full font-mono font-bold ${active ? "bg-white/20 text-white" : "bg-slate-100 text-[#333333]"}`}>
                                        {count}
                                    </span>
                                </button>
                            );
                        })}
                        {isAdminUser && (
                            <button
                                type="button"
                                onClick={openManageContactTypes}
                                data-testid="marketing-manage-contact-types-btn"
                                className="inline-flex items-center gap-1 px-2.5 py-1 border border-dashed border-[#d4d4d4] rounded-full text-[10px] font-semibold text-[#333333] hover:text-[#111111] hover:border-[#333333] transition-colors shrink-0 whitespace-nowrap"
                                title="Manage Contact Types"
                            >
                                <Settings2 className="w-3 h-3" /> Manage
                            </button>
                        )}
                    </div>
                </div>
            </div>

            {/* Bulk Actions Controls Deck & Action Bar */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 py-3.5 px-4 mb-6 bg-slate-50 border border-[#eaeaea]/60 rounded-2xl">
                <div className="flex items-center gap-3">
                    <label className="flex items-center gap-2 cursor-pointer select-none">
                        <input
                            type="checkbox"
                            checked={isAllVisibleSelected}
                            ref={el => {
                                if (el) {
                                    el.indeterminate = isAnyVisibleSelected && !isAllVisibleSelected;
                                }
                            }}
                            onChange={selectAllVisible}
                            className="w-4.5 h-4.5 rounded border-[#d4d4d4] text-[#111111] focus:ring-slate-900 cursor-pointer"
                        />
                        <span className="text-xs font-semibold text-[#111111]">Select All Visible</span>
                    </label>
                    {selectedIds.size > 0 && (
                        <span className="text-xs font-mono font-bold text-[#333333] bg-slate-200/60 px-2 py-0.5 rounded-lg select-none">
                            {selectedIds.size} Selected
                        </span>
                    )}
                </div>

                {/* Desktop Bulk Actions */}
                {selectedIds.size > 0 && (
                    <div className="hidden md:flex items-center gap-2">
                        <button
                            type="button"
                            onClick={() => setBulkTagOpen(true)}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-white border border-[#eaeaea] hover:border-slate-355 rounded-xl text-xs font-semibold text-[#111111] transition-colors shadow-sm active:scale-[0.98]"
                        >
                            Assign Tags
                        </button>
                        <button
                            type="button"
                            onClick={() => setBulkArchiveOpen(true)}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-white border border-[#eaeaea] hover:border-slate-355 rounded-xl text-xs font-semibold text-[#111111] transition-colors shadow-sm active:scale-[0.98]"
                        >
                            Archive Selected
                        </button>
                        <button
                            type="button"
                            onClick={() => setBulkDeleteOpen(true)}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-red-50 hover:bg-red-100 border border-red-200 hover:border-red-300 rounded-xl text-xs font-semibold text-red-700 transition-colors shadow-sm active:scale-[0.98]"
                        >
                            Delete Selected
                        </button>
                        <button
                            type="button"
                            onClick={clearSelection}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-[#333333] hover:text-[#111111] transition-colors"
                        >
                            Clear Selection
                        </button>
                    </div>
                )}
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
                                onClick={(e) => handleCardClick(e, c, i)}
                                onTouchStart={(e) => handleTouchStart(e, c.id)}
                                onTouchMove={(e) => handleTouchMove(e, c.id)}
                                onTouchEnd={() => handleTouchEnd(c.id)}
                                data-testid={`marketing-client-row-${c.id}`}
                                className={`group bg-white border rounded-2xl p-4 sm:p-5 cursor-pointer transition-all duration-200 hover:shadow-md select-none ${
                                    selectedIds.has(c.id)
                                        ? "border-slate-900 bg-slate-50/40 shadow-sm"
                                        : isFocused 
                                            ? "border-slate-900 ring-1 ring-slate-950 bg-slate-50/20" 
                                            : "border-[#eaeaea] hover:border-[#d4d4d4]"
                                }`}
                            >
                                <div className="flex items-start gap-3 sm:gap-5">
                                    {/* Hover checkbox / Mobile check indicator */}
                                    <div className={`shrink-0 flex items-center justify-center mr-1 sm:mr-2 self-center transition-all ${
                                        selectedIds.has(c.id) 
                                            ? "opacity-100 scale-100" 
                                            : "opacity-0 md:group-hover:opacity-100 scale-90 md:scale-100"
                                    }`}>
                                        <input
                                            type="checkbox"
                                            checked={selectedIds.has(c.id)}
                                            onChange={() => toggleSelect(c.id)}
                                            className="w-4.5 h-4.5 rounded border-[#d4d4d4] text-[#111111] focus:ring-slate-950 cursor-pointer transition-all shadow-sm"
                                        />
                                    </div>

                                    {/* Glassmorphic Initial Avatar */}
                                    <div className="w-10 h-10 sm:w-12 sm:h-12 rounded-xl sm:rounded-2xl bg-slate-50 border border-[#eaeaea] flex items-center justify-center font-display text-base sm:text-lg font-medium text-[#111111] shrink-0 shadow-sm">
                                        {initial}
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2.5 mb-1.5 flex-wrap">
                                            <h3 className="text-base sm:text-lg leading-tight font-semibold text-[#111111]">
                                                {c.name}
                                            </h3>
                                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-[9px] font-bold uppercase tracking-wider border ${health.color}`}>
                                                <HealthIcon className="w-2.5 h-2.5" />
                                                {health.label}
                                            </span>
                                            {c.designation && (
                                                <span className="inline-flex items-center gap-0.5 px-2 py-0.5 bg-[#B89B5E]/6 border border-[#B89B5E]/15 rounded-lg text-[9px] font-mono font-bold text-[#B89B5E] uppercase tracking-wider">
                                                    {c.designation}
                                                </span>
                                            )}
                                        </div>

                                        <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-xs mb-2.5 text-[#222222]">
                                            {c.company_name && (
                                                <div className="flex items-center gap-1">
                                                    <Building2 className="w-3.5 h-3.5 text-[#333333]" />
                                                    <span>{c.company_name}</span>
                                                </div>
                                            )}
                                            {c.email && (
                                                <div className="flex items-center gap-1 font-mono">
                                                    <Mail className="w-3.5 h-3.5 text-[#333333]" />
                                                    <span className="break-all">{c.email}</span>
                                                </div>
                                            )}
                                            {c.phone_number && (
                                                <div className="flex items-center gap-1 font-mono">
                                                    <PhoneCall className="w-3.5 h-3.5 text-[#333333]" />
                                                    <span>{c.phone_number}</span>
                                                </div>
                                            )}
                                        </div>

                                        {/* Tags rendering */}
                                        {c.tags && c.tags.length > 0 && (
                                            <div className="flex gap-1.5 flex-wrap mb-3 font-mono">
                                                {c.tags.map(t => (
                                                    <span key={t} className="inline-block px-2 py-0.5 bg-slate-50 border border-[#eaeaea]/80 rounded-md text-[10px] text-[#333333]">
                                                        #{t}
                                                    </span>
                                                ))}
                                            </div>
                                        )}

                                        <div className="flex flex-wrap gap-x-3.5 gap-y-1 text-[10px] text-[#333333] font-mono">
                                            <div className="flex items-center gap-1">
                                                <Calendar className="w-3.5 h-3.5 text-[#333333]" />
                                                <span>Contact: {formatDate(c.last_contacted_date)}</span>
                                            </div>
                                            <div className="flex items-center gap-1">
                                                <TrendingUp className="w-3.5 h-3.5 text-[#333333]" />
                                                <span>{momentum}</span>
                                            </div>
                                            {c.interaction_count > 0 && (
                                                <div className="flex items-center gap-1">
                                                    <MessageSquare className="w-3.5 h-3.5 text-[#333333]" />
                                                    <span>{c.interaction_count} log{c.interaction_count !== 1 ? 's' : ''}</span>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                    <div className="shrink-0 flex items-center h-10 sm:h-12">
                                        <ChevronRight className="w-4 h-4 text-slate-300 group-hover:text-[#333333] transition-colors" />
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
                companies={companies}
                contactTypes={contactTypes}
                onCompanyCreate={handleCompanyCreate}
                onContactTypeCreate={handleContactTypeCreate}
                onManageCompanies={openManageCompanies}
                onManageContactTypes={openManageContactTypes}
                isAdminUser={isAdminUser}
            />

            <ManageLookupDialog
                open={manageCompaniesOpen}
                onClose={() => setManageCompaniesOpen(false)}
                title="Manage Production Houses / Companies"
                items={managedCompanies.map((c) => ({ id: c.id, label: c.name, active: c.active }))}
                onRename={renameCompany}
                onDeactivate={deactivateCompany}
                onReactivate={reactivateCompany}
            />
            <ManageLookupDialog
                open={manageTypesOpen}
                onClose={() => setManageTypesOpen(false)}
                title="Manage Contact Types"
                items={managedContactTypes.map((t) => ({ id: t.id, label: t.label, active: t.active }))}
                onRename={renameContactType}
                onDeactivate={deactivateContactType}
                onReactivate={reactivateContactType}
            />

            {/* Mobile Sticky Bottom Action Bar */}
            {selectedIds.size > 0 && (
                <div className="md:hidden fixed bottom-0 left-0 right-0 z-50 bg-white border-t border-[#eaeaea] shadow-[0_-8px_30px_rgb(0,0,0,0.12)] p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] animate-in slide-in-from-bottom duration-300">
                    <div className="flex items-center justify-between gap-4 mb-3">
                        <span className="text-xs font-semibold text-[#111111]">
                            {selectedIds.size} Contact(s) Selected
                        </span>
                        <button
                            type="button"
                            onClick={clearSelection}
                            className="text-xs font-semibold text-[#333333] hover:text-[#111111]"
                        >
                            Clear
                        </button>
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                        <button
                            type="button"
                            onClick={() => setBulkTagOpen(true)}
                            className="inline-flex items-center justify-center py-2.5 bg-slate-900 text-white rounded-xl text-xs font-bold transition-all active:scale-[0.98]"
                        >
                            Assign Tags
                        </button>
                        <button
                            type="button"
                            onClick={() => setBulkArchiveOpen(true)}
                            className="inline-flex items-center justify-center py-2.5 bg-slate-50 border border-[#eaeaea] rounded-xl text-xs font-semibold text-[#111111] transition-all active:scale-[0.98]"
                        >
                            Archive
                        </button>
                        <button
                            type="button"
                            onClick={() => setBulkDeleteOpen(true)}
                            className="inline-flex items-center justify-center py-2.5 bg-red-50 border border-red-200 rounded-xl text-xs font-semibold text-red-700 transition-all active:scale-[0.98]"
                        >
                            Delete
                        </button>
                    </div>
                </div>
            )}

            {/* Bulk Archive Dialog */}
            <Dialog open={bulkArchiveOpen} onOpenChange={setBulkArchiveOpen}>
                <DialogContent className="bg-white border-[#eaeaea] text-[#111111] sm:max-w-md rounded-2xl shadow-xl p-6">
                    <DialogHeader>
                        <DialogTitle className="text-xl font-semibold text-slate-950">
                            Archive Contacts
                        </DialogTitle>
                        <DialogDescription className="text-[#333333] text-xs mt-1">
                            Are you sure you want to archive {selectedIds.size} selected contact(s)?
                            Archiving will remove them from the active list but preserve their interaction logs.
                        </DialogDescription>
                    </DialogHeader>
                    <DialogFooter className="mt-6 gap-3">
                        <button
                            type="button"
                            onClick={() => setBulkArchiveOpen(false)}
                            className="px-4 py-2.5 text-sm font-semibold text-[#222222] hover:text-[#111111]"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={handleBulkArchive}
                            disabled={updating}
                            className="inline-flex items-center gap-1.5 bg-slate-950 text-white px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-slate-800 disabled:opacity-50 shadow-sm"
                        >
                            {updating && <Loader2 className="w-3 animate-spin" />}
                            Archive {selectedIds.size} Contacts
                        </button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* Bulk Delete Dialog */}
            <Dialog open={bulkDeleteOpen} onOpenChange={setBulkDeleteOpen}>
                <DialogContent className="bg-white border-[#eaeaea] text-[#111111] sm:max-w-md rounded-2xl shadow-xl p-6">
                    <DialogHeader>
                        <DialogTitle className="text-xl font-semibold text-red-700">
                            Delete Contacts
                        </DialogTitle>
                        <DialogDescription className="text-[#333333] text-xs mt-1">
                            Are you sure you want to delete {selectedIds.size} selected contact(s)?
                            This action cannot be undone. Type <strong className="text-[#111111]">DELETE</strong> to proceed.
                        </DialogDescription>
                    </DialogHeader>
                    
                    <div className="mt-4">
                        <input
                            type="text"
                            value={bulkDeleteConfirmInput}
                            onChange={(e) => setBulkDeleteConfirmInput(e.target.value)}
                            placeholder="Type DELETE to confirm"
                            className="w-full px-4 py-2.5 bg-slate-50 border border-[#eaeaea] rounded-xl text-sm placeholder:text-[#333333] focus:bg-white focus:border-red-300 focus:outline-none transition-colors"
                        />
                    </div>

                    <DialogFooter className="mt-6 gap-3">
                        <button
                            type="button"
                            onClick={() => setBulkDeleteOpen(false)}
                            className="px-4 py-2.5 text-sm font-semibold text-[#222222] hover:text-[#111111]"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={handleBulkDelete}
                            disabled={updating || bulkDeleteConfirmInput !== "DELETE"}
                            className="inline-flex items-center gap-1.5 bg-red-600 hover:bg-red-700 text-white px-4 py-2.5 rounded-xl text-xs font-semibold disabled:opacity-50 shadow-sm"
                        >
                            {updating && <Loader2 className="w-3 animate-spin" />}
                            Delete {selectedIds.size} Contacts
                        </button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* Bulk Tag Dialog */}
            <Dialog open={bulkTagOpen} onOpenChange={setBulkTagOpen}>
                <DialogContent className="bg-white border-[#eaeaea] text-[#111111] sm:max-w-md rounded-2xl shadow-xl p-6">
                    <DialogHeader>
                        <DialogTitle className="text-xl font-semibold text-slate-950">
                            Assign Tags
                        </DialogTitle>
                        <DialogDescription className="text-[#333333] text-xs mt-1">
                            Assign or create tags for {selectedIds.size} selected contact(s).
                            New tags will be merged with their existing tags.
                        </DialogDescription>
                    </DialogHeader>

                    {/* Pre-existing tags multi select */}
                    <div className="mt-4 space-y-3">
                        <div className="text-[10px] font-semibold tracking-wider font-mono text-[#333333] uppercase">
                            Select Tags to Merge:
                        </div>
                        <div className="flex flex-wrap gap-2 max-h-36 overflow-y-auto pr-1">
                            {allExistingTags.map(tag => {
                                const active = selectedBulkTags.includes(tag);
                                return (
                                    <button
                                        key={tag}
                                        type="button"
                                        onClick={() => {
                                            setSelectedBulkTags(prev => 
                                                prev.includes(tag) 
                                                    ? prev.filter(t => t !== tag) 
                                                    : [...prev, tag]
                                            );
                                        }}
                                        className={`px-3 py-1.5 border rounded-xl text-xs font-medium transition-all ${
                                            active
                                                ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                : "bg-white text-[#222222] border-[#eaeaea] hover:border-slate-355"
                                        }`}
                                    >
                                        #{tag}
                                    </button>
                                );
                            })}
                        </div>

                        {/* Inline new tag creator */}
                        <div className="pt-2 border-t border-slate-100">
                            <label className="block">
                                <span className="text-[10px] font-semibold tracking-wider font-mono text-[#333333] uppercase">
                                    Create New Tag:
                                </span>
                                <input
                                    type="text"
                                    value={newBulkTagInput}
                                    onChange={(e) => setNewBulkTagInput(e.target.value)}
                                    placeholder="Enter new tag..."
                                    className="mt-1.5 w-full px-4 py-2.5 bg-slate-50 border border-[#eaeaea] rounded-xl text-sm placeholder:text-[#333333] focus:bg-white focus:border-slate-350 focus:outline-none transition-colors"
                                />
                            </label>
                        </div>
                    </div>

                    <DialogFooter className="mt-6 gap-3">
                        <button
                            type="button"
                            onClick={() => setBulkTagOpen(false)}
                            className="px-4 py-2.5 text-sm font-semibold text-[#222222] hover:text-[#111111]"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={handleBulkTag}
                            disabled={updating || (selectedBulkTags.length === 0 && !newBulkTagInput.trim())}
                            className="inline-flex items-center gap-1.5 bg-slate-950 text-white px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-slate-800 disabled:opacity-50 shadow-sm"
                        >
                            {updating && <Loader2 className="w-3 animate-spin" />}
                            Apply Tags
                        </button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <ClientDrawer
                client={activeClient}
                onClose={() => setActiveClient(null)}
                onClientUpdated={handleClientUpdated}
                onClientDeleted={handleClientDeleted}
                onInteractionAdded={handleInteractionAdded}
                companies={companies}
                contactTypes={contactTypes}
                onCompanyCreate={handleCompanyCreate}
                onContactTypeCreate={handleContactTypeCreate}
                onManageCompanies={openManageCompanies}
                onManageContactTypes={openManageContactTypes}
                isAdminUser={isAdminUser}
            />
        </div>
    );
}

// ============================================================================
// ADD CLIENT DIALOG (UPGRADED)
// ============================================================================

function AddClientDialog({ open, onClose, onCreated, companies, contactTypes, onCompanyCreate, onContactTypeCreate, onManageCompanies, onManageContactTypes, isAdminUser }) {
    const [name, setName] = useState("");
    const [companyId, setCompanyId] = useState(null);
    const [phone, setPhone] = useState("");
    const [email, setEmail] = useState("");
    const [stage, setStage] = useState("lead");
    const [contactType, setContactType] = useState(null);
    const [designation, setDesignation] = useState("");
    const [notes, setNotes] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) {
            setName("");
            setCompanyId(null);
            setPhone("");
            setEmail("");
            setStage("lead");
            setContactType(null);
            setDesignation("");
            setNotes("");
            setSaving(false);
        }
    }, [open]);

    const submit = async (e) => {
        e.preventDefault();
        if (!name.trim()) {
            toast.error("Contact name is required");
            return;
        }
        setSaving(true);
        try {
            const { data } = await adminApi.post("/marketing/clients", {
                name: name.trim(),
                company_id: companyId || null,
                phone_number: phone.trim() || null,
                email: email.trim() || null,
                stage: stage,
                contact_type: contactType || null,
                designation: designation.trim() || null,
                notes: notes.trim() || null,
            });
            onCreated(data);
        } catch (err) {
            toast.error(formatErrorDetail(err, "Failed to create contact"));
        } finally {
            setSaving(false);
        }
    };

    const companyItems = useMemo(() => companies.map((c) => ({ key: c.id, label: c.name })), [companies]);
    const typeItems = useMemo(() => contactTypes.map((t) => ({ key: t.value, label: t.label, group: t.group })), [contactTypes]);

    return (
        <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
            <DialogContent
                className="bg-white border-[#eaeaea] text-[#111111] sm:max-w-lg rounded-2xl shadow-xl overflow-y-auto max-h-[90vh] p-0"
                data-testid="marketing-add-client-dialog"
            >
                <div className="bg-slate-50 border-b border-slate-100 p-6">
                    <DialogHeader>
                        <DialogTitle className="text-2xl font-light tracking-tight text-slate-950 font-display">
                            Create Industry Contact
                        </DialogTitle>
                        <DialogDescription className="text-[#333333] text-xs">
                            Log a new person in Talentgram's industry relationship directory.
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
                        <LookupPicker
                            label="Production House / Company"
                            items={companyItems}
                            itemKey={companyId}
                            onSelect={setCompanyId}
                            onCreate={onCompanyCreate}
                            onManage={onManageCompanies}
                            isAdminUser={isAdminUser}
                            entityName="Company"
                            placeholder="Select company..."
                            testId="marketing-input-company"
                        />
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <LookupPicker
                            label="Contact Type"
                            items={typeItems}
                            itemKey={contactType}
                            onSelect={setContactType}
                            onCreate={onContactTypeCreate}
                            onManage={onManageContactTypes}
                            isAdminUser={isAdminUser}
                            entityName="Contact Type"
                            placeholder="Select type (optional)..."
                            testId="marketing-input-contact-type"
                        />
                        <FieldInput
                            label="Designation / Role"
                            value={designation}
                            onChange={setDesignation}
                            placeholder="E.g. Line Producer"
                            testId="marketing-input-designation"
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
                            <div className="text-[10px] tracking-[0.08em] font-semibold text-[#333333] uppercase font-mono mb-1.5 flex justify-between select-none">
                                <span>Relationship Status</span>
                            </div>
                            <select
                                value={stage}
                                onChange={(e) => setStage(e.target.value)}
                                data-testid="marketing-input-stage"
                                className="mt-1.5 w-full bg-slate-50 border border-[#eaeaea] rounded-xl px-4 py-2.5 text-sm text-[#111111] focus:bg-white focus:border-[#d4d4d4] focus:outline-none transition-colors"
                            >
                                {RELATIONSHIP_STATUSES.map((s) => (
                                    <option key={s.value} value={s.value}>{s.label}</option>
                                ))}
                            </select>
                        </label>
                    </div>

                    <FieldTextarea
                        label="Notes"
                        value={notes}
                        onChange={setNotes}
                        placeholder="Anything worth remembering about this relationship..."
                        testId="marketing-input-notes"
                    />

                    <DialogFooter className="pt-4 border-t border-slate-100 gap-3">
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={saving}
                            data-testid="marketing-add-cancel-btn"
                            className="px-4 py-2.5 text-sm font-semibold text-[#222222] hover:text-[#111111] transition-colors disabled:opacity-40"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={saving}
                            data-testid="marketing-add-submit-btn"
                            className="inline-flex items-center justify-center gap-2 bg-slate-900 text-white px-5 py-2.5 rounded-xl text-sm font-semibold hover:bg-slate-800 transition-colors disabled:opacity-50 min-w-36 shadow-sm"
                        >
                            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : "Save Contact"}
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

function ClientDrawer({ client, onClose, onClientUpdated, onClientDeleted, onInteractionAdded, companies, contactTypes, onCompanyCreate, onContactTypeCreate, onManageCompanies, onManageContactTypes, isAdminUser }) {
    const open = !!client;
    const [interactions, setInteractions] = useState([]);
    const [loadingList, setLoadingList] = useState(false);
    const [type, setType] = useState("call");
    const [notes, setNotes] = useState("");
    const [saving, setSaving] = useState(false);

    // Editing states
    const [isEditing, setIsEditing] = useState(false);
    const [editName, setEditName] = useState("");
    const [editCompanyId, setEditCompanyId] = useState(null);
    const [editPhone, setEditPhone] = useState("");
    const [editEmail, setEditEmail] = useState("");
    const [editStage, setEditStage] = useState("lead");
    const [editContactType, setEditContactType] = useState(null);
    const [editDesignation, setEditDesignation] = useState("");
    const [editNotes, setEditNotes] = useState("");
    const [updating, setUpdating] = useState(false);

    const companyItems = useMemo(() => companies.map((c) => ({ key: c.id, label: c.name })), [companies]);
    const typeItems = useMemo(() => contactTypes.map((t) => ({ key: t.value, label: t.label, group: t.group })), [contactTypes]);

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
        setEditCompanyId(client.company_id || null);
        setEditPhone(client.phone_number || "");
        setEditEmail(client.email || "");
        setEditStage(client.stage || "lead");
        setEditContactType(client.contact_type || null);
        setEditDesignation(client.designation || "");
        setEditNotes(client.notes || "");
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
            const { data } = await adminApi.put(`/marketing/clients/${client.id}`, {
                name: editName.trim(),
                company_id: editCompanyId || "",
                phone_number: editPhone.trim() || null,
                email: editEmail.trim() || null,
                stage: editStage,
                contact_type: editContactType || null,
                designation: editDesignation.trim() || null,
                notes: editNotes.trim() || null,
            });
            onClientUpdated(data);
            setIsEditing(false);
            toast.success("Contact updated successfully.");
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
                className="w-full sm:max-w-2xl bg-white border-l border-[#eaeaea] text-[#111111] overflow-y-auto shadow-2xl p-0 [&>button]:hidden"
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
                                <SheetDescription className="text-[#333333] text-sm sm:text-base font-mono flex items-center gap-2 flex-wrap">
                                    <span>{client.company_name || "Independent Contact"}</span>
                                    {client.designation && (
                                        <>
                                            <span className="text-slate-300">•</span>
                                            <span>{client.designation}</span>
                                        </>
                                    )}
                                    {client.contact_type && (
                                        <>
                                            <span className="text-slate-300">•</span>
                                            <span className="bg-amber-50 text-amber-800 border border-amber-200/50 px-2 py-0.5 rounded-full text-xs font-semibold font-sans tracking-normal uppercase">
                                                {contactTypes.find(t => t.value === client.contact_type)?.label || client.contact_type}
                                            </span>
                                        </>
                                    )}
                                </SheetDescription>
                            </SheetHeader>
                            <div className="absolute right-5 top-6 sm:top-8 flex items-center gap-2">
                                <button
                                    onClick={() => setIsEditing(!isEditing)}
                                    className="p-2 text-[#333333] hover:text-[#111111] border border-[#eaeaea] hover:border-[#d4d4d4] rounded-xl transition-all shadow-sm bg-white"
                                    title="Edit Profile"
                                >
                                    {isEditing ? <Check className="w-4 h-4 text-[#5A7D5A]" /> : <Edit2 className="w-4 h-4" />}
                                </button>
                                <button
                                    type="button"
                                    onClick={onClose}
                                    data-testid="marketing-drawer-close-btn"
                                    className="p-2 text-[#333333] hover:text-[#111111] border border-[#eaeaea] hover:border-[#d4d4d4] rounded-xl transition-all shadow-sm bg-white"
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
                                <form onSubmit={submitUpdate} className="bg-slate-50/50 border border-[#eaeaea] rounded-2xl p-5 space-y-4 shadow-sm animate-in fade-in duration-200">
                                    <h4 className="text-xs font-mono font-semibold text-[#333333] uppercase tracking-wider mb-2">Edit Contact</h4>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <FieldInput label="Name" value={editName} onChange={setEditName} required />
                                        <LookupPicker
                                            label="Production House / Company"
                                            items={companyItems}
                                            itemKey={editCompanyId}
                                            onSelect={setEditCompanyId}
                                            onCreate={onCompanyCreate}
                                            onManage={onManageCompanies}
                                            isAdminUser={isAdminUser}
                                            entityName="Company"
                                            placeholder="Select company..."
                                        />
                                    </div>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <LookupPicker
                                            label="Contact Type"
                                            items={typeItems}
                                            itemKey={editContactType}
                                            onSelect={setEditContactType}
                                            onCreate={onContactTypeCreate}
                                            onManage={onManageContactTypes}
                                            isAdminUser={isAdminUser}
                                            entityName="Contact Type"
                                            placeholder="Select type (optional)..."
                                        />
                                        <FieldInput label="Designation / Role" value={editDesignation} onChange={setEditDesignation} placeholder="E.g. Line Producer" />
                                    </div>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <FieldInput label="Phone" value={editPhone} onChange={setEditPhone} />
                                        <FieldInput label="Email" value={editEmail} onChange={setEditEmail} />
                                    </div>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <label className="block">
                                            <div className="text-[10px] tracking-[0.08em] font-semibold text-[#333333] uppercase font-mono mb-1.5 flex justify-between select-none">
                                                <span>Relationship Status</span>
                                            </div>
                                            <select
                                                value={editStage}
                                                onChange={(e) => setEditStage(e.target.value)}
                                                className="mt-1.5 w-full bg-slate-50 border border-[#eaeaea] rounded-xl px-4 py-2.5 text-sm text-[#111111] focus:bg-white focus:border-[#d4d4d4] focus:outline-none transition-colors"
                                            >
                                                {RELATIONSHIP_STATUSES.map((s) => (
                                                    <option key={s.value} value={s.value}>{s.label}</option>
                                                ))}
                                            </select>
                                        </label>
                                    </div>
                                    <FieldTextarea label="Notes" value={editNotes} onChange={setEditNotes} placeholder="Anything worth remembering about this relationship..." />

                                    <div className="flex gap-2.5 justify-end pt-2">
                                        <button
                                            type="button"
                                            onClick={() => setIsEditing(false)}
                                            className="px-4 py-2 text-xs font-semibold text-[#333333] hover:text-[#111111]"
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
                                        <Sparkles className="w-4 h-4 text-[#333333]" />
                                        <h3 className="text-xs font-mono font-semibold text-[#333333] uppercase tracking-wider">Relationship Scorecard</h3>
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
                                            <div className={`grid ${gridColsClass} gap-2.5 bg-slate-50 border border-[#eaeaea]/60 rounded-2xl p-2.5`}>
                                                {showPhone && (
                                                    <a
                                                        href={`tel:${client.phone_number}`}
                                                        className="flex flex-col items-center justify-center gap-1.5 py-3.5 border border-[#eaeaea]/80 bg-white hover:bg-slate-50 rounded-xl text-[11px] font-semibold text-[#111111] shadow-sm transition-all duration-200 active:scale-[0.97]"
                                                    >
                                                        <Phone className="w-4 h-4 text-[#222222]" />
                                                        <span>Call</span>
                                                    </a>
                                                )}
                                                {showEmail && (
                                                    <a
                                                        href={`mailto:${client.email}`}
                                                        className="flex flex-col items-center justify-center gap-1.5 py-3.5 border border-[#eaeaea]/80 bg-white hover:bg-slate-50 rounded-xl text-[11px] font-semibold text-[#111111] shadow-sm transition-all duration-200 active:scale-[0.97]"
                                                    >
                                                        <Mail className="w-4 h-4 text-[#222222]" />
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
                                            <div className="text-[10px] font-semibold font-mono text-[#333333] uppercase mb-1">Status</div>
                                            <div className="flex items-center justify-center">
                                                {health && (
                                                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[9px] font-bold uppercase tracking-wider border ${health.color}`}>
                                                        {health.label}
                                                    </span>
                                                )}
                                            </div>
                                        </div>
                                        <div className="bg-slate-50/50 border border-slate-100 rounded-xl p-3.5 text-center">
                                            <div className="text-[10px] font-semibold font-mono text-[#333333] uppercase mb-1">Logs</div>
                                            <div className="text-xs font-semibold text-[#111111]">{client.interaction_count || 0} times</div>
                                        </div>
                                        <div className="bg-slate-50/50 border border-slate-100 rounded-xl p-3.5 text-center">
                                            <div className="text-[10px] font-semibold font-mono text-[#333333] uppercase mb-1">Role</div>
                                            <div className="text-xs font-semibold text-[#111111] truncate" title={client.designation || ""}>
                                                {client.designation || "—"}
                                            </div>
                                        </div>
                                    </div>

                                    {/* Relationship card details */}
                                    <div className="bg-slate-50/30 border border-[#eaeaea]/60 rounded-2xl p-4 space-y-2.5 text-xs">
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1 border-b border-slate-100 pb-2.5">
                                            <span className="text-[#333333] font-medium">Email Address</span>
                                            <span className="font-mono text-[#111111] font-semibold break-all">{client.email || "—"}</span>
                                        </div>
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1 border-b border-slate-100 pb-2.5">
                                            <span className="text-[#333333] font-medium">Phone Number</span>
                                            <span className="font-mono text-[#111111] font-semibold">{client.phone_number || "—"}</span>
                                        </div>
                                        {client.tags && client.tags.length > 0 && (
                                            <div className="flex flex-col sm:flex-row sm:justify-between gap-1 border-b border-slate-100 pb-2.5">
                                                <span className="text-[#333333] font-medium">Tags Registered</span>
                                                <span className="font-mono text-[#222222] font-medium">
                                                    {client.tags.map(t => `#${t}`).join(" ")}
                                                </span>
                                            </div>
                                        )}
                                        {client.notes && (
                                            <div className="flex flex-col gap-1 border-b border-slate-100 pb-2.5">
                                                <span className="text-[#333333] font-medium">Notes</span>
                                                <span className="text-[#111111] whitespace-pre-wrap">{client.notes}</span>
                                            </div>
                                        )}
                                        <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                                            <span className="text-[#333333] font-medium">Last Contacted</span>
                                            <span className="text-[#111111] font-semibold">
                                                {formatDateTime(client.last_contacted_date)}
                                                {daysSince !== null && (
                                                    <span className="ml-1.5 text-[#333333] text-[10px] font-mono">({daysSince}d ago)</span>
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
                                        <p className="text-[11px] text-[#333333] leading-normal select-none">
                                            Manage the visibility of this contact record. Deleting or archiving will remove it from your active workspace.
                                        </p>
                                        <div className="flex gap-2">
                                            <button
                                                type="button"
                                                onClick={handleArchive}
                                                disabled={updating}
                                                data-testid="marketing-drawer-archive-btn"
                                                className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 border border-[#eaeaea] hover:border-[#d4d4d4] rounded-xl text-xs font-semibold text-[#111111] bg-white hover:bg-slate-50 transition-colors shadow-sm disabled:opacity-40"
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
                                    <MessageSquare className="w-4 h-4 text-[#333333]" />
                                    <h3 className="text-xs font-mono font-semibold text-[#333333] uppercase tracking-wider">Log Communication</h3>
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
                                                            : "bg-white text-[#222222] border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50"
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
                                            className="w-full bg-slate-50/50 border border-[#eaeaea] rounded-2xl px-4 py-3 text-sm text-[#111111] placeholder:text-[#333333] focus:bg-white focus:border-[#d4d4d4] focus:ring-1 focus:ring-slate-300 focus:outline-none transition-colors resize-none shadow-inner"
                                        />
                                        <div className="absolute right-3.5 bottom-3 text-[10px] font-mono text-[#333333]">
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
                                    <Clock className="w-4 h-4 text-[#333333]" />
                                    <h3 className="text-xs font-mono font-semibold text-[#333333] uppercase tracking-wider">
                                        Timeline Records ({interactions.length})
                                    </h3>
                                </div>
                                {/* Unified WhatsApp timeline (Slice 4 / Feature 2) */}
                                {client?.id && (
                                    <div className="mb-5 pb-5 border-b border-[#eaeaea]">
                                        <CommTimeline subjectType="CRM_CLIENT" subjectId={client.id} title="WhatsApp Timeline" />
                                    </div>
                                )}
                                {loadingList ? (
                                    <div className="py-8 flex justify-center">
                                        <Loader2 className="w-5 h-5 animate-spin text-slate-300" />
                                    </div>
                                ) : interactions.length === 0 ? (
                                    <div
                                        className="text-xs text-[#333333] py-10 text-center border-2 border-dashed border-[#eaeaea] rounded-2xl bg-slate-50/20"
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
                                                    
                                                    <div className="bg-white border border-[#eaeaea] rounded-2xl p-4 ml-2 shadow-[0_2px_10px_rgba(15,23,42,0.02)] hover:shadow-md transition-shadow duration-200">
                                                        <div className="flex flex-wrap items-center justify-between gap-2 mb-2 border-b border-slate-50 pb-2">
                                                            <div className="flex items-center gap-1.5">
                                                                <Icon className="w-3.5 h-3.5 text-[#333333]" />
                                                                <span className="text-xs font-semibold text-[#111111] capitalize">
                                                                    {it.type}
                                                                </span>
                                                            </div>
                                                            <span className="font-mono text-[9px] text-[#333333]">
                                                                {formatDateTime(it.created_at)}
                                                            </span>
                                                        </div>
                                                        {it.notes && (
                                                            <div className="text-sm text-[#111111] leading-relaxed whitespace-pre-wrap break-words">
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
