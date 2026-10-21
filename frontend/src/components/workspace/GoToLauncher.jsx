import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Compass, Search } from "lucide-react";
import { adminApi } from "@/lib/api";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { destinationsFor, filterDestinations } from "@/components/workspace/paths";

/**
 * "Go to": a floating button beside the Assistant's that lists the pages this account may open (derived from the sidebar's NAV_ITEMS, so
 * admin-only pages never appear for anyone else) and opens the chosen one in a NEW IN-APP TAB. Typing a project name also offers that project's
 * Pipeline and Submission Review. The route guards are the real enforcement; this list is only what is offered.
 */
const MAX_PROJECT_HITS = 5;

export default function GoToLauncher({ assistantVisible = false, lifted = false, dock = null }) {
  const ws = useWorkspace();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const [projects, setProjects] = useState(null);
  const btnRef = useRef(null);
  const panelRef = useRef(null);
  const inputRef = useRef(null);

  const role = ws?.role;
  const pages = useMemo(() => (role ? destinationsFor(role) : []), [role]);

  const close = useCallback((refocus = true) => { setOpen(false); setQuery(""); setCursor(0); setProjects(null); if (refocus) btnRef.current?.focus(); }, []);

  useEffect(() => {                                                   // project names are fetched only once the user starts typing, and dropped on close
    if (!open || projects !== null || query.trim().length < 2) return undefined;
    let live = true;
    adminApi.get("/projects").then(({ data }) => { if (live) setProjects(Array.isArray(data) ? data : (data?.items || [])); }).catch(() => { if (live) setProjects([]); });
    return () => { live = false; };
  }, [open, query, projects]);

  const items = useMemo(() => {
    const out = filterDestinations(pages, query);
    const q = query.trim().toLowerCase();
    if (q.length >= 2 && projects) {
      const terms = q.split(/\s+/);
      projects.filter((p) => p?.id && p.brand_name && terms.every((t) => p.brand_name.toLowerCase().includes(t))).slice(0, MAX_PROJECT_HITS).forEach((p) => {
        out.push({ to: `/admin/projects/${p.id}`, label: `${p.brand_name} — Pipeline`, group: "Projects", keywords: [] });
        out.push({ to: `/admin/projects/${p.id}/submissions`, label: `${p.brand_name} — Submission Review`, group: "Projects", keywords: [] });
      });
    }
    return out;
  }, [pages, query, projects]);

  useEffect(() => { setCursor((c) => Math.min(c, Math.max(0, items.length - 1))); }, [items.length]);
  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (!panelRef.current?.contains(e.target) && !btnRef.current?.contains(e.target)) close(false); };
    document.addEventListener("mousedown", away); document.addEventListener("touchstart", away);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("touchstart", away); };
  }, [open, close]);
  useEffect(() => { panelRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: "nearest" }); }, [cursor, open]);

  if (!ws || !role) return null;

  const choose = (item) => { if (!item) return; ws.open(item.to); close(false); };
  const onKeyDown = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => (items.length ? (c + 1) % items.length : 0)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => (items.length ? (c - 1 + items.length) % items.length : 0)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(items[cursor]); }
  };

  // sits exactly where the assistant's launcher sits (same bottom), to its left; alone it takes the corner
  const right = assistantVisible ? "right-[4.5rem] sm:right-[5.5rem]" : "right-4 sm:right-6";
  const bottomBtn = lifted ? "bottom-[calc(9rem+env(safe-area-inset-bottom))] sm:bottom-28" : "bottom-[calc(5.5rem+env(safe-area-inset-bottom))] sm:bottom-6";
  const bottomPanel = lifted ? "bottom-[calc(12.75rem+env(safe-area-inset-bottom))] sm:bottom-[10.75rem]" : "bottom-[calc(9.25rem+env(safe-area-inset-bottom))] sm:bottom-[5.25rem]";
  const lift = lifted && dock ? { bottom: `${dock}px` } : undefined;
  const liftPanel = lifted && dock ? { bottom: `${dock + 60}px` } : undefined;

  let lastGroup = null;
  return (
    <>
      <button ref={btnRef} type="button" onClick={() => (open ? close() : setOpen(true))} aria-label="Go to" aria-haspopup="dialog" aria-expanded={open} data-testid="goto-launcher" style={lift}
        className={`fixed z-30 ${right} ${bottomBtn} w-12 h-12 rounded-full bg-white text-black border border-black/[0.15] shadow-[0_6px_20px_rgba(0,0,0,0.18)] flex items-center justify-center hover:bg-black/[0.03] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-black`}>
        <Compass className="w-5 h-5" aria-hidden />
      </button>
      {open && (
        <div ref={panelRef} role="dialog" aria-label="Go to a page" data-testid="goto-panel" style={liftPanel}
          className={`fixed z-40 right-4 sm:right-6 ${bottomPanel} w-[min(22rem,calc(100vw-2rem))] max-h-[min(60dvh,26rem)] flex flex-col bg-white border border-black/[0.12] rounded-2xl shadow-[0_12px_40px_rgba(0,0,0,0.22)] overflow-hidden`}>
          <div className="flex items-center gap-2 px-3 py-2.5 border-b border-black/[0.08] shrink-0">
            <Search className="w-4 h-4 text-black/40 shrink-0" aria-hidden />
            <input ref={inputRef} value={query} onChange={(e) => { setQuery(e.target.value); setCursor(0); }} onKeyDown={onKeyDown} placeholder="Go to a page…" aria-label="Search pages"
              role="combobox" aria-expanded="true" aria-controls="goto-list" aria-autocomplete="list" aria-activedescendant={items[cursor] ? `goto-opt-${cursor}` : undefined} autoComplete="off"
              className="flex-1 min-w-0 bg-transparent text-base sm:text-sm focus:outline-none placeholder:text-black/40" data-testid="goto-search" />
          </div>
          <ul id="goto-list" role="listbox" aria-label="Pages" className="overflow-y-auto overscroll-contain py-1.5 min-h-0" data-testid="goto-list">
            {items.length === 0 && <li className="px-4 py-3 text-xs text-black/50" role="presentation">No page matches “{query.trim()}”.</li>}
            {items.map((it, i) => {
              const head = it.group !== lastGroup ? (lastGroup = it.group) : null;
              const Icon = it.icon;
              return (
                <React.Fragment key={`${it.to}|${it.label}`}>
                  {head && <li role="presentation" className="px-4 pt-2 pb-1 text-[10px] tracking-[0.14em] uppercase text-black/40 font-medium">{head}</li>}
                  <li role="option" id={`goto-opt-${i}`} aria-selected={i === cursor} data-testid={`goto-opt-${i}`} onMouseEnter={() => setCursor(i)} onClick={() => choose(it)}
                    className={`mx-1.5 px-3 min-h-[44px] flex items-center gap-2.5 rounded-lg text-sm cursor-pointer ${i === cursor ? "bg-black text-white" : "text-black/75 hover:bg-black/[0.04]"}`}>
                    {Icon ? <Icon className="w-4 h-4 shrink-0 opacity-70" strokeWidth={1.5} aria-hidden /> : <span className="w-4 shrink-0" />}
                    <span className="truncate">{it.label}</span>
                  </li>
                </React.Fragment>
              );
            })}
          </ul>
        </div>
      )}
    </>
  );
}
