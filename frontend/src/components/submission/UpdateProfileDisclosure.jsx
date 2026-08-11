import React, { useEffect, useState } from "react";
import { ChevronDown, UserCog } from "lucide-react";

// A collapsible "everything else" wrapper for Steps 1-2 of the submission
// wizard. Purely presentational and layout-agnostic about WHICH fields go
// where — the host page (SubmissionPage.jsx) decides that by rendering the
// admin-required fields as normal page content and handing everything else
// to this component as `children`. No requirement-engine logic lives here;
// this only renders a toggle around whatever it's given.
//
// `defaultOpen` is the caller's call: expanded for a first-time talent
// (nothing to hide, matches today's behavior of always showing every
// field), collapsed for a returning talent (reduce friction — they've
// already filled this in once).
export default function UpdateProfileDisclosure({
    defaultOpen = true,
    label = "Update my Profile",
    hint = "Optional details you can review or change",
    children,
}) {
    const [open, setOpen] = useState(defaultOpen);
    // `defaultOpen` can change after mount (e.g. prefill data arrives
    // asynchronously after the wizard already rendered) — follow it.
    useEffect(() => setOpen(defaultOpen), [defaultOpen]);

    return (
        <div className="mt-2" data-testid="update-profile-disclosure" data-open={open}>
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center gap-2.5 py-3 px-1 text-left"
                aria-expanded={open}
            >
                <span className="shrink-0 w-7 h-7 rounded-full bg-slate-50 border border-[#eaeaea] flex items-center justify-center text-[#666]">
                    <UserCog className="w-3.5 h-3.5" />
                </span>
                <span className="flex-1 min-w-0">
                    <span className="block text-[12.5px] font-semibold text-slate-900">{label}</span>
                    <span className="block text-[11px] text-[#999]">{hint}</span>
                </span>
                <ChevronDown
                    className={`w-4 h-4 text-[#999] shrink-0 transition-transform duration-200 ${open ? "" : "-rotate-90"}`}
                />
            </button>
            {open && (
                <div className="pt-1 pb-2 animate-in fade-in slide-in-from-top-1 duration-200" data-testid="update-profile-disclosure-content">
                    {children}
                </div>
            )}
        </div>
    );
}
