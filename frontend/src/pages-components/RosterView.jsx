'use client';

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { api as axios, API, getViewerToken, saveViewerToken } from "@/lib/api";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { Download, Loader2 } from "lucide-react";
import RosterShell from "@/pages-components/RosterShell";
import RosterSections from "@/pages-components/RosterSections";

/**
 * Public Roster link. This file owns ACCESS and state only: the Generated Links identify flow
 * (name + email -> viewer token bound to this slug, so views / unique viewers are counted by the
 * existing link_views machinery), loading, and the error states. What a signed-in viewer sees is
 * the vertical, one-section-per-talent roster in RosterSections.jsx. The downloadable PDF is a separate artefact built
 * server-side and is not affected by anything here.
 */

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

export default function RosterView() {
    const { slug } = useParams();
    const [phase, setPhase] = useState("boot"); // boot | identify | loading | ready | inactive | error
    const [data, setData] = useState(null);
    const [meta, setMeta] = useState({ title: "Talentgram Roster", subtitle: null });
    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [busy, setBusy] = useState(false);
    const [pdfStarting, setPdfStarting] = useState(false);
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

    const downloadPdf = () => {
        if (pdfStarting) return;
        setPdfStarting(true);
        window.location.assign(`${API}/public/links/${slug}/roster/pdf?token=${encodeURIComponent(getViewerToken(slug) || "")}`);
        setTimeout(() => setPdfStarting(false), 12000);
    };

    /* ------------------------------- gate / states ------------------------------ */
    if (phase === "boot" || phase === "loading") {
        return (
            <RosterShell title={meta.title}>
                <div className="py-32 text-center text-sm text-black/45" aria-busy="true" role="status" data-testid="roster-loading">
                    <Loader2 className="w-5 h-5 animate-spin mx-auto mb-3" /> Loading roster…
                </div>
            </RosterShell>
        );
    }
    if (phase === "identify") {
        return (
            <RosterShell title={meta.title} bare>
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
            </RosterShell>
        );
    }
    if (phase === "inactive" || phase === "error") {
        return (
            <RosterShell title={meta.title}>
                <div className="py-28 px-6 text-center max-w-md mx-auto" data-testid="roster-unavailable">
                    <h1 className="font-display text-2xl mb-2">{phase === "inactive" ? "This link is no longer active" : "Couldn’t load the roster"}</h1>
                    <p className="text-sm text-black/55 leading-relaxed">
                        {phase === "inactive" ? "Please contact Talentgram for an updated link." : "Please check your connection and try again."}
                    </p>
                    {phase === "error" && <button onClick={load} className="mt-6 inline-flex items-center justify-center min-h-[44px] px-6 rounded-lg bg-black text-white text-sm font-medium">Try again</button>}
                </div>
            </RosterShell>
        );
    }

    /* ---------------------------------- ready ----------------------------------- */
    const pdfButton = data.pdf_available && (
        <button type="button" onClick={downloadPdf} disabled={pdfStarting} data-testid="roster-download-pdf"
            aria-label={pdfStarting ? "Preparing PDF" : "Download PDF"}
            className="inline-flex items-center justify-center gap-2 bg-black text-white rounded-lg text-xs font-medium disabled:opacity-70 min-w-[40px] min-h-[40px] px-2.5 md:px-5">
            {pdfStarting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            <span className="hidden md:inline">{pdfStarting ? "Preparing PDF…" : "Download PDF"}</span>
        </button>
    );

    return <RosterSections data={data} slug={slug} pdfButton={pdfButton} />;
}
