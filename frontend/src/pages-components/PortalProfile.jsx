import React, { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Sparkles, Instagram, Save, ImageIcon, Video as VideoIcon } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { portalApi, PORTAL_TOKEN_KEY, IMAGE_URL } from "@/lib/api";
import { normalizeInstagramHandle, isVideo, thumbnailUrl, posterUrl } from "@/lib/mediaUtils";
import SkillsSelector from "@/components/SkillsSelector";
import LocationSelector from "@/components/LocationSelector";
import { isoToDisplay } from "@/lib/dob";

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
    const token = typeof window !== "undefined" ? localStorage.getItem(PORTAL_TOKEN_KEY) : null;

    const [profile, setProfile] = useState({
        name: "",
        phone: "",
        // Canonical structured shape ([{city, country}]) — matches
        // TalentIn.location on the master schema and the Phase 1 portal
        // normalization fix. Never a plain string; see LocationSelector below.
        location: [],
        height: "",
        dob: "",
        bio: "",
        instagram_handle: "",
        // Read-only — GET /portal/profile already returns this (enrich_talent),
        // but PUT /portal/profile does not accept it (no schema change made
        // for this task). Displayed in the Social section, not editable here.
        instagram_followers: "",
        work_links: [],
        interested_in: [],
        skills: [],
        // Read-only — same existing GET response, not part of the PUT payload.
        media: [],
    });

    const [linksDraft, setLinksDraft] = useState("");

    const categoryOptions = ["Acting", "Modeling", "Influencer Campaigns"];

    useEffect(() => {
        if (!token) {
            toast.error("Please sign in to access your portal");
            navigate("/");
            return;
        }

        const fetchProfile = async () => {
            try {
                const { data } = await portalApi.get(`/portal/profile`);
                setProfile({
                    name: data.name || "",
                    phone: data.phone || "",
                    location: Array.isArray(data.location) ? data.location : [],
                    height: data.height || "",
                    dob: data.dob || "",
                    bio: data.bio || "",
                    instagram_handle: data.instagram_handle || "",
                    instagram_followers: data.instagram_followers || "",
                    work_links: data.work_links || [],
                    interested_in: data.interested_in || [],
                    skills: data.skills || [],
                    media: Array.isArray(data.media) ? data.media : [],
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
    }, [token, navigate]);

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
            // Target talent is derived from the portal token server-side.
            // `media`/`instagram_followers` are read-only display-only fields
            // (see state comments above) — excluded here since the existing
            // PUT /portal/profile schema doesn't accept them; not sent rather
            // than silently ignored server-side.
            const { media, instagram_followers, ...writable } = profile;
            await portalApi.put("/portal/profile", writable);

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
        <div className="flex-1 bg-[#fafafa] text-black flex flex-col justify-between" data-testid="portal-profile-page">
            <div>
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
                                    <label className="text-xs text-black/60 font-medium">Full Name <span className="text-black/35 font-normal">· managed by Talentgram</span></label>
                                    <input
                                        type="text"
                                        name="name"
                                        value={profile.name || ""}
                                        readOnly
                                        title="This field is managed by Talentgram and can't be edited here."
                                        placeholder="e.g. Elena Rostova"
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-black/[0.03] border border-black/10 rounded-lg text-black/55 cursor-not-allowed focus:outline-none transition duration-150 h-[44px]"
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
                                <div className="flex flex-col gap-1.5 sm:col-span-2">
                                    <label className="text-xs text-black/60 font-medium">City / Location</label>
                                    <LocationSelector
                                        value={profile.location}
                                        onChange={(arr) => setProfile((prev) => ({ ...prev, location: arr }))}
                                        testid="portal-profile-location"
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Height <span className="text-black/35 font-normal">· managed by Talentgram</span></label>
                                    <input
                                        type="text"
                                        name="height"
                                        value={profile.height || ""}
                                        readOnly
                                        title="This field is managed by Talentgram and can't be edited here."
                                        placeholder={"e.g. 5'9\""}
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-black/[0.03] border border-black/10 rounded-lg text-black/55 cursor-not-allowed focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Date of Birth <span className="text-black/35 font-normal">· managed by Talentgram</span></label>
                                    <input
                                        type="text"
                                        name="dob"
                                        value={isoToDisplay(profile.dob) || ""}
                                        readOnly
                                        title="This field is managed by Talentgram and can't be edited here."
                                        placeholder="DD/MM/YYYY"
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-black/[0.03] border border-black/10 rounded-lg text-black/55 cursor-not-allowed focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
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

                        {/* Portfolio — read-only display of existing talent media
                            (image categories). Reuses IMAGE_URL/isVideo from
                            lib/api + lib/mediaUtils; no new upload engine, no
                            new endpoint — this is the same media[] array
                            GET /portal/profile already returns. Upload
                            capability is deferred to a later increment. */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2 flex items-center gap-2">
                                <ImageIcon className="w-3.5 h-3.5" />
                                Portfolio
                            </h2>
                            {(() => {
                                const portfolioImages = profile.media.filter((m) => !isVideo(m));
                                if (portfolioImages.length === 0) {
                                    return <p className="text-xs text-black/40">No portfolio images yet.</p>;
                                }
                                return (
                                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                                        {portfolioImages.map((m) => (
                                            <div key={m.id} className="aspect-square rounded-lg overflow-hidden bg-black/5 border border-black/5">
                                                <img
                                                    src={IMAGE_URL(m)}
                                                    alt={m.category || "Portfolio image"}
                                                    className="w-full h-full object-cover"
                                                />
                                            </div>
                                        ))}
                                    </div>
                                );
                            })()}
                        </div>

                        {/* Media — read-only display of video items (intro
                            video / takes). Same media[] array, same reuse
                            rationale as Portfolio above. */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2 flex items-center gap-2">
                                <VideoIcon className="w-3.5 h-3.5" />
                                Media
                            </h2>
                            {(() => {
                                const videos = profile.media.filter(isVideo);
                                if (videos.length === 0) {
                                    return <p className="text-xs text-black/40">No videos yet.</p>;
                                }
                                return (
                                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                                        {videos.map((m) => (
                                            <div key={m.id} className="aspect-square rounded-lg overflow-hidden bg-black/90 border border-black/5 relative">
                                                {posterUrl(m) || thumbnailUrl(m) ? (
                                                    <img
                                                        src={posterUrl(m) || thumbnailUrl(m)}
                                                        alt={m.category || "Video"}
                                                        className="w-full h-full object-cover opacity-80"
                                                    />
                                                ) : (
                                                    <div className="w-full h-full flex items-center justify-center">
                                                        <VideoIcon className="w-6 h-6 text-white/50" />
                                                    </div>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                );
                            })()}
                        </div>

                        {/* Skills — same SkillsSelector previously nested
                            inside Personal Information, moved to its own
                            section per the target IA. No new component. */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                                Skills
                            </h2>
                            <SkillsSelector
                                selectedSkills={profile.skills || []}
                                onChange={(arr) => setProfile((prev) => ({ ...prev, skills: arr }))}
                            />
                        </div>

                        {/* Social — Instagram Handle moved here from Personal
                            Information (same input, same normalize-on-blur
                            behavior). Instagram Followers is read-only: GET
                            /portal/profile already returns it, but the PUT
                            schema doesn't accept it (no schema change made
                            for this task) — displayed for transparency only. */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                                Social
                            </h2>
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
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
                                <div className="flex flex-col gap-1.5">
                                    <label className="text-xs text-black/60 font-medium">Instagram Followers <span className="text-black/35 font-normal">· managed by Talentgram</span></label>
                                    <input
                                        type="text"
                                        value={profile.instagram_followers || "—"}
                                        readOnly
                                        title="This field is managed by Talentgram and can't be edited here."
                                        style={{ fontSize: "16px" }}
                                        className="px-3 py-2 bg-black/[0.03] border border-black/10 rounded-lg text-black/55 cursor-not-allowed focus:outline-none transition duration-150 h-[44px]"
                                    />
                                </div>
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

                        {/* Work Links — unchanged, only the heading renamed
                            (was "Portfolio & Work Links") now that Portfolio
                            is its own media section above. */}
                        <div className="bg-white border border-black/5 rounded-2xl p-6 flex flex-col gap-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-black/45 border-b border-black/5 pb-2">
                                Work Links
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
