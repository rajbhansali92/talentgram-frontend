import React, { useState, useEffect, useCallback } from "react";
import {
  Cloud,
  Trash2,
  Archive,
  RefreshCw,
  Database,
  FileVideo,
  Image as ImageIcon,
  CheckCircle,
  AlertTriangle,
  FolderOpen,
  Volume2,
  HardDrive,
  Activity,
  RotateCcw,
  Sparkles,
  Info,
  ChevronDown,
  ChevronRight,
  Users,
  X,
  Video,
} from "lucide-react";
import { adminApi } from "@/lib/api";

function formatBytes(bytes, decimals = 2) {
  if (bytes === 0 || !bytes) return "0 Bytes";
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["Bytes", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
}

export default function StorageDashboard() {
  const [summary, setSummary] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [health, setHealth] = useState(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthScanned, setHealthScanned] = useState(false);

  const [expandedProject, setExpandedProject] = useState(null);
  const [projectTalents, setProjectTalents] = useState({}); // project_id -> talents[]
  const [talentsLoading, setTalentsLoading] = useState(null); // project_id currently loading

  const [manageTarget, setManageTarget] = useState(null); // { project_id, project_name, talent_id, talent_name }

  const [confirmDialog, setConfirmDialog] = useState(null);
  const [actionLoading, setActionLoading] = useState(false);

  const fetchCore = useCallback(async () => {
    setLoading(true);
    try {
      const [resSummary, resAnalytics, resProjects] = await Promise.all([
        adminApi.get(`/admin/cloudinary/summary`),
        adminApi.get(`/admin/cloudinary/analytics`),
        adminApi.get(`/admin/cloudinary/projects`),
      ]);
      setSummary(resSummary.data);
      setAnalytics(resAnalytics.data);
      setProjects(resProjects.data);
      setError(null);
    } catch (err) {
      console.error("Failed to load storage dashboard metrics:", err);
      setError("Unable to retrieve storage console metrics. Verify that you are signed in as an administrator.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCore();
    // Health scan (a full Cloudinary + R2 object listing) is intentionally
    // NOT fetched here — it used to run unconditionally on every page load
    // and measured ~19s by itself. It now only runs when the admin clicks
    // "Scan Now" / "Re-Scan" below.
  }, [fetchCore]);

  const runHealthScan = async () => {
    setHealthLoading(true);
    try {
      const res = await adminApi.get(`/admin/cloudinary/health`);
      setHealth(res.data);
      setHealthScanned(true);
    } catch (err) {
      console.error("Health scan failed:", err);
    } finally {
      setHealthLoading(false);
    }
  };

  const handleOneClickCleanup = async () => {
    setHealthLoading(true);
    try {
      const res = await adminApi.post(`/admin/cloudinary/health/cleanup`);
      alert(`Cleanup completed! Cleaned ${res.data.cleaned_orphaned} orphaned files, ${res.data.cleaned_broken} broken references, and ${res.data.cleaned_unused} unused files.`);
      await runHealthScan();
      await fetchCore();
    } catch (err) {
      alert("Failed to run storage cleanup");
    } finally {
      setHealthLoading(false);
    }
  };

  const toggleProject = async (projectId) => {
    if (expandedProject === projectId) {
      setExpandedProject(null);
      return;
    }
    setExpandedProject(projectId);
    if (!projectTalents[projectId]) {
      setTalentsLoading(projectId);
      try {
        const res = await adminApi.get(`/admin/cloudinary/projects/${projectId}/talents`);
        setProjectTalents((prev) => ({ ...prev, [projectId]: res.data }));
      } catch (err) {
        console.error("Failed to load project talents:", err);
        setProjectTalents((prev) => ({ ...prev, [projectId]: [] }));
      } finally {
        setTalentsLoading(null);
      }
    }
  };

  const handleArchive = async (projectId) => {
    try {
      await adminApi.post(`/admin/cloudinary/projects/${projectId}/archive`, {});
      fetchCore();
    } catch (err) {
      alert("Failed to archive project");
    }
  };

  const handleRestore = async (projectId) => {
    try {
      await adminApi.post(`/admin/cloudinary/projects/${projectId}/restore`, {});
      fetchCore();
    } catch (err) {
      alert("Failed to restore project");
    }
  };

  const refreshAfterDelete = async (projectId) => {
    // Re-pull this project's talent breakdown + the global totals so the
    // UI reflects the real post-delete Cloudinary state, not a guess.
    try {
      const res = await adminApi.get(`/admin/cloudinary/projects/${projectId}/talents`);
      setProjectTalents((prev) => ({ ...prev, [projectId]: res.data }));
    } catch (err) {
      console.error("Failed to refresh talent breakdown:", err);
    }
    fetchCore();
  };

  if (loading && !summary) {
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
          onClick={fetchCore}
          className="mt-2 h-11 px-4 text-xs font-semibold bg-slate-900 text-white rounded-md inline-flex items-center gap-2"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Try Again
        </button>
      </div>
    );
  }

  const creditsPercent = summary?.credits_used_percent;

  return (
    <div className="p-4 sm:p-6 md:p-8 space-y-6 md:space-y-8 max-w-7xl mx-auto">
      {/* HEADER */}
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold tracking-tight text-slate-900 font-display flex items-center gap-2">
            <HardDrive className="w-6 h-6 text-slate-800 shrink-0" />
            Storage &amp; Asset Management Console
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            Live Cloudinary usage, per-project / per-talent storage, and safe media deletion.
          </p>
        </div>
        <button
          onClick={fetchCore}
          disabled={loading}
          className="h-11 px-4 text-xs font-semibold bg-white border border-slate-200 rounded-md inline-flex items-center gap-2 hover:bg-slate-50 transition-colors shadow-sm shrink-0"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* CLOUDINARY LIVE USAGE (single source of truth) */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px]">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Cloudinary Storage Used</span>
            <div className="text-3xl font-bold mt-2 text-slate-900 font-display">
              {formatBytes(summary?.storage_bytes)}
            </div>
            <p className="text-xs text-slate-500 mt-1">Live account usage, not an estimate</p>
          </div>
          <div className="absolute right-4 bottom-4 p-2.5 bg-slate-50 rounded-full">
            <Cloud className="w-5 h-5 text-slate-500" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px]">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Total Objects</span>
            <div className="text-3xl font-bold mt-2 text-slate-950 font-display">
              {summary?.object_count ?? "—"}
            </div>
            <p className="text-xs text-slate-500 mt-1">{summary?.resources ?? 0} originals + {summary?.derived_resources ?? 0} derived</p>
          </div>
          <div className="absolute right-4 bottom-4 p-2.5 bg-emerald-50 rounded-full">
            <Database className="w-5 h-5 text-emerald-600" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[130px] sm:col-span-2">
          <div>
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              {summary?.plan || "Cloudinary"} Plan — Credit Usage
            </span>
            <div className="text-3xl font-bold mt-2 text-slate-950 font-display">
              {summary?.credits_used != null ? summary.credits_used.toFixed(1) : "—"}
              {summary?.credits_limit != null && (
                <span className="text-base font-normal text-slate-400"> / {summary.credits_limit} credits</span>
              )}
            </div>
            {summary?.credits_used_percent != null ? (
              <>
                <div className="w-full bg-slate-100 rounded-full h-2 mt-3">
                  <div
                    className={`h-2 rounded-full transition-all duration-500 ${
                      creditsPercent > 100 ? "bg-red-500" : creditsPercent > 80 ? "bg-amber-500" : "bg-slate-900"
                    }`}
                    style={{ width: `${Math.min(100, creditsPercent)}%` }}
                  />
                </div>
                <p className={`text-xs mt-1.5 ${creditsPercent > 100 ? "text-red-600 font-semibold" : "text-slate-500"}`}>
                  {creditsPercent.toFixed(1)}% of plan credits used this period
                  {creditsPercent > 100 ? " — over plan limit" : ""}
                </p>
              </>
            ) : (
              <p className="text-xs text-slate-500 mt-1">
                Pay-as-you-go plans meter storage + bandwidth + transformations as combined credits — there is no separate fixed storage GB cap to show.
              </p>
            )}
          </div>
        </div>
      </div>

      {/* STORAGE HEALTH & CLEANUP — lazy, explicit scan only */}
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-6">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <div>
            <h3 className="text-base font-bold text-slate-900 font-display flex items-center gap-2">
              <Activity className="w-5 h-5 text-slate-700" />
              Storage Health Scan
            </h3>
            <p className="text-xs text-slate-500 mt-1">
              Scans every Cloudinary object for orphaned files, broken references, duplicates, and unused uploads.
              Runs only when you ask — a full scan takes ~15-20s.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={runHealthScan}
              disabled={healthLoading}
              className="h-10 px-3 text-xs bg-slate-50 hover:bg-slate-100 border border-slate-200 text-slate-700 rounded-md inline-flex items-center gap-1.5"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${healthLoading ? "animate-spin" : ""}`} />
              {healthScanned ? "Re-Scan" : "Scan Now"}
            </button>
            {healthScanned && (
              <button
                onClick={handleOneClickCleanup}
                disabled={healthLoading || !health || (health.orphaned_count === 0 && health.broken_count === 0 && health.unused_count === 0)}
                className="h-10 px-4 text-xs font-semibold bg-slate-950 text-white rounded-md inline-flex items-center gap-2 hover:bg-slate-800 disabled:opacity-40"
              >
                <Sparkles className="w-3.5 h-3.5" />
                One-Click Repair &amp; Cleanup
              </button>
            )}
          </div>
        </div>

        {healthScanned && health && (
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
              <p className="text-[10px] text-slate-500">Failed files &amp; deleted projects</p>
            </div>
          </div>
        )}
      </div>

      {/* CATEGORIES BREAKDOWN */}
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-4">
        <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
          <Database className="w-4 h-4 text-slate-500" />
          Storage by Asset Category
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {analytics?.categories && Object.entries(analytics.categories).map(([key, cat]) => (
            <div key={key} className="p-4 border border-slate-100 rounded-xl hover:bg-slate-50/50 transition-colors flex items-center gap-3">
              <div className="p-2 bg-slate-50 border border-slate-200/50 rounded-lg shrink-0">
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
              <div className="min-w-0">
                <span className="text-xs text-slate-500 block font-medium truncate">{cat.label}</span>
                <span className="text-sm font-bold text-slate-800 font-mono block">
                  {formatBytes(cat.size)}
                </span>
                <span className="text-[10px] text-slate-400">{cat.count} files</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* TOP CONSUMERS */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 lg:gap-8">
        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6 space-y-4">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
            <FolderOpen className="w-4 h-4 text-slate-400" />
            Top Campaigns by Storage
          </h3>
          <div className="divide-y divide-slate-100">
            {analytics?.top_projects?.map((item) => (
              <div key={item.project_id || "permanent"} className="py-3 flex justify-between items-center gap-2">
                <span className="text-sm font-medium text-slate-700 truncate" title={item.name}>
                  {item.name}
                </span>
                <span className="text-xs font-mono font-semibold text-slate-500 shrink-0">
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
            <Users className="w-4 h-4 text-slate-400" />
            Top Talents by Storage
          </h3>
          <div className="divide-y divide-slate-100">
            {analytics?.top_talents?.map((item) => (
              <div key={item.talent_id} className="py-3 flex justify-between items-center gap-2">
                <span className="text-sm font-medium text-slate-700 truncate" title={item.name}>
                  {item.name}
                </span>
                <span className="text-xs font-mono font-semibold text-slate-500 shrink-0">
                  {formatBytes(item.size)}
                </span>
              </div>
            ))}
            {(!analytics?.top_talents || analytics.top_talents.length === 0) && (
              <p className="text-xs text-slate-400 py-4">No talents recorded.</p>
            )}
          </div>
        </div>
      </div>

      {/* PROJECT -> TALENT STORAGE */}
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
        <div className="px-4 sm:px-6 py-5 border-b border-slate-100">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider">Project Storage</h3>
        </div>
        <div className="divide-y divide-slate-100">
          {projects.map((proj) => (
            <ProjectRow
              key={proj.project_id || "permanent"}
              project={proj}
              expanded={expandedProject === proj.project_id}
              talents={projectTalents[proj.project_id]}
              talentsLoading={talentsLoading === proj.project_id}
              onToggle={() => toggleProject(proj.project_id)}
              onArchive={() => handleArchive(proj.project_id)}
              onRestore={() => handleRestore(proj.project_id)}
              onManageTalent={(talent) =>
                setManageTarget({
                  project_id: proj.project_id,
                  project_name: proj.name,
                  talent_id: talent.talent_id,
                  talent_name: talent.talent_name,
                })
              }
            />
          ))}
          {projects.length === 0 && (
            <div className="text-center py-8 text-slate-400 text-xs">No projects found.</div>
          )}
        </div>
      </div>

      {manageTarget && (
        <TalentMediaModal
          target={manageTarget}
          onClose={() => setManageTarget(null)}
          onDeleted={() => refreshAfterDelete(manageTarget.project_id)}
        />
      )}
    </div>
  );
}

function ProjectRow({ project, expanded, talents, talentsLoading, onToggle, onArchive, onRestore, onManageTalent }) {
  return (
    <div>
      <div className="px-4 sm:px-6 py-4 hover:bg-slate-50/50 transition-colors">
        <div className="flex items-center gap-3">
          <button
            onClick={onToggle}
            className="shrink-0 p-1 -ml-1 rounded hover:bg-slate-100"
            aria-label={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <ChevronDown className="w-4 h-4 text-slate-500" /> : <ChevronRight className="w-4 h-4 text-slate-500" />}
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={`font-semibold truncate ${project.status === "deleted" ? "text-red-600" : "text-slate-900"}`}>
                {project.name}
              </span>
              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider shrink-0 ${
                project.status === "archived"
                  ? "bg-amber-50 text-amber-700 border border-amber-200/50"
                  : project.status === "deleted" || project.status === "purged"
                  ? "bg-red-50 text-red-700 border border-red-200/50"
                  : "bg-emerald-50 text-emerald-700 border border-emerald-200/50"
              }`}>
                {project.status}
              </span>
            </div>
            <div className="text-[10px] text-slate-400 font-mono mt-0.5 select-all truncate">
              {project.project_id || "no project id (permanent/system)"}
            </div>
          </div>
          <div className="text-right shrink-0">
            <div className="text-sm font-mono font-semibold text-slate-700">{formatBytes(project.total_storage)}</div>
            <div className="text-[10px] text-slate-400 flex items-center gap-1 justify-end">
              <Users className="w-3 h-3" /> {project.talent_count} talent{project.talent_count === 1 ? "" : "s"}
            </div>
          </div>
          <div className="hidden sm:flex items-center gap-1.5 shrink-0">
            {project.status === "active" && (
              <button onClick={onArchive} className="h-8 px-2 text-[11px] bg-slate-50 border border-slate-200 hover:bg-slate-100 rounded inline-flex items-center gap-1 text-slate-600">
                <Archive className="w-3 h-3" /> Archive
              </button>
            )}
            {project.status === "archived" && (
              <button onClick={onRestore} className="h-8 px-2 text-[11px] bg-slate-50 border border-slate-200 hover:bg-slate-100 rounded inline-flex items-center gap-1 text-slate-600">
                <RotateCcw className="w-3 h-3" /> Restore
              </button>
            )}
          </div>
        </div>
      </div>

      {expanded && (
        <div className="bg-slate-50/60 border-t border-slate-100 px-4 sm:px-6 py-3 space-y-2">
          {talentsLoading && (
            <div className="text-xs text-slate-400 py-4 flex items-center gap-2">
              <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Loading talents…
            </div>
          )}
          {!talentsLoading && talents && talents.length === 0 && (
            <p className="text-xs text-slate-400 py-2">No talent media found for this project.</p>
          )}
          {!talentsLoading && talents && talents.map((t) => (
            <div key={t.talent_id} className="bg-white border border-slate-200/70 rounded-xl p-3 sm:p-4 flex flex-col sm:flex-row sm:items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-sm text-slate-900 truncate">{t.talent_name}</div>
                <div className="flex flex-wrap gap-x-4 gap-y-0.5 mt-1 text-[11px] text-slate-500 font-mono">
                  <span>Audition {formatBytes(t.audition_videos)}</span>
                  <span>Intro {formatBytes(t.intro_videos)}</span>
                  <span>Images {formatBytes(t.images)}</span>
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <span className="text-sm font-mono font-semibold text-slate-700">{formatBytes(t.total)}</span>
                <button
                  onClick={() => onManageTalent(t)}
                  className="h-9 px-3 text-xs font-semibold bg-slate-900 text-white rounded-md hover:bg-slate-800"
                >
                  Manage
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TalentMediaModal({ target, onClose, onDeleted }) {
  const [loading, setLoading] = useState(true);
  const [media, setMedia] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(null); // { kind, label, action }
  const [selectedImageIds, setSelectedImageIds] = useState(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await adminApi.get(`/admin/cloudinary/projects/${target.project_id}/talents/${target.talent_id}`);
      setMedia(res.data.media || []);
      setError(null);
    } catch (err) {
      console.error("Failed to load talent media detail:", err);
      setError("Failed to load media for this talent.");
    } finally {
      setLoading(false);
    }
  }, [target.project_id, target.talent_id]);

  useEffect(() => {
    load();
  }, [load]);

  const auditionItems = media.filter((m) => m.category === "audition_videos");
  const introItems = media.filter((m) => m.category === "intro_videos");
  const imageItems = media.filter((m) => m.category !== "audition_videos" && m.category !== "intro_videos");

  const runDelete = async (fn, kind) => {
    setBusy(true);
    try {
      await fn();
      setConfirm(null);
      setSelectedImageIds(new Set());
      await load();
      onDeleted();
    } catch (err) {
      alert(`Failed to delete ${kind}. Nothing was changed.`);
    } finally {
      setBusy(false);
    }
  };

  const deleteAuditions = () =>
    runDelete(
      () => adminApi.delete(`/admin/cloudinary/projects/${target.project_id}/talents/${target.talent_id}/auditions`),
      "audition takes"
    );
  const deleteIntro = () =>
    runDelete(
      () => adminApi.delete(`/admin/cloudinary/projects/${target.project_id}/talents/${target.talent_id}/intro-video`),
      "introduction video"
    );
  const deleteSelectedImages = () =>
    runDelete(
      () =>
        adminApi.post(`/admin/cloudinary/projects/${target.project_id}/talents/${target.talent_id}/images/delete`, {
          media_ids: Array.from(selectedImageIds),
        }),
      "selected images"
    );

  const toggleImage = (id) => {
    setSelectedImageIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-3 sm:p-4">
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-2xl max-w-2xl w-full max-h-[85vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100 shrink-0">
          <div className="min-w-0">
            <h3 className="text-base font-bold text-slate-950 font-display truncate">{target.talent_name}</h3>
            <p className="text-xs text-slate-500 truncate">{target.project_name}</p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded hover:bg-slate-100 shrink-0" aria-label="Close">
            <X className="w-4 h-4 text-slate-500" />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-4 space-y-6 flex-1">
          {loading && (
            <div className="text-sm text-slate-400 flex items-center gap-2 py-8 justify-center">
              <RefreshCw className="w-4 h-4 animate-spin" /> Loading media…
            </div>
          )}
          {error && <p className="text-sm text-red-600">{error}</p>}

          {!loading && !error && (
            <>
              {/* Audition Takes */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                    <Video className="w-3.5 h-3.5" /> Audition Takes ({auditionItems.length})
                  </h4>
                  {auditionItems.length > 0 && (
                    <button
                      onClick={() => setConfirm({ kind: "auditions", label: `Delete all ${auditionItems.length} audition take(s)?`, action: deleteAuditions })}
                      className="h-8 px-2.5 text-[11px] font-semibold bg-red-50 border border-red-100 text-red-600 hover:bg-red-100 rounded inline-flex items-center gap-1"
                    >
                      <Trash2 className="w-3 h-3" /> Delete Audition Takes
                    </button>
                  )}
                </div>
                {auditionItems.length === 0 ? (
                  <p className="text-xs text-slate-400">No audition takes.</p>
                ) : (
                  <ul className="space-y-1">
                    {auditionItems.map((m) => (
                      <li key={m.media_id} className="text-xs text-slate-600 font-mono flex justify-between">
                        <span className="truncate">{m.original_filename || m.public_id}</span>
                        <span className="text-slate-400 shrink-0 ml-2">{formatBytes(m.size)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* Introduction Video */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                    <FileVideo className="w-3.5 h-3.5" /> Introduction Video
                  </h4>
                  {introItems.length > 0 && (
                    <button
                      onClick={() => setConfirm({ kind: "intro", label: "Delete the introduction video?", action: deleteIntro })}
                      className="h-8 px-2.5 text-[11px] font-semibold bg-red-50 border border-red-100 text-red-600 hover:bg-red-100 rounded inline-flex items-center gap-1"
                    >
                      <Trash2 className="w-3 h-3" /> Delete Introduction Video
                    </button>
                  )}
                </div>
                {introItems.length === 0 ? (
                  <p className="text-xs text-slate-400">No introduction video.</p>
                ) : (
                  <ul className="space-y-1">
                    {introItems.map((m) => (
                      <li key={m.media_id} className="text-xs text-slate-600 font-mono flex justify-between">
                        <span className="truncate">{m.original_filename || m.public_id}</span>
                        <span className="text-slate-400 shrink-0 ml-2">{formatBytes(m.size)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* Images */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                    <ImageIcon className="w-3.5 h-3.5" /> Images ({imageItems.length})
                  </h4>
                  {selectedImageIds.size > 0 && (
                    <button
                      onClick={() => setConfirm({ kind: "images", label: `Delete ${selectedImageIds.size} selected image(s)?`, action: deleteSelectedImages })}
                      className="h-8 px-2.5 text-[11px] font-semibold bg-red-50 border border-red-100 text-red-600 hover:bg-red-100 rounded inline-flex items-center gap-1"
                    >
                      <Trash2 className="w-3 h-3" /> Delete Selected ({selectedImageIds.size})
                    </button>
                  )}
                </div>
                {imageItems.length === 0 ? (
                  <p className="text-xs text-slate-400">No images.</p>
                ) : (
                  <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                    {imageItems.map((m) => (
                      <label
                        key={m.media_id}
                        className={`relative block rounded-lg overflow-hidden border-2 cursor-pointer ${
                          selectedImageIds.has(m.media_id) ? "border-slate-900" : "border-transparent"
                        }`}
                      >
                        <input
                          type="checkbox"
                          className="absolute top-1.5 left-1.5 z-10 w-4 h-4 accent-slate-900"
                          checked={selectedImageIds.has(m.media_id)}
                          onChange={() => toggleImage(m.media_id)}
                        />
                        <img
                          src={m.thumbnail_url || m.url}
                          alt={m.original_filename || m.raw_category || "image"}
                          className="w-full h-20 object-cover bg-slate-100"
                          loading="lazy"
                        />
                        <span className="absolute bottom-0 inset-x-0 bg-black/50 text-white text-[9px] px-1 py-0.5 truncate">
                          {formatBytes(m.size)}
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>

        {confirm && (
          <div className="absolute inset-0 bg-slate-900/50 flex items-center justify-center p-4 rounded-2xl">
            <div className="bg-white rounded-xl border border-slate-200 shadow-xl max-w-sm w-full p-5 space-y-4">
              <div className="flex items-center gap-2 text-red-600">
                <AlertTriangle className="w-5 h-5 shrink-0" />
                <p className="text-sm font-semibold text-slate-900">{confirm.label}</p>
              </div>
              <p className="text-xs text-slate-500">
                Media shared with the talent's global profile or another submission will only have this reference
                removed — the underlying file stays intact wherever else it's used.
              </p>
              <div className="flex justify-end gap-2">
                <button onClick={() => setConfirm(null)} disabled={busy} className="h-9 px-3 text-xs font-semibold bg-white border border-slate-200 rounded-md">
                  Cancel
                </button>
                <button
                  onClick={confirm.action}
                  disabled={busy}
                  className="h-9 px-3 text-xs font-semibold bg-red-600 hover:bg-red-700 text-white rounded-md inline-flex items-center gap-1.5 disabled:opacity-50"
                >
                  {busy ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                  Confirm Delete
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
