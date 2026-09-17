import React from "react";
import { ArrowRight, Check, AlertTriangle, X as XIcon, Minus, Play, Link2, Users, Plus, FileText, UploadCloud } from "lucide-react";
import HlsVideo from "@/components/HlsVideo";

const MB = 1024 * 1024;
function fmtBytes(n) {
    if (!n && n !== 0) return "";
    if (n < MB) return `${Math.max(1, Math.round(n / 1024))} KB`;
    return `${(n / MB).toFixed(1)} MB`;
}

/**
 * Submission preparation preview (Phase 4B) — "SUBMISSION READY". Shows the
 * resolved talent / project / authoritative submission, the current form,
 * what media is already on the submission, what's available in the talent's
 * library, and exactly what would be attached. Nothing is written until
 * "Confirm Attach". Reuses HlsVideo for inline inspection.
 */
function SubMediaRow({ m, testid }) {
    const [open, setOpen] = React.useState(false);
    return (
        <div className="mrow" data-testid={testid}>
            <button type="button" className="mbtn" onClick={() => setOpen(!open)}>
                {m.kind === "image" ? <FileText size={12} /> : <Play size={12} />} {m.label}
            </button>
            {open && m.url && (
                <div className="mplayer">
                    {m.kind === "image" ? (
                        <img src={m.url} alt={m.label} />
                    ) : (
                        <HlsVideo src={m.url} poster={m.poster_url || undefined} controls playsInline
                            data-testid="assistant-sub-media-video" />
                    )}
                </div>
            )}
        </div>
    );
}

function SubmissionPreviewCard({ sub, response, onConfirm, onCancel, busy, executed }) {
    const isAttach = sub.action_type === "attach_media";
    const isIngest = sub.action_type === "ingest_audition";
    const up = sub.upload || {};
    const title = isIngest ? "INCOMING AUDITION" : isAttach ? "ATTACH TO SUBMISSION" : "SUBMISSION";
    return (
        <div className="tga-comm" data-testid="assistant-sub-preview">
            <div className="tga-comm-h">{title}</div>
            <div className="tga-comm-body">
                <div className="tga-comm-rows">
                    <div><span className="k">Talent</span><span className="v">{sub.talent?.label}</span></div>
                    <div><span className="k">Project</span><span className="v">{sub.project?.label}</span></div>
                    <div><span className="k">Status</span><span className="v">{sub.submission_status || "—"}</span></div>
                    <div><span className="k">Form</span><span className="v">{sub.form_found ? "✓ Found" : "— Not found"}</span></div>
                </div>

                {sub.form_found && Object.keys(sub.form || {}).length > 0 && (
                    <div className="tga-fields" data-testid="assistant-sub-form">
                        {Object.entries(sub.form).map(([k, v]) => (
                            <div className="f" key={k}><b>{k[0].toUpperCase() + k.slice(1)}</b><span>{String(v)}</span></div>
                        ))}
                    </div>
                )}

                {sub.existing_media?.length > 0 && (
                    <div className="tga-comm-media" data-testid="assistant-sub-existing">
                        <div className="mh">On the submission</div>
                        {sub.existing_media.map((m, i) => <SubMediaRow key={m.submission_media_id || i} m={m} />)}
                    </div>
                )}

                {sub.available_media?.length > 0 && (
                    <div className="tga-comm-media" data-testid="assistant-sub-available">
                        <div className="mh">Available in {sub.talent?.label}'s profile</div>
                        {sub.available_media.map((m, i) => <SubMediaRow key={m.source_id || i} m={m} />)}
                    </div>
                )}

                {sub.missing?.length > 0 && (
                    <div className="tga-warn" data-testid="assistant-sub-missing" style={{ borderRadius: 8, marginTop: 10 }}>
                        Missing: {sub.missing.join(", ")}
                    </div>
                )}

                {isIngest && (
                    <div className="tga-plan" data-testid="assistant-sub-upload" style={{ marginTop: 12 }}>
                        <div className="tga-plan-h">Incoming file — nothing uploaded yet</div>
                        <div className="tga-fields">
                            <div className="f"><b>File</b><span data-testid="assistant-upload-filename">{up.filename || "—"}</span></div>
                            {up.size ? <div className="f"><b>Size</b><span>{fmtBytes(up.size)}</span></div> : null}
                            {up.content_type ? <div className="f"><b>Type</b><span>{up.content_type}</span></div> : null}
                            <div className="f"><b>Will upload as</b><span data-testid="assistant-upload-target">{up.target_label || "—"}</span></div>
                            <div className="f"><b>Destination</b><span>{sub.talent?.label} → {sub.project?.label} submission</span></div>
                        </div>
                    </div>
                )}

                {isAttach && sub.proposed?.length > 0 && (
                    <div className="tga-plan" data-testid="assistant-sub-proposed" style={{ marginTop: 12 }}>
                        <div className="tga-plan-h">Proposed attachment — preview only</div>
                        {sub.proposed.map((m, i) => (
                            <div className="tga-crow" key={m.source_id || i} style={{ gridTemplateColumns: "16px 1fr" }}>
                                <Plus size={13} style={{ color: "#0f5132" }} />
                                <span className="who">{m.label}</span>
                            </div>
                        ))}
                    </div>
                )}

                {sub.warnings?.length > 0 && (
                    <div className="tga-warn" data-testid="assistant-sub-warnings" style={{ borderRadius: 8, marginTop: 10 }}>
                        {sub.warnings.map((w, i) => <div key={i}>{w}</div>)}
                    </div>
                )}
            </div>

            {response.execError && (
                <div className="tga-warn" data-testid="assistant-sub-error" style={{ borderRadius: 8, margin: "0 14px 12px" }}>
                    {response.execError}
                </div>
            )}

            {executed && (response.state === "attached" || response.state === "partial") ? (
                <div className="tga-exec" style={{ margin: "0 14px 14px" }} data-testid="assistant-sub-result">
                    {(response.result?.outcomes || []).map((o, i) => {
                        const good = ["attached", "already_attached", "uploaded"].includes(o.status);
                        const Icon = good ? Check : XIcon;
                        return (
                            <div className={`tga-exec-row ${good ? "ok" : "bad"}`} key={i}>
                                <Icon size={13} />
                                <span className="who">{o.label}</span>
                                <span className="detail">
                                    {o.status === "attached" ? "attached"
                                        : o.status === "uploaded" ? "uploaded"
                                        : o.status === "already_attached" ? "already attached"
                                        : o.reason || "failed"}
                                </span>
                            </div>
                        );
                    })}
                </div>
            ) : executed && response.state === "stale" ? (
                <div className="tga-stale" data-testid="assistant-sub-stale">{response.message}</div>
            ) : executed && (response.state === "blocked" || response.state === "failed") ? (
                <div className="tga-warn" style={{ borderRadius: 8, margin: "0 14px 12px" }} data-testid="assistant-sub-blocked">{response.message}</div>
            ) : (isAttach || isIngest) && sub.executable && response.state !== "cancelled" && !response.execError ? (
                <div className="tga-actions" style={{ padding: "0 14px 14px" }} data-testid="assistant-sub-actions">
                    <button type="button" className="tga-btn ghost" onClick={onCancel} disabled={busy}
                        data-testid="assistant-sub-cancel">Cancel</button>
                    <button type="button" className="tga-btn primary" onClick={onConfirm} disabled={busy}
                        data-testid="assistant-sub-confirm">{isIngest ? "Confirm Upload" : "Confirm Attach"}</button>
                </div>
            ) : null}
        </div>
    );
}

/**
 * Communication preview (Phase 4A) — "SEND TO WHATSAPP" / "SEND TALENT
 * PROFILE" card. Shows exactly what will go where. Nothing is sent until
 * "Confirm Send". Reuses HlsVideo so the user can inspect the existing
 * media before sending (no download, no copy).
 */
function CommPreviewCard({ comm, response, onConfirm, onCancel, busy, executed }) {
    const [open, setOpen] = React.useState(null); // media index playing
    const isProfile = comm.action_type === "send_profile_link";
    const isAttach = comm.action_type === "attach_media";
    const isInfo = comm.action_type === "send_project_info";
    const title = isProfile
        ? "SEND TALENT PROFILE"
        : isAttach
          ? "ATTACH TO SUBMISSION"
          : isInfo
            ? "SEND PROJECT DETAILS"
            : "SEND TO WHATSAPP";

    return (
        <div className="tga-comm" data-testid="assistant-comm-preview">
            <div className="tga-comm-h">{title}</div>
            <div className="tga-comm-body">
                <div className="tga-comm-rows">
                    {comm.talent && (
                        <div><span className="k">Talent</span><span className="v">{comm.talent.label}</span></div>
                    )}
                    {comm.project && (
                        <div><span className="k">Project</span><span className="v">{comm.project.label}</span></div>
                    )}
                    <div>
                        <span className="k">Destination</span>
                        <span className="v">
                            {comm.destination_type === "whatsapp_group" && <Users size={12} />}
                            {comm.destination_type === "whatsapp_number" && <Users size={12} />}
                            {" "}{comm.destination_label || comm.destination || "—"}
                        </span>
                    </div>
                </div>

                {isProfile && comm.profile_link && (
                    <div className="tga-comm-link" data-testid="assistant-comm-link">
                        <Link2 size={13} />
                        <a href={comm.profile_link.url} target="_blank" rel="noreferrer">
                            {comm.profile_link.url}
                        </a>
                    </div>
                )}

                {(comm.media || []).length > 0 && (
                    <div className="tga-comm-media" data-testid="assistant-comm-media">
                        <div className="mh">MEDIA</div>
                        {comm.media.map((m, i) => (
                            <div className="mrow" key={m.media_id || i}>
                                <button type="button" className="mbtn" onClick={() => setOpen(open === i ? null : i)}
                                    data-testid="assistant-comm-media-play">
                                    <Play size={12} /> {m.label}
                                </button>
                                {open === i && m.url && (
                                    <div className="mplayer">
                                        <HlsVideo src={m.url} poster={m.poster_url || undefined} controls playsInline
                                            data-testid="assistant-comm-media-video" />
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}

                {comm.warnings?.length > 0 && (
                    <div className="tga-warn" data-testid="assistant-comm-warnings" style={{ borderRadius: 8, marginTop: 10 }}>
                        {comm.warnings.map((w, i) => <div key={i}>{w}</div>)}
                    </div>
                )}
            </div>

            {response.execError && (
                <div className="tga-warn" data-testid="assistant-comm-error" style={{ borderRadius: 8, margin: "0 14px 12px" }}>
                    {response.execError}
                </div>
            )}

            {executed && response.state === "stale" ? (
                <div className="tga-stale" data-testid="assistant-comm-stale">{response.message}</div>
            ) : executed && response.state === "blocked" ? (
                <div className="tga-warn" style={{ borderRadius: 8, margin: "0 14px 12px" }} data-testid="assistant-comm-blocked">
                    {response.message}
                </div>
            ) : executed ? (
                <CommExecutionCard response={response} />
            ) : comm.executable && response.state !== "cancelled" && !response.execError ? (
                <div className="tga-actions" style={{ padding: "0 14px 14px" }} data-testid="assistant-comm-actions">
                    <button type="button" className="tga-btn ghost" onClick={onCancel} disabled={busy}
                        data-testid="assistant-comm-cancel">
                        Cancel
                    </button>
                    <button type="button" className="tga-btn primary" onClick={onConfirm} disabled={busy}
                        data-testid="assistant-comm-confirm">
                        Confirm Send
                    </button>
                </div>
            ) : null}
        </div>
    );
}

const COMM_META = {
    queued: { cls: "ok", Icon: Check },
    sent: { cls: "ok", Icon: Check },
    failed: { cls: "bad", Icon: XIcon },
};

function CommExecutionCard({ response }) {
    const outcomes = response.result?.outcomes || [];
    return (
        <div className="tga-exec" style={{ margin: "0 14px 14px" }} data-testid="assistant-comm-result">
            {outcomes.map((o, i) => {
                const m = COMM_META[o.status] || COMM_META.failed;
                return (
                    <div className={`tga-exec-row ${m.cls}`} key={i}>
                        <m.Icon size={13} />
                        <span className="who">{o.label}</span>
                        <span className="detail">{o.status === "queued" ? "queued to WhatsApp" : o.reason || o.status}</span>
                    </div>
                );
            })}
        </div>
    );
}

/**
 * Renders the running transcript of command turns. Structured cards, not
 * chat bubbles — this is a Command Centre, not a chat window.
 */
const OUTCOME_META = {
    success: { cls: "ok", Icon: Check, word: "changed" },
    unchanged: { cls: "muted", Icon: Minus, word: "unchanged" },
    stale: { cls: "warn", Icon: AlertTriangle, word: "not current" },
    failed: { cls: "bad", Icon: XIcon, word: "failed" },
};

function ExecutionResultCard({ result }) {
    const outcomes = result?.outcomes || [];
    return (
        <div className="tga-exec" data-testid="assistant-exec-result">
            {outcomes.length > 0 && (
                <div className="tga-exec-rows">
                    {outcomes.map((o, i) => {
                        const m = OUTCOME_META[o.status] || OUTCOME_META.failed;
                        return (
                            <div className={`tga-exec-row ${m.cls}`} key={o.talent_id || i}>
                                <m.Icon size={13} />
                                <span className="who">{o.talent_label}</span>
                                <span className="detail">
                                    {o.status === "success" && o.from_label
                                        ? `${o.from_label} → ${o.to_label}`
                                        : o.reason || m.word}
                                </span>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

function PlanCard({ plan, response, onConfirm, onCancel, busy, executed }) {
    const isCreate = plan.intent === "create_project";
    const stale = response.state === "stale";
    const executable = plan.executable && !isCreate;
    return (
        <>
            {plan.project && (
                <div style={{ fontSize: 12, color: "#5a5a5a", marginTop: 8 }}>
                    Project: <b style={{ color: "#0B1F3A" }}>{plan.project.label}</b>
                </div>
            )}
            {isCreate ? (
                <div className="tga-plan" data-testid="assistant-plan">
                    <div className="tga-plan-h">Would create</div>
                    <div className="tga-fields">
                        {Object.entries(plan.changes[0]?.fields || {}).map(([k, v]) => (
                            <div className="f" key={k}><b>{k}</b><span>{v}</span></div>
                        ))}
                    </div>
                    {plan.warnings?.length > 0 && (
                        <div className="tga-warn">{plan.warnings.join(" ")}</div>
                    )}
                </div>
            ) : plan.changes.length > 0 ? (
                <div className="tga-plan" data-testid="assistant-plan">
                    <div className="tga-plan-h">
                        {plan.changes.length} change{plan.changes.length !== 1 ? "s" : ""} — preview only
                    </div>
                    {plan.changes.map((c, i) => (
                        <div className="tga-crow" key={c.entity_id || i}>
                            <span className="who">{c.entity_label}</span>
                            <span className="tga-pill">{c.current_label || "—"}</span>
                            <span className="tga-arrow"><ArrowRight size={13} /></span>
                            <span className={`tga-pill ${c.op === "remove" ? "rm" : c.op === "add" ? "add" : "to"}`}>
                                {c.proposed_label || "—"}
                            </span>
                        </div>
                    ))}
                    {plan.warnings?.length > 0 && (
                        <div className="tga-warn" data-testid="assistant-plan-warnings">
                            {plan.warnings.map((w, i) => <div key={i}>{w}</div>)}
                        </div>
                    )}
                </div>
            ) : (
                plan.warnings?.length > 0 && (
                    <div className="tga-plan"><div className="tga-warn">{plan.warnings.join(" ")}</div></div>
                )
            )}

            {response.execError && (
                <div className="tga-warn" data-testid="assistant-exec-error" style={{ marginTop: 10, borderRadius: 8 }}>
                    {response.execError}
                </div>
            )}

            {executed && response.state === "executed" ? (
                <ExecutionResultCard result={response.result} />
            ) : executed && stale ? (
                <div className="tga-stale" data-testid="assistant-stale">
                    {response.message}
                </div>
            ) : executed && response.state === "blocked" ? (
                <div className="tga-warn" data-testid="assistant-exec-blocked" style={{ marginTop: 10, borderRadius: 8 }}>
                    {response.message}
                </div>
            ) : plan.requires_confirmation && response.state !== "cancelled" && !response.execError ? (
                <div className="tga-actions" data-testid="assistant-plan-actions">
                    <button
                        type="button"
                        className="tga-btn primary"
                        onClick={onConfirm}
                        disabled={busy || !plan.requires_confirmation}
                        data-testid="assistant-confirm"
                        title={executable ? "This will make the change" : "Preview only"}
                    >
                        {executable ? "Confirm" : "OK"}
                    </button>
                    {isCreate && (
                        <button type="button" className="tga-btn ghost" disabled title="Not available yet">
                            Edit
                        </button>
                    )}
                    <button type="button" className="tga-btn ghost" onClick={onCancel} disabled={busy}
                        data-testid="assistant-cancel">
                        Cancel
                    </button>
                </div>
            ) : null}
        </>
    );
}

function ClarificationCard({ clarification, onPick }) {
    return (
        <div className="tga-opts" data-testid="assistant-clarification">
            {(clarification.options || []).map((o) => (
                <button key={o.index} type="button" className="tga-opt"
                    onClick={() => onPick(o)} data-testid="assistant-clarify-option">
                    <span className="num">{o.index}</span>
                    <span>{o.label}</span>
                </button>
            ))}
        </div>
    );
}

const STATUS_META = {
    verified: { cls: "ok", Icon: Check, word: "VERIFIED" },
    sent: { cls: "ok", Icon: Check, word: "SENT" },
    sending: { cls: "warn", Icon: AlertTriangle, word: "SENDING" },
    queued: { cls: "muted", Icon: Minus, word: "QUEUED" },
    pending: { cls: "muted", Icon: Minus, word: "QUEUED" },
    failed: { cls: "bad", Icon: XIcon, word: "FAILED" },
    skipped: { cls: "muted", Icon: Minus, word: "SKIPPED" },
    unknown: { cls: "muted", Icon: Minus, word: "UNKNOWN" },
};

function fmtTs(v) {
    if (!v) return null;
    try {
        const d = new Date(v);
        if (isNaN(d.getTime())) return String(v);
        return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch { return String(v); }
}

/** WHATSAPP STATUS — read-only delivery state from the existing engine. */
function StatusCard({ status }) {
    const m = STATUS_META[status.overall] || STATUS_META.unknown;
    return (
        <div className="tga-comm" data-testid="assistant-status-card">
            <div className="tga-comm-h">WHATSAPP STATUS</div>
            <div className="tga-comm-body">
                <div className="tga-comm-rows">
                    <div><span className="k">Talent</span><span className="v">{status.talent?.label || "—"}</span></div>
                    <div><span className="k">Project</span><span className="v">{status.project?.label || "—"}</span></div>
                    <div><span className="k">Destination</span><span className="v">{status.destination || "—"}</span></div>
                </div>

                {status.media?.length > 0 && (
                    <div className="tga-comm-media" data-testid="assistant-status-media">
                        <div className="mh">MEDIA</div>
                        {status.media.map((x, i) => {
                            const mm = STATUS_META[x.status] || STATUS_META.unknown;
                            return (
                                <div className={`tga-exec-row ${mm.cls}`} key={i} style={{ margin: "2px 0" }}>
                                    <mm.Icon size={12} /><span className="who">{x.label}</span>
                                    <span className="detail">{x.status}</span>
                                </div>
                            );
                        })}
                    </div>
                )}

                <div className={`tga-exec-row ${m.cls}`} data-testid="assistant-status-overall" style={{ marginTop: 10, fontWeight: 600 }}>
                    <m.Icon size={13} /><span className="who">Status</span><span className="detail">{m.word}</span>
                </div>
                <div style={{ fontSize: 12, color: "#5a6b80", margin: "6px 0 0" }}>{status.overall_copy}</div>

                <div className="tga-fields" style={{ marginTop: 10 }}>
                    {fmtTs(status.queued_at || status.queued_by_assistant_at) && (
                        <div className="f"><b>Queued</b><span>{fmtTs(status.queued_at || status.queued_by_assistant_at)}</span></div>
                    )}
                    {fmtTs(status.sent_at) && <div className="f"><b>Sent</b><span>{fmtTs(status.sent_at)}</span></div>}
                    <div className="f"><b>Jobs</b><span>{status.job_count} · {status.overall}</span></div>
                </div>

                {status.overall === "failed" && status.failure_reason && (
                    <div className="tga-warn" data-testid="assistant-status-failure" style={{ borderRadius: 8, marginTop: 10 }}>
                        Reason: {status.failure_reason}
                        <div style={{ marginTop: 4 }}>No automatic retry was performed.</div>
                    </div>
                )}
            </div>
        </div>
    );
}

/** WHATSAPP REPLY — a captured inbound message, shown verbatim, never interpreted. */
function InboundCard({ inbound }) {
    const talentOk = !!inbound.talent;
    const projectOk = !!inbound.project;
    return (
        <div className="tga-comm" data-testid="assistant-inbound-card">
            <div className="tga-comm-h">{talentOk ? "WHATSAPP REPLY" : "INBOUND WHATSAPP MESSAGE"}</div>
            <div className="tga-comm-body">
                <div className="tga-comm-rows">
                    <div>
                        <span className="k">Talent</span>
                        <span className="v" data-testid="assistant-inbound-talent">
                            {talentOk ? inbound.talent.label
                                : inbound.talent_resolution === "ambiguous" ? "Ambiguous — matches multiple records"
                                : "Unresolved"}
                        </span>
                    </div>
                    {!talentOk && (
                        <div><span className="k">Sender</span><span className="v">{inbound.sender_phone_masked}</span></div>
                    )}
                    <div>
                        <span className="k">Project</span>
                        <span className="v" data-testid="assistant-inbound-project">
                            {projectOk ? inbound.project.label
                                : inbound.project_resolution === "ambiguous_project" ? "Could not be determined reliably"
                                : "Unresolved"}
                        </span>
                    </div>
                    {inbound.received_at && (
                        <div><span className="k">Received</span><span className="v">{fmtTs(inbound.received_at)}</span></div>
                    )}
                    {inbound.group_name && (
                        <div><span className="k">Group</span><span className="v">{inbound.group_name}</span></div>
                    )}
                </div>

                <div className="tga-inbound-msg" data-testid="assistant-inbound-text"
                    style={{ marginTop: 12, padding: "10px 12px", background: "#f7f9fc", border: "1px solid var(--tga-line)", borderRadius: 8, fontSize: 14 }}>
                    “{inbound.message_text}”
                </div>

                {!projectOk && inbound.project_candidates?.length > 0 && (
                    <div className="tga-warn" data-testid="assistant-inbound-candidates" style={{ borderRadius: 8, marginTop: 10 }}>
                        Possible recent projects:
                        <ol style={{ margin: "4px 0 0 18px" }}>
                            {inbound.project_candidates.map((p) => <li key={p.id}>{p.label}</li>)}
                        </ol>
                        <div style={{ marginTop: 4 }}>No project was assigned.</div>
                    </div>
                )}

                <div className="tga-fields" style={{ marginTop: 10 }}>
                    <div className="f"><b>Resolution</b><span>
                        {talentOk ? "✓ Talent" : "— Talent"}{"  ·  "}{projectOk ? "✓ Project" : "— Project"}
                    </span></div>
                    <div className="f"><b>Source</b><span>WhatsApp</span></div>
                </div>
                {!talentOk && (
                    <div style={{ fontSize: 12, color: "#5a6b80", marginTop: 8 }}>No action was taken.</div>
                )}
            </div>
        </div>
    );
}

const CONF_META = { high: { cls: "ok", word: "High" }, medium: { cls: "warn", word: "Medium" }, low: { cls: "muted", word: "Low" } };
const ANSWERABILITY_META = {
    answerable: { cls: "ok", word: "Answerable" },
    partially_answerable: { cls: "warn", word: "Partially answerable" },
    insufficient_information: { cls: "muted", word: "Insufficient information" },
    ambiguous: { cls: "bad", word: "Ambiguous" },
};

/** INBOUND WHATSAPP — deterministic read-only intelligence. Nothing was sent. */
function IntelligenceCard({ intel, response, onConfirm, onCancel, busy, executed }) {
    const talentOk = !!intel.talent;
    const projectOk = !!intel.project;
    const draft = intel.ai_draft;
    const [edited, setEdited] = React.useState(draft && !draft.failed ? draft.text : "");
    React.useEffect(() => {
        if (draft && !draft.failed) setEdited(draft.text);
    }, [draft && draft.text, draft && draft.failed]);
    const ambiguousProj = (intel.project_candidates || []).length > 0 && !projectOk;
    const cm = CONF_META[intel.topic_confidence] || CONF_META.low;
    const am = ANSWERABILITY_META[intel.answerability] || ANSWERABILITY_META.insufficient_information;
    const know = intel.project_knowledge || {};
    const intents = (intel.intents && intel.intents.length)
        ? intel.intents
        : (intel.topic ? [{ topic: intel.topic, confidence: intel.topic_confidence || "medium" }] : []);
    return (
        <div className="tga-comm" data-testid="assistant-intel-card">
            <div className="tga-comm-h">INBOUND WHATSAPP</div>
            <div className="tga-comm-body">
                <div className="tga-comm-rows">
                    <div><span className="k">Talent</span><span className="v" data-testid="assistant-intel-talent">
                        {talentOk ? intel.talent.label : intel.talent_resolution === "ambiguous" ? "Ambiguous" : "Unresolved"}
                    </span></div>
                    <div><span className="k">Project</span><span className="v" data-testid="assistant-intel-project">
                        {projectOk ? intel.project.label : ambiguousProj ? "Could not be determined reliably" : "Unresolved"}
                    </span></div>
                    {intel.received_at && <div><span className="k">Received</span><span className="v">{fmtTs(intel.received_at)}</span></div>}
                </div>

                <div className="tga-inbound-msg" data-testid="assistant-intel-text"
                    style={{ marginTop: 12, padding: "10px 12px", background: "#f7f9fc", border: "1px solid var(--tga-line)", borderRadius: 8, fontSize: 14 }}>
                    “{intel.message_text}”
                </div>

                {ambiguousProj ? (
                    <div className="tga-warn" data-testid="assistant-intel-candidates" style={{ borderRadius: 8, marginTop: 10 }}>
                        Possible projects:
                        <ol style={{ margin: "4px 0 0 18px" }}>
                            {intel.project_candidates.map((p) => <li key={p.id}>{p.label}</li>)}
                        </ol>
                        <div style={{ marginTop: 4 }}>No project information was used.</div>
                    </div>
                ) : intel.topic ? (
                    <>
                        {intents.length > 0 && (
                            <div data-testid="assistant-intel-detected" style={{ marginTop: 10 }}>
                                <div style={{ fontSize: 11, letterSpacing: ".05em", color: "#5a6b80", fontWeight: 700 }}>DETECTED</div>
                                {intents.map((it, i) => (
                                    <div key={i} className={`tga-exec-row ${it.confidence === "high" ? "ok" : "warn"}`}
                                        style={{ margin: "2px 0" }} data-testid={i === 0 ? "assistant-intel-topic" : undefined}>
                                        {it.confidence === "high" ? <Check size={12} /> : <AlertTriangle size={12} />}
                                        <span className="who">{it.topic.replace(/_/g, " ")}</span>
                                        <span className="detail">{it.confidence}</span>
                                    </div>
                                ))}
                            </div>
                        )}

                        {(() => {
                            const km = intel.project_knowledge_multi || {};
                            const rows = Object.entries(km.facts || {});
                            const miss = km.missing || [];
                            const hid = km.hidden || [];
                            if (!rows.length && !miss.length && !hid.length && !know.label) return null;
                            return (
                                <div data-testid="assistant-intel-verified" style={{ marginTop: 10 }}>
                                    <div style={{ fontSize: 11, letterSpacing: ".05em", color: "#5a6b80", fontWeight: 700 }}>VERIFIED INFORMATION</div>
                                    <div className="tga-fields">
                                        {rows.map(([lbl, val]) => (
                                            <div className="f" key={lbl}><b>{lbl[0].toUpperCase() + lbl.slice(1)}</b>
                                                <span className="tga-exec-row ok" style={{ padding: 0, border: 0 }}><Check size={12} /> {val}</span></div>
                                        ))}
                                        {miss.map((lbl) => (
                                            <div className="f" key={lbl}><b>{lbl[0].toUpperCase() + lbl.slice(1)}</b>
                                                <span className="tga-exec-row warn" style={{ padding: 0, border: 0 }}><AlertTriangle size={12} /> not available in Talentgram</span></div>
                                        ))}
                                        {hid.map((lbl) => (
                                            <div className="f" key={lbl}><b>{lbl[0].toUpperCase() + lbl.slice(1)}</b>
                                                <span className="tga-exec-row muted" style={{ padding: 0, border: 0 }}><Minus size={12} /> hidden from talent</span></div>
                                        ))}
                                        {!rows.length && !miss.length && !hid.length && know.label && (
                                            <div className="f" data-testid="assistant-intel-knowledge"><b>{know.label[0].toUpperCase() + know.label.slice(1)}</b>
                                                <span>{know.available ? know.value : "not available in Talentgram"}</span></div>
                                        )}
                                    </div>
                                </div>
                            );
                        })()}

                        <div className="tga-fields" style={{ marginTop: 8 }}>
                            <div className="f"><b>Answerability</b>
                                <span className={`tga-exec-row ${am.cls}`} style={{ padding: 0, border: 0 }} data-testid="assistant-intel-answerability">{am.word}</span>
                            </div>
                            {intel.needs_review && (
                                <div className="f"><b>Review</b>
                                    <span className="tga-exec-row warn" style={{ padding: 0, border: 0 }} data-testid="assistant-intel-confidence">Needs review</span></div>
                            )}
                        </div>

                        {intel.proposed_response && (
                            <div className="tga-plan" data-testid="assistant-intel-proposed" style={{ marginTop: 10 }}>
                                <div className="tga-plan-h">Suggested response — display only</div>
                                <div style={{ fontSize: 14, padding: "4px 2px" }}>“{intel.proposed_response}”</div>
                            </div>
                        )}
                        {intel.next_step && (
                            <div style={{ fontSize: 12, color: "#5a6b80", marginTop: 8 }} data-testid="assistant-intel-nextstep">
                                Suggested next step: {intel.next_step}
                            </div>
                        )}
                    </>
                ) : null}

                {intel.recent_conversation && (intel.recent_conversation.messages || []).length > 0 && (
                    <div data-testid="assistant-intel-conversation" style={{ marginTop: 12 }}>
                        <div style={{ fontSize: 11, letterSpacing: ".05em", color: "#5a6b80", fontWeight: 700 }}>
                            RECENT CONVERSATION — context only, not authoritative
                        </div>
                        <div style={{ fontSize: 11, color: "#8a97a8", margin: "2px 0 6px" }}>
                            last {intel.recent_conversation.window_hours}h
                            {intel.recent_conversation.truncated ? " · older messages not shown" : ""}
                        </div>
                        {intel.recent_conversation.messages.map((m, i) => (
                            <div key={i} data-testid="assistant-intel-conversation-msg" data-direction={m.direction}
                                style={{
                                    display: "flex",
                                    justifyContent: m.direction === "in" ? "flex-start" : "flex-end",
                                    margin: "3px 0",
                                }}>
                                <div style={{
                                    maxWidth: "78%", padding: "5px 9px", borderRadius: 9, fontSize: 13,
                                    background: m.direction === "in" ? "#eef1f4" : "#dcf8c6",
                                }}>{m.message_text}</div>
                            </div>
                        ))}
                    </div>
                )}

                {draft && draft.failed && (
                    <div className="tga-warn" data-testid="assistant-intel-draft-failed" style={{ borderRadius: 8, marginTop: 12 }}>
                        {draft.reason || "I couldn't produce a safe reply."} No message was sent.
                    </div>
                )}

                {draft && !draft.failed && !executed && (
                    <div className="tga-plan" data-testid="assistant-intel-draft" style={{ marginTop: 12 }}>
                        <div className="tga-plan-h">AI SUGGESTED RESPONSE</div>
                        <textarea
                            data-testid="assistant-intel-draft-text"
                            value={edited}
                            onChange={(e) => setEdited(e.target.value)}
                            disabled={busy || response.state === "cancelled"}
                            rows={3}
                            style={{ width: "100%", font: "inherit", fontSize: 14, padding: "8px 10px",
                                borderRadius: 8, border: "1px solid var(--tga-line)", resize: "vertical", marginTop: 6 }}
                        />
                        <div style={{ fontSize: 12, color: "#5a6b80", marginTop: 6 }}>
                            AI GENERATED · Needs your approval{draft.confidence && draft.confidence !== "high" ? " · low confidence" : ""}
                        </div>
                        {response.execError && (
                            <div className="tga-warn" data-testid="assistant-intel-approve-error" style={{ borderRadius: 8, marginTop: 8 }}>
                                {response.execError}
                            </div>
                        )}
                        {response.state !== "cancelled" && (
                            <div className="tga-actions" style={{ marginTop: 10 }} data-testid="assistant-intel-actions">
                                <button type="button" className="tga-btn ghost" onClick={onCancel} disabled={busy}
                                    data-testid="assistant-intel-reject">Reject</button>
                                <button type="button" className="tga-btn primary" onClick={() => onConfirm({ finalText: edited })}
                                    disabled={busy || !edited.trim()} data-testid="assistant-intel-approve">
                                    Approve &amp; Send
                                </button>
                            </div>
                        )}
                    </div>
                )}

                {executed && (response.state === "queued") && (
                    <div className="tga-exec" data-testid="assistant-intel-sent" style={{ marginTop: 12 }}>
                        <div className="tga-exec-row ok"><Check size={13} /><span className="who">Reply</span>
                            <span className="detail">queued to WhatsApp</span></div>
                    </div>
                )}
                {executed && (response.state === "stale" || response.state === "blocked" || response.state === "failed") && (
                    <div className={response.state === "stale" ? "tga-stale" : "tga-warn"}
                        data-testid="assistant-intel-approve-blocked"
                        style={{ borderRadius: 8, marginTop: 12 }}>{response.message}</div>
                )}

                {!(executed && response.state === "queued") && (
                    <div className="tga-exec-row bad" data-testid="assistant-intel-nosend" style={{ marginTop: 12, fontWeight: 600 }}>
                        <XIcon size={13} /><span className="who">NO MESSAGE SENT</span>
                        <span className="detail">{draft ? "human approval required" : "read-only intelligence · pipeline mutation: none"}</span>
                    </div>
                )}
            </div>
        </div>
    );
}

function AnswerCard({ answer }) {
    if (!answer) return null;
    if (answer.kind === "conversation") {
        return (
            <div className="tga-ans" data-testid="assistant-conversation">
                <div style={{ fontSize: 12, color: "#5a6b80", marginBottom: 6 }}>
                    Read-only · last {answer.window_hours}h · scoped to {answer.project}
                    {answer.truncated ? " · older messages not shown" : ""}
                </div>
                {(answer.messages || []).map((m, i) => (
                    <div key={i} data-testid="assistant-conversation-msg"
                        data-direction={m.direction}
                        style={{
                            display: "flex",
                            justifyContent: m.direction === "in" ? "flex-start" : "flex-end",
                            margin: "4px 0",
                        }}>
                        <div style={{
                            maxWidth: "78%", padding: "6px 10px", borderRadius: 10, fontSize: 14,
                            background: m.direction === "in" ? "#eef1f4" : "#dcf8c6",
                        }}>
                            <div style={{ fontSize: 11, color: "#5a6b80", marginBottom: 2 }}>
                                {m.direction === "in" ? answer.talent : "Talentgram"} · {fmtTs(m.timestamp)}
                            </div>
                            {m.message_text}
                        </div>
                    </div>
                ))}
            </div>
        );
    }
    if (answer.kind === "inbound_question_list") {
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>When</th><th>Talent</th><th>Topic</th><th>Message</th><th>Status</th></tr></thead>
                        <tbody>
                            {(answer.rows || []).map((r, i) => (
                                <tr key={i}><td>{r.when}</td><td>{r.talent}</td><td>{r.topic}</td><td>{r.message}</td><td>{r.status}</td></tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (answer.kind === "inbound_list") {
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>When</th><th>Talent</th><th>Project</th><th>Message</th></tr></thead>
                        <tbody>
                            {(answer.rows || []).map((r, i) => (
                                <tr key={i}><td>{r.when}</td><td>{r.talent}</td><td>{r.project}</td><td>{r.message}</td></tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (answer.kind === "whatsapp_activity") {
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>When</th><th>Talent</th><th>Project</th><th>Media</th><th>Status</th></tr></thead>
                        <tbody>
                            {(answer.rows || []).map((r, i) => (
                                <tr key={i}><td>{r.when}</td><td>{r.talent}</td><td>{r.project}</td><td>{r.media}</td><td>{r.status}</td></tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (answer.kind === "project_pipeline") {
        const p = answer.project;
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>Stage</th><th>Count</th></tr></thead>
                        <tbody>
                            {(p.stage_summary || []).map((s) => (
                                <tr key={s.key}><td>{s.label}</td><td>{s.count}</td></tr>
                            ))}
                            <tr><td><b>Tests received</b></td><td><b>{p.tests_received}</b></td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (answer.kind === "priorities" && answer.rows?.length) {
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>Project</th><th>Follow-ups</th><th>Tests in</th><th>Pipeline</th></tr></thead>
                        <tbody>
                            {answer.rows.map((r, i) => (
                                <tr key={i}><td>{r.project}</td><td>{r.follow_ups}</td><td>{r.tests_received}</td><td>{r.pipeline_total}</td></tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (answer.kind === "tests_received" && answer.rows?.length) {
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>Talent</th><th>Project</th><th>Stage</th></tr></thead>
                        <tbody>
                            {answer.rows.map((r, i) => (
                                <tr key={i}><td>{r.talent}</td><td>{r.project}</td><td>{r.stage}</td></tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (answer.kind === "counts" && answer.projects?.length) {
        return (
            <div className="tga-ans" data-testid="assistant-answer">
                <div className="tga-ans-scroll">
                    <table>
                        <thead><tr><th>Project</th><th>Status</th><th>Pipeline</th></tr></thead>
                        <tbody>
                            {answer.projects.slice(0, 12).map((p) => (
                                <tr key={p.id}><td>{p.name}</td><td>{p.status}</td><td>{p.pipeline_total}</td></tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    return null;
}

export default function AssistantConsole({ turns, busy, onConfirm, onCancel, onPick }) {
    if (!turns.length) return null;
    return (
        <div className="tga-console tga-fade tga-d2" data-testid="assistant-console">
            {turns.map((turn) => {
                const r = turn.response;
                return (
                    <div className="tga-turn" key={turn.id} data-testid="assistant-turn">
                        <div className="tga-turn-you">{turn.user}</div>
                        {r && (
                            <div className="tga-turn-a">
                                <div
                                    className={`lead ${r.state === "error" ? "err" : ""} ${
                                        r.state === "executed" && !r.result?.counts?.failed && !r.result?.counts?.stale
                                            ? "ok"
                                            : ""
                                    }`}
                                    data-testid="assistant-turn-message"
                                    style={{ whiteSpace: "pre-line" }}
                                >
                                    {r.message}
                                </div>

                                {r.plan && r.state !== "cancelled" && (
                                    <PlanCard
                                        plan={r.plan}
                                        response={r}
                                        busy={busy}
                                        executed={!!r.executed}
                                        onConfirm={() => onConfirm(turn.id)}
                                        onCancel={() => onCancel(turn.id)}
                                    />
                                )}

                                {r.comm && r.state !== "cancelled" && (
                                    <CommPreviewCard
                                        comm={r.comm}
                                        response={r}
                                        busy={busy}
                                        executed={!!r.executed}
                                        onConfirm={() => onConfirm(turn.id)}
                                        onCancel={() => onCancel(turn.id)}
                                    />
                                )}

                                {r.sub && r.state !== "cancelled" && (
                                    <SubmissionPreviewCard
                                        sub={r.sub}
                                        response={r}
                                        busy={busy}
                                        executed={!!r.executed}
                                        onConfirm={() => onConfirm(turn.id)}
                                        onCancel={() => onCancel(turn.id)}
                                    />
                                )}

                                {r.clarification && (
                                    <ClarificationCard
                                        clarification={r.clarification}
                                        onPick={(o) => onPick(o)}
                                    />
                                )}

                                {r.status && <StatusCard status={r.status} />}

                                {r.inbound && <InboundCard inbound={r.inbound} />}

                                {r.intelligence && (
                                    <IntelligenceCard
                                        intel={r.intelligence}
                                        response={r}
                                        busy={busy}
                                        executed={!!r.executed}
                                        onConfirm={(extra) => onConfirm(turn.id, extra)}
                                        onCancel={() => onCancel(turn.id)}
                                    />
                                )}

                                {r.answer && <AnswerCard answer={r.answer} />}
                            </div>
                        )}
                    </div>
                );
            })}
        </div>
    );
}
