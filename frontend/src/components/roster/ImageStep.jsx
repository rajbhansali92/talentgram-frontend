import React, { memo, useCallback, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, ImageOff, Loader2, Star, Video } from "lucide-react";
import { makeHero, moveItem, selectAllIds, toggleId } from "@/lib/rosterSelection";

const EMPTY = [];

// Off-screen talent sections are skipped by the browser's layout/paint (they stay in the DOM, so find-in-page,
// keyboard focus and tab order keep working); the intrinsic size keeps the scrollbar stable, so there is no jump.
const SECTION_STYLE = { contentVisibility: "auto", containIntrinsicSize: "auto 560px" };

const smallBtn = "px-3 min-h-[40px] text-xs border border-black/[0.12] rounded-lg text-black/65 hover:bg-black/[0.03]";

/** A thumbnail with a placeholder while it loads and a visible fallback if it fails — selection still works. */
function Thumb({ src }) {
    const [failed, setFailed] = useState(false);
    if (!src || failed) {
        return <span className="absolute inset-0 flex items-center justify-center text-black/25" data-testid="roster-img-unavailable"><ImageOff className="w-5 h-5" aria-hidden /></span>;
    }
    return <img src={src} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} className="w-full h-full object-cover" />;
}

/**
 * One selectable image. Memoised on primitives only, so toggling an image re-renders just that tile — and the
 * tile whose order badge changes — never the ~1,000 others.
 */
const ImageTile = memo(function ImageTile({ id, src, label, on, order, onToggle }) {
    return (
        <button type="button" aria-pressed={on} aria-label={label} data-testid={`roster-img-${id}`} onClick={() => onToggle(id)}
            className={`relative aspect-[3/4] rounded-md overflow-hidden bg-[#f3f2ef] border ${on ? "border-black ring-2 ring-black" : "border-black/10 hover:border-black/40"}`}>
            <Thumb src={src} />
            {on && <span className="absolute top-1 right-1 min-w-[20px] h-5 px-1 rounded-full bg-black text-white text-[10px] font-medium flex items-center justify-center">{order}</span>}
        </button>
    );
});

/** The "Order in roster" strip tile. */
const OrderTile = memo(function OrderTile({ id, src, i, last, onMoveEarlier, onMoveLater, onHero }) {
    return (
        <li className="shrink-0 w-24">
            <div className="relative aspect-[3/4] rounded-md overflow-hidden bg-[#f3f2ef] border border-black/10">
                <Thumb src={src} />
                {i === 0 && <span className="absolute top-1 left-1 text-[9px] tracking-widest uppercase bg-black text-white px-1.5 py-0.5 rounded">Hero</span>}
            </div>
            <div className="flex items-center justify-between mt-1">
                <button type="button" aria-label="Move earlier" disabled={i === 0} onClick={() => onMoveEarlier(i)} className="p-2 text-black/50 hover:text-black disabled:opacity-25"><ChevronLeft className="w-4 h-4" /></button>
                {i > 0 ? (
                    <button type="button" aria-label="Make hero" title="Make hero" onClick={() => onHero(id)} className="p-2 text-black/50 hover:text-black"><Star className="w-4 h-4" /></button>
                ) : <span className="w-8" />}
                <button type="button" aria-label="Move later" disabled={last} onClick={() => onMoveLater(i)} className="p-2 text-black/50 hover:text-black disabled:opacity-25"><ChevronRight className="w-4 h-4" /></button>
            </div>
        </li>
    );
});

/**
 * One talent's comp-card section. Memoised: it re-renders only when ITS OWN props change — its picked ids
 * (`sel`, a new array only when this talent's picks change), its options, or its position — so ticking an image
 * on one talent costs one section, regardless of how many talents the roster holds.
 */
const TalentImages = memo(function TalentImages({ talentId, index, opt, sel, maxPer, onChange }) {
    const thumbById = useMemo(() => {
        const m = new Map();
        for (const g of opt.groups) for (const i of g.items) m.set(i.id, i.thumb_url || i.url);
        return m;
    }, [opt]);
    const orderById = useMemo(() => new Map(sel.map((id, i) => [id, i + 1])), [sel]);
    const empty = useMemo(() => opt.groups.every((g) => g.items.length === 0), [opt]);

    const toggle = useCallback((id) => onChange(talentId, (c) => toggleId(c, id, maxPer)), [onChange, talentId, maxPer]);
    const earlier = useCallback((i) => onChange(talentId, (c) => moveItem(c, i, i - 1)), [onChange, talentId]);
    const later = useCallback((i) => onChange(talentId, (c) => moveItem(c, i, i + 1)), [onChange, talentId]);
    const hero = useCallback((id) => onChange(talentId, (c) => makeHero(c, id)), [onChange, talentId]);

    return (
        <section className="bg-white border border-black/[0.08] rounded-xl p-4 md:p-6" style={SECTION_STYLE} data-testid={`roster-images-${talentId}`}>
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
                <div>
                    <div className="flex items-baseline gap-3">
                        <span className="text-[11px] font-mono text-black/40">{String(index + 1).padStart(2, "0")}</span>
                        <h2 className="font-display text-xl text-black/90">{opt.short_name}</h2>
                    </div>
                    <p className="text-xs text-black/45 mt-1 flex items-center gap-2">
                        <span data-testid={`roster-count-${talentId}`}>{sel.length} of {maxPer} images</span>
                        <span className="inline-flex items-center gap-1"><Video className="w-3 h-3" /> {opt.has_video ? "Introduction video included" : "No introduction video"}</span>
                    </p>
                </div>
                <div className="flex gap-2">
                    <button type="button" onClick={() => onChange(talentId, (c) => selectAllIds(c, opt.groups, maxPer))} className={smallBtn}>Select all</button>
                    <button type="button" onClick={() => onChange(talentId, (c) => (c.length ? EMPTY : c))} className={smallBtn}>Deselect all</button>
                    <button type="button" onClick={() => onChange(talentId, () => opt.default_media_ids)} className={smallBtn}>Suggested</button>
                </div>
            </div>

            {sel.length > 0 && (
                <div className="mb-5 p-3 rounded-lg bg-[#faf9f7] border border-black/[0.06]" data-testid={`roster-order-${talentId}`}>
                    <div className="text-[10px] tracking-widest uppercase text-black/40 mb-2">Order in roster</div>
                    <ol className="flex gap-3 overflow-x-auto pb-1">
                        {sel.map((id, i) => (
                            <OrderTile key={id} id={id} src={thumbById.get(id)} i={i} last={i === sel.length - 1}
                                onMoveEarlier={earlier} onMoveLater={later} onHero={hero} />
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
                            const order = orderById.get(m.id);
                            return (
                                <ImageTile key={m.id} id={m.id} src={m.thumb_url || m.url} label={`${g.label} image ${idx + 1}`}
                                    on={order !== undefined} order={order || 0} onToggle={toggle} />
                            );
                        })}
                    </div>
                </div>
            ))}
        </section>
    );
});

/* ------------------------------ Step 3: images ------------------------------ */
export default function ImageStep({ selected, options, mediaByTalent, updateMedia, loading, loadedCount, failedIds, onRetry, maxPer }) {
    if (loading && !loadedCount) {
        return <p className="text-sm text-black/45 py-10" data-testid="roster-images-loading">Loading images…</p>;
    }
    return (
        <div className="space-y-6" data-testid="roster-step-images">
            <p className="text-sm text-black/55 max-w-2xl">
                Choose the images for each comp card. The <strong>first</strong> image is the hero. We pre-selected a curated few — adjust freely.
                The introduction video is included automatically when a talent has one.
            </p>
            {failedIds.length > 0 && (
                <div role="alert" className="flex flex-wrap items-center gap-3 border border-red-200 bg-red-50 text-red-700 rounded-lg px-4 py-3 text-sm" data-testid="roster-images-failed">
                    Couldn&apos;t load images for {failedIds.length} talent{failedIds.length === 1 ? "" : "s"}. They can&apos;t be saved until they load.
                    <button type="button" onClick={onRetry} className="underline font-medium" data-testid="roster-images-retry">Retry</button>
                </div>
            )}
            {loading && (
                <p className="flex items-center gap-2 text-xs text-black/45" data-testid="roster-images-progress" aria-live="polite">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading images… {loadedCount} of {selected.length} talents
                </p>
            )}
            {selected.map((t, ti) => {
                const opt = options[t.id];
                if (!opt) return null;
                return (
                    <TalentImages key={t.id} talentId={t.id} index={ti} opt={opt} sel={mediaByTalent[t.id] || EMPTY}
                        maxPer={maxPer} onChange={updateMedia} />
                );
            })}
        </div>
    );
}
