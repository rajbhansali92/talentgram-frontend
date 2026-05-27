import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api as axios } from "@/lib/api";
import { toast } from "sonner";
import MaterialModal from "@/components/MaterialModal";
import Logo from "@/components/Logo";
import ThemeToggle from "@/components/ThemeToggle";
import { thumbnailUrl } from "@/lib/mediaUtils";
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

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const MAX_IMAGES = 8;
// Phase 3: per-category portfolio image cap. Each of `image`/`indian`/
// `western` is independently capped at this value, NOT combined.
const MAX_IMAGES_PER_CATEGORY = 10;
const LS_KEY = (slug) => `tg_submission_${slug}`;
const LS_DRAFT_KEY = (slug) => `tg_draft_${slug}`;

function readSaved(slug) {
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
    try {
        return JSON.parse(localStorage.getItem(LS_DRAFT_KEY(slug)) || "null");
    } catch {
        return null;
    }
}

export default function SubmissionPage() {
    const { slug } = useParams();
    const [project, setProject] = useState(null);
    const [loading, setLoading] = useState(true);
    const [saved, setSaved] = useState(() => readSaved(slug));
    const [showMaterial, setShowMaterial] = useState(false);

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
            location: "",
            // Phase 2 — schema unification: every talent-facing form writes
            // the SAME shape directly into the talent record. No separate
            // mappings.
            gender: "",
            ethnicity: "",
            instagram_handle: "",
            instagram_followers: "",
            bio: "",
            work_links: [],
            competitive_brand: "",
            availability: { status: "", note: "" },
            budget: { status: "", value: "" },
            commission: "",
            custom_answers: {},
        };
        return draft ? { ...base, ...draft } : base;
    });
    const [starting, setStarting] = useState(false);

    const [submission, setSubmission] = useState(null);
    const [uploading, setUploading] = useState(null);
    const [uploadPct, setUploadPct] = useState(0);
    const [finalizing, setFinalizing] = useState(false);
    const [editMode, setEditMode] = useState(false);

    // Mobile-only 3-step wizard state. Desktop ignores this entirely (the
    // markup is unchanged for `md+`; we just toggle visibility classes for
    // `<md`). Step transitions auto-validate the relevant fields and persist
    // a draft on every advance.
    //   1 = Profile  · 2 = Brief / Questions  · 3 = Uploads
    const [mobileStep, setMobileStep] = useState(1);

    // Collapsible sections state
    const [collapsedSections, setCollapsedSections] = useState({
        profile: false,           // open by default
        projectQuestions: false,   // open by default
        workLinks: false,          // open by default
        uploads: false,            // open by default
    });
    // Upload retry queue: per-slot pending file with attempt counter so a
    // transient network drop doesn't lose the file selection.
    const [retryQueue, setRetryQueue] = useState({}); // { slotKey: { file, category, label, attempt } }

    // Email-first gate: hides every form section EXCEPT the email field
    // until the talent's email has been blurred and the prefill response
    // is processed (Use this / Edit manually / no match).
    // Initialised here (rather than later in the component body) so
    // validateForm / validateStep1 can read it without TDZ surprises.
    const [emailGateUnlocked, setEmailGateUnlocked] = useState(() => !!saved);
    const [prefillTried, setPrefillTried] = useState(false);
    const [prefillSuggestion, setPrefillSuggestion] = useState(null); // {data}
    const [prefillEmail, setPrefillEmail] = useState("");

    const introRef = useRef();
    const take1Ref = useRef();
    const newTakeRef = useRef();
    const imagesRef = useRef();
    const cameraImagesRef = useRef(); // mobile camera-first photo capture
    const indianImagesRef = useRef();
    const westernImagesRef = useRef();

    // Load project
    useEffect(() => {
        (async () => {
            try {
                const { data } = await axios.get(
                    `${API}/public/projects/${slug}`,
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
                    `${API}/public/submissions/${saved.id}`,
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
        if (!form.location.trim()) return "Current location is required";
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
        if (!form.location.trim()) return "Current location is required";
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

    // Auto-scroll to top whenever step changes so the user always sees the
    // step heading first instead of mid-form.
    useEffect(() => {
        if (typeof window !== "undefined") {
            window.scrollTo({ top: 0, behavior: "smooth" });
        }
    }, [mobileStep]);

    // Persist a debounced draft of the form so a refresh / app-switch never
    // loses progress before the talent record is created on the backend.
    useEffect(() => {
        const t = setTimeout(() => {
            try {
                localStorage.setItem(LS_DRAFT_KEY(slug), JSON.stringify(form));
            } catch (e) { console.error(e); }
        }, 400);
        return () => clearTimeout(t);
    }, [form, slug]);

    // Auto-jump to step 3 once we have a backend submission record (i.e.
    // the talent has finished steps 1+2). Keeps mobile users from
    // re-seeing the profile step on a return visit.
    useEffect(() => {
        if (saved && mobileStep < 3) setMobileStep(3);
    // `mobileStep` and `setMobileStep` are intentionally omitted: this
    // effect is meant to fire ONLY when `saved` transitions (e.g. talent
    // record just got created). Including `mobileStep` would re-trigger
    // every time the user manually navigates back to step 1/2 via
    // `goToStep` and force them forward again — the exact UX bug we
    // designed this guard to avoid.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [saved]);

    const goToStep = async (n) => {
        if (n === mobileStep) return;
        // Forward navigation must validate; backward is free.
        if (n > mobileStep) {
            if (mobileStep === 1) {
                const err = validateStep1();
                if (err) {
                    toast.error(err);
                    return;
                }
                // Persist draft on every advance so a refresh / app-switch
                // never loses progress (combined with backend upsert).
                // (`saveForm` / `startSubmissionDirect` are declared later
                // in the component body — safe here because goToStep only
                // fires from button clicks AFTER render. See validateForm.)
                await saveForm();
            } else if (mobileStep === 2) {
                const err = validateStep2();
                if (err) {
                    toast.error(err);
                    return;
                }
                if (!saved) {
                    // First time crossing from step 2 → 3 finalises the
                    // talent-details and creates the submission shell.
                    const ok = await startSubmissionDirect();
                    if (!ok) return;
                } else {
                    await saveForm();
                }
            }
        }
        setMobileStep(n);
    };
    // Convenience wrapper that returns a boolean (vs the form-handler version).
    // Function declaration (not `const = async () =>`) so it's fully hoisted
    // and `goToStep` above can call it without a TDZ reference.
    async function startSubmissionDirect() {
        const err = validateForm();
        if (err) {
            toast.error(err);
            return false;
        }
        setStarting(true);
        try {
            const { data } = await axios.post(
                `${API}/public/projects/${slug}/submission`,
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
                    custom_answers: form.custom_answers,
                    commission_percent: form.commission || null,
                },
            );
            const next = { id: data.id, token: data.token };
            localStorage.setItem(LS_KEY(slug), JSON.stringify(next));
            setSaved(next);
            setSubmission(data);
            toast.success("Profile saved — let's add your audition takes");
            return true;
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Could not save profile");
            return false;
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
    const tryPrefill = async () => {
        if (saved) return; // submission already started — too late
        const email = (form.email || "").trim().toLowerCase();
        if (!email || !email.includes("@")) return;
        if (email === prefillEmail && prefillTried) return; // already tried
        setPrefillEmail(email);
        setPrefillTried(true);
        try {
            const { data } = await axios.get(
                `${API}/public/prefill?email=${encodeURIComponent(email)}`,
            );
            if (!data || !data.first_name) {
                // New talent — quietly unlock the rest of the form.
                setPrefillSuggestion(null);
                setEmailGateUnlocked(true);
                return;
            }
            // Surface the prompt; rest of the form stays hidden until the
            // user picks Use/Edit so we never silently overwrite.
            setPrefillSuggestion({ data });
        } catch {
            // 429 (rate-limited) or network — fail silently AND unlock so
            // the user isn't blocked behind a transient network error.
            setEmailGateUnlocked(true);
        }
    };

    // "Use this" — apply the suggested fields, but ONLY where the user
    // hasn't already typed something. Never touches media or rich answers.
    const applyPrefill = () => {
        const data = prefillSuggestion?.data;
        if (!data) return;
        setForm((f) => ({
            ...f,
            first_name: f.first_name || data.first_name || "",
            last_name: f.last_name || data.last_name || "",
            phone: f.phone || data.phone || "",
            age: f.age || (data.age != null ? String(data.age) : ""),
            dob: f.dob || data.dob || "",
            height: f.height || data.height || "",
            location: f.location || data.location || "",
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
        }));
        setPrefillSuggestion(null);
        setEmailGateUnlocked(true);
        toast.success(`Welcome back, ${data.first_name}`);
    };

    const dismissPrefill = () => {
        setPrefillSuggestion(null);
        setEmailGateUnlocked(true);
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
                    competitive_brand: project.competitive_brand_enabled
                        ? form.competitive_brand
                        : "",
                    availability: form.availability,
                    budget: form.budget,
                    custom_answers: form.custom_answers || {},
                },
            };
            const { data } = await axios.post(
                `${API}/public/projects/${slug}/submission`,
                payload,
            );
            const ref = { id: data.id, token: data.token };
            localStorage.setItem(LS_KEY(slug), JSON.stringify(ref));
            setSaved(ref);
            toast.success("Details saved. Now upload your audition.");
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
                `${API}/public/submissions/${saved.id}`,
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

    const uploadFile = async (file, category, label = null) => {
        // Client-side guard mirrors backend cap (200 MB videos / 20 MB images) (P5)
        const isVideoSlot = ["intro_video", "take", "take_1", "take_2", "take_3"].includes(category);
        const CAP_MB = isVideoSlot ? 200 : 20;
        if (file) {
            const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
            if (isVideoSlot) {
                if (file.size > CAP_MB * 1024 * 1024) {
                    toast.error(`Video is too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
                    return;
                }
                const allowedVideoExts = ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.3gp'];
                if (!allowedVideoExts.includes(ext) && !file.type.startsWith('video/')) {
                    toast.error(`Unsupported video format. Please upload MP4, MOV, or WEBM.`);
                    return;
                }
            } else {
                if (file.size > CAP_MB * 1024 * 1024) {
                    toast.error(`Image too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
                    return;
                }
                if (['.bmp', '.tiff', '.heic', '.heif'].includes(ext) || ['image/bmp', 'image/tiff', 'image/heic', 'image/heif'].includes(file.type)) {
                    toast.error(`HEIC, BMP, and TIFF formats are not supported. Please upload JPEG or PNG.`);
                    return;
                }
                if (!file.type.startsWith('image/') && !['.jpg', '.jpeg', '.png', '.webp'].includes(ext)) {
                    toast.error(`Unsupported image format. Please upload JPG, PNG, or WEBP.`);
                    return;
                }
            }
        }
        const slotKey = label ? `${category}:${label}` : category;
        setUploading(slotKey);
        setUploadPct(0);
        // Persist the pending file in retryQueue so reload + retry button work.
        setRetryQueue((q) => ({ ...q, [slotKey]: { category, label, attempt: 0, fileName: file.name, fileSize: file.size } }));

        // Auto-retry loop with exponential backoff (3 attempts, 1s/2s/4s).
        // Solves transient mobile-network disconnects without backend changes.
        // True resumable-from-offset upload is a P1 follow-up.
        const MAX_ATTEMPTS = 3;
        let lastErr = null;
        for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("category", category);
                if (label) fd.append("label", label);
                const { data } = await axios.post(
                    `${API}/public/submissions/${saved.id}/upload`,
                    fd,
                    {
                        ...authCfg,
                        headers: {
                            ...authCfg.headers,
                            "Content-Type": "multipart/form-data",
                        },
                        timeout: 0, // no per-request timeout — let mobile networks breathe
                        onUploadProgress: (e) => {
                            if (e.total) setUploadPct(Math.round((e.loaded / e.total) * 100));
                        },
                    },
                );
                setSubmission(data);
                setRetryQueue((q) => {
                    const n = { ...q }; delete n[slotKey]; return n;
                });
                setUploading(null);
                setUploadPct(0);
                if (attempt > 1) toast.success(`Recovered after ${attempt} attempts`);
                return;
            } catch (err) {
                lastErr = err;
                const isNetwork = !err?.response; // axios sets `response` on HTTP errors only
                if (!isNetwork || attempt === MAX_ATTEMPTS) break;
                // Exponential backoff: 1s, 2s, 4s
                const wait = 1000 * Math.pow(2, attempt - 1);
                toast.message(`Network blip — retrying in ${wait / 1000}s (attempt ${attempt}/${MAX_ATTEMPTS})`);
                setRetryQueue((q) => ({
                    ...q,
                    [slotKey]: { ...(q[slotKey] || {}), attempt },
                }));
                await new Promise((r) => setTimeout(r, wait));
            }
        }
        // Failed after retries — keep entry in retryQueue so user can tap "Retry".
        setRetryQueue((q) => ({
            ...q,
            [slotKey]: { ...(q[slotKey] || {}), failed: true, error: lastErr?.response?.data?.detail || "Upload failed", file },
        }));
        setUploading(null);
        setUploadPct(0);
        toast.error(lastErr?.response?.data?.detail || "Upload failed — tap Retry to try again");
    };

    // Manual retry for a slot whose auto-retries exhausted.
    const retryUpload = async (slotKey) => {
        const entry = retryQueue[slotKey];
        if (!entry?.file) {
            toast.error("Re-select the file to retry");
            return;
        }
        await uploadFile(entry.file, entry.category, entry.label);
    };

    const patchTakeLabel = async (mid, label) => {
        try {
            const { data } = await axios.patch(
                `${API}/public/submissions/${saved.id}/media/${mid}`,
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
        setUploading(imageCategory);
        setUploadPct(0);
        try {
            let last = null;
            const totalFiles = accepted.length;
            for (let i = 0; i < accepted.length; i++) {
                const f = accepted[i];
                const fd = new FormData();
                fd.append("file", f);
                fd.append("category", imageCategory);
                const { data } = await axios.post(
                    `${API}/public/submissions/${saved.id}/upload`,
                    fd,
                    {
                        ...authCfg,
                        headers: {
                            ...authCfg.headers,
                            "Content-Type": "multipart/form-data",
                        },
                        onUploadProgress: (e) => {
                            if (e.total) {
                                const thisPct = (e.loaded / e.total) * 100;
                                const overall = (i * 100 + thisPct) / totalFiles;
                                setUploadPct(Math.round(overall));
                            }
                        },
                    },
                );
                last = data;
            }
            if (last) setSubmission(last);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
            setUploadPct(0);
        }
    };

    const removeMedia = async (mid) => {
        try {
            await axios.delete(
                `${API}/public/submissions/${saved.id}/media/${mid}`,
                authCfg,
            );
            const { data } = await axios.get(
                `${API}/public/submissions/${saved.id}`,
                authCfg,
            );
            setSubmission(data);
        } catch {
            toast.error("Could not remove file");
        }
    };

    const finalize = async () => {
        await saveForm();
        setFinalizing(true);
        try {
            await axios.post(
                `${API}/public/submissions/${saved.id}/finalize`,
                {},
                authCfg,
            );
            const { data } = await axios.get(
                `${API}/public/submissions/${saved.id}`,
                authCfg,
            );
            setSubmission(data);
            // Once the user finalises, clear the local draft — the
            // canonical state lives on the backend now.
            try { localStorage.removeItem(LS_DRAFT_KEY(slug)); } catch (e) { console.error(e); }
            toast.success("Submitted — the team will review soon");
        } catch (err) {
            toast.error(
                err?.response?.data?.detail || "Please complete all required fields",
            );
        } finally {
            setFinalizing(false);
        }
    };

    // ---------------------------------------------------------------
    if (loading) {
        return (
            <div className="min-h-dvh flex items-center justify-center bg-gradient-to-b from-slate-50 to-white">
                <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
            </div>
        );
    }
    if (!project) {
        return (
            <div className="min-h-dvh flex items-center justify-center bg-gradient-to-b from-slate-50 to-white text-slate-500 p-6 text-center">
                <p>Project not found.</p>
            </div>
        );
    }

    const intro = media.find((m) => m.category === "intro_video");
    // Renamable takes: new `take` category + legacy `take_1/2/3` (auto-labelled)
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
    const MAX_TAKES = 5;
    const canAddTake = takes.length < MAX_TAKES;
    const isSubmitted =
        submission?.status === "submitted" || submission?.status === "updated";

    const readyToSubmit =
        // Phase 1 v37c: media is no longer mandatory. Submission requires
        // ONLY the form-data identity / availability / budget fields. Intro
        // video, audition takes, and portfolio images are recommended but
        // optional — talents who are pressed for time can ship a "form-only"
        // submission and add media later via Refine.
        form.first_name &&
        form.last_name &&
        form.height &&
        form.location &&
        form.availability.status &&
        (form.availability.status !== "no" ||
            form.availability.note.trim()) &&
        form.budget.status &&
        (form.budget.status !== "custom" || form.budget.value.trim());

    // Specific, actionable checklist of what's still missing — shown under
    // the Submit button so talents never guess why it's disabled. Media
    // entries are intentionally excluded (they are recommended, not required).
    const missing = [];
    if (!form.first_name) missing.push("First name");
    if (!form.last_name) missing.push("Last name");
    if (!form.height) missing.push("Height");
    if (!form.location) missing.push("Current location");
    if (!form.availability.status) missing.push("Availability (Yes / No)");
    else if (form.availability.status === "no" && !form.availability.note.trim())
        missing.push("Availability note");
    if (!form.budget.status) missing.push("Budget (Accept / Custom)");
    else if (form.budget.status === "custom" && !form.budget.value.trim())
        missing.push("Budget amount");

    // ---------------------------------------------------------------
    // SUBMITTED / UPDATED state — offer a "Refine my submission" path
    if (isSubmitted && !editMode) {
        const statusLabel =
            submission?.status === "updated" ? "Resubmitted" : "Submitted";
        const feedback = submission?.client_feedback || [];
        return (
            <div className="min-h-dvh bg-gradient-to-b from-slate-50 via-white to-slate-50/30 text-slate-900 relative overflow-hidden">
                <div className="absolute inset-0 pointer-events-none opacity-20 blur-3xl bg-amber-200/20" />
                <div className="absolute top-5 right-5 z-10">
                    <ThemeToggle />
                </div>
                <div className="max-w-xl mx-auto px-6 py-16 md:py-24 tg-fade-up">
                    <div className="bg-white/80 backdrop-blur-sm rounded-3xl p-10 border border-slate-200/60 shadow-[0_20px_40px_-12px_rgba(0,0,0,0.05)] text-center">
                        <div className="relative w-20 h-20 mx-auto mb-8">
                            <div className="absolute inset-0 rounded-full bg-emerald-100/60 blur-xl animate-pulse" />
                            <div className="relative w-full h-full rounded-full bg-emerald-50 border border-emerald-100 flex items-center justify-center shadow-sm">
                                <Check className="w-8 h-8 text-emerald-600" />
                            </div>
                        </div>
                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-slate-400 mb-4">{statusLabel}</p>
                        <h1 className="font-display text-4xl md:text-5xl tracking-tight text-slate-900 mb-6 leading-[1.05]">
                            Thank you,{" "}
                            <span className="text-slate-700">{form.first_name || submission.talent_name?.split(" ")[0]}</span>.
                        </h1>
                        <p className="text-[13px] leading-relaxed text-slate-500 mb-10 max-w-md mx-auto">
                            Your audition for{" "}
                            <span className="font-medium text-slate-800">
                                {project.brand_name}
                            </span>{" "}
                            has been received. The Talentgram team will review and
                            reach out if you're shortlisted.
                        </p>
                        <button
                            type="button"
                            onClick={() => setEditMode(true)}
                            data-testid="refine-submission-btn"
                            className="text-[11px] font-mono text-slate-400 hover:text-slate-600 underline underline-offset-4 transition-colors"
                        >
                            Want to refine or replace a take? Update your submission →
                        </button>
                    </div>

                    {/* Client Feedback inbox — only approved+shared rows ever appear
                        here. The relay is mediated by the team, so notes the talent
                        sees have been reviewed. Order is approval-time ascending. */}
                    <section
                        className="mt-16"
                        data-testid="talent-feedback-section"
                    >
                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-slate-400 mb-4">Client Feedback</p>
                        {feedback.length === 0 ? (
                            <div
                                className="bg-white/40 rounded-2xl p-6 text-[13px] leading-relaxed text-slate-400 border border-slate-200/60"
                                data-testid="talent-feedback-empty"
                            >
                                No feedback yet — the team will share notes here
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
        <div className="min-h-dvh bg-gradient-to-b from-slate-50 via-white to-slate-50/30 text-slate-900 relative overflow-hidden" data-testid="submission-page" data-mobile-step={mobileStep}>
            {/* Ambient luxury background blobs */}
            <div className="absolute inset-0 pointer-events-none opacity-30 blur-3xl">
                <div className="absolute top-0 -left-40 w-80 h-80 rounded-full bg-amber-200/40 mix-blend-multiply animate-blob" />
                <div className="absolute bottom-0 -right-40 w-80 h-80 rounded-full bg-slate-200/40 mix-blend-multiply animate-blob animation-delay-2000" />
            </div>

            <header className="sticky top-0 z-30 bg-white/80 backdrop-blur-xl border-b border-slate-200/60 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                <div className="max-w-3xl mx-auto px-5 py-5 flex items-center justify-between">
                    <Logo size="sm" />
                    <ThemeToggle size="sm" />
                </div>
                {/* Mobile-only 3-step indicator. Desktop renders the full
                    form vertically (this bar is hidden via md:hidden).
                    Email-first gate: hidden until the talent has tabbed
                    out of the email field and chosen Use/Edit (or no match). */}
                {emailGateUnlocked && (
                <div
                    className="md:hidden bg-white/60 backdrop-blur-sm border-t border-slate-200/40"
                    data-testid="wizard-stepbar"
                >
                    <div className="max-w-3xl mx-auto px-5 py-4 flex items-center gap-3">
                        {[1, 2, 3].map((n, i) => {
                            const labels = ["Profile", "Brief", "Uploads"];
                            const reached = mobileStep >= n;
                            const active = mobileStep === n;
                            return (
                                <button
                                    key={n}
                                    type="button"
                                    onClick={() => goToStep(n)}
                                    data-testid={`wizard-step-${n}`}
                                    className={`flex-1 flex items-center gap-2 py-1 text-left transition-all active:scale-[0.97] ${active ? "opacity-100" : reached ? "opacity-80" : "opacity-50"}`}
                                >
                                    <span
                                        className={`w-6 h-6 rounded-full inline-flex items-center justify-center text-[10px] font-mono shrink-0 ${active ? "bg-slate-900 text-white" : reached ? "bg-slate-200 text-slate-700 border border-slate-300" : "border border-slate-300 text-slate-500"}`}
                                    >
                                        {reached && !active ? <Check className="w-3 h-3" /> : n}
                                    </span>
                                    <span className={`text-[11px] tracking-wider uppercase font-medium ${active ? "text-slate-900" : "text-slate-500"}`}>
                                        {labels[i]}
                                    </span>
                                </button>
                            );
                        })}
                    </div>
                    <div className="h-0.5 bg-slate-200/60">
                        <div
                            className="h-full bg-slate-800 transition-all duration-300"
                            style={{ width: `${(mobileStep - 1) * 50}%` }}
                            data-testid="wizard-progress-bar"
                        />
                    </div>
                </div>
                )}
            </header>

            <div data-testid="submission-content" className="max-w-2xl mx-auto px-4 sm:px-6 md:px-8 py-8 md:py-16">
                {/* SECTION 1 — Project Info */}
                <section className="mb-10 sm:mb-16 bg-white/60 rounded-3xl p-5 sm:p-7 border border-slate-200/60 shadow-[0_4px_20px_rgba(15,23,42,0.04)] bg-[radial-gradient(circle_at_top,rgba(251,191,36,0.05),transparent_60%)]" data-testid="project-info-section" data-step="1">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mb-4">Audition Brief</p>
                    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-8 border-b border-slate-100 pb-4">
                        <h1 className="font-display text-2xl sm:text-3xl md:text-4xl tracking-tight text-slate-900 leading-[1.05]">
                            Talentgram × {project.brand_name}
                        </h1>
                        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-50/60 border border-emerald-100/50 text-emerald-700 text-[11px] font-mono shadow-[0_1px_2px_rgba(0,0,0,0.02)] self-start sm:self-auto">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                            <span>Draft Auto-Saved</span>
                        </div>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-5 border-t border-slate-200/50 pt-6">
                        <Info label="Character" value={project.character} />
                        <Info label="Shoot Dates" value={project.shoot_dates} />
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
                            <p className="text-[11px] text-slate-400 tracking-[0.2em] uppercase font-mono mb-2">
                                Additional Details
                            </p>
                            <p className="text-[13px] leading-relaxed text-slate-600 whitespace-pre-line">
                                {project.additional_details}
                            </p>
                        </div>
                    )}
                    {((project.materials || []).length > 0 ||
                        (project.video_links || []).length > 0) && (
                        <button
                            onClick={() => setShowMaterial(true)}
                            data-testid="view-audition-material-btn"
                            className="inline-flex items-center gap-2 px-5 py-2.5 mt-6 border border-slate-200 hover:border-slate-300 rounded-full text-[13px] text-slate-700 transition-all hover:shadow-sm hover:-translate-y-[1px] bg-white/40"
                        >
                            <FolderOpen className="w-4 h-4" /> View Audition Material
                        </button>
                    )}
                </section>

                {/* SECTION 2 — TALENT DETAILS FORM */}
                <section
                    className="pt-4 mb-10 sm:mb-16"
                    data-testid="talent-details-section"
                    data-step="1-2"
                >
                    <div className="bg-white rounded-3xl p-7 border border-slate-200/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)]">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mb-4" data-step="1">Talent Details</p>
                    <h2 className="font-display text-2xl md:text-3xl tracking-tight text-slate-900 mb-3 leading-[1.05]" data-step="1">
                        Your profile.
                    </h2>
                    <p className="text-[13px] leading-relaxed text-slate-500 mb-10" data-step="1">
                        All fields are required unless marked optional.
                    </p>

                    <form onSubmit={startSubmission} className="space-y-8">
                        {/* Phase 1 — email-first identity. The email field
                            anchors the form so we can prefill known talents
                            BEFORE they retype everything. */}
                        <div data-step="1">
                            <PremiumFormField
                                label="Email *"
                                type="email"
                                value={form.email}
                                onChange={(v) => {
                                    setForm({ ...form, email: v });
                                    // Re-arm the gate if the user is editing
                                    // an already-tried email — they should
                                    // be able to fix a typo without being
                                    // locked into a stale prefill state.
                                    if (
                                        !saved &&
                                        v.trim().toLowerCase() !== prefillEmail
                                    ) {
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
                            <p className="text-[11px] text-slate-400 mt-3 font-mono">
                                We use email to recognise you.{" "}
                                {emailGateUnlocked
                                    ? "Returning talents see saved details below."
                                    : "Tab out of the email field to continue."}
                            </p>
                        </div>

                        {/* Prefill suggestion card — only shown when an
                            approved talent record matches the email. The
                            user is in control: Use this OR Edit manually. */}
                        {prefillSuggestion && !saved && (
                            <div
                                className="bg-amber-50/40 border border-amber-200/60 rounded-2xl p-5 flex flex-col md:flex-row md:items-center gap-4 justify-between"
                                data-testid="prefill-suggestion-card"
                                data-step="1"
                            >
                                <div className="min-w-0 flex-1">
                                    <p className="text-[13px] text-slate-800">
                                        We found your profile.{" "}
                                        <span className="text-slate-500">
                                            Use saved details?
                                        </span>
                                    </p>
                                    <p className="text-[11px] text-slate-400 font-mono mt-1 truncate">
                                        {prefillSuggestion.data.first_name}{" "}
                                        {prefillSuggestion.data.last_name || ""}
                                        {prefillSuggestion.data.location ? ` · ${prefillSuggestion.data.location}` : ""}
                                        {prefillSuggestion.data.height ? ` · ${prefillSuggestion.data.height}` : ""}
                                    </p>
                                </div>
                                <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 w-full md:w-auto shrink-0">
                                    <button
                                        type="button"
                                        onClick={applyPrefill}
                                        data-testid="prefill-use-btn"
                                        className="bg-slate-900 text-white px-5 py-2.5 text-[12px] rounded-full hover:bg-slate-800 hover:-translate-y-[1px] hover:shadow-md inline-flex items-center justify-center gap-1.5 min-h-[44px] active:scale-[0.97] transition-all duration-200 w-full sm:w-auto"
                                    >
                                        <Check className="w-3.5 h-3.5" />
                                        Use this
                                    </button>
                                    <button
                                        type="button"
                                        onClick={dismissPrefill}
                                        data-testid="prefill-dismiss-btn"
                                        className="border border-slate-200 text-slate-600 hover:border-slate-300 hover:-translate-y-[1px] px-5 py-2.5 text-[12px] rounded-full inline-flex items-center justify-center gap-1.5 min-h-[44px] active:scale-[0.97] transition-all duration-200 bg-white/50 w-full sm:w-auto"
                                    >
                                        Edit manually
                                    </button>
                                </div>
                            </div>
                        )}

                        {emailGateUnlocked && (
                        <>
                        {/* Section 1: Your Profile */}
                        <div className="bg-slate-50/40 rounded-2xl border border-slate-200/50 p-6">
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-slate-200/30">
                                <h3 className="text-sm font-semibold text-slate-800 tracking-wide">Your Profile</h3>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            profile: !prev.profile,
                                        }))
                                    }
                                    className="p-1 border border-slate-200 hover:border-slate-300 hover:bg-slate-50 rounded-full text-slate-500 transition-all duration-200"
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
                                            onChange={(v) =>
                                                setForm({ ...form, first_name: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-first-name"
                                            required
                                        />
                                        <PremiumFormField
                                            label="Last Name *"
                                            value={form.last_name}
                                            onChange={(v) =>
                                                setForm({ ...form, last_name: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-last-name"
                                            required
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
                                            label="Date of Birth (optional)"
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
                                        />
                                        
                                        {/* Project-specific age override checkbox and input */}
                                        <div className="mt-4 p-5 rounded-2xl bg-slate-50/50 border border-slate-200/50 focus-within:border-amber-300 focus-within:ring-4 focus-within:ring-amber-50/50 transition-all duration-300 col-span-1 md:col-span-2">
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
                                                    className="w-5 h-5 rounded border-slate-300 text-amber-600 focus:ring-amber-500 focus:ring-2 cursor-pointer transition duration-150 ease-in-out"
                                                />
                                                <span className="text-sm font-medium text-slate-700 select-none">
                                                    Use different age for this project?
                                                </span>
                                            </label>
                                            
                                            {form.overrideAge && (
                                                <div className="mt-4 animate-fadeIn transition-all duration-300">
                                                    <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                        className="mt-2 w-full bg-white rounded-xl border border-slate-200 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-3 px-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                    />
                                                    <p className="text-[10px] text-slate-400 font-mono mt-1.5">
                                                        This age override is isolated to this submission only and will not change your global profile.
                                                    </p>
                                                </div>
                                            )}
                                        </div>

                                        <div data-testid="form-age-field">
                                            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                className="mt-2 w-full bg-slate-100 rounded-2xl border border-slate-200 outline-none py-3 px-4 text-[15px] text-slate-500 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                            />
                                        </div>
                                        <div data-testid="form-height-field">
                                            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                        className="bg-white/60 border border-slate-200 rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-slate-700 transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select height" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-slate-200 rounded-2xl">
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
                                        </div>
                                        <PremiumFormField
                                            label="Current Location *"
                                            value={form.location}
                                            onChange={(v) =>
                                                setForm({ ...form, location: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-location"
                                            required
                                        />
                                        {project.competitive_brand_enabled && (
                                            <PremiumFormField
                                                label="Competitive Brand (declare conflicts)"
                                                value={form.competitive_brand}
                                                onChange={(v) =>
                                                    setForm({
                                                        ...form,
                                                        competitive_brand: v,
                                                    })
                                                }
                                                onBlur={saveForm}
                                                placeholder="Any brand conflict? Type 'None' if not"
                                                testid="form-competitive-brand"
                                                wide
                                            />
                                        )}
                                    </div>

                                    {/* Phase 2 — unified identity fields */}
                                    <div
                                        className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8"
                                        data-step="1"
                                        data-testid="unified-identity-block"
                                    >
                                        <div data-testid="form-gender-field">
                                            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                                    : "bg-white/60 border-slate-200 hover:border-slate-300 text-slate-600"
                                                            }`}
                                                        >
                                                            {g.label}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                        <div data-testid="form-ethnicity-field">
                                            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                        className="bg-white/60 border border-slate-200 rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-slate-700 transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select ethnicity" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-slate-200 rounded-2xl">
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
                                            onChange={(v) =>
                                                setForm({ ...form, instagram_handle: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-instagram-handle"
                                            placeholder="@yourhandle"
                                        />
                                        <div data-testid="form-instagram-followers-field">
                                            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                        className="bg-white/60 border border-slate-200 rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-slate-700 transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select range" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-slate-200 rounded-2xl">
                                                        {FOLLOWER_TIERS.map((tier) => (
                                                            <SelectGroup key={tier.label}>
                                                                <SelectLabel className="text-[10px] tracking-wide uppercase text-slate-400 font-mono">
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
                                        <label className="block md:col-span-2" data-testid="form-bio-field">
                                            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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
                                                className="mt-2 w-full bg-white/60 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-3 px-4 text-[16px] md:text-[15px] resize-none transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                placeholder="A short note about you (max 600 chars)"
                                            />
                                        </label>
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Section 2: Project Questions */}
                        <div data-step="2" className="bg-slate-50/40 rounded-2xl border border-slate-200/50 p-6">
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-slate-200/30">
                                <h3 className="text-sm font-semibold text-slate-800 tracking-wide">Project Questions</h3>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            projectQuestions: !prev.projectQuestions,
                                        }))
                                    }
                                    className="p-1 border border-slate-200 hover:border-slate-300 hover:bg-slate-50 rounded-full text-slate-500 transition-all duration-200"
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
                                    >
                                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mb-3">
                                            Availability{" "}
                                            <span className="text-rose-500">*</span>
                                        </p>
                                        {project.shoot_dates && (
                                            <p className="text-[12px] text-slate-500 mb-5 leading-relaxed">
                                                {project.shoot_dates}
                                                {" — "}Costume trial and rehearsal dates
                                                (if any) will be informed.
                                            </p>
                                        )}
                                        <div className="grid grid-cols-2 gap-2 mb-4">
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
                                                        className={`px-4 py-3.5 rounded-full text-[13px] border transition-all duration-200 min-h-[52px] ${
                                                            active
                                                                ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                                : "bg-white/60 border-slate-200 hover:border-slate-300 text-slate-600"
                                                        }`}
                                                    >
                                                        {opt.label}
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
                                                className="w-full bg-white/60 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-3 px-4 text-[16px] md:text-[13px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                            />
                                        )}
                                    </div>

                                    {/* BUDGET — decision block */}
                                    <div
                                        data-testid="budget-block"
                                        data-step="2"
                                    >
                                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mb-5">
                                            Budget{" "}
                                            <span className="text-rose-500">*</span>
                                        </p>
                                        {project.commission_percent && (
                                            <div
                                                className="flex items-center justify-between bg-white/60 border border-slate-200/80 rounded-2xl px-5 py-4 mb-6 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                data-testid="commission-card"
                                            >
                                                <span className="text-[11px] tracking-[0.2em] uppercase font-mono text-slate-500">
                                                    Commission
                                                </span>
                                                <span className="font-display text-2xl text-slate-800">
                                                    {project.commission_percent}
                                                </span>
                                            </div>
                                        )}
                                        {(project.talent_budget || []).length > 0 && (
                                            <div
                                                className="bg-white/60 border border-slate-200/80 rounded-2xl px-5 py-4 mb-6 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                data-testid="talent-budget-hint"
                                            >
                                                <div className="text-[10px] tracking-[0.2em] uppercase font-mono text-slate-400 mb-3">
                                                    Offered Budget
                                                </div>
                                                <div className="space-y-2">
                                                    {project.talent_budget.map((row, i) => (
                                                        <div
                                                            key={`${row.label || ""}-${i}`}
                                                            className="flex items-center justify-between gap-3 text-[13px]"
                                                            data-testid={`talent-budget-line-${i}`}
                                                        >
                                                            <span className="text-slate-500">
                                                                {row.label || "—"}
                                                            </span>
                                                            <span className="font-mono text-slate-700">
                                                                {row.value || "—"}
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
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
                                                className={`px-4 py-3.5 rounded-full text-[13px] border transition-all duration-200 min-h-[52px] ${
                                                    form.budget.status === "accept"
                                                        ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                        : "bg-white/60 border-slate-200 hover:border-slate-300 text-slate-600"
                                                }`}
                                            >
                                                Accept
                                                {project.budget_per_day && (
                                                    <span className="block text-[11px] font-mono opacity-70 mt-1">
                                                        {project.budget_per_day} / day
                                                    </span>
                                                )}
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
                                                className={`px-4 py-3.5 rounded-full text-[13px] border transition-all duration-200 min-h-[52px] ${
                                                    form.budget.status === "custom"
                                                        ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                        : "bg-white/60 border-slate-200 hover:border-slate-300 text-slate-600"
                                                }`}
                                            >
                                                Not accepting
                                                <span className="block text-[11px] font-mono opacity-70 mt-1">
                                                    Propose your own
                                                </span>
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
                                                className="w-full bg-white/60 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-3 px-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                            />
                                        )}
                                    </div>

                                    {project.medium_usage && (
                                        <div className="border-t border-slate-100 pt-8" data-step="2">
                                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mb-4">Medium / Usage</p>
                                            <p className="text-[13px] leading-relaxed text-slate-600">
                                                {project.medium_usage}
                                            </p>
                                        </div>
                                    )}

                                    {(project.custom_questions || []).length > 0 && (
                                        <div className="border-t border-slate-100 pt-8 space-y-6" data-step="2">
                                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70">Additional Questions</p>
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
                        <div data-step="2" className="bg-slate-50/40 rounded-2xl border border-slate-200/50 p-6">
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-slate-200/30">
                                <h3 className="text-sm font-semibold text-slate-800 tracking-wide">Work Links</h3>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            workLinks: !prev.workLinks,
                                        }))
                                    }
                                    className="p-1 border border-slate-200 hover:border-slate-300 hover:bg-slate-50 rounded-full text-slate-500 transition-all duration-200"
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
                                        <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
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

                {/* SECTION 3 — UPLOADS (gated on saved + email-first gate) */}
                {emailGateUnlocked && saved && (
                    <section
                        className="pt-4"
                        data-testid="uploads-section"
                        data-step="3"
                    >
                        <div className="bg-white rounded-3xl p-7 border border-slate-200/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)]">
                        <div className="flex items-center justify-between mb-4 pb-2 border-b border-slate-200/30">
                            <div>
                                <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mb-1">Uploads</p>
                                <h2 className="font-display text-2xl tracking-tight text-slate-900 leading-[1.05]">
                                    Show us your work.
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
                                className="p-1 border border-slate-200 hover:border-slate-300 hover:bg-slate-50 rounded-full text-slate-500 transition-all duration-200"
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
                                <p
                                    className="text-[12px] text-slate-500 mb-10 font-mono"
                                    data-testid="uploads-optional-hint"
                                >
                                    Optional — but recommended to increase your selection chances.
                                </p>

                                <PremiumUploadSlot
                                    title="Introduction Video"
                                    note="Optional (recommended). Your most recent professional introduction video (without contact info)."
                                    icon={Video}
                                    accept="video/*"
                                    inputRef={introRef}
                                    onPick={(f) => uploadFile(f[0], "intro_video")}
                                    uploading={uploading === "intro_video"}
                                    uploadPct={uploadPct}
                                    media={intro}
                                    onRemove={(m) => removeMedia(m.id)}
                                    testid="upload-intro"
                                    cameraCapture="user"
                                    failed={Boolean(retryQueue["intro_video"]?.failed)}
                                    onRetry={() => retryUpload("intro_video")}
                                />

                                <div className="mb-10" data-testid="takes-section">
                                    <div className="flex items-center justify-between mb-4">
                                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70">
                                            Audition Takes{" "}
                                            <span className="text-slate-400">
                                                (up to {MAX_TAKES})
                                            </span>
                                        </p>
                                        <span
                                            className="text-[11px] font-mono text-slate-400"
                                            data-testid="takes-counter"
                                        >
                                            {takes.length}/{MAX_TAKES}
                                        </span>
                                    </div>
                                    <p className="text-[12px] leading-relaxed text-slate-500 mb-6">
                                        Optional (recommended). Upload each take as a
                                        separate video and label it (e.g., "Scene 1",
                                        "Closeup emotional"). Talents with takes have
                                        a stronger chance of selection.
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
                                        />
                                    ))}

                                    {canAddTake && (
                                        <PremiumAddTakeSlot
                                            number={takes.length + 1}
                                            uploading={uploading}
                                            uploadPct={uploadPct}
                                            onPick={(file, label) =>
                                                uploadFile(file, "take", label)
                                            }
                                            inputRef={newTakeRef}
                                        />
                                    )}
                                </div>

                                <div className="mb-8" data-testid="images-upload-section">
                                    <div className="flex items-center justify-between mb-3">
                                        <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70">
                                            Images{" "}
                                            <span className="text-slate-400">
                                                (optional)
                                            </span>
                                        </p>
                                        <span
                                            data-testid="image-counter"
                                            className="text-[11px] font-mono text-slate-400"
                                        >
                                            {images.length}/{MAX_IMAGES_PER_CATEGORY}
                                        </span>
                                    </div>
                                    <p className="text-[12px] leading-relaxed text-slate-500 mb-6">
                                        Optional (recommended). High-resolution
                                        portfolio images aligned with the brand's
                                        aesthetic improve your selection odds. Up to{" "}
                                        {MAX_IMAGES_PER_CATEGORY} per category
                                        (Indian / Western / general looks).
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
                                        uploading={uploading}
                                        uploadPct={uploadPct}
                                        testidPrefix="indian"
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
                                        uploading={uploading}
                                        uploadPct={uploadPct}
                                        testidPrefix="western"
                                    />

                                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70 mt-4 mb-4" data-testid="generic-portfolio-label">
                                        Portfolio (general)
                                    </p>

                                    <div className="grid grid-cols-3 md:grid-cols-4 gap-3 mb-4">
                                        {images.map((m) => (
                                            <div
                                                key={m.id}
                                                className="relative aspect-square bg-slate-100 rounded-2xl overflow-hidden border border-slate-200 group shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.1)] transition-all duration-300 hover:scale-[1.02]"
                                            >
                                                <img
                                                    src={thumbnailUrl(m)}
                                                    alt=""
                                                    className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                                                />
                                                <button
                                                    onClick={() => removeMedia(m.id)}
                                                    className="absolute top-2 right-2 p-1.5 bg-white/80 backdrop-blur-sm hover:bg-rose-50 rounded-full shadow-sm transition-colors opacity-0 group-hover:opacity-100"
                                                >
                                                    <X className="w-3 h-3 text-slate-700" />
                                                </button>
                                            </div>
                                        ))}
                                        {images.length < MAX_IMAGES_PER_CATEGORY && (
                                            <button
                                                onClick={() =>
                                                    imagesRef.current?.click()
                                                }
                                                disabled={uploading === "image"}
                                                data-testid="add-image-btn"
                                                className="relative aspect-square rounded-2xl border border-dashed border-slate-300 hover:border-amber-300 hover:bg-amber-50/20 flex items-center justify-center text-slate-400 hover:text-amber-600 transition-all duration-200 overflow-hidden bg-gradient-to-b from-white to-slate-50/70 shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px]"
                                            >
                                                {uploading === "image" && uploadPct > 0 && (
                                                    <span
                                                        aria-hidden
                                                        className="absolute inset-y-0 left-0 bg-amber-200/30"
                                                        style={{ width: `${uploadPct}%` }}
                                                    />
                                                )}
                                                {uploading === "image" ? (
                                                    <div className="relative flex flex-col items-center gap-1">
                                                        <Loader2 className="w-5 h-5 animate-spin" />
                                                        <span className="text-[10px] font-mono">
                                                            {uploadPct ? `${uploadPct}%` : "…"}
                                                        </span>
                                                    </div>
                                                ) : (
                                                    <div className="relative flex flex-col items-center gap-1">
                                                        <Camera className="w-5 h-5" />
                                                        <span className="text-[10px] font-mono">
                                                            Add
                                                        </span>
                                                    </div>
                                                )}
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
                                            disabled={uploading === "image" || images.length >= MAX_IMAGES_PER_CATEGORY}
                                            data-testid="add-image-camera-btn"
                                            className="border border-slate-200 hover:border-slate-300 p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                        >
                                            <Camera className="w-3.5 h-3.5" /> Take photo
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => imagesRef.current?.click()}
                                            disabled={uploading === "image" || images.length >= MAX_IMAGES_PER_CATEGORY}
                                            data-testid="add-image-library-btn"
                                            className="border border-slate-200 hover:border-slate-300 p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                        >
                                            <FolderOpen className="w-3.5 h-3.5" /> From library
                                        </button>
                                    </div>
                                </div>
                            </div>
                        )}

                        <div className="sticky bottom-0 z-30 bg-gradient-to-t from-white via-white/95 to-transparent pt-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
                            <button
                                onClick={finalize}
                                disabled={finalizing || !readyToSubmit}
                                data-testid="finalize-submission-btn"
                                className="w-full bg-slate-900 text-white py-4 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-2 min-h-[52px] transition-all duration-200"
                                style={{ WebkitTapHighlightColor: "transparent" }}
                            >
                                {finalizing ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Sparkles className="w-4 h-4" />
                                )}
                                Submit Audition
                            </button>
                            {!readyToSubmit && (
                                <p className="text-[11px] text-slate-400 text-center mt-3 font-mono">
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

            {/* Mobile-only sticky bottom action bar for steps 1 & 2.
                Step 3 uses the in-section "Submit Audition" sticky button. */}
            {emailGateUnlocked && mobileStep < 3 && (
                <div
                    className="md:hidden fixed bottom-0 left-0 right-0 z-30 bg-white/90 backdrop-blur-xl border-t border-slate-200/60 px-4 pt-4 pb-[calc(1rem+env(safe-area-inset-bottom))] shadow-[0_-2px_10px_rgba(0,0,0,0.02)]"
                    data-testid="wizard-bottom-bar"
                >
                    <div className="flex items-center gap-2 max-w-3xl mx-auto">
                        {mobileStep > 1 && (
                            <button
                                type="button"
                                onClick={() => goToStep(mobileStep - 1)}
                                data-testid="wizard-back-btn"
                                className="px-5 py-3 border border-slate-200 text-slate-600 rounded-full text-[13px] min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                style={{ WebkitTapHighlightColor: "transparent" }}
                            >
                                Back
                            </button>
                        )}
                        <button
                            type="button"
                            onClick={() => goToStep(mobileStep + 1)}
                            disabled={starting}
                            data-testid="wizard-next-btn"
                            className="flex-1 bg-slate-900 text-white py-3 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] inline-flex items-center justify-center gap-2 min-h-[48px] transition-all duration-200 disabled:opacity-50"
                            style={{ WebkitTapHighlightColor: "transparent" }}
                        >
                            {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                            {mobileStep === 1 ? "Continue to Brief" : "Continue to Uploads"}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

function Info({ label, value, wide }) {
    if (!value) return null;
    return (
        <div className={wide ? "col-span-1 sm:col-span-2" : ""}>
            <div className="text-[10px] tracking-[0.2em] uppercase font-mono text-slate-400 mb-1">
                {label}
            </div>
            <div className="text-[13px] font-medium text-slate-700">{value}</div>
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
    uploading,
    uploadPct,
    testidPrefix,
}) {
    const isUploading = uploading === category;
    const reachedCap = allImagesCount >= maxImages;
    return (
        <div className="mb-8" data-testid={`portfolio-group-${testidPrefix}`}>
            <div className="flex items-center justify-between mb-2">
                <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-slate-500">{label}</p>
                <span className="text-[10px] font-mono text-slate-400">
                    {items.length}
                </span>
            </div>
            {hint && (
                <p className="text-[11px] text-slate-400 mb-4 font-mono">
                    {hint}
                </p>
            )}
            <div className="grid grid-cols-3 md:grid-cols-4 gap-3">
                {items.map((m) => (
                    <div
                        key={m.id}
                        className="relative aspect-square bg-slate-100 rounded-2xl overflow-hidden border border-slate-200 group shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.1)] transition-all duration-300 hover:scale-[1.02]"
                        data-testid={`${testidPrefix}-image-${m.id}`}
                    >
                        <img
                            src={thumbnailUrl(m)}
                            alt=""
                            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                        />
                        <button
                            onClick={() => removeMedia(m.id)}
                            data-testid={`${testidPrefix}-image-remove-${m.id}`}
                            className="absolute top-2 right-2 p-1.5 bg-white/80 backdrop-blur-sm hover:bg-rose-50 rounded-full shadow-sm transition-colors opacity-0 group-hover:opacity-100"
                        >
                            <X className="w-3 h-3 text-slate-700" />
                        </button>
                    </div>
                ))}
                {!reachedCap && (
                    <button
                        type="button"
                        onClick={() => inputRef.current?.click()}
                        disabled={isUploading}
                        data-testid={`add-${testidPrefix}-image-btn`}
                        className="relative aspect-square rounded-2xl border border-dashed border-slate-300 hover:border-amber-300 hover:bg-amber-50/20 flex items-center justify-center text-slate-400 hover:text-amber-600 transition-all duration-200 overflow-hidden disabled:opacity-50 bg-gradient-to-b from-white to-slate-50/70 shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px]"
                    >
                        {isUploading && uploadPct > 0 && (
                            <span
                                aria-hidden
                                className="absolute inset-y-0 left-0 bg-amber-200/30"
                                style={{ width: `${uploadPct}%` }}
                            />
                        )}
                        {isUploading ? (
                            <div className="relative flex flex-col items-center gap-1">
                                <Loader2 className="w-5 h-5 animate-spin" />
                                <span className="text-[10px] font-mono">
                                    {uploadPct ? `${uploadPct}%` : "…"}
                                </span>
                            </div>
                        ) : (
                            <div className="relative flex flex-col items-center gap-1">
                                <Plus className="w-5 h-5" />
                                <span className="text-[10px] font-mono">Add</span>
                            </div>
                        )}
                    </button>
                )}
            </div>
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

function WorkLinksEditor({ links, onChange }) {
    const [input, setInput] = useState("");
    const add = () => {
        const v = input.trim();
        if (!v) return;
        onChange([...(links || []), v]);
        setInput("");
    };
    const remove = (i) =>
        onChange((links || []).filter((_, idx) => idx !== i));
    return (
        <div className="mt-2 space-y-3" data-testid="work-links-editor">
            {(links || []).map((w, i) => (
                <div
                    key={`${w}-${i}`}
                    className="flex items-center justify-between gap-2 px-4 py-2 bg-white/60 rounded-2xl border border-slate-200 text-[11px] font-mono break-all shadow-[0_1px_2px_rgba(0,0,0,0.02)]"
                    data-testid={`work-link-row-${i}`}
                >
                    <span className="truncate text-slate-600">{w}</span>
                    <button
                        type="button"
                        onClick={() => remove(i)}
                        data-testid={`work-link-remove-${i}`}
                        className="text-slate-400 hover:text-rose-500 shrink-0 transition-colors"
                    >
                        <X className="w-3.5 h-3.5" />
                    </button>
                </div>
            ))}
            <div className="flex items-center gap-2">
                <input
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === "Enter") {
                            e.preventDefault();
                            add();
                        }
                    }}
                    inputMode="url"
                    placeholder="https://… (paste & press Enter)"
                    data-testid="work-link-input"
                    className="flex-1 bg-white/60 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-2.5 px-4 text-[16px] md:text-[13px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                />
                <button
                    type="button"
                    onClick={add}
                    data-testid="work-link-add-btn"
                    className="text-[11px] px-4 py-2.5 border border-slate-200 hover:border-slate-300 rounded-full min-h-[44px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-slate-600"
                >
                    Add
                </button>
            </div>
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
            <span className="text-[11px] text-slate-500 tracking-[0.2em] uppercase font-mono">
                {label}
            </span>
            <input
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
                className={`mt-2 w-full bg-white/60 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-amber-100/50 focus:border-amber-200 outline-none py-3 px-4 text-[16px] md:text-[15px] text-slate-800 transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] disabled:text-slate-400 ${className}`}
            />
            {hint && (
                <span className="block text-[10px] text-slate-400 mt-1 font-mono">
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
    accept,
    inputRef,
    onPick,
    uploading,
    uploadPct,
    media,
    onRemove,
    testid,
    compact,
    cameraCapture, // "user" | "environment" — shows a camera-first option on mobile
    onRetry,       // optional: shown when this slot has a failed retry queue entry
    failed,
}) {
    const hasFile = Boolean(media);
    const cameraRef = useRef(null);
    const isVideo = (accept || "").includes("video");
    return (
        <div className={compact ? "mb-4" : "mb-10"}>
            {!compact && (
                <div className="flex items-center justify-between mb-3">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-amber-600/70">
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
                <p className="text-[12px] leading-relaxed text-slate-500 mb-5">
                    {note}
                </p>
            )}
            {hasFile ? (
                <div className="bg-white/60 border border-slate-200 rounded-2xl p-3 flex items-center gap-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                    <Icon className="w-4 h-4 text-slate-500 shrink-0" />
                    <div className="min-w-0 flex-1">
                        <div className="text-[13px] truncate">
                            {compact && (
                                <span className="font-display mr-2 text-slate-700">
                                    {title}
                                    {required && (
                                        <span className="text-rose-500">
                                            {" "}*
                                        </span>
                                    )}
                                </span>
                            )}
                            <span className="text-slate-500 font-mono text-[11px]">
                                {media.original_filename || "file"}
                            </span>
                        </div>
                    </div>
                    <button
                        onClick={() => onRemove(media)}
                        className="text-slate-400 hover:text-rose-500 p-1 min-w-[44px] min-h-[44px] flex items-center justify-center transition-colors"
                    >
                        <Trash2 className="w-4 h-4" />
                    </button>
                </div>
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
                                disabled={uploading}
                                data-testid={`${testid}-camera-btn`}
                                className="border border-slate-200 hover:border-slate-300 p-3.5 text-[13px] rounded-full flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                            >
                                <Camera className="w-4 h-4" />
                                {isVideo ? "Record" : "Take photo"}
                            </button>
                            <button
                                type="button"
                                onClick={() => inputRef.current?.click()}
                                disabled={uploading}
                                data-testid={`${testid}-library-btn`}
                                className="border border-slate-200 hover:border-slate-300 p-3.5 text-[13px] rounded-full flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                            >
                                <FolderOpen className="w-4 h-4" />
                                From library
                            </button>
                        </div>
                    )}
                    <button
                        onClick={() => inputRef.current?.click()}
                        disabled={uploading}
                        data-testid={`${testid}-btn`}
                        className={`w-full bg-gradient-to-b from-white to-slate-50/70 border border-slate-200 hover:border-amber-200 p-4 text-left min-h-[60px] flex items-center gap-3 transition-all duration-200 relative overflow-hidden rounded-2xl shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px] ${cameraCapture ? "hidden md:flex" : ""}`}
                    >
                        {uploading && typeof uploadPct === "number" && uploadPct > 0 && (
                            <span
                                aria-hidden
                                className="absolute inset-y-0 left-0 bg-amber-200/30 transition-[width] duration-300"
                                style={{ width: `${uploadPct}%` }}
                            />
                        )}
                        {uploading ? (
                            <Loader2 className="w-4 h-4 animate-spin relative text-slate-600" />
                        ) : (
                            <Upload className="w-4 h-4 text-slate-500 relative" />
                        )}
                        {compact ? (
                            <span className="text-[13px] flex-1 relative text-slate-700">
                                <span className="font-display mr-2">
                                    {title}
                                    {required && (
                                        <span className="text-rose-500"> *</span>
                                    )}
                                </span>
                                <span className="text-slate-400 text-[11px]">
                                    {uploading && uploadPct ? `Uploading… ${uploadPct}%` : "Tap to upload"}
                                </span>
                            </span>
                        ) : (
                            <span className="text-[13px] text-slate-600 relative">
                                {uploading && uploadPct ? `Uploading… ${uploadPct}%` : "Tap to upload"}
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
        </div>
    );
}

// --------------------------------------------------------------------------
// Renamable take row (existing take) — supports inline label edit + remove
// --------------------------------------------------------------------------
function PremiumTakeRow({ index, media, canRename, onRename, onRemove }) {
    const [label, setLabel] = useState(media.label || `Take ${index}`);
    const [dirty, setDirty] = useState(false);

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

    return (
        <div
            className="bg-white/60 border border-slate-200 rounded-2xl p-3 flex items-center gap-3 mb-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.06)]"
            data-testid={`take-row-${index}`}
        >
            <Video className="w-4 h-4 text-slate-500 shrink-0" />
            <div className="flex-1 min-w-0">
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
                        className={`bg-transparent outline-none text-[16px] md:text-[13px] w-full py-1.5 px-2 rounded-xl border transition-all duration-200 ${dirty ? "border-amber-300 bg-amber-50/30" : "border-transparent"} focus:border-amber-200 focus:ring-2 focus:ring-amber-100/50 text-slate-700`}
                        data-testid={`take-label-${index}`}
                    />
                ) : (
                    <div className="text-[13px] text-slate-700 py-1.5">
                        {label}
                        <span className="ml-2 text-[10px] text-slate-400 font-mono">
                            (legacy)
                        </span>
                    </div>
                )}
                <div className="text-[10px] font-mono text-slate-400 truncate mt-1">
                    {media.original_filename || "file"}
                </div>
            </div>
            <button
                type="button"
                onClick={onRemove}
                className="text-[11px] text-slate-400 hover:text-rose-500 transition-colors"
                data-testid={`take-remove-${index}`}
            >
                Remove
            </button>
        </div>
    );
}

// --------------------------------------------------------------------------
// Add-a-new-take slot — user picks a file, we upload with the label they type
// (falls back to "Take N" if empty).
// --------------------------------------------------------------------------
function PremiumAddTakeSlot({ number, required, uploading, uploadPct, onPick, inputRef }) {
    const [label, setLabel] = useState("");
    const cameraRef = useRef(null);
    const busy = uploading && uploading.startsWith("take");
    const fallback = `Take ${number}`;
    const triggerLib = () => inputRef.current?.click();
    const triggerCam = () => cameraRef.current?.click();

    return (
        <div
            className="bg-gradient-to-b from-white to-slate-50/70 border border-slate-200 hover:border-amber-200 rounded-2xl p-3 relative overflow-hidden shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] transition-all duration-200 hover:-translate-y-[1px]"
            data-testid={`add-take-${number}`}
        >
            {busy && typeof uploadPct === "number" && uploadPct > 0 && (
                <span
                    aria-hidden
                    className="absolute inset-y-0 left-0 bg-amber-200/30 transition-all duration-300"
                    style={{ width: `${uploadPct}%` }}
                />
            )}
            <div className="flex items-center gap-2 relative">
                <input
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder={`${fallback} — add a label`}
                    className="flex-1 bg-transparent outline-none text-[16px] md:text-[13px] py-1.5 px-3 rounded-xl border border-slate-200 focus:border-amber-200 focus:ring-2 focus:ring-amber-100/50 transition-all duration-200 text-slate-700"
                    enterKeyHint="done"
                    data-testid={`new-take-label-${number}`}
                />
                <button
                    type="button"
                    onClick={triggerLib}
                    disabled={busy}
                    className="hidden md:inline-flex relative text-[11px] px-4 py-2 border border-slate-200 hover:border-slate-300 rounded-full items-center gap-1 disabled:opacity-40 min-h-[44px] bg-white/60 text-slate-600 transition-all duration-200"
                    data-testid={`new-take-upload-${number}`}
                >
                    {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
                    {busy && uploadPct ? `${uploadPct}%` : "Upload"}
                    {required && <span className="text-rose-500">*</span>}
                </button>
            </div>
            {/* Mobile-only camera-first dual buttons */}
            <div className="md:hidden grid grid-cols-2 gap-2 mt-3 relative">
                <button
                    type="button"
                    onClick={triggerCam}
                    disabled={busy}
                    className="border border-slate-200 hover:border-slate-300 p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-slate-600"
                    data-testid={`new-take-camera-${number}`}
                >
                    <Camera className="w-3.5 h-3.5" /> Record
                </button>
                <button
                    type="button"
                    onClick={triggerLib}
                    disabled={busy}
                    className="border border-slate-200 hover:border-slate-300 p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-slate-600"
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
            className="bg-white/60 border border-slate-200 rounded-2xl p-5 shadow-[0_1px_2px_rgba(0,0,0,0.02)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.06)]"
            data-testid={`talent-feedback-${fb.id}`}
        >
            <div className="flex items-center justify-between gap-3 mb-3">
                <span className="inline-flex items-center gap-1.5 text-[10px] tracking-[0.2em] uppercase font-mono text-slate-500">
                    {isVoice ? (
                        <Mic className="w-3 h-3" />
                    ) : (
                        <MessageSquare className="w-3 h-3" />
                    )}
                    {isVoice ? "Voice" : "Text"}
                </span>
                <span className="text-[10px] font-mono text-slate-400">
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
                    className="text-[13px] leading-relaxed text-slate-700 whitespace-pre-wrap"
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
