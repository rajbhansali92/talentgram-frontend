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
export function buildRosterPayload({ title, subtitle, talentIds, mediaByTalent, isPublic = true }) {
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
