import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { adminApi, getSubdomainUrl } from "@/lib/api";
import { toast } from "sonner";
import { ArrowLeft, ArrowRight, Check, Copy, Download, ExternalLink, Loader2 } from "lucide-react";
import { buildRosterPayload, countEnabled, countImages, DEFAULT_ROSTER_LIMITS, toggleField } from "@/lib/rosterSelection";
import { clearRosterSnapshot } from "@/lib/talentRosterCache";
import TalentStep, { ROSTER_DIRECTORY_KEY } from "@/components/roster/TalentStep";
import ImageStep from "@/components/roster/ImageStep";
import { useRosterMedia } from "@/components/roster/useRosterMedia";
import { useDirectoryFacets } from "@/components/roster/useDirectoryFacets";
import { usePdfDownload } from "@/components/roster/pdfDownload";

const STEPS = ["Details", "Talents", "Images"];

export default function RosterBuilder({ editId = null }) {
    const nav = useNavigate();
    const [step, setStep] = useState(1);
    const [title, setTitle] = useState("Talentgram X ");
    const [subtitle, setSubtitle] = useState("");
    // Selected talents, in roster order: [{id, name, thumb}]
    const [selected, setSelected] = useState([]);
    const [saving, setSaving] = useState(false);
    const [created, setCreated] = useState(null); // saved link
    const pdf = usePdfDownload();
    const [loadingEdit, setLoadingEdit] = useState(Boolean(editId));
    const editSelectionRef = useRef(null);
    // Roster-level field visibility. The registry (groups, labels, defaults) comes from the
    // server so the builder never hard-codes which Global Talent fields exist.
    const [registry, setRegistry] = useState(null);
    const [fields, setFields] = useState(null);
    // A fresh wizard starts with fresh talent filters; within one wizard the Talents step restores its
    // filters / page / results when you come back to it from the Images step.
    useState(() => { clearRosterSnapshot(ROSTER_DIRECTORY_KEY); });
    const [allowPdf, setAllowPdf] = useState(true); // "Allow PDF Download" — ON for new rosters and for any saved roster without the setting

    useEffect(() => {
        let live = true;
        adminApi.get("/roster/fields").then(({ data }) => {
            if (!live) return;
            setRegistry(data);
            setFields((prev) => prev || data.defaults);
        }).catch(() => toast.error("Couldn't load the field options"));
        return () => { live = false; };
    }, []);

    // ---- edit: hydrate from the saved roster --------------------------------
    useEffect(() => {
        if (!editId) return undefined;
        let live = true;
        (async () => {
            try {
                const { data } = await adminApi.get(`/links/${editId}`);
                if (!live) return;
                setTitle(data.title || "");
                setSubtitle(data.roster?.subtitle || "");
                // Saved field config wins; a roster made before this feature has none and
                // (like the public page) falls back to the standard defaults.
                if (data.roster?.fields) setFields(data.roster.fields);
                setAllowPdf(data.roster?.allow_pdf_download !== false);
                const entries = data.roster?.talents || [];
                editSelectionRef.current = Object.fromEntries(entries.map((e) => [e.talent_id, e.media_ids || []]));
                const ids = entries.map((e) => e.talent_id);
                const { data: bulk } = await adminApi.post("/talents/bulk", { ids });
                const list = Array.isArray(bulk) ? bulk : bulk?.items || bulk?.data || [];
                const byId = Object.fromEntries(list.map((t) => [t.id, t]));
                setSelected(ids.filter((i) => byId[i]).map((i) => ({
                    id: i, name: byId[i].name, thumb: byId[i].cover_thumbnail_url || byId[i].image_url || null, full: byId[i].image_url || null,
                })));
            } catch {
                toast.error("Couldn't load this roster");
            } finally {
                if (live) setLoadingEdit(false);
            }
        })();
        return () => { live = false; };
    }, [editId]);

    const limits = { ...DEFAULT_ROSTER_LIMITS, ...(registry?.limits || {}) };
    const selectedIds = useMemo(() => selected.map((t) => t.id), [selected]);
    const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
    const facets = useDirectoryFacets(step >= 2);
    const media = useRosterMedia({ selectedIds, enabled: step === 3, editSelectionRef, limits });
    const { mediaByTalent } = media;

    // ---- save ---------------------------------------------------------------
    const save = async () => {
        if (saving) return;
        setSaving(true);
        try {
            const payload = buildRosterPayload({ title, subtitle, talentIds: selectedIds, mediaByTalent, fields, allowPdfDownload: allowPdf });
            const { data } = editId
                ? await adminApi.put(`/links/${editId}`, payload)
                : await adminApi.post("/links", payload);
            setCreated(data);
            toast.success(editId ? "Roster updated" : "Roster created");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't save the roster");
        } finally {
            setSaving(false);
        }
    };

    const totalImages = countImages(mediaByTalent, selectedIds);
    const canNext1 = title.trim().length > 0;
    const canNext2 = selected.length > 0;
    const canSave = selected.length > 0 && totalImages > 0 && media.ready;

    /* ------------------------------ Done screen ----------------------------- */
    if (created) {
        const url = `${getSubdomainUrl("links")}/${created.slug}`;
        return (
            <div className="p-6 md:p-10 max-w-2xl mx-auto text-center" data-testid="roster-created">
                <div className="w-12 h-12 rounded-full bg-black text-white mx-auto flex items-center justify-center mb-6">
                    <Check className="w-6 h-6" />
                </div>
                <p className="eyebrow mb-3">{editId ? "Roster Updated" : "Roster Created"}</p>
                <h1 className="font-display text-3xl md:text-4xl tracking-tight text-black/90 break-words">{created.title}</h1>
                <p className="text-sm text-black/50 mt-3">
                    {created.talent_ids?.length || 0} talent{(created.talent_ids?.length || 0) === 1 ? "" : "s"} · {totalImages} image{totalImages === 1 ? "" : "s"}
                </p>
                <p className="text-[11px] text-black/45 font-mono mt-4 break-all">/l/{created.slug}</p>
                <div className="flex flex-col sm:flex-row gap-3 justify-center mt-8">
                    <a href={`/l/${created.slug}`} target="_blank" rel="noreferrer" data-testid="roster-open"
                        className="inline-flex items-center justify-center gap-2 bg-black text-white px-6 min-h-[44px] rounded-lg text-xs font-medium hover:bg-black/90">
                        <ExternalLink className="w-4 h-4" /> Open Roster
                    </a>
                    <button type="button" data-testid="roster-copy"
                        onClick={async () => { try { await navigator.clipboard.writeText(url); toast.success("Link copied"); } catch { toast.error("Couldn't copy — copy it manually"); } }}
                        className="inline-flex items-center justify-center gap-2 border border-black/[0.12] px-6 min-h-[44px] rounded-lg text-xs font-medium text-black/75 hover:bg-black/[0.03]">
                        <Copy className="w-4 h-4" /> Copy Link
                    </button>
                    <button type="button" data-testid="roster-pdf" disabled={pdf.busy}
                        onClick={() => pdf.download(created.id, created.title)}
                        className="inline-flex items-center justify-center gap-2 border border-black/[0.12] px-6 min-h-[44px] rounded-lg text-xs font-medium text-black/75 hover:bg-black/[0.03] disabled:opacity-60">
                        {pdf.busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                        {pdf.busy ? `Preparing PDF… ${pdf.elapsed}s` : "Download PDF"}
                    </button>
                </div>
                {pdf.busy && <p className="text-xs text-black/45 mt-4" role="status">Large rosters can take a minute — keep this page open.</p>}
                <Link to="/admin/links" className="inline-block mt-8 text-xs text-black/45 hover:text-black/80">Back to Generated Links</Link>
            </div>
        );
    }

    if (loadingEdit) {
        return <div className="p-10 text-sm text-black/45" data-testid="roster-loading">Loading roster…</div>;
    }

    return (
        <div className="p-6 md:p-10 max-w-6xl mx-auto pb-32" data-testid="roster-builder">
            <Link to="/admin/links" className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6">
                <ArrowLeft className="w-3 h-3" /> Back
            </Link>
            <p className="eyebrow mb-3">{editId ? "Edit Roster" : "New Link"} · M4</p>
            <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">Roster / PDF</h1>

            <ol className="flex flex-wrap items-center gap-y-2 gap-x-2 mt-6 mb-8 text-[11px] tracking-widest uppercase" aria-label="Progress">
                {STEPS.map((s, i) => (
                    <li key={s} className="flex items-center gap-2">
                        <span className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-full border ${step === i + 1 ? "bg-black text-white border-black" : step > i + 1 ? "border-black/30 text-black/70" : "border-black/10 text-black/35"}`}>
                            <span className="font-mono">{String(i + 1).padStart(2, "0")}</span>{s}
                        </span>
                        {i < STEPS.length - 1 && <span className="w-4 h-px bg-black/15" />}
                    </li>
                ))}
            </ol>

            {step === 1 && (
                <section className="bg-white border border-black/[0.08] rounded-xl p-6 md:p-8 max-w-2xl" data-testid="roster-step-details">
                    <label className="block text-[11px] tracking-widest uppercase text-black/45 mb-2" htmlFor="roster-title">Roster name</label>
                    <input id="roster-title" data-testid="roster-title" value={title} onChange={(e) => setTitle(e.target.value)}
                        placeholder="Talentgram X Pepsi" maxLength={120}
                        className="w-full border border-black/[0.12] rounded-lg px-4 min-h-[48px] text-base focus:outline-none focus:border-black/50" />
                    <p className="text-xs text-black/45 mt-2">Appears on the cover and as the link title.</p>
                    <label className="block text-[11px] tracking-widest uppercase text-black/45 mt-6 mb-2" htmlFor="roster-subtitle">Subtitle <span className="normal-case tracking-normal">(optional)</span></label>
                    <input id="roster-subtitle" data-testid="roster-subtitle" value={subtitle} onChange={(e) => setSubtitle(e.target.value)}
                        placeholder="Selected Talent — October 2026" maxLength={120}
                        className="w-full border border-black/[0.12] rounded-lg px-4 min-h-[48px] text-base focus:outline-none focus:border-black/50" />

                    <div className="mt-8 pt-6 border-t border-black/[0.08] flex items-center justify-between gap-4" data-testid="roster-pdf-setting">
                        <div className="min-w-0">
                            <h2 className="text-[11px] tracking-widest uppercase text-black/45">Allow PDF Download</h2>
                            <p className="text-xs text-black/45 mt-1">Allow clients to download the roster as a PDF.</p>
                        </div>
                        <button type="button" role="switch" aria-checked={allowPdf} aria-label="Allow PDF Download" data-testid="roster-allow-pdf"
                            onClick={() => setAllowPdf((v) => !v)} className="shrink-0 min-w-[48px] min-h-[44px] flex items-center justify-end">
                            <span className={`relative inline-flex h-6 w-11 rounded-full transition-colors ${allowPdf ? "bg-black" : "bg-black/20"}`}>
                                <span className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${allowPdf ? "translate-x-5" : ""}`} />
                            </span>
                        </button>
                    </div>

                    {registry && fields && (
                        <div className="mt-8 pt-6 border-t border-black/[0.08]" data-testid="roster-fields">
                            <div className="flex items-baseline justify-between mb-1">
                                <h2 className="text-[11px] tracking-widest uppercase text-black/45">Information shown</h2>
                                <span className="text-[11px] text-black/40" data-testid="roster-fields-count">{countEnabled(fields)} selected</span>
                            </div>
                            <p className="text-xs text-black/45 mb-4">Pulled from each talent&apos;s Global Talent profile. Empty values are simply left out.</p>
                            <div className="space-y-4">
                                {registry.groups.map((g) => (
                                    <div key={g.key}>
                                        <div className="text-[10px] tracking-widest uppercase text-black/35 mb-2">{g.label}</div>
                                        <div className="flex flex-wrap gap-2">
                                            {g.fields.map((f) => {
                                                const on = !!fields[f.key];
                                                return (
                                                    <button key={f.key} type="button" role="switch" aria-checked={on}
                                                        data-testid={`roster-field-${f.key}`}
                                                        onClick={() => setFields((cur) => toggleField(cur, f.key))}
                                                        className={`inline-flex items-center gap-1.5 px-3.5 min-h-[40px] rounded-full text-xs font-medium border transition-colors ${on ? "bg-black text-white border-black" : "bg-white text-black/55 border-black/[0.14] hover:border-black/40"}`}>
                                                        {on && <Check className="w-3.5 h-3.5" />}{f.label}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </section>
            )}

            {step === 2 && (
                <TalentStep selected={selected} setSelected={setSelected} selectedSet={selectedSet} maxTalents={limits.max_talents}
                    availableTags={facets.tags} availableLocations={facets.locations} />
            )}

            {step === 3 && (
                <ImageStep selected={selected} options={media.options} mediaByTalent={mediaByTalent} updateMedia={media.updateMedia}
                    loading={media.loading} loadedCount={media.loadedCount} failedIds={media.failedIds} onRetry={media.retry}
                    maxPer={limits.max_images_per_talent} />
            )}

            <div className="fixed inset-x-0 bottom-0 z-30 bg-white/95 backdrop-blur border-t border-black/[0.08] px-4 md:px-10 py-3"
                style={{ paddingBottom: "max(12px, env(safe-area-inset-bottom))" }}>
                <div className="max-w-6xl mx-auto flex items-center justify-between gap-3">
                    <div className="text-xs text-black/55" data-testid="roster-summary">
                        {selected.length} talent{selected.length === 1 ? "" : "s"}
                        {step === 3 && <> · <span data-testid="roster-image-total">{totalImages} of {limits.max_total_images}</span> image{totalImages === 1 ? "" : "s"}</>}
                        {saving && totalImages > 300 && <span className="block text-black/40" role="status">Saving — large rosters can take up to a minute…</span>}
                    </div>
                    <div className="flex items-center gap-2">
                        {step > 1 && (
                            <button type="button" data-testid="roster-back" onClick={() => setStep(step - 1)}
                                className="inline-flex items-center gap-2 border border-black/[0.12] px-4 min-h-[44px] rounded-lg text-xs font-medium text-black/70 hover:bg-black/[0.03]">
                                <ArrowLeft className="w-3.5 h-3.5" /> Back
                            </button>
                        )}
                        {step < 3 ? (
                            <button type="button" data-testid="roster-next" onClick={() => setStep(step + 1)}
                                disabled={step === 1 ? !canNext1 : !canNext2}
                                className="inline-flex items-center gap-2 bg-black text-white px-5 min-h-[44px] rounded-lg text-xs font-medium hover:bg-black/90 disabled:opacity-40">
                                Next <ArrowRight className="w-3.5 h-3.5" />
                            </button>
                        ) : (
                            <button type="button" data-testid="roster-save" onClick={save} disabled={!canSave || saving}
                                className="inline-flex items-center gap-2 bg-black text-white px-5 min-h-[44px] rounded-lg text-xs font-medium hover:bg-black/90 disabled:opacity-40">
                                {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                                {editId ? "Update Roster" : "Create Roster"}
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
