import React from "react";
import { Trash2, X, Check } from "lucide-react";

/**
 * Floating action bar rendered at the bottom of the viewport when the user
 * has selected 1+ rows. Works across Talents / Projects / Links lists.
 *
 *  <BulkSelectBar
 *    count={selected.size}
 *    onClear={() => setSelected(new Set())}
 *    onSelectAll={() => setSelected(new Set(items.map(i => i.id)))}
 *    allSelected={selected.size === items.length}
 *    onDelete={() => setConfirmOpen(true)}
 *    labelSingular="talent"
 *    labelPlural="talents"
 *  />
 */
export default function BulkSelectBar({
    count,
    total,
    allSelected,
    onSelectAll,
    onClear,
    onDelete,
    onAssignTags,
    onRemoveTags,
    onExport,
    labelSingular = "item",
    labelPlural = "items",
    testid = "bulk-select-bar",
}) {
    if (count === 0) return null;
    const noun = count === 1 ? labelSingular : labelPlural;
    return (
        <div
            className="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 border border-border bg-background/95 backdrop-blur shadow-[0_8px_32px_rgba(0,0,0,0.5)] rounded-sm px-4 py-2.5 flex items-center gap-3 animate-in fade-in slide-in-from-bottom-2 max-w-[95vw] overflow-x-auto"
            data-testid={testid}
        >
            <span
                className="text-sm font-medium shrink-0"
                data-testid={`${testid}-count`}
            >
                {count} {noun} selected
            </span>
            {typeof total === "number" && total > count && (
                <button
                    type="button"
                    onClick={onSelectAll}
                    className="text-xs tg-mono text-muted-foreground hover:text-foreground inline-flex items-center gap-1 shrink-0"
                    data-testid={`${testid}-select-all`}
                >
                    <Check className="w-3 h-3" /> Select all {total}
                </button>
            )}
            {allSelected && total > 1 && (
                <span className="text-[10px] tg-mono text-muted-foreground shrink-0">
                    all selected
                </span>
            )}
            <span className="w-px h-5 bg-border shrink-0" />
            
            {onAssignTags && (
                <button
                    type="button"
                    onClick={onAssignTags}
                    className="text-xs px-3 py-2 border border-border hover:border-foreground/60 rounded-sm text-foreground inline-flex items-center gap-1 shrink-0"
                    data-testid={`${testid}-assign-tags`}
                >
                    Assign Tags
                </button>
            )}

            {onRemoveTags && (
                <button
                    type="button"
                    onClick={onRemoveTags}
                    className="text-xs px-3 py-2 border border-border hover:border-foreground/60 rounded-sm text-foreground inline-flex items-center gap-1 shrink-0"
                    data-testid={`${testid}-remove-tags`}
                >
                    Remove Tags
                </button>
            )}

            {onExport && (
                <button
                    type="button"
                    onClick={onExport}
                    className="text-xs px-3 py-2 border border-border hover:border-foreground/60 rounded-sm text-foreground inline-flex items-center gap-1 shrink-0"
                    data-testid={`${testid}-export`}
                >
                    Export
                </button>
            )}

            <button
                type="button"
                onClick={onDelete}
                className="text-xs px-3 py-2 bg-[var(--tg-danger)] text-white rounded-sm inline-flex items-center gap-1.5 hover:opacity-90 shrink-0"
                data-testid={`${testid}-delete`}
            >
                <Trash2 className="w-3.5 h-3.5" />
                Delete {count}
            </button>
            <button
                type="button"
                onClick={onClear}
                className="text-xs px-2 py-2 border border-border hover:border-foreground/60 rounded-sm shrink-0"
                aria-label="Clear selection"
                data-testid={`${testid}-clear`}
            >
                <X className="w-3.5 h-3.5" />
            </button>
        </div>
    );
}
