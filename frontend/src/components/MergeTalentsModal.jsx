import React, { useState, useEffect, useCallback } from "react";
import { X, Loader2, AlertTriangle, GitMerge, CheckCircle2, ArrowRight } from "lucide-react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { formatTalentLocation } from "@/lib/sanitize";

// Fields shown side-by-side in the Review step (Part 3 of the spec) — one
// row per field, both profiles' raw values, no merge logic applied yet.
const REVIEW_FIELDS = [
    { key: "email", label: "Email" },
    { key: "phone", label: "Phone" },
    { key: "dob", label: "DOB" },
    { key: "height", label: "Height" },
    { key: "instagram_handle", label: "Instagram" },
    { key: "location", label: "Location", format: formatTalentLocation },
    { key: "gender", label: "Gender" },
    { key: "skills", label: "Skills", format: (v) => (v || []).join(", ") },
    { key: "tags", label: "Tags", format: (v) => (v || []).map((t) => t.name).join(", ") },
];

function fieldValue(talent, field) {
    const raw = talent?.[field.key];
    const display = field.format ? field.format(raw) : raw;
    return display || display === 0 ? String(display) : "—";
}

function StepBadge({ n, active, done, label }) {
    return (
        <div className="flex items-center gap-1.5 shrink-0">
            <div
                className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-medium ${
                    done ? "bg-foreground text-background" : active ? "border border-foreground text-foreground" : "border border-border text-muted-foreground"
                }`}
            >
                {done ? <CheckCircle2 className="w-3 h-3" /> : n}
            </div>
            <span className={`text-[11px] tg-mono uppercase tracking-wide ${active ? "text-foreground" : "text-muted-foreground"}`}>{label}</span>
        </div>
    );
}

/**
 * Multi-step admin-controlled Talent merge wizard, launched from the Global
 * Talents roster's bulk-select bar once exactly 2 talents are selected.
 *
 *  <MergeTalentsModal
 *    open={showMergeModal}
 *    talentAId={ids[0]} talentBId={ids[1]}
 *    onClose={() => setShowMergeModal(false)}
 *    onSuccess={() => { clear(); refetch(); }}
 *  />
 *
 * Flow: review (side-by-side + choose canonical) -> preview (computed field
 * diff, from the DB, never guessed) -> confirm (strong, type-to-confirm
 * gate) -> success. Every step reads from `POST /talents/merge/preview`
 * (read-only); only the final confirm step calls `POST /talents/merge`.
 */
export default function MergeTalentsModal({ open, talentAId, talentBId, onClose, onSuccess }) {
    const [step, setStep] = useState("loading"); // loading | review | preview | confirm | success | error
    const [data, setData] = useState(null); // preview response (talent_a/talent_b/recommendation/conflicts)
    const [canonicalId, setCanonicalId] = useState(null);
    const [plan, setPlan] = useState(null); // merge_plan for the chosen direction
    const [busy, setBusy] = useState(false);
    const [errorMsg, setErrorMsg] = useState(null);
    const [confirmTyped, setConfirmTyped] = useState("");
    const [result, setResult] = useState(null);

    const loadReview = useCallback(async () => {
        setStep("loading");
        setErrorMsg(null);
        try {
            const { data: resp } = await adminApi.post("/talents/merge/preview", {
                talent_a_id: talentAId, talent_b_id: talentBId,
            });
            setData(resp);
            setCanonicalId(resp.recommended_canonical_id);
            setStep("review");
        } catch (e) {
            setErrorMsg(formatErrorDetail(e, "Could not load these talents for comparison"));
            setStep("error");
        }
    }, [talentAId, talentBId]);

    useEffect(() => {
        if (open && talentAId && talentBId) {
            setConfirmTyped("");
            setResult(null);
            loadReview();
        }
    }, [open, talentAId, talentBId, loadReview]);

    if (!open) return null;

    const talentA = data?.talent_a;
    const talentB = data?.talent_b;
    const canonical = canonicalId === talentA?.id ? talentA : talentB;
    const duplicate = canonicalId === talentA?.id ? talentB : talentA;

    const loadPreviewPlan = async () => {
        setBusy(true);
        try {
            const { data: resp } = await adminApi.post("/talents/merge/preview", {
                talent_a_id: talentAId, talent_b_id: talentBId, canonical_id: canonicalId,
            });
            setPlan(resp.merge_plan);
            setStep("preview");
        } catch (e) {
            toast.error(formatErrorDetail(e, "Could not compute the merge preview"));
        } finally {
            setBusy(false);
        }
    };

    const runMerge = async () => {
        if (busy) return;
        setBusy(true);
        try {
            const { data: resp } = await adminApi.post("/talents/merge", {
                canonical_talent_id: canonical.id, duplicate_talent_id: duplicate.id,
            });
            setResult(resp);
            setStep("success");
        } catch (e) {
            toast.error(formatErrorDetail(e, "Merge failed"));
        } finally {
            setBusy(false);
        }
    };

    const hasIdentityConflicts = (data?.identity_conflicts || []).length > 0;
    const requireTypedConfirm = hasIdentityConflicts;
    const confirmEnabled = !requireTypedConfirm || confirmTyped.trim().toUpperCase() === "MERGE";

    return (
        <div
            className="fixed inset-0 z-[60] bg-black/70 backdrop-blur flex items-center justify-center p-4"
            data-testid="merge-talents-modal"
        >
            <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto border border-border bg-background rounded-sm relative">
                <div className="sticky top-0 bg-background border-b border-border px-6 py-4 flex items-center justify-between z-10">
                    <div className="flex items-center gap-3">
                        <GitMerge className="w-4 h-4 text-muted-foreground" />
                        <h3 className="font-display text-xl leading-tight">Merge Talent Profiles</h3>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-muted-foreground hover:text-foreground"
                        data-testid="merge-modal-close"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {step !== "loading" && step !== "error" && step !== "success" && (
                    <div className="px-6 pt-4 flex items-center gap-4 flex-wrap">
                        <StepBadge n={1} label="Review" active={step === "review"} done={step === "preview" || step === "confirm"} />
                        <ArrowRight className="w-3 h-3 text-muted-foreground" />
                        <StepBadge n={2} label="Preview" active={step === "preview"} done={step === "confirm"} />
                        <ArrowRight className="w-3 h-3 text-muted-foreground" />
                        <StepBadge n={3} label="Confirm" active={step === "confirm"} done={false} />
                    </div>
                )}

                <div className="p-6">
                    {step === "loading" && (
                        <div className="flex items-center justify-center py-12 text-muted-foreground">
                            <Loader2 className="w-5 h-5 animate-spin" />
                        </div>
                    )}

                    {step === "error" && (
                        <div className="text-center py-8">
                            <AlertTriangle className="w-6 h-6 text-[var(--tg-danger)] mx-auto mb-3" />
                            <p className="text-sm text-muted-foreground">{errorMsg}</p>
                        </div>
                    )}

                    {step === "review" && talentA && talentB && (
                        <div data-testid="merge-step-review">
                            {(talentA.status === "MERGED" || talentB.status === "MERGED") && (
                                <div className="mb-4 p-3 border border-[var(--tg-danger)]/40 bg-[var(--tg-danger)]/5 text-xs text-[var(--tg-danger)] rounded-sm">
                                    One of these talents is already marked MERGED. Refresh the roster before continuing.
                                </div>
                            )}
                            <div className="grid grid-cols-2 gap-4 mb-5">
                                {[talentA, talentB].map((t) => (
                                    <div key={t.id} className="text-center">
                                        <p className="font-display text-base truncate">{t.name}</p>
                                        <p className="text-[10px] tg-mono text-muted-foreground truncate">{t.id}</p>
                                    </div>
                                ))}
                            </div>

                            <div className="border border-border rounded-sm divide-y divide-border mb-5">
                                {REVIEW_FIELDS.map((f) => (
                                    <div key={f.key} className="grid grid-cols-[80px_1fr_1fr] gap-2 px-3 py-2 text-xs">
                                        <span className="text-muted-foreground">{f.label}</span>
                                        <span className="truncate">{fieldValue(talentA, f)}</span>
                                        <span className="truncate">{fieldValue(talentB, f)}</span>
                                    </div>
                                ))}
                                <div className="grid grid-cols-[80px_1fr_1fr] gap-2 px-3 py-2 text-xs">
                                    <span className="text-muted-foreground">Media</span>
                                    <span>{talentA.media_count}</span>
                                    <span>{talentB.media_count}</span>
                                </div>
                                <div className="grid grid-cols-[80px_1fr_1fr] gap-2 px-3 py-2 text-xs">
                                    <span className="text-muted-foreground">Submissions</span>
                                    <span>{talentA.relationship_counts?.submissions ?? 0}</span>
                                    <span>{talentB.relationship_counts?.submissions ?? 0}</span>
                                </div>
                            </div>

                            {hasIdentityConflicts && (
                                <div className="mb-5 p-3 border border-amber-500/40 bg-amber-500/5 rounded-sm">
                                    <p className="text-xs font-medium text-amber-600 mb-1.5 flex items-center gap-1.5">
                                        <AlertTriangle className="w-3.5 h-3.5" /> Identity fields differ
                                    </p>
                                    <ul className="text-[11px] text-muted-foreground space-y-0.5">
                                        {data.identity_conflicts.map((c) => (
                                            <li key={c.field}>
                                                <span className="tg-mono">{c.field}</span>: "{c.canonical}" vs "{c.other}"
                                            </li>
                                        ))}
                                    </ul>
                                    <p className="text-[11px] text-muted-foreground mt-1.5">
                                        You can still merge — the profile you keep decides which value survives.
                                    </p>
                                </div>
                            )}

                            <p className="text-[11px] tracking-widest uppercase text-muted-foreground mb-2">
                                Which profile should remain?
                            </p>
                            <div className="space-y-2 mb-6">
                                {[talentA, talentB].map((t) => (
                                    <label
                                        key={t.id}
                                        className={`flex items-center gap-3 border rounded-sm px-3 py-2.5 cursor-pointer ${
                                            canonicalId === t.id ? "border-foreground bg-foreground/5" : "border-border hover:border-foreground/40"
                                        }`}
                                        data-testid={`merge-canonical-choice-${t.id}`}
                                    >
                                        <input
                                            type="radio"
                                            name="canonical"
                                            checked={canonicalId === t.id}
                                            onChange={() => setCanonicalId(t.id)}
                                        />
                                        <div className="min-w-0 flex-1">
                                            <p className="text-sm truncate">{t.name}</p>
                                            <p className="text-[10px] tg-mono text-muted-foreground truncate">{t.id}</p>
                                        </div>
                                        {data.recommended_canonical_id === t.id && (
                                            <span className="text-[10px] tg-mono uppercase text-emerald-600 shrink-0">
                                                Recommended — {data.recommendation_reason}
                                            </span>
                                        )}
                                    </label>
                                ))}
                            </div>

                            <div className="flex gap-2">
                                <button type="button" onClick={onClose} className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm">
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    onClick={loadPreviewPlan}
                                    disabled={busy}
                                    className="flex-1 bg-foreground text-background py-2.5 rounded-sm text-sm inline-flex items-center justify-center gap-2 disabled:opacity-40"
                                    data-testid="merge-continue-to-preview"
                                >
                                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                                    Continue
                                </button>
                            </div>
                        </div>
                    )}

                    {step === "preview" && plan && canonical && duplicate && (
                        <div data-testid="merge-step-preview">
                            <p className="eyebrow mb-1">Merge Preview</p>
                            <p className="text-xs text-muted-foreground mb-5">
                                Exactly what will happen — computed from the current database state.
                            </p>

                            <div className="grid grid-cols-2 gap-3 mb-5">
                                <div className="border border-emerald-500/30 bg-emerald-500/5 rounded-sm p-3">
                                    <p className="text-[10px] tg-mono uppercase text-emerald-600 mb-1">Keeping</p>
                                    <p className="text-sm truncate">{canonical.name}</p>
                                    <p className="text-[10px] tg-mono text-muted-foreground truncate">{canonical.id}</p>
                                </div>
                                <div className="border border-[var(--tg-danger)]/30 bg-[var(--tg-danger)]/5 rounded-sm p-3">
                                    <p className="text-[10px] tg-mono uppercase text-[var(--tg-danger)] mb-1">Merging into this profile</p>
                                    <p className="text-sm truncate">{duplicate.name}</p>
                                    <p className="text-[10px] tg-mono text-muted-foreground truncate">{duplicate.id}</p>
                                </div>
                            </div>

                            {Object.keys(plan.field_changes).length > 0 && (
                                <div className="mb-5">
                                    <p className="text-[11px] tracking-widest uppercase text-muted-foreground mb-2">Profile Data</p>
                                    <div className="border border-border rounded-sm divide-y divide-border">
                                        {Object.entries(plan.field_changes).map(([field, ch]) => (
                                            <div key={field} className="px-3 py-2 text-xs" data-testid={`merge-field-change-${field}`}>
                                                <p className="tg-mono text-muted-foreground mb-1">{field}</p>
                                                <div className="grid grid-cols-3 gap-2">
                                                    <span className="truncate">Current: {String(ch.canonical ?? "—")}</span>
                                                    <span className="truncate">Incoming: {String(ch.other ?? "—")}</span>
                                                    <span className="truncate font-medium">Result: {Array.isArray(ch.proposed) ? ch.proposed.length : String(ch.proposed ?? "—")}</span>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {plan.conflicts.length > 0 && (
                                <div className="mb-5 p-3 border border-amber-500/40 bg-amber-500/5 rounded-sm">
                                    <p className="text-xs font-medium text-amber-600 mb-1.5">Conflict — canonical value will be retained</p>
                                    <ul className="text-[11px] text-muted-foreground space-y-0.5">
                                        {plan.conflicts.map((c) => (
                                            <li key={c.field}>
                                                <span className="tg-mono">{c.field}</span> — Profile kept: "{c.canonical}" · Other profile: "{c.other}"
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            )}

                            <div className="grid grid-cols-2 gap-3 mb-6">
                                <div className="border border-border rounded-sm p-3 text-xs">
                                    <p className="text-muted-foreground mb-1">Submissions</p>
                                    <p>Current: {plan.relationship_counts.canonical.submissions}</p>
                                    <p>Other: {plan.relationship_counts.other.submissions}</p>
                                    <p className="font-medium mt-1">After merge: {plan.proposed_submissions_total}</p>
                                </div>
                                <div className="border border-border rounded-sm p-3 text-xs">
                                    <p className="text-muted-foreground mb-1">Media</p>
                                    <p>Current: {plan.media.canonical_count}</p>
                                    <p>Other: {plan.media.other_count}</p>
                                    <p className="font-medium mt-1">After merge: {plan.media.proposed_count}</p>
                                </div>
                            </div>

                            <div className="flex gap-2">
                                <button type="button" onClick={() => setStep("review")} className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm">
                                    Back
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setStep("confirm")}
                                    className="flex-1 bg-foreground text-background py-2.5 rounded-sm text-sm"
                                    data-testid="merge-continue-to-confirm"
                                >
                                    Continue
                                </button>
                            </div>
                        </div>
                    )}

                    {step === "confirm" && canonical && duplicate && (
                        <div data-testid="merge-step-confirm">
                            <div className="flex gap-4 mb-5">
                                <div className="shrink-0 w-10 h-10 rounded-full bg-[var(--tg-danger)]/10 border border-[var(--tg-danger)]/25 flex items-center justify-center">
                                    <AlertTriangle className="w-4 h-4 text-[var(--tg-danger)]" />
                                </div>
                                <div>
                                    <p className="eyebrow mb-1">Danger zone</p>
                                    <h4 className="font-display text-lg leading-tight">Confirm Talent Merge</h4>
                                </div>
                            </div>
                            <p className="text-sm text-muted-foreground mb-2">
                                You are merging <span className="text-foreground font-medium">{duplicate.name}</span> into{" "}
                                <span className="text-foreground font-medium">{canonical.name}</span>.
                            </p>
                            <p className="text-sm text-muted-foreground mb-5">
                                This will combine their profile data, submissions, media and related records. The secondary
                                profile will be archived as <span className="tg-mono">MERGED</span>.
                            </p>
                            {requireTypedConfirm && (
                                <label className="block mb-5">
                                    <span className="text-[11px] tracking-widest uppercase text-muted-foreground">
                                        Identity fields differ — type <span className="tg-mono text-foreground">MERGE</span> to confirm
                                    </span>
                                    <input
                                        value={confirmTyped}
                                        onChange={(e) => setConfirmTyped(e.target.value)}
                                        autoFocus
                                        data-testid="merge-confirm-typed-input"
                                        className="mt-2 w-full bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm tg-mono tracking-wider"
                                    />
                                </label>
                            )}
                            <div className="flex gap-2">
                                <button type="button" onClick={() => setStep("preview")} disabled={busy} className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm">
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    onClick={runMerge}
                                    disabled={!confirmEnabled || busy}
                                    className="flex-1 bg-[var(--tg-danger)] text-white py-2.5 rounded-sm text-sm inline-flex items-center justify-center gap-2 disabled:opacity-40"
                                    data-testid="merge-confirm-button"
                                >
                                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GitMerge className="w-3.5 h-3.5" />}
                                    Confirm Merge
                                </button>
                            </div>
                        </div>
                    )}

                    {step === "success" && result && (
                        <div className="text-center py-4" data-testid="merge-step-success">
                            <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto mb-3" />
                            <h4 className="font-display text-lg mb-1">Talent Profiles Merged</h4>
                            <p className="text-sm text-muted-foreground mb-5">
                                {duplicate?.name || "The duplicate profile"}'s duplicate profile has been merged successfully.
                            </p>
                            <div className="grid grid-cols-2 gap-3 mb-6 text-left">
                                <div className="border border-border rounded-sm p-3 text-xs">
                                    <p className="text-muted-foreground">Submissions preserved</p>
                                    <p className="text-base font-medium">{result.submissions_preserved}</p>
                                </div>
                                <div className="border border-border rounded-sm p-3 text-xs">
                                    <p className="text-muted-foreground">Media preserved</p>
                                    <p className="text-base font-medium">{result.media_preserved}</p>
                                </div>
                                <div className="border border-border rounded-sm p-3 text-xs">
                                    <p className="text-muted-foreground">Tags merged</p>
                                    <p className="text-base font-medium">{result.tags_merged}</p>
                                </div>
                                <div className="border border-border rounded-sm p-3 text-xs">
                                    <p className="text-muted-foreground">Fields updated</p>
                                    <p className="text-base font-medium">{result.fields_updated.length}</p>
                                </div>
                            </div>
                            <div className="flex gap-2">
                                <button
                                    type="button"
                                    onClick={() => { onSuccess?.(); onClose(); }}
                                    className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm"
                                >
                                    Back to Talents
                                </button>
                                <a
                                    href={`/admin/talents/${result.canonical_talent_id}`}
                                    className="flex-1 bg-foreground text-background py-2.5 rounded-sm text-sm inline-flex items-center justify-center"
                                    data-testid="merge-view-canonical"
                                >
                                    View Canonical Talent
                                </a>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
