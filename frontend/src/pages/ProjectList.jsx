import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi } from "@/lib/api";
import {
    Plus,
    Clapperboard,
    Calendar,
    IndianRupee,
    Briefcase,
} from "lucide-react";

export default function ProjectList() {
    const [projects, setProjects] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        (async () => {
            try {
                const { data } = await adminApi.get("/projects");
                setProjects(data);
            } finally {
                setLoading(false);
            }
        })();
    }, []);

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
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 md:gap-6">
                    {projects.map((p) => (
                        <Link
                            key={p.id}
                            to={`/admin/projects/${p.id}`}
                            data-testid={`project-card-${p.id}`}
                            className="group border border-white/10 hover:border-white/30 p-6 transition-all tg-fade-up"
                        >
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
                                {p.shoot_dates && (
                                    <Row
                                        icon={Calendar}
                                        label={p.shoot_dates}
                                    />
                                )}
                                {p.budget_per_day && (
                                    <Row
                                        icon={IndianRupee}
                                        label={`${p.budget_per_day}${p.commission_percent ? ` · ${p.commission_percent} comm.` : ""}`}
                                    />
                                )}
                                {p.director && (
                                    <Row
                                        icon={Briefcase}
                                        label={`Dir. ${p.director}`}
                                    />
                                )}
                            </div>
                        </Link>
                    ))}
                </div>
            )}
        </div>
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
