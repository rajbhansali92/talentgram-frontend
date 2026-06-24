import React, { useCallback, useEffect, useRef, useState } from "react";
import { Search, X, Star, Clock } from "lucide-react";
import {
  searchProjects, getRecentProjects, getPinnedProjects, pinProject, unpinProject,
} from "@/lib/whatsappApi";

/**
 * Server-side project search modal (Feature 4 / Slice 3).
 * Replaces the dropdown so the picker scales to hundreds of projects:
 * keyboard search, status filter, recent + pinned sections, infinite scroll.
 *
 *   <ProjectSearchModal open={open} onClose={...} onSelect={(project) => ...} />
 */
const PAGE = 30;
const STATUSES = ["", "ongoing", "hold", "complete", "locked"];

export default function ProjectSearchModal({ open, onClose, onSelect }) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [recent, setRecent] = useState([]);
  const [pinned, setPinned] = useState([]);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  const showBrowse = !q.trim() && !status;

  const loadPage = useCallback(async (nextOffset, replace) => {
    setLoading(true);
    try {
      const data = await searchProjects({ q: q || undefined, status: status || undefined, offset: nextOffset, limit: PAGE });
      setTotal(data.total || 0);
      setOffset(nextOffset);
      setResults((prev) => (replace ? data.items : [...prev, ...(data.items || [])]));
    } catch {
      /* surfaced by empty list */
    } finally {
      setLoading(false);
    }
  }, [q, status]);

  // Reset + initial loads when opened / query changes
  useEffect(() => {
    if (!open) return;
    setResults([]); setOffset(0); setTotal(0);
    if (showBrowse) {
      getRecentProjects(10).then(setRecent).catch(() => {});
      getPinnedProjects().then(setPinned).catch(() => {});
    } else {
      loadPage(0, true);
    }
  }, [open, q, status, showBrowse, loadPage]);

  useEffect(() => { if (open) setTimeout(() => inputRef.current?.focus(), 50); }, [open]);

  const onScroll = (e) => {
    const el = e.currentTarget;
    if (loading || results.length >= total) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 80) loadPage(offset + PAGE, false);
  };

  const togglePin = async (p, isPinned) => {
    try {
      if (isPinned) { await unpinProject(p.id); setPinned((x) => x.filter((q2) => q2.id !== p.id)); }
      else { await pinProject(p.id); setPinned((x) => [p, ...x]); }
    } catch { /* ignore */ }
  };

  if (!open) return null;
  const pinnedIds = new Set(pinned.map((p) => p.id));

  const Row = ({ p, showPin = true }) => (
    <div className="flex items-center gap-2 px-3 py-2 hover:bg-[#f8f8f6] cursor-pointer border-b border-black/[0.04]"
         onClick={() => { onSelect(p); onClose(); }}>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{p.name || p.brand_name}</p>
        <p className="text-[10px] text-black/40 font-mono">{p.status} · {p.slug}</p>
      </div>
      {showPin && (
        <button onClick={(e) => { e.stopPropagation(); togglePin(p, pinnedIds.has(p.id)); }}
                className="p-1 rounded hover:bg-black/5" title={pinnedIds.has(p.id) ? "Unpin" : "Pin"}>
          <Star className={`w-3.5 h-3.5 ${pinnedIds.has(p.id) ? "fill-amber-400 text-amber-400" : "text-black/30"}`} />
        </button>
      )}
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[10vh]" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-2xl w-full max-w-lg max-h-[70vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 p-3 border-b border-black/10">
          <Search className="w-4 h-4 text-black/40" />
          <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)}
                 placeholder="Search projects by name…"
                 className="flex-1 text-sm focus:outline-none" data-testid="project-search-input" />
          <select value={status} onChange={(e) => setStatus(e.target.value)}
                  className="text-xs border border-black/10 rounded px-1.5 py-1">
            {STATUSES.map((s) => <option key={s} value={s}>{s || "All status"}</option>)}
          </select>
          <button onClick={onClose} className="p-1 rounded hover:bg-black/5"><X className="w-4 h-4" /></button>
        </div>

        <div ref={scrollRef} onScroll={onScroll} className="overflow-y-auto flex-1">
          {showBrowse ? (
            <>
              {pinned.length > 0 && (
                <div>
                  <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-black/40 bg-[#f8f8f6] flex items-center gap-1"><Star className="w-3 h-3" /> Pinned</div>
                  {pinned.map((p) => <Row key={`pin-${p.id}`} p={p} />)}
                </div>
              )}
              <div>
                <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-black/40 bg-[#f8f8f6] flex items-center gap-1"><Clock className="w-3 h-3" /> Recent</div>
                {recent.map((p) => <Row key={`rec-${p.id}`} p={p} />)}
                {recent.length === 0 && <p className="px-3 py-4 text-xs text-black/40">No projects yet.</p>}
              </div>
            </>
          ) : (
            <>
              {results.map((p) => <Row key={p.id} p={p} />)}
              {!loading && results.length === 0 && <p className="px-3 py-6 text-xs text-black/40 text-center">No matching projects.</p>}
              {loading && <p className="px-3 py-3 text-xs text-black/40 text-center">Loading…</p>}
              {results.length > 0 && <p className="px-3 py-2 text-[10px] text-black/30 text-center">Showing {results.length} of {total}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
