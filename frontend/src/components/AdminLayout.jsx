import React, { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { clearAdminSession, getAdmin } from "@/lib/api";
import Logo from "@/components/Logo";
import ChangePasswordModal from "@/components/ChangePasswordModal";
import NotificationBell from "@/components/NotificationBell";
import { LayoutDashboard, Users, Link2, LogOut, Clapperboard, UserPlus, Shield, KeyRound, MessageSquare, Briefcase } from "lucide-react";

const baseNav = [
    { to: "/admin", label: "Overview", icon: LayoutDashboard, end: true },
    { to: "/admin/talents", label: "Talents", icon: Users },
    { to: "/admin/applications", label: "Applications", icon: UserPlus },
    { to: "/admin/projects", label: "Projects", icon: Clapperboard },
    { to: "/admin/links", label: "Links", icon: Link2 },
    { to: "/admin/marketing", label: "Marketing", icon: Briefcase },
    { to: "/admin/feedback", label: "Feedback", icon: MessageSquare },
];

const adminOnlyNav = [
    { to: "/admin/users", label: "Users", icon: Shield, adminOnly: true },
];

export default function AdminLayout() {
    const nav = useNavigate();
    const admin = getAdmin();
    const role = admin?.role || "team";
    const isAdminRole = role === "admin";
    const navItems = isAdminRole ? [...baseNav, ...adminOnlyNav] : baseNav;
    const [pwOpen, setPwOpen] = useState(false);

    const logout = () => {
        clearAdminSession();
        nav("/admin/login");
    };

    return (
        <div className="min-h-screen flex bg-[#f3f3f1] text-black">
            {/* Sidebar */}
            <aside
                className="hidden md:flex w-64 shrink-0 flex-col bg-white border-r border-black/[0.08] sticky top-0 h-screen"
                data-testid="admin-sidebar"
            >
                <div className="px-6 py-7 border-b border-black/[0.06] bg-white">
                    <div className="flex items-start justify-between gap-2">
                        <Logo size="md" />
                        <NotificationBell />
                    </div>
                    <p className="eyebrow mt-4 text-center text-black/70">Portfolio Engine</p>
                </div>
                <nav className="flex-1 p-3 space-y-1">
                    {navItems.map((it) => (
                        <NavLink
                            key={it.to}
                            to={it.to}
                            end={it.end}
                            data-testid={`nav-${it.label.toLowerCase()}`}
                            className={({ isActive }) =>
                                `group flex items-center gap-3 px-4 py-3 rounded-sm text-sm transition-colors duration-150 ${
                                    isActive
                                        ? "bg-black text-white"
                                        : "text-black/60 hover:text-black hover:bg-black/[0.04]"
                                }`
                            }
                        >
                            <it.icon className="w-4 h-4" strokeWidth={1.5} />
                            <span>{it.label}</span>
                        </NavLink>
                    ))}
                </nav>
                <div className="p-4 border-t border-black/[0.06]">
                    <div className="flex items-center justify-between mb-3">
                        <div className="min-w-0">
                            <div className="flex items-center gap-1.5">
                                <span className="text-xs text-black/70 truncate">
                                    {admin?.name || "Admin"}
                                </span>
                                <span
                                    className={`text-[9px] tracking-widest uppercase px-1.5 py-0.5 rounded-sm ${isAdminRole ? "bg-[#c9a961]/10 text-[#9b7b35] border border-[#c9a961]/25" : "bg-black/[0.03] text-black/50 border border-black/[0.06]"}`}
                                    data-testid="role-badge"
                                >
                                    {role}
                                </span>
                            </div>
                            <div className="text-[11px] text-black/45 truncate">
                                {admin?.email}
                            </div>
                        </div>
                    </div>
                    <button
                        data-testid="admin-change-password-btn"
                        onClick={() => setPwOpen(true)}
                        className="w-full flex items-center gap-2 px-3 py-2 rounded-sm text-xs text-black/70 hover:text-black hover:bg-black/[0.04] transition-colors duration-150 mb-1"
                    >
                        <KeyRound className="w-3.5 h-3.5" strokeWidth={1.5} />
                        Change password
                    </button>
                    <button
                        data-testid="admin-logout-btn"
                        onClick={logout}
                        className="w-full flex items-center gap-2 px-3 py-2 rounded-sm text-xs text-black/70 hover:text-black hover:bg-black/[0.04] transition-colors duration-150"
                    >
                        <LogOut className="w-3.5 h-3.5" strokeWidth={1.5} />
                        Sign out
                    </button>
                </div>
            </aside>

            {/* Mobile top bar */}
            <div className="md:hidden fixed top-0 left-0 right-0 z-40 border-b border-black/[0.06] bg-white/95 backdrop-blur-sm px-4 py-3 flex items-center justify-between">
                <Logo size="sm" />
                <div className="flex items-center gap-2">
                    <NotificationBell />
                    <button
                        onClick={() => setPwOpen(true)}
                        data-testid="admin-change-password-mobile-btn"
                        className="text-[11px] text-black/60 inline-flex items-center gap-1 hover:text-black transition-colors duration-150"
                    >
                        <KeyRound className="w-3 h-3" />
                        Password
                    </button>
                    <button
                        onClick={logout}
                        data-testid="admin-logout-mobile-btn"
                        className="text-xs text-black/60 hover:text-black transition-colors duration-150"
                    >
                        Sign out
                    </button>
                </div>
            </div>

            <main className="flex-1 min-w-0 md:pt-0 pt-14">
                <div
                    className="md:hidden flex gap-0 border-b border-black/[0.06] bg-white overflow-x-auto whitespace-nowrap"
                    style={{ scrollbarWidth: "none" }}
                    data-testid="mobile-nav-strip"
                >
                    {navItems.map((it) => (
                        <NavLink
                            key={it.to}
                            to={it.to}
                            end={it.end}
                            className={({ isActive }) =>
                                `inline-flex items-center justify-center px-5 py-3 text-xs transition-colors duration-150 min-h-[44px] shrink-0 ${
                                    isActive
                                        ? "text-black border-b-2 border-black"
                                        : "text-black/50 border-b-2 border-transparent hover:text-black/75"
                                }`
                            }
                        >
                            {it.label}
                        </NavLink>
                    ))}
                </div>
                <Outlet />
            </main>
            <ChangePasswordModal open={pwOpen} onClose={() => setPwOpen(false)} />
        </div>
    );
}
