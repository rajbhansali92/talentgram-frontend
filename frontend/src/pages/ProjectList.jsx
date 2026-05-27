import React, { useEffect, useState, useMemo } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import {
    Plus,
    Clapperboard,
    Calendar,
    IndianRupee,
    Briefcase,
    Check,
    ChevronDown,
} from "lucide-react";
import { toast } from "sonner";
import BulkSelectBar from "@/components/BulkSelectBar";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";

export default function ProjectList() {
    const [projects, setProjects] = useState([]);
    const [loading, setLoading] = useState(true);
    const [selected, setSelected] = useState(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [collapsedGroups, setCollapsedGroups] = useState({
        ongoing: false,
        hold: true,
        complete: true,
        locked: true,
    });

    const canBulkDelete = isAdmin();

    const load = async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/projects");
            setProjects(data);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
    }, []);

    const toggle = (id) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };
    const clear = () => setSelected(new Set());
    const selectAll = () => setSelected(new Set(projects.map((p) => p.id)));

    const bulkDelete = async () => {
        const ids = Array.from(selected);
        try {
            const res = await adminApi.post("/projects/bulk-delete", { ids });
             
            console.info("[bulk-delete projects]", ids, res?.data);
            toast.success(
                `Deleted ${res.data.deleted} project${res.data.deleted === 1 ? "" : "s"}${res.data.cascaded_submissions ? ` (+${res.data.cascaded_submissions} submissions)` : ""}`,
            );
            clear();
            setConfirmOpen(false);
            load();
        } catch (err) {
             
            console.error("[bulk-delete projects] failed", err?.response?.data || err);
            toast.error(err?.response?.data?.detail || "Bulk delete failed");
            throw err;
        }
    };

    const isSelectionMode = selected.size > 0;

    const groups = useMemo(() => {
        const ongoing = [];
        const hold = [];
        const complete = [];
        const locked = [];
        for (const p of projects) {
            const status = p.status || "ongoing";
            if (status === "ongoing") ongoing.push(p);
            else if (status === "hold") hold.push(p);
            else if (status === "complete") complete.push(p);
            else if (status === "locked") locked.push(p);
            else ongoing.push(p);
        }
        return { ongoing, hold, complete, locked };
    }, [projects]);

    const renderGroup = (key, title, list) => {
        const isCollapsed = collapsedGroups[key];
        const count = list.length;

        return (
            <div key={key} className="mb-6" data-testid={`project-group-${key}`}>
                <div
                    onClick={() => setCollapsedGroups(prev => ({ ...prev, [key]: !prev[key] }))}
                    className="flex items-center justify-between py-3 px-4 border border-black/[0.06] bg-black/[0.02] hover:bg-black/[0.04] rounded-lg cursor-pointer transition-colors duration-150 mb-4 select-none"
                >
                    <div className="flex items-center gap-3">
                        <span className="font-display text-sm tracking-wide text-black/80 font-medium">
                            {title}
                        </span>
                        <span className="inline-flex items-center justify-center px-2.5 py-0.5 text-[10px] font-semibold text-black/45 bg-black/[0.05] rounded-full">
                            {count}
                        </span>
                    </div>
                    <button
                        type="button"
                        className="p-1 border border-black/[0.08] hover:border-black/[0.16] rounded-md text-black/55 hover:text-black transition-colors"
                        aria-label={isCollapsed ? `Expand ${title}` : `Collapse ${title}`}
                    >
                        <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${isCollapsed ? "-rotate-90" : ""}`} />
                    </button>
                </div>

                {!isCollapsed && (
                    list.length === 0 ? (
                        <div className="text-xs text-black/40 pl-4 py-4 border border-dashed border-black/[0.08] rounded-xl text-center mb-4">
                            No {title.toLowerCase()}
                        </div>
                    ) : (
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 md:gap-5 mb-4">
                            {list.map((p) => {
                                const checked = selected.has(p.id);
                                const goesToDetail = !isSelectionMode;
                                return (
                                    <div
                                        key={p.id}
                                        data-testid={`project-card-${p.id}`}
                                        className={`group relative border rounded-xl transition-colors duration-150 ${
                                            checked 
                                                ? "border-black/40 bg-white" 
                                                : "border-black/[0.08] bg-white hover:border-black/[0.16]"
                                        }`}
                                    >
                                        {canBulkDelete && (
                                            <button
                                                type="button"
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    toggle(p.id);
                                                }}
                                                aria-label={
                                                    checked ? "Deselect" : "Select"
                                                }
                                                data-testid={`project-check-${p.id}`}
                                                className={`absolute top-3 left-3 z-10 w-5 h-5 rounded-md border flex items-center justify-center transition-colors duration-150 ${
                                                    checked 
                                                        ? "bg-black border-black text-white" 
                                                        : "bg-white border-black/[0.2] text-transparent group-hover:border-black/40"
                                                } ${isSelectionMode ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}
                                            >
                                                {checked && (
                                                    <Check className="w-3 h-3" />
                                                )}
                                            </button>
                                        )}
                                        {goesToDetail ? (
                                            <Link
                                                to={`/admin/projects/${p.id}`}
                                                className="block p-5"
                                            >
                                                <Inner p={p} />
                                            </Link>
                                        ) : (
                                            <button
                                                type="button"
                                                onClick={() => toggle(p.id)}
                                                className="block w-full p-5 text-left"
                                            >
                                                <Inner p={p} />
                                            </button>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    )
                )}
            </div>
        );
    };

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto pb-24"
            data-testid="project-list-page"
        >
            <div className="flex items-end justify-between mb-8 flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Audition Engine</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        Projects
                    </h1>
                </div>
                <Link
                    to="/admin/projects/new"
                    data-testid="new-project-btn"
                    className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150"
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} /> New Project
                </Link>
            </div>

            {loading ? (
                <div className="text-black/45 text-sm">Loading...</div>
            ) : projects.length === 0 ? (
                <div className="border border-black/[0.08] bg-white rounded-xl p-12 text-center">
                    <Clapperboard
                        className="w-10 h-10 text-black/20 mx-auto mb-4"
                        strokeWidth={1}
                    />
                    <p className="text-black/60 mb-6">No projects yet</p>
                    <Link
                        to="/admin/projects/new"
                        className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150"
                    >
                        Create your first project
                    </Link>
                </div>
            ) : (
                <div className="pb-10">
                    {renderGroup("ongoing", "Ongoing Projects", groups.ongoing)}
                    {renderGroup("hold", "Hold Projects", groups.hold)}
                    {renderGroup("complete", "Complete Projects", groups.complete)}
                    {renderGroup("locked", "Locked Projects", groups.locked)}
                </div>
            )}

            {canBulkDelete && (
                <BulkSelectBar
                    count={selected.size}
                    total={projects.length}
                    allSelected={selected.size === projects.length}
                    onSelectAll={selectAll}
                    onClear={clear}
                    onDelete={() => setConfirmOpen(true)}
                    labelSingular="project"
                    labelPlural="projects"
                    testid="projects-bulk-bar"
                />
            )}
            <ConfirmDeleteDialog
                open={confirmOpen}
                title={`Delete ${selected.size} project${selected.size === 1 ? "" : "s"}?`}
                description="This permanently removes the selected projects and all their submissions. Client links that already reference these projects keep their snapshots. This cannot be undone."
                confirmLabel={`Delete ${selected.size}`}
                typeToConfirm="DELETE"
                onCancel={() => setConfirmOpen(false)}
                onConfirm={bulkDelete}
                testid="projects-bulk-confirm"
            />
        </div>
    );
}

function Inner({ p }) {
    const status = p.status || "ongoing";
    const statusConfig = {
        ongoing: { color: "bg-green-500", label: "Ongoing" },
        hold: { color: "bg-amber-500", label: "Hold" },
        complete: { color: "bg-gray-400", label: "Complete" },
        locked: { color: "bg-neutral-600", label: "Locked" },
    };
    const currentStatus = statusConfig[status] || statusConfig.ongoing;

    return (
        <>
            <div className="flex items-start justify-between mb-3 gap-2">
                <div className="flex items-center gap-2">
                    <Clapperboard
                        className="w-4 h-4 text-black/40"
                        strokeWidth={1.5}
                    />
                    <div className="flex items-center gap-1 bg-black/[0.03] px-2 py-0.5 rounded-full border border-black/[0.04]">
                        <span className={`w-1.5 h-1.5 rounded-full ${currentStatus.color}`} />
                        <span className="text-[9px] font-medium text-black/50 uppercase tracking-wider">{currentStatus.label}</span>
                    </div>
                </div>
                <span className="text-[10px] text-black/45 shrink-0">
                    {p.materials?.length || 0} material{p.materials?.length === 1 ? "" : "s"}
                </span>
            </div>
            <h3 className="font-display text-lg tracking-tight text-black/85 mb-1">
                {p.brand_name}
            </h3>
            {p.character && (
                <p className="text-xs text-black/45 mb-3">
                    {p.character}
                </p>
            )}
            <div className="mt-3 space-y-2 text-xs text-black/60">
                {p.shoot_dates && <Row icon={Calendar} label={p.shoot_dates} />}
                {p.budget_per_day && (
                    <Row
                        icon={IndianRupee}
                        label={`${p.budget_per_day}${p.commission_percent ? ` · ${p.commission_percent} comm.` : ""}`}
                    />
                )}
                {p.director && (
                    <Row icon={Briefcase} label={`Dir. ${p.director}`} />
                )}
            </div>
        </>
    );
}

function Row({ icon: Icon, label }) {
    return (
        <div className="flex items-center gap-2">
            <Icon className="w-3 h-3 text-black/40" strokeWidth={1.5} />
            <span className="truncate text-black/60">{label}</span>
        </div>
    );
}
