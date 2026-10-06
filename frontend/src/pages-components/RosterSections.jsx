'use client';

import React, { memo, useCallback, useEffect, useRef, useState } from "react";
import { api as axios, IMAGE_URL, getViewerToken } from "@/lib/api";
import Logo from "@/components/Logo";
import RosterShell from "@/pages-components/RosterShell";
import { toast } from "sonner";
import { Check, ChevronLeft, ChevronRight, ExternalLink, Heart, Instagram, LayoutGrid, Play, X } from "lucide-react";
import { arrowStep, clampIndex, neighbourImages, pad2, swipeStep } from "@/lib/rosterCarousel";

// Shared branding (the PDF cover uses the same destination/handle).
const INSTAGRAM_URL = "https://www.instagram.com/talentgram.agency/";
const INSTAGRAM_HANDLE = "@talentgram.agency";

function getSessionId() {
    try { return sessionStorage.getItem("tg_session_id") || undefined; } catch { return undefined; }
}

/**
 * Public Roster — one vertically scrolling page, ONE SECTION PER TALENT. The client scrolls to move
 * from talent to talent; there is no talent-to-talent navigation. Inside a section the only
 * carousel is that talent's own images, and the Shortlist button belongs to that talent alone.
 * Everything shown comes from the server payload, which is already limited to this roster's
 * talents, selected images and configured fields.
 */
export default function RosterSections({ data, slug, pdfButton }) {
    const talents = data.talents;
    const total = talents.length;
    const [shortlisted, setShortlisted] = useState(() => new Set(data.shortlisted || []));
    const [pending, setPending] = useState(() => new Set());
    const [indexOpen, setIndexOpen] = useState(false);
    const shortRef = useRef(shortlisted);
    shortRef.current = shortlisted;
    const pendingRef = useRef(new Set());

    const toggleShortlist = useCallback(async (key) => {
        if (pendingRef.current.has(key)) return;               // a double tap while saving is ignored
        const next = !shortRef.current.has(key);
        const apply = (on) => setShortlisted((s) => { const n = new Set(s); if (on) n.add(key); else n.delete(key); return n; });
        apply(next);                                            // immediate feedback, this talent only
        pendingRef.current.add(key);
        setPending(new Set(pendingRef.current));
        try {
            await axios.post(`/public/links/${slug}/roster/shortlist`, { key, shortlisted: next, session_id: getSessionId() },
                { headers: { Authorization: `Bearer ${getViewerToken(slug)}` } });
        } catch {
            apply(!next);                                       // never show a state the server doesn't have
            toast.error("Couldn't update your shortlist. Please try again.");
        } finally {
            pendingRef.current.delete(key);
            setPending(new Set(pendingRef.current));
        }
    }, [slug]);

    const jumpTo = (i) => {
        setIndexOpen(false);
        document.getElementById(talents[i].key)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    };

    const headerRight = (
        <>
            {shortlisted.size > 0 && (
                <span className="hidden md:inline text-[11px] tracking-[0.12em] uppercase text-black/45 mr-1" data-testid="roster-shortlist-count">{shortlisted.size} shortlisted</span>
            )}
            {total > 1 && (
                <button type="button" onClick={() => setIndexOpen(true)} data-testid="roster-index-open" aria-label="All talents"
                    className="inline-flex items-center justify-center gap-2 min-w-[40px] min-h-[40px] px-2.5 rounded-lg text-xs font-medium text-black/70 hover:bg-black/[0.05]">
                    <LayoutGrid className="w-[18px] h-[18px]" strokeWidth={1.6} /><span className="hidden md:inline">All talents</span>
                </button>
            )}
            {pdfButton}
        </>
    );

    return (
        <RosterShell title={data.title} right={headerRight}>
            {/* Cover — compact, so the first talent is nearly in view */}
            <section className="px-4 sm:px-6 pt-10 pb-10 md:pt-14 md:pb-12 text-center flex flex-col items-center" data-testid="roster-cover">
                <Logo size={72} forceVariant="black" className="mb-8 md:mb-10" />
                <h1 className="font-display text-2xl sm:text-3xl md:text-4xl tracking-[0.14em] uppercase break-words max-w-4xl mx-auto" data-testid="roster-title">{data.title}</h1>
                <div className="w-10 h-px bg-black mx-auto my-6" />
                {data.subtitle && <p className="text-xs tracking-[0.22em] uppercase text-black/45" data-testid="roster-subtitle">{data.subtitle}</p>}
                <p className="text-xs text-black/40 mt-3">{total} talent{total === 1 ? "" : "s"}</p>
            </section>

            {total === 0 ? (
                <div className="py-20 px-6 text-center text-sm text-black/55" data-testid="roster-empty">This roster has no available talents.</div>
            ) : (
                <main data-testid="roster-sections">
                    {talents.map((t, i) => (
                        <TalentSection key={t.key} t={t} position={i + 1} total={total} first={i === 0}
                            short={shortlisted.has(t.key)} busy={pending.has(t.key)} onToggle={toggleShortlist} />
                    ))}
                </main>
            )}

            <footer className="flex justify-center py-14 border-t border-black/[0.08]"><InstagramLink /></footer>

            {indexOpen && <TalentIndex talents={talents} shortlisted={shortlisted} onPick={jumpTo} onClose={() => setIndexOpen(false)} />}
        </RosterShell>
    );
}

/** True while the element is within ~one screen of the viewport (images are mounted only then). */
function useNearViewport(ref, fallback) {
    const [near, setNear] = useState(false);
    useEffect(() => {
        const el = ref.current;
        if (!el) return undefined;
        if (typeof IntersectionObserver === "undefined") { setNear(fallback); return undefined; }
        const io = new IntersectionObserver(([e]) => setNear(e.isIntersecting), { rootMargin: "100% 0px" });
        io.observe(el);
        return () => io.disconnect();
    }, [ref, fallback]);
    return near;
}

/** One talent = one section = its own carousel state, its own Shortlist button. */
const TalentSection = memo(function TalentSection({ t, position, total, first, short, busy, onToggle }) {
    const images = t.images;
    const [ii, setIi] = useState(0);
    const heroRef = useRef(null);
    const touch = useRef(null);
    const warmed = useRef(new Set());
    const near = useNearViewport(heroRef, position <= 3);
    const cur = images[ii];
    const multi = images.length > 1;
    const step = useCallback((d) => setIi((i) => clampIndex(i + d, images.length)), [images.length]);

    // Warm only this talent's neighbouring images, and only while its carousel is on screen.
    useEffect(() => {
        if (!near || typeof window === "undefined" || !window.Image) return;
        for (const im of neighbourImages(images, ii)) {
            const url = IMAGE_URL(im);
            if (!url || warmed.current.has(url)) continue;
            warmed.current.add(url);
            const pre = new window.Image();
            pre.decoding = "async";
            pre.src = url;
        }
    }, [near, images, ii]);

    return (
        <section id={t.key} data-testid={`roster-talent-${t.key}`} aria-label={t.name}
            className={`scroll-mt-[72px] py-9 md:py-14 ${first ? "" : "border-t border-black/[0.08]"}`}>
            <div className="max-w-[1280px] mx-auto px-3 sm:px-6 grid gap-x-14 gap-y-5 lg:grid-cols-[minmax(0,1.3fr)_minmax(320px,0.7fr)]">
                {/* name — above the hero on small screens, top of the right column on desktop */}
                <div className="min-w-0 lg:col-start-2 lg:row-start-1 lg:pt-4">
                    <p className="text-[11px] font-mono tracking-[0.18em] text-black/40" data-testid={`roster-position-${t.key}`}>{pad2(position)} / {pad2(total)}</p>
                    <h2 className="font-display text-3xl lg:text-5xl tracking-[0.03em] uppercase mt-2 break-words" data-testid={`roster-name-${t.key}`}>{t.name}</h2>
                    <div className="w-6 h-px bg-black mt-5" />
                </div>

                {/* hero carousel — this talent's images only */}
                <div ref={heroRef} tabIndex={0} role="group" aria-roledescription="image carousel" aria-label={`${t.name} images`} data-testid={`roster-hero-${t.key}`}
                    onKeyDown={(e) => { const d = arrowStep(e); if (d) { e.preventDefault(); step(d); } }}
                    onTouchStart={(e) => { const p = e.touches?.[0]; touch.current = p ? { x: p.clientX, y: p.clientY } : null; }}
                    onTouchEnd={(e) => {
                        const p = e.changedTouches?.[0]; const s = touch.current; touch.current = null;
                        if (!p || !s) return;
                        const d = swipeStep(p.clientX - s.x, p.clientY - s.y);
                        if (d) step(d);
                    }}
                    style={{ touchAction: "pan-y" }}
                    className="relative w-full max-w-[680px] mx-auto lg:max-w-none lg:mx-0 bg-[#ecebe7] overflow-hidden outline-none focus-visible:ring-2 focus-visible:ring-black/60
                               h-[min(calc((100vw-24px)*1.25),calc(100dvh-210px))] min-h-[260px] lg:h-[min(calc(100dvh-129px),820px)] lg:min-h-[480px] lg:col-start-1 lg:row-start-1 lg:row-span-2">
                    {near && cur && (
                        <img key={cur.id} src={IMAGE_URL(cur)} alt={`${t.name} — image ${ii + 1} of ${images.length}`} data-testid={`roster-image-${t.key}`}
                            draggable={false} decoding="async" className="absolute inset-0 w-full h-full object-contain select-none" />
                    )}
                    {multi && <ArrowButton dir="prev" label={`Previous image of ${t.name}`} disabled={ii === 0} onClick={() => step(-1)} tk={t.key} />}
                    {multi && <ArrowButton dir="next" label={`Next image of ${t.name}`} disabled={ii === images.length - 1} onClick={() => step(1)} tk={t.key} />}
                    {multi && (
                        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 px-3 py-1 rounded-full bg-white/80 backdrop-blur text-[11px] font-mono tracking-[0.18em] text-black/70"
                            aria-live="polite" data-testid={`roster-counter-${t.key}`}>{pad2(ii + 1)} / {pad2(images.length)}</div>
                    )}
                </div>

                {/* details */}
                <div className="min-w-0 lg:col-start-2 lg:row-start-2 lg:self-start">
                    {t.info.length > 0 && (
                        <dl className="grid grid-cols-2 gap-x-6 gap-y-5" data-testid={`roster-info-${t.key}`}>
                            {t.info.map((it) => (
                                <div key={it.key || it.label} data-testid={`roster-field-${t.key}-${it.key}`}
                                    className={`min-w-0 ${it.block || it.key === "location" || it.value.length > 16 ? "col-span-2" : ""}`}>
                                    <dt className="text-[10px] tracking-[0.16em] uppercase text-black/40">{it.label}</dt>
                                    <dd className="text-base lg:text-lg mt-1 break-words [overflow-wrap:anywhere]">
                                        {it.href ? <a href={it.href} target="_blank" rel="noreferrer" className="inline-block py-3 -my-3 underline-offset-2 hover:underline">{it.value}</a> : it.value}
                                    </dd>
                                </div>
                            ))}
                        </dl>
                    )}
                    <div className={`flex flex-col gap-3 ${t.info.length > 0 ? "mt-7" : ""}`}>
                        <button type="button" onClick={() => onToggle(t.key)} aria-pressed={short} aria-busy={busy} data-testid={`roster-shortlist-${t.key}`}
                            className={`min-h-[50px] rounded-lg text-sm font-medium inline-flex items-center justify-center gap-2 border transition-colors ${short ? "bg-black text-white border-black" : "bg-white text-black border-black/70 hover:bg-black hover:text-white"}`}>
                            {short ? <Check className="w-4 h-4" /> : <Heart className="w-4 h-4" />}
                            {short ? "Shortlisted" : "Shortlist"}
                        </button>
                        {t.video && (
                            <a href={t.video.url} target="_blank" rel="noreferrer" data-testid={`roster-video-${t.key}`}
                                className="inline-flex items-center justify-center gap-2.5 border border-black/25 rounded-lg min-h-[48px] px-7 text-[11px] tracking-[0.16em] uppercase font-semibold text-black hover:border-black hover:bg-black hover:text-white transition-colors">
                                <Play className="w-3.5 h-3.5 fill-current" /> Introduction Video <ExternalLink className="w-3.5 h-3.5 opacity-50" />
                            </a>
                        )}
                    </div>
                </div>
            </div>
        </section>
    );
});

function ArrowButton({ dir, label, disabled, onClick, tk }) {
    const Icon = dir === "prev" ? ChevronLeft : ChevronRight;
    return (
        <button type="button" onClick={onClick} disabled={disabled} aria-label={label} data-testid={`roster-${dir}-${tk}`}
            className={`absolute top-1/2 -translate-y-1/2 ${dir === "prev" ? "left-2.5" : "right-2.5"} w-11 h-11 rounded-full bg-white/85 backdrop-blur border border-black/10 shadow-sm text-black/80 hover:bg-white flex items-center justify-center transition-opacity disabled:opacity-30 disabled:pointer-events-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-black`}>
            <Icon className="w-5 h-5" strokeWidth={1.6} />
        </button>
    );
}

function InstagramLink({ className = "" }) {
    return (
        <a href={INSTAGRAM_URL} target="_blank" rel="noreferrer" aria-label={`Talentgram on Instagram ${INSTAGRAM_HANDLE}`} data-testid="roster-instagram"
            className={`inline-flex items-center gap-2.5 min-h-[44px] px-2 text-[12px] tracking-[0.08em] font-medium text-black/80 hover:text-black ${className}`}>
            <Instagram className="w-[18px] h-[18px]" strokeWidth={1.6} /> {INSTAGRAM_HANDLE}
        </a>
    );
}

/** Compact "jump to" list (it scrolls the page — it does not replace it). Mounted only while open. */
function TalentIndex({ talents, shortlisted, onPick, onClose }) {
    useEffect(() => {
        const onKey = (e) => { if (e.key === "Escape") onClose(); };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);
    return (
        <div className="fixed inset-0 z-40 bg-white overflow-y-auto" role="dialog" aria-modal="true" aria-label="All talents" data-testid="roster-index">
            <div className="sticky top-0 bg-white/95 backdrop-blur border-b border-black/[0.06] flex items-center justify-between px-4 h-[57px]">
                <span className="text-[11px] tracking-[0.2em] uppercase text-black/50">All talents · {talents.length}</span>
                <button type="button" onClick={onClose} aria-label="Close" data-testid="roster-index-close" className="min-w-[44px] min-h-[44px] flex items-center justify-center"><X className="w-5 h-5" /></button>
            </div>
            <ol className="max-w-2xl mx-auto px-3 py-3">
                {talents.map((x, i) => (
                    <li key={x.key}>
                        <button type="button" onClick={() => onPick(i)} data-testid={`roster-index-${x.key}`}
                            className="w-full flex items-center gap-4 min-h-[48px] px-3 rounded-lg text-left hover:bg-black/[0.04]">
                            <span className="font-mono text-[11px] opacity-60 w-6">{pad2(i + 1)}</span>
                            <span className="flex-1 min-w-0 truncate uppercase tracking-[0.06em] text-sm">{x.name}</span>
                            {shortlisted.has(x.key) && <Check className="w-4 h-4 shrink-0" aria-label="Shortlisted" />}
                        </button>
                    </li>
                ))}
            </ol>
        </div>
    );
}
