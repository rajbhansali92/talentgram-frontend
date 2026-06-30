// ────────────────────────────────────────────────────────────────────────
// Date-of-birth display/storage conversion layer.
//
// The whole platform shows DOB to users as DD/MM/YYYY (India format) but the
// backend keeps storing the canonical ISO `YYYY-MM-DD` string. These helpers
// are the single conversion boundary between the two — no native
// `<input type="date">` (and therefore no browser-locale guessing) anywhere.
// ────────────────────────────────────────────────────────────────────────

const pad2 = (n) => String(n).padStart(2, "0");

// "YYYY-MM-DD" (or full ISO timestamp) → "DD/MM/YYYY". Returns "" if unparseable.
export function isoToDisplay(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || "").trim());
    if (!m) return "";
    return `${m[3]}/${m[2]}/${m[1]}`;
}

// Parse a "DD/MM/YYYY" string into its parts, rejecting impossible dates.
// Returns { y, mo, d, date } or null. Rejects 29/02 on non-leap years,
// day/month out of range, etc. (uses Date round-trip to catch overflow).
export function parseDisplay(display) {
    const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(String(display || "").trim());
    if (!m) return null;
    const d = parseInt(m[1], 10);
    const mo = parseInt(m[2], 10);
    const y = parseInt(m[3], 10);
    if (mo < 1 || mo > 12) return null;
    if (d < 1 || d > 31) return null;
    if (y < 1900 || y > 9999) return null;
    const date = new Date(y, mo - 1, d);
    // Date() silently rolls overflow forward (e.g. 31/02 → 03 Mar), so verify
    // the components survived the round-trip.
    if (
        date.getFullYear() !== y ||
        date.getMonth() !== mo - 1 ||
        date.getDate() !== d
    ) {
        return null;
    }
    return { y, mo, d, date };
}

// "DD/MM/YYYY" → canonical "YYYY-MM-DD". Returns "" if invalid/incomplete.
export function displayToIso(display) {
    const p = parseDisplay(display);
    if (!p) return "";
    return `${p.y}-${pad2(p.mo)}-${pad2(p.d)}`;
}

export function isValidDisplay(display) {
    return parseDisplay(display) !== null;
}

// Mask free-typed input into the DD/MM/YYYY shape as digits are entered.
// Drops non-digits and auto-inserts the two slashes.
export function maskDobInput(raw) {
    const digits = String(raw || "").replace(/\D/g, "").slice(0, 8);
    const dd = digits.slice(0, 2);
    const mm = digits.slice(2, 4);
    const yyyy = digits.slice(4, 8);
    let out = dd;
    if (digits.length > 2) out += "/" + mm;
    if (digits.length > 4) out += "/" + yyyy;
    return out;
}
