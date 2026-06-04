import React, { useState, useEffect, useCallback, Suspense, lazy } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { clearAdminSession, getAdmin } from "@/lib/api";
import Logo from "@/components/Logo";
import NotificationBell from "@/components/NotificationBell";
import {
  LayoutDashboard,
  Users,
  Link2,
  LogOut,
  Clapperboard,
  UserPlus,
  Shield,
  KeyRound,
  MessageSquare,
  Briefcase,
  Menu,
  ChevronLeft,
  X,
  AlertCircle,
  ListTodo,
} from "lucide-react";

// ============================================================================
// LAZY LOADED MODAL (ISSUE 4 FIX)
// ============================================================================
const ChangePasswordModal = lazy(() => import("@/components/ChangePasswordModal"));

// ============================================================================
// HELPER: cn() for conditional classes
// ============================================================================
const cn = (...classes) => classes.filter(Boolean).join(" ");

// ============================================================================
// ERROR BOUNDARY (Can be moved later - ISSUE 5)
// ============================================================================
class LayoutErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error("Layout error caught:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback || (
          <div className="min-h-screen flex items-center justify-center bg-[#f3f3f1] p-6">
            <div className="text-center space-y-4">
              <div className="w-16 h-16 rounded-full bg-red-50 flex items-center justify-center mx-auto">
                <AlertCircle className="w-8 h-8 text-red-500" />
              </div>
              <h2 className="text-xl font-medium text-black">Something went wrong</h2>
              <p className="text-black/60 text-sm max-w-md">
                The application encountered an unexpected error. Please try refreshing the page.
              </p>
              <button
                onClick={() => window.location.reload()}
                className="px-4 py-2 bg-black text-white rounded-sm text-sm hover:bg-black/90 transition-colors"
              >
                Refresh Page
              </button>
            </div>
          </div>
        )
      );
    }
    return this.props.children;
  }
}

// ============================================================================
// CONFIGURATION - Centralized navigation
// ============================================================================
export const NAV_ITEMS = {
  base: [
    { to: "/admin", label: "Overview", icon: LayoutDashboard, end: true },
    { to: "/admin/talents", label: "Talents", icon: Users },
    { to: "/admin/applications", label: "Applications", icon: UserPlus },
    { to: "/admin/projects", label: "Projects", icon: Clapperboard },
    { to: "/admin/links", label: "Links", icon: Link2 },
    { to: "/admin/marketing", label: "Marketing", icon: Briefcase },
    { to: "/admin/feedback", label: "Feedback", icon: MessageSquare },
    { to: "/admin/workflow", label: "Workflow", icon: ListTodo },
  ],
  adminOnly: [
    { to: "/admin/users", label: "Users", icon: Shield, adminOnly: true },
  ],
};

// Storage key for sidebar state
const SIDEBAR_STORAGE_KEY = "admin-sidebar-collapsed";

// ============================================================================
// CUSTOM HOOK: useSidebarState
// ============================================================================
const useSidebarState = () => {
  const [isCollapsed, setIsCollapsed] = useState(() => {
    if (typeof window !== "undefined") {
      const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY);
      return stored === "true";
    }
    return false;
  });

  const toggleSidebar = useCallback(() => {
    setIsCollapsed((prev) => {
      const newState = !prev;
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(newState));
      return newState;
    });
  }, []);

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.altKey && e.key === "s") {
        e.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleSidebar]);

  return { isCollapsed, toggleSidebar };
};

// ============================================================================
// CUSTOM HOOK: useMobileDrawer
// ============================================================================
const useMobileDrawer = () => {
  const [isOpen, setIsOpen] = useState(false);

  const openDrawer = useCallback(() => setIsOpen(true), []);
  const closeDrawer = useCallback(() => setIsOpen(false), []);

  useEffect(() => {
    if (!isOpen) return;
    const handleEscape = (e) => {
      if (e.key === "Escape") closeDrawer();
    };
    document.addEventListener("keydown", handleEscape);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", handleEscape);
      document.body.style.overflow = "";
    };
  }, [isOpen, closeDrawer]);

  return { isOpen, openDrawer, closeDrawer };
};

// ============================================================================
// COMPONENT: NavItem (Refined active state - ISSUE 7)
// ============================================================================
const NavItem = React.memo(({ to, label, icon: Icon, end, isCollapsed, onClick }) => {
  const [showTooltip, setShowTooltip] = useState(false);
  const timeoutRef = React.useRef(null);

  const handleMouseEnter = () => {
    if (!isCollapsed) return;
    timeoutRef.current = setTimeout(() => setShowTooltip(true), 300);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    setShowTooltip(false);
  };

  const baseClasses = "group relative flex items-center gap-3 rounded-sm text-sm transition-colors duration-200 ease-out focus:outline-none focus:ring-2 focus:ring-black/20 focus:ring-offset-1";
  const collapsedClasses = isCollapsed ? "justify-center w-12 mx-auto px-0 py-3" : "px-4 py-2.5 w-full";
  
  // Refined active state with subtle depth (ISSUE 7 FIX)
  const activeClasses = "bg-black text-white shadow-sm border border-white/10";
  const inactiveClasses = "text-black/50 hover:text-black hover:bg-black/[0.03]";

  return (
    <div className="relative" onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}>
      <NavLink
        to={to}
        end={end}
        onClick={onClick}
        aria-label={isCollapsed ? label : undefined}
        className={({ isActive }) =>
          cn(baseClasses, collapsedClasses, isActive ? activeClasses : inactiveClasses)
        }
      >
        <Icon
          className={cn(
            "w-4.5 h-4.5 shrink-0 transition-transform duration-200",
            isCollapsed ? "mx-auto" : "",
            "group-hover:scale-105"
          )}
          strokeWidth={1.5}
        />
        {!isCollapsed && <span className="truncate font-medium tracking-wide">{label}</span>}
      </NavLink>
      {isCollapsed && showTooltip && (
        <div
          className="absolute left-full ml-3 px-2.5 py-1.5 bg-[#1a1a1a] text-white text-[11px] font-medium tracking-wide rounded-md shadow-lg whitespace-nowrap z-50 border border-white/10"
          role="tooltip"
        >
          {label}
          <div className="absolute left-0 top-1/2 -translate-x-1 -translate-y-1/2 w-1 h-1 bg-[#1a1a1a] rotate-45 border-l border-t border-white/10" />
        </div>
      )}
    </div>
  );
});

NavItem.displayName = "NavItem";

// ============================================================================
// COMPONENT: SidebarHeader
// ============================================================================
const SidebarHeader = React.memo(({ isCollapsed, onToggle }) => {
  return (
    <div className="relative px-5 py-[22px] border-b border-black/[0.06] bg-white">
      <div className={cn("flex items-center justify-between gap-3", isCollapsed ? "flex-col" : "")}>
        {!isCollapsed && <Logo size="md" />}
        {isCollapsed && (
          <div className="mx-auto">
            <Logo size="sm" />
          </div>
        )}
        <div className={cn("flex items-center gap-2", isCollapsed ? "flex flex-col items-center gap-2 mx-auto mt-3" : "")}>
          <NotificationBell />
          <button
            onClick={onToggle}
            className="p-1.5 rounded-sm text-black/40 hover:text-black/70 hover:bg-black/[0.03] transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-black/20"
            aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={`${isCollapsed ? "Expand" : "Collapse"} sidebar (Alt+S)`}
          >
            <ChevronLeft
              className={cn("w-4 h-4 transition-transform duration-300", isCollapsed ? "rotate-180" : "")}
              strokeWidth={1.5}
            />
          </button>
        </div>
      </div>
      {!isCollapsed && (
        <p className="text-[10px] tracking-[0.2em] uppercase mt-4 text-center text-black/40 font-medium">
          Portfolio Engine
        </p>
      )}
    </div>
  );
});

SidebarHeader.displayName = "SidebarHeader";

// ============================================================================
// COMPONENT: SidebarFooter
// ============================================================================
const SidebarFooter = React.memo(({
  isCollapsed,
  admin,
  role,
  isAdminRole,
  onPasswordChange,
  onLogout,
}) => {
  return (
    <div className={cn("p-3 border-t border-black/[0.06] bg-white/50", isCollapsed ? "text-center" : "")}>
      {!isCollapsed && (
        <div className="mb-3 px-1">
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-medium text-black/80 truncate">
                  {admin?.name || "Admin"}
                </span>
                <span
                  className={cn(
                    "text-[9px] tracking-wider uppercase px-1.5 py-0.5 rounded-sm font-medium",
                    isAdminRole
                      ? "bg-[#c9a961]/10 text-[#9b7b35] border border-[#c9a961]/25"
                      : "bg-black/[0.03] text-black/50 border border-black/[0.06]"
                  )}
                  data-testid="role-badge"
                >
                  {role}
                </span>
              </div>
              <div className="text-[10px] text-black/40 truncate font-mono">{admin?.email}</div>
            </div>
          </div>
        </div>
      )}

      <div className="space-y-1">
        <button
          data-testid="admin-change-password-btn"
          onClick={onPasswordChange}
          className={cn(
            "w-full flex items-center gap-2 px-3 py-2 rounded-sm text-xs font-medium text-black/60 hover:text-black hover:bg-black/[0.04] transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-black/20 group",
            isCollapsed ? "justify-center" : ""
          )}
          aria-label={isCollapsed ? "Change password" : undefined}
          title={isCollapsed ? "Change password" : undefined}
        >
          <KeyRound className="w-3.5 h-3.5 shrink-0 transition-transform duration-200 group-hover:scale-105" strokeWidth={1.5} />
          {!isCollapsed && <span>Change password</span>}
        </button>
        <button
          data-testid="admin-logout-btn"
          onClick={onLogout}
          className={cn(
            "w-full flex items-center gap-2 px-3 py-2 rounded-sm text-xs font-medium text-black/60 hover:text-red-600 hover:bg-red-50/50 transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-red-200/50 group",
            isCollapsed ? "justify-center" : ""
          )}
          aria-label={isCollapsed ? "Sign out" : undefined}
          title={isCollapsed ? "Sign out" : undefined}
        >
          <LogOut className="w-3.5 h-3.5 shrink-0 transition-transform duration-200 group-hover:scale-105" strokeWidth={1.5} />
          {!isCollapsed && <span>Sign out</span>}
        </button>
      </div>
    </div>
  );
});

SidebarFooter.displayName = "SidebarFooter";

// ============================================================================
// COMPONENT: MobileDrawer
// ============================================================================
const MobileDrawer = React.memo(({
  isOpen,
  onClose,
  navItems,
  onNavigate,
  onPasswordChange,
  onLogout,
  admin,
  role,
  isAdminRole,
}) => {
  if (!isOpen) return null;

  return (
    <>
      <div
        className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 animate-in fade-in duration-200"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className="fixed inset-y-0 left-0 w-80 bg-white shadow-2xl z-50 flex flex-col animate-in slide-in-from-left duration-300 ease-out"
        role="dialog"
        aria-modal="true"
        aria-label="Navigation menu"
      >
        <div className="flex items-center justify-between px-6 py-5 border-b border-black/[0.06] bg-white">
          <Logo size="md" />
          <button
            onClick={onClose}
            className="p-2 rounded-sm text-black/50 hover:text-black hover:bg-black/[0.04] transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-black/20"
            aria-label="Close menu"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 py-4 border-b border-black/[0.06]">
          <div className="flex items-center justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="text-sm font-medium text-black truncate">{admin?.name || "Admin"}</span>
                <span
                  className={cn(
                    "text-[9px] tracking-wider uppercase px-1.5 py-0.5 rounded-sm font-medium",
                    isAdminRole
                      ? "bg-[#c9a961]/10 text-[#9b7b35] border border-[#c9a961]/25"
                      : "bg-black/[0.03] text-black/50 border border-black/[0.06]"
                  )}
                >
                  {role}
                </span>
              </div>
              <div className="text-[11px] text-black/40 truncate font-mono">{admin?.email}</div>
            </div>
            <NotificationBell />
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto py-6 px-4 space-y-1" aria-label="Mobile navigation">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 px-4 py-3 rounded-sm text-sm transition-colors duration-200",
                  isActive
                    ? "bg-black text-white shadow-sm"
                    : "text-black/60 hover:text-black hover:bg-black/[0.04]"
                )
              }
            >
              <item.icon className="w-4 h-4" strokeWidth={1.5} />
              <span className="font-medium">{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="p-4 border-t border-black/[0.06] space-y-2">
          <button
            onClick={() => {
              onClose();
              onPasswordChange();
            }}
            className="w-full flex items-center gap-3 px-4 py-2.5 rounded-sm text-sm font-medium text-black/60 hover:text-black hover:bg-black/[0.04] transition-colors duration-200 group"
          >
            <KeyRound className="w-4 h-4 transition-transform duration-200 group-hover:scale-105" strokeWidth={1.5} />
            Change password
          </button>
          <button
            onClick={() => {
              onClose();
              onLogout();
            }}
            className="w-full flex items-center gap-3 px-4 py-2.5 rounded-sm text-sm font-medium text-black/60 hover:text-red-600 hover:bg-red-50/50 transition-colors duration-200 group"
          >
            <LogOut className="w-4 h-4 transition-transform duration-200 group-hover:scale-105" strokeWidth={1.5} />
            Sign out
          </button>
        </div>
      </div>
    </>
  );
});

MobileDrawer.displayName = "MobileDrawer";

// ============================================================================
// COMPONENT: LoadingFallback
// ============================================================================
const LoadingFallback = () => (
  <div className="flex items-center justify-center py-12">
    <div className="w-8 h-8 border-2 border-black/20 border-t-black rounded-full animate-spin" />
  </div>
);

// ============================================================================
// MAIN COMPONENT: AdminLayout (Production Ready)
// ============================================================================
export default function AdminLayout() {
  const navigate = useNavigate();
  const admin = getAdmin();
  const role = admin?.role || "team";
  const isAdminRole = role === "admin";

  const { isCollapsed, toggleSidebar } = useSidebarState();
  const { isOpen: isDrawerOpen, openDrawer, closeDrawer } = useMobileDrawer();
  const [pwOpen, setPwOpen] = useState(false);

  useEffect(() => {
    const handleOpenModal = () => setPwOpen(true);
    window.addEventListener("open-change-password-modal", handleOpenModal);
    return () => window.removeEventListener("open-change-password-modal", handleOpenModal);
  }, []);

  // Static nav items - no useMemo needed (ISSUE 6)
  const navItems = isAdminRole ? [...NAV_ITEMS.base, ...NAV_ITEMS.adminOnly] : NAV_ITEMS.base;

  const logout = useCallback(() => {
    clearAdminSession();
    navigate("/admin/login");
  }, [navigate]);

  const sidebarWidthClass = isCollapsed ? "w-[72px]" : "w-64";

  return (
    <LayoutErrorBoundary>
      <div className="min-h-screen flex bg-gradient-to-br from-[#f8f8f6] via-[#f3f3f1] to-[#efefed] text-black">
        {/* DESKTOP SIDEBAR */}
        <aside
          className={cn(
            "hidden md:flex shrink-0 flex-col bg-white border-r border-black/[0.06] sticky top-0 h-screen transition-[width] duration-300 ease-out",
            sidebarWidthClass
          )}
          data-testid="admin-sidebar"
          aria-label="Main navigation sidebar"
        >
          <SidebarHeader
            isCollapsed={isCollapsed}
            onToggle={toggleSidebar}
          />

          <nav className="flex-1 py-4 px-3 space-y-1" aria-label="Main menu">
            {navItems.map((item) => (
              <NavItem
                key={item.to}
                to={item.to}
                label={item.label}
                icon={item.icon}
                end={item.end}
                isCollapsed={isCollapsed}
              />
            ))}
          </nav>

          <SidebarFooter
            isCollapsed={isCollapsed}
            admin={admin}
            role={role}
            isAdminRole={isAdminRole}
            onPasswordChange={() => setPwOpen(true)}
            onLogout={logout}
          />
        </aside>

        <div className="md:hidden fixed top-0 left-0 right-0 z-40 border-b border-black/[0.06] bg-white pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={openDrawer}
              className="p-1.5 rounded-sm text-black/50 hover:text-black hover:bg-black/[0.04] transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-black/20"
              aria-label="Open navigation menu"
            >
              <Menu className="w-5 h-5" />
            </button>
            <Logo size={30} />
          </div>
          <div className="flex items-center gap-3">
            <NotificationBell />
          </div>
        </div>

        {/* Mobile Drawer */}
        <MobileDrawer
          isOpen={isDrawerOpen}
          onClose={closeDrawer}
          navItems={navItems}
          onNavigate={closeDrawer}
          onPasswordChange={() => setPwOpen(true)}
          onLogout={logout}
          admin={admin}
          role={role}
          isAdminRole={isAdminRole}
        />

        {/* MAIN CONTENT */}
        <main className="flex-1 min-w-0 md:pt-0 pt-14 pb-[safe-area-inset-bottom] pb-safe">
          <Suspense fallback={<LoadingFallback />}>
            <Outlet />
          </Suspense>
        </main>

        {/* Lazy Loaded Modal */}
        <Suspense fallback={null}>
          <ChangePasswordModal open={pwOpen} onClose={() => setPwOpen(false)} />
        </Suspense>
      </div>
    </LayoutErrorBoundary>
  );
}
