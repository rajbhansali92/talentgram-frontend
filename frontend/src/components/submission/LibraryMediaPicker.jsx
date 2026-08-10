'use client';

import React from "react";
import { Video, Loader2, Check } from "lucide-react";
import { thumbnailUrl } from "@/lib/mediaUtils";

// Talent Profile Migration, Phase 3 (extracted from SubmissionPage.jsx,
// Phase 2 of Admin Submission) — "Use Existing" picker: browse the talent's
// Library across every reusable category and toggle items onto THIS
// submission by reference (copy-by-value server-side, no re-upload). "Upload
// New" is simply the existing per-category upload zone rendered right below
// this component — the same submission media array either way, so a media
// item picked from here renders identically to a freshly-uploaded one.
//
// Data-driven by `categories` (not a hardcoded branch per category) so a
// future custom project media category needs no changes here — just an
// entry added to the caller's category list.
export default function LibraryMediaPicker({
    categories,
    libraryByCategory,
    removedByCategory,
    dismissedRemovedWarnings,
    selectedLibrarySourceIds,
    libraryBusyId,
    toggleLibraryMedia,
    dismissRemovedWarning,
    removeMedia,
    selectAllLibraryMedia,
    hasAnyLibraryMedia,
}) {
    if (!hasAnyLibraryMedia) return null;
    return (
        <div
            className="mb-6 bg-indigo-50/40 border border-indigo-100 rounded-2xl p-4"
            data-testid="library-media-section"
        >
            <div className="flex items-center justify-between gap-3 mb-3">
                <div>
                    <h3 className="font-display text-lg font-bold text-slate-900">
                        My Saved Media
                    </h3>
                    <p className="text-xs text-slate-500 mt-0.5">
                        From your Talent Profile — choose what applies to this project. No re-upload needed.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={selectAllLibraryMedia}
                    className="shrink-0 text-xs font-medium px-3 py-1.5 rounded-full border border-indigo-200 bg-white hover:bg-indigo-50 transition-colors"
                    data-testid="library-select-all"
                >
                    Select All
                </button>
            </div>

            {categories.map(({ key, label }) => {
                const items = libraryByCategory[key] || [];
                const removedItems = (removedByCategory[key] || []).filter(
                    (m) => !dismissedRemovedWarnings.has(m.id),
                );
                if (items.length === 0 && removedItems.length === 0) return null;
                return (
                    <div key={key} className="mb-4 last:mb-0">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
                            {label}
                        </p>
                        <div className="flex flex-wrap gap-3">
                            {items.map((item) => {
                                const isSelected = selectedLibrarySourceIds.has(item.id);
                                const isBusy = libraryBusyId === item.id;
                                return (
                                    <button
                                        key={item.id}
                                        type="button"
                                        disabled={isBusy}
                                        onClick={() => toggleLibraryMedia(item, isSelected)}
                                        data-testid={`library-item-${item.id}`}
                                        data-selected={isSelected ? "true" : "false"}
                                        className={`relative w-24 h-24 rounded-xl overflow-hidden border-2 transition-all ${
                                            isSelected ? "border-emerald-500" : "border-transparent"
                                        } ${isBusy ? "opacity-50" : ""}`}
                                        title={isSelected ? "Selected — click to remove from this submission" : "Not selected — click to use for this submission"}
                                    >
                                        {item.category === "intro_video" ? (
                                            <div className="w-full h-full bg-slate-900 flex items-center justify-center">
                                                <Video className="w-6 h-6 text-white/70" />
                                            </div>
                                        ) : (
                                            <img
                                                src={thumbnailUrl(item)}
                                                alt=""
                                                className="w-full h-full object-cover"
                                            />
                                        )}
                                        <div
                                            className={`absolute top-1 right-1 w-5 h-5 rounded-full flex items-center justify-center ${
                                                isSelected
                                                    ? "bg-emerald-500"
                                                    : "bg-white/85 border border-slate-300"
                                            }`}
                                        >
                                            {isBusy ? (
                                                <Loader2 className="w-3 h-3 animate-spin text-slate-500" />
                                            ) : (
                                                isSelected && <Check className="w-3 h-3 text-white" />
                                            )}
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                        {removedItems.map((m) => (
                            <div
                                key={m.id}
                                className="mt-2 flex items-center justify-between gap-3 bg-amber-50 border border-amber-200 rounded-xl p-3"
                                data-testid={`removed-from-profile-${m.id}`}
                            >
                                <span className="text-xs text-amber-800">
                                    This media has been removed from your Talent Profile.
                                </span>
                                <div className="flex gap-2 shrink-0">
                                    <button
                                        type="button"
                                        onClick={() => dismissRemovedWarning(m.id)}
                                        className="text-xs px-2.5 py-1 rounded-full border border-amber-300 bg-white hover:bg-amber-50 transition-colors"
                                        data-testid={`keep-removed-${m.id}`}
                                    >
                                        Keep for this submission
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => removeMedia(m.id)}
                                        className="text-xs px-2.5 py-1 rounded-full border border-red-300 bg-white text-red-600 hover:bg-red-50 transition-colors"
                                        data-testid={`remove-removed-${m.id}`}
                                    >
                                        Remove from this submission
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                );
            })}
        </div>
    );
}
