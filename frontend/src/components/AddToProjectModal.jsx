import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { X, Search, Check, Loader2, FolderKanban } from "lucide-react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";

/**
 * AddToProjectModal — bulk "Add to Project" picker launched from the Global
 * Talent Directory's floating selection bar. Lists only ACTIVE (ongoing)
 * projects, supports selecting one or many, and submits everything through
 * the single POST /projects/bulk-add-talents call (no per-talent/per-project
 * requests). Talents land in each project's configured default pipeline
 * stage and existing (project, talent) pairs are skipped server-side —
 * never duplicated.
 */
export default function AddToProjectModal({ open, talentIds, onClose, onSuccess }) {
    const [projects, setProjects] = useState([]);
    const [loadingProjects, setLoadingProjects] = useState(false);
    const [search, setSearch] = useState("");
    const [checked, setChecked] = useState(new Set());
    const [focusedIndex, setFocusedIndex] = useState(0);
    const [submitting, setSubmitting] = useState(false);
    const searchInputRef = useRef(null);
    const listRef = useRef(null);
    const itemRefs = useRef(new Map());

    useEffect(() => {
        if (!open) return;
        setSearch("");
        setChecked(new Set());
        setFocusedIndex(0);
        let isMounted = true;
        setLoadingProjects(true);
        adminApi
            .get("/projects", { params: { status: "ongoing" } })
            .then(({ data }) => {
                if (isMounted) setProjects(Array.isArray(data) ? data : []);
            })
            .catch(() => {
                if (isMounted) toast.error("Failed to load active projects");
            })
            .finally(() => {
                if (isMounted) setLoadingProjects(false);
            });
        setTimeout(() => searchInputRef.current?.focus(), 50);
        return () => { isMounted = false; };
    }, [open]);

    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return projects;
        return projects.filter((p) =>
            (p.brand_name || "").toLowerCase().includes(q) ||
            (p.character || "").toLowerCase().includes(q) ||
            (p.status || "").toLowerCase().includes(q)
        );
    }, [projects, search]);

    const toggle = useCallback((id) => {
        setChecked((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    // Keyboard navigation: Up/Down move focus, Space/Enter toggles the
    // focused project, Escape closes. Scoped to the search input so typing
    // still works normally.
    const handleKeyDown = useCallback(
        (e) => {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                setFocusedIndex((i) => {
                    const next = Math.min(filtered.length - 1, i + 1);
                    itemRefs.current.get(filtered[next]?.id)?.scrollIntoView({ block: "nearest" });
                    return next;
                });
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setFocusedIndex((i) => {
                    const next = Math.max(0, i - 1);
                    itemRefs.current.get(filtered[next]?.id)?.scrollIntoView({ block: "nearest" });
                    return next;
                });
            } else if (e.key === "Enter" || e.key === " ") {
                if (filtered[focusedIndex]) {
                    e.preventDefault();
                    toggle(filtered[focusedIndex].id);
                }
            } else if (e.key === "Escape") {
                onClose();
            }
        },
        [filtered, focusedIndex, toggle, onClose]
    );

    useEffect(() => {
        if (focusedIndex >= filtered.length) setFocusedIndex(Math.max(0, filtered.length - 1));
    }, [filtered, focusedIndex]);

    const handleAdd = async () => {
        if (checked.size === 0 || submitting) return;
        setSubmitting(true);
        try {
            const { data } = await adminApi.post("/projects/bulk-add-talents", {
                project_ids: Array.from(checked),
                talent_ids: talentIds,
            });
            const skippedNote = data.skipped > 0 ? ` (${data.skipped} already existed)` : "";
            toast.success(
                `Added ${data.added} to ${data.project_count} project${data.project_count === 1 ? "" : "s"}${skippedNote}`
            );
            onSuccess?.(data);
            onClose();
        } catch (err) {
            toast.error(formatErrorDetail(err, "Failed to add talents to project"));
        } finally {
            setSubmitting(false);
        }
    };

    if (!open) return null;

    const talentCount = talentIds.length;

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
            onClick={onClose}
        >
            <div
                className="bg-white rounded-2xl p-6 max-w-md w-full shadow-2xl border border-black/[0.08] text-black relative flex flex-col max-h-[80vh]"
                onClick={(e) => e.stopPropagation()}
                data-testid="add-to-project-modal"
            >
                <div className="flex items-center justify-between mb-4 shrink-0">
                    <div className="flex items-center gap-2">
                        <FolderKanban className="w-4 h-4 text-black/50" />
                        <h3 className="font-semibold text-sm text-neutral-800">Add to Project</h3>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full hover:bg-black/5 transition-colors" aria-label="Close">
                        <X className="w-4 h-4 text-black/40 hover:text-black" />
                    </button>
                </div>

                <p className="text-xs text-black/50 mb-4 shrink-0">
                    Add {talentCount} selected talent{talentCount === 1 ? "" : "s"} to one or more active projects.
                </p>

                <div className="relative mb-3 shrink-0">
                    <Search className="absolute left-0 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-black/30" />
                    <input
                        ref={searchInputRef}
                        type="text"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder="Search active projects..."
                        data-testid="add-to-project-search"
                        className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2 pl-5 text-sm text-black/85 placeholder:text-black/30"
                    />
                </div>

                <div
                    ref={listRef}
                    className="flex-1 overflow-y-auto mb-4 border border-black/[0.04] rounded-lg divide-y divide-black/[0.04] min-h-[120px]"
                    data-testid="add-to-project-list"
                >
                    {loadingProjects ? (
                        <div className="flex items-center justify-center py-8">
                            <Loader2 className="w-4 h-4 animate-spin text-black/40" />
                        </div>
                    ) : filtered.length === 0 ? (
                        <div className="p-4 text-xs text-black/30 italic text-center">
                            {projects.length === 0 ? "No active projects" : "No projects match your search"}
                        </div>
                    ) : (
                        filtered.map((p, i) => {
                            const isChecked = checked.has(p.id);
                            const isFocused = i === focusedIndex;
                            return (
                                <button
                                    key={p.id}
                                    ref={(el) => { if (el) itemRefs.current.set(p.id, el); }}
                                    type="button"
                                    onClick={() => toggle(p.id)}
                                    onMouseEnter={() => setFocusedIndex(i)}
                                    data-testid={`add-to-project-option-${p.id}`}
                                    className={[
                                        "w-full text-left px-3.5 py-2.5 text-xs flex items-center justify-between gap-3 transition-colors",
                                        isFocused ? "bg-black/[0.03]" : "hover:bg-black/[0.02]",
                                    ].join(" ")}
                                >
                                    <div className="flex items-center gap-2.5 min-w-0">
                                        <span
                                            className={[
                                                "w-4 h-4 rounded-[4px] border flex items-center justify-center shrink-0",
                                                isChecked ? "bg-black border-black text-white" : "border-black/25 text-transparent",
                                            ].join(" ")}
                                        >
                                            {isChecked && <Check className="w-3 h-3" strokeWidth={2.5} />}
                                        </span>
                                        <div className="min-w-0">
                                            <div className="font-medium text-neutral-800 truncate">{p.brand_name || "Untitled project"}</div>
                                            <div className="text-[10px] text-black/40 truncate">
                                                {[p.character, p.shoot_dates].filter(Boolean).join(" · ") || " "}
                                            </div>
                                        </div>
                                    </div>
                                    <span className="text-[9px] uppercase tracking-wide text-emerald-600 font-semibold shrink-0">
                                        {p.status}
                                    </span>
                                </button>
                            );
                        })
                    )}
                </div>

                <div className="flex gap-2 shrink-0">
                    <button
                        onClick={onClose}
                        disabled={submitting}
                        className="flex-1 py-2.5 border border-black/[0.08] hover:bg-black/[0.02] text-black/60 hover:text-black text-xs font-medium rounded-lg transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={handleAdd}
                        disabled={checked.size === 0 || submitting}
                        data-testid="add-to-project-submit"
                        className="flex-1 py-2.5 bg-black text-white text-xs font-medium rounded-lg transition-colors disabled:opacity-40 hover:bg-black/85 flex items-center justify-center gap-1.5"
                    >
                        {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                        Add{checked.size > 0 ? ` to ${checked.size} project${checked.size === 1 ? "" : "s"}` : ""}
                    </button>
                </div>
            </div>
        </div>
    );
}
