import React, { useState, useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { ArrowLeft, ArrowUpRight, Calendar, Building2, User, DollarSign, Film, AlertCircle } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { api as axios, portalApi, PORTAL_TOKEN_KEY } from "@/lib/api";
import { getEngagementStatusDetails } from "@/lib/engagementStatus";

/**
 * Phase 2 item 4 — Project Detail (see docs/TALENT_DASHBOARD_ARCHITECTURE.md
 * and docs/TALENT_MIGRATION_PLAN.md). New wrapper around the existing
 * submission experience, not a replacement for it: /submit/{slug} remains
 * the canonical editor, reached from this page's Quick Actions.
 *
 * Data sources — both pre-existing, unmodified:
 *   - GET /public/projects/{slug} (submissions.py) — public, no auth,
 *     already used elsewhere; returns project metadata + submission_requirements.
 *   - GET /portal/projects (portal.py) — same call PortalProjects.jsx makes;
 *     find this project's own card in the response for status/decision.
 *
 * Per this task's explicit scope, fetches independently rather than sharing
 * a data layer with PortalHome/PortalProjects (no DashboardDataContext yet).
 *
 * KNOWN GAP, not filled in this step (see the Phase 2 item 4 report):
 * "Pending Items" and "Submission Summary" per the target spec need the
 * submission's actual form_data/media (what's uploaded, availability,
 * budget response, etc.) to be meaningful. No endpoint reachable with the
 * portal_token returns that — GET /public/submissions/{sid} and
 * GET /public/projects/{slug}/submission/me both require a submitter JWT or
 * opaque access_token scoped to one specific project, which the Dashboard
 * doesn't hold for arbitrary engagements. Rather than run the real
 * Requirement Engine against an empty form (which would incorrectly mark
 * an already-submitted talent's requirements as "missing"), these two
 * sections show only the coarse status data that IS already available
 * (draft vs. submitted vs. decision) and are honest about the gap.
 */
export default function ProjectDetail() {
    const { slug } = useParams();
    const navigate = useNavigate();
    const token = typeof window !== "undefined" ? localStorage.getItem(PORTAL_TOKEN_KEY) : null;

    const [project, setProject] = useState(null);
    const [engagement, setEngagement] = useState(null); // this project's card from /portal/projects
    const [theme, setTheme] = useState("ongoing");
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        if (!token) {
            toast.error("Please sign in to access your portal");
            navigate("/");
            return;
        }

        const fetchDetail = async () => {
            try {
                const [projectRes, engagementsRes] = await Promise.all([
                    axios.get(`/public/projects/${slug}`),
                    portalApi.get(`/portal/projects`),
                ]);

                setProject(projectRes.data);

                const groups = engagementsRes.data || {};
                let found = null;
                let foundTheme = "ongoing";
                for (const [groupTheme, list] of Object.entries(groups)) {
                    const match = (list || []).find((p) => p.project_slug === slug);
                    if (match) {
                        found = match;
                        foundTheme = groupTheme === "shortlisted" ? "shortlisted" : groupTheme === "completed" ? "completed" : "ongoing";
                        break;
                    }
                }

                if (!found) {
                    toast.error("This project isn't linked to your account.");
                    navigate("/portal/projects");
                    return;
                }

                setEngagement(found);
                setTheme(foundTheme);
            } catch (err) {
                console.error("Project detail fetch error:", err);
                const status = err?.response?.status;
                if (status === 404) {
                    toast.error("Project not found.");
                    navigate("/portal/projects");
                } else if (status === 401) {
                    localStorage.removeItem(PORTAL_TOKEN_KEY);
                    localStorage.removeItem("talentgram_portal_email");
                    toast.error("Please sign in again.");
                    navigate("/");
                } else {
                    toast.error("Unable to load this project.");
                    navigate("/portal/projects");
                }
            } finally {
                setLoading(false);
            }
        };

        fetchDetail();
    }, [slug, token, navigate]);

    if (loading) {
        return (
            <div className="flex-1 bg-white text-black flex flex-col items-center justify-center py-16">
                <Logo size={64} className="animate-pulse" forceVariant="black" />
                <p className="text-xs text-black/45 uppercase tracking-[0.15em] mt-4">Loading project...</p>
            </div>
        );
    }

    if (!project || !engagement) {
        return null; // already redirected above
    }

    const statusDetails = getEngagementStatusDetails(engagement, theme);
    const isDraft = engagement.status === "draft";

    return (
        <div className="flex-1 bg-[#fafafa] text-black" data-testid="project-detail-page">
            <main className="max-w-3xl mx-auto py-8 px-6 md:py-12 flex flex-col gap-8">
                <Link to="/portal/projects" className="inline-flex items-center gap-1.5 text-xs text-black/60 hover:text-black transition-colors duration-150 w-fit">
                    <ArrowLeft className="w-3.5 h-3.5" />
                    Back to Projects
                </Link>

                {/* Project Header */}
                <div className="flex flex-col gap-2">
                    <h1 className="text-2xl md:text-3xl font-semibold tracking-tight text-black">{project.brand_name}</h1>
                    <div className="flex items-center gap-2">
                        <span className={`w-2 h-2 rounded-full ${statusDetails.color}`} />
                        <span className="text-sm text-black/60 font-medium">{statusDetails.text}</span>
                    </div>
                </div>

                {/* Submission Status */}
                <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-2">
                    <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                        Submission Status
                    </h2>
                    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm text-black/70 pt-1">
                        <span>Status: <strong className="text-black">{statusDetails.text}</strong></span>
                        {engagement.decision && engagement.decision !== "pending" && (
                            <span>Decision: <strong className="text-black capitalize">{engagement.decision.replace(/_/g, " ")}</strong></span>
                        )}
                        {engagement.updated_at && (
                            <span>Last updated: <strong className="text-black">{new Date(engagement.updated_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</strong></span>
                        )}
                    </div>
                </div>

                {/* Project Information */}
                <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                    <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                        Project Information
                    </h2>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm">
                        {project.shoot_dates && (
                            <div className="flex items-start gap-2">
                                <Calendar className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                <div>
                                    <div className="text-[10px] uppercase tracking-wider text-black/40">Shoot Dates</div>
                                    <div className="text-black/80">{project.shoot_dates}</div>
                                </div>
                            </div>
                        )}
                        {project.production_house && (
                            <div className="flex items-start gap-2">
                                <Building2 className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                <div>
                                    <div className="text-[10px] uppercase tracking-wider text-black/40">Production House</div>
                                    <div className="text-black/80">{project.production_house}</div>
                                </div>
                            </div>
                        )}
                        {project.director && (
                            <div className="flex items-start gap-2">
                                <User className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                <div>
                                    <div className="text-[10px] uppercase tracking-wider text-black/40">Director</div>
                                    <div className="text-black/80">{project.director}</div>
                                </div>
                            </div>
                        )}
                        {!project.hide_budget_from_talent && project.budget_per_day && (
                            <div className="flex items-start gap-2">
                                <DollarSign className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                <div>
                                    <div className="text-[10px] uppercase tracking-wider text-black/40">Budget / Day</div>
                                    <div className="text-black/80">{project.budget_per_day}</div>
                                </div>
                            </div>
                        )}
                        {project.medium_usage && (
                            <div className="flex items-start gap-2">
                                <Film className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                <div>
                                    <div className="text-[10px] uppercase tracking-wider text-black/40">Medium / Usage</div>
                                    <div className="text-black/80">{project.medium_usage}</div>
                                </div>
                            </div>
                        )}
                    </div>
                    {!project.shoot_dates && !project.production_house && !project.director && !project.medium_usage && (
                        <p className="text-xs text-black/40">No additional project details provided.</p>
                    )}
                </div>

                {/* Pending Items — coarse status only, see file header for why */}
                <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-2">
                    <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                        Pending Items
                    </h2>
                    <div className="flex items-start gap-2 pt-1">
                        <AlertCircle className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                        <p className="text-sm text-black/60">
                            {isDraft
                                ? "Your submission is still in progress. Open it to see exactly what's still needed."
                                : "Your submission has been received — open it to review what you submitted."}
                        </p>
                    </div>
                </div>

                {/* Submission Summary — coarse, see file header for why */}
                <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-2">
                    <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                        Submission Summary
                    </h2>
                    <p className="text-sm text-black/60 pt-1">
                        A detailed breakdown (media uploaded, availability, budget response) isn't available here yet — open your submission to see the full picture.
                    </p>
                </div>

                {/* Quick Actions */}
                <div className="flex flex-col gap-3 sm:flex-row">
                    <a
                        href={`/submit/${slug}`}
                        className="flex-1 inline-flex items-center justify-center gap-2 bg-black text-white px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 transition-all duration-150"
                    >
                        {isDraft ? "Continue Submission" : "Open Submission"}
                        <ArrowUpRight className="w-4 h-4" />
                    </a>
                </div>
            </main>
        </div>
    );
}
