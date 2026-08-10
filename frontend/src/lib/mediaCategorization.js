// Automatic Media Categorization (Admin Submission Phase 2, item 3).
//
// Pure heuristic, entirely client-side, no ML model and no paid API — per
// the locked-in decision for this feature. Filename keyword matching is the
// primary signal; image aspect-ratio/orientation is a weak secondary signal
// used only when the filename gives no match. This is a SUGGESTION engine
// only: callers must never auto-save a suggested category — the admin
// reviews and confirms (or reassigns) every group before anything uploads.
// Below the confidence threshold, the category is left `null`
// (uncategorized) for manual assignment, exactly as specified.

// Longest/most-specific keyword checked first so e.g. "side_profile_01.jpg"
// matches `side_profile`, not the more generic `profiles` below it.
const KEYWORD_RULES = [
    { category: "side_profile", keywords: ["side_profile", "side-profile", "sideprofile", "side"] },
    { category: "full_length", keywords: ["full_length", "full-length", "fulllength", "full"] },
    { category: "indian", keywords: ["indian"] },
    { category: "western", keywords: ["western"] },
    { category: "ethnic", keywords: ["ethnic"] },
    { category: "selfie", keywords: ["selfie", "self"] },
    { category: "profiles", keywords: ["profile"] },
    { category: "additional_portfolio", keywords: ["additional_portfolio", "additional-portfolio", "extra_portfolio"] },
    { category: "image", keywords: ["portfolio"] },
];

export const CATEGORIZATION_CONFIDENCE_THRESHOLD = 0.5;

function matchByFilename(filename) {
    const name = (filename || "").toLowerCase();
    for (const rule of KEYWORD_RULES) {
        for (const kw of rule.keywords) {
            if (name.includes(kw)) {
                // Multi-char, specific keywords score higher than short/common ones.
                const confidence = kw.length >= 6 ? 0.9 : 0.75;
                return { category: rule.category, confidence, source: "filename" };
            }
        }
    }
    return null;
}

async function readImageDimensions(file) {
    if (!file.type?.startsWith("image/")) return null;
    try {
        if (typeof createImageBitmap === "function") {
            const bitmap = await createImageBitmap(file);
            const dims = { width: bitmap.width, height: bitmap.height };
            bitmap.close?.();
            return dims;
        }
    } catch {
        // fall through to the <img> based fallback below
    }
    return new Promise((resolve) => {
        try {
            const url = URL.createObjectURL(file);
            const img = new Image();
            img.onload = () => {
                URL.revokeObjectURL(url);
                resolve({ width: img.naturalWidth, height: img.naturalHeight });
            };
            img.onerror = () => {
                URL.revokeObjectURL(url);
                resolve(null);
            };
            img.src = url;
        } catch {
            resolve(null);
        }
    });
}

// Orientation-only guess — deliberately weak (always below the confidence
// threshold on its own) since there's no face/pose detection to actually
// tell a selfie from a full-length portrait shot; a very tall portrait is
// the one orientation signal worth surfacing as a low-confidence hint.
function matchByOrientation(dims) {
    if (!dims || !dims.width || !dims.height) return null;
    const ratio = dims.height / dims.width;
    if (ratio >= 1.6) {
        return { category: "full_length", confidence: 0.35, source: "orientation" };
    }
    return null;
}

/**
 * Suggests a category for one file. Returns
 * `{ category: string|null, confidence: number, source: "filename"|"orientation"|null }`.
 * `category` is null whenever confidence is below
 * CATEGORIZATION_CONFIDENCE_THRESHOLD — the caller must treat that as
 * "uncategorized, admin assigns manually", never as a default bucket.
 */
export async function suggestCategory(file) {
    const byName = matchByFilename(file?.name);
    if (byName && byName.confidence >= CATEGORIZATION_CONFIDENCE_THRESHOLD) {
        return byName;
    }
    const dims = await readImageDimensions(file);
    const byOrientation = matchByOrientation(dims);
    if (byOrientation && byOrientation.confidence >= CATEGORIZATION_CONFIDENCE_THRESHOLD) {
        return byOrientation;
    }
    // Below threshold — surface the best guess (if any) for display only,
    // category itself stays null (uncategorized).
    const best = byName || byOrientation;
    return { category: null, confidence: best?.confidence || 0, weakGuess: best?.category || null, source: best?.source || null };
}

/**
 * Suggests categories for a batch of files and groups them.
 * Returns `{ groups: { [category]: File[] }, uncategorized: File[] }`.
 * `groups` never includes a `null`/uncategorized key — those files are
 * always in `uncategorized`, regardless of any weak guess attached.
 */
export async function suggestCategoriesForBatch(files) {
    const groups = {};
    const uncategorized = [];
    for (const file of files) {
        const suggestion = await suggestCategory(file);
        if (suggestion.category) {
            (groups[suggestion.category] ||= []).push(file);
        } else {
            uncategorized.push({ file, weakGuess: suggestion.weakGuess });
        }
    }
    return { groups, uncategorized };
}
