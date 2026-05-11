import { useCallback, useMemo, useState } from "react";
import { DEFAULT_FILTERS, normaliseStage } from "@/components/pipeline/constants";

/**
 * usePipelineFilters — view-layer-only filtering (PATCH 4E).
 *
 * Filters NEVER mutate `data`. We compute `filteredData` lazily via
 * useMemo, keyed on the four filter inputs. `hiddenStages` is a Set
 * applied at the section/column render level (column stops being
 * rendered, not filtered out of `data`). `statusFocus === "follow_up"`
 * is special: it collapses every section except the follow-up lane,
 * giving the casting team a single-click "needs attention" view.
 *
 * `has_submission` is a best-effort inference from the existing API
 * surface — we use `is_follow_up` as the proxy (true ⇒ no submission;
 * false ⇒ submission has been received OR the talent has progressed
 * beyond ask_to_test). Reasonable because once a talent leaves
 * ask_to_test, they were necessarily acted on by an admin or by
 * Patch 3B's auto-sync, which requires a submission.
 */
export function usePipelineFilters(data) {
    const [search, setSearch] = useState(DEFAULT_FILTERS.search);
    const [statusFocus, setStatusFocus] = useState(DEFAULT_FILTERS.statusFocus);
    const [hasSubmission, setHasSubmission] = useState(DEFAULT_FILTERS.hasSubmission);
    const [hasIg, setHasIg] = useState(DEFAULT_FILTERS.hasIg);
    const [hiddenStages, setHiddenStages] = useState(() => new Set());

    const filtersActive =
        Boolean(search) ||
        statusFocus !== DEFAULT_FILTERS.statusFocus ||
        hasSubmission !== DEFAULT_FILTERS.hasSubmission ||
        hasIg !== DEFAULT_FILTERS.hasIg ||
        hiddenStages.size > 0;

    const filteredData = useMemo(() => {
        let rows = data;

        if (search) {
            const q = search.toLowerCase().trim();
            if (q) {
                rows = rows.filter((r) => {
                    const hay = [
                        r.talent_name,
                        r.instagram_handle,
                        r.talent_email,
                        r.email,
                        r.talent_phone,
                    ]
                        .filter(Boolean)
                        .join(" ")
                        .toLowerCase();
                    return hay.includes(q);
                });
            }
        }

        if (statusFocus !== "all") {
            rows = rows.filter((r) => {
                if (statusFocus === "follow_up") return r.is_follow_up === true;
                const s = normaliseStage(r.stage);
                if (statusFocus === "pending") return s === "ask_to_test" || s === "hold";
                return s === statusFocus;
            });
        }

        if (hasSubmission !== "any") {
            rows = rows.filter((r) =>
                hasSubmission === "yes" ? r.is_follow_up === false : r.is_follow_up === true,
            );
        }

        if (hasIg !== "any") {
            rows = rows.filter((r) =>
                hasIg === "yes" ? Boolean(r.instagram_handle) : !r.instagram_handle,
            );
        }

        return rows;
    }, [data, search, statusFocus, hasSubmission, hasIg]);

    const toggleStageVisibility = useCallback((stage) => {
        setHiddenStages((prev) => {
            const next = new Set(prev);
            if (next.has(stage)) next.delete(stage);
            else next.add(stage);
            return next;
        });
    }, []);

    const clearAllFilters = useCallback(() => {
        setSearch(DEFAULT_FILTERS.search);
        setStatusFocus(DEFAULT_FILTERS.statusFocus);
        setHasSubmission(DEFAULT_FILTERS.hasSubmission);
        setHasIg(DEFAULT_FILTERS.hasIg);
        setHiddenStages(new Set());
    }, []);

    const showOnlyFollowUp = statusFocus === "follow_up";
    const hasZeroAfterFilter = filteredData.length === 0 && filtersActive;

    return {
        search,
        setSearch,
        statusFocus,
        setStatusFocus,
        hasSubmission,
        setHasSubmission,
        hasIg,
        setHasIg,
        hiddenStages,
        toggleStageVisibility,
        clearAllFilters,
        filtersActive,
        filteredData,
        showOnlyFollowUp,
        hasZeroAfterFilter,
    };
}
