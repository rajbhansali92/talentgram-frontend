// ── Talent Invite (apply) local draft storage ownership ───────────────────
// Single source of truth for the apply-draft localStorage key scheme, shared
// by ApplicationPage (read/write/resume) and GoogleCallback (write on Google
// resume). Keeping the key derivation in one place guarantees the digest can
// never drift between writers and readers.
//
// The draft cache is namespaced by the talent's normalized email — the same
// identity the backend uses as its `talent_email` uniqueness key — so a draft
// created under one email can NEVER be restored into another invite's context.
// The application id (`aid`) remains the backend record identifier and the JWT
// remains the credential; both live INSIDE the per-email value, not the key.
// Mirrors SubmissionPage's per-`slug` namespacing.
//
// `LEGACY_APP_DRAFT_KEY` is the old single global slot. It is migrated into a
// per-email slot on first load when (and only when) its stored email matches
// the resolved identity, then removed. No live code writes it anymore; the
// migration exists only to adopt drafts left by the previous deployment.

export const LEGACY_APP_DRAFT_KEY = "tg_application";
export const APP_DRAFT_PREFIX = "tg_application_";

// Draft expiry: local data (token + PII) is wiped after 30 days even if the
// user never finalises — defense-in-depth against stale tokens / stale PII.
export const APP_DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000;

// Match backend core.normalize_email exactly: strip + lowercase (no plus / dot
// folding), so the same person always resolves to the same slot.
export const normEmail = (e) => (e || "").trim().toLowerCase();

// FNV-1a → 8-hex digest. Deterministic, synchronous, and opaque, so the raw
// email is not written into an enumerable localStorage key name. Not a
// security boundary (the value still holds PII under the 30-day TTL) — purely
// key hygiene + character safety.
export function emailDigest(email) {
    const s = normEmail(email);
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
}

export const appDraftKey = (email) => `${APP_DRAFT_PREFIX}${emailDigest(email)}`;

// Newest local draft across all per-email slots (and the legacy slot). Used
// ONLY when there is no invite/session identity to resume "what I was last
// doing" on a plain /apply visit. Never consulted when ?email= or a verified
// session exists, so it can never override an invite context.
export function newestLocalDraft() {
    if (typeof window === "undefined") return null;
    try {
        let best = null;
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (!k || (!k.startsWith(APP_DRAFT_PREFIX) && k !== LEGACY_APP_DRAFT_KEY)) continue;
            const raw = localStorage.getItem(k);
            if (!raw) continue;
            let v;
            try { v = JSON.parse(raw); } catch { continue; }
            const ts = v?.savedAt || 0;
            if (!best || ts > best.ts) best = { key: k, raw, ts };
        }
        return best;
    } catch {
        return null;
    }
}
