import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { clearAdminSession, getAdmin } from "@/lib/api";
import ThemeToggle from "@/components/ThemeToggle";
import Logo from "@/components/Logo";
import { LayoutDashboard, Users, Link2, LogOut } from "lucide-react";

const navItems = [
    { to: "/admin", label: "Overview", icon: LayoutDashboard, end: true },
    { to: "/admin/talents", label: "Talents", icon: Users },
    { to: "/admin/links", label: "Links", icon: Link2 },
];

export default function AdminLayout() {
    const nav = useNavigate();
    const admin = getAdmin();

    const logout = () => {
        clearAdminSession();
        nav("/admin/login");
    };

    return (
        <div className="min-h-screen flex bg-[#050505] text-[var(--tg-text)]">
            {/* Sidebar */}
            <aside
                className="hidden md:flex w-64 shrink-0 flex-col border-r border-white/10 sticky top-0 h-screen"
                data-testid="admin-sidebar"
            >
                <div className="px-6 py-7 border-b border-white/10">
                    <Logo size="md" />
                    <p className="eyebrow mt-4 text-center">Portfolio Engine</p>
                </div>
                <nav className="flex-1 p-3 space-y-1">
                    {navItems.map((it) => (
                        <NavLink
                            key={it.to}
                            to={it.to}
                            end={it.end}
                            data-testid={`nav-${it.label.toLowerCase()}`}
                            className={({ isActive }) =>
                                `group flex items-center gap-3 px-4 py-3 rounded-sm text-sm transition-all duration-300 ${
                                    isActive
                                        ? "bg-white text-black"
                                        : "text-white/70 hover:text-white hover:bg-white/5"
                                }`
                            }
                        >
                            <it.icon className="w-4 h-4" strokeWidth={1.5} />
                            <span>{it.label}</span>
                        </NavLink>
                    ))}
                </nav>
                <div className="p-4 border-t border-white/10">
                    <div className="flex items-center justify-between mb-3">
                        <div className="min-w-0">
                            <div className="text-xs text-white/50 truncate">
                                {admin?.name || "Admin"}
                            </div>
                            <div className="text-[11px] text-white/40 truncate">
                                {admin?.email}
                            </div>
                        </div>
                        <ThemeToggle size="sm" />
                    </div>
                    <button
                        data-testid="admin-logout-btn"
                        onClick={logout}
                        className="w-full flex items-center gap-2 px-3 py-2 rounded-sm text-xs text-white/70 hover:text-white hover:bg-white/5 transition-all"
                    >
                        <LogOut className="w-3.5 h-3.5" strokeWidth={1.5} />
                        Sign out
                    </button>
                </div>
            </aside>

            {/* Mobile top bar */}
            <div className="md:hidden fixed top-0 left-0 right-0 z-40 border-b border-white/10 bg-black/80 backdrop-blur-xl px-4 py-3 flex items-center justify-between">
                <Logo size="sm" />
                <button
                    onClick={logout}
                    data-testid="admin-logout-mobile-btn"
                    className="text-xs text-white/60"
                >
                    Sign out
                </button>
                <ThemeToggle size="sm" />
            </div>

            <main className="flex-1 min-w-0 md:pt-0 pt-14">
                <div className="md:hidden flex gap-1 border-b border-white/10 px-2 bg-[#080808]">
                    {navItems.map((it) => (
                        <NavLink
                            key={it.to}
                            to={it.to}
                            end={it.end}
                            className={({ isActive }) =>
                                `flex-1 text-center py-3 text-xs transition-all ${
                                    isActive
                                        ? "text-white border-b-2 border-white"
                                        : "text-white/50"
                                }`
                            }
                        >
                            {it.label}
                        </NavLink>
                    ))}
                </div>
                <Outlet />
            </main>
        </div>
    );
}
