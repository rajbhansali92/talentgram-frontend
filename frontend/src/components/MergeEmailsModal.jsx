import React, { useState, useEffect, useCallback } from "react";
import { X, Loader2, AlertTriangle, Mail, CheckCircle2, ArrowRight } from "lucide-react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";

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
 * "Merge Different Emails" — a SEPARATE admin action from Merge Talents
 * (MergeTalentsModal.jsx), launched from the same Global Talents bulk-select
 * bar once exactly 2 talents are selected, via its own sibling button. Solves
 * a distinct problem: the same real person submitted under two different
 * email addresses, creating two Talent records. Unlike Merge Talents, the
 * losing profile's email is never discarded — it's linked onto the surviving
 * profile as an `alternate_email`, so a future submission using EITHER email
 * keeps resolving to the one canonical talent.
 *
 *  <MergeEmailsModal
 *    open={showMergeEmailsModal}
 *    talentAId={ids[0]} talentBId={ids[1]}
 *    onClose={() => setShowMergeEmailsModal(false)}
 *    onSuccess={() => { clear(); refetch(); }}
 *  />
 *
 * Flow: review (both profiles + both emails, choose which survives) ->
 * confirm (irreversible, plain-language) -> success. Reads
 * `POST /talents/merge-emails/preview` (read-only); only the final confirm
 * step calls `POST /talents/merge-emails`.
 */
export default function MergeEmailsModal({ open, talentAId, talentBId, onClose, onSuccess }) {
    const [step, setStep] = useState("loading"); // loading | review | confirm | success | error
    const [data, setData] = useState(null);
    const [canonicalId, setCanonicalId] = useState(null);
    const [busy, setBusy] = useState(false);
    const [errorMsg, setErrorMsg] = useState(null);
    const [result, setResult] = useState(null);

    const loadReview = useCallback(async () => {
        setStep("loading");
        setErrorMsg(null);
        try {
            const { data: resp } = await adminApi.post("/talents/merge-emails/preview", {
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
            setResult(null);
            loadReview();
        }
    }, [open, talentAId, talentBId, loadReview]);

    if (!open) return null;

    const talentA = data?.talent_a;
    const talentB = data?.talent_b;
    const canonical = canonicalId === talentA?.id ? talentA : talentB;
    const duplicate = canonicalId === talentA?.id ? talentB : talentA;

    const runMerge = async () => {
        if (busy) return;
        setBusy(true);
        try {
            const { data: resp } = await adminApi.post("/talents/merge-emails", {
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

    return (
        <div
            className="fixed inset-0 z-[60] bg-black/70 backdrop-blur flex items-center justify-center p-4"
            data-testid="merge-emails-modal"
        >
            <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto border border-border bg-background rounded-sm relative">
                <div className="sticky top-0 bg-background border-b border-border px-6 py-4 flex items-center justify-between z-10">
                    <div className="flex items-center gap-3">
                        <Mail className="w-4 h-4 text-muted-foreground" />
                        <h3 className="font-display text-xl leading-tight">Merge Different Emails</h3>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-muted-foreground hover:text-foreground"
                        data-testid="merge-emails-modal-close"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {step !== "loading" && step !== "error" && step !== "success" && (
                    <div className="px-6 pt-4 flex items-center gap-4 flex-wrap">
                        <StepBadge n={1} label="Review" active={step === "review"} done={step === "confirm"} />
                        <ArrowRight className="w-3 h-3 text-muted-foreground" />
                        <StepBadge n={2} label="Confirm" active={step === "confirm"} done={false} />
                    </div>
                )}

                <div className="p-6">
                    {step === "loading" && (
                        <div className="flex items-center justify-center py-12 text-muted-foreground">
                            <Loader2 className="w-5 h-5 animate-spin" />
                        </div>
                    )}

                    {step === "error" && (
                        <div className="text-center py-8" data-testid="merge-emails-step-error">
                            <AlertTriangle className="w-6 h-6 text-[var(--tg-danger)] mx-auto mb-3" />
                            <p className="text-sm text-muted-foreground">{errorMsg}</p>
                        </div>
                    )}

                    {step === "review" && talentA && talentB && (
                        <div data-testid="merge-emails-step-review">
                            {(talentA.status === "MERGED" || talentB.status === "MERGED") && (
                                <div className="mb-4 p-3 border border-[var(--tg-danger)]/40 bg-[var(--tg-danger)]/5 text-xs text-[var(--tg-danger)] rounded-sm">
                                    One of these talents is already marked MERGED. Refresh the roster before continuing.
                                </div>
                            )}

                            <p className="text-xs text-muted-foreground mb-5">
                                These two profiles have different email addresses. Choose which one should remain the
                                primary profile — the other profile's email will stay linked to it, and every future
                                submission using either email will keep resolving to this one talent.
                            </p>

                            <div className="space-y-2 mb-5">
                                {[talentA, talentB].map((t) => (
                                    <label
                                        key={t.id}
                                        className={`block border rounded-sm px-3 py-2.5 cursor-pointer ${
                                            canonicalId === t.id ? "border-foreground bg-foreground/5" : "border-border hover:border-foreground/40"
                                        }`}
                                        data-testid={`merge-emails-canonical-choice-${t.id}`}
                                    >
                                        <div className="flex items-center gap-3">
                                            <input
                                                type="radio"
                                                name="canonical"
                                                checked={canonicalId === t.id}
                                                onChange={() => setCanonicalId(t.id)}
                                            />
                                            <div className="min-w-0 flex-1">
                                                <p className="text-sm truncate">{t.name}</p>
                                                <p className="text-[11px] tg-mono text-muted-foreground truncate">{t.email || "—"}</p>
                                                <p className="text-[10px] tg-mono text-muted-foreground truncate">{t.id}</p>
                                            </div>
                                            {data.recommended_canonical_id === t.id && (
                                                <span className="text-[10px] tg-mono uppercase text-emerald-600 shrink-0">
                                                    Recommended — {data.recommendation_reason}
                                                </span>
                                            )}
                                        </div>
                                        <div className="grid grid-cols-2 gap-2 mt-2 pl-7 text-[11px] text-muted-foreground">
                                            <span>Submissions: {t.relationship_counts?.submissions ?? 0}</span>
                                            <span>Media: {t.media_count}</span>
                                        </div>
                                    </label>
                                ))}
                            </div>

                            <div className="flex gap-2">
                                <button type="button" onClick={onClose} className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm">
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setStep("confirm")}
                                    className="flex-1 bg-foreground text-background py-2.5 rounded-sm text-sm"
                                    data-testid="merge-emails-continue-to-confirm"
                                >
                                    Continue
                                </button>
                            </div>
                        </div>
                    )}

                    {step === "confirm" && canonical && duplicate && (
                        <div data-testid="merge-emails-step-confirm">
                            <div className="flex gap-4 mb-5">
                                <div className="shrink-0 w-10 h-10 rounded-full bg-amber-500/10 border border-amber-500/25 flex items-center justify-center">
                                    <Mail className="w-4 h-4 text-amber-600" />
                                </div>
                                <div>
                                    <p className="eyebrow mb-1">Confirm</p>
                                    <h4 className="font-display text-lg leading-tight">Link These Two Emails</h4>
                                </div>
                            </div>
                            <p className="text-sm text-muted-foreground mb-2">
                                <span className="text-foreground font-medium">{canonical.name}</span>{" "}
                                (<span className="tg-mono">{canonical.email}</span>) will remain the primary profile.
                            </p>
                            <p className="text-sm text-muted-foreground mb-2">
                                <span className="text-foreground font-medium">{duplicate.email}</span> will be linked to
                                it as an alternate email — both addresses will resolve to this one talent from now on.
                            </p>
                            <p className="text-sm text-muted-foreground mb-5">
                                All of {duplicate.name}'s submissions, media, project relationships and tags are kept and
                                combined onto the primary profile. This cannot be undone.
                            </p>
                            <div className="flex gap-2">
                                <button type="button" onClick={() => setStep("review")} disabled={busy} className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm">
                                    Back
                                </button>
                                <button
                                    type="button"
                                    onClick={runMerge}
                                    disabled={busy}
                                    className="flex-1 bg-foreground text-background py-2.5 rounded-sm text-sm inline-flex items-center justify-center gap-2 disabled:opacity-40"
                                    data-testid="merge-emails-confirm-button"
                                >
                                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Mail className="w-3.5 h-3.5" />}
                                    Confirm Merge
                                </button>
                            </div>
                        </div>
                    )}

                    {step === "success" && result && (
                        <div className="text-center py-4" data-testid="merge-emails-step-success">
                            <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto mb-3" />
                            <h4 className="font-display text-lg mb-1">Emails Linked</h4>
                            <p className="text-sm text-muted-foreground mb-5">
                                Both emails now resolve to the same talent profile.
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
                                    data-testid="merge-emails-view-canonical"
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
