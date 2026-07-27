import React, { useState, useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { ArrowLeft, ArrowUpRight, Calendar, Building2, User, DollarSign, Film, MapPin, Wallet, MessageCircleQuestion, Link2, ImageIcon, Video as VideoIcon, Clapperboard } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { api as axios, portalApi, PORTAL_TOKEN_KEY } from "@/lib/api";
import { getEngagementStatusDetails } from "@/lib/engagementStatus";
import { formatTalentLocation } from "@/lib/sanitize";
import { parseStoredLink } from "@/lib/workLinks";
import MediaGrid from "@/components/shared/MediaGrid";
import FeedbackRow from "@/components/shared/FeedbackRow";

/**
 * Phase 3 item 2 — Project Detail now renders the real canonical submission
 * (see docs/TALENT_DASHBOARD_ARCHITECTURE.md). Structure: Project Header,
 * Submission Status, Project Information, Submission Summary, Quick
 * Actions. "Pending Items" is removed — deferred until Requirement Engine
 * integration, per this task's explicit scope.
 *
 * Data sources, all pre-existing and unmodified:
 *   - GET /public/projects/{slug} — Project Header/Information (unchanged
 *     from Phase 2 item 4).
 *   - GET /portal/projects — Submission Status (unchanged).
 *   - GET /portal/projects/{slug}/submission (Phase 3 item 1) — the ONLY
 *     source for Submission Summary. Nothing here is derived: every value
 *     rendered below is a direct field of the canonical submission
 *     document, exactly as build_talent_submission_view() returns it. No
 *     Requirement Engine, no Readiness Engine, no computed status.
 *
 * "Project Answers" pairs submission.form_data.custom_answers (id -> answer)
 * with project.custom_questions (id -> question text) for a readable label
 * — both pieces of data are already fetched here; this is a direct lookup,
 * not a derived/computed value, and applies no visibility filtering (that's
 * a client-facing concern in _submission_to_client_shape, not relevant to
 * a talent viewing their own answers).
 */
export default function ProjectDetail() {
    const { slug } = useParams();
    const navigate = useNavigate();
    const token = typeof window !== "undefined" ? localStorage.getItem(PORTAL_TOKEN_KEY) : null;

    const [project, setProject] = useState(null);
    const [engagement, setEngagement] = useState(null); // this project's card from /portal/projects
    const [submission, setSubmission] = useState(null); // canonical submission, or null if unavailable
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

                // Submission Summary source — independent fetch, per this
                // task's scope. A 404 here just means no summary to show;
                // it doesn't invalidate the rest of the page.
                try {
                    const subRes = await portalApi.get(`/portal/projects/${slug}/submission`);
                    setSubmission(subRes.data);
                } catch (subErr) {
                    if (subErr?.response?.status !== 404) {
                        console.error("Submission fetch error:", subErr);
                    }
                    setSubmission(null);
                }
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

    // Everything below is read directly off `submission` — no computation,
    // no filtering beyond "does this field/category have a value."
    const fd = submission?.form_data || {};
    const availability = fd.availability || {};
    const budget = fd.budget || {};
    const location = Array.isArray(fd.location) ? fd.location : [];
    const workLinks = Array.isArray(fd.work_links) ? fd.work_links : [];
    const media = Array.isArray(submission?.media) ? submission.media : [];
    const portfolioImages = media.filter((m) => ["image", "indian", "western"].includes(m.category));
    const introVideo = media.filter((m) => m.category === "intro_video");
    const auditionTakes = media.filter((m) => ["take", "take_1", "take_2", "take_3"].includes(m.category));
    const clientFeedback = Array.isArray(submission?.client_feedback) ? submission.client_feedback : [];

    const customAnswers = fd.custom_answers && typeof fd.custom_answers === "object" ? fd.custom_answers : {};
    const questionTextById = {};
    for (const cq of project.custom_questions || []) {
        if (cq?.id && cq?.question) questionTextById[cq.id] = cq.question;
    }
    const answeredQuestions = Object.entries(customAnswers).filter(([, v]) => v !== null && v !== undefined && v !== "");

    const hasSummaryContent =
        !!availability.status ||
        !!budget.status ||
        location.length > 0 ||
        answeredQuestions.length > 0 ||
        workLinks.length > 0 ||
        portfolioImages.length > 0 ||
        introVideo.length > 0 ||
        auditionTakes.length > 0;

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

                {/* Submission Summary — real canonical data, rendered as-is */}
                {hasSummaryContent && (
                    <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-5">
                        <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                            Submission Summary
                        </h2>

                        {(availability.status || budget.status || location.length > 0) && (
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
                                {availability.status && (
                                    <div className="flex items-start gap-2">
                                        <Calendar className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                        <div>
                                            <div className="text-[10px] uppercase tracking-wider text-black/40">Availability</div>
                                            <div className="text-black/80">{availability.status}</div>
                                            {availability.note && <div className="text-black/50 text-xs mt-0.5">{availability.note}</div>}
                                        </div>
                                    </div>
                                )}
                                {budget.status && (
                                    <div className="flex items-start gap-2">
                                        <Wallet className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                        <div>
                                            <div className="text-[10px] uppercase tracking-wider text-black/40">Budget</div>
                                            <div className="text-black/80">{budget.status}</div>
                                            {budget.value && <div className="text-black/50 text-xs mt-0.5">{budget.value}</div>}
                                        </div>
                                    </div>
                                )}
                                {location.length > 0 && (
                                    <div className="flex items-start gap-2">
                                        <MapPin className="w-4 h-4 text-black/40 mt-0.5 shrink-0" />
                                        <div>
                                            <div className="text-[10px] uppercase tracking-wider text-black/40">Current Location</div>
                                            <div className="text-black/80">{formatTalentLocation(location)}</div>
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}

                        {answeredQuestions.length > 0 && (
                            <div className="flex flex-col gap-3">
                                <div className="flex items-center gap-2 text-black/60">
                                    <MessageCircleQuestion className="w-4 h-4" />
                                    <span className="text-[10px] uppercase tracking-wider font-medium">Project Answers</span>
                                </div>
                                <div className="flex flex-col gap-3">
                                    {answeredQuestions.map(([qid, answer]) => (
                                        <div key={qid}>
                                            <div className="text-xs text-black/50">{questionTextById[qid] || qid}</div>
                                            <div className="text-sm text-black/80">{String(answer)}</div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {workLinks.length > 0 && (
                            <div className="flex flex-col gap-2">
                                <div className="flex items-center gap-2 text-black/60">
                                    <Link2 className="w-4 h-4" />
                                    <span className="text-[10px] uppercase tracking-wider font-medium">Work Links</span>
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    {workLinks.map((stored, idx) => {
                                        const { label, url } = parseStoredLink(stored);
                                        return (
                                            <div key={idx} className="flex items-center gap-2 bg-black/[0.03] border border-black/[0.06] rounded-lg px-3 py-2 text-xs">
                                                {label && <span className="text-black/60 font-medium shrink-0 max-w-[140px] truncate">{label}</span>}
                                                <a href={url} target="_blank" rel="noopener noreferrer" className="text-black/50 hover:text-black font-mono truncate flex-1 min-w-0 underline underline-offset-2">
                                                    {url}
                                                </a>
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                        )}

                        {introVideo.length > 0 && (
                            <div className="flex flex-col gap-2">
                                <div className="flex items-center gap-2 text-black/60">
                                    <VideoIcon className="w-4 h-4" />
                                    <span className="text-[10px] uppercase tracking-wider font-medium">Introduction Video</span>
                                </div>
                                <MediaGrid items={introVideo} variant="video" />
                            </div>
                        )}

                        {auditionTakes.length > 0 && (
                            <div className="flex flex-col gap-2">
                                <div className="flex items-center gap-2 text-black/60">
                                    <Clapperboard className="w-4 h-4" />
                                    <span className="text-[10px] uppercase tracking-wider font-medium">Audition Takes</span>
                                </div>
                                <MediaGrid items={auditionTakes} variant="video" />
                            </div>
                        )}

                        {portfolioImages.length > 0 && (
                            <div className="flex flex-col gap-2">
                                <div className="flex items-center gap-2 text-black/60">
                                    <ImageIcon className="w-4 h-4" />
                                    <span className="text-[10px] uppercase tracking-wider font-medium">Portfolio</span>
                                </div>
                                <MediaGrid items={portfolioImages} variant="image" />
                            </div>
                        )}
                    </div>
                )}

                {/* Client Feedback — only ever approved+shared rows, exactly
                    as the canonical submission already returns them. Hidden
                    entirely if none exist. */}
                {clientFeedback.length > 0 && (
                    <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                        <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                            Client Feedback
                        </h2>
                        <div className="flex flex-col gap-3">
                            {clientFeedback.map((fb) => (
                                <FeedbackRow key={fb.id} fb={fb} />
                            ))}
                        </div>
                    </div>
                )}

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
