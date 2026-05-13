import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import { adminApi, getAdmin, isAdmin } from "@/lib/api";
import { toast } from "sonner";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    UserPlus,
    Copy,
    Shield,
    Users as UsersIcon,
    Ban,
    Check,
    Trash2,
    KeyRound,
    Loader2,
    X,
    Mail,
} from "lucide-react";

const ROLE_BADGE = {
    admin: "bg-[#c9a961]/10 text-[#c9a961] border border-[#c9a961]/20 group-hover:border-[#c9a961]/40 transition-colors",
    team: "bg-white/5 text-white/60 border border-white/10 group-hover:border-white/20 transition-colors",
};

const STATUS_BADGE = {
    active: "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20",
    invited: "bg-sky-500/10 text-sky-400 border border-sky-500/20",
    disabled: "bg-white/5 text-white/40 border border-white/10",
};

function StatCard({ label, value, icon: Icon, testid }) {
    const [displayValue, setDisplayValue] = React.useState(0);

    React.useEffect(() => {
        const duration = 800;
        const steps = 30;
        const increment = value / steps;
        let current = 0;
        const timer = setInterval(() => {
            current += increment;
            if (current >= value) {
                setDisplayValue(value);
                clearInterval(timer);
            } else {
                setDisplayValue(Math.floor(current));
            }
        }, duration / steps);
        return () => clearInterval(timer);
    }, [value]);

    return (
        <div
            className="group relative overflow-hidden border border-white/[0.08] bg-gradient-to-br from-white/[0.02] to-transparent backdrop-blur-sm p-5 transition-all duration-500 hover:border-white/[0.15] hover:shadow-[0_8px_30px_rgba(0,0,0,0.4)] hover:-translate-y-0.5"
            data-testid={testid}
        >
            <div className="absolute inset-0 bg-gradient-to-br from-white/0 via-white/0 to-white/[0.02] opacity-0 group-hover:opacity-100 transition-opacity duration-700" />
            <div className="flex items-center gap-4 relative z-10">
                <div className="w-10 h-10 rounded-sm border border-white/10 bg-black/20 flex items-center justify-center text-white/40 group-hover:text-white/70 group-hover:border-white/20 transition-all duration-300">
                    <Icon className="w-4 h-4" />
                </div>
                <div>
                    <div className="text-[10px] tracking-[0.2em] uppercase text-white/50 group-hover:text-white/70 transition-colors">
                        {label}
                    </div>
                    <div className="font-display text-3xl mt-1 tracking-tight">
                        {displayValue}
                    </div>
                </div>
            </div>
            <div className="absolute bottom-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent group-hover:via-white/20 transition-all duration-500" />
        </div>
    );
}

function formatDate(iso) {
    if (!iso) return "—";
    try {
        const d = new Date(iso);
        return d.toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
        });
    } catch {
        return iso;
    }
}

function InviteModal({ open, onClose, onInvited }) {
    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [role, setRole] = useState("team");
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState(null);

    const reset = () => {
        setName("");
        setEmail("");
        setRole("team");
        setResult(null);
    };

    const submit = async () => {
        if (!name.trim() || !email.trim()) {
            toast.error("Name and email are required");
            return;
        }
        setBusy(true);
        try {
            const { data } = await adminApi.post("/users/invite", {
                name: name.trim(),
                email: email.trim().toLowerCase(),
                role,
            });
            setResult(data);
            onInvited();
            toast.success("Invite created");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to invite");
        } finally {
            setBusy(false);
        }
    };

    const inviteUrl = result
        ? `${window.location.origin}${result.invite_path}`
        : "";

    const copy = () => {
        navigator.clipboard.writeText(inviteUrl);
        toast.success("Invite link copied");
    };

    if (!open) return null;
    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            data-testid="invite-modal"
            style={{
                background: "radial-gradient(ellipse at center, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.95) 100%)",
                backdropFilter: "blur(8px)",
            }}
        >
            <div className="relative w-full max-w-lg animate-in fade-in zoom-in-95 duration-300">
                <div className="absolute inset-0 bg-gradient-to-br from-white/[0.05] via-transparent to-black/40 rounded-2xl blur-xl" />
                <div className="relative border border-white/[0.08] bg-black/80 backdrop-blur-md p-6 md:p-8 rounded-2xl shadow-2xl">
                    <button
                        type="button"
                        onClick={() => {
                            onClose();
                            reset();
                        }}
                        className="absolute top-5 right-5 text-white/40 hover:text-white/80 transition-all hover:scale-110"
                        data-testid="invite-close-btn"
                    >
                        <X className="w-4 h-4" />
                    </button>
                    <p className="eyebrow mb-1 text-[#c9a961]">Invite</p>
                    <h2 className="font-display text-3xl tracking-tight mb-8 bg-gradient-to-r from-white to-white/70 bg-clip-text text-transparent">
                        Add a team member
                    </h2>

                    {!result ? (
                        <>
                            <label className="block mb-6">
                                <span className="text-[11px] tracking-[0.2em] uppercase text-white/50">
                                    Full name
                                </span>
                                <input
                                    value={name}
                                    onChange={(e) => setName(e.target.value)}
                                    placeholder="e.g. Raj Khanna"
                                    data-testid="invite-name"
                                    className="mt-2 w-full bg-white/[0.03] border-b border-white/10 focus:border-[#c9a961]/60 outline-none py-3 text-sm transition-all focus:bg-white/[0.05] text-white"
                                />
                            </label>
                            <label className="block mb-6">
                                <span className="text-[11px] tracking-[0.2em] uppercase text-white/50">
                                    Email
                                </span>
                                <input
                                    type="email"
                                    value={email}
                                    onChange={(e) => setEmail(e.target.value)}
                                    placeholder="name@talentgram.com"
                                    data-testid="invite-email"
                                    className="mt-2 w-full bg-white/[0.03] border-b border-white/10 focus:border-[#c9a961]/60 outline-none py-3 text-sm transition-all focus:bg-white/[0.05] text-white"
                                />
                            </label>
                            <label className="block mb-8">
                                <span className="text-[11px] tracking-[0.2em] uppercase text-white/50">
                                    Role
                                </span>
                                <div className="mt-2" data-testid="invite-role-wrap">
                                    <Select value={role} onValueChange={setRole}>
                                        <SelectTrigger
                                            className="w-full bg-white/[0.03] border-white/10 text-white hover:bg-white/[0.06] transition-all"
                                            data-testid="invite-role-trigger"
                                        >
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent className="bg-black/90 backdrop-blur-md border-white/10">
                                            <SelectItem value="team">Team</SelectItem>
                                            <SelectItem value="admin">Admin</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>
                            </label>
                            <button
                                onClick={submit}
                                disabled={busy}
                                data-testid="invite-submit-btn"
                                className="relative w-full overflow-hidden bg-gradient-to-r from-white to-white/90 text-black py-3.5 rounded-lg text-sm font-medium hover:shadow-lg hover:shadow-white/10 transition-all duration-300 hover:-translate-y-0.5 disabled:opacity-50 disabled:hover:translate-y-0 inline-flex items-center justify-center gap-2"
                            >
                                {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                                Create invite
                            </button>
                        </>
                    ) : (
                        <div data-testid="invite-result" className="animate-in fade-in duration-300">
                            <div className="border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 mb-6 text-sm text-emerald-300 rounded-lg inline-flex items-center gap-2 backdrop-blur-sm">
                                <Check className="w-4 h-4" /> Invite created for{" "}
                                <span className="font-medium">{result.user.email}</span>
                            </div>
                            <p className="text-xs text-white/60 mb-3">
                                Send this link to the invitee. It is single-use and
                                expires on {formatDate(result.expires_at)}.
                            </p>
                            <div className="flex items-center gap-2 border border-white/10 bg-black/40 rounded-lg p-3 mb-6 backdrop-blur-sm">
                                <Mail className="w-3.5 h-3.5 text-white/40 shrink-0" />
                                <code
                                    className="text-xs font-mono flex-1 truncate text-white/80"
                                    data-testid="invite-url"
                                >
                                    {inviteUrl}
                                </code>
                                <button
                                    type="button"
                                    onClick={copy}
                                    className="text-xs px-3 py-1.5 border border-white/10 hover:border-white/30 rounded-md inline-flex items-center gap-1 transition-all hover:bg-white/5"
                                    data-testid="invite-copy-btn"
                                >
                                    <Copy className="w-3 h-3" /> Copy
                                </button>
                            </div>
                            <button
                                onClick={() => {
                                    onClose();
                                    reset();
                                }}
                                className="w-full border border-white/10 hover:border-white/30 py-3.5 rounded-lg text-sm transition-all hover:bg-white/5"
                                data-testid="invite-done-btn"
                            >
                                Done
                            </button>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function ResetLinkModal({ open, onClose, payload }) {
    if (!open || !payload) return null;
    const BASE = window.location.origin;
    const fullLink = `${BASE}${payload.reset_path}`;
    const copy = () => {
        navigator.clipboard.writeText(fullLink);
        toast.success("Reset link copied to clipboard");
    };
    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            data-testid="reset-link-modal"
            style={{
                background: "radial-gradient(ellipse at center, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.95) 100%)",
                backdropFilter: "blur(8px)",
            }}
        >
            <div className="relative w-full max-w-md animate-in fade-in zoom-in-95 duration-300">
                <div className="absolute inset-0 bg-gradient-to-br from-white/[0.05] via-transparent to-black/40 rounded-2xl blur-xl" />
                <div className="relative border border-white/[0.08] bg-black/80 backdrop-blur-md p-6 md:p-8 rounded-2xl shadow-2xl">
                    <p className="eyebrow mb-1 text-[#c9a961]">Password reset</p>
                    <h2 className="font-display text-2xl tracking-tight mb-4">Single-use reset link</h2>
                    <p className="text-sm text-white/60 mb-6 leading-relaxed">
                        Send this link to <span className="text-white font-medium">{payload.email}</span> over a secure channel.
                        It expires in 1 hour and can only be used once. We never store the raw token.
                    </p>
                    <div
                        className="text-xs font-mono break-all bg-black/40 border border-white/10 rounded-lg p-4 mb-6 text-white/80"
                        data-testid="reset-link-value"
                    >
                        {fullLink}
                    </div>
                    <div className="flex gap-3">
                        <button
                            onClick={copy}
                            className="flex-1 border border-white/10 hover:border-white/30 py-3.5 rounded-lg text-sm inline-flex items-center justify-center gap-2 transition-all hover:bg-white/5"
                            data-testid="reset-link-copy-btn"
                        >
                            <Copy className="w-3.5 h-3.5" /> Copy link
                        </button>
                        <button
                            onClick={onClose}
                            className="flex-1 bg-gradient-to-r from-white to-white/90 text-black py-3.5 rounded-lg text-sm font-medium hover:shadow-lg transition-all"
                            data-testid="reset-link-close-btn"
                        >
                            Done
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default function UserManagement() {
    const admin = getAdmin();

    const [items, setItems] = useState([]);
    const [stats, setStats] = useState({ total: 0, admin: 0, team: 0 });
    const [loading, setLoading] = useState(true);
    const [inviteOpen, setInviteOpen] = useState(false);
    const [resetLink, setResetLink] = useState(null);
    const [busyId, setBusyId] = useState(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/users");
            setItems(data.items || []);
            setStats(data.stats || {});
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to load users");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const patchRole = async (uid, role) => {
        setBusyId(uid);
        try {
            await adminApi.post(`/users/${uid}/role`, { role });
            toast.success("Role updated");
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to update role");
        } finally {
            setBusyId(null);
        }
    };

    const toggleDisable = async (u) => {
        setBusyId(u.id);
        try {
            const action = u.status === "disabled" ? "enable" : "disable";
            await adminApi.post(`/users/${u.id}/${action}`);
            toast.success(action === "enable" ? "User enabled" : "User disabled");
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        } finally {
            setBusyId(null);
        }
    };

    const resetPw = async (u) => {
        setBusyId(u.id);
        try {
            const { data } = await adminApi.post(`/users/${u.id}/reset-password`);
            setResetLink({
                reset_path: data.reset_path,
                email: data.email || u.email,
                expires_at: data.expires_at,
            });
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        } finally {
            setBusyId(null);
        }
    };

    const remove = async (u) => {
        if (
            !window.confirm(
                `Permanently delete ${u.email}? This cannot be undone.`,
            )
        )
            return;
        setBusyId(u.id);
        try {
            await adminApi.delete(`/users/${u.id}`);
            toast.success("User deleted");
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        } finally {
            setBusyId(null);
        }
    };

    const sorted = useMemo(() => {
        const scoreStatus = (s) =>
            s === "disabled" ? 2 : s === "invited" ? 1 : 0;
        return [...items].sort((a, b) => {
            if (a.id === admin?.id) return -1;
            if (b.id === admin?.id) return 1;
            const sa = scoreStatus(a.status);
            const sb = scoreStatus(b.status);
            if (sa !== sb) return sa - sb;
            if (a.role !== b.role) return a.role === "admin" ? -1 : 1;
            return (a.created_at || "").localeCompare(b.created_at || "");
        });
    }, [items, admin?.id]);

    if (!isAdmin()) return <Navigate to="/admin" replace />;

    return (
        <div className="relative min-h-screen" data-testid="user-management-page">
            {/* Cinematic atmospheric background */}
            <div className="fixed inset-0 bg-black" />
            <div className="fixed inset-0 bg-gradient-to-br from-black via-black/95 to-black/90" />
            <div className="fixed inset-0 bg-[radial-gradient(ellipse_at_top_right,_rgba(201,169,97,0.08)_0%,_transparent_60%)]" />
            <div className="fixed inset-0 bg-[radial-gradient(ellipse_at_bottom_left,_rgba(255,255,255,0.03)_0%,_transparent_70%)]" />
            
            <div className="relative z-10 p-6 md:p-12">
                <div className="max-w-[1400px] mx-auto">
                    {/* Header */}
                    <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 mb-10 animate-in fade-in slide-in-from-top-4 duration-700">
                        <div>
                            <p className="text-[#c9a961] text-[11px] tracking-[0.3em] uppercase mb-2 font-semibold">
                                Settings
                            </p>
                            <h1 className="font-display text-5xl md:text-6xl tracking-tight bg-gradient-to-r from-white via-white to-white/70 bg-clip-text text-transparent">
                                User Management
                            </h1>
                            <p className="text-white/50 mt-3 text-sm">
                                Control access, roles, and team permissions
                            </p>
                        </div>
                        <button
                            onClick={() => setInviteOpen(true)}
                            className="group relative overflow-hidden bg-gradient-to-r from-[#c9a961] to-[#b8944f] text-black px-6 py-3 rounded-lg text-sm font-semibold inline-flex items-center gap-2 hover:shadow-lg hover:shadow-[#c9a961]/20 transition-all duration-300 hover:-translate-y-0.5"
                            data-testid="invite-user-btn"
                        >
                            <UserPlus className="w-4 h-4 transition-transform group-hover:scale-110" /> 
                            <span>Invite User</span>
                        </button>
                    </div>

                    {/* Stats Grid */}
                    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-12 animate-in fade-in slide-in-from-bottom-4 duration-700 delay-100">
                        <StatCard
                            label="Total"
                            value={stats.total || 0}
                            icon={UsersIcon}
                            testid="stat-total"
                        />
                        <StatCard
                            label="Admins"
                            value={stats.admin || 0}
                            icon={Shield}
                            testid="stat-admin"
                        />
                        <StatCard
                            label="Team"
                            value={stats.team || 0}
                            icon={UsersIcon}
                            testid="stat-team"
                        />
                        <StatCard
                            label="Disabled"
                            value={stats.disabled || 0}
                            icon={Ban}
                            testid="stat-disabled"
                        />
                    </div>

                    {/* Users Table - Desktop */}
                    <div className="hidden lg:block animate-in fade-in duration-700 delay-200">
                        <div className="relative rounded-xl overflow-hidden border border-white/[0.06] bg-white/[0.02] backdrop-blur-sm">
                            {/* Table Header */}
                            <div className="grid grid-cols-[1.5fr_2fr_0.8fr_0.8fr_1fr_1.2fr] gap-4 px-6 py-4 text-[10px] font-semibold tracking-[0.2em] uppercase text-white/50 border-b border-white/[0.06] bg-white/[0.02]">
                                <div>Name</div>
                                <div>Email</div>
                                <div>Role</div>
                                <div>Status</div>
                                <div>Last login</div>
                                <div className="text-right">Actions</div>
                            </div>

                            {/* Table Body */}
                            {loading && (
                                <div className="p-16 text-center">
                                    <Loader2 className="w-6 h-6 animate-spin mx-auto text-[#c9a961]/60" />
                                    <p className="text-white/50 text-sm mt-3">Loading users...</p>
                                </div>
                            )}
                            {!loading && sorted.length === 0 && (
                                <div className="p-16 text-center">
                                    <div className="w-16 h-16 mx-auto mb-4 rounded-full border border-white/10 flex items-center justify-center">
                                        <UsersIcon className="w-8 h-8 text-white/20" />
                                    </div>
                                    <p className="text-white/50 text-sm">No users yet. Invite your first team member.</p>
                                </div>
                            )}
                            {!loading && sorted.map((u) => {
                                const self = u.id === admin?.id;
                                const dimmed = u.status === "disabled";
                                return (
                                    <div
                                        key={u.id}
                                        className={`group relative grid grid-cols-[1.5fr_2fr_0.8fr_0.8fr_1fr_1.2fr] gap-4 px-6 py-4 items-center border-b border-white/[0.03] transition-all duration-300 hover:bg-white/[0.04] hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.05)] ${dimmed ? "opacity-50" : ""}`}
                                        data-testid={`user-row-${u.id}`}
                                    >
                                        <div className="flex items-center gap-2">
                                            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-white/10 to-white/5 border border-white/10 flex items-center justify-center text-xs font-medium">
                                                {u.name?.[0]?.toUpperCase() || u.email[0]?.toUpperCase()}
                                            </div>
                                            <div className="truncate font-medium text-white/90">
                                                {u.name || "—"}
                                                {self && (
                                                    <span className="ml-2 text-[10px] font-mono text-[#c9a961]/70">
                                                        (you)
                                                    </span>
                                                )}
                                            </div>
                                        </div>
                                        <div className="truncate font-mono text-xs text-white/70">
                                            {u.email}
                                        </div>
                                        <div>
                                            <Select
                                                value={u.role}
                                                onValueChange={(r) =>
                                                    r !== u.role && patchRole(u.id, r)
                                                }
                                                disabled={self || busyId === u.id}
                                            >
                                                <SelectTrigger
                                                    className={`h-7 text-xs px-2 py-0 border-0 ${ROLE_BADGE[u.role]} w-[90px] focus:ring-0 focus:ring-offset-0 transition-all group-hover:scale-105`}
                                                    data-testid={`role-select-${u.id}`}
                                                >
                                                    <SelectValue />
                                                </SelectTrigger>
                                                <SelectContent className="bg-black/90 backdrop-blur-md border-white/10">
                                                    <SelectItem value="team">Team</SelectItem>
                                                    <SelectItem value="admin">Admin</SelectItem>
                                                </SelectContent>
                                            </Select>
                                        </div>
                                        <div>
                                            <span
                                                className={`inline-flex px-2 py-0.5 rounded-md text-[10px] font-bold tracking-wider uppercase ${STATUS_BADGE[u.status]}`}
                                                data-testid={`user-status-${u.id}`}
                                            >
                                                {u.status}
                                            </span>
                                        </div>
                                        <div className="text-xs font-mono text-white/50">
                                            {formatDate(u.last_login)}
                                        </div>
                                        <div className="flex items-center gap-1.5 justify-end">
                                            <button
                                                disabled={self || busyId === u.id}
                                                onClick={() => toggleDisable(u)}
                                                title={u.status === "disabled" ? "Enable user" : "Disable user"}
                                                data-testid={`disable-btn-${u.id}`}
                                                className="p-2 rounded-md border border-white/10 hover:border-white/30 bg-white/[0.02] hover:bg-white/[0.06] transition-all disabled:opacity-30 hover:scale-105"
                                            >
                                                {u.status === "disabled" ? (
                                                    <Check className="w-3.5 h-3.5" />
                                                ) : (
                                                    <Ban className="w-3.5 h-3.5" />
                                                )}
                                            </button>
                                            <button
                                                disabled={busyId === u.id}
                                                onClick={() => resetPw(u)}
                                                title="Reset password"
                                                data-testid={`reset-pw-btn-${u.id}`}
                                                className="p-2 rounded-md border border-white/10 hover:border-white/30 bg-white/[0.02] hover:bg-white/[0.06] transition-all disabled:opacity-30 hover:scale-105"
                                            >
                                                <KeyRound className="w-3.5 h-3.5" />
                                            </button>
                                            <button
                                                disabled={self || busyId === u.id}
                                                onClick={() => remove(u)}
                                                title="Delete user"
                                                data-testid={`delete-btn-${u.id}`}
                                                className="p-2 rounded-md border border-white/10 hover:border-red-500/50 hover:text-red-400 bg-white/[0.02] hover:bg-red-500/10 transition-all disabled:opacity-30 hover:scale-105"
                                            >
                                                <Trash2 className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>

                    {/* Users Table - Mobile/Tablet (Card View) */}
                    <div className="lg:hidden space-y-3 animate-in fade-in duration-700 delay-200">
                        {loading && (
                            <div className="p-16 text-center">
                                <Loader2 className="w-6 h-6 animate-spin mx-auto text-[#c9a961]/60" />
                                <p className="text-white/50 text-sm mt-3">Loading users...</p>
                            </div>
                        )}
                        {!loading && sorted.length === 0 && (
                            <div className="p-16 text-center border border-white/10 rounded-xl bg-white/[0.02]">
                                <div className="w-16 h-16 mx-auto mb-4 rounded-full border border-white/10 flex items-center justify-center">
                                    <UsersIcon className="w-8 h-8 text-white/20" />
                                </div>
                                <p className="text-white/50 text-sm">No users yet. Invite your first team member.</p>
                            </div>
                        )}
                        {!loading && sorted.map((u) => {
                            const self = u.id === admin?.id;
                            const dimmed = u.status === "disabled";
                            return (
                                <div
                                    key={u.id}
                                    className={`relative p-5 rounded-xl border border-white/[0.08] bg-gradient-to-br from-white/[0.02] to-transparent backdrop-blur-sm transition-all duration-300 hover:border-white/[0.15] ${dimmed ? "opacity-50" : ""}`}
                                    data-testid={`user-row-${u.id}`}
                                >
                                    <div className="flex items-start justify-between mb-4">
                                        <div className="flex items-center gap-3">
                                            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-white/10 to-white/5 border border-white/10 flex items-center justify-center text-sm font-medium">
                                                {u.name?.[0]?.toUpperCase() || u.email[0]?.toUpperCase()}
                                            </div>
                                            <div>
                                                <div className="font-semibold text-white/90">
                                                    {u.name || "—"}
                                                    {self && (
                                                        <span className="ml-2 text-[10px] font-mono text-[#c9a961]/70">
                                                            (you)
                                                        </span>
                                                    )}
                                                </div>
                                                <div className="font-mono text-xs text-white/60 mt-0.5">
                                                    {u.email}
                                                </div>
                                            </div>
                                        </div>
                                        <span
                                            className={`inline-flex px-2 py-0.5 rounded-md text-[9px] font-bold tracking-wider uppercase ${STATUS_BADGE[u.status]}`}
                                            data-testid={`user-status-${u.id}`}
                                        >
                                            {u.status}
                                        </span>
                                    </div>
                                    <div className="grid grid-cols-2 gap-3 mb-4 text-sm">
                                        <div>
                                            <div className="text-[10px] tracking-wider uppercase text-white/50 mb-1">Role</div>
                                            <Select
                                                value={u.role}
                                                onValueChange={(r) =>
                                                    r !== u.role && patchRole(u.id, r)
                                                }
                                                disabled={self || busyId === u.id}
                                            >
                                                <SelectTrigger
                                                    className={`h-8 text-xs ${ROLE_BADGE[u.role]} w-full transition-all`}
                                                    data-testid={`role-select-${u.id}`}
                                                >
                                                    <SelectValue />
                                                </SelectTrigger>
                                                <SelectContent className="bg-black/90 backdrop-blur-md border-white/10">
                                                    <SelectItem value="team">Team</SelectItem>
                                                    <SelectItem value="admin">Admin</SelectItem>
                                                </SelectContent>
                                            </Select>
                                        </div>
                                        <div>
                                            <div className="text-[10px] tracking-wider uppercase text-white/50 mb-1">Last login</div>
                                            <div className="font-mono text-xs text-white/60">
                                                {formatDate(u.last_login)}
                                            </div>
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2 justify-end pt-2 border-t border-white/10">
                                        <button
                                            disabled={self || busyId === u.id}
                                            onClick={() => toggleDisable(u)}
                                            title={u.status === "disabled" ? "Enable user" : "Disable user"}
                                            data-testid={`disable-btn-${u.id}`}
                                            className="flex-1 py-2 rounded-md border border-white/10 hover:border-white/30 bg-white/[0.02] hover:bg-white/[0.06] transition-all disabled:opacity-30 text-sm inline-flex items-center justify-center gap-2"
                                        >
                                            {u.status === "disabled" ? (
                                                <Check className="w-3.5 h-3.5" />
                                            ) : (
                                                <Ban className="w-3.5 h-3.5" />
                                            )}
                                            {u.status === "disabled" ? "Enable" : "Disable"}
                                        </button>
                                        <button
                                            disabled={busyId === u.id}
                                            onClick={() => resetPw(u)}
                                            title="Reset password"
                                            data-testid={`reset-pw-btn-${u.id}`}
                                            className="flex-1 py-2 rounded-md border border-white/10 hover:border-white/30 bg-white/[0.02] hover:bg-white/[0.06] transition-all disabled:opacity-30 text-sm inline-flex items-center justify-center gap-2"
                                        >
                                            <KeyRound className="w-3.5 h-3.5" />
                                            Reset
                                        </button>
                                        <button
                                            disabled={self || busyId === u.id}
                                            onClick={() => remove(u)}
                                            title="Delete user"
                                            data-testid={`delete-btn-${u.id}`}
                                            className="flex-1 py-2 rounded-md border border-white/10 hover:border-red-500/50 hover:text-red-400 bg-white/[0.02] hover:bg-red-500/10 transition-all disabled:opacity-30 text-sm inline-flex items-center justify-center gap-2"
                                        >
                                            <Trash2 className="w-3.5 h-3.5" />
                                            Delete
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            </div>

            <InviteModal
                open={inviteOpen}
                onClose={() => setInviteOpen(false)}
                onInvited={load}
            />
            <ResetLinkModal
                open={!!resetLink}
                onClose={() => setResetLink(null)}
                payload={resetLink}
            />
        </div>
    );
}
