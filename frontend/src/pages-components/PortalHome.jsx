import React, { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { User, MapPin, Edit3, Briefcase, HeartPulse, Clock, ArrowUpRight, ListChecks, Settings as SettingsIcon } from "lucide-react";
import Logo from "@/components/Logo";
import { formatTalentLocation } from "@/lib/sanitize";
import { toast } from "sonner";
import { portalApi, PORTAL_TOKEN_KEY } from "@/lib/api";
import ProjectCard from "@/components/shared/ProjectCard";

/**
 * Phase 2 item 2 — Dashboard/Projects content separation (see
 * docs/TALENT_DASHBOARD_ARCHITECTURE.md). PortalHome.jsx is now the
 * "Dashboard" overview rather than the full project list (that moved to
 * PortalProjects.jsx verbatim). Sections built from data this page already
 * fetches:
 *   - Welcome: the same Profile Summary Card that was already here.
 *   - Continue Submission / Recent projects summary: derived client-side
 *     from the same /portal/projects response already being fetched here —
 *     no new backend logic.
 *   - Quick actions: links to existing routes.
 * Profile Health and Recent Profile Activity are NOT built with real logic
 * in this step — both require data/endpoints that don't exist yet (a
 * completeness score, and a talent-facing read of profile_audits). Per
 * "no redesign yet, just move existing functionality," they're honest
 * placeholders reserving their slot in the layout, same treatment as the
 * Projects/Settings stubs from Phase 2 item 1 — not real features yet.
 */
export default function PortalHome() {
    const navigate = useNavigate();
    const [talent, setTalent] = useState(null);
    const [projects, setProjects] = useState({ ongoing: [], shortlisted: [], completed: [] });
    const [loading, setLoading] = useState(true);
    const token = typeof window !== "undefined" ? localStorage.getItem(PORTAL_TOKEN_KEY) : null;

    useEffect(() => {
        if (!token) {
            toast.error("Please sign in to access your portal");
            navigate("/");
            return;
        }

        const fetchPortalData = async () => {
            try {
                // Identity is derived from the portal token; no email param.
                const profileRes = await portalApi.get(`/portal/profile`);
                setTalent(profileRes.data);

                // Fetch synced projects
                const projectsRes = await portalApi.get(`/portal/projects`);
                setProjects(projectsRes.data);
            } catch (err) {
                console.error("Portal fetch error:", err);
                const status = err?.response?.status;
                if (status === 404) {
                    toast.error("Your profile session has expired or was removed.");
                } else {
                    toast.error("Unable to load your profile. Please sign in again.");
                }
                localStorage.removeItem(PORTAL_TOKEN_KEY);
                localStorage.removeItem("talentgram_portal_email");
                navigate("/");
            } finally {
                setLoading(false);
            }
        };


        fetchPortalData();
    }, [token, navigate]);

    // Sign-out lives in the shared DashboardLayout shell (see
    // components/DashboardLayout.jsx) — this page no longer renders its own
    // header/sign-out button.

    if (loading) {
        return (
            <div className="min-h-dvh bg-white text-black flex flex-col items-center justify-center">
                <Logo size={80} className="animate-pulse" forceVariant="black" />
                <p className="text-xs text-black/45 uppercase tracking-[0.15em] mt-4">Loading your Talentgram...</p>
            </div>
        );
    }

    const hasAnyProjects =
        projects.ongoing.length > 0 ||
        projects.shortlisted.length > 0 ||
        projects.completed.length > 0;

    // Continue Submission: the first ongoing engagement still in draft —
    // derived from the same /portal/projects data already fetched above,
    // no new backend call.
    const draftInProgress = projects.ongoing.find((p) => p.status === "draft");

    // Recent projects summary: most-recently-updated across all groups,
    // same data, client-side sort/slice only.
    const recentProjects = [...projects.shortlisted, ...projects.ongoing, ...projects.completed]
        .filter((p) => p.updated_at)
        .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at))
        .slice(0, 3);

    return (
        <div className="flex-1 bg-[#fafafa] text-black flex flex-col justify-between" data-testid="portal-home-page">
            <div>
                {/* Welcome */}
                <section className="bg-white border-b border-black/5 py-8 md:py-12 px-6 md:px-12">
                    <div className="max-w-4xl mx-auto flex flex-col md:flex-row md:items-center justify-between gap-6">
                        <div className="flex items-center gap-5">
                            {talent?.image_url ? (
                                <img
                                    src={talent.image_url}
                                    alt={talent.name}
                                    className="w-20 h-20 md:w-24 md:h-24 rounded-full object-cover border border-black/10"
                                />
                            ) : (
                                <div className="w-20 h-20 md:w-24 md:h-24 rounded-full bg-black/5 flex items-center justify-center border border-black/10">
                                    <User className="w-8 h-8 text-black/30" />
                                </div>
                            )}
                            <div className="flex flex-col gap-1.5">
                                <h1 className="text-2xl md:text-3xl font-semibold text-black tracking-tight">Welcome back, {talent?.name?.split(" ")[0] || "there"}</h1>
                                <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-black/55">
                                    {formatTalentLocation(talent?.location) && (
                                        <span className="flex items-center gap-1">
                                            <MapPin className="w-3.5 h-3.5 text-black/40" />
                                            {formatTalentLocation(talent.location)}
                                        </span>
                                    )}
                                    {talent?.height && <span>{talent.height}</span>}
                                    {talent?.age && <span>{talent.age} years old</span>}
                                </div>
                                <div className="flex flex-wrap gap-1.5 mt-1">
                                    {talent?.interested_in?.map((cat, idx) => (
                                        <span key={idx} className="bg-black/5 px-2 py-0.5 rounded text-[10px] uppercase tracking-wider text-black/60 font-medium">
                                            {cat}
                                        </span>
                                    ))}
                                </div>
                            </div>
                        </div>

                        <Link
                            to="/portal/profile"
                            className="inline-flex items-center justify-center gap-1.5 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:opacity-90 transition-all duration-150 self-start md:self-center"
                        >
                            <Edit3 className="w-3.5 h-3.5" />
                            Edit Profile
                        </Link>
                    </div>
                </section>

                <main className="max-w-4xl mx-auto py-8 px-6 md:py-12 flex flex-col gap-10">
                    {/* Profile Health + Recent Profile Activity — reserved slots,
                        not real features yet (see file header). */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-2">
                            <div className="flex items-center gap-2 text-black/70">
                                <HeartPulse className="w-4 h-4" />
                                <h2 className="text-xs font-semibold tracking-wider uppercase">Profile Health</h2>
                            </div>
                            <p className="text-sm text-black/45">Coming soon — a quality indicator for your profile, not an access restriction.</p>
                        </div>
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-2">
                            <div className="flex items-center gap-2 text-black/70">
                                <Clock className="w-4 h-4" />
                                <h2 className="text-xs font-semibold tracking-wider uppercase">Recent Profile Activity</h2>
                            </div>
                            <p className="text-sm text-black/45">Coming soon — a transparency feed of updates made to your profile.</p>
                        </div>
                    </div>

                    {/* Continue Submission */}
                    {draftInProgress && (
                        <div className="bg-black text-white rounded-2xl p-6 flex flex-col md:flex-row md:items-center justify-between gap-4">
                            <div>
                                <h2 className="text-xs font-semibold tracking-wider uppercase text-white/60 mb-1">Continue Submission</h2>
                                <p className="text-base font-medium">{draftInProgress.project_title}</p>
                                <p className="text-sm text-white/60">You have a draft in progress.</p>
                            </div>
                            <a
                                href={`/submit/${draftInProgress.project_slug}`}
                                className="inline-flex items-center justify-center gap-1.5 bg-white text-black px-5 py-2.5 rounded-lg text-xs font-medium hover:opacity-90 transition-all duration-150 shrink-0"
                            >
                                Continue
                                <ArrowUpRight className="w-3.5 h-3.5" />
                            </a>
                        </div>
                    )}

                    {/* Recent projects summary */}
                    <div className="flex flex-col gap-4">
                        <div className="flex items-center justify-between pb-2 border-b border-black/5">
                            <div className="flex items-center gap-2">
                                <Briefcase className="w-4 h-4 text-black/80" />
                                <h2 className="text-sm font-semibold tracking-wider uppercase text-black/85">Recent Projects</h2>
                            </div>
                            {hasAnyProjects && (
                                <Link to="/portal/projects" className="text-xs font-medium text-black/60 hover:text-black transition-colors duration-150">
                                    View All
                                </Link>
                            )}
                        </div>

                        {!hasAnyProjects ? (
                            <div className="bg-white border border-black/5 rounded-2xl p-12 text-center flex flex-col items-center gap-4 max-w-lg mx-auto my-4">
                                <Briefcase className="w-10 h-10 text-black/25" strokeWidth={1.5} />
                                <h3 className="font-semibold text-lg text-black">No Synced Projects</h3>
                                <p className="text-sm text-black/50 leading-relaxed">
                                    You haven't started any project submissions yet. When an agency invites you or you apply to open briefs, they will show up here dynamically.
                                </p>
                            </div>
                        ) : (
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                {recentProjects.map((proj) => (
                                    <ProjectCard
                                        key={proj.project_id}
                                        project={proj}
                                        theme={
                                            projects.shortlisted.includes(proj)
                                                ? "shortlisted"
                                                : projects.completed.includes(proj)
                                                    ? "completed"
                                                    : "ongoing"
                                        }
                                    />
                                ))}
                            </div>
                        )}
                    </div>

                    {/* Quick actions */}
                    <div className="flex flex-col gap-4">
                        <div className="flex items-center gap-2 pb-2 border-b border-black/5">
                            <ListChecks className="w-4 h-4 text-black/80" />
                            <h2 className="text-sm font-semibold tracking-wider uppercase text-black/85">Quick Actions</h2>
                        </div>
                        <div className="flex flex-wrap gap-3">
                            <Link
                                to="/portal/profile"
                                className="inline-flex items-center gap-1.5 bg-white border border-black/10 hover:border-black/30 px-4 py-2.5 rounded-lg text-xs font-medium transition-all duration-150"
                            >
                                <Edit3 className="w-3.5 h-3.5" />
                                Edit Profile
                            </Link>
                            <Link
                                to="/portal/projects"
                                className="inline-flex items-center gap-1.5 bg-white border border-black/10 hover:border-black/30 px-4 py-2.5 rounded-lg text-xs font-medium transition-all duration-150"
                            >
                                <Briefcase className="w-3.5 h-3.5" />
                                View All Projects
                            </Link>
                            <Link
                                to="/portal/settings"
                                className="inline-flex items-center gap-1.5 bg-white border border-black/10 hover:border-black/30 px-4 py-2.5 rounded-lg text-xs font-medium transition-all duration-150"
                            >
                                <SettingsIcon className="w-3.5 h-3.5" />
                                Settings
                            </Link>
                        </div>
                    </div>
                </main>
            </div>

            {/* Global Luxury Footer */}
            <footer className="w-full text-center text-[10px] tracking-[0.1em] uppercase text-black/40 py-8 bg-white border-t border-black/5">
                <span>Editorial Fashion Casting Platform · © Talentgram</span>
            </footer>
        </div>
    );
}
