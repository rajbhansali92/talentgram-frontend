import React, { useEffect, useState, useMemo } from "react";
import { useParams, Link } from "react-router-dom";
import { adminApi, getSubdomainUrl } from "@/lib/api";
import { toast } from "sonner";
import WhatsAppShareButton from "@/components/WhatsAppShareButton";
import { generateClientViewMessage } from "@/lib/whatsappShare";
import {
    ArrowLeft,
    ExternalLink,
    Copy,
    MessageCircle,
    Star,
    ThumbsUp,
    XCircle,
    HelpCircle,
    Download,
    Settings,
    Lock,
    ClipboardCheck,
    Flame,
    Thermometer,
    Activity,
    ChevronDown,
    ChevronUp,
    FileText,
    Calendar,
    Clock,
    User,
    Search,
    Play,
    CheckCircle,
} from "lucide-react";

const ACTION_META = {
    ask_for_test: {
        label: "Ask for Test",
        icon: ClipboardCheck,
        color: "text-amber-600",
        bg: "bg-amber-50 border-amber-100",
    },
    interested: {
        label: "Audition Approved",
        icon: ThumbsUp,
        color: "text-green-600",
        bg: "bg-green-50 border-green-100",
    },
    not_for_this: {
        label: "Does Not Work For This Project",
        icon: XCircle,
        color: "text-red-600",
        bg: "bg-red-50 border-red-100",
    },
    shortlist: {
        label: "Shortlist",
        icon: Star,
        color: "text-amber-600",
        bg: "bg-amber-50 border-amber-100",
    },
    lock: {
        label: "Lock",
        icon: Lock,
        color: "text-indigo-600",
        bg: "bg-indigo-50 border-indigo-100",
    },
    not_sure: {
        label: "Unsure",
        icon: HelpCircle,
        color: "text-black/45",
        bg: "bg-slate-50 border-slate-200",
    },
};

// Centralized date formatting
const formatDate = (dateString) => {
    if (!dateString) return "N/A";
    try {
        const d = new Date(dateString);
        return d.toLocaleDateString("en-US", {
            day: "numeric",
            month: "short",
            year: "numeric",
        });
    } catch {
        return "Invalid date";
    }
};

const formatTime = (dateString) => {
    if (!dateString) return "N/A";
    try {
        const d = new Date(dateString);
        return d.toLocaleTimeString("en-US", {
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
        });
    } catch {
        return "N/A";
    }
};

const formatDateTime = (dateString) => {
    if (!dateString) return "N/A";
    return `${formatDate(dateString)} at ${formatTime(dateString)}`;
};

export default function LinkResults() {
    const { id } = useParams();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    // Accordion state for talent rows
    const [expandedTalents, setExpandedTalents] = useState({});

    // Drill down states
    const [activeViewerEmail, setActiveViewerEmail] = useState(null);
    const [activeTalentId, setActiveTalentId] = useState(null);

    useEffect(() => {
        const fetchResults = async () => {
            try {
                setLoading(true);
                setError(null);
                const res = await adminApi.get(`/links/${id}/results`);
                setData(res.data);
            } catch (err) {
                console.error("Failed to fetch link results:", err);
                setError(err.message || "Failed to load results");
                toast.error("Failed to load results. Please try again.");
            } finally {
                setLoading(false);
            }
        };

        fetchResults();
    }, [id]);

    const subjects = data?.subjects || {};
    const url = data ? `${getSubdomainUrl("links")}/${data.link.slug}` : "";
    const viewers = data?.viewers || [];
    const downloads = data?.downloads || [];
    const actions = data?.actions || [];
    const events = data?.events || [];
    const actionHistory = data?.action_history || [];
    // WhatsApp share analytics (separate from downloads): created | dispatched | opened.
    const shares = data?.shares || [];
    const shareDispatches = shares.filter((s) => s.event === "dispatched");

    const summaryByTalent = {};
    if (data?.summary) {
        data.summary.forEach((s) => {
            summaryByTalent[s.talent_id] = s;
        });
    }

    // Helper to get talent name safely
    const getTalentName = (tid) => {
        if (tid === "all" || tid === "campaign") return "Entire Project";
        return subjects[tid]?.name || "Unnamed Talent";
    };

    // Helper to get description of download item
    const getDownloadItemDesc = (d) => {
        if (d.media_id === "zip:campaign_bundle") return "Entire Project ZIP";
        if (d.media_id === "zip:talent_folder") return "Full Talent Folder";
        
        // Lookup media in subjects
        const t = subjects[d.talent_id];
        if (t && t.media) {
            const m = t.media.find(x => x.id === d.media_id);
            if (m) {
                if (m.category === "intro_video") return "Introduction Video";
                if (m.category.startsWith("take")) {
                    const takeNum = m.category.split("_")[1] || "1";
                    return `Audition Take ${takeNum}`;
                }
                if (m.category === "portfolio" || m.category === "indian" || m.category === "western" || m.category === "image") {
                    return "Portfolio Image";
                }
                return m.category.replace("_", " ");
            }
        }
        return d.media_id || "Media File";
    };

    // Describe a WhatsApp share dispatch (it carries an inline media[] list).
    const getShareItemsDesc = (s) => {
        const names = (s.media || []).map((m) => m.name).filter(Boolean);
        if (names.length === 0) return `${s.file_count || 0} item(s)`;
        if (names.length <= 2) return names.join(", ");
        return `${names.slice(0, 2).join(", ")} +${names.length - 2} more`;
    };
    const shareMethodLabel = (method) =>
        method === "native_file_share" ? "Files" : "Secure links";

    // Calculate viewer sessions
    const viewerSessions = useMemo(() => {
        // Group events, downloads, and actions by session_id
        const sessionsMap = {};

        // 1. Initialize sessions from viewers (identifies)
        viewers.forEach(v => {
            const sid = v.session_id || `session-${v.created_at}`;
            if (!sessionsMap[sid]) {
                sessionsMap[sid] = {
                    session_id: sid,
                    viewer_name: v.viewer_name || "Guest",
                    viewer_email: v.viewer_email,
                    device: v.device || "Unknown",
                    browser: v.browser || "Unknown",
                    started_at: new Date(v.created_at),
                    ended_at: new Date(v.created_at),
                    events_count: 0,
                };
            }
        });

        // 2. Walk events to update ended_at and link session
        events.forEach(e => {
            const sid = e.session_id;
            if (!sid) return;
            const t = new Date(e.created_at);
            if (!sessionsMap[sid]) {
                sessionsMap[sid] = {
                    session_id: sid,
                    viewer_name: e.viewer_name || "Guest",
                    viewer_email: e.viewer_email || "Anonymous",
                    started_at: t,
                    ended_at: t,
                    events_count: 0,
                };
            }
            if (t < sessionsMap[sid].started_at) sessionsMap[sid].started_at = t;
            if (t > sessionsMap[sid].ended_at) sessionsMap[sid].ended_at = t;
            sessionsMap[sid].events_count++;
        });

        // 3. Walk downloads to update ended_at
        downloads.forEach(d => {
            const sid = d.session_id;
            if (!sid) return;
            const t = new Date(d.created_at);
            if (!sessionsMap[sid]) {
                sessionsMap[sid] = {
                    session_id: sid,
                    viewer_name: d.viewer_name || "Guest",
                    viewer_email: d.viewer_email,
                    started_at: t,
                    ended_at: t,
                    events_count: 0,
                };
            }
            if (t < sessionsMap[sid].started_at) sessionsMap[sid].started_at = t;
            if (t > sessionsMap[sid].ended_at) sessionsMap[sid].ended_at = t;
        });

        // 4. Walk action history
        actionHistory.forEach(a => {
            const sid = a.session_id;
            if (!sid) return;
            const t = new Date(a.created_at);
            if (!sessionsMap[sid]) {
                sessionsMap[sid] = {
                    session_id: sid,
                    viewer_name: a.viewer_name || "Guest",
                    viewer_email: a.viewer_email,
                    started_at: t,
                    ended_at: t,
                    events_count: 0,
                };
            }
            if (t < sessionsMap[sid].started_at) sessionsMap[sid].started_at = t;
            if (t > sessionsMap[sid].ended_at) sessionsMap[sid].ended_at = t;
        });

        return Object.values(sessionsMap).sort((a, b) => b.started_at - a.started_at);
    }, [viewers, events, downloads, actionHistory]);

    // Compute detailed talent stats
    const talentStats = useMemo(() => {
        const stats = {};

        // Initialize empty stats for every subject
        Object.keys(subjects).forEach(tid => {
            stats[tid] = {
                intro_views: 0,
                intro_completions: 0,
                intro_downloads: 0,
                take_views: {}, // take_1 -> count, etc.
                take_completions: {},
                take_downloads: {},
                image_views: 0,
                image_downloads: 0,
                folder_downloads: 0,
                total_views: 0,
            };
        });

        // Parse events
        events.forEach(e => {
            const tid = e.talent_id;
            if (!tid || !stats[tid]) return;

            if (e.event_type === "view_talent") {
                stats[tid].total_views++;
            } else if (e.event_type === "view_media" && e.media_id) {
                const t = subjects[tid];
                const m = t?.media?.find(x => x.id === e.media_id);
                if (m) {
                    if (m.category === "portfolio" || m.category === "indian" || m.category === "western" || m.category === "image") {
                        stats[tid].image_views++;
                    }
                }
            } else if (e.event_type === "watch_video" && e.media_id) {
                const t = subjects[tid];
                const m = t?.media?.find(x => x.id === e.media_id);
                if (m) {
                    const isIntro = m.category === "intro_video";
                    const isCompletion = e.video_action === "completion";
                    
                    if (isIntro) {
                        if (isCompletion) stats[tid].intro_completions++;
                        else if (e.video_action === "play") stats[tid].intro_views++;
                    } else if (m.category.startsWith("take")) {
                        const takeCat = m.category;
                        if (isCompletion) {
                            stats[tid].take_completions[takeCat] = (stats[tid].take_completions[takeCat] || 0) + 1;
                        } else if (e.video_action === "play") {
                            stats[tid].take_views[takeCat] = (stats[tid].take_views[takeCat] || 0) + 1;
                        }
                    }
                }
            }
        });

        // Parse downloads
        downloads.forEach(d => {
            const tid = d.talent_id;
            if (!tid || !stats[tid]) return;

            if (d.media_id === "zip:talent_folder") {
                stats[tid].folder_downloads++;
            } else {
                const t = subjects[tid];
                const m = t?.media?.find(x => x.id === d.media_id);
                if (m) {
                    if (m.category === "intro_video") {
                        stats[tid].intro_downloads++;
                    } else if (m.category.startsWith("take")) {
                        const takeCat = m.category;
                        stats[tid].take_downloads[takeCat] = (stats[tid].take_downloads[takeCat] || 0) + 1;
                    } else {
                        stats[tid].image_downloads++;
                    }
                }
            }
        });

        return stats;
    }, [events, downloads, subjects]);

    // Drill down viewer data
    const viewerDrillDownData = useMemo(() => {
        if (!activeViewerEmail) return null;
        const vEvents = events.filter(e => e.viewer_email === activeViewerEmail);
        const vDownloads = downloads.filter(d => d.viewer_email === activeViewerEmail);
        const vActions = actionHistory.filter(a => a.viewer_email === activeViewerEmail);
        const vSessions = viewerSessions.filter(s => s.viewer_email === activeViewerEmail);
        
        // Count unique profiles viewed
        const viewedProfiles = new Set(vEvents.filter(e => e.event_type === "view_talent" && e.talent_id).map(e => e.talent_id));

        // Combined chronological history for viewer
        const timeline = [];
        vSessions.forEach(s => {
            timeline.push({ type: "session_start", time: s.started_at, detail: `Started session on ${s.device} (${s.browser})` });
        });
        vEvents.forEach(e => {
            let actionText = "";
            let detail = "";
            if (e.event_type === "open") {
                actionText = "Opened Link";
                detail = "Started review session";
            } else if (e.event_type === "view_talent") {
                actionText = "Viewed Profile";
                detail = getTalentName(e.talent_id);
            } else if (e.event_type === "view_media") {
                actionText = "Portfolio View";
                detail = `Opened image for ${getTalentName(e.talent_id)}`;
            } else if (e.event_type === "watch_video") {
                const act = e.video_action === "play" ? "Played Video" : (e.video_action === "completion" ? "Completed Video" : "Watched Video");
                detail = `${act} for ${getTalentName(e.talent_id)}`;
            }
            timeline.push({ type: "event", time: new Date(e.created_at), actionText, detail });
        });
        vDownloads.forEach(d => {
            timeline.push({ type: "download", time: new Date(d.created_at), actionText: "Downloaded", detail: `${getDownloadItemDesc(d)} (${getTalentName(d.talent_id)})` });
        });
        shareDispatches
            .filter(s => s.viewer_email === activeViewerEmail)
            .forEach(s => {
                timeline.push({
                    type: "share",
                    time: new Date(s.created_at),
                    actionText: "Shared via WhatsApp",
                    detail: `${getShareItemsDesc(s)} (${getTalentName(s.talent_id)}) · ${shareMethodLabel(s.share_method)} · ${s.file_count || (s.media || []).length} file(s)`,
                });
            });
        vActions.forEach(a => {
            const meta = ACTION_META[a.action] || { label: a.action || "Cleared Action" };
            timeline.push({ type: "action", time: new Date(a.created_at), actionText: "Action", detail: `Marked ${meta.label} for ${getTalentName(a.talent_id)}` });
        });

        timeline.sort((a, b) => b.time - a.time);

        return {
            email: activeViewerEmail,
            name: vSessions[0]?.viewer_name || "Viewer",
            sessionsCount: vSessions.length,
            profilesViewedCount: viewedProfiles.size,
            downloadsCount: vDownloads.length,
            sharesCount: shareDispatches.filter(s => s.viewer_email === activeViewerEmail).length,
            timeline,
        };
    }, [activeViewerEmail, events, downloads, shareDispatches, actionHistory, viewerSessions]);

    // Drill down talent data
    const talentDrillDownData = useMemo(() => {
        if (!activeTalentId) return null;
        const tEvents = events.filter(e => e.talent_id === activeTalentId);
        const tDownloads = downloads.filter(d => d.talent_id === activeTalentId);
        const tActions = actionHistory.filter(a => a.talent_id === activeTalentId);
        const stats = talentStats[activeTalentId] || {};

        // Chronological interactions log
        const timeline = [];
        tEvents.forEach(e => {
            let act = "";
            let detail = "";
            if (e.event_type === "view_talent") {
                act = "Profile Viewed";
            } else if (e.event_type === "view_media") {
                act = "Portfolio Image Opened";
            } else if (e.event_type === "watch_video") {
                act = e.video_action === "play" ? "Played Video" : (e.video_action === "completion" ? "Completed Video" : "Watched Video");
            }
            timeline.push({ time: new Date(e.created_at), viewer: e.viewer_name || e.viewer_email || "Client", act, detail });
        });
        tDownloads.forEach(d => {
            timeline.push({ time: new Date(d.created_at), viewer: d.viewer_name || d.viewer_email, act: `Downloaded ${getDownloadItemDesc(d)}` });
        });
        tActions.forEach(a => {
            const meta = ACTION_META[a.action] || { label: a.action || "Cleared Action" };
            timeline.push({ time: new Date(a.created_at), viewer: a.viewer_name || a.viewer_email, act: `Marked as ${meta.label}` });
        });

        timeline.sort((a, b) => b.time - a.time);

        return {
            id: activeTalentId,
            name: getTalentName(activeTalentId),
            views: stats.total_views || 0,
            stats,
            actions: tActions,
            timeline,
        };
    }, [activeTalentId, events, downloads, actionHistory, talentStats]);

    // CSV/Excel/PDF Export functions
    const exportToCSV = (format = "csv") => {
        const sep = format === "excel" ? "\t" : ",";
        const extension = format === "excel" ? "xls" : "csv";
        let content = "\uFEFF"; // Unicode BOM for Excel compatibility

        // 1. Sessions tab
        content += "Viewer Sessions\n";
        content += ["Viewer", "Email", "Date", "Started", "Ended", "Duration"].join(sep) + "\n";
        viewerSessions.forEach(s => {
            const duration = Math.round((s.ended_at - s.started_at) / 60000);
            content += [
                s.viewer_name,
                s.viewer_email,
                formatDate(s.started_at),
                formatTime(s.started_at),
                formatTime(s.ended_at),
                `${duration} mins`
            ].map(x => `"${x}"`).join(sep) + "\n";
        });

        // 2. Profile views
        content += "\nProfile Views & Video Analytics\n";
        content += ["Talent", "Profile Views", "Intro Views", "Intro Completions", "Intro Downloads", "Folder Downloads"].join(sep) + "\n";
        Object.keys(subjects).forEach(tid => {
            const stats = talentStats[tid] || {};
            content += [
                getTalentName(tid),
                stats.total_views,
                stats.intro_views,
                stats.intro_completions,
                stats.intro_downloads,
                stats.folder_downloads
            ].map(x => `"${x}"`).join(sep) + "\n";
        });

        // 3. Downloads
        content += "\nDownloads Log\n";
        content += ["Viewer", "Email", "Talent", "Item", "Date", "Time"].join(sep) + "\n";
        downloads.forEach(d => {
            content += [
                d.viewer_name,
                d.viewer_email,
                getTalentName(d.talent_id),
                getDownloadItemDesc(d),
                formatDate(d.created_at),
                formatTime(d.created_at)
            ].map(x => `"${x}"`).join(sep) + "\n";
        });

        // 3b. WhatsApp Shares (separate from downloads)
        content += "\nWhatsApp Shares Log\n";
        content += ["Viewer", "Email", "Talent", "Media", "Method", "Files", "Date", "Time"].join(sep) + "\n";
        shareDispatches.forEach(s => {
            content += [
                s.viewer_name,
                s.viewer_email,
                getTalentName(s.talent_id),
                getShareItemsDesc(s),
                shareMethodLabel(s.share_method),
                s.file_count || (s.media || []).length,
                formatDate(s.created_at),
                formatTime(s.created_at)
            ].map(x => `"${x}"`).join(sep) + "\n";
        });

        // 4. Actions
        content += "\nTalent Actions History\n";
        content += ["Viewer", "Email", "Talent", "Action", "Date", "Time"].join(sep) + "\n";
        actionHistory.forEach(a => {
            const meta = ACTION_META[a.action] || { label: a.action || "Cleared" };
            content += [
                a.viewer_name,
                a.viewer_email,
                getTalentName(a.talent_id),
                meta.label,
                formatDate(a.created_at),
                formatTime(a.created_at)
            ].map(x => `"${x}"`).join(sep) + "\n";
        });

        const blob = new Blob([content], { type: `text/${format === "excel" ? "tab-separated-values" : "csv"};charset=utf-8;` });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.setAttribute("download", `${data.link.title}_Activity_Log.${extension}`);
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        toast.success(`Activity log exported as ${format.toUpperCase()}`);
    };

    const handlePrintPDF = () => {
        window.print();
    };

    const copyLink = async () => {
        const success = await copyToClipboard(url);
        if (success) {
            toast.success("Link copied");
        } else {
            toast.error("Failed to copy link. Please copy manually.");
        }
    };

    const whatsApp = () => {
        window.open(generateClientViewMessage(data.link.title, url), "_blank");
    };

    if (error) {
        return (
            <div className="p-6 md:p-10 max-w-7xl mx-auto">
                <Link
                    to="/admin/links"
                    className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6 transition-colors duration-150"
                >
                    <ArrowLeft className="w-3 h-3" /> Back to links
                </Link>
                <div className="bg-red-50 border border-red-200 rounded-xl p-8 text-center">
                    <div className="text-red-600 mb-2">Failed to load results</div>
                    <div className="text-sm text-red-500 mb-4">{error}</div>
                    <button
                        onClick={() => window.location.reload()}
                        className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm hover:bg-red-700 transition-colors"
                    >
                        Try Again
                    </button>
                </div>
            </div>
        );
    }

    const toggleTalentExpand = (tid) => {
        setExpandedTalents(prev => ({ ...prev, [tid]: !prev[tid] }));
    };

    const totalDownloads = downloads.length;
    const totalShares = shareDispatches.length;
    const analytics = data?.link?.analytics || {};
    const trackingViews = analytics.total_views !== undefined ? analytics.total_views : (data?.view_count || data?.link?.view_count || 0);
    const trackingUniq = analytics.unique_views !== undefined ? (analytics.unique_views.length || 0) : (data?.unique_viewers || data?.link?.unique_viewers || 0);
    const viewedTalentsCount = Object.keys(analytics.viewed_talents || {}).length;
    const totalWatchSeconds = Object.values(analytics.watch_durations || {}).reduce((a, b) => a + b, 0);
    const watchMinutes = Math.round(totalWatchSeconds / 60);

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto space-y-10 print:p-0 print:max-w-none"
            data-testid="link-results-page"
        >
            {/* Print Header Style block to hide navigation / sidebars when printing */}
            <style dangerouslySetInnerHTML={{ __html: `
                @media print {
                    body * {
                        visibility: hidden;
                    }
                    #print-section, #print-section * {
                        visibility: visible;
                    }
                    #print-section {
                        position: absolute;
                        left: 0;
                        top: 0;
                        width: 100%;
                    }
                }
            `}} />

            <div id="print-section" className="space-y-8">
                <div className="flex items-start justify-between flex-wrap gap-6 border-b border-black/[0.05] pb-6 print:border-0 print:pb-0">
                    <div>
                        <Link
                            to="/admin/links"
                            className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-4 transition-colors duration-150 print:hidden"
                        >
                            <ArrowLeft className="w-3 h-3" /> Back to links
                        </Link>
                        <p className="eyebrow mb-2">Results & Client Activity</p>
                        <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90 mb-2">
                            {data?.link?.title}
                        </h1>
                        <p className="text-xs text-black/45 font-mono print:hidden">
                            {url}
                        </p>
                    </div>
                    <div className="flex gap-2 flex-wrap print:hidden">
                        <a
                            href={`/l/${data?.link?.slug}`}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-2 px-4 py-2 border border-black/[0.08] hover:border-black/[0.16] rounded-lg text-xs font-medium text-black/70 hover:text-black transition-colors"
                        >
                            <ExternalLink className="w-3.5 h-3.5" /> Open
                        </a>
                        <button
                            onClick={copyLink}
                            className="inline-flex items-center gap-2 px-4 py-2 border border-black/[0.08] hover:border-black/[0.16] rounded-lg text-xs font-medium text-black/70 hover:text-black transition-colors"
                        >
                            <Copy className="w-3.5 h-3.5" /> Copy
                        </button>
                        <WhatsAppShareButton
                            onClick={whatsApp}
                        />
                        <Link
                            to={`/admin/links/${id}/edit`}
                            className="inline-flex items-center gap-2 px-4 py-2 border border-black/[0.08] hover:border-black/[0.16] rounded-lg text-xs font-medium text-black/70 hover:text-black transition-colors"
                        >
                            <Settings className="w-3.5 h-3.5" /> Edit
                        </Link>
                        
                        {/* Exports dropdown / buttons */}
                        <div className="flex border border-black/[0.08] rounded-lg overflow-hidden">
                            <button
                                onClick={() => exportToCSV("csv")}
                                className="px-3 py-2 bg-white hover:bg-slate-50 text-xs font-medium border-r border-black/[0.08] text-black/75 transition-colors"
                                title="Export to CSV"
                            >
                                CSV
                            </button>
                            <button
                                onClick={() => exportToCSV("excel")}
                                className="px-3 py-2 bg-white hover:bg-slate-50 text-xs font-medium border-r border-black/[0.08] text-black/75 transition-colors"
                                title="Export to Excel"
                            >
                                Excel
                            </button>
                            <button
                                onClick={handlePrintPDF}
                                className="px-3 py-2 bg-white hover:bg-slate-50 text-xs font-medium text-black/75 transition-colors"
                                title="Export to PDF"
                            >
                                PDF
                            </button>
                        </div>
                    </div>
                </div>

                {loading ? (
                    <LoadingSkeleton />
                ) : (
                    <>
                        {/* Summary Metrics */}
                        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
                            {[
                                { label: "Total Views", value: trackingViews },
                                { label: "Unique Viewers", value: trackingUniq },
                                { label: "Profiles Viewed", value: viewedTalentsCount },
                                { label: "Video Watched", value: `${watchMinutes} min` },
                                { label: "Total Actions", value: actions.filter((a) => a.action).length },
                                { label: "Downloads", value: totalDownloads },
                                { label: "WhatsApp Shares", value: totalShares },
                            ].map((s) => (
                                <div
                                    key={s.label}
                                    className="bg-white border border-black/[0.08] rounded-xl p-5 hover:border-black/[0.12] transition-colors"
                                >
                                    <div className="font-display text-2xl md:text-3xl font-semibold text-black/85">
                                        {s.value}
                                    </div>
                                    <div className="text-[10px] text-black/45 tracking-widest uppercase mt-2 font-medium">
                                        {s.label}
                                    </div>
                                </div>
                            ))}
                        </div>

                        {/* Talent Breakdown List */}
                        <section className="bg-white border border-black/[0.08] rounded-xl overflow-hidden shadow-sm">
                            <div className="px-6 py-4 border-b border-black/[0.06] flex items-center justify-between">
                                <p className="eyebrow">Talent Breakdown</p>
                                <p className="text-xs text-black/45">
                                    {data?.summary?.length || 0} talents
                                </p>
                            </div>
                            {!data?.summary || data.summary.length === 0 ? (
                                <div className="p-8 text-center text-black/45 text-sm">
                                    No viewer feedback recorded yet.
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.06]">
                                    {data.summary.map((s) => {
                                        const t = subjects[s.talent_id];
                                        const isExpanded = !!expandedTalents[s.talent_id];
                                        const stats = talentStats[s.talent_id] || {};
                                        
                                        // Retrieve all take items
                                        const videoTakes = t?.media?.filter(m => m.category.startsWith("take")) || [];

                                        return (
                                            <div
                                                key={s.talent_id}
                                                className="p-6 transition-colors duration-150 hover:bg-slate-50/50"
                                            >
                                                <div className="flex items-start justify-between flex-wrap gap-4">
                                                    <div 
                                                        className="cursor-pointer" 
                                                        onClick={() => setActiveTalentId(s.talent_id)}
                                                        title="Click to view talent drilldown log"
                                                    >
                                                        <h3 className="font-display text-lg text-black/85 font-medium hover:text-indigo-600 transition-colors flex items-center gap-2">
                                                            {t?.name || "Unnamed Talent"}
                                                            <span className="text-xs font-normal text-slate-400 font-mono">(Click to drill down)</span>
                                                        </h3>
                                                        <div className="text-[11px] text-black/45 mt-1 font-medium">
                                                            {t?.source === "submission" ? "Audition submission" : "Talent Profile"} · {stats.total_views || 0} views
                                                        </div>
                                                    </div>
                                                    
                                                    <div className="flex items-center gap-6">
                                                        <div className="flex gap-4 flex-wrap text-xs">
                                                            {Object.entries(ACTION_META).map(([k, m]) => (
                                                                <div key={k} className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-black/[0.04] bg-white">
                                                                    <m.icon className={`w-3.5 h-3.5 ${m.color}`} />
                                                                    <span className="font-mono font-semibold text-black/80">
                                                                        {summaryByTalent[s.talent_id]?.[k] ?? s[k] ?? 0}
                                                                    </span>
                                                                    <span className="text-black/45 text-[10px]">{m.label}</span>
                                                                </div>
                                                            ))}
                                                        </div>
                                                        <button
                                                            onClick={() => toggleTalentExpand(s.talent_id)}
                                                            className="p-1.5 hover:bg-black/[0.04] rounded-md transition-colors text-black/45 hover:text-black/80"
                                                        >
                                                            {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                                                        </button>
                                                    </div>
                                                </div>

                                                {/* Expanded row details (Per-Talent Analytics) */}
                                                {isExpanded && (
                                                    <div className="mt-6 border-t border-black/[0.05] pt-5 space-y-4 text-xs text-black/75">
                                                        <h4 className="font-semibold text-black/90 uppercase tracking-wider text-[10px]">Media Interaction Audit</h4>
                                                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                                                            
                                                            {/* Intro Video */}
                                                            <div className="bg-slate-50 border border-black/[0.04] rounded-lg p-3 space-y-2">
                                                                <div className="font-semibold text-black/85 flex items-center gap-1.5">
                                                                    <Play className="w-3 h-3 text-indigo-500" /> Introduction Video
                                                                </div>
                                                                <div className="space-y-1 font-mono text-[11px] text-black/60">
                                                                    <div>Plays: <span className="font-bold text-black/80">{stats.intro_views}</span></div>
                                                                    <div>Completions: <span className="font-bold text-black/80">{stats.intro_completions}</span></div>
                                                                    <div>Downloads: <span className="font-bold text-black/80">{stats.intro_downloads}</span></div>
                                                                </div>
                                                            </div>

                                                            {/* Video Takes */}
                                                            {videoTakes.map((take, idx) => {
                                                                const views = stats.take_views[take.category] || 0;
                                                                const completions = stats.take_completions[take.category] || 0;
                                                                const downloads = stats.take_downloads[take.category] || 0;
                                                                return (
                                                                    <div key={take.id} className="bg-slate-50 border border-black/[0.04] rounded-lg p-3 space-y-2">
                                                                        <div className="font-semibold text-black/85 flex items-center gap-1.5">
                                                                            <Play className="w-3 h-3 text-indigo-500" /> Audition Take {idx + 1}
                                                                        </div>
                                                                        <div className="space-y-1 font-mono text-[11px] text-black/60">
                                                                            <div>Plays: <span className="font-bold text-black/80">{views}</span></div>
                                                                            <div>Completions: <span className="font-bold text-black/80">{completions}</span></div>
                                                                            <div>Downloads: <span className="font-bold text-black/80">{downloads}</span></div>
                                                                        </div>
                                                                    </div>
                                                                );
                                                            })}

                                                            {/* Images */}
                                                            <div className="bg-slate-50 border border-black/[0.04] rounded-lg p-3 space-y-2">
                                                                <div className="font-semibold text-black/85 flex items-center gap-1.5">
                                                                    <FileText className="w-3 h-3 text-indigo-500" /> Portfolio Images
                                                                </div>
                                                                <div className="space-y-1 font-mono text-[11px] text-black/60">
                                                                    <div>Opened: <span className="font-bold text-black/80">{stats.image_views}</span></div>
                                                                    <div>Downloaded: <span className="font-bold text-black/80">{stats.image_downloads}</span></div>
                                                                </div>
                                                            </div>

                                                            {/* ZIP folder */}
                                                            <div className="bg-slate-50 border border-black/[0.04] rounded-lg p-3 space-y-2">
                                                                <div className="font-semibold text-black/85 flex items-center gap-1.5">
                                                                    <Download className="w-3 h-3 text-indigo-500" /> Full Folder ZIP
                                                                </div>
                                                                <div className="space-y-1 font-mono text-[11px] text-black/60">
                                                                    <div>Downloaded: <span className="font-bold text-black/80">{stats.folder_downloads}</span></div>
                                                                </div>
                                                            </div>
                                                        </div>

                                                        {/* Action Buttons history for this talent */}
                                                        <div className="mt-4 pt-3 border-t border-black/[0.04]">
                                                            <h5 className="font-semibold text-[10px] uppercase text-black/60 tracking-wider mb-2">Viewer Action History</h5>
                                                            {actionHistory.filter(a => a.talent_id === s.talent_id).length === 0 ? (
                                                                <div className="text-[11px] text-black/35 font-mono">No action buttons pressed yet.</div>
                                                            ) : (
                                                                <div className="flex flex-wrap gap-2 items-center text-[11px]">
                                                                    {actionHistory.filter(a => a.talent_id === s.talent_id).map((ah, ahIdx) => {
                                                                        const am = ACTION_META[ah.action] || { label: ah.action || "Cleared", color: "text-slate-500", bg: "bg-slate-50 border-slate-200" };
                                                                        return (
                                                                            <span key={ah.id || ahIdx} className={`inline-flex items-center gap-1 px-2.5 py-1 rounded border ${am.bg} ${am.color} font-medium`}>
                                                                                <span>{formatTime(ah.created_at)}</span>
                                                                                <span className="font-bold">{ah.viewer_name || "Client"}</span>
                                                                                <span>{am.label}</span>
                                                                            </span>
                                                                        );
                                                                    })}
                                                                </div>
                                                            )}
                                                        </div>
                                                    </div>
                                                )}

                                                {/* Comments */}
                                                {s.comments?.length > 0 && (
                                                    <div className="mt-4 space-y-2 max-h-96 overflow-y-auto pl-4 border-l-2 border-slate-200">
                                                        {s.comments.map((c, i) => (
                                                            <div
                                                                key={`${c.viewer_email}-${c.updated_at || i}`}
                                                                className="text-sm"
                                                            >
                                                                <div className="text-black/80">"{c.comment}"</div>
                                                                <div className="text-[10px] text-black/45 mt-1">
                                                                    — {c.viewer_name} ({c.viewer_email})
                                                                </div>
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </section>

                        {/* Login Session History Section */}
                        <section className="bg-white border border-black/[0.08] rounded-xl overflow-hidden shadow-sm">
                            <div className="px-6 py-4 border-b border-black/[0.06] flex items-center justify-between">
                                <p className="eyebrow">Viewer Login & Session History</p>
                                <p className="text-xs text-black/45">{viewerSessions.length} sessions</p>
                            </div>
                            {viewerSessions.length === 0 ? (
                                <div className="p-6 text-black/45 text-sm text-center">No sessions recorded yet.</div>
                            ) : (
                                <div className="overflow-x-auto">
                                    <table className="w-full text-left text-xs border-collapse">
                                        <thead>
                                            <tr className="bg-slate-50 border-b border-black/[0.04] text-[10px] uppercase font-semibold text-black/55 tracking-wider">
                                                <th className="px-6 py-3">Viewer</th>
                                                <th className="px-6 py-3">Date</th>
                                                <th className="px-6 py-3">Time</th>
                                                <th className="px-6 py-3">Session Started</th>
                                                <th className="px-6 py-3">Session Ended</th>
                                                <th className="px-6 py-3">Duration</th>
                                                <th className="px-6 py-3">Actions Count</th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-black/[0.04]">
                                            {viewerSessions.map((session, idx) => {
                                                const duration = Math.round((session.ended_at - session.started_at) / 60000);
                                                return (
                                                    <tr 
                                                        key={session.session_id || idx} 
                                                        className="hover:bg-slate-50/50 cursor-pointer"
                                                        onClick={() => setActiveViewerEmail(session.viewer_email)}
                                                        title="Click to view client drilldown details"
                                                    >
                                                        <td className="px-6 py-4">
                                                            <div className="font-semibold text-indigo-600 hover:underline">{session.viewer_name}</div>
                                                            <div className="text-[10px] text-black/35 font-mono">{session.viewer_email}</div>
                                                        </td>
                                                        <td className="px-6 py-4 font-medium text-black/75">{formatDate(session.started_at)}</td>
                                                        <td className="px-6 py-4 text-black/60">{formatTime(session.started_at)}</td>
                                                        <td className="px-6 py-4 text-black/60">{formatDateTime(session.started_at)}</td>
                                                        <td className="px-6 py-4 text-black/60">{session.events_count > 0 ? formatDateTime(session.ended_at) : "In progress / Opened"}</td>
                                                        <td className="px-6 py-4 font-semibold text-black/80">{session.events_count > 0 ? `${duration} mins` : "< 1 min"}</td>
                                                        <td className="px-6 py-4 text-black/60">{session.events_count} events</td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                </div>
                            )}
                        </section>

                        {/* Chronological Activity Timeline */}
                        <section className="bg-white border border-black/[0.08] rounded-xl overflow-hidden shadow-sm">
                            <div className="px-6 py-4 border-b border-black/[0.06] flex items-center justify-between">
                                <p className="eyebrow">Client Activity Timeline</p>
                                <p className="text-xs text-black/45">Chronological log</p>
                            </div>
                            {events.length === 0 ? (
                                <div className="p-8 text-center text-black/45 text-sm">
                                    No client activity recorded yet.
                                </div>
                            ) : (
                                <div className="p-6 max-h-[600px] overflow-y-auto space-y-4">
                                    {events.map((e, idx) => {
                                        let actionDesc = "";
                                        let target = "";

                                        if (e.event_type === "open") {
                                            actionDesc = "logged in";
                                        } else if (e.event_type === "view_talent") {
                                            actionDesc = "Viewed Profile";
                                            target = getTalentName(e.talent_id);
                                        } else if (e.event_type === "view_media") {
                                            actionDesc = "Opened Portfolio Image";
                                            target = getTalentName(e.talent_id);
                                        } else if (e.event_type === "watch_video") {
                                            const va = e.video_action === "play" ? "Played" : (e.video_action === "completion" ? "Completed" : "Watched");
                                            const t = subjects[e.talent_id];
                                            const m = t?.media?.find(x => x.id === e.media_id);
                                            const name = m?.category === "intro_video" ? "Introduction Video" : "Audition Video";
                                            actionDesc = `${va} ${name}`;
                                            target = getTalentName(e.talent_id);
                                        }

                                        return (
                                            <div key={e.id || idx} className="flex items-start gap-4 text-xs font-mono py-1.5 border-b border-slate-50 last:border-0">
                                                <div className="text-slate-400 shrink-0 w-24">{formatDate(e.created_at)}</div>
                                                <div className="text-slate-400 shrink-0 w-16">{formatTime(e.created_at)}</div>
                                                <div className="flex-1">
                                                    <span className="font-bold text-slate-800">{e.viewer_name || "Guest"}</span>{" "}
                                                    <span className="text-slate-500 font-normal">{actionDesc}</span>{" "}
                                                    {target && <span className="font-bold text-indigo-600">{target}</span>}
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </section>

                        {/* Viewers & Better Download Log */}
                        <section className="grid md:grid-cols-2 gap-6">
                            
                            {/* Viewers list */}
                            <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden shadow-sm">
                                <div className="px-6 py-4 border-b border-black/[0.06] flex items-center gap-2">
                                    <p className="eyebrow">Viewers Drill Down</p>
                                </div>
                                {viewers.length === 0 ? (
                                    <div className="p-6 text-black/45 text-sm">
                                        No viewers yet
                                    </div>
                                ) : (
                                    <div className="divide-y divide-black/[0.06] max-h-96 overflow-y-auto">
                                        {Array.from(new Set(viewers.map(v => v.viewer_email))).map((email) => {
                                            const v = viewers.find(x => x.viewer_email === email);
                                            return (
                                                <div
                                                    key={email}
                                                    className="px-6 py-4 text-sm transition-colors duration-150 hover:bg-slate-50 cursor-pointer"
                                                    onClick={() => setActiveViewerEmail(email)}
                                                    title="Click to drill down on viewer details"
                                                >
                                                    <div className="font-semibold text-indigo-600 hover:underline">
                                                        {v.viewer_name}
                                                    </div>
                                                    <div className="text-xs text-black/45 mt-0.5 font-mono">
                                                        {v.viewer_email}
                                                    </div>
                                                    <div className="text-[10px] text-black/35 mt-2">
                                                        First identified: {formatDateTime(v.created_at)}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>

                            {/* Better Download Log */}
                            <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden shadow-sm">
                                <div className="px-6 py-4 border-b border-black/[0.06] flex items-center gap-2">
                                    <Download className="w-3.5 h-3.5 text-black/45" />
                                    <p className="eyebrow">Richer Download Log</p>
                                </div>
                                {downloads.length === 0 ? (
                                    <div className="p-6 text-black/45 text-sm">
                                        No downloads yet
                                    </div>
                                ) : (
                                    <div className="divide-y divide-black/[0.06] max-h-96 overflow-y-auto">
                                        {downloads.map((d) => {
                                            const itemDesc = getDownloadItemDesc(d);
                                            const talentName = getTalentName(d.talent_id);
                                            return (
                                                <div
                                                    key={d.id}
                                                    className="px-6 py-4 text-sm transition-colors duration-150 hover:bg-slate-50"
                                                >
                                                    <div className="flex items-center justify-between">
                                                        <span className="font-semibold text-black/85">{d.viewer_name}</span>
                                                        <span className="text-[10px] font-mono text-black/35">{formatTime(d.created_at)}</span>
                                                    </div>
                                                    <div className="text-xs text-black/60 mt-1">
                                                        Downloaded <span className="font-bold text-slate-800">{itemDesc}</span> for{" "}
                                                        <span className="font-semibold text-indigo-600">{talentName}</span>
                                                    </div>
                                                    <div className="text-[10px] text-black/35 mt-1.5">{formatDate(d.created_at)}</div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>
                        </section>
                    </>
                )}
            </div>

            {/* Viewer Drill-Down Modal */}
            {activeViewerEmail && viewerDrillDownData && (
                <div className="fixed inset-0 bg-black/45 flex items-center justify-center p-4 z-50 overflow-y-auto">
                    <div className="bg-white rounded-2xl max-w-2xl w-full p-6 space-y-6 max-h-[85vh] overflow-y-auto shadow-2xl relative">
                        <button
                            onClick={() => setActiveViewerEmail(null)}
                            className="absolute top-4 right-4 p-2 hover:bg-slate-100 rounded-full transition-colors font-semibold text-slate-500"
                        >
                            ✕
                        </button>
                        
                        <div className="space-y-1">
                            <h2 className="text-2xl font-bold font-display text-black/85">{viewerDrillDownData.name}</h2>
                            <p className="text-xs text-slate-500 font-mono">{viewerDrillDownData.email}</p>
                        </div>

                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-center">
                            <div className="bg-slate-50 border border-slate-100 rounded-xl p-4">
                                <div className="text-xl font-bold text-slate-800">{viewerDrillDownData.sessionsCount}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Sessions</div>
                            </div>
                            <div className="bg-slate-50 border border-slate-100 rounded-xl p-4">
                                <div className="text-xl font-bold text-slate-800">{viewerDrillDownData.profilesViewedCount}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Profiles Viewed</div>
                            </div>
                            <div className="bg-slate-50 border border-slate-100 rounded-xl p-4">
                                <div className="text-xl font-bold text-slate-800">{viewerDrillDownData.downloadsCount}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Downloads</div>
                            </div>
                            <div className="bg-slate-50 border border-slate-100 rounded-xl p-4">
                                <div className="text-xl font-bold text-slate-800">{viewerDrillDownData.sharesCount}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">WA Shares</div>
                            </div>
                        </div>

                        <div className="space-y-3">
                            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400">Chronological Activity Log</h3>
                            <div className="border-l border-slate-100 pl-4 space-y-4 max-h-60 overflow-y-auto">
                                {viewerDrillDownData.timeline.map((item, idx) => (
                                    <div key={idx} className="relative text-xs">
                                        <div className="absolute -left-[21px] top-1 w-2.5 h-2.5 bg-indigo-500 border border-white rounded-full" />
                                        <div className="text-[10px] text-slate-400 font-mono mb-0.5">{formatDateTime(item.time)}</div>
                                        <div className="text-slate-700">
                                            {item.actionText && <span className="font-semibold mr-1.5 text-slate-800">{item.actionText}</span>}
                                            <span>{item.detail}</span>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* Talent Drill-Down Modal */}
            {activeTalentId && talentDrillDownData && (
                <div className="fixed inset-0 bg-black/45 flex items-center justify-center p-4 z-50 overflow-y-auto">
                    <div className="bg-white rounded-2xl max-w-2xl w-full p-6 space-y-6 max-h-[85vh] overflow-y-auto shadow-2xl relative">
                        <button
                            onClick={() => setActiveTalentId(null)}
                            className="absolute top-4 right-4 p-2 hover:bg-slate-100 rounded-full transition-colors font-semibold text-slate-500"
                        >
                            ✕
                        </button>

                        <div className="space-y-1">
                            <h2 className="text-2xl font-bold font-display text-black/85">{talentDrillDownData.name}</h2>
                            <p className="text-xs text-slate-500 font-mono">Talent Interaction Details</p>
                        </div>

                        <div className="bg-slate-50 border border-slate-100 rounded-xl p-4 grid grid-cols-2 md:grid-cols-4 gap-4 text-center">
                            <div>
                                <div className="text-lg font-bold text-slate-800">{talentDrillDownData.views}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Total Views</div>
                            </div>
                            <div>
                                <div className="text-lg font-bold text-slate-800">{talentDrillDownData.stats.intro_views || 0}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Video Views</div>
                            </div>
                            <div>
                                <div className="text-lg font-bold text-slate-800">{talentDrillDownData.stats.intro_downloads || 0}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Video Downloads</div>
                            </div>
                            <div>
                                <div className="text-lg font-bold text-slate-800">{talentDrillDownData.stats.folder_downloads || 0}</div>
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 mt-1 font-semibold">Folder Downloads</div>
                            </div>
                        </div>

                        <div className="space-y-3">
                            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono">Interactions Log</h3>
                            <div className="border-l border-slate-100 pl-4 space-y-4 max-h-60 overflow-y-auto">
                                {talentDrillDownData.timeline.map((item, idx) => (
                                    <div key={idx} className="relative text-xs">
                                        <div className="absolute -left-[21px] top-1 w-2.5 h-2.5 bg-indigo-500 border border-white rounded-full" />
                                        <div className="text-[10px] text-slate-400 font-mono mb-0.5">{formatDateTime(item.time)}</div>
                                        <div className="text-slate-700">
                                            <span className="font-bold text-slate-800 mr-1.5">{item.viewer}</span>
                                            <span className="text-indigo-600 font-semibold">{item.act}</span>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

// Simple loading skeleton
const LoadingSkeleton = () => (
    <div className="animate-pulse space-y-8">
        <div className="grid grid-cols-2 md:grid-cols-6 gap-4">
            {[1, 2, 3, 4, 5, 6].map((i) => (
                <div key={i} className="bg-white border border-black/[0.08] rounded-xl p-5">
                    <div className="h-8 bg-black/[0.08] rounded w-20 mb-2"></div>
                    <div className="h-3 bg-black/[0.08] rounded w-16"></div>
                </div>
            ))}
        </div>
        <div className="bg-white border border-black/[0.08] rounded-xl h-64"></div>
    </div>
);

// Centralized clipboard copy helper
const copyToClipboard = async (text) => {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.top = "-9999px";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand("copy");
            return true;
        } catch {
            return false;
        } finally {
            document.body.removeChild(textarea);
        }
    }
};

const computeHeat = (score) => {
    if (score >= 12) return { label: "Very Interested", icon: Flame, cls: "text-rose-600", bg: "bg-rose-50 border-rose-200" };
    if (score >= 6)  return { label: "Hot",             icon: Flame, cls: "text-orange-500", bg: "bg-orange-50 border-orange-200" };
    if (score >= 2)  return { label: "Warm",            icon: Thermometer, cls: "text-amber-500", bg: "bg-amber-50 border-amber-200" };
    return null;
};

const _submission_to_client_shape = (s, project = null) => {
    const media = [];
    
    // Video items
    if (s.video_url) {
        media.push({
            id: s.video_media_id || "intro_video",
            category: "intro_video",
            url: s.video_url,
            resource_type: "video",
        });
    }
    
    // Add additional submission media
    if (s.media) {
        s.media.forEach(m => {
            if (m.id !== s.video_media_id) {
                media.push(m);
            }
        });
    }

    return {
        id: s.id,
        name: s.talent_name || "Submission",
        cover_media_id: s.cover_media_id,
        media,
    };
};

const sign_r2_media_if_needed = (subject) => {
    return subject; // Backend already handles R2 signing
};
