'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { api as axios, PORTAL_TOKEN_KEY } from "@/lib/api";
import { toast } from "sonner";
import { useUploadManager } from "@/context/UploadManagerContext";
import MaterialModal from "@/components/MaterialModal";
import Logo from "@/components/Logo";
import SkillsSelector from "@/components/SkillsSelector";
import LocationSelector from "@/components/LocationSelector";
import ThemeToggle from "@/components/ThemeToggle";
import { thumbnailUrl, posterUrl, normalizeInstagramHandle } from "@/lib/mediaUtils";
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
    FolderOpen,
    Upload,
    Video,
    Camera,
    Check,
    Trash2,
    Loader2,
    X,
    Sparkles,
    Plus,
    Mic,
    MessageSquare,
    ChevronDown,
    ArrowRight,
    ChevronRight,
    User,
    Search,
} from "lucide-react";
import {
    HEIGHT_OPTIONS,
    GENDER_OPTIONS,
    ETHNICITY_OPTIONS,
    FOLLOWER_TIERS,
    AVAILABILITY_OPTIONS,
    BUDGET_OPTIONS,
    calcAge,
} from "@/lib/talentSchema";

const MAX_IMAGES = 8;
// Phase 3: per-category portfolio image cap. Each of `image`/`indian`/
// `western` is independently capped at this value, NOT combined.
const MAX_IMAGES_PER_CATEGORY = 10;
const LS_KEY = (slug) => `tg_submission_${slug}`;
const LS_DRAFT_KEY = (slug) => `tg_draft_${slug}`;
// Long-lived opaque access token (stored in DB). Survives JWT expiry and
// cross-browser / cross-device scenarios where only the URL slug is known.
const LS_ATK_KEY = (slug) => `tg_atk_${slug}`;


function readSaved(slug) {
    if (typeof window === "undefined") return null;
    try {
        return JSON.parse(localStorage.getItem(LS_KEY(slug)) || "null");
    } catch {
        return null;
    }
}

// Draft form persistence — survives a refresh / app-switch on mobile so
// users never lose what they've typed even before the talent record is
// created on the backend.
function readDraft(slug) {
    if (typeof window === "undefined") return null;
    try {
        return JSON.parse(localStorage.getItem(LS_DRAFT_KEY(slug)) || "null");
    } catch {
        return null;
    }
}

const formatDuration = (sec) => {
    if (!sec) return null;
    const s = Math.round(sec);
    const mins = Math.floor(s / 60);
    const secs = s % 60;
    return `${mins}:${secs < 10 ? "0" : ""}${secs}`;
};

const formatMediaTimestamp = (m) => {
    const dStr = m?.updated_at || m?.created_at;
    if (!dStr) return null;
    try {
        const d = new Date(dStr);
        if (isNaN(d.getTime())) return null;
        return d.toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        });
    } catch {
        return null;
    }
};

function SubmissionPage() {
    const { slug } = useParams();
    const [project, setProject] = useState(null);
    const [loading, setLoading] = useState(true);
    const [saved, setSaved] = useState(() => readSaved(slug));
    const [showMaterial, setShowMaterial] = useState(false);
    const [activeLightboxImage, setActiveLightboxImage] = useState(null);

    // Full form (with draft restoration from localStorage)
    const [form, setForm] = useState(() => {
        const draft = readDraft(slug);
        const base = {
            first_name: "",
            last_name: "",
            email: "",
            phone: "",
            dob: "",
            age: "",
            overrideAge: false,
            submitted_age_override: "",
            height: "",
            location: [],
            // Phase 2 — schema unification: every talent-facing form writes
            // the SAME shape directly into the talent record. No separate
            // mappings.
            gender: "",
            ethnicity: "",
            instagram_handle: "",
            instagram_followers: "",
            bio: "",
            work_links: [],
            skills: [],
            competitive_brand: "",
            availability: { status: "", note: "" },
            budget: { status: "", value: "" },
            commission: "",
            custom_answers: {},
        };
        return draft ? { ...base, ...draft } : base;
    });
    const [starting, setStarting] = useState(false);
    const [submitAttempted, setSubmitAttempted] = useState(false);
    const [validationErrors, setValidationErrors] = useState({}); // { fieldId: errorMessage }
    const fieldRefs = useRef({}); // { fieldId: HTMLElement }

    const [submission, setSubmission] = useState(null);
    const { activeUploads, retryQueue, uploadFile, retryUpload } = useUploadManager();
    const [finalizing, setFinalizing] = useState(false);
    const [editMode, setEditMode] = useState(false);

    // Collapsible sections state
    const [collapsedSections, setCollapsedSections] = useState({
        profile: false,           // open by default
        projectQuestions: false,   // open by default
        uploads: false,            // open by default
    });
    const [isGenericPortfolioCollapsed, setIsGenericPortfolioCollapsed] = useState(() => {
        return typeof window !== "undefined" && window.innerWidth < 768;
    });


    // Portfolio (General) — tracks which thumbnail has its action overlay
    // visible on touch devices (tap-to-reveal). null = all overlays hidden.
    const [activePortfolioThumbId, setActivePortfolioThumbId] = useState(null);

    // Ref: prevents the ATK-resume useEffect from running more than once per mount.
    const atkTriedRef = useRef(false);


    // Email-first gate: hides every form section EXCEPT the email field
    // until the talent's email has been blurred and the prefill response
    // is processed (Use this / Edit manually / no match).
    // Initialised here (rather than later in the component body) so
    // validateForm / validateStep1 can read it without TDZ surprises.
    const [emailGateUnlocked, setEmailGateUnlocked] = useState(() => {
        if (typeof window === "undefined") return false;
        const hasDraft = !!readSaved(slug);
        const hasPortalSession = !!localStorage.getItem("talentgram_portal_email");
        return hasDraft || hasPortalSession;
    });
    const [prefillTried, setPrefillTried] = useState(false);
    const [prefillSuggestion, setPrefillSuggestion] = useState(null); // {data}
    const [prefillEmail, setPrefillEmail] = useState("");

    // Inline Portal Gateway states
    const [gatewayEmail, setGatewayEmail] = useState("");
    const [gatewayLoading, setGatewayLoading] = useState(false);
    const [gatewayRecognition, setGatewayRecognition] = useState(null);
    const [otpSent, setOtpSent] = useState(false);
    const [otpValue, setOtpValue] = useState("");
    const [otpLoading, setOtpLoading] = useState(false);
    const [otpResending, setOtpResending] = useState(false);


    const introRef = useRef();
    const take1Ref = useRef();
    const newTakeRef = useRef();
    const imagesRef = useRef();
    const cameraImagesRef = useRef(); // mobile camera-first photo capture
    const indianImagesRef = useRef();
    const westernImagesRef = useRef();
    const uploadsSectionRef = useRef();

    // Load project
    useEffect(() => {
        (async () => {
            try {
                const { data } = await axios.get(
                    `/public/projects/${slug}`,
                );
                setProject(data);
                // Snapshot commission on the form so it's preserved at submission time
                setForm((f) => ({
                    ...f,
                    commission: f.commission || data.commission_percent || "",
                }));
            } catch {
                toast.error("Project not found");
            } finally {
                setLoading(false);
            }
        })();
    }, [slug]);

    // Dismiss the portfolio-thumbnail action overlay when the user taps/clicks
    // anywhere outside of the active tile. Uses a deferred document listener
    // (setTimeout) so the same event that opened the overlay does not
    // immediately close it via bubbling.
    useEffect(() => {
        if (!activePortfolioThumbId) return;
        const dismiss = () => setActivePortfolioThumbId(null);
        const tid = setTimeout(() => document.addEventListener("click", dismiss), 0);
        return () => {
            clearTimeout(tid);
            document.removeEventListener("click", dismiss);
        };
    }, [activePortfolioThumbId]);

    // Prefill from query params, localStorage talent portal session, or Google Sign-In
    useEffect(() => {
        const urlParams = new URLSearchParams(window.location.search);
        const queryEmail = urlParams.get("email");
        const portalEmail = localStorage.getItem("talentgram_portal_email");
        
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
                setForm((f) => ({
                    ...f,
                    email: googleEmail,
                    first_name: f.first_name || first,
                    last_name: f.last_name || last,
                }));
                setEmailGateUnlocked(true);
                
                // Show welcome banner once
                const onboardKey = `tg_onboard_shown_${slug}`;
                if (!localStorage.getItem(onboardKey)) {
                    toast.success("Welcome to Talentgram! Let's create your profile");
                    localStorage.setItem(onboardKey, "true");
                }
            }
            return;
        }

        const emailToPrefill = queryEmail || portalEmail;
        if (emailToPrefill && !form.email) {
            const formatted = emailToPrefill.trim().toLowerCase();
            setForm((f) => ({ ...f, email: formatted }));
            setPrefillEmail(formatted);
            setEmailGateUnlocked(true);
            
            // Trigger pre-fill lookup immediately
            (async () => {
                try {
                    const { data } = await axios.get(
                        `/public/prefill?email=${encodeURIComponent(formatted)}`,
                    );
                    if (data && Object.keys(data).length > 0 && data.first_name) {
                        populatePrefillData(data);
                        setPrefillSuggestion({ data });
                        setPrefillTried(true);
                    }
                } catch (e) {
                    console.error("Auto prefill lookup failed:", e);
                }
            })();
        }
    }, [form.email, slug]);


    // Branded page title — replaces the raw slug-based title users used to
    // see in the browser tab. Shape: "Talentgram | <Brand> Audition".
    useEffect(() => {
        const prev = document.title;
        const brand = (project?.brand_name || "").trim();
        document.title = brand
            ? `Talentgram | ${brand} Audition`
            : "Talentgram | Audition";
        return () => {
            document.title = prev;
        };
    }, [project?.brand_name]);

    // Resume submission
    useEffect(() => {
        if (!saved?.token || !saved?.id) return;
        (async () => {
            try {
                const { data } = await axios.get(
                    `/public/submissions/${saved.id}`,
                    { headers: { Authorization: `Bearer ${saved.token}` } },
                );
                setSubmission(data);
                if (data.form_data) {
                    setForm((f) => {
                        const fd = data.form_data;
                        return {
                            ...f,
                            ...fd,
                            availability:
                                typeof fd.availability === "object" &&
                                fd.availability !== null
                                    ? { status: "", note: "", ...fd.availability }
                                    : f.availability,
                            budget:
                                typeof fd.budget === "object" && fd.budget !== null
                                    ? { status: "", value: "", ...fd.budget }
                                    : f.budget,
                        };
                    });
                }
            } catch {
                localStorage.removeItem(LS_KEY(slug));
                setSaved(null);
            }
        })();
    }, [saved, slug]);

    // Persistent ATK-based resume — runs after JWT resume path so it only
    // fires when `saved` is null (no valid JWT in localStorage). Uses the
    // long-lived opaque access_token stored in LS_ATK_KEY to call the
    // /public/projects/{slug}/submission/me endpoint and restore the full
    // submission state without re-entering any identity details.
    useEffect(() => {
        if (saved) return;          // JWT resume already handled this session
        if (atkTriedRef.current) return; // already attempted once this mount
        const atk = localStorage.getItem(LS_ATK_KEY(slug));
        if (!atk) return;
        atkTriedRef.current = true;
        (async () => {
            try {
                const { data } = await axios.get(
                    `/public/projects/${slug}/submission/me`,
                    { params: { atk } },
                );
                if (data?.id) {
                    // Restore saved state — use the ATK as the bearer token
                    // (decode_submitter now supports opaque ATK lookup)
                    const next = { id: data.id, token: atk };
                    localStorage.setItem(LS_KEY(slug), JSON.stringify(next));
                    setSaved(next);
                    setSubmission(data);
                    if (data.form_data) {
                        const fd = data.form_data;
                        setForm((f) => ({
                            ...f,
                            ...fd,
                            availability:
                                typeof fd.availability === "object" && fd.availability !== null
                                    ? { status: "", note: "", ...fd.availability }
                                    : f.availability,
                            budget:
                                typeof fd.budget === "object" && fd.budget !== null
                                    ? { status: "", value: "", ...fd.budget }
                                    : f.budget,
                        }));
                    }
                    // Restore the email into the form so it's visible on
                    // the dashboard header and any validation checks pass.
                    if (data.talent_email) {
                        setForm((f) => ({ ...f, email: data.talent_email }));
                    }
                    setEmailGateUnlocked(true);
                }
            } catch {
                // Token invalid or submission deleted — clear stale ATK.
                localStorage.removeItem(LS_ATK_KEY(slug));
            }
        })();
    // `saved` in deps: if JWT resume runs first and sets saved→null (expired),
    // this effect re-evaluates and runs the ATK check as a fallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [slug, saved]);

    const computedAge = useMemo(() => {
        if (form.overrideAge && form.submitted_age_override) {
            return parseInt(form.submitted_age_override, 10) || null;
        }
        return calcAge(form.dob) ?? (form.age ? parseInt(form.age, 10) : null);
    }, [form.dob, form.age, form.overrideAge, form.submitted_age_override]);

    const authCfg = useMemo(
        () =>
            saved?.token
                ? { headers: { Authorization: `Bearer ${saved.token}` } }
                : {},
        [saved],
    );

    const validateForm = () => {
        // Email-first ordering (Phase 1 v37 fix): the email field is the
        // gate that reveals every other field. Validation MUST surface
        // an email error before any other "required" message — otherwise
        // a returning user who hasn't filled email yet sees a confusing
        // "First name is required" toast.
        if (!form.email.trim()) return "Email is required";
        // Email-first gate guard: if the rest of the form isn't unlocked
        // yet (no prefill decision made), we deliberately skip every
        // other validation. The Continue button is already gated on
        // `emailGateUnlocked` in the UI; this is a defense-in-depth.
        if (!emailGateUnlocked) return "Please complete the email step first";
        if (!form.first_name.trim()) return "First name is required";
        if (!form.last_name.trim()) return "Last name is required";
        if (!form.height) return "Height is required";
        if (!form.location || form.location.length === 0) return "Current location is required";
        if (!form.availability.status) return "Please confirm your availability";
        if (
            form.availability.status === "no" &&
            !form.availability.note.trim()
        )
            return "Please share your alternate availability";
        if (!form.budget.status) return "Please confirm the budget";
        if (form.budget.status === "custom" && !form.budget.value.trim())
            return "Please enter your expected budget";
        return null;
    };

    // Mobile wizard step validators — narrower than the full form so users
    // can advance after completing only the current step's fields.
    const validateStep1 = () => {
        // Same email-first rule as validateForm. Step 1 IS the talent-
        // details step, so without a confirmed email we have nothing to
        // validate against.
        if (!form.email.trim()) return "Email is required";
        if (!emailGateUnlocked) return "Please complete the email step first";
        if (!form.first_name.trim()) return "First name is required";
        if (!form.last_name.trim()) return "Last name is required";
        if (!form.height) return "Height is required";
        if (!form.location || form.location.length === 0) return "Current location is required";
        return null;
    };
    const validateStep2 = () => {
        if (!form.availability.status) return "Please confirm your availability";
        if (form.availability.status === "no" && !form.availability.note.trim())
            return "Please share your alternate availability";
        if (!form.budget.status) return "Please confirm the budget";
        if (form.budget.status === "custom" && !form.budget.value.trim())
            return "Please enter your expected budget";
        return null;
    };

    // Persist a debounced draft of the form so a refresh / app-switch never
    // loses progress before the talent record is created on the backend.
    useEffect(() => {
        const t = setTimeout(() => {
            try {
                localStorage.setItem(LS_DRAFT_KEY(slug), JSON.stringify(form));
            } catch (e) { console.error(e); }
        }, 1200);
        return () => clearTimeout(t);
    }, [form, slug]);
    // Convenience wrapper that returns a boolean (vs the form-handler version).
    async function startSubmissionDirect() {
        const err = validateForm();
        if (err) {
            toast.error(err);
            return null;
        }
        setStarting(true);
        try {
            const { data } = await axios.post(
                `/public/projects/${slug}/submission`,
                {
                    name: `${form.first_name} ${form.last_name}`.trim(),
                    email: form.email.trim().toLowerCase(),
                    phone: form.phone || null,
                    age: computedAge != null ? String(computedAge) : form.age || null,
                    height: form.height,
                    location: form.location,
                    // Phase 2 unified identity
                    dob: form.dob || null,
                    gender: form.gender || null,
                    ethnicity: form.ethnicity || null,
                    instagram_handle: form.instagram_handle || null,
                    instagram_followers: form.instagram_followers || null,
                    bio: form.bio || null,
                    work_links: form.work_links || [],
                    competitive_brand: form.competitive_brand || null,
                    availability: form.availability,
                    budget: form.budget,
                    skills: form.skills || [],
                    custom_answers: form.custom_answers,
                    commission_percent: form.commission || null,
                },
            );
            const next = { id: data.id, token: data.token };
            localStorage.setItem(LS_KEY(slug), JSON.stringify(next));
            // Persist the long-lived access_token separately so the talent
            // can resume their submission from any browser/device as long as
            // this localStorage key survives (much longer than the 3-day JWT).
            if (data.access_token) {
                localStorage.setItem(LS_ATK_KEY(slug), data.access_token);
            }
            setSaved(next);
            setSubmission(data);
            setCollapsedSections((prev) => ({ ...prev, uploads: false }));
            toast.success("✓ Details saved successfully.");
            return next;
        } catch (e) {
            // P0-2: ownership now required when a record already exists for the
            // email. Route the returning talent through the one-time-code flow.
            if (e?.response?.status === 403) {
                const verifyEmail = (form.email || "").trim().toLowerCase();
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
                return null;
            }
            toast.error(e?.response?.data?.detail || "Could not save profile");
            return null;
        } finally {
            setStarting(false);
        }
    }

    // Email-first auto-fill (Phase 1).
    //
    // On email blur we hit `/api/public/prefill`. If we get a hit, we DO NOT
    // silently overwrite — we surface a small inline confirmation card with
    // "Use this" / "Edit manually" so the talent stays in control.
    //
    // Strict scope (per Phase 1 spec):
    //   ✅ Auto-fill: first_name, last_name, age, dob, height, phone, location, instagram_*
    //   ❌ NEVER auto-fill: any media (intro, takes, images), previous form
    //      answers, custom-questions, availability/budget/commission.
    //
    // The user can re-trigger prefill by re-entering the email — we only
    // auto-trigger ONCE per email value.
    function populatePrefillData(data) {
        if (!data) return;
        setForm((f) => ({
            ...f,
            first_name: f.first_name || data.first_name || "",
            last_name: f.last_name || data.last_name || "",
            phone: f.phone || data.phone || "",
            age: f.age || (data.age != null ? String(data.age) : ""),
            dob: f.dob || data.dob || "",
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
        if (data.prefill_media) {
            setSubmission((s) => ({
                ...(s || {}),
                media: data.prefill_media || [],
            }));
            
            // Debugging requirement:
            console.log("[DEBUG] Talent found");
            console.log("[DEBUG] Profile loaded");
            console.log(`[DEBUG] Skills loaded count: ${(data.skills || []).length}`);
            console.log(`[DEBUG] Images loaded count: ${(data.prefill_media || []).filter(m => m.category !== "intro_video" && m.category !== "video").length}`);
            console.log(`[DEBUG] Videos loaded count: ${(data.prefill_media || []).filter(m => m.category === "intro_video" || m.category === "video").length}`);
            console.log(`[DEBUG] Portfolio categories loaded: ${Array.from(new Set((data.prefill_media || []).map(m => m.category))).join(", ")}`);
        }
    }

    const tryPrefill = async () => {
        if (saved) return; // submission already started — too late
        const email = (form.email || "").trim().toLowerCase();
        if (!email || !email.includes("@")) return;
        if (email === prefillEmail && prefillTried) return; // already tried
        setPrefillEmail(email);
        setPrefillTried(true);
        try {
            const { data } = await axios.get(
                `/public/prefill?email=${encodeURIComponent(email)}`,
            );
            if (!data || !data.first_name) {
                // New talent — quietly unlock the rest of the form.
                setPrefillSuggestion(null);
                setEmailGateUnlocked(true);
                return;
            }
            // Returning talent — auto-load and open form immediately
            populatePrefillData(data);
            setPrefillSuggestion({ data });
            setEmailGateUnlocked(true);
            toast.success(`Welcome back, ${data.first_name}`);
        } catch {
            // 429 (rate-limited) or network — fail silently AND unlock so
            // the user isn't blocked behind a transient network error.
            setEmailGateUnlocked(true);
        }
    };

    const handleUseAnotherEmail = () => {
        localStorage.removeItem("talentgram_portal_email");
        localStorage.removeItem("talentgram_google_email");
        localStorage.removeItem("talentgram_google_first_name");
        localStorage.removeItem("talentgram_google_last_name");
        localStorage.removeItem("talentgram_google_avatar");
        localStorage.removeItem("talentgram_google_profile_data");
        const onboardKey = `tg_onboard_shown_${slug}`;
        localStorage.removeItem(onboardKey);
        
        setForm({
            first_name: "",
            last_name: "",
            email: "",
            phone: "",
            dob: "",
            age: "",
            overrideAge: false,
            submitted_age_override: "",
            height: "",
            location: [],
            gender: "",
            ethnicity: "",
            instagram_handle: "",
            instagram_followers: "",
            bio: "",
            work_links: [],
            skills: [],
            competitive_brand: "",
            availability: { status: "", note: "" },
            budget: { status: "", value: "" },
            commission: project ? (project.commission_percent || "") : "",
            custom_answers: {},
        });
        setPrefillEmail("");
        setPrefillTried(false);
        setPrefillSuggestion(null);
        setEmailGateUnlocked(false);
        setGatewayRecognition(null);
        setGatewayEmail("");
        toast.info("Please enter your email to proceed.");
    };

    const handleGoogleLogin = () => {
        const clientId = process.env.REACT_APP_GOOGLE_CLIENT_ID || "339414275037-rrm7uugj1t4gq2b02q9r51d9l6m39vbe.apps.googleusercontent.com";
        const redirectUri = `${window.location.origin}/google-callback`;
        const state = slug;
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
                slug: slug
            });

            if (data.existing) {
                if (data.token && data.submission_id) {
                    const ref = { id: data.submission_id, token: data.token };
                    localStorage.setItem(`tg_submission_${slug}`, JSON.stringify(ref));
                    localStorage.setItem(`tg_atk_${slug}`, data.token);
                    setSaved(ref);
                    toast.success("Welcome back!");
                } else {
                    toast.success("Welcome back!");
                }
                if (data.talent) {
                    populatePrefillData(data.talent);
                    setPrefillSuggestion({ data: data.talent });
                    setPrefillTried(true);
                }
            } else {
                toast.success("Successfully authenticated. Welcome to Talentgram!");
            }

            // OTP proved ownership — persist the portal session token (Path B).
            if (data.portal_token) {
                localStorage.setItem(PORTAL_TOKEN_KEY, data.portal_token);
            }
            localStorage.setItem("talentgram_portal_email", trimmedEmail);
            setForm((f) => ({ ...f, email: trimmedEmail }));
            setPrefillEmail(trimmedEmail);
            setEmailGateUnlocked(true);
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
        setForm((f) => ({ ...f, email: formatted }));
        setPrefillEmail(formatted);
        setEmailGateUnlocked(true);
        
        if (gatewayRecognition.isGoogle) {
            const profileDataStr = localStorage.getItem("talentgram_google_profile_data");
            if (profileDataStr) {
                const profileData = JSON.parse(profileDataStr);
                populatePrefillData(profileData);
                setPrefillSuggestion({ data: profileData });
                setPrefillTried(true);
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
                    populatePrefillData(data);
                    setPrefillSuggestion({ data });
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
        const onboardKey = `tg_onboard_shown_${slug}`;
        localStorage.removeItem(onboardKey);
        setGatewayRecognition(null);
        setGatewayEmail("");
    };

    const startSubmission = async (e) => {
        e.preventDefault();
        const err = validateForm();
        if (err) {
            toast.error(err);
            return;
        }
        setStarting(true);
        try {
            const payload = {
                name: `${form.first_name} ${form.last_name}`.trim(),
                email: form.email,
                phone: form.phone || null,
                form_data: {
                    first_name: form.first_name,
                    last_name: form.last_name,
                    dob: form.dob || null,
                    age: computedAge != null ? String(computedAge) : "",
                    height: form.height,
                    location: form.location,
                    // Phase 2 unified identity (mirrored to talent on finalize)
                    gender: form.gender || "",
                    ethnicity: form.ethnicity || "",
                    instagram_handle: form.instagram_handle || "",
                    instagram_followers: form.instagram_followers || "",
                    bio: form.bio || "",
                    work_links: form.work_links || [],
                    skills: form.skills || [],
                    competitive_brand: project.competitive_brand_enabled
                        ? form.competitive_brand
                        : "",
                    availability: form.availability,
                    budget: form.budget,
                    custom_answers: form.custom_answers || {},
                },
            };
            const { data } = await axios.post(
                `/public/projects/${slug}/submission`,
                payload,
            );
            const ref = { id: data.id, token: data.token };
            localStorage.setItem(LS_KEY(slug), JSON.stringify(ref));
            // Persist the long-lived access_token so the talent can resume
            // from any browser/device using the ATK-based resume path.
            if (data.access_token) {
                localStorage.setItem(LS_ATK_KEY(slug), data.access_token);
            }
            setSaved(ref);
            setCollapsedSections((prev) => ({ ...prev, uploads: false }));
            toast.success("✓ Details saved successfully. Next step: Upload your introduction video, audition takes and portfolio images.");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to start");
        } finally {
            setStarting(false);
        }
    };

    // Function declaration (not `const = async () =>`) so it's fully hoisted
    // and `goToStep` above can call it without a TDZ reference.
    async function saveForm() {
        if (!saved) return;
        try {
            await axios.put(
                `/public/submissions/${saved.id}`,
                {
                    form_data: {
                        ...form,
                        age:
                            computedAge != null
                                ? String(computedAge)
                                : form.age || "",
                    },
                },
                authCfg,
            );
        } catch (e) { console.error(e); }
    }

    const triggerUpload = async (file, category, label = null) => {
        await uploadFile(file, category, label, {
            token: saved?.token,
            endpoint: saved ? `/public/submissions/${saved.id}/upload` : null,
            onSuccess: (data) => {
                setSubmission(data);
            },
            onBeforeUpload: async () => {
                let currentSaved = saved;
                if (!currentSaved) {
                    const err = validateStep1();
                    if (err) {
                        toast.error("Please complete the required Profile fields first before uploading files.");
                        setCollapsedSections((prev) => ({ ...prev, profile: false }));
                        return null;
                    }
                    const next = await startSubmissionDirect();
                    if (!next) return null;
                    currentSaved = next;
                }
                return {
                    token: currentSaved.token,
                    endpoint: `/public/submissions/${currentSaved.id}/upload`
                };
            }
        });
    };

    const patchTakeLabel = async (mid, label) => {
        try {
            const { data } = await axios.patch(
                `/public/submissions/${saved.id}/media/${mid}`,
                { label },
                authCfg,
            );
            setSubmission(data);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Could not rename");
        }
    };

    // Media derivations (initialised here before uploadImages so the
    // `allImages.length` read inside it never sees an undefined value).
    // `submission` is React state — it's null on first render, so `media`
    // safely falls back to []. Kept as `const` so re-renders pick up the
    // latest submission state automatically.
    const media = submission?.media || [];
    // Phase 2 — portfolio images come in 3 flavours: generic, indian look,
    // western look. They share the MAX_IMAGES bucket so the talent doesn't
    // exceed the 8-image cap by splitting categories.
    const images = media.filter((m) => m.category === "image");
    const indianImages = media.filter((m) => m.category === "indian");
    const westernImages = media.filter((m) => m.category === "western");
    const allImages = [...images, ...indianImages, ...westernImages];

    const intro = media.find((m) => m.category === "intro_video");
    const takes = media
        .filter(
            (m) =>
                m.category === "take" ||
                m.category === "take_1" ||
                m.category === "take_2" ||
                m.category === "take_3",
        )
        .map((m) => {
            if (m.category === "take") return m;
            const n = m.category.replace("take_", "");
            return { ...m, _legacy: true, label: m.label || `Take ${n}` };
        });

    const activeConditionalVideoRules = useMemo(() => {
        if (!project || !Array.isArray(project.conditional_video_rules)) return [];
        return project.conditional_video_rules.filter((rule) => {
            const ans = (form.custom_answers || {})[rule.question_id];
            return (
                ans &&
                String(ans).trim().toLowerCase() ===
                    String(rule.trigger_value).trim().toLowerCase()
            );
        });
    }, [project, form.custom_answers]);

    const regularTakes = takes.filter(
        (t) => !activeConditionalVideoRules.some((r) => r.video_label === t.label),
    );

    const uploadImages = async (files, imageCategory = "image") => {
        // Phase 3 — per-category cap (10 each), not combined. Look up the
        // current count of THIS category and refuse uploads that would
        // overflow it.
        const currentForCategory =
            imageCategory === "indian"
                ? indianImages.length
                : imageCategory === "western"
                  ? westernImages.length
                  : images.length;
        const room = MAX_IMAGES_PER_CATEGORY - currentForCategory;
        const accepted = Array.from(files).slice(0, Math.max(0, room));
        if (room <= 0) {
            const label = imageCategory === "indian" ? "Indian look" : imageCategory === "western" ? "Western look" : "Portfolio";
            toast.error(`${label} image limit reached (${MAX_IMAGES_PER_CATEGORY})`);
            return;
        }
        if (files.length > room) {
            toast.info(`Only ${room} more ${imageCategory} images allowed (max ${MAX_IMAGES_PER_CATEGORY})`);
        }
        // Client-side per-image cap (20 MB) (P5)
        const over = accepted.find((f) => f.size > 20 * 1024 * 1024);
        if (over) {
            toast.error(`"${over.name}" is too large (max 20 MB per image).`);
            return;
        }
        const badFormat = accepted.find((f) => {
            const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
            return ['.bmp', '.tiff', '.heic', '.heif'].includes(ext) || ['image/bmp', 'image/tiff', 'image/heic', 'image/heif'].includes(f.type);
        });
        if (badFormat) {
            toast.error(`HEIC, BMP, and TIFF formats are not supported. Please upload JPEG or PNG.`);
            return;
        }
        const unsupportedImage = accepted.find((f) => {
            const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
            return !f.type.startsWith('image/') && !['.jpg', '.jpeg', '.png', '.webp'].includes(ext);
        });
        if (unsupportedImage) {
            toast.error(`"${unsupportedImage.name}" is not a supported image format. Please upload JPG, PNG, or WEBP.`);
            return;
        }

        await Promise.all(
            accepted.map((f) => triggerUpload(f, imageCategory, f.name))
        );
    };

    const removeMedia = async (mid) => {
        try {
            await axios.delete(
                `/public/submissions/${saved.id}/media/${mid}`,
                authCfg,
            );
            const { data } = await axios.get(
                `/public/submissions/${saved.id}`,
                authCfg,
            );
            setSubmission(data);
        } catch {
            toast.error("Could not remove file");
        }
    };

    const replaceMediaFile = async (oldMedia, file) => {
        const isVideoSlot = ["intro_video", "take", "take_1", "take_2", "take_3"].includes(oldMedia.category);
        const label = oldMedia.category === "take" ? oldMedia.label : (!isVideoSlot ? file.name : null);
        await triggerUpload(file, oldMedia.category, label);
        await removeMedia(oldMedia.id);
    };

    const finalize = async () => {
        // ── Guided validation: run inline before network call ──────────────
        const missing = getMissingRequirements();
        if (missing.length > 0) {
            // Build error map
            const errors = {};
            missing.forEach((req) => {
                errors[req.id] = req.label + " is required";
            });
            setValidationErrors(errors);

            // Expand collapsed sections that contain errors
            const sectionsToOpen = {};
            missing.forEach((req) => {
                if (req.section === "profile" && collapsedSections.profile) {
                    sectionsToOpen.profile = false;
                }
                if (req.section === "uploads" && collapsedSections.uploads) {
                    sectionsToOpen.uploads = false;
                }
                if (req.section === "projectQuestions" && collapsedSections.projectQuestions) {
                    sectionsToOpen.projectQuestions = false;
                }
            });
            if (Object.keys(sectionsToOpen).length > 0) {
                setCollapsedSections((prev) => ({ ...prev, ...sectionsToOpen }));
            }

            // Scroll + focus the first error after a brief paint
            setTimeout(() => {
                const first = missing[0];
                // Try fieldRefs first (most precise — actual input element)
                const refEl = fieldRefs.current[first.id];
                if (refEl) {
                    refEl.scrollIntoView({ behavior: "smooth", block: "center" });
                    try { refEl.focus(); } catch (_) {}
                    return;
                }
                // Fallback: CSS selector on the data-testid attribute
                if (first.selector) {
                    const domEl = document.querySelector(first.selector);
                    if (domEl) {
                        domEl.scrollIntoView({ behavior: "smooth", block: "center" });
                        const focusable = domEl.querySelector(
                            "input, select, textarea, button"
                        );
                        if (focusable) {
                            try { focusable.focus(); } catch (_) {}
                        }
                    }
                }
            }, 120);

            toast.error(
                `Please fill in: ${missing[0].label}${
                    missing.length > 1 ? ` (+${missing.length - 1} more)` : ""
                }`
            );
            return;
        }

        const isResubmission = submission && submission.status && submission.status !== "draft";
        
        // All good — clear any stale errors and proceed
        setValidationErrors({});

        let currentSaved = saved;
        if (!currentSaved) {
            const next = await startSubmissionDirect();
            if (!next) return;
            currentSaved = next;
        } else {
            await saveForm();
        }
        setFinalizing(true);
        try {
            await axios.post(
                `/public/submissions/${currentSaved.id}/finalize`,
                {},
                {
                    headers: {
                        Authorization: `Bearer ${currentSaved.token}`,
                    }
                },
            );
            const { data } = await axios.get(
                `/public/submissions/${currentSaved.id}`,
                {
                    headers: {
                        Authorization: `Bearer ${currentSaved.token}`,
                    }
                },
            );
            setSubmission(data);
            setEditMode(false);
            // Once the user finalises, clear the local draft — the
            // canonical state lives on the backend now.
            try { localStorage.removeItem(LS_DRAFT_KEY(slug)); } catch (e) { console.error(e); }
            
            if (isResubmission) {
                toast.success("Your audition has been updated successfully.");
                setTimeout(() => {
                    toast.success("The Talentgram team will review the latest version.");
                }, 500);
            } else {
                toast.success("Your audition has been received.");
            }
        } catch (err) {
            toast.error(
                err?.response?.data?.detail || "Please complete all required fields",
            );
        } finally {
            setFinalizing(false);
        }
    };

    const getMissingRequirements = useCallback(() => {
        const missingList = [];
        const requirements = project?.submission_requirements;
        if (!requirements) {
            // Fallback legacy validation rules
            if (!form.first_name?.trim()) {
                missingList.push({ id: "first_name", label: "First name", section: "profile", selector: '[data-testid="form-first-name"]' });
            }
            if (!form.last_name?.trim()) {
                missingList.push({ id: "last_name", label: "Last name", section: "profile", selector: '[data-testid="form-last-name"]' });
            }
            if (!form.height?.trim()) {
                missingList.push({ id: "height", label: "Height", section: "profile", selector: '[data-testid="form-height-field"]' });
            }
            if (!form.location || form.location.length === 0) {
                missingList.push({ id: "location", label: "Current location", section: "profile", selector: '[data-testid="form-location"]' });
            }
            
            const avail = form.availability || {};
            const status = (avail.status || "").trim();
            if (status !== "yes" && status !== "no") {
                missingList.push({ id: "availability", label: "Availability (Yes / No)", section: "profile", selector: '[data-testid="availability-block"]' });
            } else if (status === "no" && !(avail.note || "").trim()) {
                missingList.push({ id: "availability_note", label: "Availability note", section: "profile", selector: '[data-testid="availability-note-input"]' });
            }

            const budget = form.budget || {};
            const bstatus = (budget.status || "").trim();
            if (bstatus !== "accept" && bstatus !== "custom") {
                missingList.push({ id: "budget", label: "Budget (Accept / Custom)", section: "profile", selector: '[data-testid="budget-block"]' });
            } else if (bstatus === "custom" && !(budget.value || "").trim()) {
                missingList.push({ id: "budget_value", label: "Expected budget details", section: "profile", selector: '[data-testid="budget-value-input"]' });
            }
            return missingList;
        }

        if (requirements.strictness !== "strict") {
            return [];
        }

        const fieldsConfig = requirements.fields || {};

        // 1. Standard Profile Fields
        if (fieldsConfig.name === "required") {
            if (!form.first_name?.trim()) {
                missingList.push({ id: "first_name", label: "First name", section: "profile", selector: '[data-testid="form-first-name"]' });
            }
            if (!form.last_name?.trim()) {
                missingList.push({ id: "last_name", label: "Last name", section: "profile", selector: '[data-testid="form-last-name"]' });
            }
        }
        if (fieldsConfig.email === "required" && !(submission?.talent_email || form.email)?.trim()) {
            missingList.push({ id: "email", label: "Email", section: "profile", selector: '[data-testid="form-email"]' });
        }
        if (fieldsConfig.phone === "required" && !form.phone?.trim()) {
            missingList.push({ id: "phone", label: "Phone", section: "profile", selector: '[data-testid="form-phone"]' });
        }
        if (fieldsConfig.dob === "required" && !form.dob?.trim()) {
            missingList.push({ id: "dob", label: "Date of Birth", section: "profile", selector: '[data-testid="form-dob"]' });
        }
        if (fieldsConfig.age === "required" && (form.age === undefined || form.age === null || String(form.age).trim() === "")) {
            missingList.push({ id: "age", label: "Age", section: "profile", selector: '[data-testid="form-age-field"]' });
        }
        if (fieldsConfig.height === "required" && !form.height?.trim()) {
            missingList.push({ id: "height", label: "Height", section: "profile", selector: '[data-testid="form-height-field"]' });
        }
        if (fieldsConfig.location === "required" && (!form.location || form.location.length === 0)) {
            missingList.push({ id: "location", label: "Current location", section: "profile", selector: '[data-testid="form-location"]' });
        }
        if (fieldsConfig.gender === "required" && !form.gender?.trim()) {
            missingList.push({ id: "gender", label: "Gender", section: "profile", selector: '[data-testid="form-gender-field"]' });
        }
        if (fieldsConfig.ethnicity === "required" && !form.ethnicity?.trim()) {
            missingList.push({ id: "ethnicity", label: "Ethnicity", section: "profile", selector: '[data-testid="form-ethnicity-field"]' });
        }
        if (fieldsConfig.instagram_handle === "required" && !form.instagram_handle?.trim()) {
            missingList.push({ id: "instagram_handle", label: "Instagram Handle", section: "profile", selector: '[data-testid="form-instagram-handle"]' });
        }
        if (fieldsConfig.instagram_followers === "required" && !form.instagram_followers?.trim()) {
            missingList.push({ id: "instagram_followers", label: "Instagram Followers", section: "profile", selector: '[data-testid="form-instagram-followers-field"]' });
        }
        if (fieldsConfig.bio === "required" && !form.bio?.trim()) {
            missingList.push({ id: "bio", label: "Bio", section: "profile", selector: '[data-testid="form-bio-field"]' });
        }
        if (fieldsConfig.competitive_brand === "required" && !form.competitive_brand?.trim()) {
            missingList.push({ id: "competitive_brand", label: "Competitive Brand details", section: "projectQuestions", selector: '[data-testid="form-competitive-brand"]' });
        }

        if (fieldsConfig.availability === "required") {
            const avail = form.availability || {};
            const status = (avail.status || "").trim();
            if (status !== "yes" && status !== "no") {
                missingList.push({ id: "availability", label: "Availability (Yes / No)", section: "projectQuestions", selector: '[data-testid="availability-block"]' });
            } else if (status === "no" && !(avail.note || "").trim()) {
                missingList.push({ id: "availability_note", label: "Availability note", section: "projectQuestions", selector: '[data-testid="availability-note-input"]' });
            }
        }

        if (fieldsConfig.budget_expectation === "required") {
            const budget = form.budget || {};
            const bstatus = (budget.status || "").trim();
            if (bstatus !== "accept" && bstatus !== "custom") {
                missingList.push({ id: "budget", label: "Budget (Accept / Custom)", section: "projectQuestions", selector: '[data-testid="budget-block"]' });
            } else if (bstatus === "custom" && !(budget.value || "").trim()) {
                missingList.push({ id: "budget_value", label: "Expected budget details", section: "projectQuestions", selector: '[data-testid="budget-value-input"]' });
            }
        }

        if (requirements.interested_in === "required") {
            if (!form.interested_in || form.interested_in.length === 0) {
                missingList.push({ id: "interested_in", label: "Casting Interests", section: "profile", selector: '[data-testid="interested-in-section"]' });
            }
        }

        // 2. Custom Questions
        const customReqs = requirements.custom_questions || {};
        const customAnswers = form.custom_answers || {};
        (project?.custom_questions || []).forEach(cq => {
            if (cq.id && customReqs[cq.id] === "required") {
                if (!String(customAnswers[cq.id] || "").trim()) {
                    missingList.push({
                        id: `cq_${cq.id}`,
                        label: `"${cq.question}" answers`,
                        section: "projectQuestions",
                        selector: `[data-testid="form-cq-${cq.id}"]`
                    });
                }
            }
        });

        // 3. Media Uploads
        const mediaList = submission?.media || [];
        if (requirements.intro_video === "required") {
            const hasIntro = mediaList.some(m => m.category === "intro_video");
            if (!hasIntro) {
                missingList.push({ id: "intro_video", label: "Introduction Video", section: "uploads", selector: '[data-testid="uploads-section"]' });
            }
        }

        const minTakes = parseInt(requirements.min_audition_takes || 0, 10);
        if (minTakes > 0) {
            const takesCount = mediaList.filter(m => ["take", "take_1", "take_2", "take_3"].includes(m.category)).length;
            if (takesCount < minTakes) {
                missingList.push({ id: "takes", label: `Audition Takes (minimum ${minTakes})`, section: "uploads", selector: '[data-testid="takes-section"]' });
            }
        }

        const portfolioReqs = requirements.portfolio || {};
        const portfolioCats = [
            { category: "image", label: "Portfolio (General)", selector: '[data-testid="portfolio-group-generic"]' },
            { category: "indian", label: "Indian Look", selector: '[data-testid="portfolio-group-indian"]' },
            { category: "western", label: "Western Look", selector: '[data-testid="portfolio-group-western"]' }
        ];
        portfolioCats.forEach(cat => {
            const minCount = parseInt(portfolioReqs[cat.category] || 0, 10);
            if (minCount > 0) {
                const count = mediaList.filter(m => m.category === cat.category).length;
                if (count < minCount) {
                    missingList.push({
                        id: `portfolio_${cat.category}`,
                        label: `${cat.label} (minimum ${minCount})`,
                        section: "uploads",
                        selector: cat.selector
                    });
                }
            }
        });

        // 4. Work Links
        const minLinks = parseInt(requirements.min_work_links || 0, 10);
        if (minLinks > 0) {
            const linksCount = (form.work_links || []).length;
            if (linksCount < minLinks) {
                missingList.push({ id: "work_links", label: `Work Links (minimum ${minLinks})`, section: "profile", selector: '[data-testid="form-work-links-field"]' });
            }
        }

        // 5. Skills & Special Abilities
        const skillsReqs = requirements.skills || {};
        const userSkills = form.skills || [];
        const SKILLS_CATEGORIES = {
            "language": ["English", "Hindi", "Spanish", "French", "Mandarin Chinese", "Japanese", "Russian", "German", "Arabic", "Marathi", "Gujarati", "Punjabi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Urdu", "Other"],
            "languages": ["English", "Hindi", "Spanish", "French", "Mandarin Chinese", "Japanese", "Russian", "German", "Arabic", "Marathi", "Gujarati", "Punjabi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Urdu", "Other"],
            "performance": ["Actor", "Voice Artist", "Dancer", "Singer", "Host", "Anchor", "Model", "Theatre Artist", "Improvisation", "Stand-up Comedy"],
            "sports": ["Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing", "Kickboxing", "Wrestling", "CrossFit", "Calisthenics", "Cricket", "Football", "Basketball", "Tennis", "Badminton"],
            "sports & fitness": ["Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing", "Kickboxing", "Wrestling", "CrossFit", "Calisthenics", "Cricket", "Football", "Basketball", "Tennis", "Badminton"],
            "action": ["Martial Arts", "Karate", "Taekwondo", "Judo", "Kung Fu", "Fight Choreography", "Horse Riding", "Rock Climbing", "Parkour", "Sword Fighting"],
            "action & stunts": ["Martial Arts", "Karate", "Taekwondo", "Judo", "Kung Fu", "Fight Choreography", "Horse Riding", "Rock Climbing", "Parkour", "Sword Fighting"],
            "vehicle": ["Drive Manual Car", "Drive Automatic Car", "Ride Motorcycle", "Ride Scooter", "Ride Bicycle", "Drive Truck", "Operate Boat", "Ride Jet Ski"],
            "vehicle skills": ["Drive Manual Car", "Drive Automatic Car", "Ride Motorcycle", "Ride Scooter", "Ride Bicycle", "Drive Truck", "Operate Boat", "Ride Jet Ski"],
            "special": ["Skateboarding", "Roller Skating", "Ice Skating", "Surfing", "Scuba Diving", "Fire Performance", "Juggling"],
            "special skills": ["Skateboarding", "Roller Skating", "Ice Skating", "Surfing", "Scuba Diving", "Fire Performance", "Juggling"],
            "dance": ["Hip Hop", "Contemporary", "Bollywood", "Bharatanatyam", "Kathak", "Salsa", "Ballet"],
            "music": ["Singer", "Piano", "Keyboard", "Guitar", "Violin", "Drums", "Flute", "Ukulele", "DJ", "Beatboxing", "Rapper", "Composer", "Music Producer"]
        };
        Object.keys(skillsReqs).forEach(cat => {
            if (skillsReqs[cat]) {
                const validSkills = SKILLS_CATEGORIES[cat.toLowerCase()] || [];
                const hasSkill = userSkills.some(s => validSkills.includes(s));
                if (!hasSkill) {
                    missingList.push({
                        id: `skills_${cat}`,
                        label: `At least one skill from category "${cat}"`,
                        section: "profile",
                        selector: '[data-testid="form-skills-field"]'
                    });
                }
            }
        });

        // 6. Conditional Rules
        const getMediaLabel = (m) => {
            if (m.label) return m.label;
            if (m.category === "intro_video") return "Introduction Video";
            if (m.category === "take_1") return "Take 1";
            if (m.category === "take_2") return "Take 2";
            if (m.category === "take_3") return "Take 3";
            return "";
        };
        const conditionalRules = requirements.conditional_rules || [];
        conditionalRules.forEach(rule => {
            const qid = rule.question_id;
            const trigger = rule.trigger_value;
            const videoLabel = rule.video_label;
            if (qid && trigger && videoLabel) {
                const ans = String(customAnswers[qid] || "").trim().toLowerCase();
                if (ans === String(trigger).trim().toLowerCase()) {
                    const hasCondVideo = mediaList.some(m =>
                        ["take", "intro_video", "take_1", "take_2", "take_3"].includes(m.category) &&
                        getMediaLabel(m).trim().toLowerCase() === videoLabel.trim().toLowerCase()
                    );
                    if (!hasCondVideo) {
                        const cqObj = (project?.custom_questions || []).find(q => q.id === qid);
                        const questionText = cqObj ? cqObj.question : "additional question";
                        missingList.push({
                            id: `conditional_${qid}_${videoLabel}`,
                            label: `"${videoLabel}" required (Because you answered "${trigger}" to "${questionText}")`,
                            section: "uploads",
                            selector: '[data-testid="uploads-section"]'
                        });
                    }
                }
            }
        });

        return missingList;
    }, [project, form, submission]);

    const missingRequirements = getMissingRequirements();
    const readyToSubmit = missingRequirements.length === 0;
    const missing = missingRequirements.map(req => req.label);

    const MAX_TAKES = 5;
    const canAddTake = takes.length < MAX_TAKES;
    const isSubmitted =
        submission && submission.status && submission.status !== "draft";

    // ---------------------------------------------------------------
    if (loading) {
        return (
            <div className="min-h-dvh flex items-center justify-center bg-gradient-to-b from-slate-50 to-white">
                <Loader2 className="w-6 h-6 animate-spin text-[#333333]" />
            </div>
        );
    }
    if (!project) {
        return (
            <div className="min-h-dvh flex items-center justify-center bg-gradient-to-b from-slate-50 to-white text-[#333333] p-6 text-center">
                <p>Project not found.</p>
            </div>
        );
    }

    // ---------------------------------------------------------------
    // SUBMITTED / UPDATED / RETEST state — permanent Submission Hub dashboard
    if (isSubmitted && !editMode) {
        const getStatusLabel = () => {
            const status = submission?.status;
            if (status === "updated") return "Resubmitted";
            if (status === "retest") return "Retest Requested";
            if (status === "approved") return "Approved";
            if (status === "shortlisted") return "Shortlisted";
            if (status === "rejected") return "Closed";
            return "Submitted";
        };

        const getStatusStyles = () => {
            const status = submission?.status;
            if (status === "retest") return "bg-rose-50 border border-rose-200 text-rose-700";
            if (status === "approved" || status === "shortlisted") return "bg-emerald-50 border border-emerald-200 text-emerald-700";
            if (status === "rejected") return "bg-slate-100 border border-slate-200 text-slate-600";
            return "bg-slate-50 border border-slate-200 text-[#333333]";
        };

        const statusLabel = getStatusLabel();
        const statusClass = getStatusStyles();
        
        const lastUpdated = formatMediaTimestamp({
            updated_at: submission?.updated_at,
            created_at: submission?.created_at
        });

        const feedback = submission?.client_feedback || [];
        
        return (
            <div className="min-h-dvh bg-gradient-to-b from-slate-50 via-white to-slate-50/30 text-[#111111] relative overflow-hidden">
                <div className="absolute inset-0 pointer-events-none opacity-20 blur-3xl bg-[#0c2340]/20" />
                <div className="absolute top-5 right-5 z-10">
                    <ThemeToggle />
                </div>
                <div className="max-w-xl mx-auto px-4 sm:px-6 py-16 md:py-24 tg-fade-up">
                    
                    {submission?.status === "retest" && (
                        <div className="mb-8 bg-rose-50/60 border border-rose-200 rounded-3xl p-6 text-left animate-in fade-in slide-in-from-top-4 duration-250">
                            <div className="flex items-start gap-3">
                                <span className="shrink-0 w-6 h-6 rounded-full bg-rose-500 text-white flex items-center justify-center font-bold text-xs shadow-sm mt-0.5">!</span>
                                <div>
                                    <h4 className="font-semibold text-sm text-rose-950">Action Required: Retest Request</h4>
                                    <p className="text-xs text-rose-800 leading-relaxed mt-1">
                                        The casting team has requested a retest or additional takes for your audition. Please check the feedback below, record your updates, and click the "Update Submission" button to submit your new takes.
                                    </p>
                                </div>
                            </div>
                        </div>
                    )}

                    <div className="bg-white/80 backdrop-blur-sm rounded-3xl p-10 border border-[#eaeaea]/60 shadow-[0_20px_40px_-12px_rgba(0,0,0,0.05)] text-center">
                        <div className="relative w-20 h-20 mx-auto mb-8">
                            <div className="absolute inset-0 rounded-full bg-emerald-100/60 blur-xl animate-pulse" />
                            <div className="relative w-full h-full rounded-full bg-emerald-50 border border-emerald-100 flex items-center justify-center shadow-sm">
                                <Check className="w-8 h-8 text-emerald-600" />
                            </div>
                        </div>
                        
                        <div className="flex flex-col items-center gap-2 mb-6">
                            <span className={`px-4 py-1.5 rounded-full text-[10px] tracking-[0.1em] uppercase font-mono font-semibold ${statusClass}`}>
                                {statusLabel}
                            </span>
                            {lastUpdated && (
                                <p className="text-[10px] font-mono text-[#333333] tracking-wide">
                                    Last Updated: {lastUpdated}
                                </p>
                            )}
                        </div>

                        <h1 className="font-display text-4xl md:text-5xl tracking-tight text-[#111111] mb-6 leading-[1.05]">
                            Thank you,{" "}
                            <span className="text-[#111111]">{form.first_name || submission.talent_name?.split(" ")[0]}</span>.
                        </h1>
                        <p className="text-[13px] leading-relaxed text-[#333333] mb-10 max-w-md mx-auto">
                            Your audition for{" "}
                            <span className="font-medium text-[#111111]">
                                {project.brand_name}
                            </span>{" "}
                            has been received. The Talentgram team will review and
                            reach out if you're shortlisted.
                        </p>
                        
                        <div className="pt-4 border-t border-[#eaeaea]/50">
                            <button
                                type="button"
                                onClick={() => setEditMode(true)}
                                data-testid="update-submission-hub-btn"
                                className="w-full bg-slate-900 text-white py-3.5 px-6 rounded-full text-xs font-semibold hover:bg-slate-800 hover:-translate-y-[1px] active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 shadow-sm"
                            >
                                Update Submission
                            </button>
                            {/* Same-origin relative link only. The portal session token
                                lives in this origin's localStorage (written at OTP verify),
                                so the dashboard must be opened on this same subdomain. Do
                                NOT change to an absolute/cross-subdomain URL — the token
                                would not be present and the user would be bounced out. */}
                            <a
                                href="/portal/home"
                                data-testid="view-dashboard-btn"
                                className="mt-3 w-full border border-slate-300 text-slate-700 py-3.5 px-6 rounded-full text-xs font-semibold hover:bg-slate-50 hover:-translate-y-[1px] active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5"
                            >
                                View My Talent Dashboard
                            </a>
                        </div>
                    </div>

                    {/* Client Feedback inbox — only approved+shared rows ever appear
                        here. The relay is mediated by the team, so notes the talent
                        sees have been reviewed. Order is approval-time ascending. */}
                    <section
                        className="mt-16"
                        data-testid="talent-feedback-section"
                    >
                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#333333] mb-4">Client Feedback & Reviews</p>
                        {feedback.length === 0 ? (
                            <div
                                className="bg-white/40 rounded-2xl p-6 text-[13px] leading-relaxed text-[#333333] border border-[#eaeaea]/60"
                                data-testid="talent-feedback-empty"
                            >
                                No reviews yet — the team will share notes here
                                once a client responds.
                            </div>
                        ) : (
                            <div className="space-y-4">
                                {feedback.map((f) => (
                                    <FeedbackRow key={f.id} fb={f} />
                                ))}
                            </div>
                        )}
                    </section>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-dvh bg-gradient-to-b from-slate-50 via-white to-slate-50/30 text-[#111111] relative overflow-hidden" data-testid="submission-page">
            {/* Ambient luxury background blobs */}
            <div className="absolute inset-0 pointer-events-none opacity-30 blur-3xl">
                <div className="absolute top-0 -left-40 w-80 h-80 rounded-full bg-[#0c2340]/10 mix-blend-multiply animate-blob" />
                <div className="absolute bottom-0 -right-40 w-80 h-80 rounded-full bg-slate-200/40 mix-blend-multiply animate-blob animation-delay-2000" />
            </div>

            <header className="relative w-full pt-10 pb-8 px-5 border-b border-[#eaeaea]/60 bg-white/40">
                <div className="absolute top-5 right-5 z-40">
                    <ThemeToggle size="sm" />
                </div>
                <div className="max-w-2xl mx-auto flex flex-col items-center text-center">
                    {/* Centered Logo */}
                    <div className="mb-4">
                        <Logo size={76} className="mx-auto" />
                    </div>

                    {/* Clickable Instagram icon */}
                    <div className="mb-4">
                        <a
                            href="https://www.instagram.com/talentgram.agency/"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center justify-center p-2 rounded-full text-[#111111] hover:bg-slate-100 transition-all duration-200 cursor-pointer group"
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

                    {/* Trust and Credibility Copy */}
                    <div className="max-w-md mx-auto">
                        <p className="text-[13px] font-semibold text-[#111111] tracking-tight leading-relaxed">
                            Complete your profile and upload your audition materials.
                        </p>
                        <p className="text-[11px] text-[#333333] font-medium leading-relaxed mt-1">
                            Your submission will be reviewed by the Talentgram team.
                        </p>
                    </div>
                </div>
            </header>

            <div data-testid="submission-content" className="max-w-2xl mx-auto px-4 sm:px-6 md:px-8 py-6 md:py-10">
                {/* SECTION 1 — Project Info */}
                <section className="mb-8 bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/60 shadow-[0_4px_20px_rgba(15,23,42,0.04)]" data-testid="project-info-section" data-step="1">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-4">Audition Brief</p>
                    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-8 border-b border-slate-100 pb-4">
                        <div className="flex flex-col gap-1">
                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#333333]">PROJECT</p>
                            <h1 className="font-display text-2xl sm:text-3xl md:text-4xl tracking-tight text-[#111111] leading-[1.05]">
                                Talentgram × {project.brand_name}
                            </h1>
                        </div>
                        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-50/60 border border-emerald-100/50 text-emerald-700 text-[11px] font-mono shadow-[0_1px_2px_rgba(0,0,0,0.02)] self-start sm:self-auto">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                            <span>Draft Auto-Saved</span>
                        </div>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-5 border-t border-[#eaeaea]/50 pt-6">
                        <Info label="Character" value={project.character} />
                        <Info label="Shoot Dates" value={project.shoot_dates} />
                        {project.budget_per_day && (
                            <Info label="Budget Per Day" value={project.budget_per_day} />
                        )}
                        {/* Phase 1 (v37): expose Director + Production House so
                            talents see who's behind the project before they
                            invest time. Both fields already exist in the
                            project schema — this is rendering-only. `<Info>`
                            already auto-hides when value is empty, so no
                            extra guard needed. */}
                        <Info label="Director" value={project.director} />
                        <Info label="Production House" value={project.production_house} />
                        <Info label="Commission" value={project.commission_percent} />
                        <Info label="Medium / Usage" value={project.medium_usage} wide />
                    </div>
                    {project.additional_details && (
                        <div className="mt-6 pt-4 border-t border-slate-100">
                            <p className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono mb-2">
                                Additional Details
                            </p>
                            <p className="text-[13px] leading-relaxed text-[#222222] whitespace-pre-line">
                                {project.additional_details}
                            </p>
                        </div>
                    )}
                    {((Array.isArray(project.materials) && project.materials.length > 0) ||
                        (Array.isArray(project.video_links) && project.video_links.length > 0)) && (
                        <button
                            onClick={() => setShowMaterial(true)}
                            data-testid="view-audition-material-btn"
                            className="inline-flex items-center gap-2 px-5 py-2.5 mt-6 border border-[#0c2340] hover:border-[#0c2340] hover:bg-[#0c2340]/[0.08] active:scale-[0.98] rounded-full text-[13px] text-[#0c2340] font-semibold transition-all hover:shadow-md hover:-translate-y-[1px] bg-[#0c2340]/[0.04]"
                        >
                            <FolderOpen className="w-4 h-4 text-[#0c2340]" /> View Audition Material
                        </button>
                    )}
                </section>

                {/* SUBMISSION PROGRESS CHECKLIST */}
                {emailGateUnlocked && (
                    <section className="mb-10 bg-white rounded-3xl p-6 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.03)]" data-testid="submission-progress-card">
                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-black font-semibold mb-3">Submission Progress</p>
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                            {[
                                { 
                                    label: "Your Profile", 
                                    completed: !!(form.first_name?.trim() && form.last_name?.trim() && form.height && form.location && form.location.length > 0) 
                                },
                                { 
                                    label: "Project Questions", 
                                    completed: !!(form.availability?.status && (form.availability.status !== "no" || form.availability.note?.trim()) && form.budget?.status && (form.budget.status !== "custom" || form.budget.value?.trim())) 
                                },
                                { 
                                    label: "Uploads (Optional)", 
                                    completed: true 
                                }
                            ].map((item, idx) => (
                                <div 
                                    key={idx} 
                                    className={`flex items-center gap-2.5 px-3 py-2.5 rounded-2xl border transition-all duration-300 ${
                                        item.completed 
                                            ? "bg-emerald-50/40 border-emerald-100/50 text-emerald-900 font-semibold" 
                                            : "bg-slate-50/50 border-slate-100 text-black font-semibold"
                                    }`}
                                >
                                    <span className="shrink-0">
                                        {item.completed ? (
                                            <div className="w-5 h-5 rounded-full bg-emerald-500 text-white flex items-center justify-center shadow-sm">
                                                <Check className="w-3 h-3 stroke-[3]" />
                                            </div>
                                        ) : (
                                            <div className="w-5 h-5 rounded-full border-2 border-[#eaeaea] bg-white" />
                                        )}
                                    </span>
                                    <span className="text-[12px] font-medium tracking-tight truncate">
                                        {item.label}
                                    </span>
                                </div>
                            ))}
                        </div>
                    </section>
                )}

                {/* SECTION 2 — TALENT DETAILS FORM */}
                <section
                    className="pt-4 mb-10 sm:mb-16"
                    data-testid="talent-details-section"
                    data-step="1-2"
                >
                    <div className="bg-white rounded-3xl p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)]">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70 mb-4" data-step="1">Talent Details</p>
                    <h2 className="font-display text-2xl md:text-3xl tracking-tight text-[#111111] mb-3 leading-[1.05]" data-step="1">
                        Your profile.
                    </h2>
                    <p className="text-[13px] leading-relaxed text-[#333333] mb-10" data-step="1">
                        All fields are required unless marked optional.
                    </p>
                    <form onSubmit={startSubmission} className="space-y-8">
                        {/* Phase 1 — email-first identity. The email field
                            anchors the form so we can prefill known talents
                            BEFORE they retype everything. */}
                        <div data-step="1">
                            {!emailGateUnlocked ? (
                                otpSent ? (
                                    /* Step A.5: OTP Verification Input */
                                    <div className="flex flex-col gap-4 animate-in fade-in duration-200 text-left">
                                        <div className="flex flex-col gap-1.5">
                                            <label className="text-xs font-semibold text-[#111111] uppercase tracking-wider">
                                                Enter Verification Code
                                            </label>
                                            <p className="text-xs text-[#333333] leading-normal">
                                                We've sent a verification code to <span className="font-semibold text-slate-900">{gatewayEmail}</span>
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
                                                className="flex-1 px-4 py-2.5 bg-white border border-[#eaeaea] rounded-xl text-[#111111] placeholder:text-[#333333] focus:border-slate-500 focus:outline-none transition duration-150 h-[44px]"
                                                disabled={otpLoading}
                                            />
                                            <div className="flex gap-2">
                                                <button
                                                    type="button"
                                                    onClick={handleVerifyOtp}
                                                    disabled={otpLoading}
                                                    className="bg-slate-900 text-white px-5 py-2.5 rounded-xl text-xs font-medium hover:bg-slate-800 active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 min-w-[100px] h-[44px] cursor-pointer"
                                                >
                                                    {otpLoading ? "Verifying..." : "Verify"}
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={handleResendOtp}
                                                    disabled={otpResending || otpLoading}
                                                    className="bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#111111] text-xs font-medium px-4 py-2.5 rounded-xl transition duration-150 h-[44px] cursor-pointer"
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
                                            className="text-left text-xs text-slate-500 hover:text-slate-900 transition underline cursor-pointer"
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
                                            className="w-full bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#111111] py-3 px-4 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-2.5 transition duration-150 shadow-sm active:scale-[0.98] cursor-pointer"
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
                                            <p className="text-xs text-[#333333] leading-normal">
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
                                                className="flex-1 px-4 py-2.5 bg-white border border-[#eaeaea] rounded-xl text-[#111111] placeholder:text-[#333333] focus:border-slate-500 focus:outline-none transition duration-150 h-[44px]"
                                                disabled={gatewayLoading}
                                            />
                                            <button
                                                type="button"
                                                onClick={handleInlineLookup}
                                                disabled={gatewayLoading}
                                                className="bg-slate-900 text-white px-5 py-2.5 rounded-xl text-xs font-medium hover:bg-slate-800 active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 min-w-[120px] h-[44px]"
                                            >
                                                {gatewayLoading ? "Verifying..." : "Continue"}
                                                <ArrowRight className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                        <p className="text-[10px] text-[#333333] font-mono mt-1">
                                            We use your email to recognise you and load any previously submitted details.
                                        </p>
                                    </div>
                                ) : (
                                    /* Step B: Inline Cinematic Recognition */
                                    <div className="flex flex-col gap-5 border border-slate-100 rounded-2xl p-5 bg-slate-50/50 animate-in fade-in zoom-in-95 duration-200 text-left">
                                        <div className="flex items-center gap-4">
                                            {gatewayRecognition.image_url ? (
                                                <img
                                                    src={gatewayRecognition.image_url}
                                                    alt={gatewayRecognition.name}
                                                    className="w-20 h-20 rounded-full object-cover border border-[#eaeaea] shadow-sm shrink-0"
                                                />
                                            ) : (
                                                <div className="w-20 h-20 rounded-full bg-slate-200 flex items-center justify-center border border-[#d4d4d4] shrink-0">
                                                    <User className="w-8 h-8 text-[#333333]" />
                                                </div>
                                            )}
                                            <div className="text-left">
                                                <h4 className="font-semibold text-sm text-[#111111]">Is this you?</h4>
                                                <p className="text-xs text-[#333333] font-medium mt-1">
                                                    {gatewayRecognition.name} {(() => {
                                                        const locs = Array.isArray(gatewayRecognition.location) 
                                                            ? gatewayRecognition.location 
                                                            : (gatewayRecognition.location ? [{ city: gatewayRecognition.location }] : []);
                                                        return locs.length > 0 ? `· ${locs.map(l => l?.city || l).join(", ")}` : "";
                                                    })()}
                                                </p>
                                            </div>
                                        </div>

                                        <div className="flex flex-col items-stretch gap-2 pt-2 border-t border-[#eaeaea]/40">
                                            <button
                                                type="button"
                                                onClick={handleInlineContinue}
                                                className="w-full bg-slate-900 text-white px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-slate-850 active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 h-[40px]"
                                            >
                                                Continue to Audition
                                                <ChevronRight className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                    </div>
                                )
                            ) : (
                                /* Locked Email State (if unlocked) */
                                <>
                                    <PremiumFormField
                                        label="Email *"
                                        type="email"
                                        value={form.email}
                                        onChange={(v) => {
                                            setForm({ ...form, email: v });
                                            if (!saved && v.trim().toLowerCase() !== prefillEmail) {
                                                setPrefillTried(false);
                                                setPrefillSuggestion(null);
                                                setEmailGateUnlocked(false);
                                            }
                                        }}
                                        onBlur={() => {
                                            saveForm();
                                            tryPrefill();
                                        }}
                                        testid="form-email"
                                        required
                                        disabled={!!saved}
                                        wide
                                    />
                                    <p className="text-[11px] text-[#333333] mt-3 font-mono">
                                        We use your email to recognise you and load any previously submitted details.
                                    </p>
                                </>
                            )}
                        </div>



                        {emailGateUnlocked && (
                        <>
                        {/* Section 1: Your Profile */}
                        <div className="bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)] mb-8">
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                                <div>
                                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-1">Talent Profile</p>
                                    <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05]">Your Profile</h2>
                                    <p className="text-[13px] text-[#222222] mt-1.5 leading-relaxed">Please confirm your personal details exactly as they should appear for casting.</p>
                                </div>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            profile: !prev.profile,
                                        }))
                                    }
                                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                    title={collapsedSections.profile ? "Expand Profile" : "Collapse Profile"}
                                >
                                    <ChevronDown
                                        className={`h-4 w-4 transform transition-transform duration-200 ${
                                            collapsedSections.profile ? "-rotate-90" : ""
                                        }`}
                                    />
                                </button>
                            </div>
                            
                            {!collapsedSections.profile && (
                                <div className="space-y-8 animate-fadeIn">
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8" data-step="1">
                                        <PremiumFormField
                                            label="First Name *"
                                            value={form.first_name}
                                            onChange={(v) => {
                                                setForm({ ...form, first_name: v });
                                                if (validationErrors.first_name) setValidationErrors((e) => ({ ...e, first_name: undefined }));
                                            }}
                                            onBlur={saveForm}
                                            testid="form-first-name"
                                            required
                                            error={validationErrors.first_name}
                                            inputRef={(el) => { fieldRefs.current.first_name = el; }}
                                        />
                                        <PremiumFormField
                                            label="Last Name *"
                                            value={form.last_name}
                                            onChange={(v) => {
                                                setForm({ ...form, last_name: v });
                                                if (validationErrors.last_name) setValidationErrors((e) => ({ ...e, last_name: undefined }));
                                            }}
                                            onBlur={saveForm}
                                            testid="form-last-name"
                                            required
                                            error={validationErrors.last_name}
                                            inputRef={(el) => { fieldRefs.current.last_name = el; }}
                                        />
                                        <PremiumFormField
                                            label="Phone"
                                            type="tel"
                                            value={form.phone}
                                            onChange={(v) =>
                                                setForm({ ...form, phone: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-phone"
                                        />
                                        <PremiumFormField
                                            label="Date of Birth"
                                            type="date"
                                            value={form.dob}
                                            max={new Date().toISOString().split("T")[0]}
                                            onChange={(v) =>
                                                setForm({ ...form, dob: v, age: "" })
                                            }
                                            onBlur={saveForm}
                                            testid="form-dob"
                                            className="[color-scheme:light]"
                                            autoComplete="bday"
                                            hint="We automatically calculate age from your date of birth."
                                        />
                                        
                                        {/* Project-specific age override checkbox and input */}
                                        <div className="mt-4 p-5 rounded-2xl bg-slate-50/50 border border-[#eaeaea]/50 focus-within:border-[#0c2340]/40 focus-within:ring-4 focus-within:ring-[#0c2340]/5 transition-all duration-300 col-span-1 md:col-span-2">
                                            <label className="flex items-center gap-3 cursor-pointer min-h-[44px]">
                                                <input
                                                    type="checkbox"
                                                    checked={form.overrideAge || false}
                                                    onChange={(e) => {
                                                        const active = e.target.checked;
                                                        setForm({
                                                            ...form,
                                                            overrideAge: active,
                                                            submitted_age_override: active ? (form.submitted_age_override || String(computedAge || "")) : ""
                                                        });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="form-override-age-checkbox"
                                                    className="w-5 h-5 rounded border-[#d4d4d4] text-[#0c2340] focus:ring-[#0c2340] focus:ring-2 cursor-pointer transition duration-150 ease-in-out"
                                                />
                                                <span className="text-sm font-medium text-[#111111] select-none">
                                                    Use different age for this project?
                                                </span>
                                            </label>
                                            
                                            {form.overrideAge && (
                                                <div className="mt-4 animate-fadeIn transition-all duration-300">
                                                    <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                        Project-Specific Age Override *
                                                    </span>
                                                    <input
                                                        type="number"
                                                        inputMode="numeric"
                                                        pattern="[0-9]*"
                                                        value={form.submitted_age_override || ""}
                                                        onChange={(e) =>
                                                            setForm({
                                                                ...form,
                                                                submitted_age_override: e.target.value,
                                                            })
                                                        }
                                                        onBlur={saveForm}
                                                        min={10}
                                                        max={80}
                                                        placeholder="e.g. 25"
                                                        data-testid="form-override-age-input"
                                                        className="mt-2 w-full bg-white rounded-xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                    />
                                                    <p className="text-[10px] text-[#333333] font-mono mt-1.5">
                                                        Only use this if you wish to be presented as a different age range for this project. This override is isolated to this submission only.
                                                    </p>
                                                </div>
                                            )}
                                        </div>

                                        <div data-testid="form-age-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Age {form.dob ? "(auto calculated)" : "*"}
                                            </span>
                                            <input
                                                type="number"
                                                value={
                                                    form.dob
                                                        ? (calcAge(form.dob) ?? "")
                                                        : form.age
                                                }
                                                disabled={true}
                                                min={10}
                                                max={80}
                                                data-testid="form-age-input"
                                                className="mt-2 w-full bg-slate-100 rounded-2xl border border-[#eaeaea] outline-none py-3 px-4 text-[15px] text-[#333333] shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                            />
                                        </div>

                                        <div data-testid="form-height-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Height *
                                            </span>
                                            <div className="mt-2">
                                                <Select
                                                    value={form.height || ""}
                                                    onValueChange={(v) => {
                                                        setForm({ ...form, height: v });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                >
                                                    <SelectTrigger
                                                        data-testid="form-height-trigger"
                                                        className="bg-white/60 border border-[#eaeaea] rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-[#111111] transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select height" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-[#eaeaea] rounded-2xl">
                                                        {HEIGHT_OPTIONS.map((h) => (
                                                            <SelectItem
                                                                key={h}
                                                                value={h}
                                                            >
                                                                {h}
                                                            </SelectItem>
                                                        ))}
                                                    </SelectContent>
                                                </Select>
                                            </div>
                                            <span className="block text-[10px] text-[#333333] mt-1 font-mono">
                                                Enter your actual height without footwear.
                                            </span>
                                        </div>
                                        <div className="md:col-span-2">
                                            <span className="text-[11px] text-[#111111] tracking-[0.08em] font-semibold uppercase font-mono block mb-2">
                                                Current Location(s) *
                                            </span>
                                            <LocationSelector
                                                value={form.location || []}
                                                onChange={(arr) => {
                                                    setForm({ ...form, location: arr });
                                                    if (validationErrors.location) setValidationErrors((e) => ({ ...e, location: undefined }));
                                                    setTimeout(saveForm, 0);
                                                }}
                                                testid="form-location"
                                                error={validationErrors.location}
                                            />
                                        </div>
                                    </div>

                                    {/* Phase 2 — unified identity fields */}
                                    <div
                                        className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8"
                                        data-step="1"
                                        data-testid="unified-identity-block"
                                    >
                                        <div data-testid="form-gender-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Gender
                                            </span>
                                            <div className="mt-2 grid grid-cols-2 gap-2">
                                                {GENDER_OPTIONS.map((g) => {
                                                    const active = form.gender === g.key;
                                                    return (
                                                        <button
                                                            key={g.key}
                                                            type="button"
                                                            onClick={() => {
                                                                setForm({
                                                                    ...form,
                                                                    gender: active
                                                                        ? ""
                                                                        : g.key,
                                                                });
                                                                setTimeout(saveForm, 0);
                                                            }}
                                                            data-testid={`form-gender-${g.key}`}
                                                            className={`px-3 py-2.5 text-[12px] rounded-full border transition-all duration-200 min-h-[44px] active:scale-[0.97] ${
                                                                active
                                                                    ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                                    : "bg-white/60 border-[#eaeaea] hover:border-[#d4d4d4] text-[#222222]"
                                                            }`}
                                                        >
                                                            {g.label}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                        <div data-testid="form-ethnicity-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Ethnicity
                                            </span>
                                            <div className="mt-2">
                                                <Select
                                                    value={form.ethnicity || ""}
                                                    onValueChange={(v) => {
                                                        setForm({ ...form, ethnicity: v });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                >
                                                    <SelectTrigger
                                                        data-testid="form-ethnicity-trigger"
                                                        className="bg-white/60 border border-[#eaeaea] rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-[#111111] transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select ethnicity" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-[#eaeaea] rounded-2xl">
                                                        {ETHNICITY_OPTIONS.map((e) => (
                                                            <SelectItem
                                                                key={e.key}
                                                                value={e.key}
                                                                data-testid={`form-ethnicity-option-${e.key}`}
                                                            >
                                                                {e.label}
                                                            </SelectItem>
                                                        ))}
                                                    </SelectContent>
                                                </Select>
                                            </div>
                                        </div>
                                        <PremiumFormField
                                            label="Instagram Handle"
                                            value={form.instagram_handle}
                                            onChange={(v) => {
                                                let clean = v.trim();
                                                if (clean.includes("instagram.com/")) {
                                                    const segments = clean.split("instagram.com/");
                                                    if (segments[1]) {
                                                        clean = segments[1].split(/[?#/]/)[0];
                                                    }
                                                }
                                                if (clean.startsWith("@")) {
                                                    clean = clean.substring(1);
                                                }
                                                clean = clean.replace(/\s+/g, "");
                                                setForm({ ...form, instagram_handle: clean });
                                            }}
                                            onBlur={() => {
                                                if (form.instagram_handle) {
                                                    setForm((prev) => ({
                                                        ...prev,
                                                        instagram_handle: normalizeInstagramHandle(form.instagram_handle)
                                                    }));
                                                }
                                                saveForm();
                                            }}
                                            testid="form-instagram-handle"
                                            placeholder="@yourhandle"
                                            hint="Optional, but helps casting teams review additional work."
                                        />
                                        <div data-testid="form-instagram-followers-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Instagram Followers
                                            </span>
                                            <div className="mt-2">
                                                <Select
                                                    value={form.instagram_followers || ""}
                                                    onValueChange={(v) => {
                                                        setForm({
                                                            ...form,
                                                            instagram_followers: v,
                                                        });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                >
                                                    <SelectTrigger
                                                        data-testid="form-instagram-followers-trigger"
                                                        className="bg-white/60 border border-[#eaeaea] rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-[#111111] transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select range" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-[#eaeaea] rounded-2xl">
                                                        {FOLLOWER_TIERS.map((tier) => (
                                                            <SelectGroup key={tier.label}>
                                                                <SelectLabel className="text-[10px] tracking-wide uppercase text-[#333333] font-mono">
                                                                    {tier.label}
                                                                </SelectLabel>
                                                                {tier.items.map((it) => (
                                                                    <SelectItem
                                                                        key={it}
                                                                        value={it}
                                                                    >
                                                                        {it}
                                                                    </SelectItem>
                                                                ))}
                                                                <SelectSeparator />
                                                            </SelectGroup>
                                                        ))}
                                                    </SelectContent>
                                                </Select>
                                            </div>
                                        </div>
                                        <div className="md:col-span-2">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono block mb-2">
                                                Skills & Special Abilities
                                            </span>
                                            <SkillsSelector
                                                selectedSkills={form.skills || []}
                                                onChange={(arr) => {
                                                    setForm((prev) => ({ ...prev, skills: arr }));
                                                    setTimeout(saveForm, 0);
                                                }}
                                            />
                                        </div>
                                        <label className="block md:col-span-2" data-testid="form-bio-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Bio (optional)
                                            </span>
                                            <textarea
                                                value={form.bio}
                                                onChange={(e) =>
                                                    setForm({
                                                        ...form,
                                                        bio: e.target.value,
                                                    })
                                                }
                                                onBlur={saveForm}
                                                rows={3}
                                                maxLength={600}
                                                data-testid="form-bio"
                                                className="mt-2 w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] resize-none transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                placeholder="A short note about you (max 600 chars)"
                                            />
                                        </label>
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Section 2: Project Questions */}
                        <div data-step="2" className="bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)] mb-8">
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                                <div>
                                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-1">Project Questions</p>
                                    <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05]">Project Questions</h2>
                                    <p className="text-[13px] text-[#222222] mt-1.5 leading-relaxed">Please answer these project-specific questions and confirm your availability.</p>
                                </div>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            projectQuestions: !prev.projectQuestions,
                                        }))
                                    }
                                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                    title={collapsedSections.projectQuestions ? "Expand Project Questions" : "Collapse Project Questions"}
                                >
                                    <ChevronDown
                                        className={`h-4 w-4 transform transition-transform duration-200 ${
                                            collapsedSections.projectQuestions ? "-rotate-90" : ""
                                        }`}
                                    />
                                </button>
                            </div>

                            {!collapsedSections.projectQuestions && (
                                <div className="space-y-8 animate-fadeIn">
                                    {/* AVAILABILITY — decision block */}
                                    <div
                                        data-testid="availability-block"
                                        data-step="2"
                                        className="mb-6"
                                    >
                                        <div className="bg-white/70 border border-[#eaeaea]/80 rounded-2xl p-5 mb-4 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                                            <p className="text-[12px] tracking-[0.1em] uppercase font-mono font-semibold text-[#333333] mb-2">
                                                Availability
                                            </p>
                                            {project.shoot_dates ? (
                                                <div className="space-y-2 mt-1">
                                                    {project.shoot_dates.split("\n").map((line, idx) => {
                                                        const trimmed = line.trim();
                                                        if (!trimmed) return null;
                                                        return (
                                                            <div key={idx} className="flex items-start gap-2.5 text-[15px] font-medium text-[#111111]">
                                                                <span className="w-1.5 h-1.5 rounded-full bg-[#0c2340] shrink-0 mt-2.5" />
                                                                <span>{trimmed}</span>
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            ) : (
                                                <p className="text-[15px] font-medium text-[#333333]">Dates to be confirmed</p>
                                            )}
                                        </div>
                                        <div className="grid grid-cols-2 gap-3 mb-4">
                                            {AVAILABILITY_OPTIONS.map((opt) => {
                                                const active =
                                                    form.availability.status === opt.key;
                                                return (
                                                    <button
                                                        key={opt.key}
                                                        type="button"
                                                        onClick={() => {
                                                            setForm({
                                                                ...form,
                                                                availability: {
                                                                    ...form.availability,
                                                                    status: opt.key,
                                                                },
                                                            });
                                                            setTimeout(saveForm, 0);
                                                        }}
                                                        data-testid={`avail-${opt.key}-btn`}
                                                        className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                            active
                                                                ? "bg-slate-950 text-white border-slate-950 shadow-sm"
                                                                : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                        }`}
                                                    >
                                                        {opt.key === "yes" ? "Available" : "Not Available"}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                        {form.availability.status === "no" && (
                                            <textarea
                                                value={form.availability.note}
                                                onChange={(e) =>
                                                    setForm({
                                                        ...form,
                                                        availability: {
                                                            ...form.availability,
                                                            note: e.target.value,
                                                        },
                                                    })
                                                }
                                                onBlur={saveForm}
                                                rows={3}
                                                placeholder="Please specify reason / alternate availability"
                                                data-testid="availability-note-input"
                                                className="w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[13px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                            />
                                        )}
                                    </div>

                                    {/* COMMISSION — card */}
                                    {project.commission_percent && (
                                        <div
                                            data-testid="commission-block"
                                            className="mb-6"
                                        >
                                            <div className="bg-white/70 border border-[#eaeaea]/80 rounded-2xl p-5 shadow-[0_1px_2px_rgba(0,0,0,0.02)]" data-testid="commission-card">
                                                <p className="text-[12px] tracking-[0.1em] uppercase font-mono font-semibold text-[#333333] mb-1.5">
                                                    Commission
                                                </p>
                                                <p className="text-[18px] font-semibold text-[#111111] leading-snug">
                                                    {project.commission_percent}
                                                </p>
                                            </div>
                                        </div>
                                    )}

                                    {/* BUDGET — decision block */}
                                    {(project.budget_per_day || (project.talent_budget || []).length > 0) && (
                                        <div
                                            data-testid="budget-block"
                                            data-step="2"
                                            className="mb-6"
                                        >
                                            <div className="bg-white/70 border border-[#eaeaea]/80 rounded-2xl p-5 mb-4 shadow-[0_1px_2px_rgba(0,0,0,0.02)]" data-testid="project-budget-card">
                                                <p className="text-[12px] tracking-[0.1em] uppercase font-mono font-semibold text-[#333333] mb-2">
                                                    Project Budget
                                                </p>
                                                {project.budget_per_day && (
                                                    <p className="text-[18px] font-semibold text-[#111111] leading-snug">
                                                        {project.budget_per_day}
                                                    </p>
                                                )}
                                                {Array.isArray(project.talent_budget) && project.talent_budget.length > 0 && (
                                                    <div className={`space-y-3 ${project.budget_per_day ? "border-t border-slate-100 pt-4 mt-4" : ""}`}>
                                                        {project.talent_budget.map((row, i) => (
                                                            <div
                                                                key={`${row.label || ""}-${i}`}
                                                                className="flex flex-col sm:flex-row sm:items-start justify-between gap-1 sm:gap-4 text-[15px] leading-relaxed text-[#111111] font-medium"
                                                                data-testid={`talent-budget-line-${i}`}
                                                            >
                                                                <span className="text-[#333333] whitespace-pre-wrap">{row.label || "—"}</span>
                                                                <span className="text-slate-950 font-semibold shrink-0">{row.value || "—"}</span>
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>

                                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setForm({
                                                            ...form,
                                                            budget: {
                                                                status: "accept",
                                                                value: "",
                                                            },
                                                        });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="budget-accept-btn"
                                                    className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                        form.budget.status === "accept"
                                                            ? "bg-[#0c2340] text-white border-[#0c2340] shadow-sm"
                                                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                    }`}
                                                >
                                                    Accept Budget
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setForm({
                                                            ...form,
                                                            budget: {
                                                                ...form.budget,
                                                                status: "custom",
                                                            },
                                                        });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="budget-custom-btn"
                                                    className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                        form.budget.status === "custom"
                                                            ? "bg-[#0c2340] text-white border-[#0c2340] shadow-sm"
                                                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                    }`}
                                                >
                                                    Propose Own
                                                </button>
                                            </div>
                                            {form.budget.status === "custom" && (
                                                <input
                                                    type="text"
                                                    value={form.budget.value}
                                                    onChange={(e) =>
                                                        setForm({
                                                            ...form,
                                                            budget: {
                                                                ...form.budget,
                                                                value: e.target.value,
                                                            },
                                                        })
                                                    }
                                                    onBlur={saveForm}
                                                    placeholder="Enter your expected budget per day"
                                                    data-testid="budget-value-input"
                                                    className="w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                />
                                            )}
                                        </div>
                                    )}

                                    {project.competitive_brand_enabled && (
                                        <div
                                            data-testid="competitive-brand-block"
                                            data-step="2"
                                            className="mb-6"
                                        >
                                            <PremiumFormField
                                                label="Competitive Brand (declare conflicts)"
                                                value={form.competitive_brand}
                                                onChange={(v) => {
                                                    setForm({ ...form, competitive_brand: v });
                                                    if (validationErrors.competitive_brand) setValidationErrors((e) => ({ ...e, competitive_brand: undefined }));
                                                }}
                                                onBlur={saveForm}
                                                placeholder="Any brand conflict? Type 'None' if not"
                                                testid="form-competitive-brand"
                                                wide
                                                error={validationErrors.competitive_brand}
                                                inputRef={(el) => { fieldRefs.current.competitive_brand = el; }}
                                            />
                                        </div>
                                    )}

                                    {project.medium_usage && (
                                        <div className="border-t border-slate-100 pt-8" data-step="2">
                                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70 mb-4">Medium / Usage</p>
                                            <p className="text-[13px] leading-relaxed text-[#222222]">
                                                {project.medium_usage}
                                            </p>
                                        </div>
                                    )}

                                    {Array.isArray(project.custom_questions) && project.custom_questions.length > 0 && (
                                        <div className="border-t border-slate-100 pt-8 space-y-6" data-step="2">
                                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70">Additional Questions</p>
                                            {project.custom_questions.map((q) => (
                                                <PremiumFormField
                                                    key={q.id}
                                                    label={q.question}
                                                    value={
                                                        (form.custom_answers || {})[q.id] ||
                                                        ""
                                                    }
                                                    onChange={(v) =>
                                                        setForm({
                                                            ...form,
                                                            custom_answers: {
                                                                ...(form.custom_answers ||
                                                                    {}),
                                                                [q.id]: v,
                                                            },
                                                        })
                                                    }
                                                    onBlur={saveForm}
                                                    testid={`form-cq-${q.id}`}
                                                    wide
                                                />
                                            ))}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>

                        {/* Section 3: Work Links & Additional Material */}
                        <div data-step="2" className="bg-slate-50/40 rounded-2xl border border-[#eaeaea]/50 p-6">
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                                <div>
                                    <h3 className="text-base font-bold text-[#111111] tracking-tight">Work Links</h3>
                                    <p className="text-[12px] text-[#222222] mt-1 leading-relaxed">Add links to your professional websites or reels to showcase your previous work.</p>
                                </div>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            workLinks: !prev.workLinks,
                                        }))
                                    }
                                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                    title={collapsedSections.workLinks ? "Expand Work Links" : "Collapse Work Links"}
                                >
                                    <ChevronDown
                                        className={`h-4 w-4 transform transition-transform duration-200 ${
                                            collapsedSections.workLinks ? "-rotate-90" : ""
                                        }`}
                                    />
                                </button>
                            </div>

                            {!collapsedSections.workLinks && (
                                <div className="space-y-4 animate-fadeIn">
                                    <div data-testid="form-work-links-field">
                                        <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                            Work Links (optional)
                                        </span>
                                        <WorkLinksEditor
                                            links={form.work_links || []}
                                            onChange={(arr) => {
                                                setForm({ ...form, work_links: arr });
                                                setTimeout(saveForm, 0);
                                            }}
                                        />
                                    </div>
                                </div>
                            )}
                        </div>

                        {!saved && (
                            <button
                                type="submit"
                                disabled={starting}
                                data-testid="start-submission-btn"
                                className="hidden md:inline-flex w-full bg-slate-900 text-white py-4 rounded-full text-[13px] font-medium hover:bg-slate-800 hover:-translate-y-[1px] hover:shadow-lg items-center justify-center gap-2 min-h-[52px] transition-all duration-200"
                            >
                                {starting && (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                )}
                                Save Details & Continue to Uploads
                            </button>
                        )}
                        </>
                        )}
                    </form>
                    </div>
                </section>

                {/* SECTION 3 — UPLOADS (gated on email-first gate) */}
                {emailGateUnlocked && (
                    <section
                        ref={uploadsSectionRef}
                        className="pt-4"
                        data-testid="uploads-section"
                        data-step="3"
                    >
                        <div className="bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)]">
                        <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                            <div>
                                <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05] uppercase">
                                    AUDITION UPLOADS
                                </h2>
                            </div>
                            <button
                                type="button"
                                onClick={() =>
                                    setCollapsedSections(prev => ({
                                        ...prev,
                                        uploads: !prev.uploads,
                                    }))
                                }
                                className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                title={collapsedSections.uploads ? "Expand Uploads" : "Collapse Uploads"}
                            >
                                <ChevronDown
                                    className={`h-4 w-4 transform transition-transform duration-200 ${
                                        collapsedSections.uploads ? "-rotate-90" : ""
                                    }`}
                                />
                            </button>
                        </div>

                        {!collapsedSections.uploads && (
                            <div className="animate-fadeIn">

                                <PremiumUploadSlot
                                    title="Introduction Video"
                                    note="Upload your recent professional introduction video (no contact info)."
                                    icon={Video}
                                    accept="video/*"
                                    inputRef={introRef}
                                    onPick={(f) => triggerUpload(f[0], "intro_video")}
                                    uploadState={activeUploads["intro_video"]}
                                    media={intro}
                                    onRemove={(m) => removeMedia(m.id)}
                                    testid="upload-intro"
                                    cameraCapture="user"
                                    failed={Boolean(retryQueue["intro_video"]?.failed)}
                                    onRetry={() => retryUpload("intro_video")}
                                    hint="Recommended duration: 20–60 seconds."
                                />

                                <div className="mb-10" data-testid="takes-section">
                                    <div className="flex items-center justify-between mb-4">
                                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#333333]">
                                            Audition Takes{" "}
                                            <span className="text-[#333333]">
                                                (up to {MAX_TAKES})
                                            </span>
                                        </p>
                                        <span
                                            className="text-[11px] font-mono text-[#333333]"
                                            data-testid="takes-counter"
                                        >
                                            {takes.length}/{MAX_TAKES}
                                        </span>
                                    </div>
                                    <p className="text-[13px] leading-relaxed text-[#222222] mb-6">
                                        Upload each take as a separate video and label it (e.g., "Scene 1").
                                    </p>

                                    {takes.map((t, i) => (
                                        <PremiumTakeRow
                                            key={t.id}
                                            index={i + 1}
                                            media={t}
                                            canRename={!t._legacy}
                                            onRename={(lbl) =>
                                                patchTakeLabel(t.id, lbl)
                                            }
                                            onRemove={() => removeMedia(t.id)}
                                            onReplace={(file) => replaceMediaFile(t, file)}
                                            uploadState={activeUploads[`take:${t.label}`]}
                                        />
                                    ))}

                                    {Object.entries(activeUploads)
                                        .filter(([key, state]) => state.category === "take" && !takes.some(t => t.label === state.label))
                                        .map(([key, state]) => (
                                            <div key={key} className="bg-white border border-[#eaeaea] rounded-3xl p-4 flex flex-col gap-3 mb-4 shadow-[0_4px_20px_rgba(15,23,42,0.03)] text-left">
                                                <div className="flex items-center justify-between">
                                                    <div>
                                                        <span className="text-[11px] font-mono text-[#0c2340]/70 font-semibold uppercase tracking-wider mr-1">New Take:</span>
                                                        <span className="text-sm font-semibold text-[#111111]">{state.label}</span>
                                                    </div>
                                                    <span className="text-[10px] font-mono text-[#333333]">
                                                        {state.status === "uploading" ? `Uploading ${state.pct}%` : state.status === "failed" ? "Failed" : "Processing"}
                                                    </span>
                                                </div>
                                                {state.status === "failed" ? (
                                                    <div className="text-xs text-rose-500 font-mono mt-1 bg-rose-50/50 p-2.5 rounded-xl border border-rose-100 flex items-center justify-between gap-2">
                                                        <span className="truncate">{state.error || "Upload failed"}</span>
                                                        <button
                                                            type="button"
                                                            onClick={() => retryUpload(key)}
                                                            className="px-3 py-1 bg-white border border-rose-200 text-rose-600 rounded-full hover:bg-rose-50 active:scale-[0.97] transition-all duration-150 text-[10px]"
                                                        >
                                                            Retry
                                                        </button>
                                                    </div>
                                                ) : (
                                                    <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden mt-1">
                                                        <div
                                                            className={`h-full bg-[#0c2340] transition-all duration-300 ${state.status === "processing" ? "animate-pulse bg-emerald-500" : ""}`}
                                                            style={{ width: `${state.pct}%` }}
                                                        />
                                                    </div>
                                                )}
                                            </div>
                                        ))
                                    }

                                    {canAddTake && (
                                        <PremiumAddTakeSlot
                                            number={takes.length + 1}
                                            activeUploads={activeUploads}
                                            onPick={(file, label) =>
                                                triggerUpload(file, "take", label)
                                            }
                                            inputRef={newTakeRef}
                                        />
                                    )}
                                </div>

                                <div className="mb-8" data-testid="images-upload-section">
                                    <div className="flex items-center justify-between mb-3">
                                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#333333]">
                                            Images{" "}
                                            <span className="text-[#333333]">
                                                (optional)
                                            </span>
                                        </p>
                                        <span
                                            data-testid="image-counter"
                                            className="text-[11px] font-mono text-[#333333]"
                                        >
                                            {images.length}/{MAX_IMAGES_PER_CATEGORY}
                                        </span>
                                    </div>
                                    <p className="text-[13px] leading-relaxed text-[#222222] mb-6">
                                        Upload up to {MAX_IMAGES_PER_CATEGORY} images per category. Add your strongest recent professional looks.
                                    </p>

                                    {/* Phase 2 — optional Indian look images */}
                                    <PremiumPortfolioGroup
                                        label="Indian Look (optional)"
                                        hint="Saree, lehenga, sherwani, or any traditional/Indian-look references."
                                        items={indianImages}
                                        category="indian"
                                        allImagesCount={indianImages.length}
                                        maxImages={MAX_IMAGES_PER_CATEGORY}
                                        inputRef={indianImagesRef}
                                        uploadImages={uploadImages}
                                        removeMedia={removeMedia}
                                        activeUploads={activeUploads}
                                        onRetry={retryUpload}
                                        testidPrefix="indian"
                                        activePortfolioThumbId={activePortfolioThumbId}
                                        setActivePortfolioThumbId={setActivePortfolioThumbId}
                                        setActiveLightboxImage={setActiveLightboxImage}
                                        replaceMediaFile={replaceMediaFile}
                                    />

                                    {/* Phase 2 — optional Western look images */}
                                    <PremiumPortfolioGroup
                                        label="Western Look (optional)"
                                        hint="Casual, formal or western-styled references."
                                        items={westernImages}
                                        category="western"
                                        allImagesCount={westernImages.length}
                                        maxImages={MAX_IMAGES_PER_CATEGORY}
                                        inputRef={westernImagesRef}
                                        uploadImages={uploadImages}
                                        removeMedia={removeMedia}
                                        activeUploads={activeUploads}
                                        onRetry={retryUpload}
                                        testidPrefix="western"
                                        activePortfolioThumbId={activePortfolioThumbId}
                                        setActivePortfolioThumbId={setActivePortfolioThumbId}
                                        setActiveLightboxImage={setActiveLightboxImage}
                                        replaceMediaFile={replaceMediaFile}
                                    />

                                    {/* Generic Portfolio collapsible group */}
                                    <div className="mb-6 bg-slate-50/50 border border-[#eaeaea]/60 rounded-2xl p-4" data-testid="portfolio-group-generic">
                                        <div
                                            className="flex items-center justify-between cursor-pointer select-none"
                                            onClick={() => setIsGenericPortfolioCollapsed(!isGenericPortfolioCollapsed)}
                                        >
                                            <div className="flex items-center gap-2">
                                                <p className="uppercase tracking-[0.08em] text-[11px] font-semibold font-mono text-[#111111]">Portfolio (general)</p>
                                                <span className="text-[10px] font-mono font-semibold bg-slate-200/80 text-[#222222] px-2 py-0.5 rounded-full">
                                                    {images.length}
                                                </span>
                                            </div>
                                            <button
                                                type="button"
                                                className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                                title={isGenericPortfolioCollapsed ? "Expand" : "Collapse"}
                                            >
                                                <ChevronDown
                                                    className={`h-3.5 w-3.5 transform transition-transform duration-200 ${
                                                        isGenericPortfolioCollapsed ? "-rotate-90" : ""
                                                    }`}
                                                />
                                            </button>
                                        </div>

                                        {!isGenericPortfolioCollapsed && (
                                            <div className="mt-4 animate-fadeIn">
                                                <div className="grid grid-cols-3 md:grid-cols-4 gap-3 mb-4">
                                                    {images.map((m) => {
                                                        // Actions are hidden by default and revealed:
                                                        //   • Desktop — on hover (group-hover:opacity-100)
                                                        //   • Mobile  — on tap (activePortfolioThumbId === m.id)
                                                        const isActionsVisible = activePortfolioThumbId === m.id;
                                                        return (
                                                            <div
                                                                key={m.id}
                                                                className="relative aspect-square bg-slate-100 rounded-2xl overflow-hidden border border-[#eaeaea] group shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.1)] transition-all duration-300 hover:scale-[1.02] cursor-pointer"
                                                                onClick={(e) => {
                                                                    // Touch devices: first tap reveals the overlay;
                                                                    // the dismiss useEffect clears it when tapping outside.
                                                                    // Desktop: hover already shows the overlay, so click
                                                                    // goes straight to lightbox.
                                                                    const isTouch = window.matchMedia("(hover: none)").matches;
                                                                    if (isTouch && !isActionsVisible) {
                                                                        e.stopPropagation();
                                                                        setActivePortfolioThumbId(m.id);
                                                                        return;
                                                                    }
                                                                    setActivePortfolioThumbId(null);
                                                                    setActiveLightboxImage(m);
                                                                }}
                                                            >
                                                                <img
                                                                    src={thumbnailUrl(m)}
                                                                    alt=""
                                                                    className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                                                                />
                                                                {/* Action overlay — hidden by default, revealed on
                                                                    hover (desktop) or tap (mobile). */}
                                                                <div
                                                                    className={`absolute bottom-0 inset-x-0 h-10 bg-gradient-to-t from-black/70 via-black/45 to-transparent flex items-center justify-end px-2 gap-2 transition-opacity duration-200 ${
                                                                        isActionsVisible
                                                                            ? "opacity-100"
                                                                            : "opacity-0 group-hover:opacity-100"
                                                                    }`}
                                                                >
                                                                    <button
                                                                        type="button"
                                                                        onClick={(e) => {
                                                                            e.stopPropagation();
                                                                            setActivePortfolioThumbId(null);
                                                                            setActiveLightboxImage(m);
                                                                        }}
                                                                        className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                                                        title="Zoom"
                                                                    >
                                                                        <Search className="w-3.5 h-3.5" />
                                                                    </button>
                                                                    <button
                                                                        type="button"
                                                                        onClick={(e) => {
                                                                            e.stopPropagation();
                                                                            setActivePortfolioThumbId(null);
                                                                            const inp = document.createElement("input");
                                                                            inp.type = "file";
                                                                            inp.accept = "image/*";
                                                                            inp.onchange = (ev) => {
                                                                                if (ev.target.files?.length) {
                                                                                    replaceMediaFile(m, ev.target.files[0]);
                                                                                }
                                                                            };
                                                                            inp.click();
                                                                        }}
                                                                        className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                                                        title="Replace"
                                                                    >
                                                                        <Upload className="w-3.5 h-3.5" />
                                                                    </button>
                                                                    <button
                                                                        type="button"
                                                                        onClick={(e) => {
                                                                            e.stopPropagation();
                                                                            setActivePortfolioThumbId(null);
                                                                            removeMedia(m.id);
                                                                        }}
                                                                        className="w-7 h-7 bg-white/90 hover:bg-rose-50 text-rose-600 rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                                                        title="Delete"
                                                                    >
                                                                        <Trash2 className="w-3.5 h-3.5" />
                                                                    </button>
                                                                </div>
                                                            </div>
                                                        );
                                                    })}
                                                    {Object.entries(activeUploads)
                                                        .filter(([key, state]) => state.category === "image")
                                                        .map(([key, state]) => (
                                                            <div key={key} className="relative aspect-square bg-slate-50 border border-[#eaeaea] rounded-2xl flex flex-col items-center justify-center p-2 shadow-sm text-center">
                                                                <Loader2 className="w-5 h-5 animate-spin text-[#0c2340] mb-1" />
                                                                <span className="text-[9px] font-mono text-[#333333] truncate w-full px-1">{state.fileName}</span>
                                                                <span className="text-[10px] font-mono font-semibold text-[#111111] mt-1">
                                                                    {state.status === "uploading" ? `${state.pct}%` : state.status === "failed" ? "Failed" : "Processing"}
                                                                </span>
                                                                {state.status === "failed" ? (
                                                                    <button
                                                                        type="button"
                                                                        onClick={() => retryUpload(key)}
                                                                        className="mt-1 px-2.5 py-0.5 border border-rose-200 text-rose-600 rounded-full hover:bg-rose-50 text-[9px] font-semibold"
                                                                    >
                                                                        Retry
                                                                    </button>
                                                                ) : (
                                                                    <div className="absolute bottom-1 inset-x-2 bg-slate-100 rounded-full h-1 overflow-hidden">
                                                                        <div className={`bg-[#0c2340] h-full transition-all duration-300 ${state.status === "processing" ? "animate-pulse bg-emerald-500" : ""}`} style={{ width: `${state.pct}%` }} />
                                                                    </div>
                                                                )}
                                                            </div>
                                                        ))
                                                    }
                                                    {images.length < MAX_IMAGES_PER_CATEGORY && (
                                                        <button
                                                            type="button"
                                                            onClick={() =>
                                                                imagesRef.current?.click()
                                                            }
                                                            data-testid="add-image-btn"
                                                            className="relative aspect-square rounded-2xl border border-dashed border-[#d4d4d4] hover:border-[#0c2340]/30 hover:bg-[#0c2340]/5 flex items-center justify-center text-[#333333] hover:text-[#0c2340] transition-all duration-200 overflow-hidden bg-gradient-to-b from-white to-slate-50/70 shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px]"
                                                        >
                                                            <div className="relative flex flex-col items-center gap-1">
                                                                <Camera className="w-5 h-5" />
                                                                <span className="text-[10px] font-mono">
                                                                    Add
                                                                </span>
                                                            </div>
                                                        </button>
                                                    )}
                                                </div>
                                                <input
                                                    ref={imagesRef}
                                                    type="file"
                                                    accept="image/*"
                                                    multiple
                                                    className="hidden"
                                                    onChange={(e) => {
                                                        if (e.target.files?.length)
                                                            uploadImages(e.target.files);
                                                        e.target.value = "";
                                                    }}
                                                />
                                                {/* Mobile-only camera-first action */}
                                                <input
                                                    ref={cameraImagesRef}
                                                    type="file"
                                                    accept="image/*"
                                                    capture="environment"
                                                    className="hidden"
                                                    onChange={(e) => {
                                                        if (e.target.files?.length)
                                                            uploadImages(e.target.files);
                                                        e.target.value = "";
                                                    }}
                                                />
                                                <div className="md:hidden grid grid-cols-2 gap-2 mt-3">
                                                    <button
                                                        type="button"
                                                        onClick={() => cameraImagesRef.current?.click()}
                                                        disabled={Object.values(activeUploads).some((u) => u.category === "image" && u.status === "uploading") || images.length >= MAX_IMAGES_PER_CATEGORY}
                                                        data-testid="add-image-camera-btn"
                                                        className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                                    >
                                                        <Camera className="w-3.5 h-3.5" /> Take photo
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={() => imagesRef.current?.click()}
                                                        disabled={Object.values(activeUploads).some((u) => u.category === "image" && u.status === "uploading") || images.length >= MAX_IMAGES_PER_CATEGORY}
                                                        data-testid="add-image-library-btn"
                                                        className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                                    >
                                                        <FolderOpen className="w-3.5 h-3.5" /> From library
                                                    </button>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                </div>
                            </div>
                        )}

                        <div data-sticky-footer className="sticky bottom-0 z-30 bg-gradient-to-t from-white via-white/95 to-transparent pt-4 pb-[calc(1.5rem+env(safe-area-inset-bottom))] pb-safe">
                            <p className="text-[12px] text-[#333333] text-center mb-3 max-w-md mx-auto leading-relaxed" data-testid="submission-accuracy-warning">
                                Please ensure your details, portfolio and videos are accurate and up to date. Casting decisions are based on the information submitted here.
                            </p>
                            <button
                                onClick={finalize}
                                disabled={finalizing}
                                data-testid="finalize-submission-btn"
                                className="w-full bg-slate-900 text-white py-4 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-2 min-h-[52px] transition-all duration-200"
                                style={{ WebkitTapHighlightColor: "transparent" }}
                            >
                                {finalizing ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Sparkles className="w-4 h-4" />
                                )
                                }
                                Submit Audition
                            </button>
                            {!readyToSubmit && (
                                <p className="text-[11px] text-[#333333] text-center mt-3 font-mono">
                                    Need: First+Last name · Height · Location ·
                                    Availability · Budget
                                </p>
                            )}
                        </div>
                        </div>
                    </section>
                )}
            </div>

            {showMaterial && (
                <MaterialModal
                    project={project}
                    onClose={() => setShowMaterial(false)}
                />
            )}

            {activeLightboxImage && (
                <div
                    className="fixed inset-0 z-50 bg-black/90 backdrop-blur-sm flex items-center justify-center p-4 transition-all duration-200 animate-in fade-in"
                    onClick={() => setActiveLightboxImage(null)}
                >
                    <button
                        type="button"
                        onClick={() => setActiveLightboxImage(null)}
                        className="absolute top-4 right-4 p-2 bg-white/10 hover:bg-white/20 text-white rounded-full transition-colors min-w-[44px] min-h-[44px] flex items-center justify-center"
                    >
                        <X className="w-5 h-5" />
                    </button>
                    <img
                        src={activeLightboxImage.url}
                        alt=""
                        className="max-w-full max-h-[85vh] object-contain rounded-lg shadow-2xl animate-in zoom-in-95 duration-200"
                    />
                </div>
            )}
        </div>
    );
}

function Info({ label, value, wide }) {
    if (!value) return null;
    return (
        <div className={wide ? "col-span-1 sm:col-span-2" : ""}>
            <div className="text-[10px] tracking-[0.2em] uppercase font-mono text-[#333333] mb-1">
                {label}
            </div>
            <div className="text-[13px] font-medium text-[#111111]">{value}</div>
        </div>
    );
}

function PremiumPortfolioGroup({
    label,
    hint,
    items,
    category,
    allImagesCount,
    maxImages,
    inputRef,
    uploadImages,
    removeMedia,
    activeUploads = {},
    onRetry,
    testidPrefix,
    activePortfolioThumbId,
    setActivePortfolioThumbId,
    setActiveLightboxImage,
    replaceMediaFile,
}) {
    const reachedCap = allImagesCount >= maxImages;
    const [isCollapsed, setIsCollapsed] = useState(() => {
        return typeof window !== "undefined" && window.innerWidth < 768;
    });
    return (
        <div className="mb-6 bg-slate-50/50 border border-[#eaeaea]/60 rounded-2xl p-4" data-testid={`portfolio-group-${testidPrefix}`}>
            <div
                className="flex items-center justify-between cursor-pointer select-none"
                onClick={() => setIsCollapsed(!isCollapsed)}
            >
                <div className="flex items-center gap-2">
                    <p className="uppercase tracking-[0.08em] text-[11px] font-semibold font-mono text-[#111111]">{label}</p>
                    <span className="text-[10px] font-mono font-semibold bg-slate-200/80 text-[#222222] px-2 py-0.5 rounded-full">
                        {items.length}
                    </span>
                </div>
                <button
                    type="button"
                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                    title={isCollapsed ? "Expand" : "Collapse"}
                >
                    <ChevronDown
                        className={`h-3.5 w-3.5 transform transition-transform duration-200 ${
                            isCollapsed ? "-rotate-90" : ""
                        }`}
                    />
                </button>
            </div>
            {!isCollapsed && (
                <div className="mt-4 animate-fadeIn">
                    {hint && (
                        <p className="text-[12px] text-[#222222] mb-4 leading-relaxed">
                            {hint}
                        </p>
                    )}
                    <div className="grid grid-cols-3 md:grid-cols-4 gap-3">
                        {items.map((m) => {
                            const isActionsVisible = activePortfolioThumbId === m.id;
                            return (
                                <div
                                    key={m.id}
                                    className="relative aspect-square bg-slate-100 rounded-2xl overflow-hidden border border-[#eaeaea] group shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.1)] transition-all duration-300 hover:scale-[1.02] cursor-pointer"
                                    data-testid={`${testidPrefix}-image-${m.id}`}
                                    onClick={(e) => {
                                        const isTouch = window.matchMedia("(hover: none)").matches;
                                        if (isTouch && !isActionsVisible) {
                                            e.stopPropagation();
                                            setActivePortfolioThumbId(m.id);
                                            return;
                                        }
                                        setActivePortfolioThumbId(null);
                                        setActiveLightboxImage(m);
                                    }}
                                >
                                    <img
                                        src={thumbnailUrl(m)}
                                        alt=""
                                        className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                                    />
                                    <div
                                        className={`absolute bottom-0 inset-x-0 h-10 bg-gradient-to-t from-black/70 via-black/45 to-transparent flex items-center justify-end px-2 gap-2 transition-opacity duration-200 ${
                                            isActionsVisible
                                                ? "opacity-100"
                                                : "opacity-0 group-hover:opacity-100"
                                        }`}
                                    >
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActivePortfolioThumbId(null);
                                                setActiveLightboxImage(m);
                                            }}
                                            className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                            title="Zoom"
                                        >
                                            <Search className="w-3.5 h-3.5" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActivePortfolioThumbId(null);
                                                const inp = document.createElement("input");
                                                inp.type = "file";
                                                inp.accept = "image/*";
                                                inp.onchange = (ev) => {
                                                    if (ev.target.files?.length) {
                                                        replaceMediaFile(m, ev.target.files[0]);
                                                    }
                                                };
                                                inp.click();
                                            }}
                                            className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                            title="Replace"
                                        >
                                            <Upload className="w-3.5 h-3.5" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActivePortfolioThumbId(null);
                                                removeMedia(m.id);
                                            }}
                                            data-testid={`${testidPrefix}-image-remove-${m.id}`}
                                            className="w-7 h-7 bg-white/90 hover:bg-rose-50 text-rose-600 rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                            title="Delete"
                                        >
                                            <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                        {Object.entries(activeUploads)
                            .filter(([key, state]) => state.category === category)
                            .map(([key, state]) => (
                                <div key={key} className="relative aspect-square bg-slate-50 border border-[#eaeaea] rounded-2xl flex flex-col items-center justify-center p-2 shadow-sm text-center">
                                    <Loader2 className="w-5 h-5 animate-spin text-[#0c2340] mb-1" />
                                    <span className="text-[9px] font-mono text-[#333333] truncate w-full px-1">{state.fileName}</span>
                                    <span className="text-[10px] font-mono font-semibold text-[#111111] mt-1">
                                        {state.status === "uploading" ? `${state.pct}%` : state.status === "failed" ? "Failed" : "Processing"}
                                    </span>
                                    {state.status === "failed" ? (
                                        <button
                                            type="button"
                                            onClick={() => onRetry && onRetry(key)}
                                            className="mt-1 px-2.5 py-0.5 border border-rose-200 text-rose-600 rounded-full hover:bg-rose-50 text-[9px] font-semibold"
                                        >
                                            Retry
                                        </button>
                                    ) : (
                                        <div className="absolute bottom-1 inset-x-2 bg-slate-100 rounded-full h-1 overflow-hidden">
                                            <div className={`bg-[#0c2340] h-full transition-all duration-300 ${state.status === "processing" ? "animate-pulse bg-emerald-500" : ""}`} style={{ width: `${state.pct}%` }} />
                                        </div>
                                    )}
                                </div>
                            ))
                        }
                        {!reachedCap && (
                            <button
                                type="button"
                                onClick={() => inputRef.current?.click()}
                                data-testid={`add-${testidPrefix}-image-btn`}
                                className="relative aspect-square rounded-2xl border border-dashed border-[#d4d4d4] hover:border-[#0c2340]/30 hover:bg-[#0c2340]/5 flex items-center justify-center text-[#333333] hover:text-[#0c2340] transition-all duration-200 overflow-hidden bg-gradient-to-b from-white to-slate-50/70 shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px]"
                            >
                                <div className="relative flex flex-col items-center gap-1">
                                    <Plus className="w-5 h-5" />
                                    <span className="text-[10px] font-mono">Add</span>
                                </div>
                            </button>
                        )}
                    </div>
                </div>
            )}
            <input
                ref={inputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length)
                        uploadImages(e.target.files, category);
                    e.target.value = "";
                }}
            />
        </div>
    );
}

// ---------------------------------------------------------------------------
// Work-links helpers
// ---------------------------------------------------------------------------
const WORK_LINKS_URL_RE = /https?:\/\/[^\s]+/;

function parseStoredWorkLink(stored) {
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
            const match = WORK_LINKS_URL_RE.exec(line);
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
            const { label, url } = parseStoredWorkLink(w);
            return label ? `${label} - ${url}` : url;
        })
        .join("\n");
}

function WorkLinksEditor({ links, onChange }) {
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
        <div className="mt-2 space-y-3" data-testid="work-links-editor">
            <textarea
                value={draft}
                onChange={handleChange}
                data-testid="work-link-input"
                rows={5}
                placeholder={
                    "Paste all your work links here, one per line.\n" +
                    "Examples:\n" +
                    "Puma Campaign - https://instagram.com/reel/abc\n" +
                    "Pepsi - https://youtu.be/xyz\n" +
                    "https://vimeo.com/showreel"
                }
                className="w-full bg-white/60 border border-[#eaeaea] rounded-2xl p-4 text-[16px] md:text-[14px] text-[#111111] placeholder:text-[#333333] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none transition-all duration-200 resize-y font-mono leading-relaxed shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
            />
            <div className="flex items-center gap-2">
                <span
                    className={`text-[11px] font-mono px-2 py-0.5 rounded-full border ${
                        parsed.length > 0
                            ? "text-emerald-700 bg-emerald-50 border-emerald-100"
                            : "text-[#333333] bg-slate-50 border-slate-100"
                    }`}
                >
                    Detected Links: {parsed.length}
                </span>
                {parsed.length > 0 && (
                    <span className="text-[10px] text-[#333333]">
                        {parsed.map((s) => parseStoredWorkLink(s).label || "Unlabeled").join(" · ")}
                    </span>
                )}
            </div>
            {parsed.length > 0 && (
                <div className="space-y-1.5 pt-1" data-testid="work-links-preview">
                    {parsed.map((stored, i) => {
                        const { label, url } = parseStoredWorkLink(stored);
                        return (
                            <div
                                key={i}
                                className="flex items-center gap-2 px-3 py-2 bg-white/60 border border-[#eaeaea] rounded-xl"
                                data-testid={`work-link-row-${i}`}
                            >
                                {label && (
                                    <span className="text-[11px] text-[#333333] font-medium shrink-0 max-w-[120px] truncate">
                                        {label}
                                    </span>
                                )}
                                <a
                                    href={url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-[11px] font-mono text-[#222222] hover:text-[#111111] truncate underline underline-offset-2 flex-1 min-w-0"
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

function PremiumFormField({
    label,
    value,
    onChange,
    onBlur,
    type = "text",
    required,
    placeholder,
    testid,
    wide,
    hint,
    max,
    disabled,
    className = "",
    error,
    inputRef,
}) {
    const [localValue, setLocalValue] = useState(value || "");

    // Sync local state when prop value changes externally (e.g. from prefill)
    useEffect(() => {
        setLocalValue(value || "");
    }, [value]);

    // Debounce synchronization to parent state to avoid re-rendering parent tree on every keystroke
    useEffect(() => {
        const handler = setTimeout(() => {
            if (localValue !== (value || "")) {
                onChange(localValue);
            }
        }, 200);
        return () => clearTimeout(handler);
    }, [localValue, onChange, value]);

    const handleBlur = (e) => {
        onChange(localValue);
        if (onBlur) onBlur(e);
    };

    return (
        <label className={`block ${wide ? "md:col-span-2" : ""}`}>
            <span className="text-[11px] text-[#111111] tracking-[0.08em] font-semibold uppercase font-mono">
                {label}
            </span>
            <input
                ref={inputRef}
                type={type}
                value={localValue}
                onChange={(e) => setLocalValue(e.target.value)}
                onBlur={handleBlur}
                required={required}
                placeholder={placeholder}
                max={max}
                disabled={disabled}
                inputMode={
                    type === "email"
                        ? "email"
                        : type === "tel"
                          ? "tel"
                          : type === "number"
                            ? "numeric"
                            : undefined
                }
                enterKeyHint="next"
                autoComplete={
                    type === "email"
                        ? "email"
                        : type === "tel"
                          ? "tel"
                          : undefined
                }
                data-testid={testid}
                className={`mt-2 w-full bg-white/60 rounded-2xl border focus:ring-4 focus:ring-[#0c2340]/10 outline-none py-3 px-4 text-[16px] md:text-[15px] text-[#111111] placeholder:text-[#333333] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] disabled:text-[#333333] ${
                    error
                        ? "border-rose-400 focus:border-rose-400 focus:ring-rose-400/10 bg-rose-50/30"
                        : "border-[#eaeaea] focus:border-[#0c2340]/40 bg-white/60"
                } ${className}`}
            />
            {error && (
                <span className="block text-[11px] text-rose-500 mt-1.5 font-mono animate-in fade-in duration-150">
                    ⚠ {error}
                </span>
            )}
            {!error && hint && (
                <span className="block text-[10.5px] text-[#333333] mt-1 font-mono">
                    {hint}
                </span>
            )}
        </label>
    );
}

function PremiumUploadSlot({
    title,
    required,
    note,
    icon: Icon,
    error,
    accept,
    inputRef,
    onPick,
    uploadState, // replacement state mapping
    media,
    onRemove,
    testid,
    compact,
    cameraCapture, // "user" | "environment" — shows a camera-first option on mobile
    onRetry,       // optional: shown when this slot has a failed retry queue entry
    failed,
    hint,
}) {
    const hasFile = Boolean(media);
    const cameraRef = useRef(null);
    const isVideo = (accept || "").includes("video");
    const isPending = uploadState && uploadState.status !== "completed";
    const [isVideoCollapsed, setIsVideoCollapsed] = useState(() => {
        return isVideo && hasFile && typeof window !== "undefined" && window.innerWidth < 768;
    });

    return (
        <div
            className={`${compact ? "mb-4" : "mb-10"} ${
                error ? "rounded-2xl ring-2 ring-rose-400/60 bg-rose-50/20 p-4" : ""
            }`}
        >
            {!compact && (
                <div className="flex items-center justify-between mb-3">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70">
                        {title}
                        {required && (
                            <span className="text-rose-500"> *</span>
                        )}
                    </p>
                    {hasFile && (
                        <span className="inline-flex items-center gap-1 text-[10px] tracking-[0.2em] uppercase font-mono text-emerald-600">
                            <Check className="w-3 h-3" /> Uploaded
                        </span>
                    )}
                </div>
            )}
            {!compact && note && (
                <p className="text-[12px] leading-relaxed text-[#333333] mb-5">
                    {note}
                </p>
            )}
            {!compact && hint && (
                <p className="text-[11px] text-[#333333] font-mono mb-5">
                    {hint}
                </p>
            )}
            {hasFile ? (
                isVideo ? (
                    <div className="bg-white border border-[#eaeaea] rounded-3xl p-4 flex flex-col gap-3 shadow-[0_4px_20px_rgba(15,23,42,0.03)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.05)] text-left">
                        <div className="flex items-center justify-between border-b border-slate-100 pb-2.5">
                            <div className="flex items-center gap-2">
                                <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-slate-100 text-[#111111]">
                                    <Video className="w-4 h-4" />
                                </span>
                                <div>
                                    <h4 className="font-bold text-sm text-[#111111]">{title || "Video"}</h4>
                                    <p className="text-[11px] text-[#333333] truncate max-w-[180px] font-mono">{media.original_filename || "video_file"}</p>
                                </div>
                            </div>
                            <button
                                type="button"
                                onClick={() => setIsVideoCollapsed(!isVideoCollapsed)}
                                className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                title={isVideoCollapsed ? "Expand preview" : "Collapse preview"}
                            >
                                <ChevronDown
                                    className={`h-4 w-4 transform transition-transform duration-200 ${
                                        isVideoCollapsed ? "-rotate-90" : ""
                                    }`}
                                />
                            </button>
                        </div>

                        {!isVideoCollapsed && (
                            <div className="relative rounded-2xl overflow-hidden bg-slate-900 border border-slate-100 flex items-center justify-center max-h-[240px] animate-fadeIn">
                                <video
                                    src={media.url}
                                    poster={posterUrl(media) || thumbnailUrl(media)}
                                    controls
                                    playsInline
                                    preload="metadata"
                                    className="w-full max-h-[240px] object-contain rounded-2xl"
                                />
                                {media.duration && (
                                    <span className="absolute bottom-3 right-3 bg-black/75 backdrop-blur-sm text-white text-[10px] font-mono font-medium px-2 py-0.5 rounded-full shadow-sm">
                                        {formatDuration(media.duration)}
                                    </span>
                                )}
                            </div>
                        )}
                        <div className="flex flex-col gap-1.5 px-1">
                            {formatMediaTimestamp(media) && (
                                <span className="text-[11px] text-[#333333] font-mono">
                                    Previously uploaded · Last updated: {formatMediaTimestamp(media)}
                                </span>
                            )}
                        </div>
                        <div className="flex items-center gap-2 pt-2 border-t border-slate-100">
                            {isPending ? (
                                <div className="w-full px-1">
                                    <div className="flex items-center justify-between text-xs mb-1 font-mono text-[#333333]">
                                        <span>{uploadState.status === "uploading" ? `Replacing… ${uploadState.pct}%` : uploadState.status === "failed" ? "Failed to replace" : "Processing replacement…"}</span>
                                    </div>
                                    <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
                                        <div className={`h-full bg-[#0c2340] transition-all duration-300 ${uploadState.status === "processing" ? "animate-pulse bg-emerald-500" : ""}`} style={{ width: `${uploadState.pct}%` }} />
                                    </div>
                                </div>
                            ) : (
                                <div className="flex items-center gap-2 w-full">
                                    <button
                                        type="button"
                                        onClick={() => inputRef.current?.click()}
                                        className="flex-1 border border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111] hover:bg-slate-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                                    >
                                        <Upload className="w-3.5 h-3.5" />
                                        Replace
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => onRemove(media)}
                                        className="flex-1 border border-rose-200 hover:border-rose-300 text-rose-600 hover:bg-rose-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                        Delete
                                    </button>
                                </div>
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="bg-white/60 border border-[#eaeaea] rounded-2xl p-3 flex items-center gap-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                        <Icon className="w-4 h-4 text-[#333333] shrink-0" />
                        <div className="min-w-0 flex-1">
                            <div className="text-[13px] truncate">
                                {compact && (
                                    <span className="font-display mr-2 text-[#111111]">
                                        {title}
                                        {required && (
                                            <span className="text-rose-500">
                                                {" "}*
                                            </span>
                                        )}
                                    </span>
                                )}
                                <span className="text-[#333333] font-mono text-[11px]">
                                    {media.original_filename || "file"}
                                </span>
                            </div>
                        </div>
                        <button
                            onClick={() => onRemove(media)}
                            className="text-[#333333] hover:text-rose-500 p-1 min-w-[44px] min-h-[44px] flex items-center justify-center transition-colors"
                        >
                            <Trash2 className="w-4 h-4" />
                        </button>
                    </div>
                )
            ) : (
                <>
                    {/* Mobile: camera-first dual buttons. Desktop: single
                        upload trigger. The camera input carries `capture`
                        which makes iOS/Android jump straight into the
                        recorder UI. */}
                    {cameraCapture && (
                        <div className="md:hidden grid grid-cols-2 gap-2 mb-3">
                            <button
                                type="button"
                                onClick={() => cameraRef.current?.click()}
                                disabled={isPending}
                                data-testid={`${testid}-camera-btn`}
                                className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3.5 text-[13px] rounded-full flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                            >
                                <Camera className="w-4 h-4" />
                                {isVideo ? "Record" : "Take photo"}
                            </button>
                            <button
                                type="button"
                                onClick={() => inputRef.current?.click()}
                                disabled={isPending}
                                data-testid={`${testid}-library-btn`}
                                className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3.5 text-[13px] rounded-full flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                            >
                                <FolderOpen className="w-4 h-4" />
                                From library
                            </button>
                        </div>
                    )}
                    <button
                        onClick={() => inputRef.current?.click()}
                        disabled={isPending}
                        data-testid={`${testid}-btn`}
                        className={`w-full bg-gradient-to-b from-white to-slate-50/70 border border-[#eaeaea] hover:border-[#0c2340]/30 p-4 text-left min-h-[60px] flex items-center gap-3 transition-all duration-200 relative overflow-hidden rounded-2xl shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px] ${cameraCapture ? "hidden md:flex" : ""}`}
                    >
                        {uploadState && uploadState.status !== "failed" && uploadState.pct > 0 && (
                            <span
                                aria-hidden
                                className="absolute inset-y-0 left-0 bg-[#0c2340]/30 transition-[width] duration-300"
                                style={{ width: `${uploadState.pct}%` }}
                            />
                        )}
                        {uploadState && uploadState.status !== "failed" ? (
                            <Loader2 className="w-4 h-4 animate-spin relative text-[#222222]" />
                        ) : (
                            <Upload className="w-4 h-4 text-[#333333] relative" />
                        )}
                        {compact ? (
                            <span className="text-[13px] flex-1 relative text-[#111111]">
                                <span className="font-display mr-2">
                                    {title}
                                    {required && (
                                        <span className="text-rose-500"> *</span>
                                    )}
                                </span>
                                <span className="text-[#333333] text-[11px]">
                                    {uploadState && uploadState.status === "processing" ? "Processing…" : (uploadState && uploadState.status === "uploading" ? `Uploading… ${uploadState.pct}%` : "Tap to upload")}
                                </span>
                            </span>
                        ) : (
                            <span className="text-[13px] text-[#222222] relative">
                                {uploadState && uploadState.status === "processing" ? "Processing…" : (uploadState && uploadState.status === "uploading" ? `Uploading… ${uploadState.pct}%` : "Tap to upload")}
                            </span>
                        )}
                    </button>
                    {failed && onRetry && (
                        <button
                            type="button"
                            onClick={onRetry}
                            data-testid={`${testid}-retry-btn`}
                            className="mt-3 w-full text-[11px] px-4 py-2.5 border border-rose-200 text-rose-600 hover:bg-rose-50 rounded-full inline-flex items-center justify-center gap-2 min-h-[44px] transition-all duration-200"
                        >
                            <Loader2 className="w-3.5 h-3.5" />
                            Upload failed — Retry
                        </button>
                    )}
                </>
            )}
            <input
                ref={inputRef}
                type="file"
                accept={accept}
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length) onPick(e.target.files);
                    e.target.value = "";
                }}
            />
            {cameraCapture && (
                <input
                    ref={cameraRef}
                    type="file"
                    accept={accept}
                    capture={cameraCapture}
                    className="hidden"
                    onChange={(e) => {
                        if (e.target.files?.length) onPick(e.target.files);
                        e.target.value = "";
                    }}
                />
            )}
        {error && (
            <p className="mt-2 text-[11px] text-rose-500 font-mono animate-in fade-in duration-150">
                ⚠ {error}
            </p>
        )}
        </div>
    );
}

// --------------------------------------------------------------------------
// Renamable take row (existing take) — supports inline label edit + remove
// --------------------------------------------------------------------------
function PremiumTakeRow({ index, media, canRename, onRename, onRemove, onReplace, uploadState }) {
    const [label, setLabel] = useState(media.label || `Take ${index}`);
    const [dirty, setDirty] = useState(false);
    const localInputRef = useRef(null);

    useEffect(() => {
        setLabel(media.label || `Take ${index}`);
        setDirty(false);
    }, [media.label, media.id, index]);

    const save = () => {
        const val = (label || "").trim();
        if (!val) return;
        if (val !== (media.label || "")) onRename(val);
        setDirty(false);
    };

    const isPending = uploadState && uploadState.status !== "completed";

    return (
        <div
            className="bg-white border border-[#eaeaea] rounded-3xl p-4 flex flex-col gap-3 mb-4 shadow-[0_4px_20px_rgba(15,23,42,0.03)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.05)] text-left"
            data-testid={`take-row-${index}`}
        >
            <div className="relative rounded-2xl overflow-hidden bg-slate-900 border border-slate-100 flex items-center justify-center max-h-[220px]">
                <video
                    src={media.url}
                    poster={posterUrl(media) || thumbnailUrl(media)}
                    controls
                    playsInline
                    preload="metadata"
                    className="w-full max-h-[220px] object-contain rounded-2xl"
                />
                {media.duration && (
                    <span className="absolute bottom-3 right-3 bg-black/75 backdrop-blur-sm text-white text-[10px] font-mono font-medium px-2 py-0.5 rounded-full shadow-sm">
                        {formatDuration(media.duration)}
                    </span>
                )}
            </div>

            <div className="flex-1 min-w-0 px-1">
                <div className="flex items-center gap-2">
                    <span className="text-[11px] font-mono text-[#0c2340]/70 font-semibold uppercase tracking-wider">Label:</span>
                    {canRename ? (
                        <input
                            value={label}
                            onChange={(e) => {
                                  setLabel(e.target.value);
                                  setDirty(true);
                            }}
                            onBlur={save}
                            onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                    e.preventDefault();
                                    e.currentTarget.blur();
                                }
                            }}
                            placeholder={`Take ${index}`}
                            className={`bg-transparent outline-none text-[13px] font-semibold flex-1 py-1 px-2 rounded-lg border transition-all duration-200 ${dirty ? "border-[#0c2340]/30 bg-[#0c2340]/5" : "border-slate-100 bg-slate-50/50"} focus:border-[#0c2340]/40 text-[#111111]`}
                            data-testid={`take-label-${index}`}
                        />
                    ) : (
                        <div className="text-[13px] font-semibold text-[#111111] py-1">
                            {label}
                            <span className="ml-2 text-[10px] text-[#333333] font-mono font-normal">
                                (legacy)
                            </span>
                        </div>
                    )}
                </div>
                <div className="text-[10px] font-mono text-[#333333] truncate mt-2 flex flex-col gap-0.5">
                    <span>{media.original_filename || "file"}</span>
                    {formatMediaTimestamp(media) && (
                        <span className="text-[#333333] font-medium mt-0.5">
                            Previously uploaded · Last updated: {formatMediaTimestamp(media)}
                        </span>
                    )}
                </div>
            </div>

            <div className="flex items-center gap-2 pt-2 border-t border-slate-100">
                {isPending ? (
                    <div className="w-full px-1">
                        <div className="flex items-center justify-between text-xs mb-1 font-mono text-[#333333]">
                            <span>{uploadState.status === "uploading" ? `Replacing… ${uploadState.pct}%` : uploadState.status === "failed" ? "Failed to replace" : "Processing replacement…"}</span>
                        </div>
                        <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
                            <div className={`h-full bg-[#0c2340] transition-all duration-300 ${uploadState.status === "processing" ? "animate-pulse bg-emerald-500" : ""}`} style={{ width: `${uploadState.pct}%` }} />
                        </div>
                    </div>
                ) : (
                    <>
                        <button
                            type="button"
                            onClick={() => localInputRef.current?.click()}
                            className="flex-1 border border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111] hover:bg-slate-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                        >
                            <Upload className="w-3.5 h-3.5" />
                            Replace
                        </button>
                        <button
                            type="button"
                            onClick={onRemove}
                            className="flex-1 border border-rose-200 hover:border-rose-300 text-rose-600 hover:bg-rose-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                            data-testid={`take-remove-${index}`}
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                            Delete
                        </button>
                    </>
                )}
            </div>

            <input
                ref={localInputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length) onReplace(e.target.files[0]);
                    e.target.value = "";
                }}
            />
        </div>
    );
}

// --------------------------------------------------------------------------
// Add-a-new-take slot — user picks a file, we upload with the label they type
// (falls back to "Take N" if empty).
// --------------------------------------------------------------------------
function PremiumAddTakeSlot({ number, required, onPick, inputRef }) {
    const [label, setLabel] = useState("");
    const cameraRef = useRef(null);
    const fallback = `Take ${number}`;
    const triggerLib = () => inputRef.current?.click();
    const triggerCam = () => cameraRef.current?.click();

    return (
        <div
            className="bg-gradient-to-b from-white to-slate-50/70 border border-[#eaeaea] hover:border-[#0c2340]/30 rounded-2xl p-3 relative overflow-hidden shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] transition-all duration-200 hover:-translate-y-[1px]"
            data-testid={`add-take-${number}`}
        >
            <div className="flex items-center gap-2 relative">
                <input
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder={`${fallback} — add a label`}
                    className="flex-1 bg-transparent outline-none text-[16px] md:text-[13px] py-1.5 px-3 rounded-xl border border-[#eaeaea] focus:border-[#0c2340]/40 focus:ring-2 focus:ring-[#0c2340]/10 transition-all duration-200 text-[#111111]"
                    enterKeyHint="done"
                    data-testid={`new-take-label-${number}`}
                />
                <button
                    type="button"
                    onClick={triggerLib}
                    className="hidden md:inline-flex relative text-[11px] px-4 py-2 border border-[#eaeaea] hover:border-[#d4d4d4] rounded-full items-center gap-1 disabled:opacity-40 min-h-[44px] bg-white/60 text-[#222222] transition-all duration-200"
                    data-testid={`new-take-upload-${number}`}
                >
                    <Plus className="w-3 h-3" />
                    {"Upload"}
                    {required && <span className="text-rose-500">*</span>}
                </button>
            </div>
            {/* Mobile-only camera-first dual buttons */}
            <div className="md:hidden grid grid-cols-2 gap-2 mt-3 relative">
                <button
                    type="button"
                    onClick={triggerCam}
                    className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-[#222222]"
                    data-testid={`new-take-camera-${number}`}
                >
                    <Camera className="w-3.5 h-3.5" /> Record
                </button>
                <button
                    type="button"
                    onClick={triggerLib}
                    className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-[#222222]"
                    data-testid={`new-take-library-${number}`}
                >
                    <FolderOpen className="w-3.5 h-3.5" /> Library
                    {required && <span className="text-rose-500">*</span>}
                </button>
            </div>
            <input
                ref={inputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) onPick(f, (label || "").trim() || fallback);
                    e.target.value = "";
                    setLabel("");
                }}
            />
            <input
                ref={cameraRef}
                type="file"
                accept="video/*"
                capture="user"
                className="hidden"
                onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) onPick(f, (label || "").trim() || fallback);
                    e.target.value = "";
                    setLabel("");
                }}
            />
        </div>
    );
}

function FeedbackRow({ fb }) {
    const isVoice = fb.type === "voice";
    return (
        <div
            className="bg-white/60 border border-[#eaeaea] rounded-2xl p-5 shadow-[0_1px_2px_rgba(0,0,0,0.02)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.06)]"
            data-testid={`talent-feedback-${fb.id}`}
        >
            <div className="flex items-center justify-between gap-3 mb-3">
                <span className="inline-flex items-center gap-1.5 text-[10px] tracking-[0.2em] uppercase font-mono text-[#333333]">
                    {isVoice ? (
                        <Mic className="w-3 h-3" />
                    ) : (
                        <MessageSquare className="w-3 h-3" />
                    )}
                    {isVoice ? "Voice" : "Text"}
                </span>
                <span className="text-[10px] font-mono text-[#333333]">
                    Received {timeAgo(fb.approved_at || fb.created_at)}
                </span>
            </div>
            {isVoice ? (
                <audio
                    src={fb.content_url}
                    controls
                    className="w-full"
                    data-testid={`talent-feedback-audio-${fb.id}`}
                />
            ) : (
                <p
                    className="text-[13px] leading-relaxed text-[#111111] whitespace-pre-wrap"
                    data-testid={`talent-feedback-text-${fb.id}`}
                >
                    {fb.text}
                </p>
            )}
        </div>
    );
}

function timeAgo(iso) {
    if (!iso) return "";
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return "";
    const diff = (Date.now() - ts) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

class SubmissionErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, errorInfo) {
        console.error("SubmissionPage crashed:", error, errorInfo);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div className="min-h-dvh flex items-center justify-center bg-slate-50 px-4 py-12 sm:px-6 lg:px-8">
                    <div className="max-w-md w-full space-y-8 p-8 bg-white rounded-3xl border border-[#eaeaea] shadow-sm text-center">
                        <div className="w-16 h-16 bg-rose-50 text-rose-600 rounded-full flex items-center justify-center mx-auto mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-8 h-8">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 7.5h.008v.008H12v-.008Z" />
                            </svg>
                        </div>
                        <h2 className="text-xl font-semibold text-slate-950 tracking-tight">Something went wrong</h2>
                        <p className="mt-2 text-sm text-[#333333] leading-relaxed">
                            An unexpected error occurred while loading this page. Don't worry, your progress has not been lost. Please try reloading.
                        </p>
                        <div className="mt-6 flex flex-col gap-2">
                            <button
                                onClick={() => window.location.reload()}
                                className="w-full bg-slate-900 text-white py-3 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] transition-all duration-200"
                            >
                                Reload Page
                            </button>
                            <button
                                onClick={() => {
                                    localStorage.clear();
                                    window.location.reload();
                                }}
                                className="w-full text-[#333333] py-3 rounded-full text-[12px] font-medium hover:text-[#111111] transition-all duration-200"
                            >
                                Clear Cache & Reload
                            </button>
                        </div>
                    </div>
                </div>
            );
        }
        return this.props.children;
    }
}

export default function SubmissionPageWithErrorBoundary(props) {
    return (
        <SubmissionErrorBoundary>
            <SubmissionPage {...props} />
        </SubmissionErrorBoundary>
    );
}
