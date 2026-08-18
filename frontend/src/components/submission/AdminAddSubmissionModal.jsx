import React, { useState, useEffect, useRef } from "react";
import { toast } from "sonner";
import { X, Search, UserPlus, Loader2, Check, Trash2, Video, Image as ImageIcon, Upload } from "lucide-react";
import { adminApi, api, IMAGE_URL } from "@/lib/api";
import { useUploadManager } from "@/context/UploadManagerContext";
import { AVAILABILITY_OPTIONS, BUDGET_OPTIONS } from "@/lib/talentSchema";
import LocationSelector from "@/components/LocationSelector";
import { computeRequirementItems } from "@/lib/requirementEngine";

// Admin "Add Submission" — Phase 1. Manually creates a submission for a
// talent who couldn't submit themselves, entirely inside the Submission
// Review Center. Reuses the existing admin-start / public submission /
// upload / finalize endpoints exactly as the talent-facing wizard and the
// Review Center's own "Quick Edit" media feature already do — no parallel
// talent, requirement, or upload architecture.

function normalizeFormData(fd) {
    return {
        ...fd,
        availability:
            typeof fd?.availability === "object" && fd.availability !== null
                ? fd.availability
                : { status: "", note: fd?.availability || "" },
        budget:
            typeof fd?.budget === "object" && fd.budget !== null
                ? fd.budget
                : { status: "", value: fd?.budget || "" },
        work_links: fd?.work_links || [],
        custom_answers: fd?.custom_answers || {},
    };
}

function authHeaders(token) {
    return { headers: { Authorization: `Bearer ${token}` } };
}

export default function AdminAddSubmissionModal({ open, onClose, projectId, project, onCreated }) {
    // --- Step 1: talent search / create ---
    const [query, setQuery] = useState("");
    const [debouncedQuery, setDebouncedQuery] = useState("");
    const [results, setResults] = useState([]);
    const [searching, setSearching] = useState(false);
    const [showCreateTalent, setShowCreateTalent] = useState(false);
    const [newTalent, setNewTalent] = useState({ name: "", email: "", phone: "" });
    const [creatingTalent, setCreatingTalent] = useState(false);
    const abortRef = useRef(null);

    // --- Step 2: submission bootstrap ---
    const [starting, setStarting] = useState(false);
    const [selectedTalentName, setSelectedTalentName] = useState("");
    const [sid, setSid] = useState(null);
    const [token, setToken] = useState(null);
    const [wasResumed, setWasResumed] = useState(false);
    const [submission, setSubmission] = useState(null);
    const [form, setForm] = useState({});
    const [loadingSubmission, setLoadingSubmission] = useState(false);

    // --- Media ---
    const { uploadFile } = useUploadManager();
    const [uploadingCategory, setUploadingCategory] = useState(null);
    const introInputRef = useRef(null);
    const takeInputRef = useRef(null);
    const imageInputRef = useRef(null);

    // --- Finalize ---
    const [finalizing, setFinalizing] = useState(false);

    useEffect(() => {
        if (!open) return;
        // Reset everything each time the modal is opened fresh.
        setQuery("");
        setDebouncedQuery("");
        setResults([]);
        setShowCreateTalent(false);
        setNewTalent({ name: "", email: "", phone: "" });
        setSid(null);
        setToken(null);
        setSubmission(null);
        setForm({});
        setSelectedTalentName("");
    }, [open]);

    useEffect(() => {
        const t = setTimeout(() => setDebouncedQuery(query.trim()), 180);
        return () => clearTimeout(t);
    }, [query]);

    useEffect(() => {
        if (!open || sid) return;
        if (debouncedQuery.length < 2) {
            setResults([]);
            return;
        }
        if (abortRef.current) abortRef.current.abort();
        const controller = new AbortController();
        abortRef.current = controller;
        setSearching(true);
        adminApi
            .get("/talents/search", { params: { q: debouncedQuery }, signal: controller.signal })
            .then(({ data }) => setResults(data?.data || []))
            .catch((e) => {
                if (e.name !== "CanceledError" && e.name !== "AbortError") setResults([]);
            })
            .finally(() => setSearching(false));
        return () => controller.abort();
    }, [debouncedQuery, open, sid]);

    async function beginSubmission(talentId, talentName) {
        setStarting(true);
        try {
            const { data } = await adminApi.post(
                `/projects/${projectId}/talents/${talentId}/submissions/admin-start`
            );
            setSid(data.id);
            setToken(data.token);
            setWasResumed(!!data.resumed);
            setSelectedTalentName(data.talent_name || talentName || "");
            if (data.resumed) {
                toast.info("This talent already has a draft for this project — resuming it.");
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to start submission for this talent");
        } finally {
            setStarting(false);
        }
    }

    async function handleSelectTalent(t) {
        if (!t.email) {
            toast.error("This talent has no email on file — add one before creating a submission.");
            return;
        }
        await beginSubmission(t.id, t.name);
    }

    async function handleCreateTalent() {
        const name = newTalent.name.trim();
        const email = newTalent.email.trim();
        if (!name) {
            toast.error("Name is required");
            return;
        }
        if (!email) {
            toast.error("Email is required to create a submission");
            return;
        }
        setCreatingTalent(true);
        try {
            const { data } = await adminApi.post("/talents", {
                name,
                email,
                phone: newTalent.phone.trim() || undefined,
            });
            await beginSubmission(data.id, data.name);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to create talent");
        } finally {
            setCreatingTalent(false);
        }
    }

    // Fetch the full submission (form_data + media) once we have a token.
    useEffect(() => {
        if (!sid || !token) return;
        let alive = true;
        setLoadingSubmission(true);
        api
            .get(`/public/submissions/${sid}`, authHeaders(token))
            .then(({ data }) => {
                if (!alive) return;
                setSubmission(data);
                setForm(normalizeFormData(data.form_data || {}));
            })
            .catch(() => {
                if (alive) toast.error("Failed to load submission details");
            })
            .finally(() => {
                if (alive) setLoadingSubmission(false);
            });
        return () => {
            alive = false;
        };
    }, [sid, token]);

    function updateForm(patch) {
        setForm((f) => ({ ...f, ...patch }));
    }

    async function saveFormNow(latestForm) {
        if (!sid || !token) return;
        try {
            const { data } = await api.put(
                `/public/submissions/${sid}`,
                { form_data: latestForm },
                authHeaders(token)
            );
            setSubmission(data);
        } catch (e) {
            toast.error("Failed to save field");
        }
    }

    // Debounced autosave of the profile / project-question form.
    const saveTimerRef = useRef(null);
    useEffect(() => {
        if (!sid || !token || !submission) return;
        if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        saveTimerRef.current = setTimeout(() => saveFormNow(form), 500);
        return () => clearTimeout(saveTimerRef.current);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [form]);

    function handleUploadClick(category) {
        if (category === "intro_video") introInputRef.current?.click();
        else if (category === "take") takeInputRef.current?.click();
        else imageInputRef.current?.click();
    }

    async function handleFilesSelected(e, category) {
        const files = Array.from(e.target.files || []);
        e.target.value = "";
        if (files.length === 0 || !sid || !token) return;
        setUploadingCategory(category);
        try {
            for (const file of files) {
                let label;
                if (category === "take") {
                    const existingTakes = (submission?.media || []).filter((m) => m.category === "take");
                    label = `Take ${existingTakes.length + 1}`;
                }
                await uploadFile(file, category, label, {
                    endpoint: `/public/submissions/${sid}/upload`,
                    token,
                    onSuccess: (data) => setSubmission(data),
                });
            }
        } catch (e) {
            toast.error("Upload failed");
        } finally {
            setUploadingCategory(null);
        }
    }

    async function handleRemoveMedia(mediaId) {
        if (!sid) return;
        try {
            await adminApi.delete(`/projects/${projectId}/submissions/${sid}/media/${mediaId}`);
            setSubmission((s) => (s ? { ...s, media: (s.media || []).filter((m) => m.id !== mediaId) } : s));
        } catch (e) {
            toast.error("Failed to remove media");
        }
    }

    async function handleCreateSubmission() {
        if (!sid || !token) return;
        setFinalizing(true);
        try {
            // Persist the latest edits before finalizing.
            await saveFormNow(form);
            // Any newly-uploaded reusable media stays project-only by default —
            // never silently promoted to the talent's master profile.
            const pending = (submission?.media || []).some((m) => m.profile_sync_status === "pending");
            if (pending) {
                await api.post(
                    `/public/submissions/${sid}/media-consent`,
                    { decision: "only_this_project" },
                    authHeaders(token)
                );
            }
            await api.post(`/public/submissions/${sid}/finalize`, {}, authHeaders(token));
            toast.success("Submission created");
            onCreated?.(sid);
            onClose();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to create submission");
        } finally {
            setFinalizing(false);
        }
    }

    if (!open) return null;

    const items = submission
        ? computeRequirementItems({ project, form, submission })
        : [];
    const itemById = Object.fromEntries(items.map((it) => [it.id, it]));
    const requiredMark = (id) =>
        itemById[id]?.requirement === "required" ? <span className="text-rose-500">*</span> : null;

    const media = submission?.media || [];
    const introMedia = media.find((m) => m.category === "intro_video");
    const takeMedia = media.filter((m) => m.category === "take" || m.category === "take_1" || m.category === "take_2" || m.category === "take_3");
    const imageMedia = media.filter((m) => ["image", "indian", "western"].includes(m.category));

    const showLocation = !!itemById["location"];
    const showAvailability = !!itemById["availability"];
    const showBudget = !!itemById["budget"];
    const showCompetitiveBrand = !!itemById["competitive_brand"];
    const customQuestions = project?.custom_questions || [];

    return (
        <div className="fixed inset-0 z-[100] flex items-end sm:items-center justify-center bg-black/50 p-0 sm:p-4" data-testid="admin-add-submission-modal">
            <div className="bg-white w-full sm:max-w-2xl sm:rounded-2xl rounded-t-2xl shadow-2xl max-h-[92vh] sm:max-h-[88vh] flex flex-col overflow-hidden">
                {/* Header */}
                <div className="flex items-center justify-between px-4 sm:px-6 py-4 border-b border-black/[0.08] shrink-0">
                    <div>
                        <span className="text-[10px] uppercase tracking-wider text-black/40 font-semibold font-mono">Admin</span>
                        <h2 className="text-lg font-display font-semibold text-black/90">Add Submission</h2>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="p-2 rounded-full hover:bg-black/[0.04] text-black/50 hover:text-black/80"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="flex-1 overflow-y-auto px-4 sm:px-6 py-4 space-y-6">
                    {!sid && (
                        <div className="space-y-4">
                            <div>
                                <label className="text-xs font-semibold text-black/70 mb-1.5 block">
                                    Search for a talent by name, email, or phone
                                </label>
                                <div className="relative">
                                    <Search className="w-4 h-4 text-black/30 absolute left-3 top-1/2 -translate-y-1/2" />
                                    <input
                                        autoFocus
                                        type="search"
                                        value={query}
                                        onChange={(e) => setQuery(e.target.value)}
                                        placeholder="e.g. Jane Doe, jane@email.com, +971..."
                                        className="w-full pl-9 pr-3 py-2.5 text-sm border border-black/[0.12] focus:border-black/40 rounded-lg outline-none"
                                        data-testid="add-submission-talent-search"
                                    />
                                    {searching && <Loader2 className="w-4 h-4 animate-spin text-black/30 absolute right-3 top-1/2 -translate-y-1/2" />}
                                </div>
                            </div>

                            {results.length > 0 && (
                                <div className="border border-black/[0.08] rounded-lg divide-y divide-black/[0.06] max-h-64 overflow-y-auto" data-testid="add-submission-results">
                                    {results.map((t) => (
                                        <button
                                            key={t.id}
                                            type="button"
                                            onClick={() => handleSelectTalent(t)}
                                            disabled={starting}
                                            className="w-full text-left px-3 py-2.5 hover:bg-black/[0.03] transition-colors flex items-center justify-between gap-3 disabled:opacity-50"
                                        >
                                            <div className="min-w-0">
                                                <div className="text-sm font-medium text-black/90 truncate">{t.name}</div>
                                                <div className="text-xs text-black/45 truncate">{t.email || "no email"} {t.phone ? `· ${t.phone}` : ""}</div>
                                            </div>
                                            {starting ? <Loader2 className="w-4 h-4 animate-spin shrink-0" /> : null}
                                        </button>
                                    ))}
                                </div>
                            )}

                            {debouncedQuery.length >= 2 && !searching && results.length === 0 && (
                                <p className="text-xs text-black/45">No matching talents found.</p>
                            )}

                            <div className="pt-2 border-t border-black/[0.06]">
                                {!showCreateTalent ? (
                                    <button
                                        type="button"
                                        onClick={() => setShowCreateTalent(true)}
                                        className="flex items-center gap-1.5 text-xs font-semibold text-black/60 hover:text-black"
                                    >
                                        <UserPlus className="w-3.5 h-3.5" /> Can't find them? Create a new talent
                                    </button>
                                ) : (
                                    <div className="space-y-2.5 bg-[#fafaf9] border border-black/[0.08] rounded-lg p-3.5">
                                        <p className="text-xs font-semibold text-black/70">New talent</p>
                                        <input
                                            type="text"
                                            placeholder="Full name *"
                                            value={newTalent.name}
                                            onChange={(e) => setNewTalent((n) => ({ ...n, name: e.target.value }))}
                                            className="w-full px-3 py-2 text-sm border border-black/[0.12] rounded-lg outline-none focus:border-black/40"
                                        />
                                        <input
                                            type="email"
                                            placeholder="Email *"
                                            value={newTalent.email}
                                            onChange={(e) => setNewTalent((n) => ({ ...n, email: e.target.value }))}
                                            className="w-full px-3 py-2 text-sm border border-black/[0.12] rounded-lg outline-none focus:border-black/40"
                                        />
                                        <input
                                            type="tel"
                                            placeholder="Phone (optional)"
                                            value={newTalent.phone}
                                            onChange={(e) => setNewTalent((n) => ({ ...n, phone: e.target.value }))}
                                            className="w-full px-3 py-2 text-sm border border-black/[0.12] rounded-lg outline-none focus:border-black/40"
                                        />
                                        <div className="flex items-center gap-2 pt-1">
                                            <button
                                                type="button"
                                                onClick={handleCreateTalent}
                                                disabled={creatingTalent || starting}
                                                className="px-4 py-2 text-xs font-semibold bg-black text-white rounded-lg disabled:opacity-50 flex items-center gap-1.5"
                                            >
                                                {(creatingTalent || starting) && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                                                Create &amp; Continue
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => setShowCreateTalent(false)}
                                                className="px-3 py-2 text-xs font-semibold text-black/50 hover:text-black"
                                            >
                                                Cancel
                                            </button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    )}

                    {sid && (loadingSubmission || !submission) && (
                        <div className="flex items-center justify-center py-16 text-black/40 gap-2">
                            <Loader2 className="w-5 h-5 animate-spin" />
                            <span className="text-sm">Setting up submission for {selectedTalentName}…</span>
                        </div>
                    )}

                    {sid && submission && (
                        <div className="space-y-6" data-testid="add-submission-form">
                            <div className="flex items-center gap-2 text-sm">
                                <span className="font-semibold text-black/90">{selectedTalentName}</span>
                                {wasResumed && (
                                    <span className="text-[10px] uppercase font-mono px-2 py-0.5 bg-amber-50 text-amber-700 border border-amber-200 rounded-full">
                                        Resumed existing draft
                                    </span>
                                )}
                            </div>

                            {/* Profile */}
                            <section>
                                <h3 className="text-xs uppercase tracking-wider font-mono text-black/45 mb-2.5">Profile</h3>
                                <div className="grid grid-cols-2 gap-2.5">
                                    <Field label="First Name" mark={requiredMark("first_name")}>
                                        <input value={form.first_name || ""} onChange={(e) => updateForm({ first_name: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Last Name" mark={requiredMark("last_name")}>
                                        <input value={form.last_name || ""} onChange={(e) => updateForm({ last_name: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Phone" mark={requiredMark("phone")}>
                                        <input value={form.phone || ""} onChange={(e) => updateForm({ phone: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Date of Birth" mark={requiredMark("dob")}>
                                        <input type="date" value={form.dob || ""} onChange={(e) => updateForm({ dob: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Age" mark={requiredMark("age")}>
                                        <input type="number" value={form.age ?? ""} onChange={(e) => updateForm({ age: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Height" mark={requiredMark("height")}>
                                        <input value={form.height || ""} onChange={(e) => updateForm({ height: e.target.value })} className="tg-input" placeholder={`e.g. 5'8"`} />
                                    </Field>
                                    <Field label="Gender" mark={requiredMark("gender")}>
                                        <input value={form.gender || ""} onChange={(e) => updateForm({ gender: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Ethnicity" mark={requiredMark("ethnicity")}>
                                        <input value={form.ethnicity || ""} onChange={(e) => updateForm({ ethnicity: e.target.value })} className="tg-input" />
                                    </Field>
                                    <Field label="Instagram Handle" mark={requiredMark("instagram_handle")}>
                                        <input value={form.instagram_handle || ""} onChange={(e) => updateForm({ instagram_handle: e.target.value })} className="tg-input" placeholder="@handle" />
                                    </Field>
                                    <Field label="Instagram Followers" mark={requiredMark("instagram_followers")}>
                                        <input value={form.instagram_followers || ""} onChange={(e) => updateForm({ instagram_followers: e.target.value })} className="tg-input" />
                                    </Field>
                                </div>
                                <Field label="Work Links (one per line)" className="mt-2.5" mark={requiredMark("work_links")}>
                                    <textarea
                                        rows={2}
                                        value={(form.work_links || []).join("\n")}
                                        onChange={(e) => updateForm({ work_links: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })}
                                        className="tg-input"
                                    />
                                </Field>
                            </section>

                            {/* Project Details — only what THIS project actually configures */}
                            {(showLocation || showAvailability || showBudget || showCompetitiveBrand || customQuestions.length > 0) && (
                                <section>
                                    <h3 className="text-xs uppercase tracking-wider font-mono text-black/45 mb-2.5">Project Details</h3>
                                    <div className="space-y-3">
                                        {showLocation && (
                                            <Field label="Current Location" mark={requiredMark("location")}>
                                                <LocationSelector value={form.location || []} onChange={(loc) => updateForm({ location: loc })} />
                                            </Field>
                                        )}

                                        {showAvailability && (
                                            <Field label="Availability" mark={requiredMark("availability")}>
                                                <div className="flex flex-wrap gap-2">
                                                    {AVAILABILITY_OPTIONS.map((opt) => (
                                                        <button
                                                            key={opt.key}
                                                            type="button"
                                                            onClick={() => updateForm({ availability: { ...(form.availability || {}), status: opt.key } })}
                                                            className={`px-3 py-1.5 text-xs font-semibold rounded-full border transition-colors ${form.availability?.status === opt.key ? "bg-black text-white border-black" : "border-black/[0.12] text-black/60 hover:border-black/30"}`}
                                                        >
                                                            {opt.label}
                                                        </button>
                                                    ))}
                                                </div>
                                                {(form.availability?.status === "partial" || form.availability?.status === "no") && (
                                                    <input
                                                        value={form.availability?.note || ""}
                                                        onChange={(e) => updateForm({ availability: { ...(form.availability || {}), note: e.target.value } })}
                                                        placeholder="Details"
                                                        className="tg-input mt-2"
                                                    />
                                                )}
                                            </Field>
                                        )}

                                        {showBudget && (
                                            <Field label="Budget" mark={requiredMark("budget")}>
                                                <div className="flex flex-wrap gap-2">
                                                    {BUDGET_OPTIONS.map((opt) => (
                                                        <button
                                                            key={opt.key}
                                                            type="button"
                                                            onClick={() => updateForm({ budget: { ...(form.budget || {}), status: opt.key } })}
                                                            className={`px-3 py-1.5 text-xs font-semibold rounded-full border transition-colors ${form.budget?.status === opt.key ? "bg-black text-white border-black" : "border-black/[0.12] text-black/60 hover:border-black/30"}`}
                                                        >
                                                            {opt.label}
                                                        </button>
                                                    ))}
                                                </div>
                                                {form.budget?.status === "custom" && (
                                                    <input
                                                        value={form.budget?.value || ""}
                                                        onChange={(e) => updateForm({ budget: { ...(form.budget || {}), value: e.target.value } })}
                                                        placeholder="Expected budget"
                                                        className="tg-input mt-2"
                                                    />
                                                )}
                                            </Field>
                                        )}

                                        {showCompetitiveBrand && (
                                            <Field label="Competitive Brand Experience" mark={requiredMark("competitive_brand")}>
                                                <div className="flex gap-2">
                                                    <button
                                                        type="button"
                                                        onClick={() => updateForm({ has_competitive_brand_experience: false, competitive_brand: "" })}
                                                        className={`px-3 py-1.5 text-xs font-semibold rounded-full border transition-colors ${form.has_competitive_brand_experience === false ? "bg-black text-white border-black" : "border-black/[0.12] text-black/60 hover:border-black/30"}`}
                                                    >
                                                        None
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={() => updateForm({ has_competitive_brand_experience: true })}
                                                        className={`px-3 py-1.5 text-xs font-semibold rounded-full border transition-colors ${form.has_competitive_brand_experience === true ? "bg-black text-white border-black" : "border-black/[0.12] text-black/60 hover:border-black/30"}`}
                                                    >
                                                        Yes
                                                    </button>
                                                </div>
                                                {form.has_competitive_brand_experience === true && (
                                                    <input
                                                        value={form.competitive_brand || ""}
                                                        onChange={(e) => updateForm({ competitive_brand: e.target.value })}
                                                        placeholder="Which brands & when"
                                                        className="tg-input mt-2"
                                                    />
                                                )}
                                            </Field>
                                        )}

                                        {customQuestions.map((cq) => (
                                            <Field key={cq.id} label={cq.question} mark={requiredMark(`cq_${cq.id}`)}>
                                                <input
                                                    value={form.custom_answers?.[cq.id] || ""}
                                                    onChange={(e) =>
                                                        updateForm({ custom_answers: { ...(form.custom_answers || {}), [cq.id]: e.target.value } })
                                                    }
                                                    className="tg-input"
                                                />
                                            </Field>
                                        ))}
                                    </div>
                                </section>
                            )}

                            {/* Media */}
                            <section>
                                <h3 className="text-xs uppercase tracking-wider font-mono text-black/45 mb-2.5">Media</h3>
                                <div className="space-y-3">
                                    <MediaSlotGroup
                                        title="Introduction Video"
                                        mark={requiredMark("intro_video")}
                                        icon={<Video className="w-3.5 h-3.5" />}
                                        items={introMedia ? [introMedia] : []}
                                        onRemove={handleRemoveMedia}
                                        onAdd={() => handleUploadClick("intro_video")}
                                        busy={uploadingCategory === "intro_video"}
                                        singleSlot
                                    />
                                    <MediaSlotGroup
                                        title="Audition Take(s)"
                                        mark={requiredMark("takes")}
                                        icon={<Video className="w-3.5 h-3.5" />}
                                        items={takeMedia}
                                        onRemove={handleRemoveMedia}
                                        onAdd={() => handleUploadClick("take")}
                                        busy={uploadingCategory === "take"}
                                    />
                                    <MediaSlotGroup
                                        title="Portfolio Images"
                                        mark={requiredMark("portfolio_image")}
                                        icon={<ImageIcon className="w-3.5 h-3.5" />}
                                        items={imageMedia}
                                        onRemove={handleRemoveMedia}
                                        onAdd={() => handleUploadClick("image")}
                                        busy={uploadingCategory === "image"}
                                    />
                                </div>
                                <input ref={introInputRef} type="file" accept="video/*" hidden onChange={(e) => handleFilesSelected(e, "intro_video")} />
                                <input ref={takeInputRef} type="file" accept="video/*" hidden multiple onChange={(e) => handleFilesSelected(e, "take")} />
                                <input ref={imageInputRef} type="file" accept="image/*" hidden multiple onChange={(e) => handleFilesSelected(e, "image")} />
                            </section>
                        </div>
                    )}
                </div>

                {/* Footer */}
                {sid && submission && (
                    <div className="px-4 sm:px-6 py-3.5 border-t border-black/[0.08] flex items-center justify-end gap-2 shrink-0 bg-white">
                        <button type="button" onClick={onClose} className="px-4 py-2.5 text-xs font-semibold text-black/50 hover:text-black">
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={handleCreateSubmission}
                            disabled={finalizing}
                            data-testid="add-submission-create-btn"
                            className="px-5 py-2.5 text-xs font-semibold bg-black text-white rounded-lg disabled:opacity-50 flex items-center gap-1.5"
                        >
                            {finalizing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                            Create Submission
                        </button>
                    </div>
                )}
            </div>

            <style>{`
                .tg-input {
                    width: 100%;
                    padding: 0.5rem 0.75rem;
                    font-size: 0.8125rem;
                    border: 1px solid rgba(0,0,0,0.12);
                    border-radius: 0.5rem;
                    outline: none;
                    background: white;
                }
                .tg-input:focus { border-color: rgba(0,0,0,0.4); }
            `}</style>
        </div>
    );
}

function Field({ label, mark, children, className = "" }) {
    return (
        <div className={className}>
            <label className="text-[11px] font-medium text-black/55 mb-1 block">
                {label} {mark}
            </label>
            {children}
        </div>
    );
}

function MediaSlotGroup({ title, mark, icon, items, onRemove, onAdd, busy, singleSlot }) {
    return (
        <div className="border border-black/[0.08] rounded-lg p-3">
            <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-black/70 flex items-center gap-1.5">
                    {icon} {title} {mark}
                </span>
                {(!singleSlot || items.length === 0) && (
                    <button
                        type="button"
                        onClick={onAdd}
                        disabled={busy}
                        className="text-[11px] font-semibold text-black/60 hover:text-black flex items-center gap-1 disabled:opacity-50"
                    >
                        {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Upload className="w-3 h-3" />}
                        {items.length === 0 ? "Add" : "Add another"}
                    </button>
                )}
            </div>
            {items.length === 0 ? (
                <p className="text-[11px] text-black/35">None yet.</p>
            ) : (
                <div className="flex flex-wrap gap-2">
                    {items.map((m) => (
                        <div key={m.id} className="relative group w-16 h-16 rounded-md overflow-hidden bg-black/5 border border-black/[0.08]">
                            {m.category === "intro_video" || m.category === "take" || m.category === "take_1" || m.category === "take_2" || m.category === "take_3" ? (
                                <div className="w-full h-full flex items-center justify-center text-black/30">
                                    <Video className="w-5 h-5" />
                                </div>
                            ) : (
                                <img src={IMAGE_URL(m)} alt="" className="w-full h-full object-cover" />
                            )}
                            <button
                                type="button"
                                onClick={() => onRemove(m.id)}
                                className="absolute top-0.5 right-0.5 bg-black/60 hover:bg-black/80 text-white rounded-full p-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
                            >
                                <Trash2 className="w-2.5 h-2.5" />
                            </button>
                            {m.origin === "global" && (
                                <span className="absolute bottom-0 left-0 right-0 bg-black/60 text-white text-[8px] text-center py-0.5">Existing</span>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
