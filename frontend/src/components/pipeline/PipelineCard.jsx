import React, {
    memo,
    useState,
    useEffect,
    useRef,
    useCallback,
    useMemo,
} from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { adminApi } from "@/lib/api";
import TalentAvatar from "./TalentAvatar";
import {
    NEXT_STAGE_FLOW,
    STAGE_LABELS,
    STATUS_TONES,
    getStageLabel,
    normaliseStage,
    VISIBLE_ACTIONS_PER_CARD,
} from "./constants";
import { displayInstagramHandle, instagramProfileUrl, firstNameOf } from "@/lib/mediaUtils";
// Reuse Browse Roster's existing Quick View drawer + breakpoint hook verbatim
// — no parallel implementation, no duplicated talent-detail rendering.
import { TalentPreviewDrawer, useMediaQuery } from "./TalentBrowserModal";
import { getCachedTalent, fetchTalentOnce } from "@/lib/talentPreviewCache";
// Icons from lucide-react for consistency and maintainability
import {
    User,
    Clock,
    ArrowRight,
    MessageCircle,
    Phone,
    MoreHorizontal,
    Check,
    Bell,
    X,
    Loader2,
    Eye,
} from "lucide-react";

// ============================================================================
// CONSTANTS (will move to pipelineCard.constants.js eventually)
// ============================================================================

const ALL_PIPELINE_STAGES = [
    "ask_to_test",
    "follow_up",
    "approved",
    "shortlisted",
    "hold",
    "locked",
    "already_tested",
    "rejected",
    "not_available",
    "not_interested",
];

const PRIORITY_BADGES = {
    high_fit: { label: 'High Fit', variant: 'emerald' },
    hot: { label: 'Hot', variant: 'amber' },
    pending: { label: 'Pending', variant: 'blue' },
    callback: { label: 'Callback', variant: 'purple' },
    negotiation: { label: 'Negotiation', variant: 'indigo' },
    hold: { label: 'Hold', variant: 'gray' },
};

const PRIORITY_VARIANTS = {
    emerald: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    amber: 'bg-amber-50 text-amber-700 border-amber-200',
    blue: 'bg-blue-50 text-blue-700 border-blue-200',
    purple: 'bg-purple-50 text-purple-700 border-purple-200',
    indigo: 'bg-indigo-50 text-indigo-700 border-indigo-200',
    gray: 'bg-gray-50 text-[#222222] border-gray-200',
};

const FRESHNESS_CONFIG = {
    new: { dot: 'bg-emerald-500', label: 'New', thresholdHours: 24 },
    updated: { dot: 'bg-blue-500', label: 'Updated', thresholdHours: 72 },
    stale: { dot: 'bg-amber-500', label: 'Stale', thresholdHours: 168 },
    inactive: { dot: 'bg-gray-400', label: 'Inactive', thresholdDays: 7 },
};

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

const formatRelativeTime = (dateString) => {
    if (!dateString) return null;
    const date = new Date(dateString);
    const now = new Date();
    const diffHours = Math.floor((now - date) / (1000 * 60 * 60));
    if (diffHours < 1) return 'just now';
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays === 1) return 'yesterday';
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

const getFreshness = (updatedAt, createdAt) => {
    const date = updatedAt || createdAt;
    if (!date) return null;
    const now = new Date();
    const diffHours = (now - new Date(date)) / (1000 * 60 * 60);
    if (diffHours < FRESHNESS_CONFIG.new.thresholdHours) return FRESHNESS_CONFIG.new;
    if (diffHours < FRESHNESS_CONFIG.updated.thresholdHours) return FRESHNESS_CONFIG.updated;
    if (diffHours < FRESHNESS_CONFIG.stale.thresholdHours) return FRESHNESS_CONFIG.stale;
    return FRESHNESS_CONFIG.inactive;
};

// Security: WhatsApp URL with proper encoding and security attributes
const getWhatsAppUrl = (phoneNumber) => {
    if (!phoneNumber) return null;
    const cleaned = phoneNumber.replace(/\D/g, '');
    return `https://wa.me/${cleaned}`;
};

// ============================================================================
// MAIN COMPONENT
// ============================================================================

const PipelineCard = memo(function PipelineCard({
    projectId,
    item,
    refresh,
    bulkMode,
    isSelected,
    onToggleSelect,
    readOnly = false,
    dragSupported = false,
    isDragging = false,
    onDragStart,
    onDragEnd,
    compact = false,
}) {
    const navigate = useNavigate();
    const [moving, setMoving] = useState(false);
    // 'idle' | 'loading' | 'success' — drives Bell/X icon swap + disabled state.
    // A ref (not just the state) guards against a second click firing before
    // React re-renders with disabled=true, since window.confirm() only blocks
    // re-entrancy up to the moment it returns.
    const [reminderState, setReminderState] = useState('idle');
    const [removeState, setRemoveState] = useState('idle');
    const reminderInFlightRef = useRef(false);
    const removeInFlightRef = useRef(false);
    const reminderResetTimeoutRef = useRef(null);
    const removeRefreshTimeoutRef = useRef(null);
    const [previewTalent, setPreviewTalent] = useState(null);
    const [previewLoading, setPreviewLoading] = useState(false);
    const isMobile = useMediaQuery('(max-width: 767px)');
    
    const handleCardClick = useCallback((e) => {
        // If clicking input, buttons, or links, do not navigate to profile
        if (e.target.closest('button') || e.target.closest('a') || e.target.closest('input')) {
            return;
        }
        // The Quick View drawer is rendered via createPortal (into
        // document.body), but React bubbles its events through the
        // component tree regardless of DOM position — without this guard,
        // clicking the drawer's backdrop/content to close it would also
        // fire this card's navigate-to-profile handler.
        if (previewTalent) return;
        if (readOnly) return;
        if (item.talent_id) {
            navigate(`/admin/talents/${item.talent_id}`);
        }
    }, [item.talent_id, readOnly, navigate, previewTalent]);

    const [showMoreActions, setShowMoreActions] = useState(false);
    const [showActionRail, setShowActionRail] = useState(false);
    const [showQuickMoveMenu, setShowQuickMoveMenu] = useState(false);
    const overflowRef = useRef(null);
    const quickMoveButtonRef = useRef(null);
    const moreButtonRef = useRef(null);
    const cardRef = useRef(null);
    const actionRailRef = useRef(null);
    const hoverTimeoutRef = useRef(null);
    const abortControllerRef = useRef(null);

    // ============================================================================
    // DERIVED DATA (memoized for performance)
    // ============================================================================
    
    const canonicalStage = normaliseStage(item.stage);
    const nextStages = NEXT_STAGE_FLOW[canonicalStage] || [];
    const statusTone = STATUS_TONES[canonicalStage];
    const visibleActions = nextStages.slice(0, VISIBLE_ACTIONS_PER_CARD);
    const overflowActions = nextStages.slice(VISIBLE_ACTIONS_PER_CARD);
    
    const displayName = item.talent_name || item.talent_id || "Unknown";
    const displayEmail = item.talent_email || item.email || null;
    const displayPhone = item.talent_phone || null;
    const displayIg = item.instagram_handle || null;
    
    // Recruiter metadata
    const recruiterName = item.assigned_recruiter || item.recruiter_name || 'Unassigned';
    const lastActivity = formatRelativeTime(item.updated_at || item.last_activity_at);
    const responseStatus = item.response_status || (item.last_message_at ? 'Responsive' : 'Awaiting');
    const availability = item.availability || (item.is_available ? 'Available' : 'Booked');
    
    // Priority badges
    const priorityTags = item.priority_tags || [];
    const hasHighFit = item.fit_score >= 80 || priorityTags.includes('high_fit');
    const computedPriority = hasHighFit ? 'high_fit' : (priorityTags[0] || null);
    const activePriority = computedPriority ? PRIORITY_BADGES[computedPriority] : null;
    
    // Freshness indicator
    const freshness = getFreshness(item.updated_at, item.created_at);
    
    const draggable = dragSupported && !readOnly && !bulkMode;

    // ============================================================================
    // MEMOIZED ACTION HANDLERS (fixes ISSUE 3 - prevents recreation on each render)
    // ============================================================================
    
    const move = useCallback(async (stage, options = {}) => {
        const { silent = false, optimistic = false } = options;
        
        // Cancel any in-flight request
        if (abortControllerRef.current) {
            abortControllerRef.current.abort();
        }
        
        abortControllerRef.current = new AbortController();
        setMoving(true);
        
        // Optimistic update flag - future enhancement for rollback
        if (optimistic) {
            // Future: implement optimistic UI update with rollback
        }
        
        try {
            await adminApi.patch(`/projects/${projectId}/pipeline/move`, {
                ids: [item.id],
                stage,
            }, {
                signal: abortControllerRef.current.signal,
            });
            await refresh();
            if (!silent) {
                toast.success(`Moved ${displayName} to ${getStageLabel(stage)}`);
            }
        } catch (e) {
            if (e.name === 'AbortError') {
                console.log('Move request cancelled');
                return;
            }
            console.error("Move failed:", e);
            toast.error(formatErrorDetail(e, "Move failed"));
            // Future: implement retry logic here
        } finally {
            setMoving(false);
            abortControllerRef.current = null;
        }
    }, [item.id, refresh, displayName, projectId]);
    
    // Quick actions with security improvements (ISSUE 6 - noopener)
    const quickActions = useMemo(() => ({
        whatsapp: () => {
            const url = getWhatsAppUrl(displayPhone);
            if (url) {
                window.open(url, '_blank', 'noopener,noreferrer');
            } else {
                toast.error('No phone number available');
            }
        },
        call: () => {
            if (displayPhone) {
                window.location.href = `tel:${displayPhone}`;
            } else {
                toast.error('No phone number available');
            }
        },
    }), [displayPhone]);

    // Send Follow-up Reminder — reuses the existing WhatsApp Engine end to
    // end: the same PROJECT recipient-resolution path (group-name priority,
    // then phone number — identical routing to any real campaign), the same
    // system "Follow Up" template (looked up by its stable slug, never
    // duplicated), and the same batch/job/worker delivery pipeline. This
    // handler only narrows PROJECT resolution to this one talent via the
    // talent_ids filter added to SourceParams — no new messaging logic.
    //
    // UX state machine: idle -> loading -> success -> (1.5s) -> idle, or
    // idle -> loading -> idle on failure. reminderInFlightRef blocks a
    // second invocation firing before React commits disabled=true.
    const sendReminder = useCallback(async (e) => {
        e.stopPropagation();
        if (reminderInFlightRef.current) return;
        if (!item.talent_id) {
            toast.error("No talent linked to this card");
            return;
        }
        if (!window.confirm(`Send Follow-up reminder to ${firstNameOf(displayName) || displayName}?`)) {
            return;
        }
        reminderInFlightRef.current = true;
        setReminderState('loading');
        try {
            const { data: templates } = await adminApi.get("/whatsapp/templates");
            const followUp = (templates || []).find((t) => t.slug === "follow_up");
            if (!followUp) {
                toast.error('Follow Up template not found — check WhatsApp Engine → Templates.');
                reminderInFlightRef.current = false;
                setReminderState('idle');
                return;
            }
            const { data: batch } = await adminApi.post("/whatsapp/batches", {
                source_type: "PROJECT",
                source_params: {
                    project_id: projectId,
                    pipeline_stages: [canonicalStage],
                    talent_ids: [item.talent_id],
                },
                template_id: followUp.id,
                is_dry_run: false,
            });
            if (!batch?.jobs?.length) {
                toast.error(`${firstNameOf(displayName) || displayName} has no WhatsApp number or group on file`);
                reminderInFlightRef.current = false;
                setReminderState('idle');
                return;
            }
            toast.success(`Follow-up reminder queued for ${firstNameOf(displayName) || displayName}`);
            setReminderState('success');
            reminderResetTimeoutRef.current = setTimeout(() => {
                reminderInFlightRef.current = false;
                setReminderState('idle');
            }, 1500);
        } catch (err) {
            console.error("Send reminder failed:", err);
            toast.error(formatErrorDetail(err, "Failed to send reminder"));
            reminderInFlightRef.current = false;
            setReminderState('idle');
        }
    }, [item.talent_id, displayName, projectId, canonicalStage]);

    // Remove from Ask To Test — reuses the existing single-entry pipeline
    // delete endpoint (already built, previously unused by any UI) which
    // deletes only this one casting_pipeline row. It never touches the
    // talent, submission, application, media, or project records.
    //
    // UX state machine: idle -> loading -> success -> (checkmark shown ~1.5s)
    // -> refresh() removes the card from the board naturally. On failure,
    // idle -> loading -> idle (X restored) with the existing error toast.
    const removeFromAskToTest = useCallback(async (e) => {
        e.stopPropagation();
        if (removeInFlightRef.current) return;
        if (!window.confirm(`Remove ${firstNameOf(displayName) || displayName} from Ask To Test?`)) {
            return;
        }
        removeInFlightRef.current = true;
        setRemoveState('loading');
        try {
            await adminApi.delete(`/projects/${projectId}/pipeline/${item.id}`);
            setRemoveState('success');
            toast.success(`Removed ${firstNameOf(displayName) || displayName} from Ask To Test`);
            removeRefreshTimeoutRef.current = setTimeout(() => {
                refresh();
            }, 1500);
        } catch (err) {
            console.error("Remove from Ask To Test failed:", err);
            toast.error(formatErrorDetail(err, "Failed to remove"));
            removeInFlightRef.current = false;
            setRemoveState('idle');
        }
    }, [projectId, item.id, displayName, refresh]);

    // Quick View — reuses the exact TalentPreviewDrawer component from
    // Browse Roster's TalentBrowserModal (imported above); no parallel
    // drawer, no duplicated rendering.
    //
    // Instant-open + lazy hydration + session cache: the pipeline card
    // already carries name/instagram_handle/image_url (see
    // backend/routers/casting_pipeline.py's _talent_merge_fields), and
    // TalentPreviewDrawer already renders "—" for any field it doesn't
    // have — so opening with that partial object needs zero network
    // round trips and zero drawer changes. `media` is the one field only
    // a full /talents/{id} response carries, so its presence is the
    // hydrated-vs-partial signal. talentPreviewCache (module-level, shared
    // across every card) makes every open after the first — same talent,
    // re-open, or a different already-opened talent — free.
    const openQuickView = useCallback((e) => {
        e.stopPropagation();
        if (!item.talent_id) {
            toast.error("No talent linked to this card");
            return;
        }
        const cached = getCachedTalent(item.talent_id);
        const initial = cached || {
            id: item.talent_id,
            name: displayName,
            instagram_handle: displayIg,
            image_url: item.image_url,
        };
        setPreviewTalent(initial);

        const isFullyHydrated = Array.isArray(initial.media);
        if (isFullyHydrated) return; // cache hit — no request at all

        setPreviewLoading(true);
        fetchTalentOnce(item.talent_id, async () => {
            const { data } = await adminApi.get(`/talents/${item.talent_id}`);
            return data;
        })
            .then((full) => {
                // Only patch the drawer if the user is still looking at THIS
                // talent — they may have opened a different card meanwhile.
                setPreviewTalent((prev) => (prev && prev.id === item.talent_id ? full : prev));
            })
            .catch((err) => {
                console.error("Quick View hydration failed:", err);
                toast.error(formatErrorDetail(err, "Failed to load full talent details"));
            })
            .finally(() => setPreviewLoading(false));
    }, [item.talent_id, item.image_url, displayName, displayIg]);

    const closeMoreMenu = useCallback(() => {
        setShowMoreActions(false);
    }, []);
    
    const toggleMoreMenu = useCallback(() => {
        setShowMoreActions(prev => !prev);
    }, []);
    
    const handleOverflowAction = useCallback(async (stage) => {
        setShowMoreActions(false);
        await move(stage);
    }, [move]);
    
    // ============================================================================
    // HOVER HANDLERS WITH CLEANUP (fixes ISSUE 4 - mobile considerations)
    // ============================================================================
    
    // Touch device detection — evaluated once at mount
    const isTouchDevice = useMemo(() => {
        return 'ontouchstart' in window || navigator.maxTouchPoints > 0;
    }, []);
    
    const handleMouseEnter = useCallback(() => {
        // Touch devices use explicit trigger button — skip hover-based rail
        if (readOnly || bulkMode || isTouchDevice) return;
        hoverTimeoutRef.current = setTimeout(() => {
            setShowActionRail(true);
        }, 150);
    }, [readOnly, bulkMode, isTouchDevice]);
    
    const handleMouseLeave = useCallback(() => {
        // Touch devices manage rail via explicit toggle — skip hover-based close
        if (isTouchDevice) return;
        if (hoverTimeoutRef.current) {
            clearTimeout(hoverTimeoutRef.current);
        }
        setShowActionRail(false);
    }, [isTouchDevice]);
    
    // Explicit toggle for touch devices — replaces hover on pointer-less devices
    const handleTouchTrigger = useCallback((e) => {
        e.stopPropagation();
        setShowActionRail(prev => !prev);
    }, []);
    
    // Future: long press handler for mobile devices
    const handleTouchStart = useCallback(() => {
        if (isTouchDevice && !readOnly && !bulkMode) {
            // Future: implement long-press detection
        }
    }, [isTouchDevice, readOnly, bulkMode]);
    
    // ============================================================================
    // EFFECTS
    // ============================================================================
    
    // Click outside handler for overflow menu
    useEffect(() => {
        if (!showMoreActions) return;
        
        function handleClickOutside(e) {
            if (
                overflowRef.current &&
                !overflowRef.current.contains(e.target) &&
                moreButtonRef.current &&
                !moreButtonRef.current.contains(e.target)
            ) {
                setShowMoreActions(false);
            }
        }
        
        function handleEsc(e) {
            if (e.key === "Escape") {
                setShowMoreActions(false);
                setShowActionRail(false);
            }
        }
        
        document.addEventListener("mousedown", handleClickOutside);
        document.addEventListener("keydown", handleEsc);
        
        return () => {
            document.removeEventListener("mousedown", handleClickOutside);
            document.removeEventListener("keydown", handleEsc);
        };
    }, [showMoreActions]);

    // Click outside handler for quick move menu
    useEffect(() => {
        if (!showQuickMoveMenu) return;
        
        function handleClickOutside(e) {
            if (
                quickMoveButtonRef.current &&
                !quickMoveButtonRef.current.contains(e.target)
            ) {
                setShowQuickMoveMenu(false);
            }
        }
        
        function handleEsc(e) {
            if (e.key === "Escape") {
                setShowQuickMoveMenu(false);
            }
        }
        
        document.addEventListener("mousedown", handleClickOutside);
        document.addEventListener("keydown", handleEsc);
        
        return () => {
            document.removeEventListener("mousedown", handleClickOutside);
            document.removeEventListener("keydown", handleEsc);
        };
    }, [showQuickMoveMenu]);
    
    // Cleanup on unmount
    useEffect(() => {
        return () => {
            if (abortControllerRef.current) {
                abortControllerRef.current.abort();
            }
            if (hoverTimeoutRef.current) {
                clearTimeout(hoverTimeoutRef.current);
            }
            if (reminderResetTimeoutRef.current) {
                clearTimeout(reminderResetTimeoutRef.current);
            }
            if (removeRefreshTimeoutRef.current) {
                clearTimeout(removeRefreshTimeoutRef.current);
            }
        };
    }, []);
    
    // Close touch action rail when tapping outside card (touch devices only)
    useEffect(() => {
        if (!isTouchDevice || !showActionRail) return;
        
        function handleTouchOutside(e) {
            if (
                cardRef.current &&
                !cardRef.current.contains(e.target)
            ) {
                setShowActionRail(false);
            }
        }
        
        document.addEventListener('touchstart', handleTouchOutside, { passive: true });
        return () => document.removeEventListener('touchstart', handleTouchOutside);
    }, [isTouchDevice, showActionRail]);
    
    // ============================================================================
    // DRAG & DROP HANDLERS
    // ============================================================================
    
    const handleDragStart = useCallback((e) => {
        if (!draggable) return;
        e.dataTransfer.setData("text/plain", item.id);
        e.dataTransfer.effectAllowed = "move";
        setTimeout(() => onDragStart && onDragStart(item.id), 0);
    }, [draggable, item.id, onDragStart]);
    
    const handleDragEnd = useCallback(() => {
        if (!draggable) return;
        if (onDragEnd) onDragEnd();
    }, [draggable, onDragEnd]);
    
    const handleKeyDown = useCallback((e) => {
        if (bulkMode && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onToggleSelect(item.id);
        }
    }, [bulkMode, item.id, onToggleSelect]);
    
    // ============================================================================
    // STYLING (maintaining white luxury ATS styling)
    // ============================================================================
    
    const shellClass = [
        "group relative rounded-lg overflow-hidden transition-all duration-150 ease-out bg-white shadow-[0_1px_2px_rgba(0,0,0,0.04)] border cursor-pointer",
        isSelected
            ? "border-emerald-500 bg-emerald-50/[0.08] ring-1 ring-emerald-500/25"
            : "border-black/[0.08] hover:border-black/[0.12] hover:shadow-[0_2px_4px_rgba(0,0,0,0.04)]",
        moving ? "opacity-40 pointer-events-none" : "",
        isDragging ? "opacity-75 scale-[0.995]" : "",
        draggable ? "cursor-grab active:cursor-grabbing" : "",
    ].join(" ");

    // ============================================================================
    // MAIN RENDER
    // ============================================================================
    
    return (
        <div
            ref={cardRef}
            data-testid={`pipeline-card-${item.id}`}
            draggable={draggable}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            onTouchStart={handleTouchStart}
            onClick={handleCardClick}
            className={shellClass}
            aria-label={`Talent: ${displayName}`}
        >
            {/* Selection Checkbox Overlay - visible on hover or when selected/bulkMode active */}
            {!readOnly && (
                <div 
                    className={`absolute top-2 left-2 z-30 transition-opacity duration-150 ${
                        isSelected || bulkMode ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                    }`}
                >
                    <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={(e) => {
                            // Column/stage items array can be passed via hook or parent context.
                            // For simplicity, range-selects will check the shift key.
                            onToggleSelect(item.id, e.nativeEvent.shiftKey);
                        }}
                        onClick={(e) => e.stopPropagation()}
                        className="w-4 h-4 rounded border border-black/30 bg-white checked:bg-black checked:border-black cursor-pointer focus:outline-none focus:ring-1 focus:ring-black/20"
                        aria-label={`Select ${displayName}`}
                    />
                </div>
            )}

            {/* Remove from Ask To Test — ONLY this stage; deletes just this one
                pipeline entry (existing DELETE endpoint), never the talent/
                submission/application/media/project. */}
            {!readOnly && canonicalStage === "ask_to_test" && (
                <button
                    type="button"
                    onClick={removeFromAskToTest}
                    disabled={removeState !== 'idle'}
                    aria-label={`Remove ${displayName} from Ask To Test`}
                    title="Remove from Ask To Test"
                    className="absolute top-2 right-2 z-30 w-5 h-5 rounded-full flex items-center justify-center text-black/35 hover:text-black/70 hover:bg-black/[0.06] transition-colors disabled:opacity-100"
                >
                    {removeState === 'loading' && <Loader2 className="w-3 h-3 animate-spin" />}
                    {removeState === 'success' && <Check className="w-3 h-3 text-emerald-500" strokeWidth={3} />}
                    {removeState === 'idle' && <X className="w-3 h-3" strokeWidth={2.5} />}
                </button>
            )}

            <div className="p-3 md:p-4 space-y-2 md:space-y-2.5">
                {/* Row 1: Identity block — Avatar + Name/Handle + workflow chips inline */}
                <div className="flex items-start gap-3">
                    {/* Avatar — full opacity anchors identity */}
                    <div className="flex-shrink-0 relative">
                        {/* Selected Indicator - small overlay tag on avatar if selected */}
                        {isSelected && (
                            <div className="absolute -top-1 -right-1 z-10 bg-emerald-500 text-white rounded-full p-0.5 shadow-sm border border-white">
                                <Check className="w-2.5 h-2.5 stroke-[3]" />
                            </div>
                        )}
                        <TalentAvatar
                            src={item.image_url}
                            name={displayName}
                            size="md"
                        />
                    </div>

                    {/* Identity column — full width, no clearance hack */}
                    <div className="flex-1 min-w-0">
                        {/* Name row: name + status chip inline — chip anchors right, name anchors left */}
                        <div className="flex items-start justify-between gap-2 min-w-0">
                            <p
                                className="text-[13.5px] text-black/85 font-semibold leading-[1.2] truncate tracking-tight"
                                title={displayName}
                            >
                                {displayName}
                            </p>
                            {/* Status chip — sits inline with name, right-aligned, no absolute positioning */}
                            {statusTone && (
                                <span
                                    className={`shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded border ${statusTone.chip}`}
                                    title={statusTone.label}
                                    role="status"
                                >
                                    <span className={`w-1 h-1 rounded-full ${statusTone.dot}`} />
                                    <span className={`text-[7.5px] tracking-wide uppercase ${statusTone.text}`}>
                                        {statusTone.label}
                                    </span>
                                </span>
                            )}
                        </div>

                        {/* IG handle — secondary identity line */}
                        {displayIg && (
                            <a
                                href={instagramProfileUrl(displayIg)}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-[10px] text-black/40 hover:text-black/60 truncate mt-0.5 transition-colors duration-100"
                            >
                                {displayInstagramHandle(displayIg)}
                            </a>
                        )}

                        {/* Priority + Freshness row — workflow metadata, visually subordinate to identity */}
                        {(activePriority || freshness) && (
                            <div className="flex flex-wrap items-center gap-1 mt-1">
                                {activePriority && (
                                    <span
                                        className={`shrink-0 inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-medium border ${PRIORITY_VARIANTS[activePriority.variant]}`}
                                    >
                                        {activePriority.label}
                                    </span>
                                )}
                                {freshness && (
                                    <span
                                        className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-black/[0.04]"
                                        title={`Last activity: ${formatRelativeTime(item.updated_at || item.created_at)}`}
                                    >
                                        <span className={`w-1.5 h-1.5 rounded-full ${freshness.dot}`} />
                                        <span className="text-[9px] text-black/40">
                                            {freshness.label}
                                        </span>
                                    </span>
                                )}
                            </div>
                        )}
                    </div>
                </div>
                
                {/* Row 3: Stage actions */}
                {!readOnly && visibleActions.length > 0 && (
                    <div
                        className="flex flex-wrap items-center gap-1 pt-1.5"
                        role="group"
                        aria-label="Stage actions"
                    >
                        {visibleActions.map((stage) => (
                            <button
                                key={stage}
                                type="button"
                                onClick={() => move(stage)}
                                disabled={moving}
                                data-testid={`pipeline-card-move-${item.id}-${stage}`}
                                className="
                                    px-2.5 py-1.5 md:px-2 md:py-1 rounded-md
                                    text-[10.5px] md:text-[9px] font-medium
                                    text-black/50 hover:text-black/80
                                    bg-black/[0.03] hover:bg-black/[0.06]
                                    border border-transparent hover:border-black/[0.08]
                                    transition-all duration-100
                                    disabled:opacity-40
                                    min-h-[32px] md:min-h-0
                                    flex items-center justify-center
                                "
                            >
                                {STAGE_LABELS[stage] || getStageLabel(stage)}
                            </button>
                        ))}
                        {overflowActions.length > 0 && (
                            <div className="relative" ref={overflowRef}>
                                <button
                                    ref={moreButtonRef}
                                    type="button"
                                    onClick={toggleMoreMenu}
                                    aria-label="More actions"
                                    aria-expanded={showMoreActions}
                                    className="
                                        flex items-center justify-center
                                        w-8 h-8 md:w-5 md:h-5 rounded-md
                                        text-black/40 hover:text-black/60
                                        hover:bg-black/[0.03]
                                        transition-colors duration-100
                                    "
                                >
                                    <MoreHorizontal className="w-4 h-4 md:w-3 h-3" />
                                </button>
                                {showMoreActions && (
                                    <div className="absolute bottom-full right-0 mb-1 z-20 bg-white border border-black/[0.08] shadow-[0_4px_12px_-8px_rgba(0,0,0,0.1)] rounded-md py-1 min-w-[100px]">
                                        {overflowActions.map((stage) => (
                                            <button
                                                key={stage}
                                                type="button"
                                                onClick={() => handleOverflowAction(stage)}
                                                className="w-full text-left px-3 py-1.5 text-[9px] tracking-wide uppercase text-black/60 hover:text-black/90 hover:bg-black/[0.02]"
                                            >
                                                {STAGE_LABELS[stage] || getStageLabel(stage)}
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </div>
            
            {/* Action Rail — hover on desktop, explicit trigger on touch */}
            {!readOnly && !bulkMode && showActionRail && (
                <div
                    ref={actionRailRef}
                    className="absolute right-2 top-1/2 -translate-y-1/2 flex flex-col gap-1.5 bg-white/95 backdrop-blur-sm rounded-lg p-1.5 shadow-sm border border-black/[0.06] z-10"
                    style={{ maxHeight: 'calc(100% - 16px)' }}
                    onMouseEnter={() => {
                        if (isTouchDevice) return;
                        if (hoverTimeoutRef.current) clearTimeout(hoverTimeoutRef.current);
                        setShowActionRail(true);
                    }}
                >
                    <div className="relative" ref={quickMoveButtonRef}>
                        <button
                            onClick={(e) => {
                                e.stopPropagation();
                                setShowQuickMoveMenu((prev) => !prev);
                            }}
                            className={`w-7 h-7 rounded-md flex items-center justify-center transition-colors ${
                                showQuickMoveMenu
                                    ? "bg-black/10 text-black/80"
                                    : "text-black/50 hover:text-black/80 hover:bg-black/[0.04]"
                            }`}
                            title="Quick move"
                        >
                            <ArrowRight className="w-3.5 h-3.5" />
                        </button>
                        {showQuickMoveMenu && (
                            <div
                                className="absolute right-full mr-2 top-0 bg-white border border-black/[0.08] shadow-md rounded-lg py-1 px-1 z-30 min-w-[130px] flex flex-col gap-0.5"
                                onClick={(e) => e.stopPropagation()}
                            >
                                <div className="px-2 py-1 text-[9px] font-semibold text-black/45 tracking-wider uppercase border-b border-black/[0.04] mb-1">
                                    Move to
                                </div>
                                {ALL_PIPELINE_STAGES.filter((s) => s !== canonicalStage).map((stage) => (
                                    <button
                                        key={stage}
                                        onClick={async (e) => {
                                            e.stopPropagation();
                                            setShowQuickMoveMenu(false);
                                            await move(stage);
                                        }}
                                        className="w-full text-left px-2 py-1 text-[10px] text-black/75 hover:bg-black/[0.04] hover:text-black/90 rounded transition-colors"
                                    >
                                        {STAGE_LABELS[stage] || getStageLabel(stage)}
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>
                    {displayPhone && (
                        <button
                            onClick={quickActions.whatsapp}
                            className="w-7 h-7 rounded-md flex items-center justify-center text-[#25D366] hover:bg-[#25D366]/10 transition-colors"
                            title="WhatsApp"
                        >
                            <MessageCircle className="w-3.5 h-3.5" />
                        </button>
                    )}
                    {displayPhone && (
                        <button
                            onClick={quickActions.call}
                            className="w-7 h-7 rounded-md flex items-center justify-center text-black/50 hover:text-black/80 hover:bg-black/[0.04] transition-colors"
                            title="Call"
                        >
                            <Phone className="w-3.5 h-3.5" />
                        </button>
                    )}
                    <button
                        onClick={sendReminder}
                        disabled={reminderState !== 'idle'}
                        className="w-7 h-7 rounded-md flex items-center justify-center text-black/50 hover:text-black/80 hover:bg-black/[0.04] transition-colors disabled:opacity-100"
                        title="Send Follow-up Reminder"
                    >
                        {reminderState === 'loading' && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                        {reminderState === 'success' && <Check className="w-3.5 h-3.5 text-emerald-500" strokeWidth={3} />}
                        {reminderState === 'idle' && <Bell className="w-3.5 h-3.5" />}
                    </button>
                    <button
                        onClick={openQuickView}
                        className="w-7 h-7 rounded-md flex items-center justify-center text-black/50 hover:text-black/80 hover:bg-black/[0.04] transition-colors"
                        title="Quick View"
                    >
                        {previewLoading ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                            <Eye className="w-3.5 h-3.5" />
                        )}
                    </button>
                </div>
            )}

            {/* Touch Action Trigger — always visible on touch devices when rail is closed */}
            {isTouchDevice && !readOnly && !bulkMode && !showActionRail && (
                <button
                    type="button"
                    onClick={handleTouchTrigger}
                    aria-label="Quick actions"
                    title="Quick actions"
                    className="absolute bottom-2.5 right-2.5 z-10 w-11 h-11 md:w-6 md:h-6 rounded-md flex items-center justify-center text-black/30 bg-white/90 border border-black/[0.06] shadow-sm transition-colors duration-100 active:bg-black/[0.04] active:text-black/55"
                >
                    <MoreHorizontal className="w-4 h-4 md:w-3 md:h-3" />
                </button>
            )}

            {/* Quick View — same drawer Browse Roster uses, rendered via
                createPortal so its position here is layout-irrelevant. */}
            {previewTalent && (
                <TalentPreviewDrawer
                    talent={previewTalent}
                    onClose={() => setPreviewTalent(null)}
                    isMobile={isMobile}
                />
            )}
        </div>
    );
});

export default PipelineCard;
