import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";

/** Plain-language reason for a failed PDF request (network drop / timeout / server error / gone). */
export function pdfErrorMessage(err) {
    const status = err?.response?.status;
    if (status === 404) return "This roster has no available talents, so there is nothing to put in a PDF.";
    if (status === 401 || status === 403) return "You don't have permission to download this PDF.";
    if (!err?.response) return "The connection dropped while the PDF was being prepared. Try again — large rosters keep building on the server, so the next attempt is usually quick.";
    return "Couldn't generate the PDF. Try again in a moment — large rosters keep building on the server, so the next attempt is usually quick.";
}

/**
 * Admin roster-PDF download with visible progress. One request at a time (a second click while one is running is
 * ignored), elapsed seconds while it builds, a specific error, and a warning when the server had to leave
 * images out (X-Roster-Images-* headers) — a PDF with missing images is never presented as complete.
 */
export function usePdfDownload() {
    const [busy, setBusy] = useState(false);
    const [elapsed, setElapsed] = useState(0);
    const inFlight = useRef(false);
    const timer = useRef(null);

    useEffect(() => () => clearInterval(timer.current), []);

    const download = useCallback(async (linkId, title) => {
        if (inFlight.current) return;
        inFlight.current = true;
        setBusy(true);
        setElapsed(0);
        const started = Date.now();
        timer.current = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
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
            const total = Number(res.headers?.["x-roster-images-total"]);
            const included = Number(res.headers?.["x-roster-images-included"]);
            if (Number.isFinite(total) && Number.isFinite(included) && included < total) {
                toast.warning(`PDF downloaded, but ${total - included} of ${total} images couldn't be fetched and were left out. Try again, or check those images on the talent profiles.`, { duration: 12000 });
            }
        } catch (err) {
            toast.error(pdfErrorMessage(err), { duration: 9000 });
        } finally {
            clearInterval(timer.current);
            inFlight.current = false;
            setBusy(false);
        }
    }, []);

    return { busy, elapsed, download };
}
