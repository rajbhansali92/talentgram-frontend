const FIELD_LABEL_MAP = {
    name: "Full Name",
    email: "Email",
    phone: "Phone (WhatsApp)",
    alternate_contact_number: "Alternate Contact Number",
    whatsapp_group_name: "WhatsApp Group Name",
    instagram_handle: "Instagram Handle",
};

/**
 * Parses and formats FastAPI/Pydantic or generic Axios errors into clean, crash-proof user-facing strings.
 *
 * @param {any} e - The error object caught from the request.
 * @param {string} fallback - The fallback message if no descriptive error is resolved.
 * @returns {string} Safe string ready to be passed to toast.error or rendered.
 */
export function formatErrorDetail(e, fallback = "An error occurred") {
    if (!e) return fallback;

    const detail = e?.response?.data?.detail;

    // 1. String detail
    if (typeof detail === "string") {
        return detail;
    }

    // 2. FastAPI validation errors array: [{loc: [...], msg: "...", type: "..."}]
    if (Array.isArray(detail) && detail.length > 0) {
        const errors = detail
            .map((err) => {
                if (err && typeof err === "object") {
                    const rawField = Array.isArray(err.loc) ? err.loc[err.loc.length - 1] : "";
                    const fieldLabel =
                        FIELD_LABEL_MAP[rawField] ||
                        (rawField
                            ? String(rawField).charAt(0).toUpperCase() + String(rawField).slice(1)
                            : "");
                    const msg = err.msg || err.message || "Invalid value";
                    return fieldLabel ? `• ${fieldLabel} — ${msg}` : `• ${msg}`;
                }
                return typeof err === "string" ? `• ${err}` : null;
            })
            .filter(Boolean);

        if (errors.length > 0) {
            return `Please fix the following:\n${errors.join("\n")}`;
        }
    }

    // 3. Fallback to API custom message field
    const apiMessage = e?.response?.data?.message;
    if (typeof apiMessage === "string") {
        return apiMessage;
    }

    // 4. Fallback to standard Axios/JS error message
    if (typeof e?.message === "string") {
        return e.message;
    }

    // 5. Fallback to default
    return fallback;
}
