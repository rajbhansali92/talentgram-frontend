'use client';

import React, { useEffect, useMemo, useRef, useState } from "react";
import { api as axios, PORTAL_TOKEN_KEY } from "@/lib/api";
import {
    appDraftKey,
    normEmail,
    newestLocalDraft,
    LEGACY_APP_DRAFT_KEY as LEGACY_LS_KEY,
    APP_DRAFT_TTL_MS as DRAFT_TTL_MS,
} from "@/lib/applyDraft";
import { toast } from "sonner";
import { useUploadManager } from "@/context/UploadManagerContext";
import { useStickyFooterHeightVar } from "@/hooks/useStickyFooterHeightVar";
import LazyVideoPlayer from "@/components/LazyVideoPlayer";
import { thumbnailUrl, posterUrl, normalizeInstagramHandle } from "@/lib/mediaUtils";
import Logo from "@/components/Logo";
import SkillsSelector from "@/components/SkillsSelector";
import LocationSelector from "@/components/LocationSelector";
import DobInput from "@/components/DobInput";
import {
    Select,
    SelectContent,
    SelectGroup,
    SelectItem,
    SelectLabel,
    SelectSeparator,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    Upload,
    Video,
    Camera,
    Check,
    Trash2,
    Loader2,
    X,
    Sparkles,
    ArrowRight,
    Mail,
    Plus,
    User,
    ChevronRight,
} from "lucide-react";
import {
    HEIGHT_OPTIONS,
    GENDER_OPTIONS,
    ETHNICITY_OPTIONS,
    FOLLOWER_TIERS,
    calcAge,
} from "@/lib/talentSchema";


// Phase 3: per-category portfolio image cap. Each of `image`/`indian`/
// `western` is independently capped at this value, NOT combined.
const MAX_IMAGES_PER_CATEGORY = 10;
// Apply-draft local storage ownership (key scheme + migration helpers) lives
// in the shared module so ApplicationPage and GoogleCallback derive identical
// per-email keys. Aliased to the names used throughout this file.

export default function ApplicationPage() {
    // Flow: identity gate → sections 1-4 → submitted
    const [started, setStarted] = useState(false);
    const [aid, setAid] = useState(null);
    const [token, setToken] = useState(null);
    const [finalized, setFinalized] = useState(false);
    const [hydrating, setHydrating] = useState(false);
    const [isEditMode, setIsEditMode] = useState(false);
    const [basics, setBasics] = useState({
        first_name: "",
        last_name: "",
        email: "",
        phone: "",
        alternate_contact_number: "",
    });
    const [form, setForm] = useState({
        dob: "",
        height: "",
        gender: "",
        ethnicity: "",
        location: [],
        instagram_handle: "",
        instagram_followers: "",
        bio: "",
        work_links: [],
        interested_in: [],
        skills: [],
    });
    const [media, setMedia] = useState([]);
    const { activeUploads, uploadFile } = useUploadManager();
    const stickyFooterRef = useRef(null);
    useStickyFooterHeightVar(stickyFooterRef, "--tg-sticky-cta-h");
    const [saving, setSaving] = useState(false);
    // Email-first gate (does NOT touch /api/public/apply, validation, or schema —
    // pure conditional rendering inside the existing identity screen).
    const [emailGateUnlocked, setEmailGateUnlocked] = useState(false);
    const [applyPrefill, setApplyPrefill] = useState(null); // {data}
    const [applyPrefillTried, setApplyPrefillTried] = useState("");
    const imgRef = useRef();
    const videoRef = useRef();
    const indianRef = useRef();
    const westernRef = useRef();

    // P0-1 Inline validation: per-field error messages + refs for scroll/focus.
    const [errors, setErrors] = useState({});
    const locationRef = useRef();
    const igHandleRef = useRef();
    const igFollowersRef = useRef();
    const mediaRef = useRef();
    const identityRef = useRef();
    const clearError = (key) =>
        setErrors((prev) => (prev[key] ? { ...prev, [key]: undefined } : prev));

    // Inline Portal Gateway states
    const [gatewayEmail, setGatewayEmail] = useState("");
    const [gatewayLoading, setGatewayLoading] = useState(false);
    const [gatewayRecognition, setGatewayRecognition] = useState(null);
    const [otpSent, setOtpSent] = useState(false);
    const [otpValue, setOtpValue] = useState("");
    const [otpLoading, setOtpLoading] = useState(false);
    const [otpResending, setOtpResending] = useState(false);

    // Onboarding requirements and profile_id states
    const [profileId, setProfileId] = useState(null);
    const [requirements, setRequirements] = useState({
        profile_requirements: { name: "required", location: "required", instagram_handle: "required", instagram_followers: "required" },
        portfolio_requirements: { portfolio: "required", indian: "required", western: "required", video: "required" }
    });

    // Extract profile parameter on mount and fetch config
    useEffect(() => {
        const urlParams = new URLSearchParams(window.location.search);
        const queryProfileId = urlParams.get("profile");
        if (queryProfileId) {
            setProfileId(queryProfileId);
        }

        const fetchConfig = async () => {
            try {
                const params = queryProfileId ? { profile: queryProfileId } : {};
                const { data } = await axios.get("/public/onboarding-config", { params });
                if (data) {
                    setRequirements(data);
                }
            } catch (err) {
                console.error("Failed to fetch onboarding configuration:", err);
            }
        };
        fetchConfig();
    }, []);

    // Restore local draft
    useEffect(() => {
        // Google authentication checks
        const googleEmail = localStorage.getItem("talentgram_google_email");
        if (googleEmail) {
            const profileDataStr = localStorage.getItem("talentgram_google_profile_data");
            const avatar = localStorage.getItem("talentgram_google_avatar") || "";
            
            if (profileDataStr) {
                // Existing Google-authenticated talent
                const profileData = JSON.parse(profileDataStr);
                setGatewayRecognition({
                    name: `${profileData.first_name || ""} ${profileData.last_name || ""}`.trim(),
                    email: googleEmail,
                    location: profileData.location || [],
                    image_url: avatar || profileData.image_url || profileData.cover_url || "",
                    isGoogle: true
                });
                setEmailGateUnlocked(false);
            } else {
                // New Google-authenticated talent
                const first = localStorage.getItem("talentgram_google_first_name") || "";
                const last = localStorage.getItem("talentgram_google_last_name") || "";
                setBasics((b) => ({
                    ...b,
                    email: googleEmail,
                    first_name: b.first_name || first,
                    last_name: b.last_name || last,
                }));
                setEmailGateUnlocked(true);
                
                // Show welcome banner once
                const onboardKey = "tg_onboard_shown_apply";
                if (!localStorage.getItem(onboardKey)) {
                    toast.success("Welcome to Talentgram! Let's create your profile");
                    localStorage.setItem(onboardKey, "true");
                }
            }
            return;
        }

        const urlParams = new URLSearchParams(window.location.search);
        const queryEmail = urlParams.get("email");
        const portalEmail = localStorage.getItem("talentgram_portal_email");
        const emailToPrefill = queryEmail || portalEmail;
        if (emailToPrefill && !basics.email) {
            const formatted = emailToPrefill.trim().toLowerCase();
            setBasics((b) => ({ ...b, email: formatted }));
            setApplyPrefillTried(formatted);
            setEmailGateUnlocked(true);
            
            // Trigger pre-fill lookup immediately
            (async () => {
                try {
                    const { data } = await axios.get(
                        `/public/prefill?email=${encodeURIComponent(formatted)}`,
                    );
                    if (data && Object.keys(data).length > 0 && data.first_name) {
                        setBasics((b) => ({
                            ...b,
                            first_name: b.first_name || data.first_name || "",
                            last_name: b.last_name || data.last_name || "",
                            phone: b.phone || data.phone || "",
                        }));
                        setForm((f) => ({
                            ...f,
                            dob: f.dob || data.dob || "",
                            age: f.age || (data.age != null ? String(data.age) : ""),
                            height: f.height || data.height || "",
                            location: (f.location && f.location.length) ? f.location : (data.location || []),
                            gender: f.gender || data.gender || "",
                            ethnicity: f.ethnicity || data.ethnicity || "",
                            bio: f.bio || data.bio || "",
                            instagram_handle: f.instagram_handle || data.instagram_handle || "",
                            instagram_followers:
                                f.instagram_followers || data.instagram_followers || "",
                            work_links:
                                f.work_links && f.work_links.length
                                    ? f.work_links
                                    : (data.work_links || []),
                            skills:
                                f.skills && f.skills.length
                                    ? f.skills
                                    : (data.skills || []),
                        }));
                    }
                } catch (e) {
                    console.error("Auto prefill lookup failed:", e);
                }
            })();
        }

        // ── Resume ONLY the draft that belongs to the resolved identity ─────
        // Identity precedence (Table A): explicit ?email= invite → verified
        // portal session. (A Google session returns above.) The most-recent
        // local draft is consulted only when NONE of those exist, so a stale
        // draft can never override an invite context.
        const portalToken = localStorage.getItem(PORTAL_TOKEN_KEY);
        const intendedEmail =
            normEmail(queryEmail) || (portalToken ? normEmail(portalEmail) : "");

        let restoreKey = null;
        let raw = null;
        if (intendedEmail) {
            restoreKey = appDraftKey(intendedEmail);
            raw = localStorage.getItem(restoreKey);
            if (!raw) {
                // One-time legacy migration — adopt the old global slot ONLY
                // when its stored email matches the resolved identity. TTL/
                // savedAt are preserved by copying the value verbatim.
                const legacyRaw = localStorage.getItem(LEGACY_LS_KEY);
                if (legacyRaw) {
                    try {
                        const legacy = JSON.parse(legacyRaw);
                        if (normEmail(legacy?.basics?.email) === intendedEmail) {
                            localStorage.setItem(restoreKey, legacyRaw);
                            localStorage.removeItem(LEGACY_LS_KEY);
                            raw = legacyRaw;
                            console.log("[ApplicationPage] Migrated legacy draft → per-email slot.");
                        }
                    } catch (e) {
                        console.error("[ApplicationPage] Legacy draft parse failed:", e);
                    }
                }
            }
        } else {
            // No invite/session identity — resume the most recently saved draft
            // (personal-device convenience). This branch only runs when ?email=
            // and the portal session are both absent, so it cannot override an
            // invite. A legacy slot is normalized into a per-email slot on adopt.
            const found = newestLocalDraft();
            if (found) {
                raw = found.raw;
                restoreKey = found.key;
                if (restoreKey === LEGACY_LS_KEY) {
                    try {
                        const legacy = JSON.parse(found.raw);
                        const em = normEmail(legacy?.basics?.email);
                        if (em) {
                            restoreKey = appDraftKey(em);
                            localStorage.setItem(restoreKey, found.raw);
                            localStorage.removeItem(LEGACY_LS_KEY);
                        }
                    } catch (e) {
                        console.error("[ApplicationPage] Legacy draft parse failed:", e);
                    }
                }
            }
        }

        if (!raw) return;
        try {
            const saved = JSON.parse(raw);
            console.log("[ApplicationPage] Saved draft found in localstorage on mount:", saved.aid);
            // TTL guard — purge stale drafts (tokens + PII) after DRAFT_TTL_MS.
            if (saved.savedAt && Date.now() - saved.savedAt > DRAFT_TTL_MS) {
                console.warn("[ApplicationPage] Draft has expired. Purging local storage.");
                if (restoreKey) localStorage.removeItem(restoreKey);
                return;
            }
            if (saved.aid && saved.token) {
                console.log("[ApplicationPage] Resuming from draft token:", saved.token.slice(-10));
                setAid(saved.aid);
                setToken(saved.token);
                setStarted(true);
                setBasics(saved.basics || basics);
                setForm(saved.form || form);
                (async () => {
                    setHydrating(true);
                    try {
                        console.log("[ApplicationPage] Fetching active draft details for aid:", saved.aid);
                        const { data } = await axios.get(
                            `/public/apply/${saved.aid}`,
                            { headers: { Authorization: `Bearer ${saved.token}` } },
                        );
                        console.log("[ApplicationPage] Fetch successful. Hydrating form fields.");
                        // Backend is authoritative — it overwrites the locally
                        // cached form_data (Table C precedence).
                        setForm((f) => ({ ...f, ...(data.form_data || {}) }));
                        // Resume also rehydrates `basics` from the persisted
                        // application. `basics` (gate-only fields) is what
                        // finalize validates for name; without this, a resume
                        // that lacks a local `saved.basics` leaves first/last
                        // name empty and finalize blocks on a field that is not
                        // visible on the resumed form.
                        const rfd = data.form_data || {};
                        setBasics((b) => {
                            const talentFirst = data.talent?.first_name || b.first_name || "";
                            const talentLast = data.talent?.last_name || b.last_name || "";
                            const talentPhone = data.talent?.phone || b.phone || "";

                            const first = talentFirst || rfd.first_name || (data.talent_name || "").split(" ")[0] || "";
                            const last = talentLast || rfd.last_name || (data.talent_name || "").split(" ").slice(1).join(" ") || "";
                            const ph = talentPhone || rfd.phone || data.talent_phone || "";
                            const altPh = b.alternate_contact_number || rfd.alternate_contact_number || data.alternate_contact_number || "";

                            const updatedBasics = {
                                ...b,
                                first_name: first.trim(),
                                last_name: last.trim(),
                                phone: ph.trim(),
                                alternate_contact_number: altPh.trim(),
                            };

                            // Save to local storage right here to ensure they persist!
                            const email = normEmail(updatedBasics.email);
                            if (email) {
                                const localVal = {
                                    aid: saved.aid,
                                    token: saved.token,
                                    basics: updatedBasics,
                                    form: { ...((saved && saved.form) || {}), ...rfd },
                                    savedAt: Date.now()
                                };
                                localStorage.setItem(appDraftKey(email), JSON.stringify(localVal));
                            }

                            return updatedBasics;
                        });
                        setMedia(data.media || []);
                        if (data.status === "submitted") {
                            setFinalized(true);
                            setIsEditMode(true);
                        }
                    } catch (err) {
                        console.error("[ApplicationPage] Hydration request failed. Invalid or expired token. Status:", err?.response?.status, err?.response?.data);
                        // expired/invalid token — reset
                        if (restoreKey) localStorage.removeItem(restoreKey);
                        setStarted(false);
                        setAid(null);
                        setToken(null);
                    } finally {
                        setHydrating(false);
                    }
                })();
            }
        } catch (e) {
            console.error("[ApplicationPage] Exception decoding saved draft details:", e);
            if (restoreKey) localStorage.removeItem(restoreKey);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const saveLocal = (patch = {}) => {
        const value = { aid, token, basics, form, ...patch, savedAt: Date.now() };
        // Namespace by the draft's own email so it can only ever be resumed in
        // that identity's context. A started application always carries an
        // email; guard defensively so we never write an anonymous slot.
        const email = normEmail(value.basics?.email);
        if (!email) return;
        localStorage.setItem(appDraftKey(email), JSON.stringify(value));
    };

    const computedAge = useMemo(() => {
        return calcAge(form.dob) ?? null;
    }, [form.dob]);

    // Email-first prefill on blur. Calls EXISTING /api/public/prefill — no
    // backend changes. Decision tree:
    //   • match → show "We found your profile" card; user picks Use/Edit
    //   • miss  → unlock the rest of the gate immediately (silent)
    const tryEmailPrefill = async () => {
        const email = (basics.email || "").trim().toLowerCase();
        if (!email || !email.includes("@")) return;
        if (email === applyPrefillTried) return;
        setApplyPrefillTried(email);
        try {
            const { data } = await axios.get(
                `/public/prefill?email=${encodeURIComponent(email)}`,
            );
            if (data && data.first_name) {
                setApplyPrefill({ data });
            } else {
                setApplyPrefill(null);
                setEmailGateUnlocked(true);
            }
        } catch {
            // 429 / network — silently unlock so the user isn't blocked.
            setApplyPrefill(null);
            setEmailGateUnlocked(true);
        }
    };

    const handleGoogleLogin = () => {
        const clientId = process.env.REACT_APP_GOOGLE_CLIENT_ID || "339414275037-rrm7uugj1t4gq2b02q9r51d9l6m39vbe.apps.googleusercontent.com";
        const redirectUri = `${window.location.origin}/google-callback`;
        const state = "apply";
        const scope = "openid profile email";
        window.location.href = `https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=${clientId}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=${encodeURIComponent(scope)}&state=${encodeURIComponent(state)}`;
    };

    const handleInlineLookup = async (e) => {
        if (e) e.preventDefault();
        if (gatewayLoading) return;
        const trimmedEmail = gatewayEmail.trim().toLowerCase();
        if (!trimmedEmail || !trimmedEmail.includes("@")) {
            toast.error("Please enter a valid email address.");
            return;
        }

        setGatewayLoading(true);
        try {
            await axios.post("/auth/otp/send", { email: trimmedEmail });
            setOtpSent(true);
            toast.success("Verification code sent!");
        } catch (error) {
            console.error("OTP send error:", error);
            toast.error(error?.response?.data?.detail || "Failed to send verification code. Please try again.");
        } finally {
            setGatewayLoading(false);
        }
    };

    const handleVerifyOtp = async (e) => {
        if (e) e.preventDefault();
        if (otpLoading) return;
        const code = otpValue.trim();
        if (code.length !== 6 || !/^\d+$/.test(code)) {
            toast.error("Please enter a valid 6-digit verification code.");
            return;
        }

        setOtpLoading(true);
        try {
            const trimmedEmail = gatewayEmail.trim().toLowerCase();
            const { data } = await axios.post("/auth/otp/verify", {
                email: trimmedEmail,
                otp: code,
                slug: "apply"
            });

            if (data.existing) {
                if (data.token && data.application_id) {
                    // Restore identity fields with priority:
                    // 1. Talent Profile (canonical, db.talents)
                    // 2. Draft Application form_data (set when the draft was
                    //    first created — present even with no talent profile)
                    // 3. Empty string (recovery section below will surface).
                    const draftFd = data.application?.form_data || {};
                    const ref = {
                        aid: data.application_id,
                        token: data.token,
                        basics: {
                            first_name: data.talent?.first_name || draftFd.first_name || "",
                            last_name: data.talent?.last_name || draftFd.last_name || "",
                            email: data.email,
                            phone: data.talent?.phone || draftFd.phone || ""
                        },
                        savedAt: Date.now()
                    };
                    localStorage.setItem(appDraftKey(normEmail(data.email)), JSON.stringify(ref));
                    setAid(data.application_id);
                    setToken(data.token);
                    setBasics(ref.basics);
                    if (data.talent) {
                        setForm((f) => ({ ...f, ...(data.talent.form_data || {}) }));
                    }
                    setStarted(true);
                    toast.success("Welcome back!");
                } else {
                    toast.success("Welcome back!");
                    if (data.talent) {
                        setBasics((b) => ({
                            ...b,
                            first_name: b.first_name || data.talent.first_name || "",
                            last_name: b.last_name || data.talent.last_name || "",
                            phone: b.phone || data.talent.phone || "",
                            email: trimmedEmail
                        }));
                        setForm((f) => ({
                            ...f,
                            dob: f.dob || data.talent.dob || "",
                            age: f.age || (data.talent.age != null ? String(data.talent.age) : ""),
                            height: f.height || data.talent.height || "",
                            location: (f.location && f.location.length) ? f.location : (data.talent.location || []),
                            gender: f.gender || data.talent.gender || "",
                            ethnicity: f.ethnicity || data.talent.ethnicity || "",
                            bio: f.bio || data.talent.bio || "",
                            instagram_handle: f.instagram_handle || data.talent.instagram_handle || "",
                            instagram_followers:
                                f.instagram_followers || data.talent.instagram_followers || "",
                            work_links:
                                f.work_links && f.work_links.length
                                    ? f.work_links
                                    : (data.talent.work_links || []),
                            skills:
                                f.skills && f.skills.length
                                    ? f.skills
                                    : (data.talent.skills || []),
                        }));
                    }
                    setBasics((b) => ({ ...b, email: trimmedEmail }));
                    setEmailGateUnlocked(true);
                }
            } else {
                toast.success("Successfully authenticated. Welcome to Talentgram!");
                setBasics((b) => ({ ...b, email: trimmedEmail }));
                setEmailGateUnlocked(true);
            }

            // OTP proved ownership — persist the portal session token (Path B).
            if (data.portal_token) {
                localStorage.setItem(PORTAL_TOKEN_KEY, data.portal_token);
            }
            localStorage.setItem("talentgram_portal_email", trimmedEmail);
            setOtpSent(false);
        } catch (error) {
            console.error("OTP verify error:", error);
            toast.error(error?.response?.data?.detail || "Invalid or expired verification code.");
        } finally {
            setOtpLoading(false);
        }
    };

    const handleResendOtp = async () => {
        if (otpResending) return;
        const trimmedEmail = gatewayEmail.trim().toLowerCase();
        setOtpResending(true);
        try {
            await axios.post("/auth/otp/send", { email: trimmedEmail });
            toast.success("Verification code resent.");
        } catch (error) {
            console.error("OTP resend error:", error);
            toast.error(error?.response?.data?.detail || "Failed to resend code. Please try again.");
        } finally {
            setOtpResending(false);
        }
    };

    const handleInlineContinue = () => {
        if (!gatewayRecognition || !gatewayRecognition.email) return;
        
        const formatted = gatewayRecognition.email.trim().toLowerCase();
        localStorage.setItem("talentgram_portal_email", formatted);
        setBasics((b) => ({ ...b, email: formatted }));
        setEmailGateUnlocked(true);
        
        if (gatewayRecognition.isGoogle) {
            const profileDataStr = localStorage.getItem("talentgram_google_profile_data");
            if (profileDataStr) {
                const profileData = JSON.parse(profileDataStr);
                setBasics((b) => ({
                    ...b,
                    first_name: b.first_name || profileData.first_name || "",
                    last_name: b.last_name || profileData.last_name || "",
                    phone: b.phone || profileData.phone || "",
                }));
                setForm((f) => ({
                    ...f,
                    dob: f.dob || profileData.dob || "",
                    age: f.age || (profileData.age != null ? String(profileData.age) : ""),
                    height: f.height || profileData.height || "",
                    location: (f.location && f.location.length) ? f.location : (profileData.location || []),
                    gender: f.gender || profileData.gender || "",
                    ethnicity: f.ethnicity || profileData.ethnicity || "",
                    bio: f.bio || profileData.bio || "",
                    instagram_handle: f.instagram_handle || profileData.instagram_handle || "",
                    instagram_followers:
                        f.instagram_followers || profileData.instagram_followers || "",
                    work_links:
                        f.work_links && f.work_links.length
                            ? f.work_links
                            : (profileData.work_links || []),
                    skills:
                        f.skills && f.skills.length
                            ? f.skills
                            : (profileData.skills || []),
                }));
            }
            toast.success(`Welcome back, ${gatewayRecognition.name}!`);
            return;
        }
        
        // Trigger pre-fill lookup immediately so the talent's profile details are auto-loaded
        (async () => {
            try {
                const { data } = await axios.get(
                    `/public/prefill?email=${encodeURIComponent(formatted)}`,
                );
                if (data && Object.keys(data).length > 0 && data.first_name) {
                    setBasics((b) => ({
                        ...b,
                        first_name: b.first_name || data.first_name || "",
                        last_name: b.last_name || data.last_name || "",
                        phone: b.phone || data.phone || "",
                    }));
                    setForm((f) => ({
                        ...f,
                        dob: f.dob || data.dob || "",
                        age: f.age || (data.age != null ? String(data.age) : ""),
                        height: f.height || data.height || "",
                        location: (f.location && f.location.length) ? f.location : (data.location || []),
                        gender: f.gender || data.gender || "",
                        ethnicity: f.ethnicity || data.ethnicity || "",
                        bio: f.bio || data.bio || "",
                        instagram_handle: f.instagram_handle || data.instagram_handle || "",
                        instagram_followers:
                            f.instagram_followers || data.instagram_followers || "",
                        work_links:
                            f.work_links && f.work_links.length
                                ? f.work_links
                                : (data.work_links || []),
                        skills:
                            f.skills && f.skills.length
                                ? f.skills
                                : (data.skills || []),
                    }));
                }
            } catch (e) {
                console.error("Auto prefill lookup failed:", e);
            }
        })();
        
        toast.success(`Welcome back, ${gatewayRecognition.name}!`);
    };

    const handleInlineCancel = () => {
        localStorage.removeItem("talentgram_portal_email");
        localStorage.removeItem("talentgram_google_email");
        localStorage.removeItem("talentgram_google_first_name");
        localStorage.removeItem("talentgram_google_last_name");
        localStorage.removeItem("talentgram_google_avatar");
        localStorage.removeItem("talentgram_google_profile_data");
        const onboardKey = "tg_onboard_shown_apply";
        localStorage.removeItem(onboardKey);
        setGatewayRecognition(null);
        setGatewayEmail("");
    };

    // "Use this" — fill EMPTY fields only on basics + form; never overwrite.
    // Never touches media. Then unlock the rest of the gate.
    const useApplyPrefill = () => {
        const d = applyPrefill?.data;
        if (!d) return;
        setBasics((b) => ({
            ...b,
            first_name: b.first_name || d.first_name || "",
            last_name: b.last_name || d.last_name || "",
            phone: b.phone || d.phone || "",
        }));
        setForm((f) => ({
            ...f,
            dob: f.dob || d.dob || "",
            age: f.age || (d.age != null ? String(d.age) : ""),
            height: f.height || d.height || "",
            location: (f.location && f.location.length) ? f.location : (d.location || []),
            gender: f.gender || d.gender || "",
            ethnicity: f.ethnicity || d.ethnicity || "",
            bio: f.bio || d.bio || "",
            instagram_handle: f.instagram_handle || d.instagram_handle || "",
            instagram_followers:
                f.instagram_followers || d.instagram_followers || "",
            work_links:
                f.work_links && f.work_links.length
                    ? f.work_links
                    : (d.work_links || []),
            skills:
                f.skills && f.skills.length
                    ? f.skills
                    : (d.skills || []),
        }));
        setApplyPrefill(null);
        setEmailGateUnlocked(true);
        toast.success(`Welcome back, ${d.first_name}`);
    };

    const dismissApplyPrefill = () => {
        setApplyPrefill(null);
        setEmailGateUnlocked(true);
    };

    const startApplication = async () => {
        const { first_name, last_name, email } = basics;
        // Email-first ordering (Phase 1 v37 fix): always check email
        // before any other required field so a returning user who
        // hasn't typed anything else doesn't see a misleading
        // "First name is required" toast.
        if (!email.trim()) {
            toast.error("Email is required");
            return;
        }
        if (!emailGateUnlocked) {
            // The Continue button is already disabled while the gate is
            // locked; this is defense-in-depth in case the button is
            // clicked via keyboard / a11y pathway before the prefill
            // decision lands.
            toast.error("Please complete the email step first");
            return;
        }
        // After the gate is unlocked, name validation is performed based on the onboarding config.
        if (requirements?.profile_requirements?.name === "required") {
            if (!first_name.trim()) {
                toast.error("First name is required");
                return;
            }
            if (!last_name.trim()) {
                toast.error("Last name is required");
                return;
            }
        }
        setSaving(true);
        try {
            console.log("[startApplication] Starting application request for:", basics.email);
            const { data } = await axios.post(`/public/apply`, {
                ...basics,
                profile_id: profileId
            });
            const newAid = data.id;
            const newToken = data.token;
            let finalForm = { ...form };
            let finalMedia = [];

            // Always fetch the application document after POST /apply.
            // The backend _reconcile_draft_from_talent runs on every call
            // (for both new and resumed drafts) and hydrates canonical profile
            // data (media, fields) into the application document. We must
            // always GET the document so that the frontend state reflects
            // the canonical db.talents data, not the initial empty form.
            console.log("[startApplication] Fetching hydrated draft from backend for aid:", newAid);
            setHydrating(true);
            try {
                const res = await axios.get(
                    `/public/apply/${newAid}`,
                    { headers: { Authorization: `Bearer ${newToken}` } }
                );
                const appData = res.data;
                if (appData) {
                    console.log("[startApplication] Draft hydrated successfully. Loading form and media.");
                    finalForm = { ...form, ...(appData.form_data || {}) };
                    finalMedia = appData.media || [];
                    setForm(finalForm);
                    setMedia(finalMedia);
                    if (data.resumed) {
                        setIsEditMode(true);
                    }
                    // Resumed applications should not be auto-finalized here;
                    // the backend reset status to "draft" for returning talents.
                    if (appData.status === "submitted") setFinalized(true);
                }
            } catch (fetchErr) {
                console.error("[startApplication] Failed to fetch application details:", fetchErr);
                // Non-fatal: form state is already set from payload defaults
            } finally {
                setHydrating(false);
            }

            setAid(newAid);
            setToken(newToken);
            setStarted(true);
            if (data.resumed) toast.success("Welcome back — your application is resumed");
            const startedBasics = {
                ...basics,
                first_name: finalForm.first_name || basics.first_name,
                last_name: finalForm.last_name || basics.last_name,
            };
            const startedEmail = normEmail(startedBasics.email);
            if (startedEmail) {
                localStorage.setItem(
                    appDraftKey(startedEmail),
                    JSON.stringify({
                        aid: newAid,
                        token: newToken,
                        basics: startedBasics,
                        form: finalForm,
                        savedAt: Date.now(),
                    }),
                );
            }
        } catch (e) {
            console.error("[startApplication] Failed to start/resume application:", e);
            // P0-1: the backend now requires proof of email ownership before it
            // will touch an existing application/talent. If we hit that gate
            // (403), route the returning user through the one-time-code flow
            // instead of showing a dead error — then they retry automatically.
            if (e?.response?.status === 403) {
                const verifyEmail = (basics.email || "").trim().toLowerCase();
                setEmailGateUnlocked(false);
                setGatewayEmail(verifyEmail);
                try {
                    await axios.post("/auth/otp/send", { email: verifyEmail });
                    setOtpSent(true);
                    toast.message("Please verify your email", {
                        description: "We've sent a one-time code to continue.",
                    });
                } catch (otpErr) {
                    toast.error(
                        otpErr?.response?.data?.detail ||
                            "Please verify your email to continue.",
                    );
                }
            } else {
                toast.error(e?.response?.data?.detail || "Failed to start");
            }
        } finally {
            setSaving(false);
        }
    };

    // Autosave form_data (debounced) once started
    useEffect(() => {
        if (!started || !aid || !token || finalized || hydrating) return;
        const id = setTimeout(async () => {
            try {
                await axios.put(
                    `/public/apply/${aid}`,
                    { form_data: form },
                    { headers: { Authorization: `Bearer ${token}` } },
                );
                saveLocal();
            } catch (e) {
                console.error(e);
                // ignore; next attempt will retry
            }
        }, 800);
        return () => clearTimeout(id);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [form, started, aid, token, finalized, hydrating]);

    const upload = async (files, category) => {
        if (!files || !files.length) return;

        if (category === "image" || category === "indian" || category === "western") {
            const existing = media.filter((m) => m.category === category).length;
            const remaining = MAX_IMAGES_PER_CATEGORY - existing;
            if (remaining <= 0) {
                const label = category === "indian" ? "Indian look" : category === "western" ? "Western look" : "Portfolio";
                toast.error(`${label} image limit reached (${MAX_IMAGES_PER_CATEGORY})`);
                return;
            }
            files = Array.from(files).slice(0, remaining);
        }

        for (const file of files) {
            const label = (category === "image" || category === "indian" || category === "western") ? file.name : null;
            await uploadFile(file, category, label, {
                endpoint: `/public/apply/${aid}/upload`,
                token: token,
                onSuccess: (data) => {
                    setMedia(data.media || []);
                }
            });
        }
    };

    const removeMedia = async (mid) => {
        try {
            await axios.delete(`/public/apply/${aid}/media/${mid}`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            setMedia((m) => m.filter((x) => x.id !== mid));
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to remove");
        }
    };

    const finalize = async () => {
        // P0-1 Validation: collect ALL missing required fields (no early return),
        // render inline errors using the real field labels, then scroll to + focus
        // the first invalid field on this screen. No vague "Full Name" toast.
        const prof = requirements.profile_requirements;
        const port = requirements.portfolio_requirements;
        const next = {};

        if (prof.name === "required") {
            // Names are collected (and enforced) on the identity gate — they are
            // not on this screen. If somehow empty here, message specifically.
            if (!basics.first_name?.trim()) next.first_name = "First Name is required";
            else if (!basics.last_name?.trim()) next.last_name = "Last Name is required";
        }
        if (prof.location === "required" && (!form.location || form.location.length === 0))
            next.location = "Current Location is required";
        if (prof.instagram_handle === "required" && !form.instagram_handle?.trim())
            next.instagram_handle = "Instagram Handle is required";
        if (prof.instagram_followers === "required" && !form.instagram_followers)
            next.instagram_followers = "Instagram Followers is required";

        const missingMedia = [];
        if (port.portfolio === "required" && media.filter((m) => m.category === "image").length < 1)
            missingMedia.push("at least 1 Profile / Headshot image");
        if (port.indian === "required" && media.filter((m) => m.category === "indian").length < 1)
            missingMedia.push("an Indian Look image");
        if (port.western === "required" && media.filter((m) => m.category === "western").length < 1)
            missingMedia.push("a Western Look image");
        if (port.video === "required" && media.filter((m) => m.category === "intro_video").length < 1)
            missingMedia.push("an Introduction Video");
        if (missingMedia.length) next.media = `Please add ${missingMedia.join(", ")}.`;

        setErrors(next);

        if (Object.keys(next).length > 0) {
            // First/last name normally live off this screen (the identity
            // gate), but the recovery section above renders whenever either
            // is missing, so scroll/focus there like any other on-screen
            // field instead of dead-ending on a toast with nothing to fix.
            if (next.first_name || next.last_name) {
                toast.error(next.first_name || next.last_name);
            }
            const order = [
                [next.first_name || next.last_name, identityRef],
                [next.location, locationRef],
                [next.instagram_handle, igHandleRef],
                [next.instagram_followers, igFollowersRef],
                [next.media, mediaRef],
            ];
            const firstInvalid = order.find(([msg]) => msg);
            if (firstInvalid && firstInvalid[1].current) {
                firstInvalid[1].current.scrollIntoView({ behavior: "smooth", block: "center" });
                const focusable = firstInvalid[1].current.querySelector(
                    "input, button, textarea, select, [tabindex]"
                );
                if (focusable) setTimeout(() => focusable.focus({ preventScroll: true }), 300);
            }
            return;
        }

        setSaving(true);
        try {
            // Final sync of form_data. Includes first_name/last_name from
            // `basics` so a correction made in the recovery section above
            // reaches the backend record the finalize validator reads —
            // `PUT /public/apply/{aid}` already merges these two keys into
            // form_data and re-derives talent_name (existing backend
            // behavior, unchanged here).
            await axios.put(
                `/public/apply/${aid}`,
                { form_data: { ...form, first_name: basics.first_name, last_name: basics.last_name } },
                { headers: { Authorization: `Bearer ${token}` } },
            );
            await axios.post(
                `/public/apply/${aid}/finalize`,
                {},
                { headers: { Authorization: `Bearer ${token}` } },
            );
            setFinalized(true);
            // Clear this identity's cached draft (and any legacy slot that was
            // never migrated). Other identities' slots are untouched.
            const finalizedEmail = normEmail(basics.email);
            if (finalizedEmail) localStorage.removeItem(appDraftKey(finalizedEmail));
            localStorage.removeItem(LEGACY_LS_KEY);
            // P2-8: the local draft is the only place we mirrored PII; once the
            // submission is server-side we no longer need the bulky Google
            // profile blob cached in localStorage. Drop it to minimise PII at
            // rest (the portal token/email remain so a resumed session still works).
            try {
                localStorage.removeItem("talentgram_google_profile_data");
                localStorage.removeItem("talentgram_google_avatar");
            } catch (_) { /* localStorage unavailable — non-fatal */ }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Submission failed");
        } finally {
            setSaving(false);
        }
    };

    const images = media.filter((m) => m.category === "image");
    const indianImages = media.filter((m) => m.category === "indian");
    const westernImages = media.filter((m) => m.category === "western");
    const allImages = [...images, ...indianImages, ...westernImages];
    const intro = media.find((m) => m.category === "intro_video");

    // --- Identity gate -----------------------------------------------------
    if (!started) {
        return (
            <div
                className="min-h-dvh bg-[#ffffff] text-[#1a1a1a]"
                data-testid="application-identity-page"
            >
                <div className="max-w-xl mx-auto px-6 py-16 md:py-24 flex flex-col items-center">
                    {/* Centered standardized prominent logo with breathing room */}
                    <div className="mb-12 text-center">
                        <Logo size={120} className="mx-auto" forceVariant="black" />
                        {/* Clickable Instagram icon */}
                        <div className="mt-4">
                            <a
                                href="https://www.instagram.com/talentgram.agency/"
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center justify-center p-2 rounded-full text-[#1a1a1a] hover:bg-slate-100 transition-all duration-200 cursor-pointer group"
                                title="Follow us on Instagram"
                            >
                                <svg
                                    className="w-5 h-5 transition-colors duration-200 hover:text-[#E1306C] md:group-hover:text-[#E1306C]"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="2"
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                >
                                    <rect x="2" y="2" width="20" height="20" rx="5" ry="5" />
                                    <path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z" />
                                    <line x1="17.5" y1="6.5" x2="17.51" y2="6.5" />
                                </svg>
                            </a>
                        </div>
                    </div>

                    <div className="w-full">
                        <p className="text-[11px] tracking-[0.12em] uppercase text-[#6b6b6b] mb-3">Talent Application</p>
                        <h1 className="font-display text-4xl md:text-5xl tracking-tight text-[#1a1a1a] mb-4">
                            Apply to join Talentgram
                        </h1>
                        <p className="text-[#6b6b6b] text-sm mb-10 leading-relaxed">
                            Submit your portfolio once — get considered for every
                            brand, film, and campaign we scout. Takes about 5 minutes.
                        </p>
                        <div className="space-y-5">
                            {!emailGateUnlocked ? (
                                otpSent ? (
                                    /* Step A.5: OTP Verification Input */
                                    <div className="flex flex-col gap-4 animate-in fade-in duration-200 text-left">
                                        <div className="flex flex-col gap-1.5">
                                            <label className="text-xs font-semibold text-[#111111] uppercase tracking-wider">
                                                Enter Verification Code
                                            </label>
                                            <p className="text-xs text-[#6b6b6b] leading-normal">
                                                We've sent a verification code to <span className="font-semibold text-[#1a1a1a]">{gatewayEmail}</span>
                                            </p>
                                        </div>
                                        <div className="flex flex-col sm:flex-row gap-3">
                                            <input
                                                type="text"
                                                inputMode="numeric"
                                                pattern="[0-9]*"
                                                maxLength={6}
                                                value={otpValue}
                                                onChange={(e) => setOtpValue(e.target.value.replace(/\D/g, ''))}
                                                onKeyDown={(e) => {
                                                    if (e.key === "Enter") {
                                                        e.preventDefault();
                                                        handleVerifyOtp();
                                                    }
                                                }}
                                                placeholder="6-digit code"
                                                style={{ fontSize: "16px" }}
                                                className="flex-1 px-4 py-2.5 bg-white border border-[#eaeaea] rounded-xl text-[#1a1a1a] placeholder:text-[#6b6b6b] focus:border-[#1a1a1a] focus:outline-none transition duration-150 h-[44px]"
                                                disabled={otpLoading}
                                            />
                                            <div className="flex gap-2">
                                                <button
                                                    type="button"
                                                    onClick={handleVerifyOtp}
                                                    disabled={otpLoading}
                                                    className="bg-[#1a1a1a] text-white px-5 py-2.5 rounded-xl text-xs font-medium hover:bg-[#333] active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 min-w-[100px] h-[44px] cursor-pointer"
                                                >
                                                    {otpLoading ? "Verifying..." : "Verify"}
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={handleResendOtp}
                                                    disabled={otpResending || otpLoading}
                                                    className="bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#1a1a1a] text-xs font-medium px-4 py-2.5 rounded-xl transition duration-150 h-[44px] cursor-pointer"
                                                >
                                                    {otpResending ? "Resending..." : "Resend OTP"}
                                                </button>
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => {
                                                setOtpSent(false);
                                                setOtpValue("");
                                            }}
                                            className="text-left text-xs text-[#6b6b6b] hover:text-[#1a1a1a] transition underline cursor-pointer"
                                        >
                                            Change email address
                                        </button>
                                    </div>
                                ) : !gatewayRecognition ? (
                                    /* Step A: Inline Email Lookup */
                                    <div className="flex flex-col gap-4 animate-in fade-in duration-200 text-left">
                                        <button
                                            type="button"
                                            onClick={handleGoogleLogin}
                                            className="w-full bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#1a1a1a] py-3 px-4 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-2.5 transition duration-150 shadow-sm active:scale-[0.98] cursor-pointer"
                                        >
                                            <svg className="w-4 h-4" viewBox="0 0 24 24">
                                                <path
                                                    fill="#EA4335"
                                                    d="M12 5.04c1.78 0 3.38.61 4.64 1.8l3.46-3.46C17.99 1.19 15.21 0 12 0 7.31 0 3.28 2.69 1.34 6.61l4.08 3.16C6.4 7.02 9.01 5.04 12 5.04z"
                                                />
                                                <path
                                                    fill="#4285F4"
                                                    d="M23.49 12.27c0-.81-.07-1.59-.2-2.36H12v4.51h6.46c-.29 1.48-1.14 2.73-2.4 3.58l3.73 2.89c2.18-2.01 3.7-4.97 3.7-8.62z"
                                                />
                                                <path
                                                    fill="#FBBC05"
                                                    d="M5.42 14.78c-.24-.72-.38-1.49-.38-2.28s.14-1.56.38-2.28L1.34 7.06C.48 8.79 0 10.74 0 12.8s.48 4.01 1.34 5.74l4.08-3.76z"
                                                />
                                                <path
                                                    fill="#34A853"
                                                    d="M12 24c3.24 0 5.97-1.07 7.96-2.91l-3.73-2.89c-1.04.7-2.36 1.11-4.23 1.11-3.01 0-5.6-1.98-6.51-4.73L1.34 17.68C3.28 21.6 7.31 24 12 24z"
                                                />
                                            </svg>
                                            Continue with Google
                                        </button>
                                        <div className="flex items-center my-1.5">
                                            <div className="flex-grow border-t border-[#eaeaea]"></div>
                                            <span className="mx-4 text-[10px] text-[#888888] font-mono uppercase tracking-wider">or</span>
                                            <div className="flex-grow border-t border-[#eaeaea]"></div>
                                        </div>
                                        <div className="flex flex-col gap-1.5">
                                            <label className="text-xs font-semibold text-[#111111] uppercase tracking-wider">
                                                Continue with Email
                                            </label>
                                            <p className="text-xs text-[#6b6b6b] leading-normal">
                                                We use your email to recognise you and load any previously submitted details.
                                            </p>
                                        </div>
                                        <div className="flex flex-col sm:flex-row gap-3">
                                            <input
                                                type="email"
                                                value={gatewayEmail}
                                                onChange={(e) => setGatewayEmail(e.target.value)}
                                                onKeyDown={(e) => {
                                                    if (e.key === "Enter") {
                                                        e.preventDefault();
                                                        handleInlineLookup();
                                                    }
                                                }}
                                                placeholder="Enter your email address"
                                                style={{ fontSize: "16px" }}
                                                className="flex-1 px-4 py-2.5 bg-white border border-[#eaeaea] rounded-xl text-[#1a1a1a] placeholder:text-[#6b6b6b] focus:border-[#1a1a1a] focus:outline-none transition duration-150 h-[44px]"
                                                disabled={gatewayLoading}
                                            />
                                            <button
                                                type="button"
                                                onClick={handleInlineLookup}
                                                disabled={gatewayLoading}
                                                className="bg-[#1a1a1a] text-white px-5 py-2.5 rounded-xl text-xs font-medium hover:bg-[#333] active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 min-w-[120px] h-[44px]"
                                            >
                                                {gatewayLoading ? "Verifying..." : "Continue"}
                                                <ArrowRight className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                    </div>
                                ) : (
                                    /* Step B: Inline Cinematic Recognition */
                                    <div className="flex flex-col gap-5 border border-slate-100 rounded-2xl p-5 bg-slate-50/50 animate-in fade-in zoom-in-95 duration-200 text-left">
                                        <div className="flex items-center gap-4">
                                            {gatewayRecognition.image_url ? (
                                                <img
                                                    src={gatewayRecognition.image_url}
                                                    alt={gatewayRecognition.name}
                                                    className="w-12 h-12 rounded-full object-cover border border-[#eaeaea]"
                                                />
                                            ) : (
                                                <div className="w-12 h-12 rounded-full bg-slate-200 flex items-center justify-center border border-[#d4d4d4]">
                                                    <User className="w-5 h-5 text-[#333333]" />
                                                </div>
                                            )}
                                            <div className="text-left">
                                                <h4 className="font-semibold text-sm text-[#111111]">Is this you?</h4>
                                                <p className="text-xs text-[#6b6b6b] font-medium">
                                                    {gatewayRecognition.name} {(() => {
                                                        const locs = Array.isArray(gatewayRecognition.location) 
                                                            ? gatewayRecognition.location 
                                                            : (gatewayRecognition.location ? [{ city: gatewayRecognition.location }] : []);
                                                        return locs.length > 0 ? `· ${locs.map(l => l?.city || l).join(", ")}` : "";
                                                    })()}
                                                </p>
                                            </div>
                                        </div>

                                        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 pt-2 border-t border-[#eaeaea]/40">
                                            <button
                                                type="button"
                                                onClick={handleInlineContinue}
                                                className="flex-1 bg-[#1a1a1a] text-white px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-[#333] active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 h-[40px]"
                                            >
                                                Continue to Application
                                                <ChevronRight className="w-3.5 h-3.5" />
                                            </button>
                                            <button
                                                type="button"
                                                onClick={handleInlineCancel}
                                                className="border border-[#eaeaea] text-[#333333] hover:border-[#d4d4d4] px-4 py-2.5 rounded-xl text-xs inline-flex items-center justify-center h-[40px] bg-white"
                                            >
                                                Use another email
                                            </button>
                                        </div>
                                    </div>
                                )
                            ) : (
                                /* Locked Email State (if unlocked) */
                                <div className="space-y-5">
                                    <Row
                                        label="Email *"
                                        type="email"
                                        value={basics.email}
                                        onChange={(v) => {
                                            setBasics({ ...basics, email: v });
                                            // Re-arm the prefill if the email changed.
                                            if (v.trim().toLowerCase() !== applyPrefillTried) {
                                                setEmailGateUnlocked(false);
                                                setApplyPrefill(null);
                                            }
                                        }}
                                        onBlur={tryEmailPrefill}
                                        testid="apply-email"
                                        hint="Used as your unique identifier — we'll resume from where you stop"
                                    />

                                    {applyPrefill && !emailGateUnlocked && (
                                        <div
                                            className="bg-white rounded-xl border border-[#eaeaea] p-4 flex flex-col sm:flex-row sm:items-center gap-3 justify-between shadow-sm"
                                            data-testid="apply-prefill-card"
                                        >
                                            <div className="min-w-0 flex-1">
                                                <p className="text-sm text-[#1a1a1a]">
                                                    We found your profile.{" "}
                                                    <span className="text-[#6b6b6b]">
                                                        Use saved details?
                                                    </span>
                                                </p>
                                                <p className="text-[11px] font-mono text-[#8b8b8b] mt-1 truncate">
                                                    {applyPrefill.data.first_name}{" "}
                                                    {applyPrefill.data.last_name || ""}
                                                    {(() => {
                                                        const locs = Array.isArray(applyPrefill.data.location) 
                                                            ? applyPrefill.data.location 
                                                            : (applyPrefill.data.location ? [{ city: applyPrefill.data.location }] : []);
                                                        return locs.length > 0 ? ` · ${locs.map(l => l?.city || l).join(", ")}` : "";
                                                    })()}
                                                    {applyPrefill.data.height ? ` · ${applyPrefill.data.height}` : ""}
                                                </p>
                                            </div>
                                            <div className="flex items-center gap-2">
                                                <button
                                                    type="button"
                                                    onClick={useApplyPrefill}
                                                    data-testid="apply-prefill-use-btn"
                                                    className="bg-[#1a1a1a] text-white px-4 py-2.5 text-xs rounded-lg hover:bg-[#333] transition-colors duration-150 inline-flex items-center gap-1.5 min-h-[44px]"
                                                >
                                                    <Check className="w-3.5 h-3.5" />
                                                    Use this
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={dismissApplyPrefill}
                                                    data-testid="apply-prefill-dismiss-btn"
                                                    className="border border-[#eaeaea] bg-white text-[#4a4a4a] hover:border-[#d4d4d4] px-4 py-2.5 text-xs rounded-lg inline-flex items-center gap-1.5 min-h-[44px] transition-colors duration-150"
                                                >
                                                    Edit manually
                                                </button>
                                            </div>
                                        </div>
                                    )}

                                    {emailGateUnlocked && (
                                        <div className="space-y-5" data-testid="apply-identity-rest">
                                            <Row
                                                label={`First Name${requirements?.profile_requirements?.name === "required" ? " *" : ""}`}
                                                value={basics.first_name}
                                                onChange={(v) => setBasics({ ...basics, first_name: v })}
                                                testid="apply-first-name"
                                            />
                                            <Row
                                                label={`Last Name${requirements?.profile_requirements?.name === "required" ? " *" : ""}`}
                                                value={basics.last_name}
                                                onChange={(v) => setBasics({ ...basics, last_name: v })}
                                                testid="apply-last-name"
                                            />
                                            <Row
                                                label="Phone Number (WhatsApp)"
                                                value={basics.phone}
                                                onChange={(v) => setBasics({ ...basics, phone: v })}
                                                testid="apply-phone"
                                                hint="Please enter the number that is active on WhatsApp. This will be used for casting communication and project updates."
                                            />
                                            <Row
                                                label="Alternate Contact Number (optional)"
                                                value={basics.alternate_contact_number}
                                                onChange={(v) => setBasics({ ...basics, alternate_contact_number: v })}
                                                testid="apply-alt-phone"
                                                hint="Optional backup contact number."
                                            />
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                        <button
                            onClick={startApplication}
                            disabled={saving || !emailGateUnlocked}
                            data-testid="apply-start-btn"
                            className="mt-10 inline-flex items-center gap-2 bg-[#1a1a1a] text-white px-6 py-3.5 rounded-lg text-sm font-medium hover:bg-[#333] transition-colors duration-150 disabled:opacity-40"
                        >
                            {saving ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                                <ArrowRight className="w-4 h-4" />
                            )}
                            Continue
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    // --- Finalized ----------------------------------------------------------
    if (finalized) {
        return (
            <div
                className="min-h-dvh bg-[#faf9f6] text-[#1a1a1a] flex flex-col"
                data-testid="application-success-page"
            >
                <Header />
                <div className="flex-1 flex items-center justify-center p-6">
                    <div className="max-w-md text-center bg-white rounded-2xl p-8 md:p-10 shadow-[0_8px_30px_-12px_rgba(0,0,0,0.08)] border border-[#eaeaea]">
                        <div className="w-14 h-14 rounded-full bg-[#e6f7e6] text-[#2b6e2f] inline-flex items-center justify-center mb-6">
                            <Check className="w-6 h-6" />
                        </div>
                        <p className="text-[11px] tracking-[0.12em] uppercase text-[#6b6b6b] mb-3">
                            {isEditMode ? "Saved" : "Submitted"}
                        </p>
                        <h1 className="font-display text-3xl md:text-4xl tracking-tight text-[#1a1a1a] mb-4">
                            {isEditMode ? "Profile Updated" : `Thank you, ${basics.first_name}`}
                        </h1>
                        <p className="text-[#4a4a4a] text-sm leading-relaxed mb-4">
                            {isEditMode 
                                ? "Your changes have been saved."
                                : "Your profile has been successfully submitted."
                            }
                        </p>
                        {!isEditMode && (
                            <div className="text-left bg-[#faf9f6] border border-[#eaeaea] rounded-xl p-4 mb-6">
                                <p className="text-[11px] tracking-[0.12em] uppercase text-[#6b6b6b] mb-2">
                                    What happens next
                                </p>
                                <ul className="text-[#4a4a4a] text-sm leading-relaxed space-y-1.5">
                                    <li>✓ Your application has been received.</li>
                                    <li>✓ Our team reviews every application — usually within <strong>3–5 working days</strong>.</li>
                                    <li>✓ We'll email you either way; if anything else is needed, we'll reach out directly.</li>
                                </ul>
                            </div>
                        )}
                        <div className="text-xs text-[#8b8b8b] font-medium mb-6">
                            Thank you for your interest in joining Talentgram.
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // --- Main form ---------------------------------------------------------
    return (
        <div
            className="min-h-dvh bg-[#faf9f6] text-[#1a1a1a]"
            data-testid="application-form-page"
        >
            <Header />
            <div className="max-w-3xl mx-auto px-4 sm:px-6 py-10 md:py-16">
                <p className="text-[11px] tracking-[0.12em] uppercase text-[#6b6b6b] mb-3">Application · {basics.email}</p>
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-8 border-b border-[#eaeaea] pb-4">
                    <h1 className="font-display text-3xl md:text-4xl tracking-tight text-[#1a1a1a]">
                        Your Profile
                    </h1>
                    <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-50 border border-emerald-100/50 text-emerald-700 text-[11px] font-mono shadow-[0_1px_2px_rgba(0,0,0,0.02)] self-start sm:self-auto">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                        <span>Draft Auto-Saved</span>
                    </div>
                </div>

                {/* Recovery section — the identity gate (the only place First
                    Name / Last Name / Phone are normally editable) is skipped
                    on some resume paths (e.g. returning via OTP before a
                    canonical talent profile exists). If any of these ended up
                    missing, surface them here so the talent isn't stuck at a
                    "First Name is required" dead end with no field to fix.
                    Hidden entirely once all three are present — no change to
                    the existing experience for a normal, fully-filled draft. */}
                {(!basics.first_name?.trim() || !basics.last_name?.trim() || !basics.phone?.trim()) && (
                    <div ref={identityRef}>
                        <Section title="Contact Details" index="!">
                            <div className="space-y-5">
                                <Row
                                    label={`First Name${requirements?.profile_requirements?.name === "required" ? " *" : ""}`}
                                    value={basics.first_name}
                                    onChange={(v) => { setBasics({ ...basics, first_name: v }); clearError("first_name"); }}
                                    testid="apply-first-name-recovery"
                                />
                                <Row
                                    label={`Last Name${requirements?.profile_requirements?.name === "required" ? " *" : ""}`}
                                    value={basics.last_name}
                                    onChange={(v) => { setBasics({ ...basics, last_name: v }); clearError("last_name"); }}
                                    testid="apply-last-name-recovery"
                                />
                                <Row
                                    label="Phone Number (WhatsApp)"
                                    value={basics.phone}
                                    onChange={(v) => setBasics({ ...basics, phone: v })}
                                    testid="apply-phone-recovery"
                                    hint="Please enter the number that is active on WhatsApp. This will be used for casting communication and project updates."
                                />
                            </div>
                        </Section>
                    </div>
                )}

                {/* Section 2 — Profile Details */}
                <Section title="Profile Details" index="01">
                    <div className="grid md:grid-cols-2 gap-6">
                        <Row
                            label="Date of Birth *"
                            type="date"
                            value={form.dob}
                            onChange={(v) => setForm({ ...form, dob: v })}
                            testid="form-dob"
                            hint="Format: DD / MM / YYYY"
                        />
                        
                        <div className="flex flex-col justify-end">
                            <Label>
                                Age {form.dob ? "(auto calculated)" : ""}
                            </Label>
                            <div className="mt-2 h-11 flex items-center px-4 bg-slate-50 rounded-lg border border-[#eaeaea] text-[15px] text-[#333333] font-mono">
                                {form.dob ? (calcAge(form.dob) ?? "—") : "—"}
                            </div>
                        </div>

                        <div>
                            <Label>Height *</Label>
                            <Select
                                value={form.height}
                                onValueChange={(v) =>
                                    setForm({ ...form, height: v })
                                }
                            >
                                <SelectTrigger
                                    className="mt-2 w-full bg-white border border-[#eaeaea] rounded-lg h-11 px-4 text-[15px] text-[#1a1a1a] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] transition-all duration-150"
                                    data-testid="form-height"
                                >
                                    <SelectValue placeholder="Select height" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-[#eaeaea] rounded-xl shadow-lg">
                                    {HEIGHT_OPTIONS.map((h) => (
                                        <SelectItem key={h} value={h} className="text-[15px]">
                                            {h}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>

                        <div ref={locationRef}>
                            <Label>Current Location(s){requirements?.profile_requirements?.location === "required" ? " *" : ""}</Label>
                            <div className="mt-2">
                                <LocationSelector
                                    value={form.location || []}
                                    onChange={(arr) => {
                                        setForm({ ...form, location: arr });
                                        if (arr && arr.length) clearError("location");
                                    }}
                                    testid="form-location"
                                    placeholder="Search for a city..."
                                />
                            </div>
                            {errors.location && (
                                <p role="alert" className="text-xs text-[#d03a2a] mt-1.5" data-testid="error-location">
                                    {errors.location}
                                </p>
                            )}
                        </div>

                        <div className="md:col-span-2">
                            <Label>Gender{requirements?.profile_requirements?.gender === "required" ? " *" : ""}</Label>
                            <div className="mt-3 flex flex-wrap gap-2">
                                {GENDER_OPTIONS.map((g) => (
                                    <button
                                        key={g.key}
                                        type="button"
                                        onClick={() =>
                                            setForm({ ...form, gender: g.key })
                                        }
                                        data-testid={`form-gender-${g.key}`}
                                        className={`px-4 py-2 rounded-full border text-xs tracking-[0.08em] uppercase transition-all duration-150 ${
                                            form.gender === g.key
                                                ? "border-[#1a1a1a] bg-[#1a1a1a] text-white"
                                                : "border-[#eaeaea] bg-white text-[#4a4a4a] hover:border-[#d4d4d4]"
                                        }`}
                                    >
                                        {g.label}
                                    </button>
                                ))}
                            </div>
                        </div>
                    </div>
                </Section>

                {/* Section 3 — Professional */}
                <Section title="Professional Details" index="02">
                    <div className="grid md:grid-cols-2 gap-6">
                        <div ref={igHandleRef}>
                            <Label>Instagram Handle{requirements?.profile_requirements?.instagram_handle === "required" ? " *" : ""}</Label>
                            <input
                                value={form.instagram_handle}
                                onChange={(e) => {
                                    setForm({
                                        ...form,
                                        instagram_handle: e.target.value,
                                    });
                                    if (e.target.value.trim()) clearError("instagram_handle");
                                }}
                                onBlur={() => {
                                    if (form.instagram_handle) {
                                        setForm((prev) => ({
                                            ...prev,
                                            instagram_handle: normalizeInstagramHandle(form.instagram_handle)
                                        }));
                                    }
                                }}
                                placeholder="@yourhandle"
                                data-testid="form-instagram"
                                aria-invalid={!!errors.instagram_handle}
                                className={`mt-2 w-full bg-white border rounded-lg px-4 h-11 text-[16px] md:text-[15px] text-[#1a1a1a] placeholder:text-[#b0aea6] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] outline-none transition-all duration-150 ${errors.instagram_handle ? "border-[#d03a2a]" : "border-[#eaeaea]"}`}
                            />
                            {errors.instagram_handle && (
                                <p role="alert" className="text-xs text-[#d03a2a] mt-1.5" data-testid="error-instagram">
                                    {errors.instagram_handle}
                                </p>
                            )}
                        </div>
                        <div ref={igFollowersRef}>
                            <Label>Instagram Followers{requirements?.profile_requirements?.instagram_followers === "required" ? " *" : ""}</Label>
                            <Select
                                value={form.instagram_followers}
                                onValueChange={(v) => {
                                    setForm({ ...form, instagram_followers: v });
                                    if (v) clearError("instagram_followers");
                                }}
                            >
                                <SelectTrigger
                                    className={`mt-2 w-full bg-white border rounded-lg h-11 px-4 text-[15px] text-[#1a1a1a] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] transition-all duration-150 ${errors.instagram_followers ? "border-[#d03a2a]" : "border-[#eaeaea]"}`}
                                    data-testid="form-followers"
                                >
                                    <SelectValue placeholder="Select range" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-[#eaeaea] rounded-xl shadow-lg max-h-72">
                                    {FOLLOWER_TIERS.map((tier) => (
                                        <SelectGroup key={tier.label}>
                                            <SelectLabel className="text-[10px] tracking-[0.08em] uppercase text-[#8b8b8b] px-2 py-1.5">
                                                {tier.label}
                                            </SelectLabel>
                                            {tier.items.map((it) => (
                                                <SelectItem key={it} value={it} className="text-[15px]">
                                                    {it}
                                                </SelectItem>
                                            ))}
                                            <SelectSeparator className="bg-[#e8e6df]" />
                                        </SelectGroup>
                                    ))}
                                </SelectContent>
                            </Select>
                            {errors.instagram_followers && (
                                <p role="alert" className="text-xs text-[#d03a2a] mt-1.5" data-testid="error-followers">
                                    {errors.instagram_followers}
                                </p>
                            )}
                        </div>
                        <div>
                            <Label>Ethnicity (optional)</Label>
                            <Select
                                value={form.ethnicity}
                                onValueChange={(v) =>
                                    setForm({ ...form, ethnicity: v })
                                }
                            >
                                <SelectTrigger
                                    className="mt-2 w-full bg-white border border-[#eaeaea] rounded-lg h-11 px-4 text-[15px] text-[#1a1a1a] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] transition-all duration-150"
                                    data-testid="form-ethnicity"
                                >
                                    <SelectValue placeholder="Select ethnicity" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-[#eaeaea] rounded-xl shadow-lg max-h-72">
                                    {ETHNICITY_OPTIONS.map((e) => (
                                        <SelectItem
                                            key={e.key}
                                            value={e.key}
                                            data-testid={`form-ethnicity-${e.key}`}
                                            className="text-[15px]"
                                        >
                                            {e.label}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="md:col-span-2">
                            <Label>Skills & Special Abilities (optional)</Label>
                            <div className="mt-2">
                                <SkillsSelector
                                    selectedSkills={form.skills || []}
                                    onChange={(arr) => setForm({ ...form, skills: arr })}
                                />
                            </div>
                        </div>
                        <div className="md:col-span-2">
                            <Label>Short Bio (optional)</Label>
                            <textarea
                                value={form.bio}
                                onChange={(e) =>
                                    setForm({ ...form, bio: e.target.value })
                                }
                                rows={4}
                                placeholder="A few lines about yourself — experience, strengths, what you're looking for."
                                data-testid="form-bio"
                                className="mt-2 w-full bg-white border border-[#eaeaea] rounded-lg p-4 text-[16px] md:text-[15px] text-[#1a1a1a] placeholder:text-[#b0aea6] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] outline-none transition-all duration-150 resize-vertical"
                            />
                        </div>
                        <div className="md:col-span-2">
                            <Label>Work Links (optional)</Label>
                            <ApplyWorkLinksEditor
                                links={form.work_links || []}
                                onChange={(arr) =>
                                    setForm({ ...form, work_links: arr })
                                }
                            />
                        </div>
                    </div>
                </Section>

                {/* Section 3b — Interested In */}
                <Section title="What are you interested in?" index="03">
                    <p className="text-xs text-[#6b6b6b] mb-5 leading-relaxed">
                        Select all categories that apply. This helps us match you to the right campaigns.
                    </p>
                    <InterestedInSelector
                        selected={form.interested_in || []}
                        onChange={(v) => setForm({ ...form, interested_in: v })}
                    />
                </Section>

                {/* Section 4 — Media */}
                <div ref={mediaRef}>
                <Section title="Media" index="04">
                    {errors.media && (
                        <p role="alert" className="text-xs text-[#d03a2a] mb-4" data-testid="error-media">
                            {errors.media}
                        </p>
                    )}
                    <div className="space-y-8">
                        <div>
                            <div className="flex items-center justify-between mb-1">
                                <p className="text-sm text-[#1a1a1a] font-medium">
                                    Introduction Video{requirements?.portfolio_requirements?.video === "required" ? " *" : ""}
                                </p>
                                {intro && (
                                    <button
                                        onClick={() => removeMedia(intro.id)}
                                        className="text-[10px] font-mono text-[#8b8b8b] hover:text-[#d03a2a] transition-colors duration-150"
                                    >
                                        Replace
                                    </button>
                                )}
                            </div>
                            <p className="text-xs text-[#8b8b8b] mb-3">
                                Recommended. Your most recent professional introduction video, without contact info.
                            </p>
                            {intro ? (
                                intro.status === "processing" ? (
                                    <div className="w-full max-w-lg bg-slate-900 border border-slate-100 rounded-xl py-12 flex flex-col items-center gap-3 text-sm text-[#eaeaea] animate-fadeIn">
                                        <div className="w-6 h-6 rounded-full border-2 border-emerald-500 border-t-transparent animate-spin mb-1"></div>
                                        <p className="text-xs font-mono animate-pulse">Optimizing video on server...</p>
                                    </div>
                                ) : (
                                    <LazyVideoPlayer
                                        src={intro.url}
                                        poster={posterUrl(intro)}
                                        label="Introduction Video"
                                        className="max-w-lg shadow-sm border border-[#eaeaea]"
                                    />
                                )
                            ) : (
                                <button
                                    onClick={() => videoRef.current?.click()}
                                    data-testid="apply-intro-upload-btn"
                                    disabled={activeUploads["intro_video"]?.status === "uploading" || activeUploads["intro_video"]?.status === "processing"}
                                    className="w-full max-w-lg bg-[#f5f4f0] border border-dashed border-[#eaeaea] rounded-xl py-10 flex flex-col items-center gap-2 text-sm text-[#6b6b6b] hover:bg-[#efede8] transition-colors duration-150"
                                >
                                    {(activeUploads["intro_video"]?.status === "uploading" || activeUploads["intro_video"]?.status === "processing") ? (
                                        <Loader2 className="w-5 h-5 animate-spin" />
                                    ) : (
                                        <Video className="w-5 h-5" />
                                    )}
                                    <span>Upload video</span>
                                </button>
                            )}
                            <input
                                ref={videoRef}
                                type="file"
                                accept="video/*"
                                className="hidden"
                                onChange={(e) =>
                                    upload(e.target.files, "intro_video")
                                }
                            />
                        </div>

                        {/* Phase 2 Looks */}
                        <ApplyLookGroup
                            label={`Indian Look${requirements?.portfolio_requirements?.indian === "required" ? " *" : ""}`}
                            hint="Saree, lehenga, sherwani, or traditional/Indian-look references."
                            items={indianImages}
                            category="indian"
                            allCount={indianImages.length}
                            maxImages={MAX_IMAGES_PER_CATEGORY}
                            inputRef={indianRef}
                            upload={upload}
                            removeMedia={removeMedia}
                            activeUploads={activeUploads}
                            testidPrefix="indian"
                        />
                        <ApplyLookGroup
                            label={`Western Look${requirements?.portfolio_requirements?.western === "required" ? " *" : ""}`}
                            hint="Casual, formal or western-styled references."
                            items={westernImages}
                            category="western"
                            allCount={westernImages.length}
                            maxImages={MAX_IMAGES_PER_CATEGORY}
                            inputRef={westernRef}
                            upload={upload}
                            removeMedia={removeMedia}
                            activeUploads={activeUploads}
                            testidPrefix="western"
                        />

                        <div>
                            <div className="flex items-center justify-between mb-1">
                                <p className="text-sm text-[#1a1a1a] font-medium">
                                    Profile / Headshot Image{requirements?.portfolio_requirements?.portfolio === "required" ? " *" : ""} <span className="text-[#8b8b8b] text-xs ml-1">({images.length}/{MAX_IMAGES_PER_CATEGORY})</span>
                                </p>
                                {images.length < MAX_IMAGES_PER_CATEGORY && (
                                    <button
                                        onClick={() => imgRef.current?.click()}
                                        data-testid="apply-image-upload-btn"
                                        disabled={Object.values(activeUploads).some(u => u.category === "image" && (u.status === "uploading" || u.status === "processing"))}
                                        className="inline-flex items-center gap-1.5 text-xs border border-[#eaeaea] bg-white hover:border-[#d4d4d4] px-3 py-1.5 rounded-lg transition-colors duration-150"
                                    >
                                        {Object.values(activeUploads).some(u => u.category === "image" && (u.status === "uploading" || u.status === "processing")) ? (
                                            <Loader2 className="w-3 h-3 animate-spin" />
                                        ) : (
                                            <Upload className="w-3 h-3" />
                                        )}
                                        Add
                                    </button>
                                )}
                            </div>
                            <p className="text-xs text-[#8b8b8b] mb-3">
                                Upload at least 1 clear profile/headshot image (required). Add more (up to {MAX_IMAGES_PER_CATEGORY} per category, including Indian / Western looks above) to improve your selection chances.
                            </p>
                            <input
                                ref={imgRef}
                                type="file"
                                accept="image/*"
                                multiple
                                className="hidden"
                                onChange={(e) => upload(e.target.files, "image")}
                            />
                            {images.length === 0 ? (
                                <button
                                    onClick={() => imgRef.current?.click()}
                                    className="w-full bg-[#f5f4f0] border border-dashed border-[#eaeaea] rounded-xl py-10 flex flex-col items-center gap-2 text-sm text-[#6b6b6b] hover:bg-[#efede8] transition-colors duration-150"
                                >
                                    <Camera className="w-5 h-5" />
                                    <span>Upload images</span>
                                </button>
                            ) : (
                                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4">
                                    {images.map((m) => (
                                        <div
                                            key={m.id}
                                            className="relative aspect-[3/4] bg-[#f5f4f0] rounded-xl border border-[#eaeaea] overflow-hidden group shadow-sm"
                                        >
                                            <img
                                                src={m.url}
                                                alt=""
                                                loading="lazy"
                                                className="w-full h-full object-cover"
                                            />
                                            <button
                                                onClick={() => removeMedia(m.id)}
                                                className="absolute top-2 right-2 w-7 h-7 bg-white/90 rounded-lg opacity-100 md:opacity-0 md:group-hover:opacity-100 flex items-center justify-center hover:bg-[#d03a2a] hover:text-white transition-all duration-150 shadow-sm"
                                            >
                                                <Trash2 className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>
                </Section>
                </div>

                {/* Safe-area-aware sticky submit footer */}
                <div ref={stickyFooterRef} data-sticky-footer className="sticky bottom-0 z-20 bg-gradient-to-t from-[#faf9f6] via-[#faf9f6]/95 to-transparent pt-4 pb-safe-offset-6 -mx-4 sm:-mx-6 px-4 sm:px-6">
                <button
                    onClick={finalize}
                    disabled={saving}
                    data-testid="apply-submit-btn"
                    className="mt-2 w-full bg-[#1a1a1a] text-white py-4 rounded-xl text-sm font-medium hover:bg-[#333] transition-colors duration-150 disabled:opacity-40 inline-flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.98]"
                    style={{ WebkitTapHighlightColor: "transparent" }}
                >
                    {saving ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                        <Sparkles className="w-4 h-4" />
                    )}
                    Submit Application
                </button>
                <p className="text-[11px] text-[#8b8b8b] font-mono text-center mt-3">
                    We'll auto-save as you go — feel free to come back and
                    finish later.
                </p>
                </div>
            </div>
        </div>
    );
}

function Header() {
    return (
        <header className="sticky top-0 z-10 flex items-center justify-between px-4 sm:px-6 py-4 bg-white/90 backdrop-blur-md border-b border-[#eaeaea]">
            <Logo className="h-8" />
        </header>
    );
}

function Section({ title, index, children }) {
    return (
        <section className="mb-10 bg-white rounded-xl border border-[#eaeaea] shadow-[0_8px_30px_-12px_rgba(0,0,0,0.04)] p-4 sm:p-6 md:p-8">
            <div className="flex items-center gap-3 mb-6">
                <span className="text-[10px] font-mono text-[#b0aea6]">
                    {index}
                </span>
                <p className="text-[11px] tracking-[0.12em] uppercase text-[#6b6b6b]">{title}</p>
            </div>
            {children}
        </section>
    );
}

function Label({ children }) {
    return (
        <label className="block text-[11px] tracking-[0.08em] uppercase text-[#6b6b6b] font-medium">
            {children}
        </label>
    );
}

function Row({ label, value, onChange, onBlur, type = "text", testid, hint }) {
    const inputClass =
        "mt-2 w-full bg-white border border-[#eaeaea] rounded-lg px-4 h-11 text-[16px] md:text-[15px] text-[#1a1a1a] placeholder:text-[#b0aea6] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] outline-none transition-all duration-150";
    return (
        <div>
            <Label>{label}</Label>
            {type === "date" ? (
                <DobInput
                    value={value}
                    onChange={onChange}
                    onBlur={onBlur}
                    max={new Date().toISOString().split("T")[0]}
                    testid={testid}
                    className={inputClass}
                />
            ) : (
                <input
                    type={type}
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    onBlur={onBlur}
                    data-testid={testid}
                    className={inputClass}
                />
            )}
            {hint && (
                <p className="text-[10px] text-[#8b8b8b] font-mono mt-1.5">
                    {hint}
                </p>
            )}
        </div>
    );
}

function ApplyLookGroup({
    label,
    hint,
    items,
    category,
    allCount,
    maxImages,
    inputRef,
    upload,
    removeMedia,
    activeUploads = {},
    testidPrefix,
}) {
    const isUploading = Object.values(activeUploads).some(u => u.category === category && (u.status === "uploading" || u.status === "processing"));
    const reachedCap = allCount >= maxImages;
    return (
        <div className="mb-2" data-testid={`apply-look-group-${testidPrefix}`}>
            <div className="flex items-center justify-between mb-1">
                <p className="text-sm text-[#1a1a1a] font-medium">{label}</p>
                <span className="text-[10px] font-mono text-[#8b8b8b]">
                    {items.length}
                </span>
            </div>
            {hint && (
                <p className="text-xs text-[#8b8b8b] mb-3">{hint}</p>
            )}
            <input
                ref={inputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => upload(e.target.files, category)}
            />
            <div className="grid grid-cols-3 sm:grid-cols-4 gap-3">
                {items.map((m) => (
                    <div
                        key={m.id}
                        data-testid={`${testidPrefix}-image-${m.id}`}
                        className="relative aspect-[3/4] bg-[#f5f4f0] rounded-xl border border-[#eaeaea] overflow-hidden group shadow-sm"
                    >
                        <img
                            src={m.url}
                            alt=""
                            loading="lazy"
                            className="w-full h-full object-cover"
                        />
                        <button
                            onClick={() => removeMedia(m.id)}
                            data-testid={`${testidPrefix}-image-remove-${m.id}`}
                            className="absolute top-1 right-1 w-6 h-6 bg-white/90 rounded-lg opacity-0 group-hover:opacity-100 flex items-center justify-center hover:bg-[#d03a2a] hover:text-white transition-all duration-150 shadow-sm"
                        >
                            <Trash2 className="w-3 h-3" />
                        </button>
                    </div>
                ))}
                {!reachedCap && (
                    <button
                        type="button"
                        onClick={() => inputRef.current?.click()}
                        disabled={isUploading}
                        data-testid={`apply-add-${testidPrefix}-btn`}
                        className="aspect-[3/4] bg-[#f5f4f0] border border-dashed border-[#eaeaea] rounded-xl flex flex-col items-center justify-center gap-1 text-xs text-[#6b6b6b] hover:bg-[#efede8] transition-colors duration-150 disabled:opacity-50"
                    >
                        {isUploading ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Plus className="w-4 h-4" />
                        )}
                        <span>Add</span>
                    </button>
                )}
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Work-links helpers
// ---------------------------------------------------------------------------
const URL_RE = /https?:\/\/[^\s]+/;

/** Convert a single stored work-link string → { label, url } */
function parseStoredLink(stored) {
    if (typeof stored === "string" && stored.includes(" || ")) {
        const idx = stored.indexOf(" || ");
        return { label: stored.slice(0, idx).trim(), url: stored.slice(idx + 4).trim() };
    }
    return { label: "", url: stored || "" };
}

/**
 * Parse a multiline text block → array of stored link strings.
 * Supports: "Label - URL", "Label: URL", "Label URL", bare URL.
 */
function parseWorkLinksText(text) {
    return text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
            const match = URL_RE.exec(line);
            if (!match) return null;
            const url = match[0];
            const before = line.slice(0, match.index).replace(new RegExp("[-:" + "\\s" + "|]+$"), "").trim();
            return before ? `${before} || ${url}` : url;
        })
        .filter(Boolean);
}

/** Convert stored links array → editable textarea string */
function linksToText(links) {
    return (links || [])
        .map((w) => {
            const { label, url } = parseStoredLink(w);
            return label ? `${label} - ${url}` : url;
        })
        .join("\n");
}

function ApplyWorkLinksEditor({ links, onChange }) {
    const [draft, setDraft] = useState(() => linksToText(links));

    // Keep draft in sync if links change externally (e.g. prefill)
    const linksKey = JSON.stringify(links || []);
    const prevLinksKey = React.useRef(linksKey);
    React.useEffect(() => {
        if (prevLinksKey.current !== linksKey) {
            setDraft(linksToText(JSON.parse(linksKey)));
            prevLinksKey.current = linksKey;
        }
    }, [linksKey]);

    const parsed = parseWorkLinksText(draft);

    const handleChange = (e) => {
        const text = e.target.value;
        setDraft(text);
        onChange(parseWorkLinksText(text));
    };

    return (
        <div className="mt-2 space-y-2" data-testid="apply-work-links-editor">
            <textarea
                value={draft}
                onChange={handleChange}
                data-testid="apply-work-link-input"
                rows={5}
                placeholder={
                    "Paste all your work links here, one per line.\n" +
                    "Examples:\n" +
                    "Puma Campaign - https://instagram.com/reel/abc\n" +
                    "Pepsi - https://youtu.be/xyz\n" +
                    "https://vimeo.com/showreel"
                }
                className="w-full bg-white border border-[#eaeaea] rounded-lg p-4 text-[16px] md:text-[14px] text-[#1a1a1a] placeholder:text-[#b0aea6] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] outline-none transition-all duration-150 resize-y font-mono leading-relaxed"
            />
            <div className="flex items-center gap-2">
                <span
                    className={`text-[11px] font-mono px-2 py-0.5 rounded-full border ${
                        parsed.length > 0
                            ? "text-emerald-700 bg-emerald-50 border-emerald-100"
                            : "text-[#b0aea6] bg-[#faf9f6] border-[#eaeaea]"
                    }`}
                    data-testid="apply-work-links-count"
                >
                    Detected Links: {parsed.length}
                </span>
                {parsed.length > 0 && (
                    <span className="text-[10px] text-[#8b8b8b]">
                        {parsed.map((s) => parseStoredLink(s).label || "Unlabeled").join(" · ")}
                    </span>
                )}
            </div>
            {parsed.length > 0 && (
                <div className="space-y-1.5 pt-1" data-testid="apply-work-links-preview">
                    {parsed.map((stored, i) => {
                        const { label, url } = parseStoredLink(stored);
                        return (
                            <div
                                key={i}
                                className="flex items-center gap-2 px-3 py-2 bg-[#faf9f6] border border-[#eaeaea] rounded-lg"
                                data-testid={`apply-work-link-row-${i}`}
                            >
                                {label && (
                                    <span className="text-[11px] text-[#6b6b6b] font-medium shrink-0 max-w-[120px] truncate">
                                        {label}
                                    </span>
                                )}
                                <a
                                    href={url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-[11px] font-mono text-[#4a4a4a] hover:text-[#1a1a1a] truncate underline underline-offset-2 flex-1 min-w-0"
                                >
                                    {url}
                                </a>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Interested In Chip Selector
// ---------------------------------------------------------------------------
const INTERESTED_IN_CATEGORIES = [
    "Acting",
    "Modeling",
    "Influencer Campaigns",
];

function InterestedInSelector({ selected, onChange }) {
    const toggle = (cat) => {
        const set = new Set(selected);
        if (set.has(cat)) {
            set.delete(cat);
        } else {
            set.add(cat);
        }
        onChange([...set]);
    };

    return (
        <div
            className="flex flex-wrap gap-2.5"
            data-testid="interested-in-selector"
            role="group"
            aria-label="Work categories"
        >
            {INTERESTED_IN_CATEGORIES.map((cat) => {
                const active = selected.includes(cat);
                return (
                    <button
                        key={cat}
                        type="button"
                        onClick={() => toggle(cat)}
                        data-testid={`interested-in-${cat.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}
                        aria-pressed={active}
                        className={[
                            "px-4 py-2 rounded-full border text-xs tracking-[0.06em] transition-all duration-150 select-none",
                            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#b0aea6]",
                            "active:scale-95",
                            active
                                ? "border-[#1a1a1a] bg-[#1a1a1a] text-white shadow-sm"
                                : "border-[#eaeaea] bg-white text-[#4a4a4a] hover:border-[#9a9890] hover:bg-[#f5f4f0]",
                        ].join(" ")}
                    >
                        {cat}
                    </button>
                );
            })}
            {selected.length > 0 && (
                <button
                    type="button"
                    onClick={() => onChange([])}
                    className="px-3 py-2 rounded-full border border-transparent text-[10px] text-[#8b8b8b] hover:text-[#d03a2a] transition-colors duration-150 font-mono"
                    data-testid="interested-in-clear"
                >
                    Clear all
                </button>
            )}
        </div>
    );
}
