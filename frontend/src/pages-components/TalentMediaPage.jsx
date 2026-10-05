'use client';

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { api, API, IMAGE_URL } from "@/lib/api";
import HlsVideo from "@/components/HlsVideo";
import Logo from "@/components/Logo";
import { ChevronLeft, ChevronRight, Download, Loader2, X, AlertCircle, Play } from "lucide-react";

/**
 * Client-facing "download this talent's media" page. Opened from a WhatsApp
 * link on phones far more often than on desktop, so it is mobile-first and
 * deliberately NOT an admin page (no nav, no admin chrome).
 *
 * Always fetches the LIVE talent media for the token — nothing is cached or
 * snapshotted client-side. All downloads are plain navigations to the backend
 * (Content-Disposition: attachment), which is the one mechanism that behaves
 * the same on desktop, iOS Safari and Android Chrome (a cross-origin
 * `<a download>` attribute is ignored by browsers, and the CSP blocks a
 * browser-side fetch of the CDN).
 */
export default function TalentMediaPage() {
    const { token } = useParams();
    const [data, setData] = useState(null);
    const [state, setState] = useState("loading"); // loading | ready | unavailable | error
    const [lightbox, setLightbox] = useState(null); // { items, index }
    const [starting, setStarting] = useState(false);

    const load = useCallback(async () => {
        setState("loading");
        try {
            const res = await api.get(`/public/talent-media/${encodeURIComponent(token)}`);
            setData(res.data);
            setState("ready");
        } catch (e) {
            setState(e?.response?.status === 404 ? "unavailable" : "error");
        }
    }, [token]);

    useEffect(() => { if (token) load(); }, [token, load]);

    const base = `${API}/public/talent-media/${encodeURIComponent(token || "")}`;
    const fileUrl = (id) => `${base}/file/${encodeURIComponent(id)}`;

    const sections = useMemo(() => (data?.sections || []).filter((s) => s.items.length > 0), [data]);
    const total = data?.total || 0;

    const downloadAll = async () => {
        if (starting) return;
        setStarting(true);
        try {
            // Re-confirm the link is still live right before navigating, so a
            // toggled-off link shows a friendly message instead of a raw error.
            await api.get(`/public/talent-media/${encodeURIComponent(token)}`);
            window.location.assign(`${base}/download-all`);
        } catch (e) {
            setState(e?.response?.status === 404 ? "unavailable" : "error");
        } finally {
            setTimeout(() => setStarting(false), 6000);
        }
    };

    useEffect(() => {
        if (!lightbox) return undefined;
        const onKey = (e) => {
            if (e.key === "Escape") setLightbox(null);
            if (e.key === "ArrowRight") setLightbox((l) => l && { ...l, index: (l.index + 1) % l.items.length });
            if (e.key === "ArrowLeft") setLightbox((l) => l && { ...l, index: (l.index - 1 + l.items.length) % l.items.length });
        };
        window.addEventListener("keydown", onKey);
        const prev = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
    }, [lightbox]);

    return (
        <div className="min-h-screen bg-[#f6f6f4] text-black/85" data-testid="talent-media-page">
            <header className="bg-white border-b border-[#eaeaea]">
                <div className="mx-auto max-w-[1400px] px-4 sm:px-6 lg:px-8 py-4 flex items-center justify-between gap-4">
                    <Logo size="sm" forceVariant="black" />
                    <span className="text-[10px] tracking-[0.2em] uppercase text-black/40">Talent Media</span>
                </div>
            </header>

            {state === "loading" && (
                <main className="mx-auto max-w-[1400px] px-4 sm:px-6 lg:px-8 py-10" aria-busy="true">
                    <div className="h-8 w-56 bg-black/[0.06] rounded animate-pulse mb-8" />
                    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-6 gap-3">
                        {Array.from({ length: 6 }).map((_, i) => (
                            <div key={i} className="aspect-[4/5] bg-black/[0.06] rounded-xl animate-pulse" />
                        ))}
                    </div>
                    <p className="sr-only" role="status">Loading media…</p>
                </main>
            )}

            {(state === "unavailable" || state === "error") && (
                <main className="mx-auto max-w-md px-6 py-24 text-center" data-testid="talent-media-error">
                    <AlertCircle className="w-9 h-9 mx-auto text-black/30 mb-4" strokeWidth={1.5} />
                    <h1 className="font-display text-2xl mb-2">
                        {state === "unavailable" ? "This link isn’t available" : "Couldn’t load the media"}
                    </h1>
                    <p className="text-sm text-black/55 leading-relaxed">
                        {state === "unavailable"
                            ? "This media link is currently turned off or no longer valid. Please contact Talentgram for an updated link."
                            : "Something went wrong while loading. Please check your connection and try again."}
                    </p>
                    {state === "error" && (
                        <button onClick={load} className="mt-6 inline-flex items-center justify-center min-h-[44px] px-6 rounded-lg bg-black text-white text-sm font-medium">
                            Try again
                        </button>
                    )}
                </main>
            )}

            {state === "ready" && data && (
                <main className="mx-auto max-w-[1400px] px-4 sm:px-6 lg:px-8 pt-8 pb-32 md:pb-16">
                    <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-5 mb-8">
                        <div className="min-w-0">
                            <p className="eyebrow mb-2">Pictures &amp; Introduction Video</p>
                            <h1 className="font-display text-3xl sm:text-4xl md:text-5xl tracking-tight break-words" data-testid="talent-media-name">
                                {data.talent.name}
                            </h1>
                        </div>
                        {total > 0 && (
                            <button
                                onClick={downloadAll}
                                disabled={starting}
                                data-testid="download-all-btn"
                                className="hidden md:inline-flex items-center justify-center gap-2 min-h-[48px] px-7 rounded-lg bg-black text-white text-sm font-medium hover:bg-black/90 disabled:opacity-70 transition-colors"
                            >
                                {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                                {starting ? "Starting download…" : `Download All (${total})`}
                            </button>
                        )}
                    </div>

                    {total === 0 && (
                        <p className="text-sm text-black/50 py-16 text-center" data-testid="talent-media-empty">No media is available yet.</p>
                    )}

                    {sections.map((s) => (
                        <section key={s.key} className="bg-white border border-[#eaeaea] rounded-2xl p-4 sm:p-6 md:p-8 mb-5" data-testid={`media-section-${s.key}`}>
                            <h2 className="eyebrow mb-4 sm:mb-6">{s.label}</h2>
                            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-6 gap-3 sm:gap-4">
                                {s.items.map((m, idx) => (
                                    <figure key={m.id} className="min-w-0 m-0">
                                        <button
                                            type="button"
                                            onClick={() => setLightbox({ items: s.items, index: idx })}
                                            className="block w-full aspect-[4/5] overflow-hidden rounded-xl border border-[#eaeaea] bg-[#fafaf8] cursor-zoom-in"
                                            aria-label={`View ${s.label} image ${idx + 1}`}
                                        >
                                            <img src={IMAGE_URL(m)} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
                                        </button>
                                        <a
                                            href={fileUrl(m.id)}
                                            data-testid="download-image-btn"
                                            className="mt-2 flex items-center justify-center gap-1.5 min-h-[44px] rounded-lg border border-[#e2e2e0] bg-white text-xs font-medium text-black/75 hover:border-black/40 active:bg-black/[0.04] transition-colors"
                                        >
                                            <Download className="w-3.5 h-3.5" /> Download
                                        </a>
                                    </figure>
                                ))}
                            </div>
                        </section>
                    ))}

                    {data.video && (
                        <section className="bg-white border border-[#eaeaea] rounded-2xl p-4 sm:p-6 md:p-8 mb-5" data-testid="media-section-video">
                            <h2 className="eyebrow mb-4 sm:mb-6">Introduction Video</h2>
                            <div className="flex flex-col lg:flex-row gap-5 lg:gap-8 lg:items-start">
                                <div className="w-full lg:max-w-[560px] rounded-xl overflow-hidden bg-black">
                                    <HlsVideo
                                        src={data.video.url}
                                        poster={data.video.poster_url || undefined}
                                        controls
                                        playsInline
                                        preload="metadata"
                                        className="w-full max-h-[70vh] bg-black"
                                        data-testid="intro-video"
                                    />
                                </div>
                                <div className="min-w-0">
                                    <h3 className="font-semibold text-base mb-3 flex items-center gap-2"><Play className="w-4 h-4" /> Introduction Video</h3>
                                    <a
                                        href={fileUrl(data.video.id)}
                                        data-testid="download-video-btn"
                                        className="inline-flex w-full sm:w-auto items-center justify-center gap-2 min-h-[48px] px-6 rounded-lg bg-black text-white text-sm font-medium hover:bg-black/90"
                                    >
                                        <Download className="w-4 h-4" /> Download Video
                                    </a>
                                </div>
                            </div>
                        </section>
                    )}
                </main>
            )}

            {state === "ready" && total > 0 && (
                <div className="md:hidden fixed inset-x-0 bottom-0 z-30 bg-white/95 backdrop-blur border-t border-[#eaeaea] px-4 pt-3" style={{ paddingBottom: "max(12px, env(safe-area-inset-bottom))" }}>
                    <button
                        onClick={downloadAll}
                        disabled={starting}
                        data-testid="download-all-btn-mobile"
                        className="w-full inline-flex items-center justify-center gap-2 min-h-[50px] rounded-xl bg-black text-white text-sm font-medium disabled:opacity-70"
                    >
                        {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                        {starting ? "Starting download…" : `Download All (${total})`}
                    </button>
                </div>
            )}

            {lightbox && (
                <div className="fixed inset-0 z-50 bg-black/92 flex flex-col" role="dialog" aria-modal="true" data-testid="media-lightbox">
                    <div className="flex items-center justify-between px-3 py-2 text-white">
                        <span className="text-xs text-white/70 px-2">{lightbox.index + 1} / {lightbox.items.length}</span>
                        <div className="flex items-center gap-1">
                            <a href={fileUrl(lightbox.items[lightbox.index].id)} className="inline-flex items-center gap-1.5 min-h-[44px] px-4 rounded-lg bg-white text-black text-xs font-medium">
                                <Download className="w-3.5 h-3.5" /> Download
                            </a>
                            <button onClick={() => setLightbox(null)} aria-label="Close" className="min-w-[44px] min-h-[44px] flex items-center justify-center"><X className="w-5 h-5" /></button>
                        </div>
                    </div>
                    <div className="relative flex-1 min-h-0 flex items-center justify-center px-2" onClick={() => setLightbox(null)}>
                        {lightbox.items.length > 1 && (
                            <button aria-label="Previous" onClick={(e) => { e.stopPropagation(); setLightbox({ ...lightbox, index: (lightbox.index - 1 + lightbox.items.length) % lightbox.items.length }); }}
                                className="absolute left-1 z-10 min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full bg-white/15 text-white"><ChevronLeft className="w-5 h-5" /></button>
                        )}
                        <img src={IMAGE_URL(lightbox.items[lightbox.index])} alt="" onClick={(e) => e.stopPropagation()} className="max-w-full max-h-full object-contain" />
                        {lightbox.items.length > 1 && (
                            <button aria-label="Next" onClick={(e) => { e.stopPropagation(); setLightbox({ ...lightbox, index: (lightbox.index + 1) % lightbox.items.length }); }}
                                className="absolute right-1 z-10 min-w-[44px] min-h-[44px] flex items-center justify-center rounded-full bg-white/15 text-white"><ChevronRight className="w-5 h-5" /></button>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}
