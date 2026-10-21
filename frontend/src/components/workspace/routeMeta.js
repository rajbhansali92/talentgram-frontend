import { matchPath } from "react-router-dom";
import { NAV_ITEMS } from "@/components/workspace/navConfig";

/**
 * Every page of the admin app, with the title its tab shows. Light on purpose (no page imports) so the tab bar, the launcher and the
 * persistence code can use it without loading the pages. `adminRoutes.jsx` pairs each entry with its page element, and a test keeps the
 * two in step. `adminOnly` is derived from NAV_ITEMS.adminOnly — the same list that hides the sidebar entries — so the route guard and the
 * menus can never disagree about what an admin-only page is.
 */
export const ROUTE_META = [
  { path: "", title: "Overview" },
  { path: "simple-assistant", title: "Assistant" },
  { path: "casting-desk", title: "AI Casting Desk" },
  { path: "casting-desk/:sessionId", title: "AI Casting Desk" },
  { path: "talents", title: "Talents" },
  { path: "talents/new", title: "New Talent" },
  { path: "talents/:id", title: "Talent" },
  { path: "applications", title: "Applications" },
  { path: "projects", title: "Projects" },
  { path: "projects/new", title: "New Project" },
  { path: "projects/:id", title: "Project" },
  { path: "projects/:id/submissions", title: "Submission Review" },
  { path: "production", title: "Production Desk" },
  { path: "links", title: "Links" },
  { path: "links/new", title: "Generate Link" },
  { path: "links/:id/results", title: "Link Results" },
  { path: "links/:id/edit", title: "Edit Link" },
  { path: "users", title: "Users" },
  { path: "feedback", title: "Feedback" },
  { path: "marketing", title: "Marketing" },
  { path: "notifications", title: "Notifications" },
  { path: "workflow", title: "Workflow" },
  { path: "storage", title: "Storage" },
  { path: "whatsapp", title: "WhatsApp" },
  { path: "diagnostics", title: "Diagnostics" },
  { path: "imports", title: "Data Center" },
].map((m) => {
  const abs = m.path ? `/admin/${m.path}` : "/admin";
  const adminOnly = NAV_ITEMS.adminOnly.some((i) => abs === i.to || abs.startsWith(`${i.to}/`));
  return { ...m, abs, adminOnly };
});

/** The route entry a pathname belongs to (most specific first), or null if it is not an admin page. */
export function matchRoute(pathname) {
  const p = normalizePathname(pathname);
  const sorted = [...ROUTE_META].sort((a, b) => b.abs.length - a.abs.length);
  return sorted.find((m) => matchPath({ path: m.abs, end: true }, p)) || null;
}

export function normalizePathname(p) {
  const s = String(p || "/").replace(/\/+$/, "");
  return s || "/";
}

/** May this role open this path? Unknown paths are never permitted (they would only redirect). */
export function isPermittedPath(pathname, role) {
  const m = matchRoute(pathname);
  if (!m) return false;
  return !m.adminOnly || role === "admin";
}

export function tabTitleFor(pathname) {
  return matchRoute(pathname)?.title || "Page";
}
