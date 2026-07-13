import React, { useEffect, useState, useCallback } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import {
    Activity,
    AlertCircle,
    Calendar,
    CheckCircle,
    ChevronRight,
    Cpu,
    Filter,
    HardDrive,
    History,
    Network,
    RefreshCw,
    Search,
    Smartphone,
    Terminal,
    TrendingUp,
    Check,
    X,
} from "lucide-react";

export default function SubmissionDiagnostics() {
    const [activeTab, setActiveTab] = useState("live"); // live, recoveries, grouped, health
    const [liveData, setLiveData] = useState([]);
    const [groupedData, setGroupedData] = useState([]);
    const [metrics, setMetrics] = useState(null);
    const [loading, setLoading] = useState(true);
    const [selectedRecord, setSelectedRecord] = useState(null); // modal view

    // Filters for Live tab
    const [projectSlug, setProjectSlug] = useState("");
    const [failureType, setFailureType] = useState("");
    const [responseStatus, setResponseStatus] = useState("");
    const [device, setDevice] = useState("");
    const [browser, setBrowser] = useState("");
    const [page, setPage] = useState(1);
    const [totalRecords, setTotalRecords] = useState(0);

    const loadLiveFailures = useCallback(async () => {
        setLoading(true);
        try {
            const params = {
                page,
                size: 30,
            };
            if (activeTab === "live") {
                params.retry_succeeded = false;
            } else if (activeTab === "recoveries") {
                params.retry_succeeded = true;
            }
            
            if (projectSlug) params.project_slug = projectSlug;
            if (failureType) params.failure_type = failureType;
            if (responseStatus) params.response_status = parseInt(responseStatus, 10);
            if (device) params.device = device;
            if (browser) params.browser = browser;

            const { data } = await adminApi.get("/admin/diagnostics", { params });
            setLiveData(data.results || []);
            setTotalRecords(data.total || 0);
        } catch (e) {
            toast.error("Failed to load failures");
        } finally {
            setLoading(false);
        }
    }, [page, activeTab, projectSlug, failureType, responseStatus, device, browser]);

    const loadGroupedFailures = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/admin/diagnostics/summary");
            setGroupedData(data || []);
        } catch (e) {
            toast.error("Failed to load grouped failures");
        } finally {
            setLoading(false);
        }
    }, []);

    const loadMetrics = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/admin/diagnostics/metrics");
            setMetrics(data || null);
        } catch (e) {
            toast.error("Failed to load health metrics");
        } finally {
            setLoading(false);
        }
    }, []);

    const loadActiveTabData = useCallback(() => {
        if (activeTab === "live" || activeTab === "recoveries") {
            loadLiveFailures();
        } else if (activeTab === "grouped") {
            loadGroupedFailures();
        } else if (activeTab === "health") {
            loadMetrics();
        }
    }, [activeTab, loadLiveFailures, loadGroupedFailures, loadMetrics]);

    useEffect(() => {
        setPage(1);
    }, [activeTab]);

    useEffect(() => {
        loadActiveTabData();
    }, [loadActiveTabData]);

    const handleFilterReset = () => {
        setProjectSlug("");
        setFailureType("");
        setResponseStatus("");
        setDevice("");
        setBrowser("");
        setPage(1);
    };

    return (
        <div className="flex-1 overflow-y-auto bg-[#f9f9fb] p-6 space-y-6 text-[#333333]">
            {/* Header */}
            <div className="flex justify-between items-center">
                <div>
                    <h1 className="text-2xl font-bold tracking-tight">Submission Diagnostics</h1>
                    <p className="text-sm text-gray-500">Monitor and debug talent portfolio loading failures in real time.</p>
                </div>
                <button
                    onClick={loadActiveTabData}
                    className="p-2 bg-white border border-gray-200 rounded-lg shadow-sm hover:bg-gray-50 active:scale-95 transition-all"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                </button>
            </div>

            {/* Navigation Tabs */}
            <div className="flex border-b border-gray-200">
                <button
                    onClick={() => setActiveTab("live")}
                    className={`py-3 px-6 text-sm font-semibold border-b-2 transition-all ${activeTab === "live" ? "border-black text-black" : "border-transparent text-gray-400 hover:text-gray-600"}`}
                >
                    <div className="flex items-center gap-2">
                        <History className="w-4 h-4 text-red-500" /> Live Failures
                    </div>
                </button>
                <button
                    onClick={() => setActiveTab("recoveries")}
                    className={`py-3 px-6 text-sm font-semibold border-b-2 transition-all ${activeTab === "recoveries" ? "border-black text-black" : "border-transparent text-gray-400 hover:text-gray-600"}`}
                >
                    <div className="flex items-center gap-2">
                        <CheckCircle className="w-4 h-4 text-green-500" /> Recent Recoveries
                    </div>
                </button>
                <button
                    onClick={() => setActiveTab("grouped")}
                    className={`py-3 px-6 text-sm font-semibold border-b-2 transition-all ${activeTab === "grouped" ? "border-black text-black" : "border-transparent text-gray-400 hover:text-gray-600"}`}
                >
                    <div className="flex items-center gap-2">
                        <Filter className="w-4 h-4" /> Grouped Issues
                    </div>
                </button>
                <button
                    onClick={() => setActiveTab("health")}
                    className={`py-3 px-6 text-sm font-semibold border-b-2 transition-all ${activeTab === "health" ? "border-black text-black" : "border-transparent text-gray-400 hover:text-gray-600"}`}
                >
                    <div className="flex items-center gap-2">
                        <Activity className="w-4 h-4 text-blue-500" /> Health Dashboard
                    </div>
                </button>
            </div>

            {/* Content Container */}
            {loading && liveData.length === 0 && groupedData.length === 0 && !metrics ? (
                <div className="flex items-center justify-center py-20 bg-white border border-gray-100 rounded-xl shadow-sm">
                    <RefreshCw className="w-8 h-8 animate-spin text-gray-400" />
                </div>
            ) : (
                <div className="space-y-6">
                    {/* Live or Recoveries Tabs */}
                    {(activeTab === "live" || activeTab === "recoveries") && (
                        <div className="space-y-4">
                            {/* Filters Bar */}
                            <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-4 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 items-end">
                                <div>
                                    <label className="text-[10px] uppercase font-bold tracking-wider text-gray-400">Project</label>
                                    <input
                                        type="text"
                                        placeholder="Slug..."
                                        value={projectSlug}
                                        onChange={(e) => setProjectSlug(e.target.value)}
                                        className="w-full mt-1 border border-gray-200 rounded-lg p-2 text-xs focus:ring-1 focus:ring-black focus:outline-none"
                                    />
                                </div>
                                <div>
                                    <label className="text-[10px] uppercase font-bold tracking-wider text-gray-400">Error Type</label>
                                    <select
                                        value={failureType}
                                        onChange={(e) => setFailureType(e.target.value)}
                                        className="w-full mt-1 border border-gray-200 rounded-lg p-2 text-xs focus:ring-1 focus:ring-black focus:outline-none"
                                    >
                                        <option value="">All</option>
                                        <option value="network">Network</option>
                                        <option value="timeout">Timeout</option>
                                        <option value="not_found">404 (Not Found)</option>
                                        <option value="server_error">5xx Server Error</option>
                                        <option value="http_error">HTTP Error</option>
                                        <option value="aborted">Aborted</option>
                                    </select>
                                </div>
                                <div>
                                    <label className="text-[10px] uppercase font-bold tracking-wider text-gray-400">HTTP Status</label>
                                    <input
                                        type="text"
                                        placeholder="e.g. 500..."
                                        value={responseStatus}
                                        onChange={(e) => setResponseStatus(e.target.value)}
                                        className="w-full mt-1 border border-gray-200 rounded-lg p-2 text-xs focus:ring-1 focus:ring-black focus:outline-none"
                                    />
                                </div>
                                <div>
                                    <label className="text-[10px] uppercase font-bold tracking-wider text-gray-400">Device</label>
                                    <select
                                        value={device}
                                        onChange={(e) => setDevice(e.target.value)}
                                        className="w-full mt-1 border border-gray-200 rounded-lg p-2 text-xs focus:ring-1 focus:ring-black focus:outline-none"
                                    >
                                        <option value="">All</option>
                                        <option value="whatsapp">WhatsApp</option>
                                        <option value="instagram">Instagram</option>
                                        <option value="facebook">Facebook</option>
                                        <option value="in_app">In-App WebView</option>
                                    </select>
                                </div>
                                <div>
                                    <label className="text-[10px] uppercase font-bold tracking-wider text-gray-400">Browser</label>
                                    <select
                                        value={browser}
                                        onChange={(e) => setBrowser(e.target.value)}
                                        className="w-full mt-1 border border-gray-200 rounded-lg p-2 text-xs focus:ring-1 focus:ring-black focus:outline-none"
                                    >
                                        <option value="">All</option>
                                        <option value="safari">Safari</option>
                                        <option value="chrome">Chrome</option>
                                    </select>
                                </div>
                                <button
                                    onClick={handleFilterReset}
                                    className="w-full py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg font-semibold text-xs transition"
                                >
                                    Reset Filters
                                </button>
                            </div>

                            {/* Cards / List view */}
                            <div className="space-y-3">
                                {liveData.length === 0 ? (
                                    <div className="p-8 text-center bg-white border border-gray-200 rounded-xl shadow-sm text-gray-400 font-medium">
                                        No diagnostics telemetry records found matching filters.
                                    </div>
                                ) : (
                                    liveData.map((rec, idx) => (
                                        <div
                                            key={idx}
                                            onClick={() => setSelectedRecord(rec)}
                                            className="bg-white border border-gray-200 hover:border-black rounded-xl p-4 shadow-sm cursor-pointer transition-all hover:shadow-md flex flex-col md:flex-row justify-between items-start md:items-center gap-4"
                                        >
                                            <div className="space-y-1 w-full md:w-3/5">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className="font-bold text-black text-sm">{rec.project_slug}</span>
                                                    <span className={`px-2 py-0.5 rounded-full font-bold uppercase text-[9px] ${
                                                        rec.failure_type === 'timeout' ? 'bg-orange-50 text-orange-600' :
                                                        rec.failure_type === 'network' ? 'bg-red-50 text-red-600' :
                                                        rec.failure_type === 'not_found' ? 'bg-yellow-50 text-yellow-600' : 'bg-gray-100 text-gray-600'
                                                    }`}>
                                                        {rec.failure_type}
                                                    </span>
                                                    <span className="font-mono text-xs text-gray-500">
                                                        HTTP {rec.response_status || "—"}
                                                    </span>
                                                </div>
                                                <p className="text-[10px] text-gray-400 font-mono">
                                                    {new Date(rec.created_at).toLocaleString()}
                                                </p>
                                                {/* Detailed summary card display */}
                                                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 pt-2 text-[11px] text-gray-600 font-medium">
                                                    <span className="bg-gray-50 px-2 py-0.5 rounded border border-gray-100 flex items-center gap-1">
                                                        <Smartphone className="w-3 h-3 text-gray-400" />
                                                        {rec.device_type === "Mobile" ? "iPhone/Mobile" : "Desktop"} ({rec.os} {rec.os_version})
                                                    </span>
                                                    <span className="bg-gray-50 px-2 py-0.5 rounded border border-gray-100">
                                                        {rec.browser} {rec.browser_version}
                                                    </span>
                                                    <span className="bg-gray-50 px-2 py-0.5 rounded border border-gray-100">
                                                        Build: {rec.frontend_build_id} ({rec.app_version})
                                                    </span>
                                                    <span className="bg-gray-50 px-2 py-0.5 rounded border border-gray-100">
                                                        Cache: {rec.sw_version}
                                                    </span>
                                                </div>
                                            </div>

                                            <div className="flex items-center justify-between md:justify-end gap-4 w-full md:w-2/5 border-t md:border-t-0 pt-3 md:pt-0 border-gray-100">
                                                <div className="text-right space-y-1">
                                                    <div className="flex items-center gap-1.5 md:justify-end">
                                                        <span className="text-[11px] text-gray-500 font-semibold">
                                                            Retry {rec.retry_attempt_count}/3
                                                        </span>
                                                        <span className={`px-2 py-0.5 rounded-full font-bold uppercase text-[9px] flex items-center gap-1 ${
                                                            rec.retry_succeeded ? 'bg-green-50 text-green-600' : 'bg-red-50 text-red-600'
                                                        }`}>
                                                            {rec.retry_succeeded ? (
                                                                <>Recovered <Check className="w-2.5 h-2.5" /></>
                                                            ) : (
                                                                <>Failed <X className="w-2.5 h-2.5" /></>
                                                            )}
                                                        </span>
                                                    </div>
                                                    <p className="text-[10px] text-gray-400 truncate max-w-[200px] font-mono">
                                                        ID: {rec.x_railway_request_id || "No trace"}
                                                    </p>
                                                </div>
                                                <ChevronRight className="w-4 h-4 text-gray-400" />
                                            </div>
                                        </div>
                                    ))
                                )}
                            </div>

                            {/* Pagination */}
                            {totalRecords > 30 && (
                                <div className="flex justify-between items-center p-4 text-xs bg-white border border-gray-200 rounded-xl shadow-sm">
                                    <span className="text-gray-500">Showing {liveData.length} of {totalRecords} records</span>
                                    <div className="flex gap-2">
                                        <button
                                            disabled={page === 1}
                                            onClick={() => setPage(p => Math.max(1, p - 1))}
                                            className="px-3 py-1 bg-white border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50"
                                        >
                                            Prev
                                        </button>
                                        <button
                                            disabled={page * 30 >= totalRecords}
                                            onClick={() => setPage(p => p + 1)}
                                            className="px-3 py-1 bg-white border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50"
                                        >
                                            Next
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Grouped Tab */}
                    {activeTab === "grouped" && (
                        <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-xs border-collapse">
                                    <thead>
                                        <tr className="bg-gray-50 border-b border-gray-100 uppercase text-[10px] tracking-wider text-gray-400 font-bold">
                                            <th className="p-4">Failure Signature</th>
                                            <th className="p-4 text-center">Occurrences</th>
                                            <th className="p-4">Projects</th>
                                            <th className="p-4">Affected Devices</th>
                                            <th className="p-4">First Seen</th>
                                            <th className="p-4">Last Seen</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gray-100">
                                        {groupedData.length === 0 ? (
                                            <tr>
                                                <td colSpan="6" className="p-8 text-center text-gray-400 font-medium">
                                                    No grouped failures logged.
                                                </td>
                                            </tr>
                                        ) : (
                                            groupedData.map((item, idx) => (
                                                <tr key={idx} className="hover:bg-gray-50/55 transition-colors">
                                                    <td className="p-4 space-y-1">
                                                        <div className="flex gap-2 items-center">
                                                            <span className="px-2 py-0.5 rounded-full font-bold uppercase text-[9px] bg-red-50 text-red-600">
                                                                {item.failure_type}
                                                            </span>
                                                            <span className="font-mono text-[11px] font-semibold text-black">
                                                                HTTP {item.response_status || "—"}
                                                            </span>
                                                        </div>
                                                        <p className="text-gray-500 font-mono text-[10px] break-all max-w-md">
                                                            {item.axios_message || "No error message logged."}
                                                        </p>
                                                    </td>
                                                    <td className="p-4 text-center font-bold text-black text-sm">{item.occurrences}</td>
                                                    <td className="p-4 text-gray-600">
                                                        <div className="flex flex-wrap gap-1">
                                                            {item.projects.map((p, i) => (
                                                                <span key={i} className="px-1.5 py-0.5 bg-gray-100 rounded text-[10px]">{p}</span>
                                                            ))}
                                                        </div>
                                                    </td>
                                                    <td className="p-4 text-gray-600">
                                                        <div className="flex flex-wrap gap-1">
                                                            {item.affected_devices.map((d, i) => (
                                                                <span key={i} className="px-1.5 py-0.5 bg-gray-100 rounded text-[10px]">{d}</span>
                                                            ))}
                                                        </div>
                                                    </td>
                                                    <td className="p-4 text-gray-500">
                                                        {new Date(item.first_seen).toLocaleDateString()}
                                                    </td>
                                                    <td className="p-4 text-gray-500">
                                                        {new Date(item.last_seen).toLocaleDateString()}
                                                    </td>
                                                </tr>
                                            ))
                                        )}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    )}

                    {/* Health Dashboard Tab */}
                    {activeTab === "health" && metrics && (
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                            {/* Summary Metrics */}
                            <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-4 shadow-sm flex flex-col justify-between">
                                <h3 className="text-xs font-bold uppercase tracking-wider text-gray-400">Recovery Metrics</h3>
                                <div className="space-y-1">
                                    <p className="text-4xl font-extrabold text-black">{metrics.total_failures}</p>
                                    <p className="text-xs text-gray-500 font-semibold">Total Load Anomalies</p>
                                </div>
                                <div className="space-y-1 pt-4 border-t border-gray-100">
                                    <div className="flex justify-between items-end">
                                        <p className="text-2xl font-bold text-green-600">
                                            {metrics.total_failures > 0 ? ((metrics.recovered_failures / metrics.total_failures) * 100).toFixed(2) : 0}%
                                        </p>
                                        <div className="text-right text-[10px] text-gray-400 font-bold uppercase">
                                            Rec: {metrics.recovered_failures} / Fail: {metrics.total_failures - metrics.recovered_failures}
                                        </div>
                                    </div>
                                    <p className="text-xs text-gray-500">Auto-Recovery Rate (succeeded on retry)</p>
                                </div>
                            </div>

                            {/* Failure Types Chart Block */}
                            <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-4 shadow-sm">
                                <h3 className="text-xs font-bold uppercase tracking-wider text-gray-400">Distribution by Error Type</h3>
                                <div className="space-y-2">
                                    {metrics.failure_types.length === 0 ? (
                                        <p className="text-xs text-gray-400 font-medium py-4 text-center">No failure logs present.</p>
                                    ) : (
                                        metrics.failure_types.map((item, i) => (
                                            <div key={i} className="space-y-1">
                                                <div className="flex justify-between text-xs font-semibold text-gray-600">
                                                    <span className="uppercase">{item.type}</span>
                                                    <span>{item.count} ({((item.count / metrics.total_failures) * 100).toFixed(0)}%)</span>
                                                </div>
                                                <div className="w-full bg-gray-100 h-2 rounded-full overflow-hidden">
                                                    <div
                                                        className="bg-black h-full"
                                                        style={{ width: `${(item.count / metrics.total_failures) * 100}%` }}
                                                    />
                                                </div>
                                            </div>
                                        ))
                                    )}
                                </div>
                            </div>

                            {/* Historical Timeline block */}
                            <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-4 shadow-sm">
                                <h3 className="text-xs font-bold uppercase tracking-wider text-gray-400">Daily Trend (Last 30 days)</h3>
                                <div className="space-y-2 max-h-[160px] overflow-y-auto">
                                    {metrics.daily_failures.length === 0 ? (
                                        <p className="text-xs text-gray-400 font-medium py-4 text-center">No timeline data available.</p>
                                    ) : (
                                        metrics.daily_failures.map((item, i) => (
                                            <div key={i} className="flex justify-between text-xs font-mono py-1 border-b border-gray-50">
                                                <span className="text-gray-500">{item.date}</span>
                                                <span className="font-bold text-black">{item.count} errors</span>
                                            </div>
                                        ))
                                    )}
                                </div>
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Modal Detail Dialog */}
            {selectedRecord && (
                <div className="fixed inset-0 bg-black/40 backdrop-blur-sm z-50 flex items-center justify-center p-4">
                    <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl overflow-hidden flex flex-col max-h-[85vh]">
                        {/* Title bar */}
                        <div className="p-4 border-b border-gray-100 flex justify-between items-center bg-gray-50">
                            <div>
                                <h3 className="font-bold text-black text-sm">Diagnostics Payload</h3>
                                <p className="text-[10px] text-gray-500 font-mono">Logged: {new Date(selectedRecord.created_at).toLocaleString()}</p>
                            </div>
                            <button
                                onClick={() => setSelectedRecord(null)}
                                className="text-gray-400 hover:text-black transition"
                            >
                                Close
                            </button>
                        </div>
                        {/* Body content */}
                        <div className="p-6 overflow-y-auto space-y-5 text-xs">
                            {/* Summary Cards */}
                            <div className="grid grid-cols-2 gap-4">
                                <div className="bg-gray-50 p-3 rounded-lg border border-gray-100 space-y-1">
                                    <span className="text-[9px] uppercase font-bold tracking-wider text-gray-400">Target URL</span>
                                    <p className="font-mono text-black font-semibold break-all">{selectedRecord.request_url}</p>
                                </div>
                                <div className="bg-gray-50 p-3 rounded-lg border border-gray-100 space-y-1">
                                    <span className="text-[9px] uppercase font-bold tracking-wider text-gray-400">Correlation ID</span>
                                    <p className="font-mono text-black font-semibold break-all">
                                        {selectedRecord.x_railway_request_id || selectedRecord.x_request_id || "None"}
                                    </p>
                                </div>
                            </div>

                            {/* Core Diagnostics Table */}
                            <div className="border border-gray-100 rounded-lg overflow-hidden">
                                <table className="w-full text-left border-collapse">
                                    <tbody className="divide-y divide-gray-50">
                                        <tr>
                                            <td className="p-2.5 bg-gray-50 font-semibold w-1/3">Error Signature</td>
                                            <td className="p-2.5 font-mono">{selectedRecord.axios_code || "None"} - {selectedRecord.axios_message || "—"}</td>
                                        </tr>
                                        <tr>
                                            <td className="p-2.5 bg-gray-50 font-semibold">User Agent</td>
                                            <td className="p-2.5 text-gray-600 break-all">{selectedRecord.user_agent}</td>
                                        </tr>
                                        <tr>
                                            <td className="p-2.5 bg-gray-50 font-semibold">Platform & Language</td>
                                            <td className="p-2.5 text-gray-600">{selectedRecord.platform || "—"} / {selectedRecord.language || "—"}</td>
                                        </tr>
                                        <tr>
                                            <td className="p-2.5 bg-gray-50 font-semibold">Environment</td>
                                            <td className="p-2.5 uppercase font-bold">{selectedRecord.environment}</td>
                                        </tr>
                                        <tr>
                                            <td className="p-2.5 bg-gray-50 font-semibold">Network Latency (RTT)</td>
                                            <td className="p-2.5 font-mono">{selectedRecord.connection_info?.rtt || "—"} ms</td>
                                        </tr>
                                        <tr>
                                            <td className="p-2.5 bg-gray-50 font-semibold">Service Worker State</td>
                                            <td className="p-2.5 text-gray-600">
                                                Active: {selectedRecord.sw_controller_present ? "Yes" : "No"} | 
                                                Status: {selectedRecord.sw_registration_status} | 
                                                Version: {selectedRecord.sw_version || "None"}
                                            </td>
                                        </tr>
                                    </tbody>
                                </table>
                            </div>

                            {/* Raw payload */}
                            <div className="space-y-1">
                                <label className="text-[10px] uppercase font-bold tracking-wider text-gray-400">Complete JSON Payload</label>
                                <pre className="bg-[#1f2937] text-gray-200 p-4 rounded-lg overflow-x-auto font-mono text-[10px] max-h-[180px]">
                                    {JSON.stringify(selectedRecord, null, 2)}
                                </pre>
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
