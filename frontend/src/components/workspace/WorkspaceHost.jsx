import React, { Activity, Suspense, memo, useLayoutEffect, useMemo, useRef } from "react";
import { Navigate, Route, Routes, UNSAFE_RouteContext as RouteContext } from "react-router-dom";
import SectionErrorBoundary from "@/components/SectionErrorBoundary";
import { ADMIN_PAGE_ROUTES } from "@/components/workspace/adminRoutes";
import { PaneContext, useWorkspace } from "@/components/workspace/WorkspaceContext";

/**
 * Renders every opened page at once. The visible one is a normal page; the others sit in <Activity mode="hidden"> — their state and DOM are
 * kept (a half-built Generate Link, a filter, a form draft) while their effects are paused (no key handlers, polling or assistant selection).
 * Each pane matches its OWN location with <Routes location>, so useParams / useLocation inside a page describe that tab, not the visible one.
 */
// The host sits inside AdminLayout's own <Route path="/admin">, and a <Routes> nested in a route matches RELATIVE to it. Each pane is a self-contained
// router for ITS tab's location with absolute paths, so it starts from an empty route context.
const TOP_LEVEL = { outlet: null, matches: [], isDataRoute: false };
const ROUTES = (
  <>
    {ADMIN_PAGE_ROUTES.map((r) => <Route key={r.abs} path={r.abs} element={r.element} />)}
    <Route path="*" element={<Navigate to="/admin" replace />} />
  </>
);

// Re-runs every time the pane is revealed (Activity re-creates its effects): put every inner scroller back where it was left.
function RestoreScroll({ scrolls }) {
  useLayoutEffect(() => {
    scrolls.current.forEach((pos, el) => {
      if (!el.isConnected) { scrolls.current.delete(el); return; }
      el.scrollTop = pos.top; el.scrollLeft = pos.left;
    });
  }, [scrolls]);
  return null;
}

const Pane = memo(function Pane({ id, loc, active, reportTitle, reportDirty }) {
  const scrolls = useRef(new Map());
  const ctx = useMemo(() => ({
    tabId: id, active, pathname: loc.pathname,
    setTitle: (text) => reportTitle(id, loc.pathname, text),
    setDirty: (value) => reportDirty(id, loc.pathname, value),
  }), [id, loc.pathname, active, reportTitle, reportDirty]);
  const onScroll = (e) => {                                          // scroll doesn't bubble; the capture phase sees every inner scroller
    const el = e.target;
    if (el && el.nodeType === 1) scrolls.current.set(el, { top: el.scrollTop, left: el.scrollLeft });
  };
  return (
    <Activity mode={active ? "visible" : "hidden"}>
      <div role="tabpanel" id={`pane-${id}`} aria-labelledby={`tab-${id}`} data-testid={`tab-pane-${id}`} data-tab-pane data-active={active ? "true" : "false"} onScrollCapture={onScroll}>
        <PaneContext.Provider value={ctx}>
          <RestoreScroll scrolls={scrolls} />
          {/* a page that crashes takes down its own tab only (keyed by route, so navigating inside the tab starts it fresh) */}
          <SectionErrorBoundary key={loc.pathname} label={`tab ${loc.pathname}`} fallback={<div className="p-6 text-sm text-black/60" role="alert">This page hit an error. Your other tabs are unaffected — close this tab and open the page again.</div>}>
            <RouteContext.Provider value={TOP_LEVEL}>
              <Routes location={loc}>{ROUTES}</Routes>
            </RouteContext.Provider>
          </SectionErrorBoundary>
        </PaneContext.Provider>
      </div>
    </Activity>
  );
});

export default function WorkspaceHost() {
  const ws = useWorkspace();
  const first = useRef(true);
  useLayoutEffect(() => {                                            // the window itself scrolls: restore where this tab was left (a new tab starts at the top)
    if (first.current) { first.current = false; return; }
    const y = ws.takeScroll(ws.activeId) ?? 0;
    if (y !== window.scrollY) { try { window.scrollTo(0, y); } catch { /* not scrollable */ } }
  }, [ws.activeId]);                                                 // eslint-disable-line react-hooks/exhaustive-deps
  const live = new Set(ws.live);
  return (
    <Suspense fallback={null}>
      {ws.tabs.filter((t) => live.has(t.id)).map((t) => (
        <Pane key={t.id} id={t.id} loc={t.loc} active={t.id === ws.activeId} reportTitle={ws.reportTitle} reportDirty={ws.reportDirty} />
      ))}
    </Suspense>
  );
}
