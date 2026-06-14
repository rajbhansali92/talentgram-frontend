import React, { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { ArrowLeft, Sparkles, Instagram, Save } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { api as axios } from "@/lib/api";
import { normalizeInstagramHandle } from "@/lib/mediaUtils";
import SkillsSelector from "@/components/SkillsSelector";

// ---------------------------------------------------------------------------
// Work-links helpers (shared with ApplicationPage)
// ---------------------------------------------------------------------------
const WORK_LINK_URL_RE = /https?:\/\/[^\s]+/;

function parseStoredLink(stored) {
    if (typeof stored === "string" && stored.includes(" || ")) {
        const idx = stored.indexOf(" || ");
        const url = stored.slice(idx + 4).trim().replace(/[.,;:!?)\]>]+$/, "");
        return { label: stored.slice(0, idx).trim(), url };
    }
    const url = (stored || "").replace(/[.,;:!?)\]>]+$/, "");
    return { label: "", url };
}


function parseWorkLinksText(text) {
    return text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
            const match = WORK_LINK_URL_RE.exec(line);
            if (!match) return null;
            // Strip trailing punctuation that may have been captured by the greedy [^\s]+ match
            const url = match[0].replace(/[.,;:!?)\]>]+$/, "");
            const before = line.slice(0, match.index).replace(new RegExp("[-:" + "\\s" + "|]+$"), "").trim();
            return before ? `${before} || ${url}` : url;
        })
        .filter(Boolean);
}


function linksToText(links) {
    return (links || [])
        .map((w) => {
            const { label, url } = parseStoredLink(w);
            return label ? `${label} - ${url}` : url;
        })
        .join("\n");
}

export default function PortalProfile() {
    const navigate = useNavigate();
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const email = localStorage.getItem("talentgram_portal_email");

    const [profile, setProfile] = useState({
        name: "",
        phone: "",
        location: "",
        height: "",
        dob: "",
        bio: "",
        instagram_handle: "",
        work_links: [],
        interested_in: [],
        skills: [],
    });

    const [linksDraft, setLinksDraft] = useState("");

    const categoryOptions = ["Acting", "Modeling", "Influencer Campaigns"];

    useEffect(() => {
        if (!email) {
            toast.error("Please sign in to access your portal");
            navigate("/");
            return;
        }

        const fetchProfile = async () => {
            try {
                const { data } = await axios.get(`/portal/profile?email=${encodeURIComponent(email)}`);
                setProfile({
                    name: data.name || "",
                    phone: data.phone || "",
                    location: data.location || "",
                    height: data.height || "",
                    dob: data.dob || "",
                    bio: data.bio || "",
                    instagram_handle: data.instagram_handle || "",
                    work_links: data.work_links || [],
                    interested_in: data.interested_in || [],
                    skills: data.skills || [],
                });
                setLinksDraft(linksToText(data.work_links || []));
            } catch (err) {
                console.error("Fetch profile error:", err);
                toast.error("Unable to load profile");
                navigate("/portal/home");
            } finally {
                setLoading(false);
            }
        };

        fetchProfile();
    }, [email, navigate]);

    const handleFieldChange = (e) => {
        const { name, value } = e.target;
        setProfile((prev) => ({ ...prev, [name]: value }));
    };

    const handleCategoryToggle = (category) => {
        setProfile((prev) => {
            const current = [...prev.interested_in];
            const index = current.indexOf(category);
            if (index > -1) {
                current.splice(index, 1);
            } else {
                current.push(category);
            }
            return { ...prev, interested_in: current };
        });
    };

    const handleLinksTextChange = (text) => {
        setLinksDraft(text);
        setProfile((prev) => ({ ...prev, work_links: parseWorkLinksText(text) }));
    };

    const handleSaveProfile = async (e) => {
        e.preventDefault();
        if (!profile.name.trim()) {
            toast.error("Full name is required");
            return;
        }

        setSaving(true);
        try {
            await axios.put("/portal/profile", {
                email,
                ...profile,
            });

            toast.success("Profile saved and synchronized globally!");
            navigate("/portal/home");
        } catch (err) {
            console.error("Save profile error:", err);
            toast.error("An error occurred. Please try again.");
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <div className="min-h-dvh bg-white text-black flex flex-col items-center justify-center">
                <Logo size={80} className="animate-pulse" forceVariant="black" />
                <p className="text-xs text-black/45 uppercase tracking-[0.15em] mt-4">Loading Profile Editor...</p>
            </div>
        );
    }

    return (
        <div className="min-h-dvh bg-[#fafafa] text-black flex flex-col justify-between" data-testid="portal-profile-page">
            <div>
                {/* Global Luxury Header */}
                <header className="bg-white border-b border-black/5 px-6 py-4 flex items-center justify-between">
                    <Link to="/portal/home" className="flex items-center gap-2">
                        <Logo size={64} forceVariant="black" />
                        <span className="text-[10px] tracking-[0.12em] uppercase text-black/40 font-medium">Profile Settings</span>
                    </Link>
                    <Link 
                        to="/portal/home"
                        className="inline-flex items-center gap-1.5 text-xs text-black/60 hover:text-black transition-colors duration-150 px-3 py-1.5 border border-black/10 rounded-lg hover:border-black/30"
                    >
                        <ArrowLeft className="w-3.5 h-3.5" />
                        Back to Portal
                    </Link>
                </header>

                <main className="max-w-2xl mx-auto py-8 md:py-12 px-6">
                    <div className="flex flex-col gap-6 mb-8 text-left">
                        <h1 className="text-2xl md:text-3xl font-semibold tracking-tight text-black">
                            Profile Management
                        </h1>
                        <p className="text-sm text-black/50">
                            Keep your details updated. Changes made here will automatically synchronize with your active applications and the global casting database.
                        </p>
                    </div>

                    <form onSubmit={handleSaveProfile} className="flex flex-col gap-8 text-left">
                        {/* 1. Basic Info */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-5">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                                Personal Information
                            </h2>

                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Full Name *</label>
                                    <input
                                        type="text"
                                        name="name"
                                        value={profile.name || ""}
                                        onChange={handleFieldChange}
                                        placeholder="e.g. Elena Rostova"
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 h-[44px]"
                                        required
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Phone Number</label>
                                    <input
                                        type="tel"
                                        name="phone"
                                        value={profile.phone || ""}
                                        onChange={handleFieldChange}
                                        placeholder="e.g. +91 99999 88888"
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">City / Location</label>
                                    <input
                                        type="text"
                                        name="location"
                                        value={profile.location || ""}
                                        onChange={handleFieldChange}
                                        placeholder="e.g. Mumbai, IN"
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Height (e.g. 5'8")</label>
                                    <input
                                        type="text"
                                        name="height"
                                        value={profile.height || ""}
                                        onChange={handleFieldChange}
                                        placeholder={"e.g. 5'9\""}
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Date of Birth (YYYY-MM-DD)</label>
                                    <input
                                        type="text"
                                        name="dob"
                                        value={profile.dob || ""}
                                        onChange={handleFieldChange}
                                        placeholder="YYYY-MM-DD"
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Instagram Handle</label>
                                    <div className="relative">
                                        <Instagram className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-black/45" />
                                        <input
                                            type="text"
                                            name="instagram_handle"
                                            value={profile.instagram_handle || ""}
                                            onChange={handleFieldChange}
                                            onBlur={() => {
                                                if (profile.instagram_handle) {
                                                    setProfile((prev) => ({
                                                        ...prev,
                                                        instagram_handle: normalizeInstagramHandle(profile.instagram_handle)
                                                    }));
                                                }
                                            }}
                                            placeholder="e.g. elenarostova"
                                            style={{ fontSize: "16px" }}
                                            className="w-full pl-9 pr-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 h-[44px]"
                                        />
                                    </div>
                                </div>
                            </div>

                            <div className="flex flex-col gap-1.5 mt-2">
                                <label className="text-xs text-black/60 font-medium">Skills & Special Abilities</label>
                                <SkillsSelector
                                    selectedSkills={profile.skills || []}
                                    onChange={(arr) => setProfile((prev) => ({ ...prev, skills: arr }))}
                                />
                            </div>

                            <div className="flex flex-col gap-1.5">
                                <label className="text-xs text-black/60 font-medium">Bio / Introduction</label>
                                <textarea
                                    name="bio"
                                    value={profile.bio || ""}
                                    onChange={handleFieldChange}
                                    placeholder="Share a short statement about your experience and background..."
                                    rows={3}
                                    style={{ fontSize: "16px" }}
                                    className="px-3 py-2 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 resize-none"
                                />
                            </div>
                        </div>


                        {/* 2. Categories Selection */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                                Work Categories
                            </h2>
                            <p className="text-xs text-black/40">Select the categories you want to be matched with:</p>

                            <div className="flex flex-wrap gap-2.5 mt-1">
                                {categoryOptions.map((cat) => {
                                    const active = profile.interested_in.includes(cat);
                                    return (
                                        <button
                                            type="button"
                                            key={cat}
                                            onClick={() => handleCategoryToggle(cat)}
                                            className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-medium border transition-all duration-150 ${
                                                active 
                                                    ? "bg-black text-white border-black" 
                                                    : "bg-white border-black/10 text-black hover:border-black/35"
                                            }`}
                                        >
                                            <Sparkles className="w-3.5 h-3.5" />
                                            {cat}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>

                        {/* 3. Portfolio Links */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                                Portfolio &amp; Work Links
                            </h2>
                            <p className="text-xs text-black/40">
                                Paste all your work links below, one per line. Include a label before the URL
                                to identify each project (e.g. &ldquo;Puma Campaign - https://…&rdquo;).
                            </p>

                            <textarea
                                value={linksDraft}
                                onChange={(e) => handleLinksTextChange(e.target.value)}
                                rows={6}
                                placeholder={
                                    "Puma Campaign - https://instagram.com/reel/abc\n" +
                                    "Pepsi - https://youtu.be/xyz\n" +
                                    "https://vimeo.com/showreel"
                                }
                                style={{ fontSize: "14px" }}
                                className="w-full px-3 py-3 bg-white border border-black/15 rounded-lg text-black focus:border-black/50 focus:outline-none transition duration-150 resize-y font-mono leading-relaxed"
                            />

                            {/* Live counter */}
                            <div className="flex items-center gap-2">
                                <span
                                    className={`text-[11px] font-mono px-2.5 py-0.5 rounded-full border ${
                                        profile.work_links.length > 0
                                            ? "text-emerald-700 bg-emerald-50 border-emerald-200"
                                            : "text-black/35 bg-black/5 border-black/10"
                                    }`}
                                >
                                    Detected Links: {profile.work_links.length}
                                </span>
                                {profile.work_links.length > 0 && (
                                    <span className="text-[10px] text-black/40 truncate">
                                        {profile.work_links
                                            .map((s) => parseStoredLink(s).label || "Unlabeled")
                                            .join(" · ")}
                                    </span>
                                )}
                            </div>

                            {/* Preview of parsed links */}
                            {profile.work_links.length > 0 && (
                                <div className="flex flex-col gap-1.5">
                                    {profile.work_links.map((stored, idx) => {
                                        const { label, url } = parseStoredLink(stored);
                                        return (
                                            <div
                                                key={idx}
                                                className="flex items-center gap-2 bg-black/[0.03] border border-black/[0.06] rounded-lg px-3 py-2 text-xs"
                                            >
                                                {label && (
                                                    <span className="text-black/60 font-medium shrink-0 max-w-[140px] truncate">
                                                        {label}
                                                    </span>
                                                )}
                                                <a
                                                    href={url}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="text-black/50 hover:text-black font-mono truncate flex-1 min-w-0 underline underline-offset-2"
                                                >
                                                    {url}
                                                </a>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>

                        {/* CTA Save */}
                        <div className="flex items-center gap-4">
                            <button
                                type="submit"
                                disabled={saving}
                                className="flex-1 inline-flex items-center justify-center gap-2 bg-black text-white px-6 py-3.5 rounded-lg text-sm font-medium hover:opacity-90 active:scale-[0.99] transition-all duration-150 h-[48px]"
                            >
                                <Save className="w-4 h-4" />
                                {saving ? "Saving Changes..." : "Save and Synchronize"}
                            </button>
                            <Link
                                to="/portal/home"
                                className="inline-flex items-center justify-center border border-black/15 hover:border-black/40 text-black/80 px-6 py-3.5 rounded-lg text-sm transition-all duration-150 h-[48px]"
                            >
                                Cancel
                            </Link>
                        </div>
                    </form>
                </main>
            </div>

            {/* Global Luxury Footer */}
            <footer className="w-full text-center text-[10px] tracking-[0.1em] uppercase text-black/40 py-8 bg-white border-t border-black/5">
                <span>Editorial Fashion Casting Platform · © Talentgram</span>
            </footer>
        </div>
    );
}
