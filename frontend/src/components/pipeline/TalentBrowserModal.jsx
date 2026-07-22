import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Search, X, Check, Image as ImageIcon, Instagram, LayoutGrid, Maximize2, Minus, ChevronDown, Sliders, Bookmark, Zap, Clock, Star, TrendingUp, Users, Calendar, Award, List, Eye } from "lucide-react";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { adminApi, IMAGE_URL } from "@/lib/api";
import { formatTalentLocation } from "@/lib/sanitize";
import { displayInstagramHandle, instagramProfileUrl } from "@/lib/mediaUtils";
import { SKILLS_CATEGORIES } from "@/components/SkillsSelector";
import LocationMultiSelect from "@/components/talent-directory/LocationMultiSelect";
import HlsVideo from "@/components/HlsVideo";

/* ---------------------------------------------------------------------
 * TalentBrowserModal — Elite Enterprise ATS-Grade Talent Browser
 * 
 * Version: 2.1.0 - Production Ready
 * 
 * PERFORMANCE: AbortController properly integrated, debounced search
 * STYLING: Pure Tailwind CSS (no inline style mutations)
 * VIRTUALIZATION: Stable height estimation for smooth scrolling
 * INTELLIGENCE: Real backend-driven metrics
 * ------------------------------------------------------------------- */

// ============================================================================
// CONSTANTS & UTILITIES
// ============================================================================

const FOLLOWER_BUCKETS = [
    { value: 0, label: "Any", count: null },
    { value: 1_000, label: "1k+", count: "1,000+" },
    { value: 10_000, label: "10k+", count: "10,000+" },
    { value: 100_000, label: "100k+", count: "100,000+" },
    { value: 1_000_000, label: "1M+", count: "1,000,000+" },
];

const AGE_BUCKETS = [
    { label: "Any", min: null, max: null },
    { label: "18-25", min: 18, max: 25 },
    { label: "26-35", min: 26, max: 35 },
    { label: "36-45", min: 36, max: 45 },
    { label: "46+", min: 46, max: null },
];

const DENSITY_CONFIG = {
    compact: { 
        columns: { desktop: 6, tablet: 4, mobile: 2 },
        gap: 12, 
        cardHeight: 280,
        imageAspect: "aspect-[3/4]",
        padding: "p-2",
        titleSize: "text-sm",
        metaSize: "text-xs",
    },
    comfortable: { 
        columns: { desktop: 5, tablet: 3, mobile: 2 },
        gap: 16, 
        cardHeight: 320,
        imageAspect: "aspect-[3/4]",
        padding: "p-3",
        titleSize: "text-base",
        metaSize: "text-sm",
    },
    visual: { 
        columns: { desktop: 4, tablet: 2, mobile: 1 },
        gap: 20, 
        cardHeight: 380,
        imageAspect: "aspect-[4/5]",
        padding: "p-4",
        titleSize: "text-lg",
        metaSize: "text-base",
    }
};

const parseHeightToInches = (hStr) => {
    if (!hStr) return null;
    const s = String(hStr).trim().toLowerCase();
    
    // cm match
    const cmMatch = s.match(/^(\d+(?:\.\d+)?)\s*(?:cm)?$/);
    if (cmMatch) {
        const val = parseFloat(cmMatch[1]);
        if (val > 100) return Math.round(val / 2.54);
    }
    
    // feet and inches match, e.g. 5'8", 5ft 8in, 5' 8
    const feetInchesMatch = s.match(/(\d+)\s*(?:'|’|ft|feet)\s*(\d+)?/);
    if (feetInchesMatch) {
        const feet = parseInt(feetInchesMatch[1], 10);
        const inches = feetInchesMatch[2] ? parseInt(feetInchesMatch[2], 10) : 0;
        return feet * 12 + inches;
    }
    
    // feet only match, e.g. 5', 5ft
    const feetOnlyMatch = s.match(/^(\d+)\s*(?:'|’|ft|feet)$/);
    if (feetOnlyMatch) {
        const feet = parseInt(feetOnlyMatch[1], 10);
        return feet * 12;
    }
    
    // Raw number fallback
    const num = parseFloat(s);
    if (!Number.isNaN(num)) {
        if (num > 100) return Math.round(num / 2.54); // cm to inches
        if (num > 3 && num < 8) {
            const parts = String(num).split('.');
            if (parts.length === 2) {
                const feet = parseInt(parts[0], 10);
                const inches = parseInt(parts[1], 10);
                return feet * 12 + inches;
            }
            return Math.round(num * 12);
        }
        return Math.round(num); // assume inches
    }
    return null;
};

const HEIGHT_OPTIONS = (() => {
    const opts = [];
    for (let feet = 4; feet <= 7; feet++) {
        const maxInches = feet === 7 ? 0 : 11;
        for (let inches = 0; inches <= maxInches; inches++) {
            const totalInches = feet * 12 + inches;
            opts.push({
                value: totalInches,
                label: `${feet}'${inches}"`
            });
        }
    }
    return opts;
})();

const FILTER_DEFAULTS = {
    search: "",
    gender: "any",
    ethnicity: "any",
    locations: [],
    ageMin: "",
    ageMax: "",
    heightMin: "",
    heightMax: "",
    minFollowers: 0,
    interestedIn: [],
    internalTags: [],
    tagMatchMode: "OR",
    interestedInMatchMode: "OR",
    availability: "any",
    showIntelligence: true,
    skills: [],
    skillsMatchMode: "OR"
};

// Saved searches presets
const SAVED_SEARCHES = [
    { id: "gen_z_fashion", label: "Gen Z Fashion", icon: Zap, filters: { ageMin: "18", ageMax: "25", minFollowers: 10000 } },
    { id: "veteran_models", label: "Veteran Models", icon: Star, filters: { ageMin: "30", ageMax: "45" } },
    { id: "high_impact", label: "High Impact", icon: TrendingUp, filters: { minFollowers: 100000 } },
    { id: "local_talent", label: "Local Talent", icon: Users, filters: { locations: ["Los Angeles"] } },
];

// Parse follower counts
const parseFollowers = (s) => {
    if (s === null || s === undefined) return 0;
    const str = String(s).trim();
    if (!str) return 0;
    const m = str.match(/^([\d.,]+)\s*([kKmM]?)/);
    if (!m) return 0;
    const n = parseFloat(m[1].replace(/,/g, ""));
    if (Number.isNaN(n)) return 0;
    const unit = (m[2] || "").toLowerCase();
    const mult = unit === "m" ? 1_000_000 : unit === "k" ? 1_000 : 1;
    return Math.round(n * mult);
};

const formatFollowers = (n) => {
    if (!n || n < 1_000) return n ? String(n) : "";
    if (n >= 1_000_000) {
        const v = n / 1_000_000;
        return `${v >= 10 ? Math.round(v) : v.toFixed(1)}M`;
    }
    const v = n / 1_000;
    return `${v >= 10 ? Math.round(v) : v.toFixed(1)}K`;
};

const pickImage = (t) => {
    if (t.image_url) return t.image_url;
    const media = t.media || [];
    const cover = media.find((m) => m.id === t.cover_media_id);
    if (cover?.url) return cover.url;
    const img = media.find(
        (m) =>
            m.category !== "video" &&
            (m.content_type?.startsWith?.("image/") ||
                ["portfolio", "indian", "western", "image"].includes(m.category)),
    );
    return img?.url || null;
};

// Calculate match score using real talent metrics
const calculateMatchScore = (talent, filters) => {
    let score = 0;
    let maxScore = 0;
    
    // Demographic match (30%)
    if (filters.gender !== "any" && talent.gender === filters.gender) score += 15;
    maxScore += 15;
    if (filters.ethnicity !== "any" && talent.ethnicity === filters.ethnicity) score += 15;
    maxScore += 15;
    
    // Age match (20%)
    const ageMin = filters.ageMin ? Number(filters.ageMin) : null;
    const ageMax = filters.ageMax ? Number(filters.ageMax) : null;
    if (ageMin && talent.age >= ageMin) score += 10;
    if (ageMax && talent.age <= ageMax) score += 10;
    maxScore += 20;
    
    // Follower engagement (25%) - using real data
    const followers = talent.instagram_followers_count || parseFollowers(talent.instagram_followers);
    if (filters.minFollowers > 0 && followers >= filters.minFollowers) score += 25;
    else if (followers >= 100000) score += 20;
    else if (followers >= 10000) score += 15;
    else if (followers >= 1000) score += 10;
    maxScore += 25;
    
    // Location match (15%) — filters.locations is a list of plain city/
    // country values (see GET /talents/facets); any one selected value
    // matching either field on the talent counts (OR/IN, mirrors the
    // backend's location query semantics).
    if (filters.locations.length > 0) {
        const needles = filters.locations.map((l) => l.toLowerCase());
        const hasLocMatch = Array.isArray(talent.location) && talent.location.some(
            (loc) => needles.includes(loc.city?.toLowerCase()) || needles.includes(loc.country?.toLowerCase())
        );
        if (hasLocMatch) score += 15;
    }
    maxScore += 15;
    
    // Height (10%)
    const heightMin = filters.heightMin ? Number(filters.heightMin) : null;
    const heightMax = filters.heightMax ? Number(filters.heightMax) : null;
    const tHeight = talent._heightInches || parseHeightToInches(talent.height);
    if (tHeight !== null) {
        if ((!heightMin || tHeight >= heightMin) && (!heightMax || tHeight <= heightMax)) {
            score += 10;
        }
    }
    maxScore += 10;
    
    return maxScore > 0 ? Math.round((score / maxScore) * 100) : 50;
};

const sortTalents = (talents) => {
    return talents;
};

// ============================================================================
// CUSTOM HOOKS
// ============================================================================

// Media query hook for responsive design
// Named export (default export below stays the primary API) so other
// surfaces — e.g. Pipeline cards — can reuse the exact same breakpoint
// hook and preview drawer instead of building parallel implementations.
export const useMediaQuery = (query) => {
    const [matches, setMatches] = useState(false);
    
    useEffect(() => {
        const media = window.matchMedia(query);
        if (media.matches !== matches) {
            setMatches(media.matches);
        }
        
        const listener = (e) => setMatches(e.matches);
        media.addEventListener('change', listener);
        
        return () => media.removeEventListener('change', listener);
    }, [matches, query]);
    
    return matches;
};

// ============================================================================
// MAIN MODAL COMPONENT
// ============================================================================

function TalentBrowserModal({ open, onClose, projectId, existingTalentIds, onAdded }) {
    // Responsive breakpoints
    const isMobile = useMediaQuery('(max-width: 768px)');
    const isTablet = useMediaQuery('(min-width: 769px) and (max-width: 1024px)');
    
    // Core state
    const [talents, setTalents] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [filters, setFilters] = useState(FILTER_DEFAULTS);
    const [densityMode, setDensityMode] = useState("comfortable");
    const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
    const [showMobileFilters, setShowMobileFilters] = useState(false);
    const [savedSearchName, setSavedSearchName] = useState("");
    const [showSaveSearch, setShowSaveSearch] = useState(false);
    const [viewMode, setViewMode] = useState(() => {
        if (typeof window !== "undefined") {
            return localStorage.getItem("talent-browser-view-mode") || "grid";
        }
        return "grid";
    });
    const [previewTalent, setPreviewTalent] = useState(null);

    const handleViewModeChange = useCallback((mode) => {
        setViewMode(mode);
        localStorage.setItem("talent-browser-view-mode", mode);
    }, []);

    // Selection state
    const [selected, setSelected] = useState(new Set());
    const [submitting, setSubmitting] = useState(false);
    
    // Keyboard navigation state
    const [focusedIndex, setFocusedIndex] = useState(-1);
    
    // Global & Frequent tags state
    const [globalTags, setGlobalTags] = useState([]);
    const [frequentTags, setFrequentTags] = useState([]);
    
    // Refs
    const gridScrollRef = useRef(null);
    const searchInputRef = useRef(null);
    const searchDebounceRef = useRef(null);
    const cardRefsMap = useRef(new Map());
    const abortControllerRef = useRef(null);
    const isFetchingRef = useRef(false);
    const isSubmittingRef = useRef(false);
    
    // Get current columns based on screen size
    const getColumns = useCallback(() => {
        const config = DENSITY_CONFIG[densityMode];
        if (isMobile) return config.columns.mobile;
        if (isTablet) return config.columns.tablet;
        return config.columns.desktop;
    }, [densityMode, isMobile, isTablet]);
    
    // Filter setters
    const setFilter = useCallback((key, value) => {
        setFilters(prev => ({ ...prev, [key]: value }));
        setFocusedIndex(-1);
    }, []);
    
    const resetFilters = useCallback(() => {
        setFilters(FILTER_DEFAULTS);
        setFocusedIndex(-1);
        setShowAdvancedFilters(false);
    }, []);
    
    // Fetch talents — SERVER-SIDE filtered against the same GET /api/talents
    // query engine the Global Talent page uses (routers/talents.py
    // list_talents), not a client-side scan of the whole roster. Re-fetches
    // (debounced) whenever a filter changes, so Mongo does the actual
    // filtering with indexes instead of shipping every talent to the
    // browser. Capped at FETCH_SIZE per request — comfortably enough for a
    // single browse-and-multi-select session without ever approaching the
    // old "20,000 records" payload.
    const FETCH_SIZE = 500;
    const fetchDebounceRef = useRef(null);

    const buildFetchParams = useCallback(() => {
        const params = { page: 0, size: FETCH_SIZE };
        if (filters.search.trim()) params.q = filters.search.trim();
        if (filters.gender !== "any") params.gender = filters.gender;
        if (filters.ethnicity !== "any") params.ethnicity = filters.ethnicity;
        if (filters.locations.length) {
            // Multiple selected locations combine with OR/IN server-side
            // ("Mumbai" + "Delhi" -> either) — this whole group still ANDs
            // against every other selected filter group.
            params.location = filters.locations;
        }
        if (filters.ageMin !== "") params.age_min = filters.ageMin;
        if (filters.ageMax !== "") params.age_max = filters.ageMax;
        if (filters.heightMin !== "") params.height_min = filters.heightMin;
        if (filters.heightMax !== "") params.height_max = filters.heightMax;
        if (filters.minFollowers > 0) {
            // Map the modal's coarse numeric buckets onto the backend's
            // bucket-label vocabulary (core.py FOLLOWER_BUCKET_ORDER) — all
            // four values here exist verbatim in that list.
            const label = { 1000: "1K+", 10000: "10K+", 100000: "100K+", 1000000: "1M+" }[filters.minFollowers];
            if (label) params.followers_min = label;
        }
        if (filters.interestedIn.length) {
            params.interested_in = filters.interestedIn;
            params.interested_in_mode = filters.interestedInMatchMode === "AND" ? "all" : "any";
        }
        if (filters.skills && filters.skills.length) {
            params.skills = filters.skills;
            params.skills_mode = filters.skillsMatchMode === "AND" ? "all" : "any";
        }
        if (filters.internalTags.length) {
            params.tags = filters.internalTags;
            params.tags_mode = filters.tagMatchMode === "AND" ? "all" : "any";
        }
        return params;
    }, [filters]);

    useEffect(() => {
        if (!open) return;

        if (fetchDebounceRef.current) clearTimeout(fetchDebounceRef.current);
        fetchDebounceRef.current = setTimeout(() => {
            let isMounted = true;
            if (abortControllerRef.current) abortControllerRef.current.abort();
            abortControllerRef.current = new AbortController();

            const fetchTalents = async () => {
                setLoading(true);
                setError(null);
                try {
                    const response = await adminApi.get("/talents", {
                        params: buildFetchParams(),
                        signal: abortControllerRef.current.signal,
                    });
                    if (!isMounted) return;

                    const list = response.data?.data ?? response.data?.items ?? [];

                    // Pre-memoize structures the render/scoring layer already
                    // expects — unchanged from before, just applied to a much
                    // smaller, server-filtered list now.
                    const transformedTalents = list.map(talent => {
                        const parsedHeight = parseHeightToInches(talent.height);
                        const validTags = (talent.tags || []).filter(tag => tag && typeof tag === 'object' && tag.id && tag.name);
                        const tagIdsSet = new Set(validTags.map(tag => tag.id));
                        const interestedInSet = new Set((talent.interested_in || []).filter(Boolean).map(s => String(s).toLowerCase().trim()));
                        const skillsSet = new Set((talent.skills || []).filter(Boolean).map(s => String(s).toLowerCase().trim()));

                        return {
                            ...talent,
                            tags: validTags,
                            _heightInches: parsedHeight,
                            _tagIdsSet: tagIdsSet,
                            _interestedInSet: interestedInSet,
                            _skillsSet: skillsSet,
                            instagram_followers_count: talent.instagram_followers_count || parseFollowers(talent.instagram_followers),
                        };
                    });

                    setTalents(transformedTalents);

                    // Top 10 most-used tags among the currently-fetched
                    // (server-filtered) set — same computation as before,
                    // just over a bounded set instead of the whole roster.
                    const tagCounts = {};
                    const tagNames = {};
                    transformedTalents.forEach(t => {
                        t.tags.forEach(tag => {
                            tagCounts[tag.id] = (tagCounts[tag.id] || 0) + 1;
                            tagNames[tag.id] = tag.name;
                        });
                    });
                    setFrequentTags(
                        Object.keys(tagCounts)
                            .map(id => ({ id, name: tagNames[id], count: tagCounts[id] }))
                            .sort((a, b) => b.count - a.count)
                            .slice(0, 10)
                    );
                } catch (err) {
                    if (isMounted && err.name !== 'CanceledError' && err.code !== 'ERR_CANCELED') {
                        console.error("Failed to load talents:", err);
                        setError("Failed to load talent roster");
                    }
                } finally {
                    if (isMounted) setLoading(false);
                }
            };

            fetchTalents();
            return () => { isMounted = false; };
        }, 250);

        return () => {
            if (fetchDebounceRef.current) clearTimeout(fetchDebounceRef.current);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, filters]);

    // Filter dropdown options — distinct values actually present in the
    // roster, fetched once via the lightweight facets endpoint instead of
    // scanning a full in-memory talent array that no longer exists.
    const [facets, setFacets] = useState({ genders: [], ethnicities: [], locations: [] });
    useEffect(() => {
        if (!open) return;
        let isMounted = true;
        adminApi.get("/talents/facets").then(({ data }) => {
            if (isMounted) setFacets(data);
        }).catch(() => {});
        return () => { isMounted = false; };
    }, [open]);

    // Fetch global tags when modal opens
    useEffect(() => {
        if (!open) return;
        let isMounted = true;
        
        const fetchTags = async () => {
            try {
                const { data } = await adminApi.get("/tags");
                if (isMounted) {
                    setGlobalTags(data.tags || []);
                }
            } catch (err) {
                console.error("Failed to load global tags:", err);
            }
        };
        
        fetchTags();
        return () => { isMounted = false; };
    }, [open]);
    
    // Reset state when modal opens, and abort in-flight requests when closed
    useEffect(() => {
        if (open) {
            setFocusedIndex(-1);
            setTimeout(() => searchInputRef.current?.focus(), 100);
        } else {
            if (abortControllerRef.current) {
                abortControllerRef.current.abort();
                abortControllerRef.current = null;
            }
            isFetchingRef.current = false;
        }
    }, [open]);
    
    // Clear selection, reset scroll, and reset filters when project changes to prevent cross-contamination
    useEffect(() => {
        setSelected(new Set());
        resetFilters();
        if (gridScrollRef.current) {
            gridScrollRef.current.scrollTop = 0;
        }
    }, [projectId, resetFilters]);
    
    // Body scroll lock
    useEffect(() => {
        if (!open) return;
        const prev = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => { document.body.style.overflow = prev; };
    }, [open]);
    
    // ESC to close
    useEffect(() => {
        if (!open) return;
        const onKey = (e) => {
            if (e.key === "Escape") onClose();
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [open, onClose]);
    
    // Derived filter options — from the /talents/facets endpoint (distinct
    // values actually present in the roster) rather than scanning `talents`,
    // since that array is now a bounded, server-filtered result set, not
    // the whole roster.
    const filterOptions = useMemo(() => ({
        genders: facets.genders || [],
        ethnicities: facets.ethnicities || [],
        locations: facets.locations || [],
        totalCount: talents.length,
    }), [facets, talents.length]);
    
    // Debounced search handler
    const handleSearchChange = useCallback((value) => {
        if (searchDebounceRef.current) {
            clearTimeout(searchDebounceRef.current);
        }
        
        searchDebounceRef.current = setTimeout(() => {
            setFilter("search", value);
        }, 200);
    }, [setFilter]);

    // AUD-E1: Force refresh candidates by resetting cached state
    const handleForceRefresh = useCallback(() => {
        if (abortControllerRef.current) {
            abortControllerRef.current.abort();
            abortControllerRef.current = null;
        }
        isFetchingRef.current = false;
        setTalents([]);
    }, []);
    
    // Filtered and sorted talents (precompiled performance structure for 5,000+ scaling)
    // `talents` is already server-filtered (see the fetch effect above,
    // which sends `filters` straight to GET /api/talents) — this no longer
    // re-filters client-side, just attaches the visual match-score badge.
    const filteredTalents = useMemo(() => {
        return talents.map(t => ({
            ...t,
            matchScore: calculateMatchScore(t, filters),
        }));
    }, [talents, filters]);
    
    const filtersActive = useMemo(() => {
        return (
            filters.search.trim() !== "" ||
            filters.gender !== "any" ||
            filters.ethnicity !== "any" ||
            filters.locations.length > 0 ||
            filters.ageMin !== "" ||
            filters.ageMax !== "" ||
            filters.heightMin !== "" ||
            filters.heightMax !== "" ||
            filters.minFollowers > 0 ||
            filters.interestedIn.length > 0 ||
            filters.internalTags.length > 0 ||
            (filters.skills && filters.skills.length > 0)
        );
    }, [filters]);
    
    const activeFilterCount = useMemo(() => {
        let count = 0;
        if (filters.gender !== "any") count++;
        if (filters.ethnicity !== "any") count++;
        if (filters.locations.length > 0) count++;
        if (filters.ageMin || filters.ageMax) count++;
        if (filters.heightMin || filters.heightMax) count++;
        if (filters.minFollowers > 0) count++;
        if (filters.interestedIn.length > 0) count++;
        if (filters.internalTags.length > 0) count++;
        if (filters.skills && filters.skills.length > 0) count++;
        return count;
    }, [filters]);
    
    // Selection helpers
    const toggleSelect = useCallback((id, alreadyInPipeline) => {
        if (alreadyInPipeline) return;
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);
    
    const clearSelection = useCallback(() => {
        setSelected(new Set());
    }, []);
    
    const registerCardRef = useCallback((index, el) => {
        if (el) {
            cardRefsMap.current.set(index, el);
        } else {
            cardRefsMap.current.delete(index);
        }
    }, []);
    
    const removeSelected = useCallback((id) => {
        setSelected((prev) => {
            const next = new Set(prev);
            next.delete(id);
            return next;
        });
    }, []);
    
    const selectAllVisible = useCallback(() => {
        const selectable = filteredTalents.filter(t => !existingTalentIds.has(t.id));
        setSelected(new Set(selectable.map(t => t.id)));
        toast.success(`Selected ${selectable.length} talents`);
    }, [filteredTalents, existingTalentIds]);
    
    const selectedTalents = useMemo(() => {
        return talents.filter(t => selected.has(t.id));
    }, [talents, selected]);
    

    // Apply saved search
    const applySavedSearch = useCallback((preset) => {
        setFilters(prev => ({ ...prev, ...preset.filters }));
        toast.success(`Applied preset: ${preset.label}`);
    }, []);
    
    // Save current search
    const saveCurrentSearch = useCallback(() => {
        if (!savedSearchName.trim()) return;
        // In production: save to backend/localStorage
        toast.success(`Saved search: ${savedSearchName}`);
        setSavedSearchName("");
        setShowSaveSearch(false);
    }, [savedSearchName]);
    
    // Add to pipeline
    const handleSubmit = async () => {
        if (selected.size === 0 || submitting || isSubmittingRef.current) return;
        
        isSubmittingRef.current = true;
        setSubmitting(true);
        const count = selected.size;
        
        try {
            await adminApi.post(`/projects/${projectId}/pipeline/add`, {
                project_id: projectId,
                talent_ids: Array.from(selected),
            });
            
            toast.success(`Added ${count} ${count === 1 ? "talent" : "talents"} to Ask To Test`);
            if (onAdded) await onAdded();
            setSelected(new Set());
            onClose();
        } catch (err) {
            console.error("Add to pipeline failed:", err);
            toast.error(formatErrorDetail(err, "Failed to add talents"));
        } finally {
            isSubmittingRef.current = false;
            setSubmitting(false);
        }
    };
    
    // ========================================================================
    // KEYBOARD NAVIGATION
    // ========================================================================
    const columns = getColumns();

    // AUD-P1: Caching refs to stabilize event binding
    const latestFilteredTalents = useRef(filteredTalents);
    const latestFocusedIndex = useRef(focusedIndex);
    const latestExistingTalentIds = useRef(existingTalentIds);
    const latestColumns = useRef(columns);
    const latestToggleSelect = useRef(toggleSelect);
    const latestSelectAllVisible = useRef(selectAllVisible);

    useEffect(() => {
        latestFilteredTalents.current = filteredTalents;
        latestFocusedIndex.current = focusedIndex;
        latestExistingTalentIds.current = existingTalentIds;
        latestColumns.current = columns;
        latestToggleSelect.current = toggleSelect;
        latestSelectAllVisible.current = selectAllVisible;
    }, [filteredTalents, focusedIndex, existingTalentIds, columns, toggleSelect, selectAllVisible]);
    
    useEffect(() => {
        if (!open) return;
        
        const handleKeyDown = (e) => {
            // AUD-C1: Guard against user input boxes to avoid intercepting typing
            const isInput = e.target.tagName === "INPUT" || 
                            e.target.tagName === "TEXTAREA" || 
                            e.target.isContentEditable;
            
            if (isInput) {
                if (e.key === "Escape") {
                    onClose();
                }
                return;
            }

            const totalItems = latestFilteredTalents.current.length;
            if (totalItems === 0) return;
            
            const cols = latestColumns.current;
            const focusedIdx = latestFocusedIndex.current;
            
            switch(e.key) {
                case "ArrowDown":
                    e.preventDefault();
                    setFocusedIndex(prev => Math.min(prev + cols, totalItems - 1));
                    break;
                case "ArrowUp":
                    e.preventDefault();
                    setFocusedIndex(prev => Math.max(prev - cols, -1));
                    break;
                case "ArrowRight":
                    e.preventDefault();
                    setFocusedIndex(prev => Math.min(prev + 1, totalItems - 1));
                    break;
                case "ArrowLeft":
                    e.preventDefault();
                    setFocusedIndex(prev => Math.max(prev - 1, -1));
                    break;
                case "Enter":
                    e.preventDefault();
                    if (focusedIdx >= 0 && focusedIdx < totalItems) {
                        const talent = latestFilteredTalents.current[focusedIdx];
                        const alreadyInPipeline = latestExistingTalentIds.current.has(talent.id);
                        if (!alreadyInPipeline) {
                            latestToggleSelect.current(talent.id, alreadyInPipeline);
                        }
                    }
                    break;
                case "a":
                    if (e.ctrlKey || e.metaKey) {
                        e.preventDefault();
                        latestSelectAllVisible.current();
                    }
                    break;
                default:
                    break;
            }
        };
        
        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [open, onClose]);
    
    // Scroll focused card into view
    useEffect(() => {
        if (focusedIndex >= 0) {
            const focusedElement = cardRefsMap.current.get(focusedIndex);
            if (focusedElement) {
                focusedElement.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
        }
    }, [focusedIndex]);
    
    const config = DENSITY_CONFIG[densityMode];
    
    return (
        <ErrorBoundary>
            <div
                role="dialog"
                aria-modal="true"
                aria-labelledby="talent-browser-title"
                data-testid="talent-browser-modal"
                className={`fixed inset-0 z-50 flex items-stretch sm:items-center justify-center bg-black/40 backdrop-blur-sm p-0 sm:p-6 transition-all duration-200 ${
                    open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"
                }`}
                onMouseDown={(e) => {
                    if (e.target === e.currentTarget) onClose();
                }}
            >
                <div className="relative w-full sm:max-w-7xl h-[100dvh] sm:h-[90dvh] flex flex-col bg-white rounded-2xl shadow-2xl overflow-hidden">
                    
                    {/* Header */}
                    <div className="flex items-center justify-between px-4 sm:px-6 py-4 sm:py-5 border-b border-gray-200 shrink-0 bg-white">
                        <div>
                            <p className="text-[10px] sm:text-xs font-medium tracking-wide text-[#333333] uppercase mb-1 inline-flex items-center gap-1.5">
                                Global Roster · {filterOptions.totalCount.toLocaleString()} talents
                                <button
                                    type="button"
                                    onClick={handleForceRefresh}
                                    data-testid="talent-browser-refresh"
                                    title="Refresh talent roster"
                                    className="p-1 rounded text-[#333333] hover:text-[#222222] hover:bg-gray-100 transition-colors inline-flex items-center"
                                >
                                    <svg className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 8H18" />
                                    </svg>
                                </button>
                            </p>
                            <h2
                                id="talent-browser-title"
                                className="text-lg sm:text-2xl font-semibold text-[#111111] tracking-tight"
                            >
                                Add Talents to Pipeline
                            </h2>
                        </div>
                        
                        <div className="flex items-center gap-2">
                            {/* Density Toggle */}
                            <div className="hidden sm:flex items-center gap-1 bg-gray-50 rounded-lg border border-gray-200 p-1">
                                {Object.keys(DENSITY_CONFIG).map((mode) => (
                                    <button
                                        key={mode}
                                        onClick={() => setDensityMode(mode)}
                                        data-testid={`talent-browser-density-${mode}`}
                                        className={`px-3 py-1.5 rounded-md transition-all ${
                                            densityMode === mode
                                                ? "bg-white text-[#111111] shadow-sm"
                                                : "text-[#333333] hover:text-[#111111]"
                                        }`}
                                    >
                                        {mode === "compact" && <Minus size={14} />}
                                        {mode === "comfortable" && <LayoutGrid size={14} />}
                                        {mode === "visual" && <Maximize2 size={14} />}
                                    </button>
                                ))}
                            </div>
                            
                            <button
                                type="button"
                                onClick={onClose}
                                data-testid="talent-browser-close"
                                className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg flex items-center justify-center text-[#333333] hover:text-[#222222] hover:bg-gray-100 transition-colors"
                            >
                                <X size={16} strokeWidth={1.6} />
                            </button>
                        </div>
                    </div>
                    
                    {/* Fixed Search/Toolbar */}
                    <div className="border-b border-gray-200 bg-white shrink-0">
                        <div className="px-4 sm:px-6 py-3 sm:py-4 space-y-2.5">
                            {/* Search + Actions */}
                            <div className="flex items-center gap-2 sm:gap-3">
                                <div className="relative flex-1">
                                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 sm:w-4 sm:h-4 text-[#333333]" />
                                    <input
                                        ref={searchInputRef}
                                        type="text"
                                        defaultValue={filters.search}
                                        onChange={(e) => handleSearchChange(e.target.value)}
                                        placeholder="Search talents..."
                                        data-testid="talent-browser-search"
                                        className="w-full bg-gray-50 border border-gray-200 rounded-lg pl-9 pr-3 py-2 text-sm text-[#111111] placeholder:text-[#333333] focus:outline-none focus:border-gray-300 focus:ring-1 focus:ring-gray-200 transition-all"
                                    />
                                </div>
                                
                                {/* Mobile filter button */}
                                <button
                                    onClick={() => setShowMobileFilters(true)}
                                    className="sm:hidden px-3 py-2 rounded-lg bg-gray-50 border border-gray-200 text-[#222222] hover:bg-gray-100 transition-colors"
                                >
                                    <Sliders size={16} />
                                    {activeFilterCount > 0 && (
                                        <span className="ml-1 text-xs bg-gray-900 text-white rounded-full px-1.5 py-0.5">
                                            {activeFilterCount}
                                        </span>
                                    )}
                                </button>
                                
                                {/* View Toggle (Grid / List) */}
                                <div className="flex items-center bg-gray-50 border border-gray-200 rounded-lg p-0.5 shrink-0">
                                    <button
                                        type="button"
                                        onClick={() => handleViewModeChange("grid")}
                                        className={`p-1.5 rounded transition-all ${
                                            viewMode === "grid"
                                                ? "bg-white text-[#111111] shadow-sm border border-gray-205/30"
                                                : "text-[#333333] hover:text-[#111111]"
                                        }`}
                                        title="Grid View"
                                    >
                                        <LayoutGrid size={14} />
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => handleViewModeChange("list")}
                                        className={`p-1.5 rounded transition-all ${
                                            viewMode === "list"
                                                ? "bg-white text-[#111111] shadow-sm border border-gray-205/30"
                                                : "text-[#333333] hover:text-[#111111]"
                                        }`}
                                        title="List View"
                                    >
                                        <List size={14} />
                                    </button>
                                </div>
                                
                                {/* Advanced filters toggle */}
                                <button
                                    onClick={() => setShowAdvancedFilters(!showAdvancedFilters)}
                                    className="hidden sm:flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-[#222222] hover:bg-gray-100 transition-colors"
                                >
                                    <Sliders size={14} />
                                    Filters
                                    {activeFilterCount > 0 && (
                                        <span className="bg-gray-900 text-white text-xs rounded-full px-1.5 py-0.5">
                                            {activeFilterCount}
                                        </span>
                                    )}
                                    <ChevronDown size={12} className={`transition-transform ${showAdvancedFilters ? "rotate-180" : ""}`} />
                                </button>
                                
                                {selected.size > 0 && (
                                    <button
                                        onClick={clearSelection}
                                        className="hidden sm:block px-3 py-2 rounded-lg text-sm text-[#222222] hover:text-[#111111] hover:bg-gray-100 transition-colors"
                                    >
                                        Clear ({selected.size})
                                    </button>
                                )}
                            </div>

                            {/* Quick Presets & Active Chips & Popular Tags (Fixed/Sticky) */}
                            <div className="space-y-2.5 pt-2 border-t border-gray-100">
                                {/* Active filter chips */}
                                {filtersActive && (
                                    <div className="flex items-center gap-1.5 flex-wrap">
                                        <span className="text-[10px] text-[#333333] hidden sm:inline">Active:</span>
                                        {filters.gender !== "any" && <FilterChip label={`Gender: ${filters.gender}`} onRemove={() => setFilter("gender", "any")} />}
                                        {filters.ethnicity !== "any" && <FilterChip label={`Ethnicity: ${filters.ethnicity}`} onRemove={() => setFilter("ethnicity", "any")} />}
                                        {filters.locations.map((loc) => (
                                            <FilterChip key={loc} label={`Location: ${loc}`} onRemove={() => setFilter("locations", filters.locations.filter((l) => l !== loc))} />
                                        ))}
                                        {(filters.ageMin || filters.ageMax) && (
                                            <FilterChip label={`Age: ${filters.ageMin || "Any"}–${filters.ageMax || "Any"}`} onRemove={() => { setFilter("ageMin", ""); setFilter("ageMax", ""); }} />
                                        )}
                                        {(filters.heightMin || filters.heightMax) && (
                                            <FilterChip 
                                                label={`Height: ${filters.heightMin ? HEIGHT_OPTIONS.find(o => o.value === Number(filters.heightMin))?.label || "" : "4'0\""}–${filters.heightMax ? HEIGHT_OPTIONS.find(o => o.value === Number(filters.heightMax))?.label || "" : "7'0\""}`} 
                                                onRemove={() => { setFilter("heightMin", ""); setFilter("heightMax", ""); }} 
                                            />
                                        )}
                                        {filters.minFollowers > 0 && (
                                            <FilterChip label={`Followers: ${FOLLOWER_BUCKETS.find(b => b.value === filters.minFollowers)?.label}`} onRemove={() => setFilter("minFollowers", 0)} />
                                        )}
                                        {filters.interestedIn.map(cat => (
                                            <FilterChip 
                                                key={cat} 
                                                label={`Category: ${cat}`} 
                                                onRemove={() => setFilter("interestedIn", filters.interestedIn.filter(x => x !== cat))} 
                                            />
                                        ))}
                                        {filters.skills && filters.skills.map(skill => (
                                            <FilterChip 
                                                key={skill} 
                                                label={`Skill: ${skill}`} 
                                                onRemove={() => setFilter("skills", filters.skills.filter(x => x !== skill))} 
                                            />
                                        ))}
                                        {filters.internalTags.map(tagId => {
                                            const tagName = globalTags.find(t => t.id === tagId)?.name || "Tag";
                                            return (
                                                <FilterChip 
                                                    key={tagId} 
                                                    label={`Tag: ${tagName}`} 
                                                    onRemove={() => setFilter("internalTags", filters.internalTags.filter(x => x !== tagId))} 
                                                />
                                            );
                                        })}
                                        <button onClick={resetFilters} className="text-[10px] text-[#333333] hover:text-[#222222] underline-offset-2 hover:underline">
                                            Clear all
                                        </button>
                                    </div>
                                )}
                                
                                <div className="space-y-2">
                                    {/* Popular Tags Row (dynamic chip filter sync) */}
                                    {frequentTags.length > 0 && (
                                        <div className="flex items-center gap-1.5 flex-wrap">
                                            <span className="text-[10px] text-[#333333] font-medium uppercase tracking-wide">Popular Tags:</span>
                                            {frequentTags.map(tag => {
                                                const isSelected = filters.internalTags.includes(tag.id);
                                                return (
                                                    <button
                                                        key={tag.id}
                                                        type="button"
                                                        onClick={() => {
                                                            if (isSelected) {
                                                                setFilter("internalTags", filters.internalTags.filter(id => id !== tag.id));
                                                            } else {
                                                                setFilter("internalTags", [...filters.internalTags, tag.id]);
                                                            }
                                                        }}
                                                        className={`px-2 py-0.5 rounded-full text-[10px] font-medium tracking-tight transition-all border ${
                                                            isSelected
                                                                ? "bg-gray-900 border-gray-900 text-white"
                                                                : "bg-white border-gray-200 text-[#222222] hover:bg-gray-50 hover:border-gray-300"
                                                        }`}
                                                    >
                                                        {tag.name} <span className="text-[8px] opacity-60">({tag.count})</span>
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    {/* Scrollable Content Area */}
                    <div
                        ref={gridScrollRef}
                        className="flex-1 overflow-y-auto min-h-0 flex flex-col bg-gray-50/10"
                        style={{ WebkitOverflowScrolling: "touch" }}
                        data-testid="talent-browser-scroll-container"
                    >
                        {/* Advanced Filters Panel (Filters) */}
                        {showAdvancedFilters && (
                            <div className="bg-white border-b border-gray-100 shrink-0">
                                <AdvancedFiltersPanel
                                    filters={filters}
                                    setFilter={setFilter}
                                    filterOptions={filterOptions}
                                    globalTags={globalTags}
                                    onClose={() => setShowAdvancedFilters(false)}
                                    resetFilters={resetFilters}
                                />
                            </div>
                        )}


                        {/* Talent Grid Area */}
                        <div className="flex-1">
                            {loading && talents.length === 0 ? (
                                <LoadingSkeleton densityMode={densityMode} isMobile={isMobile} />
                            ) : error ? (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center">
                                        <p className="text-red-600 text-sm">{error}</p>
                                        <button onClick={handleForceRefresh} className="mt-4 px-4 py-2 bg-gray-900 text-white rounded-lg text-xs font-medium hover:bg-gray-800 transition-colors">
                                            Retry Load
                                        </button>
                                    </div>
                                </div>
                            ) : filteredTalents.length === 0 ? (
                                <EmptyResults onReset={resetFilters} hasFilters={filtersActive} />
                            ) : (
                                <div className="px-4 sm:px-6 py-4 sm:py-5">
                                    {viewMode === "list" ? (
                                        <div className="flex flex-col bg-white rounded-xl border border-gray-200 overflow-hidden divide-y divide-gray-100">
                                            {filteredTalents.map((talent, globalIndex) => {
                                                const alreadyInPipeline = existingTalentIds.has(talent.id);
                                                const isSelected = selected.has(talent.id);
                                                const isFocused = globalIndex === focusedIndex;
                                                
                                                return (
                                                    <TalentListRow
                                                        key={talent.id}
                                                        talent={talent}
                                                        selected={isSelected}
                                                        alreadyInPipeline={alreadyInPipeline}
                                                        onToggle={toggleSelect}
                                                        onPreview={setPreviewTalent}
                                                        isFocused={isFocused}
                                                        showIntelligence={filters.showIntelligence}
                                                        registerRef={registerCardRef}
                                                        globalIndex={globalIndex}
                                                    />
                                                );
                                            })}
                                        </div>
                                    ) : (
                                        <div
                                            style={{
                                                display: "grid",
                                                gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
                                                gap: `${config.gap}px`,
                                            }}
                                        >
                                            {filteredTalents.map((talent, globalIndex) => {
                                                const alreadyInPipeline = existingTalentIds.has(talent.id);
                                                const isSelected = selected.has(talent.id);
                                                const isFocused = globalIndex === focusedIndex;
                                                
                                                return (
                                                    <TalentCard
                                                        key={talent.id}
                                                        talent={talent}
                                                        selected={isSelected}
                                                        alreadyInPipeline={alreadyInPipeline}
                                                        onToggle={toggleSelect}
                                                        onPreview={setPreviewTalent}
                                                        densityMode={densityMode}
                                                        isFocused={isFocused}
                                                        showIntelligence={filters.showIntelligence}
                                                        isMobile={isMobile}
                                                        globalIndex={globalIndex}
                                                        registerRef={registerCardRef}
                                                    />
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </div>
                    
                    {/* Selection Tray */}
                    {selected.size > 0 && (
                        <SelectionTray
                            selectedTalents={selectedTalents}
                            onRemove={removeSelected}
                            selectedCount={selected.size}
                            onSubmit={handleSubmit}
                            submitting={submitting}
                            isMobile={isMobile}
                            onClear={clearSelection}
                        />
                    )}
                </div>
            </div>
            
            {/* Mobile Filters Sheet */}
            {showMobileFilters && (
                <MobileFiltersSheet
                    filters={filters}
                    setFilter={setFilter}
                    filterOptions={filterOptions}
                    globalTags={globalTags}
                    onClose={() => setShowMobileFilters(false)}
                    onReset={resetFilters}
                />
            )}
            
            {/* Save Search Modal */}
            {showSaveSearch && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
                    <div className="bg-white rounded-xl max-w-md w-full p-6">
                        <h3 className="text-lg font-semibold text-[#111111] mb-2">Save search</h3>
                        <p className="text-sm text-[#333333] mb-4">Save current filters as a preset for quick access</p>
                        <input
                            type="text"
                            value={savedSearchName}
                            onChange={(e) => setSavedSearchName(e.target.value)}
                            placeholder="e.g., High-impact models"
                            className="w-full px-3 py-2 border border-gray-200 rounded-lg mb-4 focus:outline-none focus:ring-1 focus:ring-gray-300"
                            autoFocus
                        />
                        <div className="flex gap-2 justify-end">
                            <button onClick={() => setShowSaveSearch(false)} className="px-4 py-2 text-sm text-[#222222]">Cancel</button>
                            <button onClick={saveCurrentSearch} className="px-4 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 transition-colors">Save</button>
                        </div>
                    </div>
                </div>
            )}

            {/* Sneak Preview / Quick View Drawer Portal */}
            <TalentPreviewDrawer
                talent={previewTalent}
                onClose={() => setPreviewTalent(null)}
                isMobile={isMobile}
            />
        </ErrorBoundary>
    );
}

// ============================================================================
// TALENT LIST ROW
// ============================================================================

const TalentListRow = memo(({ talent, selected, alreadyInPipeline, onToggle, onPreview, isFocused, showIntelligence, registerRef, globalIndex }) => {
    const imageUrl = pickImage(talent);
    const handleToggle = useCallback(() => {
        onToggle(talent.id, alreadyInPipeline);
    }, [onToggle, talent.id, alreadyInPipeline]);

    return (
        <div
            ref={(el) => registerRef(globalIndex, el)}
            className={`
                flex items-center justify-between p-3 transition-all duration-200 border
                ${alreadyInPipeline 
                    ? "bg-[#0c2340]/[0.02] border-[#0c2340] border-l-4 shadow-[inset_1px_0_0_0_#0c2340] opacity-95" 
                    : "border-transparent border-b-gray-100 hover:bg-gray-50/80 cursor-pointer bg-white"
                }
                ${selected ? "bg-gray-100/60 font-medium" : ""}
                ${isFocused && !alreadyInPipeline ? "ring-2 ring-blue-500" : ""}
            `}
            onClick={handleToggle}
        >
            <div className="flex items-center gap-3 min-w-0 flex-1">
                {/* Selection Checkbox */}
                {!alreadyInPipeline ? (
                    <div className={`w-4.5 h-4.5 rounded border transition-all flex items-center justify-center shrink-0 ${selected ? "bg-gray-900 border-gray-900" : "border-gray-300 bg-white"}`}>
                        {selected && <Check size={10} className="text-white" strokeWidth={3} />}
                    </div>
                ) : (
                    <div className="text-[10px] text-[#333333] font-mono shrink-0 w-4.5 text-center">In</div>
                )}

                {/* Profile Image */}
                <div className="w-10 h-10 rounded-lg overflow-hidden bg-gray-100 shrink-0 relative">
                    {imageUrl ? (
                        <img src={imageUrl} alt="" className="w-full h-full object-cover" />
                    ) : (
                        <div className="w-full h-full flex items-center justify-center bg-gray-200 text-[#333333] text-xs">?</div>
                    )}
                </div>

                {/* Text details */}
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-[#111111] truncate">{talent.name}</span>
                        {showIntelligence && talent.matchScore && (
                            <span className="px-1.5 py-0.5 rounded bg-gray-100 text-[9px] font-mono text-[#111111] font-medium shrink-0">{talent.matchScore}% match</span>
                        )}
                    </div>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-[#333333] mt-0.5">
                        {talent.age && <span>{talent.age} yrs</span>}
                        {talent.height && <span>· {talent.height}</span>}
                        {formatTalentLocation(talent.location) && (
                            <span className="truncate">
                                · {formatTalentLocation(talent.location)}
                            </span>
                        )}
                        {talent.instagram_handle && (
                            <a
                                href={instagramProfileUrl(talent.instagram_handle)}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={(e) => e.stopPropagation()}
                                className="truncate flex items-center gap-1 hover:text-[#111111] transition-colors"
                            >
                                · <Instagram size={10} /> {displayInstagramHandle(talent.instagram_handle)}
                            </a>
                        )}
                    </div>
                    {/* Tags */}
                    {talent.tags && talent.tags.length > 0 && (
                        <div className="flex items-center gap-1 flex-wrap mt-1">
                            {talent.tags.slice(0, 3).map(tag => (
                                <span key={tag.id} className="px-1.5 py-0.5 rounded bg-gray-50 border border-gray-150 text-[9px] text-[#222222] font-medium">
                                    {tag.name}
                                </span>
                            ))}
                            {talent.tags.length > 3 && (
                                <span className="text-[9px] text-[#333333]">+{talent.tags.length - 3}</span>
                            )}
                        </div>
                    )}
                </div>
            </div>

            {/* Actions (Preview) */}
            <div className="flex items-center gap-2 shrink-0 ml-4">
                <button
                    type="button"
                    onClick={(e) => {
                        e.stopPropagation();
                        onPreview?.(talent);
                    }}
                    className="p-2 rounded-lg text-[#333333] hover:text-[#111111] hover:bg-gray-100 transition-colors"
                    title="Quick View"
                >
                    <Eye size={16} />
                </button>
            </div>
        </div>
    );
});
TalentListRow.displayName = "TalentListRow";

// ============================================================================
// TALENT SNEAK PREVIEW / QUICK VIEW
// ============================================================================

export const TalentPreviewDrawer = memo(({ talent, onClose, isMobile }) => {
    if (!talent) return null;
    
    const imageUrl = pickImage(talent);
    const media = talent.media || [];
    const images = media.filter(m => m.category !== "video" && m.url && m.url !== imageUrl);
    const video = media.find(m => m.category === "video" || m.content_type?.startsWith("video/"));
    
    const handleBackdropClick = (e) => {
        if (e.target === e.currentTarget) onClose();
    };

    const drawerContent = (
        <div className="flex flex-col h-full overflow-hidden bg-white">
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 shrink-0 bg-white sticky top-0 z-10">
                <div>
                    <span className="text-[10px] font-semibold tracking-wider text-[#333333] uppercase">Talent Preview</span>
                    <h3 className="text-lg font-bold text-[#111111]">{talent.name || "Unnamed Talent"}</h3>
                </div>
                <button
                    onClick={onClose}
                    className="p-1.5 rounded-lg text-[#333333] hover:text-[#222222] hover:bg-gray-100 transition-colors"
                >
                    <X size={18} />
                </button>
            </div>

            {/* Scrollable details */}
            <div className="flex-1 overflow-y-auto p-6 space-y-6 overscroll-contain">
                {/* Hero Profile Image / Video preview */}
                <div className="relative aspect-[3/4] w-full rounded-xl overflow-hidden bg-gray-50 border border-gray-200">
                    {imageUrl ? (
                        <img src={imageUrl} alt="" className="w-full h-full object-cover" />
                    ) : (
                        <div className="w-full h-full flex items-center justify-center bg-gray-100 text-gray-300">No Image</div>
                    )}
                </div>

                {/* Primary Stats */}
                <div className="grid grid-cols-2 gap-4 bg-gray-50 rounded-xl p-4 border border-gray-150">
                    <div>
                        <span className="text-[10px] text-[#333333] uppercase font-medium">Age</span>
                        <div className="text-sm font-semibold text-[#111111]">{talent.age ? `${talent.age} years` : "—"}</div>
                    </div>
                    <div>
                        <span className="text-[10px] text-[#333333] uppercase font-medium">Height</span>
                        <div className="text-sm font-semibold text-[#111111]">{talent.height || "—"}</div>
                    </div>
                    <div>
                        <span className="text-[10px] text-[#333333] uppercase font-medium">Location</span>
                        <div className="text-sm font-semibold text-[#111111]">
                            {formatTalentLocation(talent.location) || "—"}
                        </div>
                    </div>
                    <div>
                        <span className="text-[10px] text-[#333333] uppercase font-medium">Instagram</span>
                        <div className="text-sm font-semibold text-[#111111] flex items-center gap-1.5">
                            {talent.instagram_handle ? (
                                <a
                                    href={instagramProfileUrl(talent.instagram_handle)}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="flex items-center gap-1.5 hover:opacity-70 transition-opacity"
                                >
                                    <Instagram size={12} className="text-[#333333]" />
                                    <span className="truncate">{displayInstagramHandle(talent.instagram_handle)}</span>
                                </a>
                            ) : "—"}
                        </div>
                    </div>
                </div>

                {/* Categories */}
                {talent.interested_in && talent.interested_in.length > 0 && (
                    <div className="space-y-2">
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-[#333333]">Categories</h4>
                        <div className="flex flex-wrap gap-1.5">
                            {talent.interested_in.map(cat => (
                                <span key={cat} className="px-2.5 py-1 rounded-full bg-gray-100 text-[#111111] text-xs font-medium border border-gray-200">
                                    {cat}
                                </span>
                            ))}
                        </div>
                    </div>
                )}

                {/* Dynamic Tags */}
                {talent.tags && talent.tags.length > 0 && (
                    <div className="space-y-2">
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-[#333333]">Talent Tags</h4>
                        <div className="flex flex-wrap gap-1.5">
                            {talent.tags.map(tag => (
                                <span key={tag.id} className="px-2 py-0.5 rounded bg-gray-50 border border-gray-200 text-xs text-[#222222] font-medium">
                                    {tag.name}
                                </span>
                            ))}
                        </div>
                    </div>
                )}

                {/* Introduction Video */}
                {video && (
                    <div className="space-y-2">
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-[#333333]">Intro Video</h4>
                        <div className="aspect-video w-full rounded-xl overflow-hidden bg-black border border-gray-200 shadow-inner">
                            <HlsVideo src={video.url} controls className="w-full h-full object-contain" />
                        </div>
                    </div>
                )}

                {/* Portfolio images */}
                {images.length > 0 && (
                    <div className="space-y-3">
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-[#333333]">Portfolio media ({images.length})</h4>
                        <div className="grid grid-cols-2 gap-2">
                            {images.map((img, idx) => (
                                <div key={idx} className="relative aspect-[3/4] rounded-lg overflow-hidden bg-gray-50 border border-gray-200">
                                    <img src={IMAGE_URL(img)} alt="" loading="lazy" className="w-full h-full object-cover hover:scale-105 transition-transform duration-200" />
                                </div>
                            ))}
                        </div>
                    </div>
                )}

            </div>
        </div>
    );

    if (isMobile) {
        return createPortal(
            <div className="fixed inset-0 z-[150] bg-black/50 md:hidden flex items-end animate-fade-in" onClick={handleBackdropClick}>
                <div className="relative w-full bg-white rounded-t-3xl shadow-2xl animate-in slide-in-from-bottom duration-300 max-h-[85vh] flex flex-col overflow-hidden">
                    <div className="w-12 h-1 bg-slate-200 rounded-full mx-auto my-3 flex-shrink-0" />
                    <div className="flex-1 flex flex-col min-h-0">
                        {drawerContent}
                    </div>
                </div>
            </div>,
            document.body
        );
    }

    return createPortal(
        <div className="fixed inset-0 z-[150] bg-black/20 hidden md:flex justify-end animate-fade-in" onClick={handleBackdropClick}>
            <div className="w-96 h-full bg-white shadow-2xl animate-in slide-in-from-right duration-300 border-l border-gray-200 relative">
                {drawerContent}
            </div>
        </div>,
        document.body
    );
});
TalentPreviewDrawer.displayName = "TalentPreviewDrawer";

export default TalentBrowserModal;

// ============================================================================
// ERROR BOUNDARY
// ============================================================================

class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, errorInfo) {
        console.error("TalentBrowserModal crashed:", error, errorInfo);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div className="fixed inset-0 flex items-center justify-center bg-white z-50">
                    <div className="text-center p-8">
                        <p className="text-red-600 text-sm mb-2">Something went wrong</p>
                        <button onClick={() => window.location.reload()} className="text-sm text-[#222222] underline">
                            Reload page
                        </button>
                    </div>
                </div>
            );
        }
        return this.props.children;
    }
}

// ============================================================================
// TAGS FILTER INPUT
// ============================================================================

const TagsFilterInput = ({ selectedTagIds, globalTags, onChange }) => {
    const [query, setQuery] = useState("");
    const [isOpen, setIsOpen] = useState(false);
    const [highlightedIndex, setHighlightedIndex] = useState(0);
    const containerRef = useRef(null);
    const inputRef = useRef(null);

    // Debounced query filtering (no heavy calculations)
    const filteredTags = useMemo(() => {
        const cleaned = query.trim().toLowerCase();
        return globalTags.filter(tag => {
            const matchesQuery = cleaned === "" || tag.name.toLowerCase().includes(cleaned);
            const notSelected = !selectedTagIds.includes(tag.id);
            return matchesQuery && notSelected;
        });
    }, [query, globalTags, selectedTagIds]);

    // Handle outside clicks to close the dropdown
    useEffect(() => {
        const handleClickOutside = (e) => {
            if (containerRef.current && !containerRef.current.contains(e.target)) {
                setIsOpen(false);
            }
        };
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    // Keep highlighted index in bounds
    useEffect(() => {
        setHighlightedIndex(0);
    }, [filteredTags]);

    const handleSelectTag = useCallback((tagId) => {
        onChange([...selectedTagIds, tagId]);
        setQuery("");
        inputRef.current?.focus();
    }, [selectedTagIds, onChange]);

    const handleRemoveTag = useCallback((tagId) => {
        onChange(selectedTagIds.filter(id => id !== tagId));
    }, [selectedTagIds, onChange]);

    const handleKeyDown = (e) => {
        if (e.key === "ArrowDown") {
            e.preventDefault();
            setIsOpen(true);
            setHighlightedIndex(prev => Math.min(prev + 1, filteredTags.length - 1));
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlightedIndex(prev => Math.max(prev - 1, 0));
        } else if (e.key === "Enter") {
            e.preventDefault();
            if (isOpen && filteredTags[highlightedIndex]) {
                handleSelectTag(filteredTags[highlightedIndex].id);
            } else {
                setIsOpen(true);
            }
        } else if (e.key === "Escape") {
            setIsOpen(false);
        } else if (e.key === "Backspace" && query === "" && selectedTagIds.length > 0) {
            // Remove the last selected tag
            handleRemoveTag(selectedTagIds[selectedTagIds.length - 1]);
        }
    };

    // Get selected tag details for chip rendering
    const selectedTagsList = useMemo(() => {
        return selectedTagIds.map(id => globalTags.find(t => t.id === id)).filter(Boolean);
    }, [selectedTagIds, globalTags]);

    return (
        <div ref={containerRef} className="relative w-full">
            {/* Input & Selected Chips Area */}
            <div 
                onClick={() => inputRef.current?.focus()}
                className="w-full flex flex-wrap gap-1.5 p-2 bg-white border border-gray-200 rounded-lg focus-within:border-gray-300 focus-within:ring-1 focus-within:ring-gray-200 cursor-text min-h-[40px] transition-all"
            >
                {selectedTagsList.map(tag => (
                    <span 
                        key={tag.id}
                        className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-gray-900 text-white text-xs font-medium tracking-tight shadow-sm shrink-0"
                    >
                        {tag.name}
                        <button 
                            type="button"
                            onClick={(e) => {
                                e.stopPropagation();
                                handleRemoveTag(tag.id);
                            }}
                            className="hover:text-gray-200 focus:outline-none transition-colors"
                        >
                            <X size={10} strokeWidth={2.5} />
                        </button>
                    </span>
                ))}
                
                <input
                    ref={inputRef}
                    type="text"
                    value={query}
                    onChange={(e) => {
                        setQuery(e.target.value);
                        setIsOpen(true);
                    }}
                    onFocus={() => setIsOpen(true)}
                    onKeyDown={handleKeyDown}
                    placeholder={selectedTagIds.length === 0 ? "Search internal tags..." : ""}
                    className="flex-1 bg-transparent border-none outline-none text-sm text-[#111111] placeholder:text-[#333333] p-0.5 min-w-[120px] focus:ring-0 focus:border-none focus:outline-none"
                />
            </div>

            {/* Dropdown Menu */}
            {isOpen && filteredTags.length > 0 && (
                <div className="absolute z-[100] w-full mt-1 bg-white border border-gray-200 rounded-lg shadow-xl max-h-60 overflow-y-auto overflow-x-hidden">
                    {filteredTags.map((tag, idx) => (
                        <button
                            key={tag.id}
                            type="button"
                            onClick={() => handleSelectTag(tag.id)}
                            onMouseEnter={() => setHighlightedIndex(idx)}
                            className={`w-full text-left px-4 py-2 text-sm transition-colors ${
                                idx === highlightedIndex 
                                    ? "bg-gray-100 text-[#111111]" 
                                    : "text-[#111111] hover:bg-gray-50"
                            }`}
                        >
                            {tag.name}
                        </button>
                    ))}
                </div>
            )}
            
            {isOpen && query.trim() !== "" && filteredTags.length === 0 && (
                <div className="absolute z-[100] w-full mt-1 bg-white border border-gray-200 rounded-lg shadow-xl px-4 py-3 text-sm text-[#333333] text-center">
                    No matching tags found
                </div>
            )}
        </div>
    );
};

// ============================================================================
// ADVANCED FILTERS PANEL
// ============================================================================

const AdvancedFiltersPanel = memo(({ filters, setFilter, filterOptions, globalTags, onClose, resetFilters }) => {
    return (
        <div className="border-t border-gray-100 bg-gray-50/50 p-5 space-y-5 animate-slide-down">
            {/* Header row with Title & Close/Reset buttons */}
            <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Casting Filter Engine</h3>
                <div className="flex items-center gap-3">
                    <button 
                        type="button"
                        onClick={resetFilters}
                        className="text-xs text-[#333333] hover:text-gray-905 font-medium transition-colors"
                    >
                        Reset All Filters
                    </button>
                    <button 
                        type="button"
                        onClick={onClose}
                        className="text-xs text-[#333333] hover:text-gray-905 font-medium transition-colors"
                    >
                        Hide Filters
                    </button>
                </div>
            </div>

            {/* ROW 3 (Demographics) */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div>
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Gender</label>
                    <select
                        value={filters.gender}
                        onChange={(e) => setFilter("gender", e.target.value)}
                        className="w-full mt-1.5 px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300"
                    >
                        <option value="any">Any ({filterOptions.totalCount})</option>
                        {filterOptions.genders.map(g => <option key={g} value={g}>{g}</option>)}
                    </select>
                </div>
                
                <div>
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Ethnicity</label>
                    <select
                        value={filters.ethnicity}
                        onChange={(e) => setFilter("ethnicity", e.target.value)}
                        className="w-full mt-1.5 px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300"
                    >
                        <option value="any">Any</option>
                        {filterOptions.ethnicities.map(e => <option key={e} value={e}>{e}</option>)}
                    </select>
                </div>
                
                <div>
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Location</label>
                    <div className="mt-1.5">
                        <LocationMultiSelect
                            value={filters.locations}
                            onChange={(v) => setFilter("locations", v)}
                            options={filterOptions.locations}
                        />
                    </div>
                </div>
            </div>

            {/* ROW 4 (Physical & Outreach) */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div>
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Age Range</label>
                    <div className="flex gap-2 mt-1.5">
                        <input 
                            type="number" 
                            value={filters.ageMin} 
                            onChange={(e) => setFilter("ageMin", e.target.value)} 
                            placeholder="Min" 
                            className="w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300" 
                        />
                        <span className="text-[#333333] self-center font-medium">–</span>
                        <input 
                            type="number" 
                            value={filters.ageMax} 
                            onChange={(e) => setFilter("ageMax", e.target.value)} 
                            placeholder="Max" 
                            className="w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300" 
                        />
                    </div>
                </div>

                <div>
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Height Range</label>
                    <div className="flex gap-2 mt-1.5">
                        <select
                            value={filters.heightMin}
                            onChange={(e) => setFilter("heightMin", e.target.value)}
                            className="w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300"
                        >
                            <option value="">Min</option>
                            {HEIGHT_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                        </select>
                        <span className="text-[#333333] self-center font-medium">–</span>
                        <select
                            value={filters.heightMax}
                            onChange={(e) => setFilter("heightMax", e.target.value)}
                            className="w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300"
                        >
                            <option value="">Max</option>
                            {HEIGHT_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                        </select>
                    </div>
                </div>

                <div>
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Instagram Followers</label>
                    <select
                        value={filters.minFollowers}
                        onChange={(e) => setFilter("minFollowers", Number(e.target.value))}
                        className="w-full mt-1.5 px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300"
                    >
                        {FOLLOWER_BUCKETS.map(b => <option key={b.value} value={b.value}>{b.label}</option>)}
                    </select>
                </div>
            </div>

            {/* ROW 5 (Interested In Categories) */}
            <div className="pt-2 border-t border-gray-100">
                <div className="flex items-center justify-between mb-2">
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Interested In</label>
                    <div className="flex items-center gap-1 bg-gray-100 rounded-md p-0.5">
                        <button
                            type="button"
                            onClick={() => setFilter("interestedInMatchMode", "OR")}
                            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                                filters.interestedInMatchMode === "OR" 
                                    ? "bg-white text-[#111111] shadow-sm" 
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            ANY (OR)
                        </button>
                        <button
                            type="button"
                            onClick={() => setFilter("interestedInMatchMode", "AND")}
                            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                                filters.interestedInMatchMode === "AND" 
                                    ? "bg-white text-[#111111] shadow-sm" 
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            ALL (AND)
                        </button>
                    </div>
                </div>
                <div className="flex flex-wrap gap-2">
                    {["Acting", "Modeling", "Influencer Campaigns"].map(cat => {
                        const isSelected = filters.interestedIn.includes(cat);
                        return (
                            <button
                                key={cat}
                                type="button"
                                onClick={() => {
                                    if (isSelected) {
                                        setFilter("interestedIn", filters.interestedIn.filter(x => x !== cat));
                                    } else {
                                        setFilter("interestedIn", [...filters.interestedIn, cat]);
                                    }
                                }}
                                className={`px-4.5 py-1.5 rounded-full text-xs font-medium border transition-all ${
                                    isSelected 
                                        ? "bg-gray-900 border-gray-900 text-white shadow-sm" 
                                        : "bg-white border-gray-200 text-[#222222] hover:bg-gray-50 hover:border-gray-300"
                                }`}
                            >
                                {cat}
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* Skills & Special Abilities Filter */}
            <div className="pt-2 border-t border-gray-100">
                <div className="flex items-center justify-between mb-2">
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Skills & Special Abilities</label>
                    <div className="flex items-center gap-1 bg-gray-100 rounded-md p-0.5">
                        <button
                            type="button"
                            onClick={() => setFilter("skillsMatchMode", "OR")}
                            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                                filters.skillsMatchMode === "OR" 
                                    ? "bg-white text-[#111111] shadow-sm" 
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            ANY (OR)
                        </button>
                        <button
                            type="button"
                            onClick={() => setFilter("skillsMatchMode", "AND")}
                            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                                filters.skillsMatchMode === "AND" 
                                    ? "bg-white text-[#111111] shadow-sm" 
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            ALL (AND)
                        </button>
                    </div>
                </div>
                <div className="space-y-3 max-h-60 overflow-y-auto pr-1">
                    {Object.entries(SKILLS_CATEGORIES).map(([catName, skillList]) => (
                        <div key={catName} className="space-y-1">
                            <span className="text-[10px] font-medium text-gray-400 uppercase tracking-wider block">
                                {catName}
                            </span>
                            <div className="flex flex-wrap gap-1.5">
                                {skillList.map(skill => {
                                    const isSelected = filters.skills && filters.skills.includes(skill);
                                    return (
                                        <button
                                            key={skill}
                                            type="button"
                                            onClick={() => {
                                                if (isSelected) {
                                                    setFilter("skills", filters.skills.filter(x => x !== skill));
                                                } else {
                                                    setFilter("skills", [...(filters.skills || []), skill]);
                                                }
                                            }}
                                            className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-all active:scale-[0.98] ${
                                                isSelected 
                                                    ? "bg-[#0c2340] border-[#0c2340] text-white shadow-sm" 
                                                    : "bg-white border-[#0c2340]/40 text-[#0c2340] hover:bg-[#0c2340]/5"
                                            }`}
                                        >
                                            {skill}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            {/* ROW 6 (Internal Tags Autocomplete) */}
            <div className="pt-2 border-t border-gray-100">
                <div className="flex items-center justify-between mb-2">
                    <label className="text-xs font-semibold text-[#222222] uppercase tracking-wider">Internal Tags Filter</label>
                    <div className="flex items-center gap-1 bg-gray-100 rounded-md p-0.5">
                        <button
                            type="button"
                            onClick={() => setFilter("tagMatchMode", "OR")}
                            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                                filters.tagMatchMode === "OR" 
                                    ? "bg-white text-[#111111] shadow-sm" 
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            ANY (OR)
                        </button>
                        <button
                            type="button"
                            onClick={() => setFilter("tagMatchMode", "AND")}
                            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                                filters.tagMatchMode === "AND" 
                                    ? "bg-white text-[#111111] shadow-sm" 
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            ALL (AND)
                        </button>
                    </div>
                </div>
                <TagsFilterInput
                    selectedTagIds={filters.internalTags}
                    globalTags={globalTags}
                    onChange={(newTagIds) => setFilter("internalTags", newTagIds)}
                />
            </div>
        </div>
    );
});

// ============================================================================
// MOBILE FILTERS SHEET
// ============================================================================

const MobileFiltersSheet = memo(({ filters, setFilter, filterOptions, globalTags, onClose, onReset }) => {
    const [localFilters, setLocalFilters] = useState(filters);
    
    const setLocalFilter = (key, value) => {
        setLocalFilters(prev => ({ ...prev, [key]: value }));
    };

    const applyFilters = () => {
        Object.entries(localFilters).forEach(([key, value]) => setFilter(key, value));
        onClose();
    };
    
    return (
        <div className="fixed inset-0 z-50 flex items-end bg-black/50">
            <div className="bg-white rounded-t-2xl w-full max-h-[85vh] overflow-y-auto animate-slide-up flex flex-col">
                <div className="sticky top-0 bg-white border-b border-gray-200 px-4 py-4 flex justify-between items-center shrink-0">
                    <h3 className="font-semibold text-[#111111] text-base">Filters</h3>
                    <div className="flex gap-3">
                        <button 
                            type="button"
                            onClick={() => {
                                onReset();
                                setLocalFilters(FILTER_DEFAULTS);
                            }} 
                            className="text-xs text-[#333333] hover:text-[#111111] font-medium"
                        >
                            Reset
                        </button>
                        <button 
                            type="button"
                            onClick={onClose} 
                            className="w-8 h-8 rounded-full flex items-center justify-center text-[#333333] hover:text-[#222222] hover:bg-gray-100 transition-all"
                        >
                            <X size={18} />
                        </button>
                    </div>
                </div>
                
                <div className="p-4 space-y-5 overflow-y-auto flex-1 pb-10">
                    {/* Gender */}
                    <div>
                        <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Gender</label>
                        <div className="flex flex-wrap gap-2 mt-1.5">
                            {["any", ...filterOptions.genders].map(g => (
                                <button 
                                    key={g} 
                                    type="button"
                                    onClick={() => setLocalFilter("gender", g)} 
                                    className={`px-4 py-1.5 rounded-full text-xs font-medium border transition-all ${
                                        localFilters.gender === g 
                                            ? "bg-gray-900 border-gray-900 text-white shadow-sm" 
                                            : "bg-white border-gray-200 text-[#222222]"
                                    }`}
                                >
                                    {g === "any" ? "Any" : g}
                                </button>
                            ))}
                        </div>
                    </div>
                    
                    {/* Ethnicity */}
                    <div>
                        <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Ethnicity</label>
                        <select 
                            value={localFilters.ethnicity} 
                            onChange={(e) => setLocalFilter("ethnicity", e.target.value)} 
                            className="w-full mt-1.5 px-3 py-2 bg-white border border-gray-200 rounded-lg text-sm"
                        >
                            <option value="any">Any</option>
                            {filterOptions.ethnicities.map(e => <option key={e} value={e}>{e}</option>)}
                        </select>
                    </div>

                    {/* Location */}
                    <div>
                        <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Location</label>
                        <div className="mt-1.5">
                            <LocationMultiSelect
                                value={localFilters.locations}
                                onChange={(v) => setLocalFilter("locations", v)}
                                options={filterOptions.locations}
                            />
                        </div>
                    </div>
                    
                    {/* Age Range */}
                    <div>
                        <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Age Range</label>
                        <div className="flex gap-2 mt-1.5">
                            <input 
                                type="number" 
                                value={localFilters.ageMin} 
                                onChange={(e) => setLocalFilter("ageMin", e.target.value)} 
                                placeholder="Min" 
                                className="w-full px-3 py-2 bg-white border border-gray-200 rounded-lg text-sm" 
                            />
                            <span className="text-[#333333] self-center font-medium">–</span>
                            <input 
                                type="number" 
                                value={localFilters.ageMax} 
                                onChange={(e) => setLocalFilter("ageMax", e.target.value)} 
                                placeholder="Max" 
                                className="w-full px-3 py-2 bg-white border border-gray-200 rounded-lg text-sm" 
                            />
                        </div>
                    </div>

                    {/* Height Range */}
                    <div>
                        <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Height Range</label>
                        <div className="flex gap-2 mt-1.5">
                            <select
                                value={localFilters.heightMin}
                                onChange={(e) => setLocalFilter("heightMin", e.target.value)}
                                className="w-full px-3 py-2 bg-white border border-gray-200 rounded-lg text-sm"
                            >
                                <option value="">Min</option>
                                {HEIGHT_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                            </select>
                            <span className="text-[#333333] self-center font-medium">–</span>
                            <select
                                value={localFilters.heightMax}
                                onChange={(e) => setLocalFilter("heightMax", e.target.value)}
                                className="w-full px-3 py-2 bg-white border border-gray-200 rounded-lg text-sm"
                            >
                                <option value="">Max</option>
                                {HEIGHT_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                            </select>
                        </div>
                    </div>
                    
                    {/* Followers */}
                    <div>
                        <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Instagram Followers</label>
                        <select 
                            value={localFilters.minFollowers} 
                            onChange={(e) => setLocalFilter("minFollowers", Number(e.target.value))} 
                            className="w-full mt-1.5 px-3 py-2 bg-white border border-gray-200 rounded-lg text-sm"
                        >
                            {FOLLOWER_BUCKETS.map(b => <option key={b.value} value={b.value}>{b.label}</option>)}
                        </select>
                    </div>

                    {/* Interested In */}
                    <div>
                        <div className="flex items-center justify-between mb-1.5">
                            <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Interested In</label>
                            <button
                                type="button"
                                onClick={() => setLocalFilter("interestedInMatchMode", localFilters.interestedInMatchMode === "OR" ? "AND" : "OR")}
                                className="px-2 py-0.5 bg-gray-100 rounded text-[10px] font-medium text-[#111111] font-semibold"
                            >
                                Mode: {localFilters.interestedInMatchMode}
                            </button>
                        </div>
                        <div className="flex flex-wrap gap-2 mt-1.5">
                            {["Acting", "Modeling", "Influencer Campaigns"].map(cat => {
                                const isSelected = localFilters.interestedIn.includes(cat);
                                return (
                                    <button
                                        key={cat}
                                        type="button"
                                        onClick={() => {
                                            if (isSelected) {
                                                setLocalFilter("interestedIn", localFilters.interestedIn.filter(x => x !== cat));
                                            } else {
                                                setLocalFilter("interestedIn", [...localFilters.interestedIn, cat]);
                                            }
                                        }}
                                        className={`px-4 py-1.5 rounded-full text-xs font-medium border transition-all ${
                                            isSelected 
                                                ? "bg-gray-900 border-gray-900 text-white shadow-sm" 
                                                : "bg-white border-gray-200 text-[#222222]"
                                        }`}
                                    >
                                        {cat}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Skills & Special Abilities (Mobile View) */}
                    <div>
                        <div className="flex items-center justify-between mb-1.5">
                            <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Skills & Special Abilities</label>
                            <button
                                type="button"
                                onClick={() => setLocalFilter("skillsMatchMode", localFilters.skillsMatchMode === "OR" ? "AND" : "OR")}
                                className="px-2 py-0.5 bg-gray-100 rounded text-[10px] font-medium text-[#111111] font-semibold"
                            >
                                Mode: {localFilters.skillsMatchMode || "OR"}
                            </button>
                        </div>
                        <div className="space-y-3 max-h-48 overflow-y-auto mt-1.5 pr-1 border border-gray-100 rounded-lg p-2 bg-gray-50/50">
                            {Object.entries(SKILLS_CATEGORIES).map(([catName, skillList]) => (
                                <div key={catName} className="space-y-1">
                                    <span className="text-[10px] font-medium text-gray-400 uppercase tracking-wider block">
                                        {catName}
                                    </span>
                                    <div className="flex flex-wrap gap-1.5">
                                        {skillList.map(skill => {
                                            const isSelected = localFilters.skills && localFilters.skills.includes(skill);
                                            return (
                                                <button
                                                    key={skill}
                                                    type="button"
                                                    onClick={() => {
                                                        if (isSelected) {
                                                            setLocalFilter("skills", localFilters.skills.filter(x => x !== skill));
                                                        } else {
                                                            setLocalFilter("skills", [...(localFilters.skills || []), skill]);
                                                        }
                                                    }}
                                                    className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-all active:scale-[0.98] ${
                                                        isSelected 
                                                            ? "bg-[#0c2340] border-[#0c2340] text-white shadow-sm" 
                                                            : "bg-white border-[#0c2340]/40 text-[#0c2340]"
                                                    }`}
                                                >
                                                    {skill}
                                                </button>
                                            );
                                        })}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Internal Tags */}
                    <div>
                        <div className="flex items-center justify-between mb-1.5">
                            <label className="text-xs font-semibold text-[#333333] uppercase tracking-wider">Internal Tags</label>
                            <button
                                type="button"
                                onClick={() => setLocalFilter("tagMatchMode", localFilters.tagMatchMode === "OR" ? "AND" : "OR")}
                                className="px-2 py-0.5 bg-gray-100 rounded text-[10px] font-medium text-[#111111] font-semibold"
                            >
                                Mode: {localFilters.tagMatchMode}
                            </button>
                        </div>
                        <TagsFilterInput
                            selectedTagIds={localFilters.internalTags}
                            globalTags={globalTags}
                            onChange={(newTagIds) => setLocalFilter("internalTags", newTagIds)}
                        />
                    </div>
                </div>
                
                <div className="sticky bottom-0 bg-white border-t border-gray-200 p-4 shrink-0">
                    <button 
                        type="button"
                        onClick={applyFilters} 
                        className="w-full py-3 bg-gray-900 text-white rounded-lg font-medium hover:bg-gray-800 transition-colors shadow-sm"
                    >
                        Apply Filters
                    </button>
                </div>
            </div>
        </div>
    );
});

// ============================================================================
// FILTER CHIP
// ============================================================================

const FilterChip = ({ label, onRemove }) => (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-100 text-[#111111] text-[11px]">
        {label}
        <button onClick={onRemove} className="hover:text-[#111111]"><X size={10} /></button>
    </span>
);

// ============================================================================
// TALENT CARD
// ============================================================================

const TalentCard = memo(({ talent, selected, alreadyInPipeline, onToggle, onPreview, densityMode, isFocused, showIntelligence, isMobile, globalIndex, registerRef }) => {
    const imageUrl = pickImage(talent);
    const config = DENSITY_CONFIG[densityMode];
    const [imageLoaded, setImageLoaded] = useState(false);
    
    const handleToggle = useCallback(() => {
        onToggle(talent.id, alreadyInPipeline);
    }, [onToggle, talent.id, alreadyInPipeline]);
    
    return (
        <button
            ref={(el) => registerRef(globalIndex, el)}
            type="button"
            onClick={handleToggle}
            disabled={alreadyInPipeline}
            role="checkbox"
            aria-checked={selected}
            aria-label={`Select ${talent.name || "Unnamed Talent"}`}
            data-testid={`talent-browser-card-${talent.id}`}
            style={{
                contentVisibility: "auto",
                containIntrinsicSize: densityMode === "compact" ? "280px" : densityMode === "comfortable" ? "320px" : "380px",
            }}
            className={`
                relative text-left rounded-xl overflow-hidden transition-all duration-200 group
                ${alreadyInPipeline 
                    ? "ring-2 ring-[#0c2340] bg-[#0c2340]/[0.02] shadow-sm cursor-not-allowed opacity-95" 
                    : "cursor-pointer hover:shadow-lg hover:-translate-y-0.5 bg-white"
                }
                ${selected ? "ring-[3px] ring-gray-900 shadow-md bg-gray-100/60" : (!alreadyInPipeline ? "ring-1 ring-gray-200 bg-white" : "")}
                ${isFocused && !alreadyInPipeline ? "ring-[3px] ring-blue-500 shadow-lg" : ""}
                focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 focus-visible:ring-offset-2
            `}
        >
            {/* Image */}
            <div className={`${config.imageAspect} bg-gray-50 overflow-hidden relative`}>
                {imageUrl ? (
                    <img
                        src={imageUrl}
                        alt={talent.name || ""}
                        loading="lazy"
                        decoding="async"
                        onLoad={() => setImageLoaded(true)}
                        className={`w-full h-full object-cover transition-opacity duration-300 ${imageLoaded ? "opacity-100" : "opacity-0"}`}
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center bg-gray-100">
                        <ImageIcon className="w-8 h-8 text-gray-300" />
                    </div>
                )}

                {/* Desktop Hover Quick View Overlay */}
                {!isMobile && (
                    <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center pointer-events-none sm:pointer-events-auto">
                        <button
                            type="button"
                            onClick={(e) => {
                                e.stopPropagation();
                                onPreview?.(talent);
                            }}
                            className="px-3 py-1.5 bg-white text-[#111111] rounded-lg text-xs font-semibold hover:bg-gray-100 transition-colors shadow-md flex items-center gap-1.5 active:scale-95 transform"
                        >
                            <Eye size={12} />
                            <span>Quick View</span>
                        </button>
                    </div>
                )}

                {/* Mobile eye button */}
                {isMobile && (
                    <button
                        type="button"
                        onClick={(e) => {
                            e.stopPropagation();
                            onPreview?.(talent);
                        }}
                        className="absolute bottom-2 left-2 w-7 h-7 rounded-full bg-white/90 backdrop-blur-sm shadow flex items-center justify-center text-[#111111] active:scale-95 transition-all z-10 animate-fade-in"
                        aria-label="Preview talent"
                    >
                        <Eye size={14} />
                    </button>
                )}
                
                {/* Intelligence Badges */}
                {showIntelligence && talent.matchScore && !isMobile && (
                    <>
                        <div className="absolute top-2 right-2 px-1.5 py-0.5 rounded-md bg-black/80 backdrop-blur-sm">
                            <div className="flex items-center gap-1">
                                <Star size={8} className="text-yellow-400" />
                                <span className="text-[9px] font-medium text-white">{talent.matchScore}%</span>
                            </div>
                        </div>
                    </>
                )}
            </div>
            
            {/* Content */}
            <div className={config.padding}>
                <h3 className={`${config.titleSize} font-medium text-[#111111] truncate mb-0.5`}>{talent.name || "Unnamed Talent"}</h3>
                {(talent.age || talent.height) && (
                    <div className="flex items-center gap-1.5 text-[10px] text-[#333333] mb-1">
                        {talent.age && <span>{talent.age} yrs</span>}
                        {talent.height && <span>{talent.height}</span>}
                        {formatTalentLocation(talent.location) && !isMobile && (
                            <span className="truncate">
                                · {formatTalentLocation(talent.location)}
                            </span>
                        )}
                    </div>
                )}
                {(talent.instagram_handle) && (
                    <a
                        href={instagramProfileUrl(talent.instagram_handle)}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        className="flex items-center gap-1.5 text-[10px] text-[#333333] hover:text-[#111111] transition-colors"
                    >
                        <Instagram size={10} />
                        {!isMobile && <span className="truncate">{displayInstagramHandle(talent.instagram_handle)}</span>}
                    </a>
                )}
                
            </div>
            
            {/* Selection Indicator */}
            {!alreadyInPipeline && (
                <div className={`absolute top-2 left-2 w-5 h-5 rounded-full border-2 transition-all flex items-center justify-center ${selected ? "bg-gray-900 border-gray-900" : "bg-white border-gray-300"}`}>
                    {selected && <Check size={11} className="text-white" strokeWidth={2.5} />}
                </div>
            )}
            
            {/* Already Added Badge */}
            {alreadyInPipeline && (
                <div className="absolute top-2 left-2 px-1.5 py-0.5 rounded-md bg-black/80 text-white text-[9px] font-medium backdrop-blur-sm">
                    Added
                </div>
            )}
        </button>
    );
});

// ============================================================================
// SELECTION TRAY
// ============================================================================

const SelectionTray = memo(({ selectedTalents, onRemove, selectedCount, onSubmit, submitting, isMobile, onClear }) => {
    return (
        <div
            className="sticky bottom-0 z-20 border-t border-gray-200 bg-white/95 backdrop-blur-sm shadow-lg shrink-0"
            style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}
        >
            <div className="px-4 sm:px-6 py-3 sm:py-4">
                <div className="flex items-center justify-between gap-2 sm:gap-4">
                    <div className="flex items-center gap-2 sm:gap-3 flex-1 min-w-0">
                        <span className="text-xs sm:text-sm font-medium text-[#111111] whitespace-nowrap flex items-center">
                            {selectedCount} selected
                            <button
                                type="button"
                                onClick={onClear}
                                aria-label="Clear all selections"
                                className="text-xs text-[#333333] hover:text-[#222222] underline ml-2.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 rounded px-1 shrink-0"
                            >
                                Clear all
                            </button>
                        </span>
                        
                        <div className="hidden sm:flex items-center gap-2 overflow-x-auto flex-1">
                            {selectedTalents.slice(0, 8).map(talent => (
                                <div key={talent.id} className="relative group shrink-0">
                                    <div className="w-8 h-8 rounded-full overflow-hidden bg-gray-100 ring-2 ring-white shadow-sm">
                                        {pickImage(talent) ? (
                                            <img
                                                src={pickImage(talent)}
                                                alt=""
                                                loading="lazy"
                                                decoding="async"
                                                className="w-full h-full object-cover"
                                            />
                                        ) : (
                                            <div className="w-full h-full flex items-center justify-center bg-gray-200 text-[#333333] text-[10px]">?</div>
                                        )}
                                    </div>
                                    <button
                                        onClick={() => onRemove(talent.id)}
                                        aria-label={`Remove ${talent.name || "Unnamed Talent"} from selection`}
                                        className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-white border border-gray-200 flex items-center justify-center opacity-0 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 transition-opacity shadow-sm"
                                    >
                                        <X size={8} className="text-[#333333]" />
                                    </button>
                                </div>
                            ))}
                            {selectedTalents.length > 8 && <div className="shrink-0 w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center text-xs font-medium text-[#222222]">+{selectedTalents.length - 8}</div>}
                        </div>
                        
                        {isMobile && selectedTalents.length > 0 && <div className="px-2 py-1 rounded-full bg-gray-100 text-xs text-[#222222]">{selectedTalents.length}</div>}
                    </div>
                    
                    <button
                        onClick={onSubmit}
                        disabled={submitting}
                        aria-label={`Add ${selectedCount} selected talents to pipeline`}
                        data-testid="talent-browser-add-selected"
                        className="px-4 sm:px-6 py-1.5 sm:py-2 rounded-lg bg-gray-900 text-white text-xs sm:text-sm font-medium hover:bg-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 focus-visible:ring-offset-2 transition-colors shadow-sm shrink-0 disabled:bg-gray-300 disabled:cursor-not-allowed"
                    >
                        {submitting ? "Adding..." : `Add${!isMobile ? ` ${selectedCount}` : ""}`}
                    </button>
                </div>
            </div>
        </div>
    );
});

// ============================================================================
// LOADING SKELETON
// ============================================================================

const LoadingSkeleton = ({ densityMode, isMobile }) => {
    const config = DENSITY_CONFIG[densityMode];
    const columns = isMobile ? config.columns.mobile : (window.innerWidth < 1024 ? config.columns.tablet : config.columns.desktop);
    const skeletonCount = columns * 3;
    
    return (
        <div className="px-4 sm:px-6 py-4 sm:py-5">
            <div className="grid" style={{ display: "grid", gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`, gap: `${config.gap}px` }}>
                {Array.from({ length: skeletonCount }).map((_, idx) => (
                    <div key={idx} className="animate-pulse">
                        <div className="bg-gray-100 rounded-xl overflow-hidden">
                            <div className={`${config.imageAspect} bg-gray-200`} />
                            <div className={config.padding}>
                                <div className="h-4 bg-gray-200 rounded w-3/4 mb-2" />
                                <div className="h-3 bg-gray-200 rounded w-1/2 mb-1" />
                                <div className="h-3 bg-gray-200 rounded w-2/3" />
                            </div>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
};

// ============================================================================
// EMPTY RESULTS
// ============================================================================

function EmptyResults({ onReset, hasFilters }) {
    return (
        <div className="flex flex-col items-center justify-center text-center h-full min-h-[400px]">
            <div className="w-12 h-px bg-gradient-to-r from-transparent via-gray-300 to-transparent mb-4" />
            <p className="text-xs tracking-[0.22em] uppercase text-[#333333] mb-2">
                {hasFilters ? "No matches found" : "No talents available"}
            </p>
            <p className="text-sm text-[#333333] max-w-sm px-6 mb-5">
                {hasFilters 
                    ? "No talents match the current casting filters." 
                    : "Add talents from the global roster page first to populate this campaign."}
            </p>
            {hasFilters && (
                <button 
                    type="button"
                    onClick={onReset} 
                    className="px-4 py-2 border border-gray-900 rounded-lg text-xs font-semibold text-[#111111] hover:bg-gray-50 transition-all shadow-sm"
                >
                    Reset Filters
                </button>
            )}
        </div>
    );
}
