import React, { useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
// FILE_URL import removed — media objects now carry a canonical `.url`
import Logo from "@/components/Logo";
import ThemeToggle from "@/components/ThemeToggle";
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
} from "lucide-react";
import {
    HEIGHT_OPTIONS,
    GENDER_OPTIONS,
    ETHNICITY_OPTIONS,
    FOLLOWER_TIERS,
    calcAge,
} from "@/lib/talentSchema";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const MAX_IMAGES = 8;
// Phase 3: per-category portfolio image cap. Each of `image`/`indian`/
// `western` is independently capped at this value, NOT combined.
const MAX_IMAGES_PER_CATEGORY = 10;
const LS_KEY = "tg_application";
// Draft expiry: local data (token + PII) is wiped after 30 days even if
// the user never finalises — defense-in-depth against stale tokens / stale PII.
const DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000;

export default function ApplicationPage() {
    // Flow: identity gate → sections 1-4 → submitted
    const [started, setStarted] = useState(false);
    const [aid, setAid] = useState(null);
    const [token, setToken] = useState(null);
    const [finalized, setFinalized] = useState(false);
    const [basics, setBasics] = useState({
        first_name: "",
        last_name: "",
        email: "",
        phone: "",
    });
    const [form, setForm] = useState({
        dob: "",
        age: "",
        height: "",
        gender: "",
        ethnicity: "",
        location: "",
        instagram_handle: "",
        instagram_followers: "",
        bio: "",
        work_links: [],
    });
    const [media, setMedia] = useState([]);
    const [uploading, setUploading] = useState(null);
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

    // Restore local draft
    useEffect(() => {
        const raw = localStorage.getItem(LS_KEY);
        if (!raw) return;
        try {
            const saved = JSON.parse(raw);
            // TTL guard — purge stale drafts (tokens + PII) after DRAFT_TTL_MS.
            if (saved.savedAt && Date.now() - saved.savedAt > DRAFT_TTL_MS) {
                localStorage.removeItem(LS_KEY);
                return;
            }
            if (saved.aid && saved.token) {
                setAid(saved.aid);
                setToken(saved.token);
                setStarted(true);
                setBasics(saved.basics || basics);
                setForm(saved.form || form);
                (async () => {
                    try {
                        const { data } = await axios.get(
                            `${API}/public/apply/${saved.aid}`,
                            { headers: { Authorization: `Bearer ${saved.token}` } },
                        );
                        setForm((f) => ({ ...f, ...(data.form_data || {}) }));
                        setMedia(data.media || []);
                        if (data.status === "submitted") setFinalized(true);
                    } catch {
                        // expired/invalid token — reset
                        localStorage.removeItem(LS_KEY);
                        setStarted(false);
                        setAid(null);
                        setToken(null);
                    }
                })();
            }
        } catch {
            localStorage.removeItem(LS_KEY);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const saveLocal = (patch = {}) => {
        localStorage.setItem(
            LS_KEY,
            JSON.stringify({
                aid,
                token,
                basics,
                form,
                ...patch,
                savedAt: Date.now(),
            }),
        );
    };

    const computedAge = useMemo(
        () => calcAge(form.dob) ?? (form.age ? parseInt(form.age, 10) : null),
        [form.dob, form.age],
    );

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
                `${API}/public/prefill?email=${encodeURIComponent(email)}`,
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
            location: f.location || d.location || "",
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
        // After the gate is unlocked, first/last name are required so we
        // can create an application document. If the gate was unlocked
        // through "Use this" on the prefill card, basics.first_name /
        // last_name are already populated from the saved profile; the
        // user just clicks Continue.
        if (!first_name.trim()) {
            toast.error("First name is required");
            return;
        }
        if (!last_name.trim()) {
            toast.error("Last name is required");
            return;
        }
        setSaving(true);
        try {
            const { data } = await axios.post(`${API}/public/apply`, basics);
            setAid(data.id);
            setToken(data.token);
            setStarted(true);
            if (data.resumed) toast.success("Welcome back — your application is resumed");
            localStorage.setItem(
                LS_KEY,
                JSON.stringify({
                    aid: data.id,
                    token: data.token,
                    basics,
                    form,
                    savedAt: Date.now(),
                }),
            );
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to start");
        } finally {
            setSaving(false);
        }
    };

    // Autosave form_data (debounced) once started
    useEffect(() => {
        if (!started || !aid || !token) return;
        const id = setTimeout(async () => {
            try {
                await axios.put(
                    `${API}/public/apply/${aid}`,
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
    }, [form, started, aid, token]);

    const upload = async (files, category) => {
        if (!files || !files.length) return;
        if (category === "image" || category === "indian" || category === "western") {
            // Phase 3 — per-category cap (10 each), not combined.
            const existing = media.filter((m) => m.category === category).length;
            const remaining = MAX_IMAGES_PER_CATEGORY - existing;
            if (remaining <= 0) {
                const label = category === "indian" ? "Indian look" : category === "western" ? "Western look" : "Portfolio";
                toast.error(`${label} image limit reached (${MAX_IMAGES_PER_CATEGORY})`);
                return;
            }
            files = Array.from(files).slice(0, remaining);
        }
        setUploading(category);
        try {
            for (const file of files) {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("category", category);
                const { data } = await axios.post(
                    `${API}/public/apply/${aid}/upload`,
                    fd,
                    {
                        headers: {
                            Authorization: `Bearer ${token}`,
                            "Content-Type": "multipart/form-data",
                        },
                    },
                );
                setMedia(data.media || []);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const removeMedia = async (mid) => {
        try {
            await axios.delete(`${API}/public/apply/${aid}/media/${mid}`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            setMedia((m) => m.filter((x) => x.id !== mid));
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to remove");
        }
    };

    const finalize = async () => {
        setSaving(true);
        try {
            // Final sync of form_data
            await axios.put(
                `${API}/public/apply/${aid}`,
                { form_data: form },
                { headers: { Authorization: `Bearer ${token}` } },
            );
            await axios.post(
                `${API}/public/apply/${aid}/finalize`,
                {},
                { headers: { Authorization: `Bearer ${token}` } },
            );
            setFinalized(true);
            localStorage.removeItem(LS_KEY);
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
                className="min-h-screen bg-[#0c0c0c] text-white"
                data-testid="application-identity-page"
            >
                <Header />
                <div className="max-w-xl mx-auto px-6 py-16 md:py-24">
                    <p className="eyebrow mb-3">Talent Application</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight mb-4">
                        Apply to join Talentgram
                    </h1>
                    <p className="text-white/60 text-sm mb-10 leading-relaxed">
                        Submit your portfolio once — get considered for every
                        brand, film, and campaign we cast. Takes about 5 minutes.
                    </p>
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
                                className="border border-[#c9a961]/40 bg-[#c9a961]/[0.06] p-4 rounded-sm flex flex-col sm:flex-row sm:items-center gap-3 justify-between"
                                data-testid="apply-prefill-card"
                            >
                                <div className="min-w-0 flex-1">
                                    <p className="text-sm text-white">
                                        We found your profile.{" "}
                                        <span className="text-white/60">
                                            Use saved details?
                                        </span>
                                    </p>
                                    <p className="text-[11px] text-white/40 tg-mono mt-1 truncate">
                                        {applyPrefill.data.first_name}{" "}
                                        {applyPrefill.data.last_name || ""}
                                        {applyPrefill.data.location ? ` · ${applyPrefill.data.location}` : ""}
                                        {applyPrefill.data.height ? ` · ${applyPrefill.data.height}` : ""}
                                    </p>
                                </div>
                                <div className="flex items-center gap-2">
                                    <button
                                        type="button"
                                        onClick={useApplyPrefill}
                                        data-testid="apply-prefill-use-btn"
                                        className="bg-white text-black px-4 py-2.5 text-xs rounded-sm hover:opacity-90 inline-flex items-center gap-1.5 min-h-[44px]"
                                    >
                                        <Check className="w-3.5 h-3.5" />
                                        Use this
                                    </button>
                                    <button
                                        type="button"
                                        onClick={dismissApplyPrefill}
                                        data-testid="apply-prefill-dismiss-btn"
                                        className="border border-white/20 text-white/70 hover:border-white px-4 py-2.5 text-xs rounded-sm inline-flex items-center gap-1.5 min-h-[44px]"
                                    >
                                        Edit manually
                                    </button>
                                </div>
                            </div>
                        )}

                        {emailGateUnlocked && (
                            <div className="space-y-5" data-testid="apply-identity-rest">
                                <Row
                                    label="First Name *"
                                    value={basics.first_name}
                                    onChange={(v) => setBasics({ ...basics, first_name: v })}
                                    testid="apply-first-name"
                                />
                                <Row
                                    label="Last Name *"
                                    value={basics.last_name}
                                    onChange={(v) => setBasics({ ...basics, last_name: v })}
                                    testid="apply-last-name"
                                />
                                <Row
                                    label="Phone (optional)"
                                    value={basics.phone}
                                    onChange={(v) => setBasics({ ...basics, phone: v })}
                                    testid="apply-phone"
                                />
                            </div>
                        )}
                    </div>
                    <button
                        onClick={startApplication}
                        disabled={saving || !emailGateUnlocked}
                        data-testid="apply-start-btn"
                        className="mt-10 inline-flex items-center gap-2 bg-white text-black px-6 py-3.5 rounded-sm text-sm font-medium hover:opacity-90 disabled:opacity-40"
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
        );
    }

    // --- Finalized ----------------------------------------------------------
    if (finalized) {
        return (
            <div
                className="min-h-screen bg-[#0c0c0c] text-white flex flex-col"
                data-testid="application-success-page"
            >
                <Header />
                <div className="flex-1 flex items-center justify-center p-6">
                    <div className="max-w-md text-center">
                        <div className="w-14 h-14 rounded-full bg-[#34C759]/20 text-[#34C759] inline-flex items-center justify-center mb-6">
                            <Check className="w-6 h-6" />
                        </div>
                        <p className="eyebrow mb-3">Submitted</p>
                        <h1 className="font-display text-3xl md:text-4xl tracking-tight mb-4">
                            Thank you, {basics.first_name}
                        </h1>
                        <p className="text-white/60 text-sm leading-relaxed mb-6">
                            Your application has been received. Our team
                            reviews every profile. If shortlisted, we'll reach
                            out to{" "}
                            <span className="text-white tg-mono">
                                {basics.email}
                            </span>
                            .
                        </p>
                        <div className="inline-flex items-center gap-2 text-xs text-white/50 tg-mono">
                            <Mail className="w-3 h-3" />
                            Check your inbox for updates.
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // --- Main form ---------------------------------------------------------
    return (
        <div
            className="min-h-screen bg-[#0c0c0c] text-white"
            data-testid="application-form-page"
        >
            <Header />
            <div className="max-w-3xl mx-auto px-6 py-10 md:py-16">
                <p className="eyebrow mb-3">Application · {basics.email}</p>
                <h1 className="font-display text-3xl md:text-5xl tracking-tight mb-8">
                    Your Profile
                </h1>

                {/* Section 2 — Profile Details */}
                <Section title="Profile Details" index="01">
                    <div className="grid md:grid-cols-2 gap-5">
                        <Row
                            label="Date of Birth *"
                            type="date"
                            value={form.dob}
                            onChange={(v) => setForm({ ...form, dob: v })}
                            testid="form-dob"
                        />
                        <div>
                            <Label>
                                Age {form.dob ? "(auto)" : ""}
                            </Label>
                            <div className="mt-2 py-2.5 text-sm tg-mono text-white/70 border-b border-white/15">
                                {computedAge ?? "—"}
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
                                    className="mt-2 bg-transparent border-0 border-b border-white/15 rounded-none px-0 h-auto py-2.5 text-sm"
                                    data-testid="form-height"
                                >
                                    <SelectValue placeholder="Select height" />
                                </SelectTrigger>
                                <SelectContent>
                                    {HEIGHT_OPTIONS.map((h) => (
                                        <SelectItem key={h} value={h}>
                                            {h}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>

                        <div>
                            <Label>Current Location *</Label>
                            <input
                                value={form.location}
                                onChange={(e) =>
                                    setForm({ ...form, location: e.target.value })
                                }
                                placeholder="City, Country"
                                data-testid="form-location"
                                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </div>

                        <div className="md:col-span-2">
                            <Label>Gender *</Label>
                            <div className="mt-3 flex flex-wrap gap-2">
                                {GENDER_OPTIONS.map((g) => (
                                    <button
                                        key={g.key}
                                        type="button"
                                        onClick={() =>
                                            setForm({ ...form, gender: g.key })
                                        }
                                        data-testid={`form-gender-${g.key}`}
                                        className={`px-4 py-2 rounded-full border text-xs tracking-widest uppercase transition-all ${
                                            form.gender === g.key
                                                ? "border-white bg-white text-black"
                                                : "border-white/20 hover:border-white/50"
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
                    <div className="grid md:grid-cols-2 gap-5">
                        <div>
                            <Label>Instagram Handle</Label>
                            <input
                                value={form.instagram_handle}
                                onChange={(e) =>
                                    setForm({
                                        ...form,
                                        instagram_handle: e.target.value,
                                    })
                                }
                                placeholder="@yourhandle"
                                data-testid="form-instagram"
                                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </div>
                        <div>
                            <Label>Instagram Followers</Label>
                            <Select
                                value={form.instagram_followers}
                                onValueChange={(v) =>
                                    setForm({ ...form, instagram_followers: v })
                                }
                            >
                                <SelectTrigger
                                    className="mt-2 bg-transparent border-0 border-b border-white/15 rounded-none px-0 h-auto py-2.5 text-sm"
                                    data-testid="form-followers"
                                >
                                    <SelectValue placeholder="Select range" />
                                </SelectTrigger>
                                <SelectContent className="max-h-72">
                                    {FOLLOWER_TIERS.map((tier) => (
                                        <SelectGroup key={tier.label}>
                                            <SelectLabel className="text-[10px] tracking-wide uppercase text-white/40">
                                                {tier.label}
                                            </SelectLabel>
                                            {tier.items.map((it) => (
                                                <SelectItem key={it} value={it}>
                                                    {it}
                                                </SelectItem>
                                            ))}
                                            <SelectSeparator />
                                        </SelectGroup>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div>
                            <Label>Ethnicity</Label>
                            <Select
                                value={form.ethnicity}
                                onValueChange={(v) =>
                                    setForm({ ...form, ethnicity: v })
                                }
                            >
                                <SelectTrigger
                                    className="mt-2 bg-transparent border-0 border-b border-white/15 rounded-none px-0 h-auto py-2.5 text-sm"
                                    data-testid="form-ethnicity"
                                >
                                    <SelectValue placeholder="Select ethnicity" />
                                </SelectTrigger>
                                <SelectContent className="max-h-72">
                                    {ETHNICITY_OPTIONS.map((e) => (
                                        <SelectItem
                                            key={e.key}
                                            value={e.key}
                                            data-testid={`form-ethnicity-${e.key}`}
                                        >
                                            {e.label}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="md:col-span-2">
                            <Label>Short Bio</Label>
                            <textarea
                                value={form.bio}
                                onChange={(e) =>
                                    setForm({ ...form, bio: e.target.value })
                                }
                                rows={4}
                                placeholder="A few lines about yourself — experience, strengths, what you're looking for."
                                data-testid="form-bio"
                                className="mt-2 w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none"
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

                {/* Section 4 — Media */}
                <Section title="Media" index="03">
                    <div className="space-y-6">
                        <div>
                            <div className="flex items-center justify-between mb-3">
                                <p className="text-sm text-white/80">
                                    Introduction Video <span className="text-white/40 text-xs ml-1">(optional)</span>
                                </p>
                                {intro && (
                                    <button
                                        onClick={() => removeMedia(intro.id)}
                                        className="text-[10px] tg-mono text-white/50 hover:text-[#FF3B30]"
                                    >
                                        Replace
                                    </button>
                                )}
                            </div>
                            <p className="text-xs text-white/40 mb-3">
                                Optional (recommended to improve selection chances). Your most recent professional introduction video, without contact info.
                            </p>
                            {intro ? (
                                <video
                                    src={intro.url}
                                    controls
                                    preload="metadata"
                                    className="w-full max-w-lg border border-white/10 bg-black rounded-sm"
                                    data-testid="apply-intro-preview"
                                />
                            ) : (
                                <button
                                    onClick={() => videoRef.current?.click()}
                                    data-testid="apply-intro-upload-btn"
                                    disabled={uploading === "intro_video"}
                                    className="w-full max-w-lg border border-dashed border-white/20 hover:border-white/50 py-10 flex flex-col items-center gap-2 text-sm text-white/60"
                                >
                                    {uploading === "intro_video" ? (
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

                        {/* Phase 2 — optional Indian/Western look images */}
                        <ApplyLookGroup
                            label="Indian Look (optional)"
                            hint="Saree, lehenga, sherwani, or traditional/Indian-look references."
                            items={indianImages}
                            category="indian"
                            allCount={indianImages.length}
                            maxImages={MAX_IMAGES_PER_CATEGORY}
                            inputRef={indianRef}
                            upload={upload}
                            removeMedia={removeMedia}
                            uploading={uploading}
                            testidPrefix="indian"
                        />
                        <ApplyLookGroup
                            label="Western Look (optional)"
                            hint="Casual, formal or western-styled references."
                            items={westernImages}
                            category="western"
                            allCount={westernImages.length}
                            maxImages={MAX_IMAGES_PER_CATEGORY}
                            inputRef={westernRef}
                            upload={upload}
                            removeMedia={removeMedia}
                            uploading={uploading}
                            testidPrefix="western"
                        />

                        <div>
                            <div className="flex items-center justify-between mb-3">
                                <p className="text-sm text-white/80">
                                    Profile / Headshot Image * <span className="text-white/40 text-xs ml-1">({images.length}/{MAX_IMAGES_PER_CATEGORY})</span>
                                </p>
                                {images.length < MAX_IMAGES_PER_CATEGORY && (
                                    <button
                                        onClick={() => imgRef.current?.click()}
                                        data-testid="apply-image-upload-btn"
                                        disabled={uploading === "image"}
                                        className="inline-flex items-center gap-1.5 text-xs border border-white/15 hover:border-white px-3 py-1.5"
                                    >
                                        {uploading === "image" ? (
                                            <Loader2 className="w-3 h-3 animate-spin" />
                                        ) : (
                                            <Upload className="w-3 h-3" />
                                        )}
                                        Add
                                    </button>
                                )}
                            </div>
                            <p className="text-xs text-white/40 mb-3">
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
                                    className="w-full border border-dashed border-white/20 hover:border-white/50 py-10 flex flex-col items-center gap-2 text-sm text-white/60"
                                >
                                    <Camera className="w-5 h-5" />
                                    <span>Upload images</span>
                                </button>
                            ) : (
                                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
                                    {images.map((m) => (
                                        <div
                                            key={m.id}
                                            className="relative aspect-[3/4] bg-[#0a0a0a] border border-white/10 overflow-hidden group"
                                        >
                                            <img
                                                src={m.url}
                                                alt=""
                                                loading="lazy"
                                                className="w-full h-full object-cover"
                                            />
                                            <button
                                                onClick={() => removeMedia(m.id)}
                                                className="absolute top-2 right-2 w-7 h-7 bg-black/60 opacity-0 group-hover:opacity-100 flex items-center justify-center hover:bg-[#FF3B30]"
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

                <button
                    onClick={finalize}
                    disabled={saving}
                    data-testid="apply-submit-btn"
                    className="mt-6 w-full bg-white text-black py-4 rounded-sm text-sm font-medium hover:opacity-90 disabled:opacity-40 inline-flex items-center justify-center gap-2"
                >
                    {saving ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                        <Sparkles className="w-4 h-4" />
                    )}
                    Submit Application
                </button>
                <p className="text-[11px] text-white/40 tg-mono text-center mt-3">
                    We'll auto-save as you go — feel free to come back and
                    finish later.
                </p>
            </div>
        </div>
    );
}

function Header() {
    return (
        <header className="flex items-center justify-between px-6 py-5 border-b border-white/10">
            <Logo className="h-8" />
            <ThemeToggle />
        </header>
    );
}

function Section({ title, index, children }) {
    return (
        <section className="mb-10 border border-white/10 p-6 md:p-8">
            <div className="flex items-center gap-3 mb-6">
                <span className="text-[10px] tg-mono text-white/40">
                    {index}
                </span>
                <p className="eyebrow">{title}</p>
            </div>
            {children}
        </section>
    );
}

function Label({ children }) {
    return (
        <label className="text-[11px] text-white/50 tracking-widest uppercase">
            {children}
        </label>
    );
}

function Row({ label, value, onChange, onBlur, type = "text", testid, hint }) {
    return (
        <div>
            <Label>{label}</Label>
            <input
                type={type}
                value={value}
                onChange={(e) => onChange(e.target.value)}
                onBlur={onBlur}
                data-testid={testid}
                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
            />
            {hint && (
                <p className="text-[10px] text-white/40 tg-mono mt-1.5">
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
    uploading,
    testidPrefix,
}) {
    const isUploading = uploading === category;
    const reachedCap = allCount >= maxImages;
    return (
        <div className="mb-2" data-testid={`apply-look-group-${testidPrefix}`}>
            <div className="flex items-center justify-between mb-1">
                <p className="text-sm text-white/80">{label}</p>
                <span className="text-[10px] tg-mono text-white/40">
                    {items.length}
                </span>
            </div>
            {hint && (
                <p className="text-xs text-white/40 mb-2">{hint}</p>
            )}
            <input
                ref={inputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => upload(e.target.files, category)}
            />
            <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                {items.map((m) => (
                    <div
                        key={m.id}
                        data-testid={`${testidPrefix}-image-${m.id}`}
                        className="relative aspect-[3/4] bg-[#0a0a0a] border border-white/10 overflow-hidden group"
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
                            className="absolute top-1 right-1 w-6 h-6 bg-black/60 opacity-0 group-hover:opacity-100 flex items-center justify-center hover:bg-[#FF3B30] rounded-sm"
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
                        className="aspect-[3/4] border border-dashed border-white/20 hover:border-white/50 flex flex-col items-center justify-center gap-1 text-xs text-white/60 disabled:opacity-50"
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

function ApplyWorkLinksEditor({ links, onChange }) {
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
        <div className="mt-2 space-y-2" data-testid="apply-work-links-editor">
            {(links || []).map((w, i) => (
                <div
                    key={`${w}-${i}`}
                    className="flex items-center justify-between gap-2 px-3 py-2 border border-white/10 rounded-sm text-xs tg-mono break-all"
                    data-testid={`apply-work-link-row-${i}`}
                >
                    <span className="truncate text-white/80">{w}</span>
                    <button
                        type="button"
                        onClick={() => remove(i)}
                        data-testid={`apply-work-link-remove-${i}`}
                        className="text-white/40 hover:text-white shrink-0"
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
                    data-testid="apply-work-link-input"
                    className="flex-1 bg-transparent border-b border-white/20 focus:border-white outline-none py-2 text-sm"
                />
                <button
                    type="button"
                    onClick={add}
                    data-testid="apply-work-link-add-btn"
                    className="text-xs px-3 py-2 border border-white/20 hover:border-white rounded-sm"
                >
                    Add
                </button>
            </div>
        </div>
    );
}
