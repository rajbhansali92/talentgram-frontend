// Pure helpers for the Roster builder's selection state — kept separate from
// the component so the ordering rules are unit-testable.

/** Move the element at `from` to index `to` (clamped). Returns a new array. */
export function moveItem(list, from, to) {
    const arr = [...list];
    if (from < 0 || from >= arr.length) return arr;
    const dest = Math.max(0, Math.min(arr.length - 1, to));
    const [it] = arr.splice(from, 1);
    arr.splice(dest, 0, it);
    return arr;
}

/** Add `id` to the ordered list if absent, otherwise remove it. */
export function toggleId(list, id, max = Infinity) {
    if (list.includes(id)) return list.filter((x) => x !== id);
    if (list.length >= max) return list;
    return [...list, id];
}

/** Make `id` the first (hero) image; no-op if it is not selected. */
export function makeHero(list, id) {
    const i = list.indexOf(id);
    return i <= 0 ? list : moveItem(list, i, 0);
}

/** Select every id of `groups` (all categories, in order) up to `max`,
 *  keeping already-selected ids (and their order) first. */
export function selectAllIds(current, groups, max = Infinity) {
    const all = groups.flatMap((g) => g.items.map((i) => i.id));
    const out = [...current];
    for (const id of all) {
        if (out.length >= max) break;
        if (!out.includes(id)) out.push(id);
    }
    return out;
}

/** Drop selected ids that are no longer offered (e.g. the image was deleted). */
export function pruneSelection(selected, groups) {
    const valid = new Set(groups.flatMap((g) => g.items.map((i) => i.id)));
    return selected.filter((id) => valid.has(id));
}

/** The shape POST/PUT /links expects for a roster link. */
export function buildRosterPayload({ title, subtitle, talentIds, mediaByTalent, fields = null, isPublic = true, allowPdfDownload = true }) {
    return {
        title: title.trim(),
        brand_name: null,
        talent_ids: talentIds,
        submission_ids: [],
        visibility: {},
        talent_field_visibility: {},
        auto_pull: false,
        auto_project_id: null,
        is_public: isPublic,
        password: null,
        notes: null,
        client_budget_override: null,
        link_type: "roster",
        roster: {
            subtitle: (subtitle || "").trim() || null,
            // Roster-level field visibility (null = leave the server's stored/default config alone).
            ...(fields ? { fields } : {}),
            // Admin switch: may the public viewer download the PDF? (server default / legacy rosters = ON)
            allow_pdf_download: allowPdfDownload !== false,
            talents: talentIds.map((id) => ({ talent_id: id, media_ids: mediaByTalent[id] || [] })),
        },
    };
}

/** "Angela Kumar" -> "Angela K" — mirrors backend talent_media.short_talent_name. */
export function shortTalentName(name) {
    const parts = (name || "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "Talent";
    if (parts.length === 1) return parts[0];
    return `${parts.slice(0, -1).join(" ")} ${parts[parts.length - 1][0].toUpperCase()}`;
}

/** Toggle one roster field; returns a new map. */
export function toggleField(fields, key) {
    return { ...fields, [key]: !fields[key] };
}

/** Number of enabled fields (for the compact summary). */
export function countEnabled(fields) {
    return Object.values(fields || {}).filter(Boolean).length;
}

/** Limits the builder falls back to until /roster/fields reports the server's (backend routers/roster.py). */
export const DEFAULT_ROSTER_LIMITS = { max_talents: 100, max_images_per_talent: 12, max_total_images: 1000 };

/** Total images across the selected talents. */
export function countImages(mediaByTalent, ids = null) {
    const keys = ids || Object.keys(mediaByTalent || {});
    let n = 0;
    for (const id of keys) n += (mediaByTalent[id] || []).length;
    return n;
}

/**
 * Add `incoming` talents ({id, ...}) after the current selection, skipping ones already selected and
 * keeping roster order. ATOMIC: if the result would exceed `max` nothing is added and `overflow` says
 * by how many — a bulk "select all" never silently drops or truncates talents.
 */
export function mergeSelection(current, incoming, max = Infinity) {
    const have = new Set(current.map((t) => t.id));
    const fresh = [];
    for (const t of incoming) {
        if (!have.has(t.id)) { have.add(t.id); fresh.push(t); }
    }
    if (current.length + fresh.length > max) {
        return { list: current, added: 0, overflow: current.length + fresh.length - max };
    }
    return { list: fresh.length ? [...current, ...fresh] : current, added: fresh.length, overflow: 0 };
}

/** Remove every talent whose id is in `ids` (a Set or array). Returns the same array if nothing matched. */
export function removeIds(current, ids) {
    const drop = ids instanceof Set ? ids : new Set(ids);
    const next = current.filter((t) => !drop.has(t.id));
    return next.length === current.length ? current : next;
}
