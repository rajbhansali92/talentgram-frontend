import React, {
    memo,
    useState,
    useEffect,
    useRef,
    useCallback,
    useMemo,
} from "react";
import { toast } from "sonner";
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
// Icons from lucide-react for consistency and maintainability
import {
    User,
    Clock,
    ArrowRight,
    FileText,
    MessageCircle,
    Mail,
    Phone,
    Star,
    MoreHorizontal,
    Check,
} from "lucide-react";

// ============================================================================
// CONSTANTS (will move to pipelineCard.constants.js eventually)
// ============================================================================

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
    gray: 'bg-gray-50 text-gray-600 border-gray-200',
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
    const [moving, setMoving] = useState(false);
    const [showMoreActions, setShowMoreActions] = useState(false);
    const [showActionRail, setShowActionRail] = useState(false);
    const overflowRef = useRef(null);
    const moreButtonRef = useRef(null);
    const cardRef = useRef(null);
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
            await adminApi.patch("/pipeline/move", {
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
            toast.error(e?.response?.data?.detail || "Move failed");
            // Future: implement retry logic here
        } finally {
            setMoving(false);
            abortControllerRef.current = null;
        }
    }, [item.id, refresh, displayName]);
    
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
        email: () => {
            if (displayEmail) {
                window.location.href = `mailto:${displayEmail}`;
            } else {
                toast.error('No email address available');
            }
        },
        call: () => {
            if (displayPhone) {
                window.location.href = `tel:${displayPhone}`;
            } else {
                toast.error('No phone number available');
            }
        },
        notes: () => toast.info(`Notes for ${displayName} - Feature coming soon`),
        shortlist: () => move('shortlist', { silent: true }),
    }), [displayPhone, displayEmail, displayName, move]);
    
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
    
    const handleMouseEnter = useCallback(() => {
        if (readOnly || bulkMode) return;
        hoverTimeoutRef.current = setTimeout(() => {
            setShowActionRail(true);
        }, 150);
    }, [readOnly, bulkMode]);
    
    const handleMouseLeave = useCallback(() => {
        if (hoverTimeoutRef.current) {
            clearTimeout(hoverTimeoutRef.current);
        }
        setShowActionRail(false);
    }, []);
    
    // Touch device detection for future long-press menu (ISSUE 8)
    const isTouchDevice = useMemo(() => {
        return 'ontouchstart' in window || navigator.maxTouchPoints > 0;
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
            }
        }
        
        document.addEventListener("mousedown", handleClickOutside);
        document.addEventListener("keydown", handleEsc);
        
        return () => {
            document.removeEventListener("mousedown", handleClickOutside);
            document.removeEventListener("keydown", handleEsc);
        };
    }, [showMoreActions]);
    
    // Cleanup on unmount
    useEffect(() => {
        return () => {
            if (abortControllerRef.current) {
                abortControllerRef.current.abort();
            }
            if (hoverTimeoutRef.current) {
                clearTimeout(hoverTimeoutRef.current);
            }
        };
    }, []);
    
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
        "group relative rounded-lg overflow-hidden",
        "transition-all duration-150 ease-out",
        "bg-white",
        "shadow-[0_1px_2px_rgba(0,0,0,0.04)]",
        "border",
        "min-h-[140px]", // Fixed minimum height for consistency (ISSUE 5)
        isSelected
            ? "border-black/20 ring-1 ring-black/10"
            : "border-black/[0.08]",
        readOnly
            ? ""
            : "hover:border-black/[0.12] hover:shadow-[0_2px_4px_rgba(0,0,0,0.04)]",
        moving ? "opacity-40 pointer-events-none" : "",
        isDragging ? "opacity-75 scale-[0.995]" : "",
        draggable ? "cursor-grab active:cursor-grabbing" : "",
    ].join(" ");
    
    // ============================================================================
    // BULK MODE RENDER
    // ============================================================================
    
    if (bulkMode) {
        return (
            <div
                data-testid={`pipeline-card-${item.id}`}
                onClick={() => onToggleSelect(item.id)}
                onKeyDown={handleKeyDown}
                draggable={draggable}
                onDragStart={handleDragStart}
                onDragEnd={handleDragEnd}
                className="group relative rounded-lg overflow-hidden transition-all duration-150 bg-[#fafafa] border border-black/[0.08] min-h-[108px] px-3 py-2.5 cursor-pointer hover:border-black/[0.12]"
                role="checkbox"
                aria-checked={isSelected}
                tabIndex={0}
            >
                <div className="flex items-center gap-2.5">
                    <div className="relative flex-shrink-0">
                        <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => onToggleSelect(item.id)}
                            onClick={(e) => e.stopPropagation()}
                            className="
                                w-4 h-4 rounded-[3px]
                                border border-black/30 bg-white
                                checked:bg-black checked:border-black
                                transition-all duration-100
                                cursor-pointer
                                focus:outline-none focus:ring-1 focus:ring-black/20
                            "
                            aria-label={`Select ${displayName}`}
                        />
                    </div>
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="md"
                    />
                    <div className="flex-1 min-w-0">
                        <p className="text-[13px] text-black/85 font-medium leading-[1.25] truncate">
                            {displayName}
                        </p>
                        {displayEmail && (
                            <p className="text-[9px] text-black/45 truncate mt-1">
                                {displayEmail}
                            </p>
                        )}
                    </div>
                </div>
            </div>
        );
    }
    
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
            className={shellClass}
            aria-label={`Talent: ${displayName}`}
        >
            <div className="p-3 space-y-2.5">
                {/* Row 1: Identity + Priority + Freshness */}
                <div className="flex items-start gap-2.5">
                    {/* Avatar - less dominant */}
                    <div className="flex-shrink-0 opacity-90">
                        <TalentAvatar
                            src={item.image_url}
                            name={displayName}
                            size="sm"
                        />
                    </div>
                    
                    <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between gap-2">
                            <p
                                className="text-[13px] text-black/85 font-medium leading-[1.25] truncate"
                                title={displayName}
                            >
                                {displayName}
                            </p>
                            {/* Priority Badge */}
                            {activePriority && (
                                <span
                                    className={`shrink-0 inline-flex items-center px-1.5 py-0.5 rounded text-[8px] font-medium border ${PRIORITY_VARIANTS[activePriority.variant]}`}
                                >
                                    {activePriority.label}
                                </span>
                            )}
                        </div>
                        
                        {/* IG or ID - subtle secondary info */}
                        {displayIg && (
                            <p className="text-[8px] text-black/45 truncate mt-0.5">
                                @{displayIg}
                            </p>
                        )}
                    </div>
                    
                    {/* Status Chip + Freshness Indicator */}
                    <div className="flex items-center gap-1.5 shrink-0">
                        {freshness && (
                            <span 
                                className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-black/[0.04]"
                                title={`Last activity: ${formatRelativeTime(item.updated_at || item.created_at)}`}
                            >
                                <span className={`w-1.5 h-1.5 rounded-full ${freshness.dot}`} />
                                <span className="text-[7px] text-black/45 uppercase tracking-wide">
                                    {freshness.label}
                                </span>
                            </span>
                        )}
                        {statusTone && (
                            <span
                                className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border ${statusTone.chip}`}
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
                </div>
                
                {/* Row 2: Compact Recruiter Metadata - using Lucide icons */}
                <div className="flex items-center justify-between text-[9px] text-black/55 border-t border-black/[0.04] pt-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="flex items-center gap-1">
                            <User className="w-2.5 h-2.5 opacity-50" />
                            <span>{recruiterName}</span>
                        </span>
                        <span className="w-px h-2 bg-black/10" />
                        <span className="flex items-center gap-1">
                            <Clock className="w-2.5 h-2.5 opacity-50" />
                            <span>{lastActivity || 'N/A'}</span>
                        </span>
                    </div>
                    <div className="flex items-center gap-2">
                        <span className={`px-1.5 py-0.5 rounded ${responseStatus === 'Responsive' ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-50 text-gray-500'}`}>
                            {responseStatus}
                        </span>
                        <span className={`px-1.5 py-0.5 rounded ${availability === 'Available' ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-50 text-gray-500'}`}>
                            {availability}
                        </span>
                    </div>
                </div>
                
                {/* Row 3: Stage Actions */}
                {!readOnly && visibleActions.length > 0 && (
                    <div 
                        className="flex flex-wrap items-center gap-1 pt-1 border-t border-black/[0.04]"
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
                                    px-2 py-1 rounded-md
                                    text-[8px] tracking-[0.08em] uppercase font-medium
                                    text-black/55 hover:text-black/80
                                    bg-black/[0.03] hover:bg-black/[0.06]
                                    border border-transparent hover:border-black/[0.08]
                                    transition-all duration-100
                                    disabled:opacity-40
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
                                        w-5 h-5 rounded-md
                                        text-black/40 hover:text-black/60
                                        hover:bg-black/[0.03]
                                        transition-colors duration-100
                                    "
                                >
                                    <MoreHorizontal className="w-3 h-3" />
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
            
            {/* Hover Action Rail - with responsive positioning (ISSUE 4 - improved) */}
            {!readOnly && !bulkMode && showActionRail && (
                <div 
                    className="absolute right-2 top-1/2 -translate-y-1/2 flex flex-col gap-1.5 bg-white/95 backdrop-blur-sm rounded-lg p-1.5 shadow-sm border border-black/[0.06] z-10"
                    style={{ maxHeight: 'calc(100% - 16px)' }}
                    onMouseEnter={() => {
                        if (hoverTimeoutRef.current) clearTimeout(hoverTimeoutRef.current);
                        setShowActionRail(true);
                    }}
                >
                    <button
                        onClick={() => move(nextStages[0])}
                        className="w-7 h-7 rounded-md flex items-center justify-center text-black/50 hover:text-black/80 hover:bg-black/[0.04] transition-colors"
                        title="Quick move"
                    >
                        <ArrowRight className="w-3.5 h-3.5" />
                    </button>
                    <button
                        onClick={quickActions.notes}
                        className="w-7 h-7 rounded-md flex items-center justify-center text-black/50 hover:text-black/80 hover:bg-black/[0.04] transition-colors"
                        title="Quick notes"
                    >
                        <FileText className="w-3.5 h-3.5" />
                    </button>
                    {displayPhone && (
                        <button
                            onClick={quickActions.whatsapp}
                            className="w-7 h-7 rounded-md flex items-center justify-center text-[#25D366] hover:bg-[#25D366]/10 transition-colors"
                            title="WhatsApp"
                        >
                            <MessageCircle className="w-3.5 h-3.5" />
                        </button>
                    )}
                    {displayEmail && (
                        <button
                            onClick={quickActions.email}
                            className="w-7 h-7 rounded-md flex items-center justify-center text-black/50 hover:text-black/80 hover:bg-black/[0.04] transition-colors"
                            title="Email"
                        >
                            <Mail className="w-3.5 h-3.5" />
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
                        onClick={quickActions.shortlist}
                        className="w-7 h-7 rounded-md flex items-center justify-center text-amber-600/60 hover:text-amber-600 hover:bg-amber-50 transition-colors"
                        title="Shortlist"
                    >
                        <Star className="w-3.5 h-3.5" />
                    </button>
                </div>
            )}
        </div>
    );
});

export default PipelineCard;
