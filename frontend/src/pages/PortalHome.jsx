import React, { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { User, MapPin, ArrowUpRight, LogOut, Edit3, Briefcase, Award, CheckCircle } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";

export default function PortalHome() {
    const navigate = useNavigate();
    const [talent, setTalent] = useState(null);
    const [projects, setProjects] = useState({ ongoing: [], shortlisted: [], completed: [] });
    const [loading, setLoading] = useState(true);
    const email = localStorage.getItem("talentgram_portal_email");

    useEffect(() => {
        if (!email) {
            toast.error("Please sign in to access your portal");
            navigate("/");
            return;
        }

        const fetchPortalData = async () => {
            try {
                // Fetch profile
                const profileRes = await fetch(`/api/portal/profile?email=${encodeURIComponent(email)}`);
                if (profileRes.status === 404) {
                    toast.error("Your profile session has expired or was removed.");
                    localStorage.removeItem("talentgram_portal_email");
                    navigate("/");
                    return;
                }
                if (!profileRes.ok) {
                    throw new Error("Failed to load profile");
                }
                const profileData = await profileRes.json();
                setTalent(profileData);

                // Fetch synced projects
                const projectsRes = await fetch(`/api/portal/projects?email=${encodeURIComponent(email)}`);
                if (projectsRes.ok) {
                    const projectsData = await projectsRes.json();
                    setProjects(projectsData);
                }
            } catch (err) {
                console.error("Portal fetch error:", err);
                toast.error("Unable to load your profile. Please sign in again.");
                localStorage.removeItem("talentgram_portal_email");
                navigate("/");
            } finally {
                setLoading(false);
            }
        };


        fetchPortalData();
    }, [email, navigate]);

    const handleSignOut = () => {
        localStorage.removeItem("talentgram_portal_email");
        toast.success("Signed out successfully");
        navigate("/");
    };

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

    return (
        <div className="min-h-dvh bg-[#fafafa] text-black flex flex-col justify-between" data-testid="portal-home-page">
            <div>
                {/* Global Luxury Header */}
                <header className="bg-white border-b border-black/5 px-6 md:px-12 py-4 flex items-center justify-between">
                    <Link to="/portal/home" className="flex items-center gap-2">
                        <Logo size={64} forceVariant="black" />
                        <span className="text-[10px] tracking-[0.12em] uppercase text-black/40 font-medium">Talent Portal</span>
                    </Link>
                    <button 
                        onClick={handleSignOut}
                        className="inline-flex items-center gap-1.5 text-xs text-black/60 hover:text-black transition-colors duration-150 px-3 py-1.5 border border-black/10 rounded-lg hover:border-black/30"
                    >
                        <LogOut className="w-3.5 h-3.5" />
                        Sign Out
                    </button>
                </header>

                {/* Profile Summary Card */}
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
                                <h1 className="text-2xl md:text-3xl font-semibold text-black tracking-tight">{talent?.name}</h1>
                                <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-black/55">
                                    {talent?.location && (
                                        <span className="flex items-center gap-1">
                                            <MapPin className="w-3.5 h-3.5 text-black/40" />
                                            {talent.location}
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

                {/* Main Content Layout */}
                <main className="max-w-4xl mx-auto py-8 px-6 md:py-12 flex flex-col gap-10">
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

            {/* Global Luxury Footer */}
            <footer className="w-full text-center text-[10px] tracking-[0.1em] uppercase text-black/40 py-8 bg-white border-t border-black/5">
                <span>Editorial Fashion Casting Platform · © Talentgram</span>
            </footer>
        </div>
    );
}

function ProjectCard({ project, theme }) {
    const navigate = useNavigate();

    const getStatusDetails = () => {
        if (theme === "shortlisted") {
            return { color: "bg-amber-500", text: "Shortlisted" };
        }
        if (theme === "completed") {
            return { color: "bg-green-600", text: "Completed" };
        }
        
        // Ongoing statuses
        if (project.status === "draft") {
            return { color: "bg-black/25", text: "Draft / Continuation" };
        }
        if (project.status === "submitted" || project.status === "updated") {
            return { color: "bg-blue-500", text: "Awaiting Review" };
        }
        return { color: "bg-black/40", text: "Active" };
    };

    const statusDetails = getStatusDetails();
    const formattedDate = project.updated_at 
        ? new Date(project.updated_at).toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
              year: "numeric",
          })
        : "N/A";

    const handleOpenProject = () => {
        navigate(`/submit/${project.project_slug}`);
    };

    return (
        <div className="bg-white border border-black/5 hover:border-black/15 hover:shadow-[0_4px_20px_rgba(0,0,0,0.02)] transition-all duration-200 rounded-xl p-5 flex flex-col justify-between gap-4">
            <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                    <span className="text-[10px] text-black/45 tracking-wider uppercase font-medium">
                        Audition
                    </span>
                    <span className="text-[10px] text-black/45">{formattedDate}</span>
                </div>
                <h3 className="font-semibold text-base text-black tracking-tight mt-0.5">
                    {project.project_title}
                </h3>
                <div className="flex items-center gap-2 mt-1">
                    <span className={`w-2 h-2 rounded-full ${statusDetails.color}`} />
                    <span className="text-xs text-black/60 font-medium">{statusDetails.text}</span>
                </div>
            </div>

            <button
                onClick={handleOpenProject}
                className="w-full inline-flex items-center justify-center gap-1 bg-[#fafafa] border border-black/10 hover:bg-black hover:text-white hover:border-black py-2.5 px-4 rounded-lg text-xs font-medium transition-all duration-150"
            >
                Open Project Submission
                <ArrowUpRight className="w-3.5 h-3.5" />
            </button>
        </div>
    );
}
