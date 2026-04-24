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
    const [finalizing, setFinalizing] = useState(false);

    const introRef = useRef();
    const take1Ref = useRef();
    const take2Ref = useRef();
    const take3Ref = useRef();
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

    const uploadFile = async (file, category) => {
        setUploading(category);
        try {
            const fd = new FormData();
            fd.append("file", file);
            fd.append("category", category);
            const { data } = await axios.post(
                `${API}/public/submissions/${saved.id}/upload`,
                fd,
                {
                    ...authCfg,
                    headers: {
                        ...authCfg.headers,
                        "Content-Type": "multipart/form-data",
                    },
                },
            );
            setSubmission(data);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const uploadImages = async (files) => {
        const current = images.length;
        const room = MAX_IMAGES - current;
        const accepted = Array.from(files).slice(0, room);
        if (files.length > room) {
            toast.info(`Only ${room} more images allowed (max ${MAX_IMAGES})`);
        }
        setUploading("image");
        try {
            let last = null;
            for (const f of accepted) {
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
                    },
                );
                last = data;
            }
            if (last) setSubmission(last);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
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
    const mediaIn = (c) => media.find((m) => m.category === c);
    const images = media.filter((m) => m.category === "image");
    const takes = [1, 2, 3].map((i) => mediaIn(`take_${i}`));
    const intro = mediaIn("intro_video");
    const isSubmitted = submission?.status === "submitted";

    const readyToSubmit =
        intro &&
        takes[0] &&
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

    // ---------------------------------------------------------------
    if (isSubmitted) {
        return (
            <div className="min-h-screen bg-[#050505] text-white flex items-center justify-center p-6 relative">
                <div className="absolute top-5 right-5"><ThemeToggle /></div>
                <div className="max-w-lg w-full text-center tg-fade-up">
                    <div className="w-14 h-14 mx-auto mb-6 rounded-full border border-white/20 flex items-center justify-center">
                        <Check className="w-6 h-6" />
                    </div>
                    <p className="eyebrow mb-3">Submitted</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight mb-5">
                        Thank you, {form.first_name || submission.talent_name.split(" ")[0]}.
                    </h1>
                    <p className="text-white/60 text-sm md:text-base leading-relaxed">
                        Your audition for{" "}
                        <span className="text-white">{project.brand_name}</span>{" "}
                        has been received. The Talentgram team will review and
                        reach out if you're shortlisted.
                    </p>
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
                            media={intro}
                            onRemove={(m) => removeMedia(m.id)}
                            testid="upload-intro"
                        />

                        <div className="mb-8">
                            <p className="eyebrow mb-3">Audition Takes</p>
                            <UploadSlot
                                title="Take 1" required icon={Video}
                                accept="video/*" inputRef={take1Ref}
                                onPick={(f) => uploadFile(f[0], "take_1")}
                                uploading={uploading === "take_1"}
                                media={takes[0]}
                                onRemove={(m) => removeMedia(m.id)}
                                testid="upload-take-1" compact
                            />
                            <UploadSlot
                                title="Take 2 (optional)" icon={Video}
                                accept="video/*" inputRef={take2Ref}
                                onPick={(f) => uploadFile(f[0], "take_2")}
                                uploading={uploading === "take_2"}
                                media={takes[1]}
                                onRemove={(m) => removeMedia(m.id)}
                                testid="upload-take-2" compact
                            />
                            <UploadSlot
                                title="Take 3 (optional)" icon={Video}
                                accept="video/*" inputRef={take3Ref}
                                onPick={(f) => uploadFile(f[0], "take_3")}
                                uploading={uploading === "take_3"}
                                media={takes[2]}
                                onRemove={(m) => removeMedia(m.id)}
                                testid="upload-take-3" compact
                            />
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
                                        className="aspect-square border border-dashed border-white/20 hover:border-white/50 flex items-center justify-center text-white/50 hover:text-white transition-all"
                                    >
                                        {uploading === "image" ? (
                                            <Loader2 className="w-5 h-5 animate-spin" />
                                        ) : (
                                            <div className="flex flex-col items-center gap-1">
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
                    className="w-full border border-dashed border-white/20 hover:border-white/50 p-4 text-left min-h-[60px] flex items-center gap-3 transition-all"
                >
                    {uploading ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                        <Upload className="w-4 h-4 text-white/60" />
                    )}
                    {compact ? (
                        <span className="text-sm flex-1">
                            <span className="font-display mr-2">
                                {title}
                                {required && (
                                    <span className="text-[#FF3B30]"> *</span>
                                )}
                            </span>
                            <span className="text-white/40 text-xs">
                                Tap to upload
                            </span>
                        </span>
                    ) : (
                        <span className="text-sm text-white/70">
                            Tap to upload
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
