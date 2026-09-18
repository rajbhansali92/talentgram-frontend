import React, { useState, useEffect, useRef } from "react";
import { X, Plus, Search, Tag, Loader2, AlertTriangle } from "lucide-react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { talentPreviewCache } from "@/lib/talentPreviewCache";

export default function TagPopover({ talent, onSave, onClose }) {
    const [allTags, setAllTags] = useState([]);
    const [tagSearch, setTagSearch] = useState("");
    const [saving, setSaving] = useState(false);
    const [localTags, setLocalTags] = useState(talent.tags || []);
    // Multi-select: clicking an existing tag only stages it here — nothing
    // is persisted until "Save & Close", so the user can pick several tags
    // in one sitting without a round-trip (and the modal staying open)
    // between every single click.
    const [pendingTags, setPendingTags] = useState([]);

    useEffect(() => {
        let isMounted = true;
        (async () => {
            try {
                const { data } = await adminApi.get("/tags");
                if (isMounted) {
                    setAllTags(data.tags || []);
                }
            } catch (err) {
                console.error("Failed to load tags", err);
            }
        })();
        return () => { isMounted = false; };
    }, []);

    const togglePendingTag = (tag) => {
        setPendingTags(prev =>
            prev.some(t => t.id === tag.id)
                ? prev.filter(t => t.id !== tag.id)
                : [...prev, { id: tag.id, name: tag.name }]
        );
    };

    const saveAndClose = async () => {
        if (pendingTags.length === 0) {
            onClose();
            return;
        }
        setSaving(true);
        const succeeded = [];
        const failed = [];
        for (const tag of pendingTags) {
            try {
                await adminApi.post(`/talents/${talent.id}/tag/${tag.id}`);
                succeeded.push(tag);
            } catch (e) {
                failed.push(tag);
                toast.error(formatErrorDetail(e, `Failed to assign tag "${tag.name}"`));
            }
        }
        setSaving(false);
        if (succeeded.length > 0) {
            const updated = [...localTags, ...succeeded];
            setLocalTags(updated);
            onSave(talent.id, updated);
            talentPreviewCache.invalidateTalent(talent.id);
            toast.success(succeeded.length === 1 ? `Assigned tag "${succeeded[0].name}"` : `Assigned ${succeeded.length} tags`);
        }
        // Only the tags that failed stay pending, so the user can retry
        // just those via Save & Close again without losing the selection.
        setPendingTags(failed);
        if (failed.length === 0) {
            onClose();
        }
    };

    const removeTag = async (tagId) => {
        setSaving(true);
        try {
            await adminApi.delete(`/talents/${talent.id}/tag/${tagId}`);
            const updated = localTags.filter(t => t.id !== tagId);
            setLocalTags(updated);
            onSave(talent.id, updated);
            talentPreviewCache.invalidateTalent(talent.id);
            toast.success("Tag removed");
        } catch (e) {
            toast.error(formatErrorDetail(e, "Failed to remove tag"));
        } finally {
            setSaving(false);
        }
    };

    const createAndAssignTag = async () => {
        const name = tagSearch.trim();
        if (!name) return;
        setSaving(true);
        try {
            const { data } = await adminApi.post("/tags", { name });
            const tag = data.tag;
            if (data.created) {
                setAllTags(prev => [...prev, tag].sort((a, b) => a.name.localeCompare(b.name)));
            }
            const already = localTags.some(t => t.id === tag.id);
            if (!already) {
                await adminApi.post(`/talents/${talent.id}/tag/${tag.id}`);
                const updated = [...localTags, { id: tag.id, name: tag.name }];
                setLocalTags(updated);
                onSave(talent.id, updated);
                talentPreviewCache.invalidateTalent(talent.id);
            }
            setTagSearch("");
            toast.success(data.created ? `Tag "${name}" created and assigned` : `Tag "${name}" assigned`);
        } catch (e) {
            toast.error(formatErrorDetail(e, "Failed to create tag"));
        } finally {
            setSaving(false);
        }
    };

    const filtered = allTags
        .filter(t => !localTags.some(lt => lt.id === t.id))
        .filter(t => !pendingTags.some(pt => pt.id === t.id))
        .filter(t => t.name.toLowerCase().includes(tagSearch.toLowerCase()));

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm" onClick={onClose}>
            <div 
                className="bg-white rounded-2xl p-5 max-w-sm w-full shadow-2xl border border-black/[0.08] text-black relative flex flex-col max-h-[85vh]"
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between mb-4 shrink-0">
                    <div className="flex items-center gap-2">
                        <Tag className="w-4 h-4 text-black/50" />
                        <h3 className="font-semibold text-sm text-neutral-800 truncate max-w-[200px]">Tags: {talent.name}</h3>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full hover:bg-black/5 transition-colors">
                        <X className="w-4 h-4 text-black/40 hover:text-black" />
                    </button>
                </div>

                {/* Assigned tags */}
                <div className="flex flex-wrap gap-1.5 mb-4 max-h-24 overflow-y-auto shrink-0 pb-1 border-b border-black/[0.05]">
                    {localTags.map(tag => (
                        <span key={tag.id} className="inline-flex items-center gap-1 px-2.5 py-1 bg-black/[0.05] border border-black/[0.06] text-[11px] rounded-full">
                            {tag.name}
                            <button onClick={() => removeTag(tag.id)} className="text-black/35 hover:text-red-500 transition-colors ml-0.5">
                                <X className="w-3 h-3" />
                            </button>
                        </span>
                    ))}
                    {localTags.length === 0 && <span className="text-xs text-black/30 italic">No tags assigned</span>}
                </div>

                {/* Pending tags — selected but not yet saved; clearly
                    distinguishable from the already-assigned chips above
                    via a dashed border, and toggle off with the same X. */}
                {pendingTags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mb-3 shrink-0" data-testid="pending-tags">
                        {pendingTags.map(tag => (
                            <span
                                key={tag.id}
                                data-testid={`pending-tag-${tag.id}`}
                                className="inline-flex items-center gap-1 px-2.5 py-1 bg-amber-50 border border-dashed border-amber-300 text-amber-700 text-[11px] rounded-full"
                            >
                                {tag.name}
                                <button onClick={() => togglePendingTag(tag)} className="text-amber-500 hover:text-red-500 transition-colors ml-0.5">
                                    <X className="w-3 h-3" />
                                </button>
                            </span>
                        ))}
                    </div>
                )}

                {/* Tag Search/Add input */}
                <div className="relative mb-3 shrink-0">
                    <input
                        type="text"
                        value={tagSearch}
                        onChange={e => setTagSearch(e.target.value)}
                        placeholder="Search or type new tag..."
                        maxLength={80}
                        className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                    />
                </div>

                {/* Suggestions List */}
                <div className="flex-1 overflow-y-auto mb-4 border border-black/[0.04] rounded-lg divide-y divide-black/[0.04]">
                    {filtered.map(tag => (
                        <button
                            key={tag.id}
                            type="button"
                            onClick={() => togglePendingTag(tag)}
                            data-testid={`tag-option-${tag.id}`}
                            className="w-full text-left px-3.5 py-2.5 text-xs hover:bg-black/[0.02] flex items-center justify-between text-black/75 hover:text-black"
                        >
                            <span>{tag.name}</span>
                            <span className="text-[10px] text-black/30">Add</span>
                        </button>
                    ))}
                    {tagSearch.trim() && !allTags.some(t => t.name.toLowerCase() === tagSearch.trim().toLowerCase()) && (
                        <button
                            type="button"
                            onClick={createAndAssignTag}
                            className="w-full text-left px-3.5 py-2.5 text-xs text-emerald-600 font-semibold hover:bg-emerald-50 flex items-center justify-between"
                        >
                            <span>Create & assign "{tagSearch.trim()}"</span>
                            <Plus className="w-3.5 h-3.5" />
                        </button>
                    )}
                    {filtered.length === 0 && !tagSearch.trim() && (
                        <div className="p-4 text-xs text-black/30 italic text-center">No other tags available</div>
                    )}
                </div>

                {/* Footer Save / Done */}
                <button
                    onClick={saveAndClose}
                    disabled={saving}
                    className="w-full py-2.5 bg-black text-white hover:bg-black/90 disabled:opacity-60 font-medium rounded-lg text-xs transition-colors shrink-0 flex items-center justify-center gap-1.5"
                >
                    {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    Save & Close
                </button>
            </div>
        </div>
    );
}
