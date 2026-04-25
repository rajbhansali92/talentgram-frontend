import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import axios from "axios";
import { toast } from "sonner";
import { FILE_URL } from "@/lib/api";
import MaterialModal from "@/components/MaterialModal";
import Logo from "@/components/Logo";
import ThemeToggle from "@/components/ThemeToggle";
import {
    Select,
    SelectContent,
    SelectItem,
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
} from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const MAX_IMAGES = 8;
const MIN_IMAGES = 5;
const LS_KEY = (slug) => `tg_submission_${slug}`;

const HEIGHT_OPTIONS = (() => {
    const out = [];
    for (let ft = 3; ft <= 6; ft++) {
        const maxIn = ft === 6 ? 7 : 11;
        for (let inch = 0; inch <= maxIn; inch++) out.push(`${ft}'${inch}"`);
    }
    return out;
})();

function calcAge(dob) {
    if (!dob) return null;
    const [y, m, d] = dob.split("-").map((n) => parseInt(n, 10));
    if (!y || !m || !d) return null;
    const today = new Date();
    let age = today.getFullYear() - y;
    const mm = today.getMonth() + 1;
    const dd = today.getDate();
    if (mm < m || (mm === m && dd < d)) age -= 1;
    return age >= 0 && age <= 120 ? age : null;
}

function readSaved(slug) {
    try {
        return JSON.parse(localStorage.getItem(LS_KEY(slug)) || "null");
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

    // Full form
    const [form, setForm] = useState({
        first_name: "",
        last_name: "",
        email: "",
        phone: "",
        dob: "",
        age: "",
        height: "",
        location: "",
        competitive_brand: "",
        availability: { status: "", note: "" },
        budget: { status: "", value: "" },
        commission: "",
        custom_answers: {},
    });
    const [starting, setStarting] = useState(false);

    const [submission, setSubmission] = useState(null);
    const [uploading, setUploading] = useState(null);
    const [uploadPct, setUploadPct] = useState(0);
    const [finalizing, setFinalizing] = useState(false);
    const [editMode, setEditMode] = useState(false);

    const introRef = useRef();
    const take1Ref = useRef();
    const newTakeRef = useRef();
    const imagesRef = useRef();

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

    const computedAge = useMemo(
        () => calcAge(form.dob) ?? (form.age ? parseInt(form.age, 10) : null),
        [form.dob, form.age],
    );

    const authCfg = useMemo(
        () =>
            saved?.token
                ? { headers: { Authorization: `Bearer ${saved.token}` } }
                : {},
        [saved],
    );

    const validateForm = () => {
        if (!form.first_name.trim()) return "First name is required";
        if (!form.last_name.trim()) return "Last name is required";
        if (!form.email.trim()) return "Email is required";
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

    // Auto-prefill on email blur — if this email has an approved talent record,
    // auto-fill known fields so returning talents don't retype everything.
    const [prefillTried, setPrefillTried] = useState(false);
    const tryPrefill = async () => {
        if (saved) return; // already started submission
        const email = (form.email || "").trim().toLowerCase();
        if (!email || !email.includes("@") || prefillTried) return;
        setPrefillTried(true);
        try {
            const { data } = await axios.get(
                `${API}/public/prefill?email=${encodeURIComponent(email)}`,
            );
            if (!data || !data.first_name) return;
            setForm((f) => ({
                ...f,
                first_name: f.first_name || data.first_name || "",
                last_name: f.last_name || data.last_name || "",
                height: f.height || data.height || "",
                location: f.location || data.location || "",
                instagram_handle: f.instagram_handle || data.instagram_handle || "",
                instagram_followers:
                    f.instagram_followers || data.instagram_followers || "",
            }));
            toast.success(
                `Welcome back, ${data.first_name} — we auto-filled what we had`,
            );
        } catch {
            // silent
        }
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

    const saveForm = async () => {
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
        } catch {}
    };

    const uploadFile = async (file, category, label = null) => {
        // Client-side guard mirrors backend cap (150 MB videos / 25 MB images)
        const isVideoSlot = ["intro_video", "take", "take_1", "take_2", "take_3"].includes(category);
        const CAP_MB = isVideoSlot ? 150 : 25;
        if (file && file.size > CAP_MB * 1024 * 1024) {
            toast.error(`File too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
            return;
        }
        setUploading(label ? `${category}:${label}` : category);
        setUploadPct(0);
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
                    onUploadProgress: (e) => {
                        if (e.total) setUploadPct(Math.round((e.loaded / e.total) * 100));
                    },
                },
            );
            setSubmission(data);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
            setUploadPct(0);
        }
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

    const uploadImages = async (files) => {
        const current = images.length;
        const room = MAX_IMAGES - current;
        const accepted = Array.from(files).slice(0, room);
        if (files.length > room) {
            toast.info(`Only ${room} more images allowed (max ${MAX_IMAGES})`);
        }
        // Client-side per-image cap (25 MB)
        const over = accepted.find((f) => f.size > 25 * 1024 * 1024);
        if (over) {
            toast.error(`"${over.name}" is too large (max 25 MB per image).`);
            return;
        }
        setUploading("image");
        setUploadPct(0);
        try {
            let last = null;
            const totalFiles = accepted.length;
            for (let i = 0; i < accepted.length; i++) {
                const f = accepted[i];
                const fd = new FormData();
                fd.append("file", f);
                fd.append("category", "image");
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
            <div className="min-h-screen flex items-center justify-center bg-[#050505]">
                <Loader2 className="w-6 h-6 animate-spin text-white/40" />
            </div>
        );
    }
    if (!project) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] text-white/60 p-6 text-center">
                <p>Project not found.</p>
            </div>
        );
    }

    const media = submission?.media || [];
    const images = media.filter((m) => m.category === "image");
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
        intro &&
        takes.length > 0 &&
        images.length >= MIN_IMAGES &&
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
    // the Submit button so talents never guess why it's disabled.
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
    if (!intro) missing.push("Introduction video");
    if (takes.length === 0) missing.push("At least 1 audition take");
    if (images.length < MIN_IMAGES)
        missing.push(
            `${MIN_IMAGES - images.length} more image${MIN_IMAGES - images.length > 1 ? "s" : ""} (${images.length}/${MIN_IMAGES} min)`,
        );

    // ---------------------------------------------------------------
    // SUBMITTED / UPDATED state — offer a "Refine my submission" path
    if (isSubmitted && !editMode) {
        const statusLabel =
            submission?.status === "updated" ? "Resubmitted" : "Submitted";
        const feedback = submission?.client_feedback || [];
        return (
            <div className="min-h-screen bg-[#050505] text-white relative">
                <div className="absolute top-5 right-5">
                    <ThemeToggle />
                </div>
                <div className="max-w-2xl w-full mx-auto px-6 py-12 md:py-20 tg-fade-up">
                    <div className="text-center">
                        <div className="w-14 h-14 mx-auto mb-6 rounded-full border border-white/20 flex items-center justify-center">
                            <Check className="w-6 h-6" />
                        </div>
                        <p className="eyebrow mb-3">{statusLabel}</p>
                        <h1 className="font-display text-4xl md:text-5xl tracking-tight mb-5">
                            Thank you,{" "}
                            {form.first_name || submission.talent_name?.split(" ")[0]}.
                        </h1>
                        <p className="text-white/60 text-sm md:text-base leading-relaxed mb-8">
                            Your audition for{" "}
                            <span className="text-white">
                                {project.brand_name}
                            </span>{" "}
                            has been received. The Talentgram team will review and
                            reach out if you're shortlisted.
                        </p>
                        <button
                            type="button"
                            onClick={() => setEditMode(true)}
                            data-testid="refine-submission-btn"
                            className="text-xs tg-mono text-white/60 hover:text-white underline underline-offset-4"
                        >
                            Want to refine or replace a take? Update your submission →
                        </button>
                    </div>

                    {/* Client Feedback inbox — only approved+shared rows ever appear
                        here. The relay is mediated by the team, so notes the talent
                        sees have been reviewed. Order is approval-time ascending. */}
                    <section
                        className="mt-14"
                        data-testid="talent-feedback-section"
                    >
                        <p className="eyebrow mb-3">Client Feedback</p>
                        {feedback.length === 0 ? (
                            <div
                                className="border border-white/10 rounded-sm p-5 text-sm text-white/50"
                                data-testid="talent-feedback-empty"
                            >
                                No feedback yet — the team will share notes here
                                once a client responds.
                            </div>
                        ) : (
                            <div className="space-y-3">
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
        <div className="min-h-screen bg-[#050505] text-white" data-testid="submission-page">
            <header className="sticky top-0 z-30 bg-black/80 backdrop-blur-xl border-b border-white/10">
                <div className="max-w-3xl mx-auto px-5 py-4 flex items-center justify-between">
                    <Logo size="sm" />
                    <ThemeToggle size="sm" />
                </div>
            </header>

            <div className="max-w-3xl mx-auto px-5 py-8 md:py-14">
                {/* SECTION 1 — Project Info */}
                <section className="mb-10" data-testid="project-info-section">
                    <p className="eyebrow mb-3">Audition Brief</p>
                    <h1 className="font-display text-3xl md:text-5xl tracking-tight mb-6">
                        Talentgram × {project.brand_name}
                    </h1>
                    <div className="grid grid-cols-2 gap-x-6 gap-y-5 mb-6 border-t border-white/10 pt-6">
                        <Info label="Character" value={project.character} />
                        <Info label="Shoot Dates" value={project.shoot_dates} />
                        <Info label="Budget / Day" value={project.budget_per_day} />
                        <Info label="Commission" value={project.commission_percent} />
                        <Info label="Medium / Usage" value={project.medium_usage} wide />
                    </div>
                    {project.additional_details && (
                        <div className="mb-6">
                            <p className="text-[11px] text-white/50 tracking-widest uppercase mb-2">
                                Additional Details
                            </p>
                            <p className="text-sm text-white/80 whitespace-pre-line">
                                {project.additional_details}
                            </p>
                        </div>
                    )}
                    {((project.materials || []).length > 0 ||
                        (project.video_links || []).length > 0) && (
                        <button
                            onClick={() => setShowMaterial(true)}
                            data-testid="view-audition-material-btn"
                            className="inline-flex items-center gap-2 px-4 py-3 border border-white/15 hover:border-white rounded-sm text-sm transition-all"
                        >
                            <FolderOpen className="w-4 h-4" /> View Audition Material
                        </button>
                    )}
                </section>

                {/* SECTION 2 — TALENT DETAILS FORM */}
                <section
                    className="border-t border-white/10 pt-10 mb-10"
                    data-testid="talent-details-section"
                >
                    <p className="eyebrow mb-3">Talent Details</p>
                    <h2 className="font-display text-2xl md:text-3xl tracking-tight mb-2">
                        Your profile.
                    </h2>
                    <p className="text-sm text-white/50 mb-8">
                        All fields are required unless marked optional.
                    </p>

                    <form onSubmit={startSubmission} className="space-y-6">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-6">
                            <FormField
                                label="First Name *"
                                value={form.first_name}
                                onChange={(v) =>
                                    setForm({ ...form, first_name: v })
                                }
                                onBlur={saveForm}
                                testid="form-first-name"
                                required
                            />
                            <FormField
                                label="Last Name *"
                                value={form.last_name}
                                onChange={(v) =>
                                    setForm({ ...form, last_name: v })
                                }
                                onBlur={saveForm}
                                testid="form-last-name"
                                required
                            />
                            <FormField
                                label="Email *"
                                type="email"
                                value={form.email}
                                onChange={(v) =>
                                    setForm({ ...form, email: v })
                                }
                                onBlur={() => {
                                    saveForm();
                                    tryPrefill();
                                }}
                                testid="form-email"
                                required
                                disabled={!!saved}
                            />
                            <FormField
                                label="Phone (optional)"
                                type="tel"
                                value={form.phone}
                                onChange={(v) =>
                                    setForm({ ...form, phone: v })
                                }
                                onBlur={saveForm}
                                testid="form-phone"
                            />
                            <FormField
                                label="Date of Birth (optional)"
                                type="date"
                                value={form.dob}
                                max={new Date().toISOString().split("T")[0]}
                                onChange={(v) =>
                                    setForm({ ...form, dob: v, age: "" })
                                }
                                onBlur={saveForm}
                                testid="form-dob"
                                className="[color-scheme:dark]"
                            />
                            <div data-testid="form-age-field">
                                <span className="text-[11px] text-white/60 tracking-widest uppercase">
                                    Age {form.dob ? "(auto)" : "*"}
                                </span>
                                <input
                                    type="number"
                                    value={
                                        form.dob
                                            ? computedAge ?? ""
                                            : form.age
                                    }
                                    disabled={!!form.dob}
                                    onChange={(e) =>
                                        setForm({
                                            ...form,
                                            age: e.target.value,
                                        })
                                    }
                                    onBlur={saveForm}
                                    min={10}
                                    max={80}
                                    data-testid="form-age-input"
                                    className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-3 text-base disabled:text-white/50"
                                />
                            </div>
                            <div data-testid="form-height-field">
                                <span className="text-[11px] text-white/60 tracking-widest uppercase">
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
                                            className="bg-transparent border-0 border-b border-white/20 rounded-none px-0 focus:border-white focus:ring-0 shadow-none h-auto py-3"
                                        >
                                            <SelectValue placeholder="Select height" />
                                        </SelectTrigger>
                                        <SelectContent className="max-h-72">
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
                            <FormField
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
                                <FormField
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

                        {/* AVAILABILITY — decision block */}
                        <div
                            className="border-t border-white/10 pt-7"
                            data-testid="availability-block"
                        >
                            <p className="eyebrow mb-2">
                                Availability{" "}
                                <span className="text-[#FF3B30]">*</span>
                            </p>
                            {project.shoot_dates && (
                                <p className="text-xs text-white/50 mb-4 leading-relaxed">
                                    {project.shoot_dates}
                                    {" — "}Costume trial and rehearsal dates
                                    (if any) will be informed.
                                </p>
                            )}
                            <div className="grid grid-cols-2 gap-2 mb-3">
                                {[
                                    { key: "yes", label: "Yes, available" },
                                    { key: "no", label: "Not available" },
                                ].map((opt) => {
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
                                            className={`px-4 py-3.5 rounded-sm text-sm border transition-all min-h-[52px] ${active ? "bg-white text-black border-white" : "border-white/20 hover:border-white/50 text-white/80"}`}
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
                                    className="w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none"
                                />
                            )}
                        </div>

                        {/* BUDGET — decision block */}
                        <div
                            className="border-t border-white/10 pt-7"
                            data-testid="budget-block"
                        >
                            <p className="eyebrow mb-4">
                                Budget{" "}
                                <span className="text-[#FF3B30]">*</span>
                            </p>
                            {project.commission_percent && (
                                <div
                                    className="flex items-center justify-between border border-white/25 bg-white/[0.04] px-4 py-3 mb-5"
                                    data-testid="commission-card"
                                >
                                    <span className="text-[11px] tracking-widest uppercase text-white/70">
                                        Commission
                                    </span>
                                    <span className="font-display text-2xl text-white">
                                        {project.commission_percent}
                                    </span>
                                </div>
                            )}
                            {(project.talent_budget || []).length > 0 && (
                                <div
                                    className="border border-white/15 bg-white/[0.03] px-4 py-3 mb-5"
                                    data-testid="talent-budget-hint"
                                >
                                    <div className="text-[10px] tracking-widest uppercase text-white/50 mb-2">
                                        Offered Budget
                                    </div>
                                    <div className="space-y-1.5">
                                        {project.talent_budget.map((row, i) => (
                                            <div
                                                key={`${row.label || ""}-${i}`}
                                                className="flex items-center justify-between gap-3 text-sm"
                                                data-testid={`talent-budget-line-${i}`}
                                            >
                                                <span className="text-white/70">
                                                    {row.label || "—"}
                                                </span>
                                                <span className="tg-mono text-white/90">
                                                    {row.value || "—"}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
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
                                    className={`px-4 py-3.5 rounded-sm text-sm border transition-all min-h-[52px] ${form.budget.status === "accept" ? "bg-white text-black border-white" : "border-white/20 hover:border-white/50 text-white/80"}`}
                                >
                                    Accept
                                    {project.budget_per_day && (
                                        <span className="block text-[11px] tg-mono opacity-70 mt-0.5">
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
                                    className={`px-4 py-3.5 rounded-sm text-sm border transition-all min-h-[52px] ${form.budget.status === "custom" ? "bg-white text-black border-white" : "border-white/20 hover:border-white/50 text-white/80"}`}
                                >
                                    Not accepting
                                    <span className="block text-[11px] tg-mono opacity-70 mt-0.5">
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
                                    className="w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-3 text-base"
                                />
                            )}
                        </div>

                        {project.medium_usage && (
                            <div className="border-t border-white/10 pt-7">
                                <p className="eyebrow mb-3">Medium / Usage</p>
                                <p className="text-sm text-white/80">
                                    {project.medium_usage}
                                </p>
                            </div>
                        )}

                        {(project.custom_questions || []).length > 0 && (
                            <div className="border-t border-white/10 pt-6 space-y-5">
                                <p className="eyebrow">Additional Questions</p>
                                {project.custom_questions.map((q) => (
                                    <FormField
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

                        {!saved && (
                            <button
                                type="submit"
                                disabled={starting}
                                data-testid="start-submission-btn"
                                className="w-full bg-white text-black py-4 rounded-sm text-sm font-medium hover:opacity-90 inline-flex items-center justify-center gap-2 min-h-[52px]"
                            >
                                {starting && (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                )}
                                Save Details & Continue to Uploads
                            </button>
                        )}
                    </form>
                </section>

                {/* SECTION 3 — UPLOADS (gated on saved) */}
                {saved && (
                    <section
                        className="border-t border-white/10 pt-10"
                        data-testid="uploads-section"
                    >
                        <p className="eyebrow mb-3">Uploads</p>
                        <h2 className="font-display text-2xl md:text-3xl tracking-tight mb-8">
                            Show us your work.
                        </h2>

                        <UploadSlot
                            title="Introduction Video"
                            required
                            note="Please provide your most recent professional introduction video (without contact info)."
                            icon={Video}
                            accept="video/*"
                            inputRef={introRef}
                            onPick={(f) => uploadFile(f[0], "intro_video")}
                            uploading={uploading === "intro_video"}
                            uploadPct={uploadPct}
                            media={intro}
                            onRemove={(m) => removeMedia(m.id)}
                            testid="upload-intro"
                        />

                        <div className="mb-8" data-testid="takes-section">
                            <div className="flex items-center justify-between mb-3">
                                <p className="eyebrow">
                                    Audition Takes{" "}
                                    <span className="text-white/40">
                                        (up to {MAX_TAKES})
                                    </span>
                                </p>
                                <span
                                    className="text-xs tg-mono text-white/50"
                                    data-testid="takes-counter"
                                >
                                    {takes.length}/{MAX_TAKES}
                                </span>
                            </div>
                            <p className="text-xs text-white/50 mb-4 leading-relaxed">
                                Upload each take as a separate video and label
                                it (e.g., "Scene 1", "Closeup emotional"). At
                                least one take is required.
                            </p>

                            {takes.map((t, i) => (
                                <TakeRow
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
                                <AddTakeSlot
                                    number={takes.length + 1}
                                    required={takes.length === 0}
                                    uploading={uploading}
                                    uploadPct={uploadPct}
                                    onPick={(file, label) =>
                                        uploadFile(file, "take", label)
                                    }
                                    inputRef={newTakeRef}
                                />
                            )}
                        </div>

                        <div className="mb-10" data-testid="images-upload-section">
                            <div className="flex items-center justify-between mb-2">
                                <p className="eyebrow">
                                    Images{" "}
                                    <span className="text-white/40">
                                        (min {MIN_IMAGES})
                                    </span>
                                </p>
                                <span
                                    data-testid="image-counter"
                                    className={`text-xs tg-mono ${images.length >= MIN_IMAGES ? "text-[#34C759]" : "text-white/70"}`}
                                >
                                    {images.length}/{MAX_IMAGES}
                                </span>
                            </div>
                            <p className="text-xs text-white/50 mb-4 leading-relaxed">
                                Please send 7–8 high-resolution professional
                                images that align with the brand's aesthetic.
                                Minimum {MIN_IMAGES} required.
                            </p>

                            <div className="grid grid-cols-3 md:grid-cols-4 gap-2 mb-3">
                                {images.map((m) => (
                                    <div
                                        key={m.id}
                                        className="relative aspect-square bg-[#0a0a0a] border border-white/10 group"
                                    >
                                        <img
                                            src={FILE_URL(m.storage_path)}
                                            alt=""
                                            className="w-full h-full object-cover"
                                        />
                                        <button
                                            onClick={() => removeMedia(m.id)}
                                            className="absolute top-1 right-1 p-1 bg-black/70 hover:bg-[#FF3B30] rounded-sm"
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    </div>
                                ))}
                                {images.length < MAX_IMAGES && (
                                    <button
                                        onClick={() =>
                                            imagesRef.current?.click()
                                        }
                                        disabled={uploading === "image"}
                                        data-testid="add-image-btn"
                                        className="relative aspect-square border border-dashed border-white/20 hover:border-white/50 flex items-center justify-center text-white/50 hover:text-white transition-all overflow-hidden"
                                    >
                                        {uploading === "image" && uploadPct > 0 && (
                                            <span
                                                aria-hidden
                                                className="absolute inset-y-0 left-0 bg-white/10"
                                                style={{ width: `${uploadPct}%` }}
                                            />
                                        )}
                                        {uploading === "image" ? (
                                            <div className="relative flex flex-col items-center gap-1">
                                                <Loader2 className="w-5 h-5 animate-spin" />
                                                <span className="text-[10px] tg-mono">
                                                    {uploadPct ? `${uploadPct}%` : "…"}
                                                </span>
                                            </div>
                                        ) : (
                                            <div className="relative flex flex-col items-center gap-1">
                                                <Camera className="w-5 h-5" />
                                                <span className="text-[10px] tg-mono">
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
                        </div>

                        <div className="sticky bottom-4">
                            <button
                                onClick={finalize}
                                disabled={finalizing || !readyToSubmit}
                                data-testid="finalize-submission-btn"
                                className="w-full bg-white text-black py-4 rounded-sm text-sm font-medium hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-2 min-h-[52px] shadow-[0_8px_40px_rgba(0,0,0,0.6)]"
                            >
                                {finalizing ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Sparkles className="w-4 h-4" />
                                )}
                                Submit Audition
                            </button>
                            {!readyToSubmit && (
                                <p className="text-[11px] text-white/40 text-center mt-3 tg-mono">
                                    Need: First+Last name · Height · Location ·
                                    Intro · Take 1 · {MIN_IMAGES}+ images
                                </p>
                            )}
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
        </div>
    );
}

function Info({ label, value, wide }) {
    if (!value) return null;
    return (
        <div className={wide ? "col-span-2" : ""}>
            <div className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                {label}
            </div>
            <div className="text-sm font-medium">{value}</div>
        </div>
    );
}

function FormField({
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
    return (
        <label className={`block ${wide ? "md:col-span-2" : ""}`}>
            <span className="text-[11px] text-white/60 tracking-widest uppercase">
                {label}
            </span>
            <input
                type={type}
                value={value || ""}
                onChange={(e) => onChange(e.target.value)}
                onBlur={onBlur}
                required={required}
                placeholder={placeholder}
                max={max}
                disabled={disabled}
                data-testid={testid}
                className={`mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-3 text-base disabled:text-white/50 ${className}`}
            />
            {hint && (
                <span className="block text-[10px] text-white/40 mt-1 tg-mono">
                    {hint}
                </span>
            )}
        </label>
    );
}

function UploadSlot({
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
}) {
    const hasFile = Boolean(media);
    return (
        <div className={compact ? "mb-3" : "mb-8"}>
            {!compact && (
                <div className="flex items-center justify-between mb-2">
                    <p className="eyebrow">
                        {title}
                        {required && (
                            <span className="text-[#FF3B30]"> *</span>
                        )}
                    </p>
                    {hasFile && (
                        <span className="inline-flex items-center gap-1 text-[10px] tracking-widest uppercase text-[#34C759]">
                            <Check className="w-3 h-3" /> Uploaded
                        </span>
                    )}
                </div>
            )}
            {!compact && note && (
                <p className="text-xs text-white/50 mb-4 leading-relaxed">
                    {note}
                </p>
            )}
            {hasFile ? (
                <div className="border border-white/10 p-3 flex items-center gap-3">
                    <Icon className="w-4 h-4 text-white/60 shrink-0" />
                    <div className="min-w-0 flex-1">
                        <div className="text-sm truncate">
                            {compact && (
                                <span className="font-display mr-2">
                                    {title}
                                    {required && (
                                        <span className="text-[#FF3B30]">
                                            {" "}*
                                        </span>
                                    )}
                                </span>
                            )}
                            <span className="text-white/60 tg-mono text-xs">
                                {media.original_filename || "file"}
                            </span>
                        </div>
                    </div>
                    <button
                        onClick={() => onRemove(media)}
                        className="text-white/50 hover:text-[#FF3B30] p-1"
                    >
                        <Trash2 className="w-4 h-4" />
                    </button>
                </div>
            ) : (
                <button
                    onClick={() => inputRef.current?.click()}
                    disabled={uploading}
                    data-testid={`${testid}-btn`}
                    className="w-full border border-dashed border-white/20 hover:border-white/50 p-4 text-left min-h-[60px] flex items-center gap-3 transition-all relative overflow-hidden"
                >
                    {uploading && typeof uploadPct === "number" && uploadPct > 0 && (
                        <span
                            aria-hidden
                            className="absolute inset-y-0 left-0 bg-white/10 transition-[width]"
                            style={{ width: `${uploadPct}%` }}
                        />
                    )}
                    {uploading ? (
                        <Loader2 className="w-4 h-4 animate-spin relative" />
                    ) : (
                        <Upload className="w-4 h-4 text-white/60 relative" />
                    )}
                    {compact ? (
                        <span className="text-sm flex-1 relative">
                            <span className="font-display mr-2">
                                {title}
                                {required && (
                                    <span className="text-[#FF3B30]"> *</span>
                                )}
                            </span>
                            <span className="text-white/40 text-xs">
                                {uploading && uploadPct ? `Uploading… ${uploadPct}%` : "Tap to upload"}
                            </span>
                        </span>
                    ) : (
                        <span className="text-sm text-white/70 relative">
                            {uploading && uploadPct ? `Uploading… ${uploadPct}%` : "Tap to upload"}
                        </span>
                    )}
                </button>
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
        </div>
    );
}

// --------------------------------------------------------------------------
// Renamable take row (existing take) — supports inline label edit + remove
// --------------------------------------------------------------------------
function TakeRow({ index, media, canRename, onRename, onRemove }) {
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
            className="border border-white/10 p-3 flex items-center gap-3 mb-3"
            data-testid={`take-row-${index}`}
        >
            <Video className="w-4 h-4 text-white/60 shrink-0" />
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
                        className={`bg-transparent outline-none text-sm w-full py-1.5 border-b ${dirty ? "border-white/40" : "border-transparent"} focus:border-white/60`}
                        data-testid={`take-label-${index}`}
                    />
                ) : (
                    <div className="text-sm text-white/80 py-1.5">
                        {label}
                        <span className="ml-2 text-[10px] text-white/30 tg-mono">
                            (legacy)
                        </span>
                    </div>
                )}
                <div className="text-[10px] tg-mono text-white/40 truncate">
                    {media.original_filename || "file"}
                </div>
            </div>
            <button
                type="button"
                onClick={onRemove}
                className="text-xs text-white/40 hover:text-[#FF3B30]"
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
function AddTakeSlot({ number, required, uploading, uploadPct, onPick, inputRef }) {
    const [label, setLabel] = useState("");
    const busy = uploading && uploading.startsWith("take");
    const fallback = `Take ${number}`;

    return (
        <div
            className="border border-dashed border-white/15 p-3 flex items-center gap-2 relative overflow-hidden"
            data-testid={`add-take-${number}`}
        >
            {busy && typeof uploadPct === "number" && uploadPct > 0 && (
                <span
                    aria-hidden
                    className="absolute inset-y-0 left-0 bg-white/10"
                    style={{ width: `${uploadPct}%` }}
                />
            )}
            <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder={`${fallback} — add a label (e.g. Scene 1)`}
                className="relative flex-1 bg-transparent outline-none text-sm py-1.5 border-b border-white/10 focus:border-white/40"
                data-testid={`new-take-label-${number}`}
            />
            <button
                type="button"
                onClick={() => inputRef.current?.click()}
                disabled={busy}
                className="relative text-xs px-3 py-2 border border-white/15 hover:border-white/40 rounded-sm inline-flex items-center gap-1 disabled:opacity-40"
                data-testid={`new-take-upload-${number}`}
            >
                {busy ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                    <Plus className="w-3 h-3" />
                )}
                {busy && uploadPct ? `${uploadPct}%` : "Upload"}
                {required && <span className="text-[#FF3B30]">*</span>}
            </button>
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
        </div>
    );
}

function FeedbackRow({ fb }) {
    const isVoice = fb.type === "voice";
    return (
        <div
            className="border border-white/10 bg-white/[0.02] p-4 rounded-sm"
            data-testid={`talent-feedback-${fb.id}`}
        >
            <div className="flex items-center justify-between gap-3 mb-2">
                <span className="inline-flex items-center gap-1.5 text-[10px] tracking-widest uppercase text-white/50">
                    {isVoice ? (
                        <Mic className="w-3 h-3" />
                    ) : (
                        <MessageSquare className="w-3 h-3" />
                    )}
                    {isVoice ? "Voice" : "Text"}
                </span>
                <span className="text-[10px] tg-mono text-white/40">
                    Received {timeAgo(fb.approved_at || fb.created_at)}
                </span>
            </div>
            {isVoice ? (
                <audio
                    src={FILE_URL(fb.content_url)}
                    controls
                    className="w-full"
                    data-testid={`talent-feedback-audio-${fb.id}`}
                />
            ) : (
                <p
                    className="text-sm text-white/85 whitespace-pre-wrap"
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


