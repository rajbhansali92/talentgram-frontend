import React, { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { DEFAULT_PATH, MAX_LIVE_PANES, fullPath, parseTo, readSaved, samePath, writeSaved } from "@/components/workspace/paths";
import { isPermittedPath, tabTitleFor } from "@/components/workspace/routeMeta";

/**
 * In-app workspace tabs.
 *
 * ONE router (the existing BrowserRouter) stays the source of truth for the ACTIVE tab's location, so links, the browser's back / forward,
 * deep links and each page's own unsaved-changes guards keep working untouched. This provider only keeps the list of open tabs, remembers
 * where each one is, and (see WorkspaceHost) keeps every opened page alive inside React's <Activity>: hidden pages keep their state and DOM
 * but their effects are paused, so a hidden page cannot answer keystrokes, poll, or feed its selection to the assistant.
 *
 * What is persisted (sessionStorage, this browser window only): the routes of the open tabs and which one is active. Nothing from the pages.
 */
const WorkspaceContext = createContext(null);
export const PaneContext = createContext(null);

export const useWorkspace = () => useContext(WorkspaceContext);
/** True when the calling page's tab is the visible one (always true outside the workspace). */
export const useTabActive = () => { const p = useContext(PaneContext); return p ? p.active : true; };
/** A page may name its tab ("MK Jewels - film 1"); the name applies only while the tab is on that route. */
export function useTabTitle(text) {
  const setTitle = useContext(PaneContext)?.setTitle;
  useEffect(() => { if (setTitle && text) setTitle(String(text)); }, [setTitle, text]);
}
/** A page with unsaved work reports it, so closing its tab (or the window) asks first. Cleared automatically when the tab leaves that route. */
export function useWorkspaceDirty(isDirty) {
  const setDirty = useContext(PaneContext)?.setDirty;
  useEffect(() => { if (setDirty) setDirty(!!isDirty); }, [setDirty, isDirty]);      // no cleanup on purpose: a hidden page is still dirty
}

const newId = () => `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
const toLoc = (l) => ({ pathname: l.pathname, search: l.search || "", hash: l.hash || "", state: l.state ?? null, key: l.key });
const makeTab = (loc) => ({ id: newId(), loc: toLoc(loc), titleOverride: null });
const withLoc = (s, id, loc) => ({ ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, loc: toLoc(loc) } : t)) });

function initialState(uid, role, location) {
  const saved = readSaved(uid);
  let tabs = [];
  if (saved) {
    const seen = new Set();
    tabs = saved.tabs
      .filter((t) => t && typeof t.path === "string" && typeof t.id === "string" && !seen.has(t.id) && seen.add(t.id) && isPermittedPath(parseTo(t.path).pathname, role))
      .map((t) => ({ id: t.id, loc: toLoc(parseTo(t.path)), titleOverride: null }));
  }
  // an entry URL this account may not open (an admin-only page, for a team member) never gets a tab of its own: the default page's tab is used
  // and the real URL is redirected to it (see the mount effect in WorkspaceProvider)
  const cur = isPermittedPath(location.pathname, role) ? toLoc(location) : toLoc({ pathname: DEFAULT_PATH, search: "", hash: "", state: null, key: location.key });
  const at = tabs.findIndex((t) => samePath(t.loc.pathname, cur.pathname));
  if (at >= 0) tabs[at] = { ...tabs[at], loc: cur }; else tabs.push(makeTab(cur));
  const active = at >= 0 ? tabs[at] : tabs[tabs.length - 1];
  return { tabs, activeId: active.id, live: [active.id] };
}

export function WorkspaceProvider({ admin, children }) {
  const location = useLocation();
  const navType = useNavigationType();
  const navigate = useNavigate();
  const role = admin?.role || "team";
  const uid = admin?.id || "anon";

  const [state, setStateRaw] = useState(() => initialState(uid, role, location));
  const stateRef = useRef(state);
  const setState = useCallback((next) => { if (next === stateRef.current) return; stateRef.current = next; setStateRaw(next); }, []);
  const owners = useRef(new Map());          // history entry key → the tab it belongs to (so Back / Forward return to the right tab, never close one)
  const dirty = useRef(new Map());           // tab id → { path, dirty }
  const scrollY = useRef(new Map());         // tab id → window scroll when it was left
  const [dirtyVersion, setDirtyVersion] = useState(0);

  const isDirtyTab = useCallback((id) => {
    const t = stateRef.current.tabs.find((x) => x.id === id);
    const d = dirty.current.get(id);
    return !!(t && d && d.dirty && d.path === t.loc.pathname);
  }, []);

  const touchLive = useCallback((live, id) => {
    const next = [id, ...live.filter((x) => x !== id)];
    while (next.length > MAX_LIVE_PANES) {                  // keep the most recently used pages alive; never evict one with unsaved work
      let i = next.length - 1;
      while (i > 0 && isDirtyTab(next[i])) i -= 1;
      if (i <= 0) break;
      next.splice(i, 1);
    }
    return next;
  }, [isDirtyTab]);

  useEffect(() => {                                                  // (an effect, not a layout effect: the Router above subscribes to history in ITS layout effect, which runs after ours)
    if (!isPermittedPath(location.pathname, role)) navigate(DEFAULT_PATH, { replace: true });
  }, []);                                                            // eslint-disable-line react-hooks/exhaustive-deps

  // ---- the real router moved (a link, the browser's Back / Forward, a deep link, or a switch this provider made) → follow it
  useLayoutEffect(() => {
    if (!isPermittedPath(location.pathname, role)) return;            // a denied URL is being redirected away: it never becomes a tab
    const s = stateRef.current;
    const loc = toLoc(location);
    const active = s.tabs.find((t) => t.id === s.activeId);
    if (active && fullPath(active.loc) === fullPath(loc)) {          // already there (our own navigation, or the first render)
      owners.current.set(location.key, active.id);
      if (active.loc.key !== loc.key || active.loc.state !== loc.state) setState(withLoc(s, active.id, loc));
      return;
    }
    if (navType === "POP") {
      const owner = owners.current.get(location.key);
      const target = s.tabs.find((t) => t.id === owner) || s.tabs.find((t) => samePath(t.loc.pathname, loc.pathname));
      if (target) {
        scrollY.current.set(s.activeId, window.scrollY);
        owners.current.set(location.key, target.id);
        setState({ ...withLoc(s, target.id, loc), activeId: target.id, live: touchLive(s.live, target.id) });
        return;
      }
      const tab = makeTab(loc);                                      // an entry whose tab is gone: its own tab, never over another tab's work
      owners.current.set(location.key, tab.id);
      setState({ tabs: [...s.tabs, tab], activeId: tab.id, live: touchLive(s.live, tab.id) });
      return;
    }
    if (active) {                                                    // navigation inside the page the user is on
      owners.current.set(location.key, active.id);
      setState(withLoc(s, active.id, loc));
    }
  }, [location.key, location.pathname, location.search, location.hash]);       // eslint-disable-line react-hooks/exhaustive-deps

  const go = useCallback((loc, replace = false) => navigate({ pathname: loc.pathname, search: loc.search, hash: loc.hash }, { replace, state: loc.state ?? undefined }), [navigate]);

  const activate = useCallback((id) => {
    const s = stateRef.current;
    const tab = s.tabs.find((t) => t.id === id);
    if (!tab || id === s.activeId) return;
    scrollY.current.set(s.activeId, window.scrollY);
    setState({ ...s, activeId: id, live: touchLive(s.live, id) });
    go(tab.loc);
  }, [go, setState, touchLive]);

  const open = useCallback((to) => {
    const target = parseTo(to);
    if (!isPermittedPath(target.pathname, role)) return false;
    const s = stateRef.current;
    const existing = s.tabs.find((t) => samePath(t.loc.pathname, target.pathname));
    if (existing) {
      const changed = target.search && target.search !== existing.loc.search;
      if (changed) setState(withLoc(stateRef.current, existing.id, { ...target, state: null, key: undefined }));
      if (existing.id === s.activeId) { if (changed) go(target); return true; }
      activate(existing.id);
      return true;
    }
    const tab = makeTab({ ...target, state: null });
    scrollY.current.set(s.activeId, window.scrollY);
    setState({ tabs: [...s.tabs, tab], activeId: tab.id, live: touchLive(s.live, tab.id) });
    go(tab.loc);
    return true;
  }, [activate, go, role, setState, touchLive]);

  const close = useCallback((id) => {
    const s = stateRef.current;
    const idx = s.tabs.findIndex((t) => t.id === id);
    if (idx < 0) return false;
    if (isDirtyTab(id) && !window.confirm("This page has unsaved changes. Close it and lose them?")) return false;
    const tabs = s.tabs.filter((t) => t.id !== id);
    dirty.current.delete(id); scrollY.current.delete(id);
    const live = s.live.filter((x) => x !== id);
    if (!tabs.length) {                                              // never leave the app blank
      const tab = makeTab({ pathname: DEFAULT_PATH, search: "", hash: "", state: null });
      setState({ tabs: [tab], activeId: tab.id, live: [tab.id] });
      go(tab.loc, true);
      return true;
    }
    if (id !== s.activeId) { setState({ ...s, tabs, live }); return true; }   // closing a background tab changes nothing on screen
    const next = tabs[idx] || tabs[idx - 1];                        // the tab to its right, else to its left
    setState({ tabs, activeId: next.id, live: touchLive(live, next.id) });
    go(next.loc, true);
    return true;
  }, [go, isDirtyTab, setState, touchLive]);

  const reportTitle = useCallback((id, path, text) => {
    const s = stateRef.current;
    const t = s.tabs.find((x) => x.id === id);
    if (!t || (t.titleOverride && t.titleOverride.path === path && t.titleOverride.text === text)) return;
    setState({ ...s, tabs: s.tabs.map((x) => (x.id === id ? { ...x, titleOverride: { path, text } } : x)) });
  }, [setState]);

  const reportDirty = useCallback((id, path, value) => {
    const cur = dirty.current.get(id);
    if (cur && cur.path === path && cur.dirty === value) return;
    dirty.current.set(id, { path, dirty: value });
    setDirtyVersion((v) => v + 1);
  }, []);

  const takeScroll = useCallback((id) => scrollY.current.get(id), []);

  useEffect(() => { writeSaved(uid, state.tabs, state.activeId); }, [state.tabs, state.activeId, uid]);

  useEffect(() => {                                                  // a hidden page's own guard is paused with it, so the window-level one lives here
    const onUnload = (e) => { if (stateRef.current.tabs.some((t) => isDirtyTab(t.id))) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [isDirtyTab]);

  const value = useMemo(() => ({
    role,
    tabs: state.tabs.map((t) => ({ ...t, title: t.titleOverride && t.titleOverride.path === t.loc.pathname ? t.titleOverride.text : tabTitleFor(t.loc.pathname), dirty: isDirtyTab(t.id) })),
    activeId: state.activeId,
    live: state.live,
    open, activate, close, reportTitle, reportDirty, takeScroll,
  }), [state, role, open, activate, close, reportTitle, reportDirty, takeScroll, isDirtyTab, dirtyVersion]);       // eslint-disable-line react-hooks/exhaustive-deps

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}
