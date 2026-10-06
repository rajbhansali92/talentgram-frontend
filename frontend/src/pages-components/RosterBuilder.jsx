import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { adminApi, getSubdomainUrl } from "@/lib/api";
import { toast } from "sonner";
import {
    ArrowLeft, ArrowRight, Check, ChevronDown, ChevronUp, ChevronLeft, ChevronRight,
    Copy, Download, ExternalLink, GripVertical, Loader2, Search, Star, Video, X,
} from "lucide-react";
import {
    buildRosterPayload, makeHero, moveItem, pruneSelection, selectAllIds, shortTalentName, toggleId,
} from "@/lib/rosterSelection";

const MAX_IMAGES_PER_TALENT = 12;
const STEPS = ["Details", "Talents", "Images"];

async function downloadAdminPdf(linkId, title, setBusy) {
    setBusy(true);
    try {
        const res = await adminApi.get(`/links/${linkId}/roster/pdf`, { responseType: "blob" });
        const url = URL.createObjectURL(res.data);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${(title || "Talentgram Roster").replace(/[^A-Za-z0-9 _.-]/g, "").trim() || "Talentgram Roster"}.pdf`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (e) {
        toast.error("Couldn't generate the PDF. Please try again in a moment.");
    } finally {
        setBusy(false);
    }
}

export default function RosterBuilder({ editId = null }) {
    const nav = useNavigate();
    const [step, setStep] = useState(1);
    const [title, setTitle] = useState("Talentgram X ");
    const [subtitle, setSubtitle] = useState("");
    // Selected talents, in roster order: [{id, name, thumb}]
    const [selected, setSelected] = useState([]);
    const [options, setOptions] = useState({}); // talent_id -> media-options entry
    const [mediaByTalent, setMediaByTalent] = useState({}); // talent_id -> ordered media ids
    const [saving, setSaving] = useState(false);
    const [loadingOptions, setLoadingOptions] = useState(false);
    const [created, setCreated] = useState(null); // saved link
    const [pdfBusy, setPdfBusy] = useState(false);
    const [loadingEdit, setLoadingEdit] = useState(Boolean(editId));
    const editSelectionRef = useRef(null);

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
                const entries = data.roster?.talents || [];
                editSelectionRef.current = Object.fromEntries(entries.map((e) => [e.talent_id, e.media_ids || []]));
                const ids = entries.map((e) => e.talent_id);
                const { data: bulk } = await adminApi.post("/talents/bulk", { ids });
                const list = Array.isArray(bulk) ? bulk : bulk?.items || bulk?.data || [];
                const byId = Object.fromEntries(list.map((t) => [t.id, t]));
                setSelected(ids.filter((i) => byId[i]).map((i) => ({
                    id: i, name: byId[i].name, thumb: byId[i].cover_thumbnail_url || byId[i].image_url || null,
                })));
            } catch {
                toast.error("Couldn't load this roster");
            } finally {
                if (live) setLoadingEdit(false);
            }
        })();
        return () => { live = false; };
    }, [editId]);

    const selectedIds = useMemo(() => selected.map((t) => t.id), [selected]);
    const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

    // ---- step 3: (re)load media options for the selected talents ------------
    const loadOptions = useCallback(async () => {
        if (!selectedIds.length) return;
        setLoadingOptions(true);
        try {
            const { data } = await adminApi.post("/roster/media-options", { talent_ids: selectedIds });
            const map = Object.fromEntries(data.talents.map((t) => [t.talent_id, t]));
            setOptions(map);
            setMediaByTalent((prev) => {
                const next = {};
                for (const id of selectedIds) {
                    const opt = map[id];
                    if (!opt) continue;
                    if (prev[id]) next[id] = pruneSelection(prev[id], opt.groups);
                    else if (editSelectionRef.current?.[id]) next[id] = pruneSelection(editSelectionRef.current[id], opt.groups);
                    else next[id] = opt.default_media_ids;
                }
                return next;
            });
        } catch {
            toast.error("Couldn't load the talents' images");
        } finally {
            setLoadingOptions(false);
        }
    }, [selectedIds]);

    useEffect(() => { if (step === 3) loadOptions(); }, [step, loadOptions]);

    // ---- save ---------------------------------------------------------------
    const save = async () => {
        if (saving) return;
        setSaving(true);
        try {
            const payload = buildRosterPayload({ title, subtitle, talentIds: selectedIds, mediaByTalent });
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

    const totalImages = selectedIds.reduce((n, id) => n + (mediaByTalent[id]?.length || 0), 0);
    const canNext1 = title.trim().length > 0;
    const canNext2 = selected.length > 0;
    const canSave = selected.length > 0 && totalImages > 0 && !loadingOptions;

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
                    <button type="button" data-testid="roster-pdf" disabled={pdfBusy}
                        onClick={() => downloadAdminPdf(created.id, created.title, setPdfBusy)}
                        className="inline-flex items-center justify-center gap-2 border border-black/[0.12] px-6 min-h-[44px] rounded-lg text-xs font-medium text-black/75 hover:bg-black/[0.03] disabled:opacity-60">
                        {pdfBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                        {pdfBusy ? "Preparing PDF…" : "Download PDF"}
                    </button>
                </div>
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

            <ol className="flex items-center gap-2 mt-6 mb-8 text-[11px] tracking-widest uppercase" aria-label="Progress">
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
                </section>
            )}

            {step === 2 && (
                <TalentStep selected={selected} setSelected={setSelected} selectedSet={selectedSet} />
            )}

            {step === 3 && (
                <ImageStep selected={selected} options={options} mediaByTalent={mediaByTalent}
                    setMediaByTalent={setMediaByTalent} loading={loadingOptions} />
            )}

            <div className="fixed inset-x-0 bottom-0 z-30 bg-white/95 backdrop-blur border-t border-black/[0.08] px-4 md:px-10 py-3"
                style={{ paddingBottom: "max(12px, env(safe-area-inset-bottom))" }}>
                <div className="max-w-6xl mx-auto flex items-center justify-between gap-3">
                    <div className="text-xs text-black/55" data-testid="roster-summary">
                        {selected.length} talent{selected.length === 1 ? "" : "s"}
                        {step === 3 && <> · {totalImages} image{totalImages === 1 ? "" : "s"}</>}
                    </div>
                    <div className="flex items-center gap-2">
                        {step > 1 && (
                            <button type="button" onClick={() => setStep(step - 1)}
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

/* ------------------------------ Step 2: talents ----------------------------- */
function TalentStep({ selected, setSelected, selectedSet }) {
    const [q, setQ] = useState("");
    const [gender, setGender] = useState("");
    const [results, setResults] = useState([]);
    const [page, setPage] = useState(0);
    const [hasMore, setHasMore] = useState(false);
    const [loading, setLoading] = useState(false);
    const dragIndex = useRef(null);
    const debounce = useRef(null);
    const seq = useRef(0);

    const fetchPage = useCallback(async (p, reset) => {
        const mine = ++seq.current;
        setLoading(true);
        try {
            const params = { page: p, size: 24 };
            if (q.trim()) params.q = q.trim();
            if (gender) params.gender = gender;
            const { data } = await adminApi.get("/talents", { params });
            if (mine !== seq.current) return;
            const items = data.items || data.data || [];
            setResults((prev) => (reset ? items : [...prev, ...items]));
            setHasMore(Boolean(data.has_more));
            setPage(p);
        } catch {
            if (mine === seq.current) toast.error("Couldn't load talents");
        } finally {
            if (mine === seq.current) setLoading(false);
        }
    }, [q, gender]);

    useEffect(() => {
        clearTimeout(debounce.current);
        debounce.current = setTimeout(() => fetchPage(0, true), 250);
        return () => clearTimeout(debounce.current);
    }, [fetchPage]);

    const add = (t) => setSelected((prev) => (prev.some((x) => x.id === t.id) ? prev : [...prev, {
        id: t.id, name: t.name, thumb: t.cover_thumbnail_url || t.image_url || null,
    }]));
    const remove = (id) => setSelected((prev) => prev.filter((t) => t.id !== id));
    const move = (i, d) => setSelected((prev) => moveItem(prev, i, i + d));

    return (
        <div className="grid lg:grid-cols-[1fr_340px] gap-6" data-testid="roster-step-talents">
            <section>
                <div className="flex flex-col sm:flex-row gap-3 mb-4">
                    <div className="relative flex-1">
                        <Search className="w-4 h-4 text-black/35 absolute left-3.5 top-1/2 -translate-y-1/2" />
                        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search talents by name, city, skill…"
                            data-testid="roster-search" aria-label="Search talents"
                            className="w-full border border-black/[0.12] rounded-lg pl-10 pr-4 min-h-[44px] text-sm focus:outline-none focus:border-black/50 bg-white" />
                    </div>
                    <div className="flex gap-2" role="group" aria-label="Gender filter">
                        {[["", "All"], ["female", "Female"], ["male", "Male"]].map(([v, label]) => (
                            <button key={v || "all"} type="button" onClick={() => setGender(v)} aria-pressed={gender === v}
                                className={`px-4 min-h-[44px] rounded-lg text-xs font-medium border ${gender === v ? "bg-black text-white border-black" : "border-black/[0.12] text-black/65 hover:bg-black/[0.03] bg-white"}`}>
                                {label}
                            </button>
                        ))}
                    </div>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3" data-testid="roster-talent-grid">
                    {results.map((t) => {
                        const on = selectedSet.has(t.id);
                        return (
                            <button key={t.id} type="button" onClick={() => (on ? remove(t.id) : add(t))} aria-pressed={on}
                                data-testid={`roster-talent-${t.id}`}
                                className={`relative text-left rounded-xl overflow-hidden border bg-white transition-colors ${on ? "border-black ring-1 ring-black" : "border-black/[0.08] hover:border-black/30"}`}>
                                <div className="aspect-[3/4] bg-[#f3f2ef]">
                                    {(t.cover_thumbnail_url || t.image_url) && (
                                        <img src={t.cover_thumbnail_url || t.image_url} alt="" loading="lazy" className="w-full h-full object-cover" />
                                    )}
                                </div>
                                <div className="p-2.5">
                                    <div className="text-sm font-medium text-black/85 truncate">{t.name}</div>
                                    <div className="text-[11px] text-black/45 truncate">{[t.age && `${t.age}y`, t.height].filter(Boolean).join(" · ") || "—"}</div>
                                </div>
                                <span className={`absolute top-2 right-2 w-6 h-6 rounded-full flex items-center justify-center border ${on ? "bg-black text-white border-black" : "bg-white/90 border-black/20"}`}>
                                    {on && <Check className="w-3.5 h-3.5" />}
                                </span>
                            </button>
                        );
                    })}
                </div>
                {!loading && results.length === 0 && <p className="text-sm text-black/45 py-10 text-center">No talents found.</p>}
                {loading && <p className="text-sm text-black/45 py-6 text-center">Loading…</p>}
                {hasMore && !loading && (
                    <div className="text-center mt-5">
                        <button type="button" onClick={() => fetchPage(page + 1, false)}
                            className="px-5 min-h-[44px] border border-black/[0.12] rounded-lg text-xs font-medium text-black/70 hover:bg-black/[0.03] bg-white">Load more</button>
                    </div>
                )}
            </section>

            <aside className="lg:sticky lg:top-6 self-start bg-white border border-black/[0.08] rounded-xl p-4" data-testid="roster-selected">
                <div className="flex items-baseline justify-between mb-3">
                    <h2 className="font-display text-base text-black/85">Selected Talents</h2>
                    <span className="text-xs text-black/50" data-testid="roster-selected-count">{selected.length}</span>
                </div>
                {selected.length === 0 ? (
                    <p className="text-xs text-black/45 py-4">Pick talents on the left. Drag to reorder — this is the order they appear in the roster.</p>
                ) : (
                    <ol className="space-y-2 max-h-[60vh] overflow-auto pr-1">
                        {selected.map((t, i) => (
                            <li key={t.id} draggable
                                onDragStart={() => { dragIndex.current = i; }}
                                onDragOver={(e) => e.preventDefault()}
                                onDrop={() => { if (dragIndex.current !== null) setSelected((p) => moveItem(p, dragIndex.current, i)); dragIndex.current = null; }}
                                className="flex items-center gap-2 border border-black/[0.08] rounded-lg p-2 bg-white" data-testid={`roster-selected-${t.id}`}>
                                <GripVertical className="w-4 h-4 text-black/30 cursor-grab shrink-0" aria-hidden />
                                <span className="text-[11px] font-mono text-black/40 w-5">{String(i + 1).padStart(2, "0")}</span>
                                <div className="w-9 h-11 rounded bg-[#f3f2ef] overflow-hidden shrink-0">
                                    {t.thumb && <img src={t.thumb} alt="" className="w-full h-full object-cover" />}
                                </div>
                                <div className="min-w-0 flex-1">
                                    <div className="text-sm text-black/85 truncate">{t.name}</div>
                                    <div className="text-[11px] text-black/45">as {shortTalentName(t.name)}</div>
                                </div>
                                <div className="flex flex-col">
                                    <button type="button" aria-label={`Move ${t.name} up`} disabled={i === 0} onClick={() => move(i, -1)} className="p-1 text-black/50 hover:text-black disabled:opacity-25"><ChevronUp className="w-4 h-4" /></button>
                                    <button type="button" aria-label={`Move ${t.name} down`} disabled={i === selected.length - 1} onClick={() => move(i, 1)} className="p-1 text-black/50 hover:text-black disabled:opacity-25"><ChevronDown className="w-4 h-4" /></button>
                                </div>
                                <button type="button" aria-label={`Remove ${t.name}`} onClick={() => remove(t.id)} className="p-1.5 text-black/40 hover:text-red-600"><X className="w-4 h-4" /></button>
                            </li>
                        ))}
                    </ol>
                )}
            </aside>
        </div>
    );
}

/* ------------------------------ Step 3: images ------------------------------ */
function ImageStep({ selected, options, mediaByTalent, setMediaByTalent, loading }) {
    if (loading && !Object.keys(options).length) {
        return <p className="text-sm text-black/45 py-10" data-testid="roster-images-loading">Loading images…</p>;
    }
    return (
        <div className="space-y-6" data-testid="roster-step-images">
            <p className="text-sm text-black/55 max-w-2xl">
                Choose the images for each comp card. The <strong>first</strong> image is the hero. We pre-selected a curated few — adjust freely.
                The introduction video is included automatically when a talent has one.
            </p>
            {selected.map((t, ti) => {
                const opt = options[t.id];
                if (!opt) return null;
                const sel = mediaByTalent[t.id] || [];
                const urlById = Object.fromEntries(opt.groups.flatMap((g) => g.items.map((i) => [i.id, i.url])));
                const set = (fn) => setMediaByTalent((prev) => ({ ...prev, [t.id]: fn(prev[t.id] || []) }));
                const empty = opt.groups.every((g) => g.items.length === 0);
                return (
                    <section key={t.id} className="bg-white border border-black/[0.08] rounded-xl p-4 md:p-6" data-testid={`roster-images-${t.id}`}>
                        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
                            <div>
                                <div className="flex items-baseline gap-3">
                                    <span className="text-[11px] font-mono text-black/40">{String(ti + 1).padStart(2, "0")}</span>
                                    <h2 className="font-display text-xl text-black/90">{opt.short_name}</h2>
                                </div>
                                <p className="text-xs text-black/45 mt-1 flex items-center gap-2">
                                    <span data-testid={`roster-count-${t.id}`}>{sel.length} of {MAX_IMAGES_PER_TALENT} images</span>
                                    <span className="inline-flex items-center gap-1"><Video className="w-3 h-3" /> {opt.has_video ? "Introduction video included" : "No introduction video"}</span>
                                </p>
                            </div>
                            <div className="flex gap-2">
                                <button type="button" onClick={() => set((c) => selectAllIds(c, opt.groups, MAX_IMAGES_PER_TALENT))}
                                    className="px-3 min-h-[40px] text-xs border border-black/[0.12] rounded-lg text-black/65 hover:bg-black/[0.03]">Select all</button>
                                <button type="button" onClick={() => set(() => [])}
                                    className="px-3 min-h-[40px] text-xs border border-black/[0.12] rounded-lg text-black/65 hover:bg-black/[0.03]">Deselect all</button>
                                <button type="button" onClick={() => set(() => opt.default_media_ids)}
                                    className="px-3 min-h-[40px] text-xs border border-black/[0.12] rounded-lg text-black/65 hover:bg-black/[0.03]">Suggested</button>
                            </div>
                        </div>

                        {sel.length > 0 && (
                            <div className="mb-5 p-3 rounded-lg bg-[#faf9f7] border border-black/[0.06]" data-testid={`roster-order-${t.id}`}>
                                <div className="text-[10px] tracking-widest uppercase text-black/40 mb-2">Order in roster</div>
                                <ol className="flex gap-3 overflow-x-auto pb-1">
                                    {sel.map((id, i) => (
                                        <li key={id} className="shrink-0 w-24">
                                            <div className="relative aspect-[3/4] rounded-md overflow-hidden bg-[#f3f2ef] border border-black/10">
                                                <img src={urlById[id]} alt="" loading="lazy" className="w-full h-full object-cover" />
                                                {i === 0 && <span className="absolute top-1 left-1 text-[9px] tracking-widest uppercase bg-black text-white px-1.5 py-0.5 rounded">Hero</span>}
                                            </div>
                                            <div className="flex items-center justify-between mt-1">
                                                <button type="button" aria-label="Move earlier" disabled={i === 0} onClick={() => set((c) => moveItem(c, i, i - 1))} className="p-1.5 text-black/50 hover:text-black disabled:opacity-25"><ChevronLeft className="w-4 h-4" /></button>
                                                {i > 0 ? (
                                                    <button type="button" aria-label="Make hero" title="Make hero" onClick={() => set((c) => makeHero(c, id))} className="p-1.5 text-black/50 hover:text-black"><Star className="w-3.5 h-3.5" /></button>
                                                ) : <span className="w-6" />}
                                                <button type="button" aria-label="Move later" disabled={i === sel.length - 1} onClick={() => set((c) => moveItem(c, i, i + 1))} className="p-1.5 text-black/50 hover:text-black disabled:opacity-25"><ChevronRight className="w-4 h-4" /></button>
                                            </div>
                                        </li>
                                    ))}
                                </ol>
                            </div>
                        )}

                        {empty && <p className="text-sm text-black/45">This talent has no Indian, Western or Additional Portfolio images to choose from.</p>}
                        {opt.groups.filter((g) => g.items.length > 0).map((g) => (
                            <div key={g.key} className="mb-5 last:mb-0">
                                <h3 className="text-[11px] tracking-widest uppercase text-black/45 mb-2">{g.label}</h3>
                                <div className="grid grid-cols-4 sm:grid-cols-6 lg:grid-cols-8 gap-2">
                                    {g.items.map((m, idx) => {
                                        const on = sel.includes(m.id);
                                        const order = sel.indexOf(m.id) + 1;
                                        return (
                                            <button key={m.id} type="button" aria-pressed={on} aria-label={`${g.label} image ${idx + 1}`}
                                                data-testid={`roster-img-${m.id}`}
                                                onClick={() => set((c) => toggleId(c, m.id, MAX_IMAGES_PER_TALENT))}
                                                className={`relative aspect-[3/4] rounded-md overflow-hidden bg-[#f3f2ef] border ${on ? "border-black ring-2 ring-black" : "border-black/10 hover:border-black/40"}`}>
                                                <img src={m.url} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
                                                {on && <span className="absolute top-1 right-1 min-w-[20px] h-5 px-1 rounded-full bg-black text-white text-[10px] font-medium flex items-center justify-center">{order}</span>}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        ))}
                    </section>
                );
            })}
        </div>
    );
}
