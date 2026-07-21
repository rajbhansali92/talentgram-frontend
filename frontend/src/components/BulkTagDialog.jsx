import React, { useState, useEffect } from "react";
import { X, Tag, Loader2 } from "lucide-react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { talentPreviewCache } from "@/lib/talentPreviewCache";

export default function BulkTagDialog({ selectedCount, selectedIds, actionType, onSave, onClose }) {
    const [allTags, setAllTags] = useState([]);
    const [tagSearch, setTagSearch] = useState("");
    const [saving, setSaving] = useState(false);

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

    const handleTagSelect = async (tag) => {
        setSaving(true);
        try {
            const url = actionType === "assign" ? "/talents/bulk-assign-tag" : "/talents/bulk-remove-tag";
            await adminApi.post(url, {
                ids: selectedIds,
                tag_id: tag.id
            });
            onSave(tag, actionType);
            selectedIds.forEach(id => talentPreviewCache.invalidateTalent(id));
            toast.success(`Successfully ${actionType === "assign" ? "assigned" : "removed"} tag "${tag.name}" for selected talents.`);
            onClose();
        } catch (e) {
            toast.error(formatErrorDetail(e, "Bulk operation failed"));
        } finally {
            setSaving(false);
        }
    };

    const handleCreateAndAssign = async () => {
        const name = tagSearch.trim();
        if (!name) return;
        setSaving(true);
        try {
            const { data } = await adminApi.post("/tags", { name });
            const tag = data.tag;
            await adminApi.post("/talents/bulk-assign-tag", {
                ids: selectedIds,
                tag_id: tag.id
            });
            onSave(tag, "assign");
            selectedIds.forEach(id => talentPreviewCache.invalidateTalent(id));
            toast.success(`Created & assigned tag "${name}" for selected talents.`);
            onClose();
        } catch (e) {
            toast.error(formatErrorDetail(e, "Failed to create & assign tag"));
        } finally {
            setSaving(false);
        }
    };

    const filtered = allTags.filter(t => t.name.toLowerCase().includes(tagSearch.toLowerCase()));

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm" onClick={onClose}>
            <div 
                className="bg-white rounded-2xl p-6 max-w-sm w-full shadow-2xl border border-black/[0.08] text-black relative flex flex-col max-h-[80vh]"
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between mb-4 shrink-0">
                    <div className="flex items-center gap-2">
                        <Tag className="w-4 h-4 text-black/50" />
                        <h3 className="font-semibold text-sm text-neutral-800">
                            {actionType === "assign" ? "Bulk Assign Tag" : "Bulk Remove Tag"}
                        </h3>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full hover:bg-black/5 transition-colors">
                        <X className="w-4 h-4 text-black/40 hover:text-black" />
                    </button>
                </div>

                <p className="text-xs text-black/50 mb-4 shrink-0">
                    {actionType === "assign" 
                        ? `Select a tag to assign to all ${selectedCount} selected talents.`
                        : `Select a tag to remove from all ${selectedCount} selected talents.`}
                </p>

                <div className="relative mb-3 shrink-0">
                    <input
                        type="text"
                        value={tagSearch}
                        onChange={e => setTagSearch(e.target.value)}
                        placeholder="Search tags..."
                        className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                    />
                </div>

                <div className="flex-1 overflow-y-auto mb-4 border border-black/[0.04] rounded-lg divide-y divide-black/[0.04]">
                    {filtered.map(tag => (
                        <button
                            key={tag.id}
                            type="button"
                            onClick={() => handleTagSelect(tag)}
                            disabled={saving}
                            className="w-full text-left px-3.5 py-2.5 text-xs hover:bg-black/[0.02] flex items-center justify-between text-black/75 hover:text-black disabled:opacity-50"
                        >
                            <span>{tag.name}</span>
                            <span className="text-[10px] text-black/30 font-medium">Select</span>
                        </button>
                    ))}
                    {actionType === "assign" && tagSearch.trim() && !allTags.some(t => t.name.toLowerCase() === tagSearch.trim().toLowerCase()) && (
                        <button
                            type="button"
                            onClick={handleCreateAndAssign}
                            disabled={saving}
                            className="w-full text-left px-3.5 py-2.5 text-xs text-emerald-600 font-semibold hover:bg-emerald-50 flex items-center justify-between disabled:opacity-50"
                        >
                            <span>Create & assign "{tagSearch.trim()}"</span>
                            <Tag className="w-3.5 h-3.5" />
                        </button>
                    )}
                    {filtered.length === 0 && (
                        <div className="p-4 text-xs text-black/30 italic text-center">No tags found</div>
                    )}
                </div>

                <div className="flex gap-2 shrink-0">
                    <button
                        onClick={onClose}
                        className="flex-1 py-2.5 border border-black/[0.08] hover:bg-black/[0.02] text-black/60 hover:text-black text-xs font-medium rounded-lg transition-colors"
                    >
                        Cancel
                    </button>
                    {saving && (
                        <div className="flex items-center justify-center px-4">
                            <Loader2 className="w-4 h-4 animate-spin text-black/50" />
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
