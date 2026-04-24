import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import {
    Plus,
    Clapperboard,
    Calendar,
    IndianRupee,
    Briefcase,
    Check,
} from "lucide-react";
import { toast } from "sonner";
import BulkSelectBar from "@/components/BulkSelectBar";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";

export default function ProjectList() {
    const [projects, setProjects] = useState([]);
    const [loading, setLoading] = useState(true);
    const [selected, setSelected] = useState(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
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

    return (
        <div
            className="p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="project-list-page"
        >
            <div className="flex items-end justify-between mb-10 flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Audition Engine</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Projects
                    </h1>
                </div>
                <Link
                    to="/admin/projects/new"
                    data-testid="new-project-btn"
                    className="inline-flex items-center gap-2 bg-white text-black px-5 py-3 rounded-sm text-xs tracking-wide hover:opacity-90 transition-all"
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} /> New Project
                </Link>
            </div>

            {loading ? (
                <div className="text-white/40 text-sm">Loading...</div>
            ) : projects.length === 0 ? (
                <div className="border border-white/10 p-12 text-center">
                    <Clapperboard
                        className="w-10 h-10 text-white/20 mx-auto mb-4"
                        strokeWidth={1}
                    />
                    <p className="text-white/60 mb-6">No projects yet</p>
                    <Link
                        to="/admin/projects/new"
                        className="inline-flex items-center gap-2 bg-white text-black px-5 py-2.5 rounded-sm text-xs"
                    >
                        Create your first project
                    </Link>
                </div>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 md:gap-6 pb-20">
                    {projects.map((p) => {
                        const checked = selected.has(p.id);
                        const goesToDetail = !isSelectionMode;
                        return (
                            <div
                                key={p.id}
                                data-testid={`project-card-${p.id}`}
                                className={`group relative border transition-all tg-fade-up ${checked ? "border-white" : "border-white/10 hover:border-white/30"}`}
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
                                        className={`absolute top-2 left-2 z-10 w-6 h-6 rounded-sm border flex items-center justify-center transition-all ${checked ? "bg-white border-white text-black" : "bg-black/60 border-white/40 text-transparent group-hover:text-white/60 opacity-0 group-hover:opacity-100"} ${isSelectionMode ? "opacity-100" : ""}`}
                                    >
                                        {checked && (
                                            <Check className="w-3.5 h-3.5" />
                                        )}
                                    </button>
                                )}
                                {goesToDetail ? (
                                    <Link
                                        to={`/admin/projects/${p.id}`}
                                        className="block p-6"
                                    >
                                        <Inner p={p} />
                                    </Link>
                                ) : (
                                    <button
                                        type="button"
                                        onClick={() => toggle(p.id)}
                                        className="block w-full p-6 text-left"
                                    >
                                        <Inner p={p} />
                                    </button>
                                )}
                            </div>
                        );
                    })}
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
    return (
        <>
            <div className="flex items-start justify-between mb-4">
                <Clapperboard
                    className="w-5 h-5 text-white/40"
                    strokeWidth={1.5}
                />
                <span className="text-[10px] tg-mono text-white/40">
                    {p.materials?.length || 0} materials
                </span>
            </div>
            <h3 className="font-display text-2xl tracking-tight mb-1">
                {p.brand_name}
            </h3>
            {p.character && (
                <p className="text-xs text-white/50 tg-mono mb-4">
                    {p.character}
                </p>
            )}
            <div className="mt-5 space-y-2 text-xs text-white/60">
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
            <Icon className="w-3 h-3 text-white/40" strokeWidth={1.5} />
            <span className="truncate">{label}</span>
        </div>
    );
}
