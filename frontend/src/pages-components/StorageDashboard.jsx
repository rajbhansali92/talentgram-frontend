import React, { useState, useEffect } from "react";
import { 
  Cloud, 
  Trash2, 
  Archive, 
  RefreshCw, 
  Database, 
  TrendingUp, 
  FileVideo, 
  Image as ImageIcon,
  CheckCircle,
  AlertTriangle,
  FolderOpen,
  Volume2,
  HardDrive,
  Activity,
  Check,
  RotateCcw,
  Sparkles,
  Info
} from "lucide-react";
import { adminApi } from "@/lib/api";

export default function StorageDashboard() {
  const [analytics, setAnalytics] = useState(null);
  const [projects, setProjects] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [healthLoading, setHealthLoading] = useState(false);
  const [error, setError] = useState(null);
  const [confirmDialog, setConfirmDialog] = useState(null); // { type, id, data, title, description, action }
  const [actionLoading, setActionLoading] = useState(false);

  const fetchData = async () => {
    setLoading(true);
    try {
      const [resAnal, resProj, resHealth] = await Promise.all([
        adminApi.get(`/admin/cloudinary/analytics`),
        adminApi.get(`/admin/cloudinary/projects`),
        adminApi.get(`/admin/cloudinary/health`)
      ]);
      setAnalytics(resAnal.data);
      setProjects(resProj.data);
      setHealth(resHealth.data);
      setError(null);
    } catch (err) {
      console.error("Failed to load storage dashboard metrics:", err);
      setError("Unable to retrieve storage console metrics. Verify that you are signed in as an administrator.");
    } finally {
      setLoading(false);
    }
  };

  const fetchHealth = async () => {
    setHealthLoading(true);
    try {
      const res = await adminApi.get(`/admin/cloudinary/health`);
      setHealth(res.data);
    } catch (err) {
      console.error("Failed to reload storage health metrics:", err);
    } finally {
      setHealthLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const handleArchive = async (projectId) => {
    try {
      await adminApi.post(`/admin/cloudinary/projects/${projectId}/archive`, {});
      fetchData();
    } catch (err) {
      alert("Failed to archive project");
    }
  };

  const handleRestore = async (projectId) => {
    try {
      await adminApi.post(`/admin/cloudinary/projects/${projectId}/restore`, {});
      fetchData();
    } catch (err) {
      alert("Failed to restore project");
    }
  };

  const handleDeleteAuditions = async (projectId) => {
    setActionLoading(true);
    try {
      await adminApi.delete(`/admin/cloudinary/projects/${projectId}/auditions`);
      setConfirmDialog(null);
      fetchData();
    } catch (err) {
      alert("Failed to delete project audition videos");
    } finally {
      setActionLoading(false);
    }
  };

  const handleDeleteVoiceNotes = async (projectId) => {
    setActionLoading(true);
    try {
      await adminApi.delete(`/admin/cloudinary/projects/${projectId}/voice-notes`);
      setConfirmDialog(null);
      fetchData();
    } catch (err) {
      alert("Failed to delete project voice notes");
    } finally {
      setActionLoading(false);
    }
  };

  const handleDeleteProject = async (projectId) => {
    setActionLoading(true);
    try {
      await adminApi.delete(`/admin/cloudinary/projects/${projectId}`);
      setConfirmDialog(null);
      fetchData();
    } catch (err) {
      alert("Failed to purge project folder");
    } finally {
      setActionLoading(false);
    }
  };

  const handleOneClickCleanup = async () => {
    setHealthLoading(true);
    try {
      const res = await adminApi.post(`/admin/cloudinary/health/cleanup`);
      alert(`Cleanup completed! Cleaned ${res.data.cleaned_orphaned} orphaned files, ${res.data.cleaned_broken} broken references, and ${res.data.cleaned_unused} unused files.`);
      fetchData();
    } catch (err) {
      alert("Failed to run storage cleanup");
    } finally {
      setHealthLoading(false);
    }
  };

  const formatBytes = (bytes, decimals = 2) => {
    if (bytes === 0 || !bytes) return "0 Bytes";
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ["Bytes", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
  };

  if (loading && !analytics) {
    return (
      <div className="p-8 space-y-6">
        <div className="flex justify-between items-center">
          <div className="h-8 w-48 rounded animate-tg-shimmer" />
          <div className="h-10 w-32 rounded animate-tg-shimmer" />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="bg-white p-6 rounded-lg border border-gray-100 space-y-4">
              <div className="h-4 w-1/2 rounded animate-tg-shimmer" />
              <div className="h-8 w-3/4 rounded animate-tg-shimmer" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8 text-center max-w-xl mx-auto space-y-4">
        <AlertTriangle className="w-12 h-12 text-red-500 mx-auto" />
        <h3 className="text-lg font-medium text-slate-900">Connection Error</h3>
        <p className="text-sm text-slate-500">{error}</p>
        <button 
          onClick={fetchData} 
          className="mt-2 h-11 px-4 text-xs font-semibold bg-slate-900 text-white rounded-md inline-flex items-center gap-2"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Try Again
        </button>
      </div>
    );
  }

  return (
    <div className="p-6 md:p-8 space-y-8 max-w-7xl mx-auto">
      {/* HEADER */}
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900 font-display flex items-center gap-2">
            <HardDrive className="w-6 h-6 text-slate-800" />
            Storage & Asset Management Console
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            Authoritative interface for monitoring cloud storage usage, health, and project asset lifecycles.
          </p>
        </div>
        <button 
          onClick={fetchData} 
          disabled={loading}
          className="h-11 px-4 text-xs font-semibold bg-white border border-slate-200 rounded-md inline-flex items-center gap-2 hover:bg-slate-50 transition-colors shadow-sm"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh Metrics
        </button>
      </div>

      {/* OVERALL STORAGE STATS CARDS */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px]">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Total Aggregated Storage</span>
            <div className="text-3xl font-bold mt-2 text-slate-900 font-display">
              {formatBytes(analytics?.total_storage)}
            </div>
            <p className="text-xs text-slate-500 mt-1">Cloudinary + Cloudflare R2 combined</p>
          </div>
          <div className="absolute right-4 bottom-4 p-2.5 bg-slate-50 rounded-full">
            <Database className="w-5 h-5 text-slate-500" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px]">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Permanent Assets</span>
            <div className="text-3xl font-bold mt-2 text-slate-950 font-display">
              {formatBytes(analytics?.permanent_storage)}
            </div>
            <p className="text-xs text-slate-500 mt-1">Master Talent Profiles & Portfolios</p>
          </div>
          <div className="absolute right-4 bottom-4 p-2.5 bg-emerald-50 rounded-full">
            <CheckCircle className="w-5 h-5 text-emerald-600" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px]">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Temporary Auditions</span>
            <div className="text-3xl font-bold mt-2 text-slate-950 font-display">
              {formatBytes(analytics?.temporary_storage)}
            </div>
            <p className="text-xs text-slate-500 mt-1">Auditions, voice notes & admin uploads</p>
          </div>
          <div className="absolute right-4 bottom-4 p-2.5 bg-indigo-50 rounded-full">
            <TrendingUp className="w-5 h-5 text-indigo-600" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px]">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Archived Projects</span>
            <div className="text-3xl font-bold mt-2 text-slate-950 font-display">
              {formatBytes(analytics?.archived_storage)}
            </div>
            <p className="text-xs text-slate-500 mt-1">Campaigns marked for archive</p>
          </div>
          <div className="absolute right-4 bottom-4 p-2.5 bg-amber-50 rounded-full">
            <Archive className="w-5 h-5 text-amber-600" />
          </div>
        </div>
      </div>

      {/* PROVIDER BREAKDOWNS */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {analytics?.providers && Object.entries(analytics.providers).map(([key, provider]) => {
          const quota = provider.quota || 1;
          const percentage = Math.min(100, Math.round((provider.used_bytes / quota) * 100));
          return (
            <div key={key} className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm space-y-4">
              <div className="flex justify-between items-center">
                <h3 className="text-base font-bold text-slate-900 font-display flex items-center gap-2">
                  <Cloud className="w-4 h-4 text-slate-500" />
                  {provider.name}
                </h3>
                <span className="text-xs font-mono font-bold text-slate-500 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded">
                  {provider.object_count} objects
                </span>
              </div>
              <div className="space-y-2">
                <div className="flex justify-between text-xs font-mono text-slate-500">
                  <span>Used: {formatBytes(provider.used_bytes)}</span>
                  <span>Limit: {formatBytes(quota)}</span>
                </div>
                <div className="w-full bg-slate-100 rounded-full h-2">
                  <div 
                    className={`h-2 rounded-full transition-all duration-500 ${
                      percentage > 85 ? "bg-red-500" : percentage > 60 ? "bg-amber-500" : "bg-slate-900"
                    }`}
                    style={{ width: `${percentage}%` }}
                  />
                </div>
                <div className="flex justify-between text-xs text-slate-400">
                  <span>Remaining: {formatBytes(provider.remaining_capacity)}</span>
                  <span>{percentage}% utilized</span>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4 pt-2 border-t border-slate-100 text-xs">
                <div>
                  <span className="text-slate-400 block">Bandwidth (Month)</span>
                  <span className="font-semibold text-slate-800">{formatBytes(provider.bandwidth_used)}</span>
                </div>
                <div>
                  <span className="text-slate-400 block">API Call Usage</span>
                  <span className="font-semibold text-slate-800">{provider.api_usage || "N/A"}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* STORAGE HEALTH & CLEANUP */}
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-6">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <div>
            <h3 className="text-base font-bold text-slate-900 font-display flex items-center gap-2">
              <Activity className="w-5 h-5 text-slate-700" />
              Automated Storage Health Monitoring
            </h3>
            <p className="text-xs text-slate-500 mt-1">
              Scans for discrepancies between database references and physical file storage systems in real-time.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchHealth}
              disabled={healthLoading}
              className="h-10 px-3 text-xs bg-slate-50 hover:bg-slate-100 border border-slate-200 text-slate-700 rounded-md inline-flex items-center gap-1.5"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${healthLoading ? "animate-spin" : ""}`} />
              Re-Scan
            </button>
            <button
              onClick={handleOneClickCleanup}
              disabled={healthLoading || !health || (health.orphaned_count === 0 && health.broken_count === 0 && health.unused_count === 0)}
              className="h-10 px-4 text-xs font-semibold bg-slate-950 text-white rounded-md inline-flex items-center gap-2 hover:bg-slate-800 disabled:opacity-40"
            >
              <Sparkles className="w-3.5 h-3.5" />
              One-Click Repair & Cleanup
            </button>
          </div>
        </div>

        {health && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div className="bg-slate-50 border border-slate-200/50 p-4 rounded-xl text-center space-y-1">
              <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400">Orphaned Files</span>
              <div className={`text-2xl font-bold ${health.orphaned_count > 0 ? "text-amber-600" : "text-slate-800"}`}>
                {health.orphaned_count}
              </div>
              <p className="text-[10px] text-slate-500">Files without DB records</p>
            </div>
            <div className="bg-slate-50 border border-slate-200/50 p-4 rounded-xl text-center space-y-1">
              <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400">Broken References</span>
              <div className={`text-2xl font-bold ${health.broken_count > 0 ? "text-red-600" : "text-slate-800"}`}>
                {health.broken_count}
              </div>
              <p className="text-[10px] text-slate-500">DB links pointing to missing files</p>
            </div>
            <div className="bg-slate-50 border border-slate-200/50 p-4 rounded-xl text-center space-y-1">
              <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400">Duplicate Media</span>
              <div className={`text-2xl font-bold ${health.duplicate_count > 0 ? "text-indigo-600" : "text-slate-800"}`}>
                {health.duplicate_count}
              </div>
              <p className="text-[10px] text-slate-500">Same asset linked multiple times</p>
            </div>
            <div className="bg-slate-50 border border-slate-200/50 p-4 rounded-xl text-center space-y-1">
              <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400">Unused Uploads</span>
              <div className={`text-2xl font-bold ${health.unused_count > 0 ? "text-amber-600" : "text-slate-800"}`}>
                {health.unused_count}
              </div>
              <p className="text-[10px] text-slate-500">Failed files & deleted projects</p>
            </div>
          </div>
        )}
      </div>

      {/* CATEGORIES BREAKDOWN & STATS */}
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-4">
        <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
          <Database className="w-4 h-4 text-slate-500" />
          Storage Metrics by Asset Category
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {analytics?.categories && Object.entries(analytics.categories).map(([key, cat]) => (
            <div key={key} className="p-4 border border-slate-100 rounded-xl hover:bg-slate-50/50 transition-colors flex items-center gap-3">
              <div className="p-2 bg-slate-50 border border-slate-200/50 rounded-lg">
                {key.includes("video") ? (
                  <FileVideo className="w-5 h-5 text-slate-600" />
                ) : key.includes("image") ? (
                  <ImageIcon className="w-5 h-5 text-slate-600" />
                ) : key.includes("voice") ? (
                  <Volume2 className="w-5 h-5 text-slate-600" />
                ) : (
                  <FolderOpen className="w-5 h-5 text-slate-600" />
                )}
              </div>
              <div>
                <span className="text-xs text-slate-500 block font-medium">{cat.label}</span>
                <span className="text-sm font-bold text-slate-800 font-mono block">
                  {formatBytes(cat.size)}
                </span>
                <span className="text-[10px] text-slate-400">{cat.count} files</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* TOP CONSUMERS SUMMARY */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-4">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
            <FolderOpen className="w-4 h-4 text-slate-400" />
            Top Campaigns by Storage
          </h3>
          <div className="divide-y divide-slate-100">
            {analytics?.top_projects?.map((item) => (
              <div key={item.project_id} className="py-3 flex justify-between items-center">
                <span className="text-sm font-medium text-slate-700 truncate max-w-[200px]" title={item.name}>
                  {item.name}
                </span>
                <span className="text-xs font-mono font-semibold text-slate-500">
                  {formatBytes(item.size)}
                </span>
              </div>
            ))}
            {(!analytics?.top_projects || analytics.top_projects.length === 0) && (
              <p className="text-xs text-slate-400 py-4">No projects recorded.</p>
            )}
          </div>
        </div>

        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-4">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-slate-400" />
            Top Talents by Storage
          </h3>
          <div className="divide-y divide-slate-100">
            {analytics?.top_talents?.map((item) => (
              <div key={item.talent_id} className="py-3 flex justify-between items-center">
                <span className="text-sm font-medium text-slate-700 truncate max-w-[200px]" title={item.name}>
                  {item.name}
                </span>
                <span className="text-xs font-mono font-semibold text-slate-500">
                  {formatBytes(item.size)}
                </span>
              </div>
            ))}
            {(!analytics?.top_talents || analytics.top_talents.length === 0) && (
              <p className="text-xs text-slate-400 py-4">No talents recorded.</p>
            )}
          </div>
        </div>

        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-4">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
            <FileVideo className="w-4 h-4 text-slate-400" />
            Audition Metrics
          </h3>
          <div className="space-y-4 pt-2">
            <div className="flex justify-between items-center">
              <span className="text-sm text-slate-500">Total Audition Submissions</span>
              <span className="text-sm font-semibold text-slate-800">{analytics?.total_auditions || 0}</span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-sm text-slate-500">Average Audition Size</span>
              <span className="text-sm font-semibold text-slate-800">{formatBytes(analytics?.average_audition_size)}</span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-sm text-slate-500">Target Size Goal</span>
              <span className="text-xs font-semibold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded">8 MB – 15 MB</span>
            </div>
          </div>
        </div>
      </div>

      {/* DETAILED PROJECT STORAGE TABLE */}
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
        <div className="px-6 py-5 border-b border-slate-100 flex justify-between items-center">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider">Project Storage & Lifecycles</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-100 text-xs font-semibold text-slate-500 uppercase">
                <th className="px-6 py-3.5">Project Name</th>
                <th className="px-6 py-3.5">Status</th>
                <th className="px-6 py-3.5">Auditions</th>
                <th className="px-6 py-3.5">Storage Used</th>
                <th className="px-6 py-3.5">Last Activity</th>
                <th className="px-6 py-3.5 text-right">Lifecycle Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-sm text-slate-700">
              {projects.map((proj) => (
                <tr key={proj.project_id} className="hover:bg-slate-50/50 transition-colors">
                  <td className="px-6 py-4">
                    <div className={`font-semibold ${proj.status === "deleted" ? "text-red-600" : "text-slate-900"}`}>{proj.name}</div>
                    <div className="text-[10px] text-slate-400 font-mono mt-0.5 select-all">Project ID: {proj.project_id}</div>
                    {proj.last_activity && (
                      <div className="text-[10px] text-slate-400 mt-0.5">
                        Created: {new Date(proj.last_activity).toLocaleDateString("en-GB", {day: 'numeric', month: 'short', year: 'numeric'})}
                      </div>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${
                      proj.status === "archived" 
                        ? "bg-amber-50 text-amber-700 border border-amber-200/50" 
                        : proj.status === "deleted"
                        ? "bg-red-550/10 text-red-700 border border-red-200/50"
                        : proj.status === "purged"
                        ? "bg-red-50 text-red-700 border border-red-200/50"
                        : "bg-emerald-50 text-emerald-700 border border-emerald-200/50"
                    }`}>
                      {proj.status}
                    </span>
                  </td>
                  <td className="px-6 py-4 font-mono text-slate-600">{proj.total_auditions}</td>
                  <td className="px-6 py-4 font-mono text-slate-600">{formatBytes(proj.total_storage)}</td>
                  <td className="px-6 py-4 text-slate-500">
                    {proj.last_activity ? new Date(proj.last_activity).toLocaleDateString() : "Never"}
                  </td>
                  <td className="px-6 py-4 text-right space-x-2">
                    {proj.status === "active" ? (
                      <button 
                        onClick={() => handleArchive(proj.project_id)}
                        className="h-9 px-2.5 text-xs bg-slate-50 border border-slate-200 hover:bg-slate-100 rounded inline-flex items-center gap-1 text-slate-600 transition-colors"
                      >
                        <Archive className="w-3.5 h-3.5" />
                        Archive
                      </button>
                    ) : proj.status === "archived" ? (
                      <button 
                        onClick={() => handleRestore(proj.project_id)}
                        className="h-9 px-2.5 text-xs bg-slate-50 border border-slate-200 hover:bg-slate-100 rounded inline-flex items-center gap-1 text-slate-600 transition-colors"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                        Restore
                      </button>
                    ) : null}
 
                    {proj.status !== "purged" && (
                      <>
                        <button
                          onClick={() => setConfirmDialog({
                            type: "auditions",
                            id: proj.project_id,
                            data: proj,
                            title: "Delete Audition Videos?",
                            description: `Are you sure you want to delete all audition videos for project ${proj.name}? This will permanently remove all takes from Cloudflare R2 and Cloudinary, and clear the references from the submission documents.`,
                            action: () => handleDeleteAuditions(proj.project_id)
                          })}
                          className="h-9 px-2.5 text-xs bg-red-50/50 border border-red-100 hover:bg-red-50 hover:text-red-700 rounded inline-flex items-center gap-1 text-red-600 transition-colors"
                        >
                          <FileVideo className="w-3.5 h-3.5" />
                          Delete Videos
                        </button>

                        <button
                          onClick={() => setConfirmDialog({
                            type: "voicenotes",
                            id: proj.project_id,
                            data: proj,
                            title: "Delete Voice Notes?",
                            description: `Are you sure you want to delete all voice-note feedback for project ${proj.name}? This will permanently delete the audio files from Cloudinary and remove feedback records from the database.`,
                            action: () => handleDeleteVoiceNotes(proj.project_id)
                          })}
                          className="h-9 px-2.5 text-xs bg-red-50/50 border border-red-100 hover:bg-red-50 hover:text-red-700 rounded inline-flex items-center gap-1 text-red-600 transition-colors"
                        >
                          <Volume2 className="w-3.5 h-3.5" />
                          Delete Voice Notes
                        </button>

                        <button
                          onClick={() => setConfirmDialog({
                            type: "purge",
                            id: proj.project_id,
                            data: proj,
                            title: "Delete Project Assets?",
                            description: `Are you sure you want to delete the audition takes and voice notes for project ${proj.name}? Introduction videos, profile/portfolio/look images, admin uploads, and project images are NOT affected.`,
                            action: () => handleDeleteProject(proj.project_id)
                          })}
                          className="h-9 px-2.5 text-xs bg-red-600 hover:bg-red-700 text-white rounded inline-flex items-center gap-1 transition-colors"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                          Delete Project Assets
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
              {projects.length === 0 && (
                <tr>
                  <td colSpan="6" className="text-center py-8 text-slate-400 text-xs">
                    No projects found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* CONFIRMATION OVERLAY MODAL */}
      {confirmDialog && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl border border-slate-200/80 shadow-2xl max-w-md w-full p-6 space-y-4">
            <div className="flex items-center gap-3 text-red-600">
              <AlertTriangle className="w-6 h-6 shrink-0" />
              <h3 className="text-lg font-bold text-slate-950 font-display">{confirmDialog.title}</h3>
            </div>
            
            <p className="text-sm text-slate-600 leading-relaxed">
              {confirmDialog.description}
            </p>

            {(confirmDialog.type === "purge" || confirmDialog.type === "auditions" || confirmDialog.type === "voicenotes") && (
              <div className="border-t border-b border-slate-100 py-3 my-2 space-y-3">
                <div className="space-y-1.5">
                  <span className="text-[10px] uppercase font-bold tracking-wider text-red-500 block">Permanently Deletes:</span>
                  <ul className="text-xs text-slate-600 space-y-1 list-disc pl-4 font-semibold">
                    {(confirmDialog.type === "purge" || confirmDialog.type === "auditions") && <li>Audition Takes</li>}
                    {(confirmDialog.type === "purge" || confirmDialog.type === "voicenotes") && <li>Voice Notes</li>}
                  </ul>
                </div>
                <div className="space-y-1.5">
                  <span className="text-[10px] uppercase font-bold tracking-wider text-emerald-600 block font-semibold">Does NOT Delete:</span>
                  <ul className="text-xs text-slate-600 space-y-1 list-none pl-0 font-medium">
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Talent Profiles</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Introduction Videos</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Portfolio Images</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Indian Look Images</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Western Look Images</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Admin Uploaded Media</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Project Images</li>
                    <li className="flex items-center gap-1.5 text-slate-500">✓ Global Talent Records</li>
                  </ul>
                </div>
              </div>
            )}
            
            <div className="bg-red-50/50 border border-red-100 rounded-xl p-3 text-xs text-red-700 space-y-1">
              <p className="flex items-center gap-1.5">
                <Info className="w-3.5 h-3.5" />
                This action is permanent and cannot be undone.
              </p>
            </div>

            <div className="flex justify-end gap-3 pt-2">
              <button 
                onClick={() => setConfirmDialog(null)}
                disabled={actionLoading}
                className="h-11 px-4 text-xs font-semibold bg-white border border-slate-200 hover:bg-slate-50 text-slate-700 rounded-md"
              >
                Cancel
              </button>
              <button 
                onClick={confirmDialog.action}
                disabled={actionLoading}
                className="h-11 px-4 text-xs font-semibold bg-red-600 hover:bg-red-700 text-white rounded-md flex items-center gap-2 disabled:opacity-40"
              >
                {actionLoading ? (
                  <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Trash2 className="w-3.5 h-3.5" />
                )}
                Confirm Action
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
