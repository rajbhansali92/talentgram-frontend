import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Briefcase, Award, CheckCircle } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { portalApi, PORTAL_TOKEN_KEY } from "@/lib/api";
import ProjectCard from "@/components/shared/ProjectCard";

/**
 * Phase 2 item 2 — Dashboard/Projects content separation (see
 * docs/TALENT_DASHBOARD_ARCHITECTURE.md). This is the grouped project list
 * previously rendered inline on PortalHome.jsx ("Dashboard"), moved here
 * verbatim — same /portal/projects call, same Shortlisted/Ongoing/Completed
 * grouping, same ProjectCard rendering, same empty state. Business logic
 * unchanged; only its destination moved.
 *
 * Per the approved Phase 2 item 2 scope, this fetches independently rather
 * than sharing a data layer with PortalHome.jsx's Dashboard — a shared
 * fetch/context is a deliberate future step once a third consumer exists,
 * not built prematurely here.
 */
export default function PortalProjects() {
    const navigate = useNavigate();
    const [projects, setProjects] = useState({ ongoing: [], shortlisted: [], completed: [] });
    const [loading, setLoading] = useState(true);
    const token = typeof window !== "undefined" ? localStorage.getItem(PORTAL_TOKEN_KEY) : null;

    useEffect(() => {
        if (!token) {
            toast.error("Please sign in to access your portal");
            navigate("/");
            return;
        }

        const fetchProjects = async () => {
            try {
                const { data } = await portalApi.get(`/portal/projects`);
                setProjects(data);
            } catch (err) {
                console.error("Projects fetch error:", err);
                const status = err?.response?.status;
                if (status === 404) {
                    toast.error("Your profile session has expired or was removed.");
                } else {
                    toast.error("Unable to load your projects. Please sign in again.");
                }
                localStorage.removeItem(PORTAL_TOKEN_KEY);
                localStorage.removeItem("talentgram_portal_email");
                navigate("/");
            } finally {
                setLoading(false);
            }
        };

        fetchProjects();
    }, [token, navigate]);

    if (loading) {
        return (
            <div className="flex-1 bg-white text-black flex flex-col items-center justify-center py-16">
                <Logo size={64} className="animate-pulse" forceVariant="black" />
                <p className="text-xs text-black/45 uppercase tracking-[0.15em] mt-4">Loading your projects...</p>
            </div>
        );
    }

    const hasAnyProjects =
        projects.ongoing.length > 0 ||
        projects.shortlisted.length > 0 ||
        projects.completed.length > 0;

    return (
        <div className="flex-1 bg-[#fafafa] text-black" data-testid="portal-projects-page">
            <main className="max-w-4xl mx-auto py-8 px-6 md:py-12 flex flex-col gap-10">
                <h1 className="text-2xl font-semibold tracking-tight text-black">Projects</h1>

                {!hasAnyProjects ? (
                    /* Empty State */
                    <div className="bg-white border border-black/5 rounded-2xl p-12 text-center flex flex-col items-center gap-4 max-w-lg mx-auto my-8">
                        <Briefcase className="w-10 h-10 text-black/25" strokeWidth={1.5} />
                        <h3 className="font-semibold text-lg text-black">No Synced Projects</h3>
                        <p className="text-sm text-black/50 leading-relaxed">
                            You haven't started any project submissions yet. When an agency invites you or you apply to open briefs, they will show up here dynamically.
                        </p>
                    </div>
                ) : (
                    /* Synced Sections */
                    <div className="flex flex-col gap-10">
                        {/* 1. Shortlisted Campaigns */}
                        {projects.shortlisted.length > 0 && (
                            <div className="flex flex-col gap-4">
                                <div className="flex items-center gap-2 pb-2 border-b border-black/5">
                                    <Award className="w-4 h-4 text-black/80" />
                                    <h2 className="text-sm font-semibold tracking-wider uppercase text-black/85">Shortlisted</h2>
                                    <span className="bg-black/5 text-black/70 px-2 py-0.5 rounded-full text-[10px] font-bold">
                                        {projects.shortlisted.length}
                                    </span>
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    {projects.shortlisted.map((proj) => (
                                        <ProjectCard key={proj.project_id} project={proj} theme="shortlisted" />
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* 2. Ongoing Projects */}
                        {projects.ongoing.length > 0 && (
                            <div className="flex flex-col gap-4">
                                <div className="flex items-center gap-2 pb-2 border-b border-black/5">
                                    <Briefcase className="w-4 h-4 text-black/80" />
                                    <h2 className="text-sm font-semibold tracking-wider uppercase text-black/85">Ongoing Submissions</h2>
                                    <span className="bg-black/5 text-black/70 px-2 py-0.5 rounded-full text-[10px] font-bold">
                                        {projects.ongoing.length}
                                    </span>
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    {projects.ongoing.map((proj) => (
                                        <ProjectCard key={proj.project_id} project={proj} theme="ongoing" />
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* 3. Completed Campaigns */}
                        {projects.completed.length > 0 && (
                            <div className="flex flex-col gap-4">
                                <div className="flex items-center gap-2 pb-2 border-b border-black/5">
                                    <CheckCircle className="w-4 h-4 text-black/65" />
                                    <h2 className="text-sm font-semibold tracking-wider uppercase text-black/65">Completed Campaigns</h2>
                                    <span className="bg-black/5 text-black/60 px-2 py-0.5 rounded-full text-[10px] font-bold">
                                        {projects.completed.length}
                                    </span>
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 opacity-75">
                                    {projects.completed.map((proj) => (
                                        <ProjectCard key={proj.project_id} project={proj} theme="completed" />
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </main>
        </div>
    );
}
