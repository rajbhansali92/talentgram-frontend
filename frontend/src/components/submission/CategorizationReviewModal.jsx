'use client';

import React, { useMemo, useState } from "react";
import { X, Check } from "lucide-react";

const CATEGORY_LABELS = {
    image: "Portfolio",
    indian: "Indian Look",
    western: "Western Look",
    selfie: "Selfie",
    profiles: "Profiles",
    full_length: "Full Length",
    side_profile: "Side Profile",
    ethnic: "Ethnic Look",
    additional_portfolio: "Additional Portfolio",
};

const ASSIGNABLE_CATEGORIES = Object.keys(CATEGORY_LABELS);

// Automatic Media Categorization review step (item 3). Shown after a batch
// drop on the generic "bulk add" zone, BEFORE anything uploads — confirms
// (or lets the admin correct) the heuristic's per-file suggestions. Nothing
// is ever auto-saved here: `onConfirm` is the only path that leads to an
// actual upload, and it fires the EXISTING `uploadImages(files, category)`
// per confirmed group, same upload path as every other zone.
export default function CategorizationReviewModal({ groups, uncategorized, onConfirm, onCancel }) {
    // Local, editable copy — `fileAssignments` maps a stable per-file key to
    // its current category (or null = leave uncategorized). Seeded from the
    // heuristic's suggestions; the admin can reassign any file before
    // confirming.
    const allFiles = useMemo(() => {
        const list = [];
        for (const [category, files] of Object.entries(groups)) {
            for (const file of files) list.push({ file, key: `${file.name}-${file.size}-${file.lastModified}`, initialCategory: category });
        }
        for (const { file, weakGuess } of uncategorized) {
            list.push({ file, key: `${file.name}-${file.size}-${file.lastModified}`, initialCategory: null, weakGuess });
        }
        return list;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const [assignments, setAssignments] = useState(() =>
        Object.fromEntries(allFiles.map((f) => [f.key, f.initialCategory])),
    );

    const grouped = useMemo(() => {
        const out = {};
        for (const f of allFiles) {
            const cat = assignments[f.key];
            const bucket = cat || "__uncategorized__";
            (out[bucket] ||= []).push(f);
        }
        return out;
    }, [allFiles, assignments]);

    const setCategory = (key, category) => {
        setAssignments((prev) => ({ ...prev, [key]: category || null }));
    };

    const setCategoryForAll = (fromBucket, category) => {
        setAssignments((prev) => {
            const next = { ...prev };
            for (const f of grouped[fromBucket] || []) next[f.key] = category || null;
            return next;
        });
    };

    const handleConfirm = () => {
        const finalGroups = {};
        for (const f of allFiles) {
            const cat = assignments[f.key];
            if (!cat) continue; // left uncategorized — admin files it manually later
            (finalGroups[cat] ||= []).push(f.file);
        }
        onConfirm(finalGroups);
    };

    const totalCount = allFiles.length;
    const categorizedCount = allFiles.filter((f) => assignments[f.key]).length;

    return (
        <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4" data-testid="categorization-review-modal">
            <div className="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[85vh] flex flex-col">
                <div className="flex items-center justify-between p-5 border-b border-slate-100">
                    <div>
                        <h3 className="font-display text-xl font-bold text-slate-950">Detected Categories</h3>
                        <p className="text-xs text-slate-500 mt-1">
                            {totalCount} file{totalCount === 1 ? "" : "s"} — {categorizedCount} categorized, {totalCount - categorizedCount} need review. Nothing uploads until you confirm.
                        </p>
                    </div>
                    <button type="button" onClick={onCancel} className="p-1.5 rounded-full hover:bg-slate-100 text-slate-500">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto p-5 space-y-5">
                    {Object.entries(grouped)
                        .sort(([a], [b]) => (a === "__uncategorized__" ? 1 : b === "__uncategorized__" ? -1 : 0))
                        .map(([bucket, files]) => {
                            const isUncategorized = bucket === "__uncategorized__";
                            return (
                                <div key={bucket} className="border border-slate-100 rounded-xl p-3.5 bg-slate-50/50">
                                    <div className="flex items-center justify-between mb-2.5">
                                        <span className="text-sm font-semibold text-slate-900">
                                            {isUncategorized ? "Uncategorized" : CATEGORY_LABELS[bucket] || bucket}
                                            <span className="ml-2 text-xs font-mono text-slate-500">{files.length}</span>
                                        </span>
                                        {!isUncategorized && (
                                            <button
                                                type="button"
                                                onClick={() => setCategoryForAll(bucket, null)}
                                                className="text-[11px] text-slate-500 hover:text-slate-800"
                                            >
                                                Move all to Uncategorized
                                            </button>
                                        )}
                                    </div>
                                    <div className="flex flex-wrap gap-2.5">
                                        {files.map((f) => (
                                            <div key={f.key} className="flex flex-col items-center gap-1 w-20">
                                                <div className="w-20 h-20 rounded-lg overflow-hidden bg-slate-200 border border-slate-200">
                                                    {f.file.type?.startsWith("image/") ? (
                                                        <img
                                                            src={URL.createObjectURL(f.file)}
                                                            alt=""
                                                            className="w-full h-full object-cover"
                                                        />
                                                    ) : (
                                                        <div className="w-full h-full flex items-center justify-center text-[10px] text-slate-500 px-1 text-center">
                                                            {f.file.name}
                                                        </div>
                                                    )}
                                                </div>
                                                <select
                                                    value={assignments[f.key] || ""}
                                                    onChange={(e) => setCategory(f.key, e.target.value)}
                                                    className="w-full text-[10px] border border-slate-200 rounded px-1 py-0.5 bg-white"
                                                    data-testid={`categorize-select-${f.key}`}
                                                >
                                                    <option value="">Uncategorized</option>
                                                    {ASSIGNABLE_CATEGORIES.map((c) => (
                                                        <option key={c} value={c}>{CATEGORY_LABELS[c]}</option>
                                                    ))}
                                                </select>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            );
                        })}
                </div>

                <div className="flex items-center justify-end gap-3 p-5 border-t border-slate-100">
                    <button
                        type="button"
                        onClick={onCancel}
                        className="px-4 py-2 rounded-lg text-sm font-medium text-slate-600 hover:bg-slate-100"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={handleConfirm}
                        disabled={categorizedCount === 0}
                        className="px-5 py-2 rounded-lg bg-[#0c2340] text-white text-sm font-semibold hover:opacity-90 disabled:opacity-40 flex items-center gap-1.5"
                        data-testid="categorize-confirm"
                    >
                        <Check className="w-4 h-4" />
                        Upload {categorizedCount} File{categorizedCount === 1 ? "" : "s"}
                    </button>
                </div>
            </div>
        </div>
    );
}
