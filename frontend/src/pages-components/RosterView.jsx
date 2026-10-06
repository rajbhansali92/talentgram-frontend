'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { api as axios, API, IMAGE_URL, getViewerToken, saveViewerToken } from "@/lib/api";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { ChevronLeft, ChevronRight, Download, ExternalLink, Loader2, Play, X } from "lucide-react";

/**
 * Public Roster / comp-card viewer. Mirrors the PDF: the server sends the same
 * layout plan (mm on an A4 page) that the PDF is drawn from, so ≥768px renders
 * those exact page layouts; on phones the plan is replaced by a natural-ratio
 * stack (a scaled A4 page would make photos and text tiny).
 *
 * Access reuses the Generated Links identify flow (name + email → viewer token
 * bound to this slug), so views / unique viewers are counted by the existing
 * link_views machinery.
 */
const PW = 210;
const PH = 297;
const pct = (v, total) => `${(v / total) * 100}%`;
const mm = (n) => `calc(var(--mm) * ${n})`;

function getBrowserAndDevice() {
    const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
    const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : /Firefox\//.test(ua) ? "Firefox" : "Other";
    const device = /iPhone|Android.*Mobile/.test(ua) ? "Mobile" : /iPad|Tablet/.test(ua) ? "Tablet" : "Desktop";
    return { browser, device };
}

function getSessionId() {
    try {
        let id = sessionStorage.getItem("tg_session_id");
        if (!id) { id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`; sessionStorage.setItem("tg_session_id", id); }
        return id;
    } catch { return undefined; }
}

function useIsDesktop() {
    const [d, setD] = useState(false);
    useEffect(() => {
        const mq = window.matchMedia("(min-width: 768px)");
        const on = () => setD(mq.matches);
        on();
        mq.addEventListener("change", on);
        return () => mq.removeEventListener("change", on);
    }, []);
    return d;
}

export default function RosterView() {
    const { slug } = useParams();
    const [phase, setPhase] = useState("boot"); // boot | identify | loading | ready | inactive | error
    const [data, setData] = useState(null);
    const [meta, setMeta] = useState({ title: "Talentgram Roster", subtitle: null });
    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [busy, setBusy] = useState(false);
    const [active, setActive] = useState("t1");
    const [lightbox, setLightbox] = useState(null); // {images, index}
    const [pdfStarting, setPdfStarting] = useState(false);
    const isDesktop = useIsDesktop();
    const inflight = useRef(false);

    const load = useCallback(async () => {
        setPhase("loading");
        try {
            const res = await axios.get(`/public/links/${slug}/roster`, { headers: { Authorization: `Bearer ${getViewerToken(slug)}` } });
            setData(res.data);
            setPhase("ready");
        } catch (e) {
            const st = e?.response?.status;
            setPhase(st === 401 ? "identify" : st === 403 ? "inactive" : "error");
        }
    }, [slug]);

    useEffect(() => {
        if (!slug) return;
        (async () => {
            try {
                const m = await axios.get(`/public/links/${slug}/meta`);
                setMeta({ title: m.data.title || "Talentgram Roster", subtitle: m.data.subtitle || null });
            } catch { /* meta is cosmetic */ }
            try {
                const saved = JSON.parse(localStorage.getItem(`client_view_${slug}`) || "null");
                if (saved) { setName(saved.name || ""); setEmail(saved.email || ""); }
            } catch { /* ignore */ }
            if (getViewerToken(slug)) load(); else setPhase("identify");
        })();
    }, [slug, load]);

    const identify = async (e) => {
        e.preventDefault();
        if (inflight.current || !name.trim() || !email.trim()) return;
        inflight.current = true;
        setBusy(true);
        try {
            const { browser, device } = getBrowserAndDevice();
            const res = await axios.post(`/public/links/${slug}/identify`, { name: name.trim(), email: email.trim(), browser, device, session_id: getSessionId() });
            if (!res.data?.token) throw new Error("No token");
            saveViewerToken(slug, res.data.token);
            try { localStorage.setItem(`client_view_${slug}`, JSON.stringify({ name: name.trim(), email: email.trim() })); } catch { /* ignore */ }
            await load();
        } catch (err) {
            if (err?.response?.status === 403) setPhase("inactive");
            else toast.error("Please check your name and email and try again.");
        } finally {
            inflight.current = false;
            setBusy(false);
        }
    };

    // active section highlight for the nav
    useEffect(() => {
        if (phase !== "ready" || !data) return undefined;
        const els = data.talents.map((t) => document.getElementById(t.key)).filter(Boolean);
        if (!els.length || typeof IntersectionObserver === "undefined") return undefined;
        const io = new IntersectionObserver((entries) => {
            const vis = entries.filter((en) => en.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
            if (vis) setActive(vis.target.id);
        }, { rootMargin: "-20% 0px -65% 0px" });
        els.forEach((el) => io.observe(el));
        return () => io.disconnect();
    }, [phase, data, isDesktop]);

    useEffect(() => {
        if (!lightbox) return undefined;
        const onKey = (e) => {
            if (e.key === "Escape") setLightbox(null);
            if (e.key === "ArrowRight") setLightbox((l) => l && { ...l, index: (l.index + 1) % l.images.length });
            if (e.key === "ArrowLeft") setLightbox((l) => l && { ...l, index: (l.index - 1 + l.images.length) % l.images.length });
        };
        window.addEventListener("keydown", onKey);
        const prev = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
    }, [lightbox]);

    const downloadPdf = () => {
        if (pdfStarting) return;
        setPdfStarting(true);
        window.location.assign(`${API}/public/links/${slug}/roster/pdf?token=${encodeURIComponent(getViewerToken(slug) || "")}`);
        setTimeout(() => setPdfStarting(false), 12000);
    };

    const goTo = (key) => {
        const el = document.getElementById(key);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    };

    /* ------------------------------- gate / states ------------------------------ */
    if (phase === "boot" || phase === "loading") {
        return (
            <Shell title={meta.title}>
                <div className="py-32 text-center text-sm text-black/45" aria-busy="true" role="status" data-testid="roster-loading">
                    <Loader2 className="w-5 h-5 animate-spin mx-auto mb-3" /> Loading roster…
                </div>
            </Shell>
        );
    }
    if (phase === "identify") {
        return (
            <Shell title={meta.title} bare>
                <div className="min-h-[80vh] flex items-center justify-center px-6" data-testid="roster-identify">
                    <form onSubmit={identify} className="w-full max-w-sm text-center">
                        <div className="flex justify-center mb-10"><Logo size={44} forceVariant="black" /></div>
                        <h1 className="font-display text-2xl tracking-[0.12em] uppercase break-words">{meta.title}</h1>
                        {meta.subtitle && <p className="text-[11px] tracking-[0.2em] uppercase text-black/40 mt-3">{meta.subtitle}</p>}
                        <p className="text-sm text-black/55 mt-8 mb-5">Please introduce yourself to view the roster.</p>
                        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" autoComplete="name" required
                            data-testid="roster-name" className="w-full border border-black/[0.14] rounded-lg px-4 min-h-[48px] text-base mb-3 focus:outline-none focus:border-black/50" />
                        <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" type="email" autoComplete="email" required
                            data-testid="roster-email" className="w-full border border-black/[0.14] rounded-lg px-4 min-h-[48px] text-base mb-4 focus:outline-none focus:border-black/50" />
                        <button type="submit" disabled={busy} data-testid="roster-enter"
                            className="w-full bg-black text-white rounded-lg min-h-[48px] text-sm font-medium disabled:opacity-60">
                            {busy ? "One moment…" : "View Roster"}
                        </button>
                    </form>
                </div>
            </Shell>
        );
    }
    if (phase === "inactive" || phase === "error") {
        return (
            <Shell title={meta.title}>
                <div className="py-28 px-6 text-center max-w-md mx-auto" data-testid="roster-unavailable">
                    <h1 className="font-display text-2xl mb-2">{phase === "inactive" ? "This link is no longer active" : "Couldn’t load the roster"}</h1>
                    <p className="text-sm text-black/55 leading-relaxed">
                        {phase === "inactive" ? "Please contact Talentgram for an updated link." : "Please check your connection and try again."}
                    </p>
                    {phase === "error" && <button onClick={load} className="mt-6 inline-flex items-center justify-center min-h-[44px] px-6 rounded-lg bg-black text-white text-sm font-medium">Try again</button>}
                </div>
            </Shell>
        );
    }

    /* ---------------------------------- ready ----------------------------------- */
    const pdfButton = (cls) => data.pdf_available && (
        <button type="button" onClick={downloadPdf} disabled={pdfStarting} data-testid="roster-download-pdf"
            className={`inline-flex items-center justify-center gap-2 bg-black text-white rounded-lg text-xs font-medium disabled:opacity-70 ${cls}`}>
            {pdfStarting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            {pdfStarting ? "Preparing PDF…" : "Download PDF"}
        </button>
    );

    return (
        <Shell title={data.title} right={pdfButton("px-5 min-h-[40px]")}>
            {/* Cover */}
            <section className="px-4 sm:px-6 pt-16 pb-12 md:pt-24 md:pb-16 text-center" data-testid="roster-cover">
                <p className="text-[10px] tracking-[0.3em] uppercase text-black/40 mb-6">Talent Roster</p>
                <h1 className="font-display text-3xl sm:text-4xl md:text-5xl tracking-[0.14em] uppercase break-words max-w-4xl mx-auto" data-testid="roster-title">{data.title}</h1>
                <div className="w-10 h-px bg-black mx-auto my-7" />
                {data.subtitle && <p className="text-xs tracking-[0.22em] uppercase text-black/45" data-testid="roster-subtitle">{data.subtitle}</p>}
                <p className="text-xs text-black/40 mt-3">{data.talents.length} talent{data.talents.length === 1 ? "" : "s"}</p>
            </section>

            {/* Navigation */}
            {data.talents.length > 1 && (
                <nav aria-label="Talents" data-testid="roster-nav"
                    className="sticky top-[57px] z-20 bg-[#f6f6f4]/95 backdrop-blur border-y border-black/[0.06]">
                    <ol className="flex gap-1 overflow-x-auto px-3 sm:px-6 py-2 max-w-[1400px] mx-auto no-scrollbar">
                        {data.talents.map((t) => (
                            <li key={t.key} className="shrink-0">
                                <button type="button" onClick={() => goTo(t.key)} aria-current={active === t.key ? "true" : undefined}
                                    data-testid={`roster-nav-${t.key}`}
                                    className={`px-3 min-h-[40px] rounded-full text-[11px] tracking-wider uppercase whitespace-nowrap ${active === t.key ? "bg-black text-white" : "text-black/55 hover:text-black hover:bg-black/[0.05]"}`}>
                                    <span className="font-mono opacity-60 mr-1.5">{String(t.index).padStart(2, "0")}</span>{t.name}
                                </button>
                            </li>
                        ))}
                    </ol>
                </nav>
            )}

            <main className="max-w-[1100px] mx-auto px-3 sm:px-6 py-8 md:py-12 space-y-14 md:space-y-20">
                {data.talents.map((t) => (
                    <section key={t.key} id={t.key} className="scroll-mt-[120px]" data-testid={`roster-talent-${t.key}`}>
                        {isDesktop
                            ? <DesktopTalent t={t} total={data.talents.length} onOpen={(images, index) => setLightbox({ images, index })} />
                            : <MobileTalent t={t} total={data.talents.length} onOpen={(images, index) => setLightbox({ images, index })} />}
                    </section>
                ))}
            </main>

            <footer className="text-center text-[10px] tracking-[0.25em] uppercase text-black/35 pb-14">talentgramagency.com</footer>

            {!isDesktop && data.pdf_available && (
                <div className="fixed inset-x-0 bottom-0 z-30 bg-white/95 backdrop-blur border-t border-black/[0.08] px-4 pt-3" style={{ paddingBottom: "max(12px, env(safe-area-inset-bottom))" }}>
                    {pdfButton("w-full min-h-[50px] text-sm rounded-xl")}
                </div>
            )}

            {lightbox && <Lightbox state={lightbox} setState={setLightbox} />}
        </Shell>
    );
}

/* ------------------------------------ shell ----------------------------------- */
function Shell({ title, right, children, bare }) {
    return (
        <div className="min-h-screen bg-[#f6f6f4] text-black/85" data-testid="roster-page">
            {!bare && (
                <header className="sticky top-0 z-30 bg-white/95 backdrop-blur border-b border-black/[0.06]">
                    <div className="max-w-[1400px] mx-auto px-4 sm:px-6 h-[57px] flex items-center justify-between gap-4">
                        <Logo size={26} forceVariant="black" />
                        <div className="hidden md:block flex-1 text-center text-[10px] tracking-[0.25em] uppercase text-black/40 truncate px-4">{title}</div>
                        <div className="hidden md:block">{right}</div>
                    </div>
                </header>
            )}
            {children}
        </div>
    );
}

/* ------------------------------ desktop: page mirror ---------------------------- */
function DesktopTalent({ t, total, onOpen }) {
    const imgById = useMemo(() => Object.fromEntries(t.images.map((i) => [i.id, i])), [t.images]);
    return (
        <div className="space-y-6">
            {t.pages.map((p, pi) => (
                <div key={pi} className="bg-white border border-black/[0.07] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_8px_24px_rgba(0,0,0,0.04)]"
                    style={{ position: "relative", aspectRatio: `${PW} / ${PH}`, containerType: "inline-size", "--mm": "calc(100cqw / 210)" }}
                    data-testid={`roster-page-${t.key}-${pi}`}>
                    {/* header */}
                    <img src="/brand/talentgram-black.png" alt="Talentgram" style={{ position: "absolute", left: pct(14, PW), top: pct(7.5, PH), width: pct(28, PW), height: "auto" }} />
                    <div style={{ position: "absolute", right: pct(14, PW), top: pct(11.2, PH), fontSize: mm(2.4), letterSpacing: "0.14em", color: "#7d7d7a", fontWeight: 500, textTransform: "uppercase" }}>
                        {pi === 0 ? `${String(t.index).padStart(2, "0")} / ${String(total).padStart(2, "0")}` : `${t.name}  ·  ${String(t.index).padStart(2, "0")} / ${String(total).padStart(2, "0")}`}
                    </div>
                    <div style={{ position: "absolute", left: pct(14, PW), right: pct(14, PW), top: pct(22, PH), height: 1, background: "#deded9" }} />

                    {p.kind === "hero" && (
                        <>
                            <div style={{ position: "absolute", left: pct(14, PW), top: pct(30, PH), fontSize: mm(9.4), fontWeight: 600, letterSpacing: "0.035em", textTransform: "uppercase", lineHeight: 1.1, color: "#111" }}>{t.name}</div>
                            <div style={{ position: "absolute", left: pct(14, PW), top: pct(49, PH), width: pct(14, PW), height: 1.5, background: "#111" }} />
                            <InfoRow info={t.info} y={p.info_y} />
                            {t.video && (
                                <a href={t.video.url} target="_blank" rel="noreferrer" data-testid={`roster-video-${t.key}`}
                                    style={{ position: "absolute", left: pct(14, PW), top: pct(p.info_y + 19, PH), width: pct(58, PW), height: pct(9, PH), background: "#111", color: "#fff", display: "flex", alignItems: "center", gap: mm(2.4), paddingLeft: mm(3.6), fontSize: mm(2.2), letterSpacing: "0.16em", fontWeight: 600, textTransform: "uppercase" }}>
                                    <Play style={{ width: mm(3), height: mm(3), fill: "#fff" }} /> Introduction Video
                                </a>
                            )}
                        </>
                    )}

                    {p.slots.map((s) => {
                        const im = imgById[s.media_id];
                        if (!im) return null;
                        const idx = t.images.findIndex((x) => x.id === s.media_id);
                        const c = s.crop;
                        const pos = c && s.fit === "cover"
                            ? `${c[2] >= 1 ? 50 : (c[0] / (1 - c[2])) * 100}% ${c[3] >= 1 ? 50 : (c[1] / (1 - c[3])) * 100}%`
                            : "50% 50%";
                        return (
                            <button key={s.media_id} type="button" onClick={() => onOpen(t.images, idx)} aria-label={`${t.name} image ${idx + 1}`}
                                style={{ position: "absolute", left: pct(s.x, PW), top: pct(s.y, PH), width: pct(s.w, PW), height: pct(s.h, PH), padding: 0, border: 0, cursor: "zoom-in", background: "#f3f2ef", overflow: "hidden" }}>
                                <img src={IMAGE_URL(im)} alt="" loading="lazy" decoding="async" style={{ width: "100%", height: "100%", objectFit: s.fit === "cover" ? "cover" : "contain", objectPosition: pos, display: "block" }} />
                            </button>
                        );
                    })}
                </div>
            ))}
        </div>
    );
}

function InfoRow({ info, y }) {
    if (!info?.length) return null;
    const W = { Age: 0.55, Height: 0.7, Location: 1.9, Instagram: 1.5 };
    const total = info.reduce((n, it) => n + (W[it.label] || 1), 0);
    return (
        <div style={{ position: "absolute", left: pct(14, PW), right: pct(14, PW), top: pct(y - 4, PH), borderTop: "1px solid #deded9", paddingTop: mm(4), display: "flex" }}>
            {info.map((it) => (
                <div key={it.label} style={{ width: `${((W[it.label] || 1) / total) * 100}%`, paddingRight: mm(2) }}>
                    <div style={{ fontSize: mm(2.1), letterSpacing: "0.16em", textTransform: "uppercase", color: "#7d7d7a", fontWeight: 500 }}>{it.label}</div>
                    {it.href
                        ? <a href={it.href} target="_blank" rel="noreferrer" style={{ fontSize: mm(3.5), color: "#111", marginTop: mm(1.4), display: "block", textDecoration: "none" }}>{it.value}</a>
                        : <div style={{ fontSize: mm(3.5), color: "#111", marginTop: mm(1.4) }}>{it.value}</div>}
                </div>
            ))}
        </div>
    );
}

/* ------------------------------- mobile: natural stack --------------------------- */
function MobileTalent({ t, total, onOpen }) {
    const [hero, ...rest] = t.images;
    return (
        <article>
            <div className="flex items-baseline justify-between mb-3">
                <h2 className="font-display text-3xl tracking-[0.03em] uppercase">{t.name}</h2>
                <span className="text-[11px] font-mono text-black/40">{String(t.index).padStart(2, "0")} / {String(total).padStart(2, "0")}</span>
            </div>
            <div className="w-6 h-px bg-black mb-4" />
            {hero && <ImgTile im={hero} onClick={() => onOpen(t.images, 0)} label={`${t.name} hero image`} />}
            {t.info.length > 0 && (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-4 border-t border-black/[0.08] mt-5 pt-4">
                    {t.info.map((it) => (
                        <div key={it.label} className={it.label === "Location" ? "col-span-2" : ""}>
                            <dt className="text-[10px] tracking-[0.16em] uppercase text-black/40">{it.label}</dt>
                            <dd className="text-base mt-1">{it.href ? <a href={it.href} target="_blank" rel="noreferrer" className="underline-offset-2 hover:underline">{it.value}</a> : it.value}</dd>
                        </div>
                    ))}
                </dl>
            )}
            {t.video && (
                <a href={t.video.url} target="_blank" rel="noreferrer" data-testid={`roster-video-${t.key}`}
                    className="mt-5 inline-flex w-full items-center justify-center gap-2 bg-black text-white rounded-lg min-h-[48px] text-[11px] tracking-[0.16em] uppercase font-semibold">
                    <Play className="w-3.5 h-3.5 fill-white" /> Introduction Video <ExternalLink className="w-3.5 h-3.5 opacity-60" />
                </a>
            )}
            {rest.length > 0 && (
                <div className="columns-2 gap-3 mt-6 [&>*]:mb-3">
                    {rest.map((im, i) => <ImgTile key={im.id} im={im} onClick={() => onOpen(t.images, i + 1)} label={`${t.name} image ${i + 2}`} />)}
                </div>
            )}
        </article>
    );
}

function ImgTile({ im, onClick, label }) {
    return (
        <button type="button" onClick={onClick} aria-label={label} className="block w-full break-inside-avoid bg-[#f3f2ef] overflow-hidden" style={{ aspectRatio: `${im.ratio}` }}>
            <img src={IMAGE_URL(im)} alt="" loading="lazy" decoding="async" className="w-full h-full object-contain block" />
        </button>
    );
}

/* ----------------------------------- lightbox ------------------------------------ */
function Lightbox({ state, setState }) {
    const { images, index } = state;
    const step = (d) => setState({ images, index: (index + d + images.length) % images.length });
    return (
        <div className="fixed inset-0 z-50 bg-black/92 flex flex-col" role="dialog" aria-modal="true" data-testid="roster-lightbox">
            <div className="flex items-center justify-between px-3 py-2 text-white">
                <span className="text-xs text-white/70 px-2">{index + 1} / {images.length}</span>
                <button onClick={() => setState(null)} aria-label="Close" className="min-w-[44px] min-h-[44px] flex items-center justify-center"><X className="w-5 h-5" /></button>
            </div>
            <div className="relative flex-1 min-h-0 flex items-center justify-center px-2" onClick={() => setState(null)}>
                {images.length > 1 && <button aria-label="Previous" onClick={(e) => { e.stopPropagation(); step(-1); }} className="absolute left-1 z-10 min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full bg-white/15 text-white"><ChevronLeft className="w-5 h-5" /></button>}
                <img src={IMAGE_URL(images[index])} alt="" onClick={(e) => e.stopPropagation()} className="max-w-full max-h-full object-contain" />
                {images.length > 1 && <button aria-label="Next" onClick={(e) => { e.stopPropagation(); step(1); }} className="absolute right-1 z-10 min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full bg-white/15 text-white"><ChevronRight className="w-5 h-5" /></button>}
            </div>
        </div>
    );
}
