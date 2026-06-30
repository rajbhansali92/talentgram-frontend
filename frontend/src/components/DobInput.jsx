import React, { useEffect, useRef, useState } from "react";
import {
    isoToDisplay,
    displayToIso,
    maskDobInput,
    parseDisplay,
} from "../lib/dob";

// ────────────────────────────────────────────────────────────────────────
// DobInput — controlled, locale-proof Date-of-Birth field.
//
// Renders a plain masked text input that always shows DD/MM/YYYY regardless
// of browser/OS locale (no native <input type="date">). The `value` prop and
// `onChange` callback both speak the canonical ISO `YYYY-MM-DD` string, so it
// is a drop-in for the old date inputs without touching API/storage.
//
//   - value:    ISO "YYYY-MM-DD" (or "")
//   - onChange: called with ISO "YYYY-MM-DD" when a valid date is entered,
//               or "" while the field is empty/partial/invalid.
//   - max:      optional ISO upper bound (e.g. today); dates after it are
//               treated as invalid and not emitted.
// ────────────────────────────────────────────────────────────────────────
export default function DobInput({
    value = "",
    onChange,
    onBlur,
    max,
    disabled,
    placeholder = "DD/MM/YYYY",
    className = "",
    testid,
    inputRef,
    autoComplete = "bday",
    ...rest
}) {
    const [display, setDisplay] = useState(() => isoToDisplay(value));
    // Tracks the last ISO value we pushed up, so external prop changes
    // (draft load / prefill) re-sync while in-progress typing does not get
    // clobbered when we emit "" for a partial date.
    const lastEmitted = useRef(value || "");

    useEffect(() => {
        const incoming = value || "";
        if (incoming !== lastEmitted.current) {
            setDisplay(isoToDisplay(incoming));
            lastEmitted.current = incoming;
        }
    }, [value]);

    const emit = (iso) => {
        if (iso !== lastEmitted.current) {
            lastEmitted.current = iso;
            onChange?.(iso);
        }
    };

    const handleChange = (e) => {
        const masked = maskDobInput(e.target.value);
        setDisplay(masked);

        if (masked === "") {
            emit("");
            return;
        }
        const iso = displayToIso(masked);
        if (iso && (!max || iso <= max)) {
            emit(iso);
        } else {
            // Partial or invalid — clear the stored value until it's complete.
            emit("");
        }
    };

    const handleBlur = (e) => {
        if (onBlur) onBlur(e);
    };

    // Error only once the user has typed a full DD/MM/YYYY that doesn't
    // resolve to a real (in-range) date — avoids flagging mid-typing.
    const parsed = parseDisplay(display);
    const isComplete = /^\d{2}\/\d{2}\/\d{4}$/.test(display);
    const invalid =
        isComplete && (!parsed || (max && displayToIso(display) > max));

    return (
        <input
            ref={inputRef}
            type="text"
            inputMode="numeric"
            autoComplete={autoComplete}
            value={display}
            onChange={handleChange}
            onBlur={handleBlur}
            disabled={disabled}
            placeholder={placeholder}
            maxLength={10}
            data-testid={testid}
            aria-invalid={invalid || undefined}
            className={className}
            {...rest}
        />
    );
}
