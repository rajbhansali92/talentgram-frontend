import React from "react";
import { NavLink, Outlet } from "react-router-dom";
import { LogOut } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { PORTAL_TOKEN_KEY } from "@/lib/api";

/**
 * Permanent Talent Dashboard shell (Phase 2 item 1 — see
 * docs/TALENT_DASHBOARD_ARCHITECTURE.md). Modeled on AdminLayout.jsx's
 * NavLink/Outlet pattern. Stays mounted across nav changes — only the
 * routed page content inside <Outlet/> changes.
 *
 * Business logic (auth guards, data fetching, sign-out semantics) is
 * unchanged from what previously lived inline in PortalHome.jsx/
 * PortalProfile.jsx — this only centralizes the header/nav chrome that was
 * duplicated across those pages.
 */
const NAV_ITEMS = [
    { to: "/portal/home", label: "Dashboard" },
    { to: "/portal/projects", label: "Projects" },
    { to: "/portal/profile", label: "Profile" },
    { to: "/portal/settings", label: "Settings" },
];

function navLinkClass({ isActive }) {
    return `px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-colors duration-150 ${
        isActive ? "bg-black text-white" : "text-black/60 hover:text-black hover:bg-black/5"
    }`;
}

export default function DashboardLayout() {
    // Identical semantics to the sign-out previously duplicated in
    // PortalHome.jsx — moved here since it's shell-level chrome, not
    // page-specific business logic.
    //
    // P1 fix: "/" is outside this app's react-router <Routes> tree
    // (PortalApp.jsx only declares /portal/*) — react-router's navigate()
    // can't leave that tree, so navigate("/") just changed the URL to
    // something PortalApp's own wildcard route immediately caught and
    // redirected back from (see PortalHome.jsx's matching fix for the
    // full explanation). A hard navigation is the correct way to cross
    // that boundary.
    const handleSignOut = () => {
        localStorage.removeItem(PORTAL_TOKEN_KEY);
        localStorage.removeItem("talentgram_portal_email");
        toast.success("Signed out successfully");
        window.location.href = "/";
    };

    return (
        <div className="min-h-dvh bg-[#fafafa] text-black flex flex-col" data-testid="dashboard-layout">
            <header className="bg-white border-b border-black/5 px-6 md:px-12 py-4 flex items-center justify-between gap-4">
                <NavLink to="/portal/home" className="flex items-center gap-2 shrink-0">
                    <Logo size={64} forceVariant="black" />
                    <span className="hidden sm:inline text-[10px] tracking-[0.12em] uppercase text-black/40 font-medium">
                        Talent Dashboard
                    </span>
                </NavLink>

                <nav className="hidden md:flex items-center gap-1">
                    {NAV_ITEMS.map((item) => (
                        <NavLink key={item.to} to={item.to} className={navLinkClass}>
                            {item.label}
                        </NavLink>
                    ))}
                </nav>

                <button
                    onClick={handleSignOut}
                    className="inline-flex items-center gap-1.5 text-xs text-black/60 hover:text-black transition-colors duration-150 px-3 py-1.5 border border-black/10 rounded-lg hover:border-black/30 shrink-0"
                >
                    <LogOut className="w-3.5 h-3.5" />
                    <span className="hidden sm:inline">Sign Out</span>
                </button>
            </header>

            {/* Mobile nav row — same NAV_ITEMS, horizontal scroll instead of
                the desktop header's inline row. */}
            <nav className="md:hidden bg-white border-b border-black/5 px-4 py-2 flex items-center gap-1 overflow-x-auto">
                {NAV_ITEMS.map((item) => (
                    <NavLink key={item.to} to={item.to} className={navLinkClass}>
                        {item.label}
                    </NavLink>
                ))}
            </nav>

            <main className="flex-1 flex flex-col">
                <Outlet />
            </main>
        </div>
    );
}
