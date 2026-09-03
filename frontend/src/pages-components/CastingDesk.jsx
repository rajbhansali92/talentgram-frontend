import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import {
    Sparkles,
    Loader2,
    Upload,
    FileText,
    Image as ImageIcon,
    Music,
    Film,
    X,
    AlertTriangle,
    CheckCircle2,
    ArrowRight,
    RefreshCw,
    Trash2,
    ChevronLeft,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Separator } from "@/components/ui/separator";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";

const COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"];
const CATEGORY_ICON = { script: FileText, image: ImageIcon, audio: Music, video_file: Film };

const REQUIREMENT_TOGGLES = [
    { key: "location", label: "Current location" },
    { key: "availability", label: "Availability" },
    { key: "budget_expectation", label: "Expected budget / fee" },
    { key: "competitive_brand", label: "Competitive brand / conflict" },
];

const CONF_STYLE = {
    stated: { label: "AI: stated", variant: "secondary" },
    inferred: { label: "AI: inferred", variant: "outline" },
    missing: { label: "AI: not in brief", variant: "destructive" },
};

function ConfidenceChip({ conf }) {
    const s = CONF_STYLE[conf] || CONF_STYLE.missing;
    return <Badge variant={s.variant} className="ml-2 shrink-0 font-normal">{s.label}</Badge>;
}

/** A labelled, blur-saved field bound to a project_draft key. */
function DraftField({ label, dkey, value, conf, onSave, multiline, placeholder, type = "text" }) {
    const [local, setLocal] = useState(value ?? "");
    useEffect(() => setLocal(value ?? ""), [value]);
    const commit = () => {
        if ((local ?? "") !== (value ?? "")) onSave(dkey, local);
    };
    const Cmp = multiline ? Textarea : Input;
    return (
        <div className="space-y-1.5">
            <div className="flex items-center text-sm font-medium text-muted-foreground">
                <span>{label}</span>
                {conf ? <ConfidenceChip conf={conf} /> : null}
            </div>
            <Cmp
                type={type}
                value={local}
                placeholder={placeholder}
                onChange={(e) => setLocal(e.target.value)}
                onBlur={commit}
                rows={multiline ? 4 : undefined}
            />
        </div>
    );
}

export default function CastingDesk() {
    const navigate = useNavigate();
    const { sessionId } = useParams();

    const [session, setSession] = useState(null);
    const [recent, setRecent] = useState([]);
    const [health, setHealth] = useState(null);
    const [rawInput, setRawInput] = useState("");
    const [busy, setBusy] = useState(false);
    const [analysing, setAnalysing] = useState(false);
    const [approving, setApproving] = useState(false);
    const [approveResult, setApproveResult] = useState(null);
    const fileRef = useRef(null);

    const status = session?.status;
    const draft = session?.project_draft || null;
    const extraction = session?.extraction || null;
    const readiness = session?.readiness || null;

    // ---- data loading -----------------------------------------------------
    const loadRecent = useCallback(async () => {
        try {
            const { data } = await adminApi.get("/casting-desk/sessions", { params: { limit: 20 } });
            setRecent(data.data || []);
        } catch (e) {
            /* non-fatal */
        }
    }, []);

    const loadSession = useCallback(async (id) => {
        try {
            const { data } = await adminApi.get(`/casting-desk/sessions/${id}`);
            setSession(data);
            setRawInput(data.raw_input || "");
            setApproveResult(null);
        } catch (e) {
            toast.error("Could not load that Casting Desk session");
            navigate("/admin/casting-desk");
        }
    }, [navigate]);

    useEffect(() => {
        adminApi.get("/casting-desk/health").then(({ data }) => setHealth(data)).catch(() => {});
        loadRecent();
    }, [loadRecent]);

    useEffect(() => {
        if (sessionId) {
            loadSession(sessionId);
        } else {
            setSession(null);
            setRawInput("");
            setApproveResult(null);
        }
    }, [sessionId, loadSession]);

    // ---- actions --------------------------------------------------------
    const ensureSession = async () => {
        if (session) return session;
        const { data } = await adminApi.post("/casting-desk/sessions", { raw_input: rawInput });
        setSession(data);
        loadRecent();
        navigate(`/admin/casting-desk/${data.id}`, { replace: true });
        return data;
    };

    const saveRawInput = async () => {
        const s = await ensureSession();
        if ((s.raw_input || "") === rawInput) return;
        const { data } = await adminApi.patch(`/casting-desk/sessions/${s.id}`, { raw_input: rawInput });
        setSession(data);
    };

    const onUpload = async (e) => {
        const files = Array.from(e.target.files || []);
        e.target.value = "";
        if (!files.length) return;
        const s = await ensureSession();
        setBusy(true);
        try {
            for (const file of files) {
                const fd = new FormData();
                fd.append("file", file);
                const { data } = await adminApi.post(`/casting-desk/sessions/${s.id}/attachments`, fd);
                setSession(data);
            }
            toast.success("Material added");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Upload failed");
        } finally {
            setBusy(false);
        }
    };

    const removeAttachment = async (aid) => {
        try {
            const { data } = await adminApi.delete(`/casting-desk/sessions/${session.id}/attachments/${aid}`);
            setSession(data);
        } catch (err) {
            toast.error("Could not remove material");
        }
    };

    const analyse = async () => {
        const s = await ensureSession();
        if ((s.raw_input || "") !== rawInput) {
            await adminApi.patch(`/casting-desk/sessions/${s.id}`, { raw_input: rawInput });
        }
        setAnalysing(true);
        try {
            const { data } = await adminApi.post(`/casting-desk/sessions/${s.id}/analyse`);
            setSession(data);
            loadRecent();
            toast.success("AI has read the requirement");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "AI could not analyse this requirement");
        } finally {
            setAnalysing(false);
        }
    };

    const saveEdit = async (key, value) => {
        try {
            const { data } = await adminApi.patch(`/casting-desk/sessions/${session.id}/draft`, {
                edits: { [key]: value },
            });
            setSession(data);
        } catch (err) {
            toast.error("Could not save that change");
        }
    };

    const toggleRequirement = async (key, checked) => {
        const sr = JSON.parse(JSON.stringify(draft.submission_requirements || {}));
        if (key === "competitive_brand") {
            sr.fields.competitive_brand = checked ? "required" : "optional";
        } else {
            sr.fields[key] = checked ? "required" : "optional";
        }
        try {
            const edits = { submission_requirements: sr };
            if (key === "competitive_brand") edits.competitive_brand_enabled = checked;
            const { data } = await adminApi.patch(`/casting-desk/sessions/${session.id}/draft`, { edits });
            setSession(data);
        } catch (err) {
            toast.error("Could not update submission requirements");
        }
    };

    const approve = async () => {
        setApproving(true);
        try {
            const { data } = await adminApi.post(`/casting-desk/sessions/${session.id}/approve`);
            setApproveResult(data);
            setSession(data.session);
            loadRecent();
            if (data.material_failures > 0) {
                toast.warning(`Project created — ${data.material_failures} material(s) failed to attach`);
            } else {
                toast.success("Project created");
            }
        } catch (err) {
            const detail = err?.response?.data?.detail;
            const msg = typeof detail === "object" ? detail.message : detail;
            toast.error(msg || "Could not create the project");
        } finally {
            setApproving(false);
        }
    };

    const deleteSession = async () => {
        if (!session || !window.confirm("Delete this Casting Desk session? The pasted requirement and AI analysis will be lost.")) return;
        await adminApi.delete(`/casting-desk/sessions/${session.id}`);
        loadRecent();
        navigate("/admin/casting-desk");
    };

    const fieldConf = (k) => extraction?.fields?.[k]?.confidence;

    // ---- render --------------------------------------------------------
    const showReview = status === "analysed" || status === "creating_project" || status === "error";
    const showCreated = status === "project_created";

    return (
        <div className="mx-auto max-w-6xl p-4 md:p-8">
            <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h1 className="flex items-center gap-2 text-2xl font-bold">
                        <Sparkles className="h-6 w-6 text-primary" /> AI Casting Desk
                    </h1>
                    <p className="text-sm text-muted-foreground">
                        Paste a casting requirement — AI drafts a project, you review and approve, and it's created in the normal Projects system.
                        <span className="ml-1 font-medium">Gate 1 of 4.</span>
                    </p>
                </div>
                {session ? (
                    <Button variant="ghost" size="sm" asChild>
                        <Link to="/admin/casting-desk"><ChevronLeft className="h-4 w-4" /> New requirement</Link>
                    </Button>
                ) : null}
            </div>

            {health && !health.llm_configured ? (
                <div className="mb-4 flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                    <AlertTriangle className="h-4 w-4" />
                    AI is not configured on the backend (ANTHROPIC_API_KEY missing). Analysis will fail until an admin sets it.
                </div>
            ) : null}

            <div className="grid gap-6 lg:grid-cols-[1fr_260px]">
                <div className="space-y-6">
                    {/* ---- INTAKE ---- */}
                    {!showCreated ? (
                        <section className="rounded-lg border bg-card p-4 md:p-6">
                            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                                New Casting Requirement
                            </h2>
                            <Textarea
                                value={rawInput}
                                onChange={(e) => setRawInput(e.target.value)}
                                onBlur={saveRawInput}
                                disabled={showReview && status !== "error"}
                                rows={8}
                                placeholder="Paste the casting director's message here — brand, project, budget, dates, location, character, audition instructions…"
                            />

                            <div className="mt-4">
                                <div className="mb-2 flex items-center justify-between">
                                    <span className="text-sm font-medium text-muted-foreground">Materials</span>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => fileRef.current?.click()}
                                        disabled={busy || showReview}
                                    >
                                        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
                                        Add script / brief / audio / reference
                                    </Button>
                                    <input
                                        ref={fileRef}
                                        type="file"
                                        multiple
                                        className="hidden"
                                        accept=".pdf,.txt,image/*,audio/*,video/*"
                                        onChange={onUpload}
                                    />
                                </div>
                                <MaterialList
                                    items={session?.attachments || []}
                                    onRemove={showReview ? null : removeAttachment}
                                    audioAvailable={health?.audio_transcription}
                                />
                            </div>

                            {!showReview ? (
                                <div className="mt-5 flex items-center gap-3">
                                    <Button onClick={analyse} disabled={analysing || (!rawInput.trim() && !(session?.attachments || []).length)}>
                                        {analysing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                                        Analyse Requirement
                                    </Button>
                                    {session ? (
                                        <Button variant="ghost" size="sm" onClick={deleteSession}>
                                            <Trash2 className="h-4 w-4" /> Discard
                                        </Button>
                                    ) : null}
                                </div>
                            ) : null}
                        </section>
                    ) : null}

                    {/* ---- REVIEW (Gate 1) ---- */}
                    {showReview && draft ? (
                        <section className="rounded-lg border bg-card p-4 md:p-6">
                            <div className="mb-4 flex items-center justify-between">
                                <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                                    AI Understanding — review &amp; edit
                                </h2>
                                <Button variant="outline" size="sm" onClick={analyse} disabled={analysing}>
                                    {analysing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                                    Re-analyse
                                </Button>
                            </div>

                            {extraction?.summary ? (
                                <p className="mb-4 rounded-md bg-muted/50 p-3 text-sm">{extraction.summary}</p>
                            ) : null}

                            <div className="grid gap-4 md:grid-cols-2">
                                <DraftField label="Brand *" dkey="brand_name" value={draft.brand_name} conf={fieldConf("brand")} onSave={saveEdit} placeholder="Required" />
                                <div className="space-y-1.5">
                                    <div className="flex items-center text-sm font-medium text-muted-foreground">
                                        Commission <ConfidenceChip conf={fieldConf("commission")} />
                                    </div>
                                    <Select
                                        value={draft.commission_percent || "none"}
                                        onValueChange={(v) => saveEdit("commission_percent", v === "none" ? null : v)}
                                    >
                                        <SelectTrigger><SelectValue placeholder="Not specified" /></SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="none">Not specified</SelectItem>
                                            {COMMISSION_OPTIONS.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                                        </SelectContent>
                                    </Select>
                                </div>
                                <DraftField label="Shoot date(s)" dkey="shoot_dates" value={draft.shoot_dates} conf={fieldConf("shoot_date")} onSave={saveEdit} />
                                <DraftField label="Budget" dkey="budget_per_day" value={draft.budget_per_day} conf={fieldConf("budget")} onSave={saveEdit} />
                                <DraftField label="Medium / usage" dkey="medium_usage" value={draft.medium_usage} conf={fieldConf("medium")} onSave={saveEdit} />
                                <DraftField label="Director" dkey="director" value={draft.director} conf={fieldConf("director")} onSave={saveEdit} />
                                <DraftField label="Production house" dkey="production_house" value={draft.production_house} conf={fieldConf("production_house")} onSave={saveEdit} />
                            </div>

                            <div className="mt-4 grid gap-4">
                                <DraftField label="Character / talent requirement" dkey="character" value={draft.character} conf={fieldConf("character")} onSave={saveEdit} multiline />
                                <DraftField label="Additional details (audition date, location, instructions, references…)" dkey="additional_details" value={draft.additional_details} onSave={saveEdit} multiline />
                                <DraftField
                                    label="Reference video links (one per line)"
                                    dkey="video_links"
                                    value={(draft.video_links || []).join("\n")}
                                    onSave={(k, v) => saveEdit(k, v.split("\n").map((x) => x.trim()).filter(Boolean))}
                                    multiline
                                />
                            </div>

                            <Separator className="my-5" />
                            <h3 className="mb-2 text-sm font-medium text-muted-foreground">Submission requirements to make mandatory</h3>
                            <div className="grid grid-cols-2 gap-3">
                                {REQUIREMENT_TOGGLES.map((t) => {
                                    const on = (draft.submission_requirements?.fields?.[t.key] || "optional") === "required";
                                    return (
                                        <label key={t.key} className="flex items-center gap-2 text-sm">
                                            <Checkbox checked={on} onCheckedChange={(c) => toggleRequirement(t.key, !!c)} />
                                            {t.label}
                                        </label>
                                    );
                                })}
                            </div>

                            {/* AI flags / readiness */}
                            {readiness?.warnings?.length ? (
                                <div className="mt-5 rounded-md border border-amber-400/40 bg-amber-50 p-3 dark:bg-amber-950/30">
                                    <div className="mb-1 flex items-center gap-2 text-sm font-medium text-amber-700 dark:text-amber-400">
                                        <AlertTriangle className="h-4 w-4" /> AI flags — information not in the brief
                                    </div>
                                    <ul className="ml-6 list-disc text-sm text-amber-800 dark:text-amber-300">
                                        {readiness.warnings.map((w, i) => <li key={i}>{w.message}</li>)}
                                    </ul>
                                </div>
                            ) : null}

                            {status === "error" && session?.error ? (
                                <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                                    {session.error}
                                </div>
                            ) : null}

                            {readiness && !readiness.can_create ? (
                                <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                                    {readiness.blocking.map((b, i) => <div key={i}>{b}</div>)}
                                </div>
                            ) : null}

                            <div className="mt-5 flex items-center gap-3">
                                <Button onClick={approve} disabled={approving || (readiness && !readiness.can_create)}>
                                    {approving ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />}
                                    Approve &amp; Create Project
                                </Button>
                                <span className="text-xs text-muted-foreground">Creates a normal Talentgram project. Nothing is sent to talent.</span>
                            </div>
                        </section>
                    ) : null}

                    {/* ---- CREATED ---- */}
                    {showCreated ? (
                        <section className="rounded-lg border bg-card p-6 text-center">
                            <CheckCircle2 className="mx-auto mb-3 h-10 w-10 text-green-600" />
                            <h2 className="text-lg font-semibold">Project created</h2>
                            <p className="mt-1 text-sm text-muted-foreground">
                                {session?.project_draft?.brand_name} is now a normal project in Talentgram.
                            </p>
                            {approveResult?.materials?.length ? (
                                <ul className="mx-auto mt-3 max-w-sm text-left text-sm">
                                    {approveResult.materials.map((m) => (
                                        <li key={m.attachment_id} className="flex items-center gap-2">
                                            {m.ok ? <CheckCircle2 className="h-3.5 w-3.5 text-green-600" /> : <X className="h-3.5 w-3.5 text-destructive" />}
                                            {m.filename || m.category}
                                            {m.ok ? "" : <span className="text-destructive"> — failed, re-upload on the project</span>}
                                        </li>
                                    ))}
                                </ul>
                            ) : null}
                            <div className="mt-5 flex justify-center gap-3">
                                <Button asChild>
                                    <Link to={`/admin/projects/${session.project_id}`}>Open Project <ArrowRight className="h-4 w-4" /></Link>
                                </Button>
                                <Button variant="outline" asChild>
                                    <Link to="/admin/casting-desk">New requirement</Link>
                                </Button>
                            </div>
                        </section>
                    ) : null}
                </div>

                {/* ---- recent sessions ---- */}
                <aside className="space-y-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recent</h3>
                    {recent.length === 0 ? (
                        <p className="text-xs text-muted-foreground">No sessions yet.</p>
                    ) : (
                        recent.map((r) => (
                            <Link
                                key={r.id}
                                to={`/admin/casting-desk/${r.id}`}
                                className={`block rounded-md border p-2.5 text-sm hover:bg-accent ${r.id === session?.id ? "border-primary bg-accent" : ""}`}
                            >
                                <div className="flex items-center justify-between">
                                    <span className="truncate font-medium">{r.brand || "Untitled"}</span>
                                    <StatusBadge status={r.status} />
                                </div>
                                <p className="mt-0.5 truncate text-xs text-muted-foreground">{r.raw_input_preview || "—"}</p>
                            </Link>
                        ))
                    )}
                </aside>
            </div>
        </div>
    );
}

function StatusBadge({ status }) {
    const map = {
        draft: { label: "Draft", variant: "outline" },
        analysed: { label: "In review", variant: "secondary" },
        creating_project: { label: "Creating", variant: "secondary" },
        project_created: { label: "Created", variant: "default" },
        error: { label: "Error", variant: "destructive" },
    };
    const s = map[status] || map.draft;
    return <Badge variant={s.variant} className="ml-2 shrink-0 font-normal">{s.label}</Badge>;
}

function MaterialList({ items, onRemove, audioAvailable }) {
    if (!items.length) return <p className="text-xs text-muted-foreground">No materials attached.</p>;
    return (
        <ul className="space-y-1.5">
            {items.map((a) => {
                const Icon = CATEGORY_ICON[a.category] || FileText;
                const notTranscribed = a.category === "audio" && a.extraction_status === "unavailable";
                return (
                    <li key={a.id} className="flex items-center gap-2 rounded-md border bg-background p-2 text-sm">
                        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                        <span className="truncate">{a.original_filename || a.category}</span>
                        <Badge variant="outline" className="shrink-0 font-normal">{a.category}</Badge>
                        {a.extraction_status === "extracted" ? (
                            <Badge variant="secondary" className="shrink-0 font-normal">text read</Badge>
                        ) : null}
                        {notTranscribed ? (
                            <Badge variant="outline" className="shrink-0 font-normal text-amber-600">
                                not transcribed{audioAvailable ? "" : " (no Whisper key)"}
                            </Badge>
                        ) : null}
                        <span className="flex-1" />
                        {onRemove ? (
                            <button onClick={() => onRemove(a.id)} className="text-muted-foreground hover:text-destructive">
                                <X className="h-4 w-4" />
                            </button>
                        ) : null}
                    </li>
                );
            })}
        </ul>
    );
}
