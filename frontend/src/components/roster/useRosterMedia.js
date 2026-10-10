import { useCallback, useEffect, useRef, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { countImages, pruneSelection } from "@/lib/rosterSelection";

// Talents requested per /roster/media-options call, and how many calls run at once. Chunking lets the Images
// step paint its first sections while the rest load, and keeps every response (and its JSON parse) small.
export const OPTIONS_CHUNK = 20;
export const OPTIONS_PARALLEL = 3;

const EMPTY = [];

/**
 * The image-selection state of the roster builder: the per-talent image options (fetched once per talent and
 * cached for the life of the wizard — filters, search and step changes never refetch them) and each talent's
 * ordered picked image ids.
 *
 * - Only talents whose options are not cached yet are requested, in small abortable chunks.
 * - A response that arrives after the selection changed / the step was left is ignored (stale-request guard).
 * - A talent whose options failed to load is reported (`failedIds`) with a retry — it is never silently
 *   saved without images (`ready` is false until every selected talent has loaded).
 * - `updateMedia(talentId, fn)` is the single narrow, immutable write path: it replaces ONE talent's array and
 *   enforces the roster-wide image limit.
 */
export function useRosterMedia({ selectedIds, enabled, editSelectionRef, limits }) {
    const [options, setOptions] = useState({});            // talent_id -> media-options entry (cache)
    const [mediaByTalent, setMediaByTalent] = useState({}); // talent_id -> ordered media ids
    const [loading, setLoading] = useState(false);
    const [failedIds, setFailedIds] = useState([]);
    const [attempt, setAttempt] = useState(0);              // bump to retry failed talents

    const optionsRef = useRef(options);
    optionsRef.current = options;
    const mediaRef = useRef(mediaByTalent);
    mediaRef.current = mediaByTalent;
    const maxTotal = limits.max_total_images;

    // Keep the picks in step with the selection: drop picks of talents that are no longer selected (the roster
    // is exactly the selected talents) and give a talent that was removed and re-added its suggested images
    // again from the cached options (no refetch).
    useEffect(() => {
        setMediaByTalent((prev) => {
            const keep = new Set(selectedIds);
            let changed = false;
            const next = {};
            for (const k of Object.keys(prev)) {
                if (keep.has(k)) next[k] = prev[k]; else changed = true;
            }
            for (const id of selectedIds) {
                const opt = optionsRef.current[id];
                if (!next[id] && opt) { next[id] = opt.default_media_ids; changed = true; }
            }
            return changed ? next : prev;
        });
    }, [selectedIds]);

    useEffect(() => {
        if (!enabled) return undefined;
        const missing = selectedIds.filter((id) => !optionsRef.current[id]);
        if (!missing.length) { setLoading(false); setFailedIds([]); return undefined; }

        const ctrl = new AbortController();
        let live = true;
        const chunks = [];
        for (let i = 0; i < missing.length; i += OPTIONS_CHUNK) chunks.push(missing.slice(i, i + OPTIONS_CHUNK));
        let cursor = 0;
        const failed = new Set();
        setLoading(true);
        setFailedIds([]);

        const apply = (talents) => {
            const map = Object.fromEntries(talents.map((t) => [t.talent_id, t]));
            setOptions((prev) => ({ ...prev, ...map }));
            setMediaByTalent((prev) => {
                const next = { ...prev };
                for (const [id, opt] of Object.entries(map)) {
                    if (next[id]) next[id] = pruneSelection(next[id], opt.groups);
                    else if (editSelectionRef?.current?.[id]) next[id] = pruneSelection(editSelectionRef.current[id], opt.groups);
                    else next[id] = opt.default_media_ids;
                }
                return next;
            });
        };

        const worker = async () => {
            while (live && cursor < chunks.length) {
                const ids = chunks[cursor++];
                try {
                    const { data } = await adminApi.post("/roster/media-options", { talent_ids: ids }, { signal: ctrl.signal });
                    if (!live) return;
                    apply(data.talents || []);
                    const got = new Set((data.talents || []).map((t) => t.talent_id));
                    ids.forEach((id) => { if (!got.has(id)) failed.add(id); });
                } catch (err) {
                    if (!live || err?.name === "CanceledError" || err?.code === "ERR_CANCELED") return;
                    ids.forEach((id) => failed.add(id));
                }
            }
        };

        Promise.all(Array.from({ length: Math.min(OPTIONS_PARALLEL, chunks.length) }, worker)).then(() => {
            if (!live) return;
            setLoading(false);
            const list = [...failed];
            setFailedIds(list);
            if (list.length) toast.error(`Couldn't load images for ${list.length} talent${list.length === 1 ? "" : "s"}. Retry before saving.`);
        });

        return () => { live = false; ctrl.abort(); };
        // `selectedIds` identity is stable between selection changes (memoised in the parent).
    }, [enabled, selectedIds, attempt, editSelectionRef]);

    const updateMedia = useCallback((talentId, fn) => {
        const cur = mediaRef.current;
        const before = cur[talentId] || EMPTY;
        const after = fn(before);
        if (after === before) return;
        if (countImages(cur) - before.length + after.length > maxTotal) {
            toast.error(`A roster can include at most ${maxTotal} images in total`);
            return;
        }
        mediaRef.current = { ...cur, [talentId]: after };
        setMediaByTalent((prev) => ({ ...prev, [talentId]: after }));
    }, [maxTotal]);

    const retry = useCallback(() => setAttempt((n) => n + 1), []);
    const loadedCount = selectedIds.reduce((n, id) => n + (options[id] ? 1 : 0), 0);
    const ready = !loading && failedIds.length === 0 && selectedIds.every((id) => options[id] && mediaByTalent[id]);

    return { options, mediaByTalent, updateMedia, loading, loadedCount, failedIds, retry, ready };
}
