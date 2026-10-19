import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Check, Copy, ExternalLink, Loader2, RotateCcw, User, X } from "lucide-react";
import { STATUS_META, getAssistantWorkers, statusLabel } from "@/lib/personalAssistant";

const TONES = {
    amber: "bg-amber-50 text-amber-800 border-amber-200",
    blue: "bg-sky-50 text-sky-800 border-sky-200",
    green: "bg-emerald-50 text-emerald-800 border-emerald-200",
    red: "bg-red-50 text-red-700 border-red-200",
    slate: "bg-black/[0.04] text-black/65 border-black/10",
};

export function StatusChip({ status, kind, uncertain = false }) {
    const isUncertain = uncertain && status === "failed";
    const m = { ...(STATUS_META[status] || { tone: "slate" }), label: statusLabel(status, kind, uncertain), ...(isUncertain ? { tone: "amber" } : {}) };
    return (
        <span data-testid="pa-status" className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-medium whitespace-nowrap ${TONES[m.tone]}`}>
            {(status === "running" || status === "queued") && <Loader2 className="w-3 h-3 animate-spin" aria-hidden />}
            {m.label}
        </span>
    );
}

function Thumb({ src, alt = "", className = "" }) {
    const [bad, setBad] = useState(false);
    if (!src || bad) return <span className={`bg-black/[0.06] flex items-center justify-center text-black/30 ${className}`}><User className="w-4 h-4" aria-hidden /></span>;
    return <img src={src} alt={alt} loading="lazy" decoding="async" onError={() => setBad(true)} className={`object-cover ${className}`} />;
}

const IDENTIFY = { talent: "Who do you mean?", project: "Which project?", template: "Which template?", recipient: "Where should it go?", stage: "Which stage?" };
const CONSEQUENTIAL = ["add", "send", "move", "share"];
const STATUS_CHIP = { ongoing: "Ongoing", complete: "Complete", completed: "Complete", hold: "On hold", locked: "Locked" };

/**
 * Tap-to-pick options (talents with photo + details, projects with their status, templates, stages) plus a search box.
 * A card the user taps is the user's own decision for THAT question only: it is visibly marked while it is being saved and
 * is sent by ID. A name the user types is searched on the server and comes back as cards to tap — typing never resolves
 * silently, and the original command stays visible above.
 */
function ClarifySlot({ slot, busy, onPick, command }) {
    const [text, setText] = useState("");
    const [picked, setPicked] = useState(null);
    const [searched, setSearched] = useState("");
    useEffect(() => { if (!busy) setPicked(null); }, [busy]);
    const searchable = slot.kind === "talent" || slot.kind === "project" || slot.kind === "template" || slot.kind === "recipient";
    const noMatch = searchable && !slot.options?.length;
    return (
        <div className="mt-2" data-testid={`pa-slot-${slot.key}`}>
            {command && <p className="text-[11px] text-black/45 mb-1.5" data-testid="pa-command-echo">Your command: “{command}”</p>}
            <p className="text-xs text-black/70 mb-2">{slot.prompt}</p>
            {searched && slot.options?.length > 0 && <p className="text-[11px] text-black/50 mb-1.5" data-testid="pa-search-note">Closest matches for “{searched}” — tap the right one:</p>}
            {slot.options?.length > 0 && (
                <ul className="grid gap-1.5">
                    {slot.options.map((o) => {
                        const on = picked === o.id;
                        const chip = slot.kind === "project" ? (STATUS_CHIP[o.status] || (o.status ? String(o.status) : "")) : "";
                        const sub = chip && o.sublabel ? o.sublabel.replace(new RegExp(`^${chip}\\s*·?\\s*`, "i"), "") : o.sublabel;      // the status is the chip, not repeated
                        return (
                            <li key={o.id}>
                                <button type="button" disabled={busy} aria-pressed={on} onClick={() => { setPicked(o.id); onPick(slot.key, { choice_id: o.id }); }} data-testid={`pa-option-${o.id}`}
                                    className={`w-full flex items-center gap-2.5 text-left p-2 rounded-lg border bg-white disabled:opacity-60 min-h-[44px] ${on ? "border-black ring-1 ring-black" : o.best ? "border-emerald-500/70" : "border-black/[0.1] hover:border-black/40"}`}>
                                    {slot.kind === "talent" && <Thumb src={o.thumb} className="w-9 h-11 rounded shrink-0" />}
                                    <span className="min-w-0 flex-1">
                                        <span className="block text-sm text-black/85 truncate">{o.label}</span>
                                        {sub && <span className="block text-[11px] text-black/50 truncate">{sub}</span>}
                                    </span>
                                    <span className="shrink-0 flex flex-col items-end gap-0.5">
                                        {o.best && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-800 border border-emerald-200" data-testid={`pa-best-${o.id}`}>Best match</span>}
                                        {chip && <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${o.status === "ongoing" ? "bg-sky-50 text-sky-800 border-sky-200" : "bg-black/[0.04] text-black/60 border-black/10"}`}>{chip}</span>}
                                        {on && <span className="text-[10px] text-black/70 inline-flex items-center gap-1"><Check className="w-3 h-3" aria-hidden /> Selected</span>}
                                    </span>
                                </button>
                            </li>
                        );
                    })}
                </ul>
            )}
            {noMatch && <p className="text-xs text-amber-700 mt-1" data-testid="pa-no-match" role="status">{searched ? `Nothing matched “${searched}”.` : "No close matches."} Try another spelling or just part of the name.</p>}
            <form className="flex gap-2 mt-2" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { setSearched(text.trim()); onPick(slot.key, { text: text.trim() }); setText(""); } }}>
                <input value={text} onChange={(e) => setText(e.target.value)} placeholder={slot.options?.length ? "Not here? Type the name again…" : "Type the name again…"} aria-label={`Search again: ${slot.prompt}`}
                    className="flex-1 min-w-0 border border-black/[0.12] rounded-lg px-3 min-h-[40px] text-sm focus:outline-none focus:border-black/50" />
                <button type="submit" disabled={busy || !text.trim()} className="px-3 min-h-[40px] rounded-lg border border-black/[0.12] text-xs disabled:opacity-40">Search</button>
            </form>
        </div>
    );
}

/** Live worker availability from the backend (existing registry + sessions). Nothing is chosen for the user when several are connected. */
export function WorkerPicker({ value, onChange, hint, disabled }) {
    const [workers, setWorkers] = useState(null);
    useEffect(() => {
        let live = true;
        const load = () => getAssistantWorkers().then((w) => { if (live) setWorkers(w); }).catch(() => { if (live) setWorkers([]); });
        load();
        const t = setInterval(load, 8000);
        return () => { live = false; clearInterval(t); };
    }, []);
    useEffect(() => {
        if (!workers) return;
        const ready = workers.filter((w) => w.ready);
        if (value && ready.some((w) => w.id === value)) return;
        const hinted = hint && ready.find((w) => w.label.toLowerCase() === hint.toLowerCase());
        onChange(hinted ? hinted.id : ready.length === 1 ? ready[0].id : "");
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [workers]);
    if (!workers) return <p className="text-[11px] text-black/45 flex items-center gap-1.5"><Loader2 className="w-3 h-3 animate-spin" /> Checking WhatsApp workers…</p>;
    const anyReady = workers.some((w) => w.ready);
    return (
        <div data-testid="pa-workers">
            <p className="text-[10px] tracking-widest uppercase text-black/40 mb-1.5">Send via</p>
            <div role="radiogroup" aria-label="Send via" className="flex flex-wrap gap-1.5">
                {workers.map((w) => (
                    <button key={w.id} type="button" role="radio" aria-checked={value === w.id} disabled={!w.ready || disabled} onClick={() => onChange(w.id)} data-testid={`pa-worker-${w.id}`}
                        className={`inline-flex items-center gap-1.5 px-2.5 min-h-[36px] rounded-lg border text-xs ${value === w.id ? "bg-black text-white border-black" : "bg-white border-black/[0.12]"} ${!w.ready ? "opacity-45 cursor-not-allowed" : ""}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${w.ready ? "bg-emerald-500" : "bg-red-400"}`} />
                        {w.label} <span className={value === w.id ? "text-white/70" : "text-black/45"}>{w.status}</span>
                    </button>
                ))}
            </div>
            {!anyReady && <p className="text-[11px] text-red-600 mt-1.5" data-testid="pa-no-worker">No WhatsApp worker is connected right now, so nothing can be sent.</p>}
        </div>
    );
}

function Outcomes({ rows }) {
    if (!rows?.length) return null;
    return (
        <ul className="mt-2 space-y-0.5 text-[11px]" data-testid="pa-outcomes">
            {rows.slice(0, 20).map((r, i) => (
                <li key={`${r.talent_id || r.name}-${i}`} className="flex gap-2 justify-between">
                    <span className="truncate text-black/70">{r.name && r.project ? `${r.name} → ${r.project}` : (r.name || r.project || "—")}</span>
                    <span className={`shrink-0 ${["sent", "moved", "added", "already_there"].includes(r.status) ? "text-emerald-700" : r.status === "failed" ? "text-red-600" : "text-black/50"}`}>
                        {r.status === "sent" ? (r.verified ? "sent ✓ verified" : "sent") : String(r.status || "").replace(/_/g, " ")}{r.reason || r.error ? ` — ${r.reason || r.error}` : ""}
                    </span>
                </li>
            ))}
        </ul>
    );
}

function mediaContentsText(c) {
    if (!c) return "Anyone with this link can view and download the talent's photos and intro video.";
    const parts = [c.indian && `${c.indian} Indian look`, c.western && `${c.western} Western look`, c.portfolio && `${c.portfolio} portfolio`, c.video && "intro video"].filter(Boolean);
    return `Opens a page where anyone with the link can view and download: ${parts.length ? parts.join(" · ") : "nothing yet"}.`;
}

function CopyBtn({ text }) {
    const [ok, setOk] = useState(false);
    return (
        <button type="button" aria-label="Copy link" onClick={async () => { try { await navigator.clipboard.writeText(text); setOk(true); setTimeout(() => setOk(false), 1500); } catch { /* clipboard blocked */ } }}
            className="inline-flex items-center gap-1 text-[11px] text-black/60 hover:text-black min-h-[32px] px-1">
            {ok ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />} {ok ? "Copied" : "Copy"}
        </button>
    );
}

// Questions ABOUT a talent's projects get the answer, not a profile: no photo strip / portfolio / video / share prompt.
const HISTORY_KINDS = ["tested", "projects", "history", "submissions"];

function TestedAnswer({ card }) {
    const tested = card.tested_projects || [];
    const submitted = card.submitted_projects || [];
    return (
        <div className="mt-2 text-[11px] space-y-1" data-testid="pa-tested">
            {tested.length > 0
                ? <p className="text-emerald-700" data-testid="pa-tested-list">Tested for {tested.length} project{tested.length === 1 ? "" : "s"}: {tested.map((x) => x.project).join(", ")}</p>
                : <p className="text-amber-700" data-testid="pa-tested-none">No project is marked “Already Tested” for {card.label}.</p>}
            {submitted.length > 0
                ? <>
                    <p className="text-black/60" data-testid="pa-submitted-head">Has submitted for {submitted.length} project{submitted.length === 1 ? "" : "s"}:</p>
                    <ul>{submitted.slice(0, 10).map((x, i) => <li key={`${x.project}-${i}`} className="flex justify-between gap-2"><span className="truncate">{x.project}</span><span className="text-black/50 shrink-0">{x.status}{x.date ? ` · ${x.date}` : ""}</span></li>)}</ul>
                    {submitted.length > 10 && <p className="text-black/45">+{submitted.length - 10} more</p>}
                </>
                : <p className="text-black/50" data-testid="pa-submitted-none">No submissions on record either.</p>}
            <p className="text-black/35 text-[10px]">“Tested” means the project's pipeline stage is Already Tested; submissions are listed separately.</p>
        </div>
    );
}

function TalentResult({ card, kind, onPrefill }) {
    const h = card.history;
    const historyOnly = HISTORY_KINDS.includes(kind);
    return (
        <div className="p-2 rounded-lg border border-black/[0.08] bg-white" data-testid={`pa-card-${card.id}`}>
            <div className="flex gap-2.5">
                <Thumb src={card.thumb} className="w-12 h-16 rounded shrink-0" />
                <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-black/90 truncate">{card.label}</p>
                    {card.sublabel && <p className="text-[11px] text-black/50">{card.sublabel}</p>}
                    <div className="flex flex-wrap items-center gap-x-3 mt-1 text-[11px]">
                        <Link to={card.profile_path} className="inline-flex items-center gap-1 text-black/70 underline">Open profile <ExternalLink className="w-3 h-3" /></Link>
                        {!historyOnly && card.portfolio_url && <a href={card.portfolio_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-black/70 underline">Portfolio <ExternalLink className="w-3 h-3" /></a>}
                        {!historyOnly && card.video_url && <a href={card.video_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-black/70 underline">Intro video <ExternalLink className="w-3 h-3" /></a>}
                    </div>
                </div>
            </div>
            {!card.media_link_requested ? null : card.media_link_available ? (
                <div className="mt-1.5 text-[11px] text-black/60" data-testid="pa-media-kit">
                    <p className="flex items-center gap-1.5 min-w-0">
                        <a href={card.media_link} target="_blank" rel="noreferrer" className="truncate underline" data-testid="pa-media-link">{card.media_link}</a>
                        <a href={card.media_link} target="_blank" rel="noreferrer" aria-label="Open media link" className="shrink-0 inline-flex items-center gap-1 min-h-[32px] px-1 hover:text-black">Open <ExternalLink className="w-3 h-3" /></a>
                        <CopyBtn text={card.media_link} />
                    </p>
                    <p className="text-black/50" data-testid="pa-media-contents">{mediaContentsText(card.media_contents)}</p>
                    {card.media_link_reason && <p className="text-amber-700" data-testid="pa-media-reason">{card.media_link_reason}</p>}
                </div>
            ) : (
                <p className="mt-1.5 text-[11px] text-amber-700" data-testid="pa-media-reason">{card.media_link_reason || "Media download link is unavailable for this talent."}</p>
            )}
            {!historyOnly && card.photos?.length > 0 && (
                <div className="flex gap-1.5 mt-1.5 overflow-x-auto" data-testid="pa-photos">
                    {card.photos.map((p, i) => <a key={`${p.url}-${i}`} href={p.url} target="_blank" rel="noreferrer" className="shrink-0"><Thumb src={p.thumb} className="w-12 h-16 rounded" /></a>)}
                </div>
            )}
            {kind === "tested" && <TestedAnswer card={card} />}
            {h && kind !== "tested" && (
                <div className="mt-2 text-[11px] space-y-1" data-testid="pa-history">
                    {card.tested_projects?.length > 0 && <p className="text-emerald-700">Tested for: {card.tested_projects.map((x) => x.project).join(", ")}</p>}
                    {h.projects.length > 0 ? <ul>{h.projects.slice(0, 12).map((x, i) => <li key={`${x.project_id}-${i}`} className="flex justify-between gap-2"><span className="truncate">{x.project}</span><span className="text-black/50 shrink-0">{x.stage}{x.since ? ` · ${x.since}` : ""}</span></li>)}</ul> : <p className="text-black/50">Not in any project yet.</p>}
                    {h.submissions.length > 0 && <><p className="text-black/40 uppercase tracking-wider text-[9px] mt-1">Submissions</p><ul>{h.submissions.slice(0, 10).map((x, i) => <li key={`${x.project_id}-${i}`} className="flex justify-between gap-2"><span className="truncate">{x.project}</span><span className="text-black/50 shrink-0">{x.status}{x.date ? ` · ${x.date}` : ""}</span></li>)}</ul></>}
                </div>
            )}
            {!historyOnly && card.media_link_available && (
                <div className="mt-1.5">
                    <button type="button" onClick={() => onPrefill(`Send ${card.label}'s media link to `)} data-testid="pa-share-whatsapp" className="text-[11px] underline text-black/70 min-h-[32px]">Share on WhatsApp…</button>
                    <p className="text-[10px] text-black/40">Name a project, a saved contact or a number — you'll see the exact destination and approve before anything is sent.</p>
                </div>
            )}
            {!historyOnly && !card.media_link_available && <button type="button" onClick={() => onPrefill(`Share ${card.label}'s media link with `)} className="mt-1.5 text-[11px] underline text-black/60 min-h-[32px]">Share this…</button>}
        </div>
    );
}

function Result({ task, onPrefill, onFollowUp, busy }) {
    const r = task.result;
    if (!r) return null;
    if (task.kind === "lookup") return <div className="mt-2 space-y-2">{r.cards.map((c) => <TalentResult key={c.id} card={c} kind={r.kind} onPrefill={onPrefill} />)}</div>;
    if (task.kind === "list") {
        return (
            <div className="mt-2 space-y-2" data-testid="pa-list">
                {r.groups.map((g) => (
                    <div key={g.project_id}>
                        <p className="text-xs text-black/70">{g.project} · {r.stage} · <strong>{g.count}</strong></p>
                        <ul className="mt-1 grid gap-1">
                            {g.talents.map((t) => <li key={t.id} className="flex items-center gap-2"><Thumb src={t.thumb} className="w-7 h-9 rounded shrink-0" /><span className="text-xs truncate">{t.label}</span><span className="text-[11px] text-black/45 truncate">{t.sublabel}</span></li>)}
                        </ul>
                        {g.count > g.talents.length && <p className="text-[11px] text-black/45 mt-1">+{g.count - g.talents.length} more in the pipeline</p>}
                    </div>
                ))}
            </div>
        );
    }
    if (task.kind === "add") {
        return (
            <div className="mt-2 text-xs text-black/70" data-testid="pa-add-result">
                Added {r.added}{r.already_there ? ` · ${r.already_there} already there` : ""}{r.failed ? ` · ${r.failed} could not be added` : ""}.
                {r.suggestions?.map((s) => (
                    <button key={s.template_id} type="button" disabled={busy} onClick={() => onFollowUp(task, s.template_id)} data-testid="pa-suggest-send"
                        className="ml-2 px-2.5 min-h-[32px] rounded-full border border-black/20 hover:border-black/50 text-[11px] disabled:opacity-50">{s.label}?</button>
                ))}
            </div>
        );
    }
    if (task.kind === "move") return <p className="mt-2 text-xs text-black/70">{r.moved} moved to {r.stage}{r.not_in_project ? ` · ${r.not_in_project} not in the project` : ""}.</p>;
    return null;
}

export default function TaskCard({ task, busy, actionError, inPlan = false, onClarify, onConfirm, onCancel, onRetry, onFollowUp, onPrefill }) {
    const [worker, setWorker] = useState("");
    const [ack, setAck] = useState(false);
    const access = task.kind === "share" && task.status === "awaiting_confirmation" && worker ? task.preview?.worker_access?.[worker] : null;
    const unverified = access?.state === "unknown";
    const blocked = access?.state === "failed_before";
    useEffect(() => { setAck(false); }, [worker, task.fingerprint]);        // an acknowledgement belongs to one worker and one version of the plan
    const needsWorker = (task.kind === "send" || task.kind === "share") && task.status === "awaiting_confirmation";
    const canCancel = ["needs_clarification", "awaiting_confirmation", "ready"].includes(task.status) || task.status === "queued";
    const dep = task.depends_on?.length > 0 && task.status === "awaiting_confirmation";
    const rows = useMemo(() => [...(task.outcomes || []), ...(task.not_queued || [])], [task.outcomes, task.not_queued]);
    return (
        <article className="rounded-xl border border-black/[0.1] bg-white p-3" data-testid={`pa-task-${task.id}`} aria-label={task.title}>
            <header className="flex items-start justify-between gap-2">
                <h4 className="text-[13px] font-medium text-black/90 leading-snug">{task.title}</h4>
                <StatusChip status={task.status} kind={task.kind} uncertain={!!task.uncertain} />
            </header>
            {task.status === "awaiting_confirmation" && (
                <p className="mt-1.5 text-[11px] font-medium text-sky-800" data-testid="pa-approve-head">
                    Approve this action?<span className="font-normal text-black/50"> Nothing has been {task.kind === "send" || task.kind === "share" ? "sent" : "changed"} yet.</span>
                </p>
            )}
            {task.summary?.length > 0 && task.status === "awaiting_confirmation" && (
                <ul className="mt-1.5 text-xs text-black/65 space-y-0.5" data-testid="pa-preview">{task.summary.map((l, i) => <li key={i} className={/twice|skipped|switched off|Not in|not verified|NOT find|can't confirm|also the casting group|is (?!ongoing)\w+, not ongoing/.test(l) ? "text-amber-700" : ""}>{l}</li>)}</ul>
            )}
            {dep && <p className="mt-1 text-[11px] text-black/45">Runs after the step before it succeeds.</p>}
            {task.params?.worker_label && task.status !== "awaiting_confirmation" && <p className="mt-1 text-[11px] text-black/45" data-testid="pa-worker-used">Worker: {task.params.worker_label}</p>}
            {task.status_detail && <p className="mt-1 text-[11px] text-black/55" data-testid="pa-detail">{task.status_detail}</p>}
            {task.error && <p className="mt-1.5 text-xs text-red-700 flex gap-1.5" role="alert"><AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" aria-hidden />{task.error}</p>}
            {actionError && <p className="mt-1.5 text-xs text-red-700" role="alert">{actionError}</p>}
            {task.status === "needs_clarification" && (
                <p className="mt-1.5 text-[11px] font-medium text-amber-800" data-testid="pa-identify-head">
                    {IDENTIFY[task.slots?.[0]?.kind] || "Which one do you mean?"}{CONSEQUENTIAL.includes(task.kind) ? <span className="font-normal text-black/50"> Picking only identifies it — nothing happens until you approve.</span> : null}
                </p>
            )}
            {task.status === "needs_clarification" && task.slots?.map((s, i) => <ClarifySlot key={s.key} slot={s} busy={busy} command={i === 0 ? task.source_text : null} onPick={(key, body) => onClarify(task, key, body)} />)}
            {needsWorker && !inPlan && <div className="mt-2.5"><WorkerPicker value={worker} onChange={setWorker} hint={task.params?.worker_hint} disabled={busy} /></div>}
            {blocked && !inPlan && <p className="mt-1.5 text-xs text-red-700" role="alert" data-testid="pa-group-blocked">“{task.preview?.group}”: this worker could not find the group last time, so it is held. Pick a different worker.</p>}
            {unverified && !inPlan && (
                <label className="mt-1.5 flex items-start gap-2 text-xs text-amber-800" data-testid="pa-group-ack">
                    <input type="checkbox" className="mt-0.5" checked={ack} disabled={busy} onChange={(e) => setAck(e.target.checked)} />
                    <span>{access.label} has never been seen sending to “{task.preview?.group}”, and the app can&apos;t check which chats a worker belongs to. Send anyway — if it isn&apos;t in the group nothing is delivered and the failure is shown.</span>
                </label>
            )}
            <Result task={task} onPrefill={onPrefill} onFollowUp={onFollowUp} busy={busy} />
            <Outcomes rows={["completed", "partial", "failed", "running", "queued", "cancelled"].includes(task.status) ? rows : []} />
            <footer className="flex flex-wrap items-center gap-2 mt-2.5">
                {task.status === "awaiting_confirmation" && !inPlan && (
                    <button type="button" disabled={busy || (needsWorker && !worker) || blocked || (unverified && !ack)} onClick={() => onConfirm(task, worker, unverified && ack)} data-testid="pa-confirm"
                        className="px-3.5 min-h-[40px] rounded-lg bg-black text-white text-xs font-medium disabled:opacity-40 inline-flex items-center gap-1.5">
                        {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />} {task.duplicate_risk ? "Send again anyway" : "Confirm"}
                    </button>
                )}
                {(task.status === "failed" || task.status === "partial") && !(task.uncertain && ["send", "share"].includes(task.kind)) && (task.batch_ids?.length > 0 || !["send", "share"].includes(task.kind)) && (
                    <button type="button" disabled={busy} onClick={() => onRetry(task)} data-testid="pa-retry" className="px-3 min-h-[36px] rounded-lg border border-black/[0.15] text-xs inline-flex items-center gap-1.5 disabled:opacity-50"><RotateCcw className="w-3.5 h-3.5" /> Retry</button>
                )}
                {canCancel && <button type="button" disabled={busy} onClick={() => onCancel(task)} data-testid="pa-cancel" className="px-3 min-h-[36px] rounded-lg border border-black/[0.15] text-xs text-black/65 inline-flex items-center gap-1 disabled:opacity-50"><X className="w-3.5 h-3.5" /> Cancel</button>}
            </footer>
        </article>
    );
}
