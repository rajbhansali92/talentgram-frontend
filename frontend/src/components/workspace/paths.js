import { NAV_ITEMS } from "@/components/workspace/navConfig";
import { isPermittedPath, normalizePathname, tabTitleFor } from "@/components/workspace/routeMeta";

export const DEFAULT_PATH = "/admin";
export const STORAGE_KEY = "tg_workspace_v1";
export const MAX_LIVE_PANES = 12;          // pages kept alive at once; the rest keep their tab and re-open fresh (a page with unsaved changes is never evicted)

export const fullPath = (loc) => `${loc?.pathname || ""}${loc?.search || ""}${loc?.hash || ""}`;
export const samePath = (a, b) => normalizePathname(a) === normalizePathname(b);

/** "/admin/links/new?x=1#h" → { pathname, search, hash } */
export function parseTo(to) {
  if (to && typeof to === "object") return { pathname: to.pathname || "/", search: to.search || "", hash: to.hash || "" };
  try {
    const u = new URL(String(to || "/"), "http://workspace.local");
    return { pathname: u.pathname, search: u.search, hash: u.hash };
  } catch {
    return { pathname: DEFAULT_PATH, search: "", hash: "" };
  }
}

/**
 * Pages the "Go to" launcher offers this role — derived from the sidebar's NAV_ITEMS (the source of truth), plus the few real routes that
 * have no sidebar entry but need no parameter. Admin-only entries are never returned to a non-admin.
 */
const EXTRA = [
  { to: "/admin/links/new", label: "Generate Link", group: "Create", keywords: ["roster", "pdf", "link generator", "new link", "comp card"] },
  { to: "/admin/talents/new", label: "New Talent", group: "Create", keywords: ["add talent", "create talent"] },
  { to: "/admin/projects/new", label: "New Project", group: "Create", keywords: ["add project", "create project"] },
  { to: "/admin/notifications", label: "Notifications", group: "More", keywords: ["alerts", "inbox"] },
];
const KEYWORDS = { "/admin/talents": ["global talent", "talent directory", "roster"], "/admin/links": ["client links", "link history"], "/admin/projects": ["pipeline", "castings"],
  "/admin": ["dashboard", "home"], "/admin/imports": ["import", "export"], "/admin/whatsapp": ["whatsapp engine", "workers", "broadcast"] };

export function destinationsFor(role) {
  const out = [];
  NAV_ITEMS.base.forEach((i) => out.push({ to: i.to, label: i.label, group: "Pages", keywords: KEYWORDS[i.to] || [], icon: i.icon }));
  if (role === "admin") NAV_ITEMS.adminOnly.forEach((i) => out.push({ to: i.to, label: i.label, group: "Admin", keywords: KEYWORDS[i.to] || [], icon: i.icon }));
  EXTRA.forEach((e) => out.push(e));
  return out.filter((d) => isPermittedPath(d.to, role));
}

export function filterDestinations(list, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return list;
  const terms = q.split(/\s+/);
  return list.filter((d) => {
    const hay = `${d.label} ${d.group} ${(d.keywords || []).join(" ")}`.toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}

// ------------------------------------------------------------------------------------------------ persistence (routes only)
/** Only the list of open pages and which one is active: route + a generic title. Never page data, never anything private. */
export function readSaved(uid) {
  try {
    const raw = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null");
    if (!raw || raw.uid !== uid || !Array.isArray(raw.tabs)) return null;
    return raw;
  } catch { return null; }
}
export function writeSaved(uid, tabs, activeId) {
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ uid, activeId, tabs: tabs.map((t) => ({ id: t.id, path: `${t.loc.pathname}${t.loc.search}${t.loc.hash}`, title: tabTitleFor(t.loc.pathname) })) })); } catch { /* storage blocked */ }
}
export function clearSaved() {
  try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* storage blocked */ }
}
