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
  FolderOpen
} from "lucide-react";
import { adminApi } from "@/lib/api";

export default function StorageDashboard() {
  const [analytics, setAnalytics] = useState(null);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [confirmDialog, setConfirmDialog] = useState(null); // { type, id, data }

  // NOTE: use the shared `adminApi` instance (baseURL = `${BACKEND_URL}/api`)
  // exactly like every other admin page. Its request interceptor attaches the
  // admin JWT from `tg_admin_token` and its response interceptor handles 401s.
  // The previous hand-rolled header read `getAdmin()?.token`, but the admin
  // *profile* object carries no token (the token lives in `tg_admin_token`),
  // so it always sent an empty Bearer and the cloudinary endpoints 401'd.

  const fetchData = async () => {
    setLoading(true);
    try {
      const [resAnal, resProj] = await Promise.all([
        adminApi.get(`/admin/cloudinary/analytics`),
        adminApi.get(`/admin/cloudinary/projects`)
      ]);
      setAnalytics(resAnal.data);
      setProjects(resProj.data);
      setError(null);
    } catch (err) {
      console.error("Failed to load storage dashboard metrics:", err);
      setError("Unable to retrieve Cloudinary storage metrics. Verify that you are signed in as an administrator.");
    } finally {
      setLoading(false);
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

  const handleDeleteProject = async (projectId) => {
    try {
      await adminApi.delete(`/admin/cloudinary/projects/${projectId}`);
      setConfirmDialog(null);
      fetchData();
    } catch (err) {
      alert("Failed to delete project");
    }
  };

  const formatBytes = (bytes, decimals = 2) => {
    if (!bytes) return "0 Bytes";
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
          className="mt-2 h-11 px-4 text-xs font-semibold bg-primary text-white rounded-md inline-flex items-center gap-2"
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
            <Cloud className="w-6 h-6 text-slate-700" />
            Cloudinary Storage Management
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            Production storage cost controls, project lifetimes, and asset lifecycle tracking.
          </p>
        </div>
        <button 
          onClick={fetchData} 
          disabled={loading}
          className="h-11 px-4 text-xs font-semibold bg-white border border-slate-200 rounded-md inline-flex items-center gap-2 hover:bg-slate-50 transition-colors"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh Metrics
        </button>
      </div>

      {/* STORAGE STATS CARDS */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
        <div className="bg-white p-6 rounded-lg border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[120px]">
          <div>
            <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Total Cloudinary Storage</span>
            <div className="text-2xl font-semibold mt-2 text-slate-900 font-display">
              {formatBytes(analytics?.total_storage)}
            </div>
          </div>
          <div className="absolute right-4 bottom-4 p-2 bg-slate-50 rounded-full">
            <Database className="w-5 h-5 text-slate-400" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-lg border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[120px]">
          <div>
            <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Permanent Assets Storage</span>
            <div className="text-2xl font-semibold mt-2 text-slate-900 font-display">
              {formatBytes(analytics?.permanent_storage)}
            </div>
          </div>
          <div className="absolute right-4 bottom-4 p-2 bg-slate-50 rounded-full">
            <CheckCircle className="w-5 h-5 text-emerald-500" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-lg border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[120px]">
          <div>
            <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Temporary Auditions Storage</span>
            <div className="text-2xl font-semibold mt-2 text-slate-900 font-display">
              {formatBytes(analytics?.temporary_storage)}
            </div>
          </div>
          <div className="absolute right-4 bottom-4 p-2 bg-slate-50 rounded-full">
            <TrendingUp className="w-5 h-5 text-blue-500" />
          </div>
        </div>

        <div className="bg-white p-6 rounded-lg border border-slate-200/80 shadow-sm relative overflow-hidden flex flex-col justify-between min-h-[120px]">
          <div>
            <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Archived Projects Storage</span>
            <div className="text-2xl font-semibold mt-2 text-slate-900 font-display">
              {formatBytes(analytics?.archived_storage)}
            </div>
          </div>
          <div className="absolute right-4 bottom-4 p-2 bg-slate-50 rounded-full">
            <Archive className="w-5 h-5 text-amber-500" />
          </div>
        </div>
      </div>

      {/* ANALYSIS SUB-SECTIONS */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* TOP STORAGE CONSUMING PROJECTS */}
        <div className="bg-white rounded-lg border border-slate-200/80 shadow-sm p-6 space-y-4">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
            <FolderOpen className="w-4 h-4 text-slate-400" />
            Top Projects by Storage
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

        {/* TOP STORAGE CONSUMING TALENTS */}
        <div className="bg-white rounded-lg border border-slate-200/80 shadow-sm p-6 space-y-4">
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

        {/* METRICS & AVERAGES */}
        <div className="bg-white rounded-lg border border-slate-200/80 shadow-sm p-6 space-y-4">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider flex items-center gap-2">
            <FileVideo className="w-4 h-4 text-slate-400" />
            Audition Video Metrics
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
      <div className="bg-white rounded-lg border border-slate-200/80 shadow-sm overflow-hidden">
        <div className="px-6 py-5 border-b border-slate-100 flex justify-between items-center">
          <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider">Projects Storage & Lifecycles</h3>
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
                  <td className="px-6 py-4 font-medium text-slate-900">{proj.name}</td>
                  <td className="px-6 py-4">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium uppercase tracking-wider ${
                      proj.status === "archived" 
                        ? "bg-amber-50 text-amber-700 border border-amber-200/50" 
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
                        className="h-10 px-3 text-xs bg-slate-50 border border-slate-200 hover:bg-slate-100 rounded inline-flex items-center gap-1.5 text-slate-600 transition-colors"
                      >
                        <Archive className="w-3.5 h-3.5" />
                        Archive
                      </button>
                    ) : proj.status === "archived" ? (
                      <button 
                        onClick={() => handleRestore(proj.project_id)}
                        className="h-10 px-3 text-xs bg-slate-50 border border-slate-200 hover:bg-slate-100 rounded inline-flex items-center gap-1.5 text-slate-600 transition-colors"
                      >
                        <RefreshCw className="w-3.5 h-3.5" />
                        Restore
                      </button>
                    ) : null}

                    {proj.status !== "purged" && (
                      <button 
                        onClick={() => setConfirmDialog({ type: "project", id: proj.project_id, data: proj })}
                        className="h-10 px-3 text-xs bg-red-50/50 border border-red-100 hover:bg-red-50 hover:text-red-700 rounded inline-flex items-center gap-1.5 text-red-600 transition-colors"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                        Purge Folder
                      </button>
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

      {/* CONFIRM DELETE DIALOG OVERLAY */}
      {confirmDialog && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-4 animate-fade-in">
          <div className="bg-white rounded-lg border border-slate-200/80 shadow-2xl max-w-md w-full p-6 space-y-4">
            <div className="flex items-center gap-3 text-red-600">
              <AlertTriangle className="w-6 h-6 shrink-0" />
              <h3 className="text-lg font-semibold text-slate-950 font-display">Permanent Deletion Warning</h3>
            </div>
            
            <p className="text-sm text-slate-600">
              Are you sure you want to permanently delete project <strong>{confirmDialog.data.name}</strong>?
            </p>
            <div className="bg-red-50/50 border border-red-100 rounded p-3 text-xs text-red-700 space-y-1">
              <p>• This will permanently nuke the entire Cloudinary project folder:</p>
              <p className="font-mono bg-red-50 p-1.5 rounded select-all break-all text-[11px] mt-1.5">
                projects/{confirmDialog.id}/
              </p>
              <p className="mt-2 font-medium">This action cannot be undone.</p>
            </div>

            <div className="flex justify-end gap-3 pt-2">
              <button 
                onClick={() => setConfirmDialog(null)}
                className="h-11 px-4 text-xs font-semibold bg-white border border-slate-200 hover:bg-slate-50 text-slate-700 rounded-md"
              >
                Cancel
              </button>
              <button 
                onClick={() => handleDeleteProject(confirmDialog.id)}
                className="h-11 px-4 text-xs font-semibold bg-red-600 hover:bg-red-700 text-white rounded-md flex items-center gap-2"
              >
                <Trash2 className="w-3.5 h-3.5" />
                Confirm Deletion
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
