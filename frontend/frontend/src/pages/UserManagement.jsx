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
    admin:
        "bg-[#c9a961]/15 text-[#c9a961] border border-[#c9a961]/30",
    team:
        "bg-white/5 text-white/70 border border-white/15",
};

const STATUS_BADGE = {
    active: "bg-emerald-500/10 text-emerald-400 border border-emerald-500/25",
    invited: "bg-sky-500/10 text-sky-400 border border-sky-500/25",
    disabled: "bg-white/5 text-white/40 border border-white/10",
};

function StatCard({ label, value, icon: Icon, testid }) {
    return (
        <div
            className="border border-white/10 p-5 flex items-center gap-4"
            data-testid={testid}
        >
            <div className="w-10 h-10 rounded-sm border border-white/10 flex items-center justify-center text-white/50">
                <Icon className="w-4 h-4" />
            </div>
            <div>
                <div className="text-[10px] tracking-widest uppercase text-white/40">
                    {label}
                </div>
                <div className="font-display text-3xl mt-1">{value}</div>
            </div>
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
            className="fixed inset-0 z-50 bg-black/80 backdrop-blur flex items-center justify-center p-4"
            data-testid="invite-modal"
        >
            <div className="w-full max-w-lg border border-border bg-background p-6 md:p-8 relative">
                <button
                    type="button"
                    onClick={() => {
                        onClose();
                        reset();
                    }}
                    className="absolute top-4 right-4 text-muted-foreground hover:text-foreground"
                    data-testid="invite-close-btn"
                >
                    <X className="w-4 h-4" />
                </button>
                <p className="eyebrow mb-1">Invite</p>
                <h2 className="font-display text-2xl mb-6">Add a team member</h2>

                {!result ? (
                    <>
                        <label className="block mb-4">
                            <span className="text-[11px] tracking-widest uppercase text-muted-foreground">
                                Full name
                            </span>
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="e.g. Raj Khanna"
                                data-testid="invite-name"
                                className="mt-2 w-full bg-transparent border-b border-border focus:border-foreground/60 outline-none py-2.5 text-sm"
                            />
                        </label>
                        <label className="block mb-4">
                            <span className="text-[11px] tracking-widest uppercase text-muted-foreground">
                                Email
                            </span>
                            <input
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                placeholder="name@talentgram.com"
                                data-testid="invite-email"
                                className="mt-2 w-full bg-transparent border-b border-border focus:border-foreground/60 outline-none py-2.5 text-sm"
                            />
                        </label>
                        <label className="block mb-8">
                            <span className="text-[11px] tracking-widest uppercase text-muted-foreground">
                                Role
                            </span>
                            <div className="mt-2" data-testid="invite-role-wrap">
                                <Select value={role} onValueChange={setRole}>
                                    <SelectTrigger
                                        className="w-full"
                                        data-testid="invite-role-trigger"
                                    >
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
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
                            className="w-full bg-foreground text-background py-3 rounded-sm text-sm hover:opacity-90 inline-flex items-center justify-center gap-2"
                        >
                            {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                            Create invite
                        </button>
                    </>
                ) : (
                    <div data-testid="invite-result">
                        <div className="border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 mb-5 text-sm text-emerald-300 inline-flex items-center gap-2">
                            <Check className="w-4 h-4" /> Invite created for{" "}
                            <span className="font-medium">{result.user.email}</span>
                        </div>
                        <p className="text-xs text-muted-foreground mb-2">
                            Send this link to the invitee. It is single-use and
                            expires on {formatDate(result.expires_at)}.
                        </p>
                        <div className="flex items-center gap-2 border border-border bg-muted/30 rounded-sm p-3 mb-6">
                            <Mail className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                            <code
                                className="text-xs tg-mono flex-1 truncate"
                                data-testid="invite-url"
                            >
                                {inviteUrl}
                            </code>
                            <button
                                type="button"
                                onClick={copy}
                                className="text-xs px-3 py-1.5 border border-border hover:border-foreground/60 rounded-sm inline-flex items-center gap-1"
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
                            className="w-full border border-border hover:border-foreground/60 py-3 rounded-sm text-sm"
                            data-testid="invite-done-btn"
                        >
                            Done
                        </button>
                    </div>
                )}
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
            className="fixed inset-0 z-50 bg-black/80 backdrop-blur flex items-center justify-center p-4"
            data-testid="reset-link-modal"
        >
            <div className="w-full max-w-md border border-border bg-background p-6 md:p-8">
                <p className="eyebrow mb-1">Password reset</p>
                <h2 className="font-display text-2xl mb-4">Single-use reset link</h2>
                <p className="text-sm text-muted-foreground mb-5">
                    Send this link to <span className="text-foreground">{payload.email}</span> over a secure channel.
                    It expires in 1 hour and can only be used once. We never store the raw token.
                </p>
                <div
                    className="text-xs tg-mono break-all bg-muted/40 border border-border rounded-sm p-4 mb-5"
                    data-testid="reset-link-value"
                >
                    {fullLink}
                </div>
                <div className="flex gap-2">
                    <button
                        onClick={copy}
                        className="flex-1 border border-border hover:border-foreground/60 py-3 rounded-sm text-sm inline-flex items-center justify-center gap-2"
                        data-testid="reset-link-copy-btn"
                    >
                        <Copy className="w-3.5 h-3.5" /> Copy link
                    </button>
                    <button
                        onClick={onClose}
                        className="flex-1 bg-foreground text-background py-3 rounded-sm text-sm"
                        data-testid="reset-link-close-btn"
                    >
                        Done
                    </button>
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
        // Pin current user first, then admins, then team, then disabled
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

    // Frontend guard — backend enforces this already via 403
    if (!isAdmin()) return <Navigate to="/admin" replace />;

    return (
        <div className="p-6 md:p-12" data-testid="user-management-page">
            <div className="flex items-center justify-between flex-wrap gap-3 mb-8">
                <div>
                    <p className="eyebrow">Settings</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight mt-1">
                        User Management
                    </h1>
                </div>
                <button
                    onClick={() => setInviteOpen(true)}
                    className="bg-white text-black px-5 py-2.5 rounded-sm text-sm inline-flex items-center gap-2 hover:opacity-90"
                    data-testid="invite-user-btn"
                >
                    <UserPlus className="w-4 h-4" /> Invite User
                </button>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
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

            <div
                className="border border-white/10 overflow-hidden"
                data-testid="users-table"
            >
                <div className="grid grid-cols-[2fr_2fr_1fr_1fr_1.2fr_1.6fr] gap-3 px-5 py-3 text-[10px] tracking-widest uppercase text-white/40 border-b border-white/10 bg-white/[0.02]">
                    <div>Name</div>
                    <div>Email</div>
                    <div>Role</div>
                    <div>Status</div>
                    <div>Last login</div>
                    <div className="text-right">Actions</div>
                </div>
                {loading && (
                    <div className="p-8 text-center text-white/50 text-sm">
                        <Loader2 className="w-5 h-5 animate-spin mx-auto" />
                    </div>
                )}
                {!loading && sorted.length === 0 && (
                    <div className="p-8 text-center text-white/50 text-sm">
                        No users yet. Invite your first team member.
                    </div>
                )}
                {!loading &&
                    sorted.map((u) => {
                        const self = u.id === admin?.id;
                        const dimmed = u.status === "disabled";
                        return (
                            <div
                                key={u.id}
                                className={`grid grid-cols-[2fr_2fr_1fr_1fr_1.2fr_1.6fr] gap-3 px-5 py-4 border-b border-white/5 items-center text-sm ${dimmed ? "opacity-50" : ""}`}
                                data-testid={`user-row-${u.id}`}
                            >
                                <div className="truncate">
                                    {u.name || "—"}
                                    {self && (
                                        <span className="ml-2 text-[10px] tg-mono text-white/40">
                                            (you)
                                        </span>
                                    )}
                                </div>
                                <div className="truncate tg-mono text-xs text-white/70">
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
                                            className={`h-8 text-xs ${ROLE_BADGE[u.role] || ""}`}
                                            data-testid={`role-select-${u.id}`}
                                        >
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="team">Team</SelectItem>
                                            <SelectItem value="admin">Admin</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>
                                <div>
                                    <span
                                        className={`inline-flex px-2 py-0.5 rounded-sm text-[10px] tracking-widest uppercase ${STATUS_BADGE[u.status] || ""}`}
                                        data-testid={`user-status-${u.id}`}
                                    >
                                        {u.status}
                                    </span>
                                </div>
                                <div className="text-xs text-white/60 tg-mono truncate">
                                    {formatDate(u.last_login)}
                                </div>
                                <div className="flex items-center gap-1.5 justify-end">
                                    <button
                                        disabled={self || busyId === u.id}
                                        onClick={() => toggleDisable(u)}
                                        title={
                                            u.status === "disabled"
                                                ? "Enable user"
                                                : "Disable user"
                                        }
                                        data-testid={`disable-btn-${u.id}`}
                                        className="p-2 border border-white/15 hover:border-white/40 rounded-sm disabled:opacity-30"
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
                                        className="p-2 border border-white/15 hover:border-white/40 rounded-sm disabled:opacity-30"
                                    >
                                        <KeyRound className="w-3.5 h-3.5" />
                                    </button>
                                    <button
                                        disabled={self || busyId === u.id}
                                        onClick={() => remove(u)}
                                        title="Delete user"
                                        data-testid={`delete-btn-${u.id}`}
                                        className="p-2 border border-white/15 hover:border-[var(--tg-danger)] hover:text-[var(--tg-danger)] rounded-sm disabled:opacity-30"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            </div>
                        );
                    })}
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
