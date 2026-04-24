import React, { useState } from "react";
import { Plus, X, IndianRupee } from "lucide-react";

/**
 * BudgetLines — editable key/value list for project budget breakdowns.
 *
 * Each line is `{ label: string, value: string }`. Empty rows are stripped
 * server-side via `_clean_budget_lines`, so we tolerate blanks locally.
 *
 * Fully theme-aware (uses shadcn tokens only).
 */
export default function BudgetLines({
    lines = [],
    onChange,
    testidPrefix = "budget",
    labelPlaceholder = "e.g. Shoot fee",
    valuePlaceholder = "e.g. ₹50,000",
}) {
    const [label, setLabel] = useState("");
    const [value, setValue] = useState("");

    const add = () => {
        const l = label.trim();
        const v = value.trim();
        if (!l && !v) return;
        onChange([...(lines || []), { label: l, value: v }]);
        setLabel("");
        setValue("");
    };

    const remove = (idx) => {
        onChange((lines || []).filter((_, j) => j !== idx));
    };

    const updateRow = (idx, patch) => {
        onChange(
            (lines || []).map((row, j) => (j === idx ? { ...row, ...patch } : row)),
        );
    };

    return (
        <div className="space-y-2" data-testid={`${testidPrefix}-list`}>
            {(lines || []).map((row, i) => (
                <div
                    key={i}
                    className="flex items-center gap-2 border-b border-border/40 pb-2"
                    data-testid={`${testidPrefix}-row-${i}`}
                >
                    <input
                        value={row.label || ""}
                        onChange={(e) => updateRow(i, { label: e.target.value })}
                        placeholder={labelPlaceholder}
                        className="flex-1 bg-transparent border-b border-transparent focus:border-foreground/40 outline-none py-1.5 text-sm"
                        data-testid={`${testidPrefix}-label-${i}`}
                    />
                    <input
                        value={row.value || ""}
                        onChange={(e) => updateRow(i, { value: e.target.value })}
                        placeholder={valuePlaceholder}
                        className="flex-1 bg-transparent border-b border-transparent focus:border-foreground/40 outline-none py-1.5 text-sm tg-mono"
                        data-testid={`${testidPrefix}-value-${i}`}
                    />
                    <button
                        type="button"
                        onClick={() => remove(i)}
                        className="text-muted-foreground hover:text-[var(--tg-danger)]"
                        data-testid={`${testidPrefix}-remove-${i}`}
                    >
                        <X className="w-3.5 h-3.5" />
                    </button>
                </div>
            ))}

            <div className="flex gap-2 pt-1">
                <div className="flex-1 flex items-center gap-2 border-b border-border focus-within:border-foreground/60 pb-1">
                    <input
                        value={label}
                        onChange={(e) => setLabel(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter") add();
                        }}
                        placeholder={labelPlaceholder}
                        className="flex-1 bg-transparent outline-none py-1.5 text-sm"
                        data-testid={`${testidPrefix}-new-label`}
                    />
                </div>
                <div className="flex-1 flex items-center gap-2 border-b border-border focus-within:border-foreground/60 pb-1">
                    <IndianRupee className="w-3 h-3 text-muted-foreground" />
                    <input
                        value={value}
                        onChange={(e) => setValue(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter") add();
                        }}
                        placeholder={valuePlaceholder}
                        className="flex-1 bg-transparent outline-none py-1.5 text-sm tg-mono"
                        data-testid={`${testidPrefix}-new-value`}
                    />
                </div>
                <button
                    type="button"
                    onClick={add}
                    className="text-xs px-3 py-2 border border-border hover:border-foreground/60 rounded-sm inline-flex items-center gap-1"
                    data-testid={`${testidPrefix}-add-btn`}
                >
                    <Plus className="w-3 h-3" /> Add
                </button>
            </div>
        </div>
    );
}
