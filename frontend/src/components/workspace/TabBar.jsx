import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";

/**
 * The tab strip. One horizontal row that scrolls when there are many tabs; each tab is a button to activate it plus a SEPARATE close button
 * (so a tap on × can never also activate the tab). Roving keyboard focus: ← → Home End move, Enter / Space activate, Delete closes.
 */
export default function TabBar() {
  const ws = useWorkspace();
  const listRef = useRef(null);
  // position:sticky never engages in this app (<html> and <body> both carry their own overflow — the same reason ProjectEdit's section bar is
  // fixed), so the bar is FIXED and an in-flow spacer of the same height reserves its room; the bar copies the spacer's left edge / width so it
  // lines up with the content column whether the sidebar is open, collapsed or absent (phone).
  const spacerRef = useRef(null);
  const [box, setBox] = useState({ left: 0, width: null });
  useLayoutEffect(() => {
    const el = spacerRef.current;
    if (!el) return undefined;
    const measure = () => { const r = el.getBoundingClientRect(); setBox((b) => (b.left === r.left && b.width === r.width ? b : { left: r.left, width: r.width })); };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    window.addEventListener("resize", measure);
    return () => { ro?.disconnect(); window.removeEventListener("resize", measure); };
  }, [!!ws]);                                                        // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                                   // keep the active tab in view when there are many
    const el = listRef.current?.querySelector('[role="tab"][aria-selected="true"]');
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [ws?.activeId, ws?.tabs.length]);
  if (!ws) return null;

  const focusTab = (i) => {
    const tabs = listRef.current?.querySelectorAll('[role="tab"]');
    if (tabs && tabs.length) tabs[(i + tabs.length) % tabs.length].focus();
  };
  const onKeyDown = (e, i, id) => {
    if (e.key === "ArrowRight") { e.preventDefault(); focusTab(i + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); focusTab(i - 1); }
    else if (e.key === "Home") { e.preventDefault(); focusTab(0); }
    else if (e.key === "End") { e.preventDefault(); focusTab(ws.tabs.length - 1); }
    else if (e.key === "Delete") { e.preventDefault(); ws.close(id); }
  };

  return (
    <>
    <div ref={spacerRef} style={{ height: "var(--tg-tabbar-h, 2.25rem)" }} aria-hidden="true" />
    <div data-testid="workspace-tabbar" style={{ left: box.left, width: box.width ?? undefined, right: box.width == null ? 0 : undefined }}
      className="fixed top-14 md:top-0 z-30 h-9 bg-[#f3f3f1]/95 backdrop-blur border-b border-black/[0.06]">
      <div ref={listRef} role="tablist" aria-label="Open pages" aria-orientation="horizontal"
        className="h-full flex items-end gap-1 px-2 overflow-x-auto overscroll-x-contain [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {ws.tabs.map((t, i) => {
          const active = t.id === ws.activeId;
          return (
            <div key={t.id} data-testid={`ws-tab-${t.id}`} data-active={active ? "true" : "false"}
              className={`group shrink-0 flex items-center max-w-[11rem] sm:max-w-[14rem] h-8 rounded-t-md border border-b-0 text-xs ${active ? "bg-white text-black border-black/[0.08] shadow-[0_-1px_0_rgba(0,0,0,0.02)]" : "bg-transparent text-black/55 border-transparent hover:bg-black/[0.04] hover:text-black/80"}`}
              onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); ws.close(t.id); } }}>
              <button type="button" role="tab" id={`tab-${t.id}`} aria-selected={active} aria-controls={`pane-${t.id}`} tabIndex={active ? 0 : -1} title={t.title}
                onClick={() => ws.activate(t.id)} onKeyDown={(e) => onKeyDown(e, i, t.id)}
                className="min-w-0 flex-1 h-full pl-3 pr-1 flex items-center gap-1.5 text-left rounded-tl-md focus:outline-none focus-visible:ring-2 focus-visible:ring-black/50">
                {t.dirty && <span className="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0" role="img" aria-label="Unsaved changes" />}
                <span className="truncate">{t.title}</span>
              </button>
              <button type="button" aria-label={`Close ${t.title}`} data-testid={`ws-close-${t.id}`} onClick={() => ws.close(t.id)}
                className="shrink-0 w-7 h-7 mr-0.5 flex items-center justify-center rounded-md text-black/40 hover:text-black hover:bg-black/[0.08] focus:outline-none focus-visible:ring-2 focus-visible:ring-black/50">
                <X className="w-3.5 h-3.5" aria-hidden />
              </button>
            </div>
          );
        })}
      </div>
    </div>
    </>
  );
}
