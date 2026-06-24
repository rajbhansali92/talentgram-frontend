import React, { useState, useEffect } from "react";
import { 
  Play, Pause, Square, QrCode, RefreshCw, AlertTriangle, CheckCircle, 
  Settings, Clock, ShieldAlert, History, Edit, Send, Save, Plus, Trash2, Database, AlertCircle
} from "lucide-react";
import { toast } from "sonner";

import {
  getTemplates, createTemplate, updateTemplate, deleteTemplate,
  getPipelineSummary,
  createBatch, getBatches, runBatchAction,
  getJobs, retryJob,
  getSessionStatus, clearQrCode, resetSession,
  getWaConfig, updateWaConfig,
  getAuditLog,
  resolveTargets, getCrmContactTypes, validateManual,
} from "@/lib/whatsappApi";
import VirtualList from "@/components/VirtualList";
import ProjectSearchModal from "@/components/ProjectSearchModal";

export default function WhatsAppEnginePage() {
  const [activeTab, setActiveTab] = useState("session"); // session | templates | campaign | history | config | audit

  return (
    <div className="min-h-screen bg-[#f8f8f6] p-4 md:p-8 text-black">
      <div className="max-w-7xl mx-auto space-y-6">
        
        {/* Header */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-black/[0.06] pb-5">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">WhatsApp Engine</h1>
            <p className="text-black/50 text-sm mt-1">
              Broadcast template updates, status notifications, and media attachments directly via automated browser execution.
            </p>
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="flex flex-wrap gap-2 border-b border-black/[0.06] pb-px">
          {[
            { id: "session", label: "Session & Status", icon: QrCode },
            { id: "campaign", label: "Launch Campaign", icon: Send },
            { id: "history", label: "Campaign History", icon: History },
            { id: "templates", label: "Templates", icon: Edit },
            { id: "config", label: "Safety Configuration", icon: Settings },
            { id: "audit", label: "Audit Logs", icon: ShieldAlert },
          ].map((tab) => {
            const Icon = tab.icon;
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2.5 text-xs font-semibold uppercase tracking-wider border-b-2 transition-all ${
                  active 
                    ? "border-black text-black" 
                    : "border-transparent text-black/40 hover:text-black/70 hover:border-black/10"
                }`}
              >
                <Icon className="w-4 h-4" />
                {tab.label}
              </button>
            );
          })}
        </div>

        {/* Tab Contents */}
        <div className="mt-6">
          {activeTab === "session" && <WESessionPanel />}
          {activeTab === "campaign" && <WECampaignLauncher />}
          {activeTab === "history" && <WEHistoryPanel />}
          {activeTab === "templates" && <WETemplateManager />}
          {activeTab === "config" && <WEConfigPanel />}
          {activeTab === "audit" && <WEAuditLogPanel />}
        </div>

      </div>
    </div>
  );
}

// ==========================================
// 1. SESSION PANEL (QR & Connection Status)
// ==========================================
function WESessionPanel() {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(true);

  const fetchSession = async () => {
    try {
      const data = await getSessionStatus();
      setSession(data);
    } catch (err) {
      console.error("Failed to load WhatsApp session status", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSession();
    let timer;
    if (polling) {
      timer = setInterval(fetchSession, 4000);
    }
    return () => clearInterval(timer);
  }, [polling]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center p-12 bg-white rounded-md border border-black/[0.06] min-h-[300px]">
        <RefreshCw className="w-8 h-8 animate-spin text-black/40 mb-3" />
        <p className="text-black/50 text-sm">Querying WhatsApp Web session state...</p>
      </div>
    );
  }

  const status = session?.status || "disconnected";
  const qrBase64 = session?.qr_code_base64;
  const errMsg = session?.error_message;
  const heartbeat = session?.last_heartbeat;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      
      {/* Status Summary */}
      <div className="bg-white p-6 rounded-md border border-black/[0.06] space-y-4">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Connection Status</h3>
        
        <div className="flex items-center gap-3">
          <div className={`w-3.5 h-3.5 rounded-full ${
            status === "authenticated" ? "bg-emerald-500 animate-pulse" :
            status === "qr_pending" ? "bg-amber-500 animate-pulse" : "bg-red-500"
          }`} />
          <span className="text-base font-semibold capitalize">{status.replace("_", " ")}</span>
        </div>

        <div className="border-t border-black/[0.06] pt-4 space-y-2 text-xs">
          <div className="flex justify-between">
            <span className="text-black/40 font-medium">Session Instance</span>
            <span className="font-mono text-black/80">default</span>
          </div>
          <div className="flex justify-between">
            <span className="text-black/40 font-medium">Last Heartbeat</span>
            <span className="text-black/80 font-mono">
              {heartbeat ? new Date(heartbeat).toLocaleTimeString() : "Never"}
            </span>
          </div>
          {session?.authenticated_at && (
            <div className="flex justify-between">
              <span className="text-black/40 font-medium">Authenticated At</span>
              <span className="text-black/80">
                {new Date(session.authenticated_at).toLocaleString()}
              </span>
            </div>
          )}
        </div>

        {errMsg && (
          <div className="p-3 bg-red-50 border border-red-200/50 rounded-sm text-red-700 text-xs flex gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold">Session Error</p>
              <p className="mt-0.5 opacity-90">{errMsg}</p>
            </div>
          </div>
        )}

        <div className="border-t border-black/[0.06] pt-4 space-y-2">
          <button
            onClick={() => setPolling(!polling)}
            className="text-xs font-semibold uppercase tracking-wider text-black/60 hover:text-black border border-black/10 px-3 py-2 rounded-sm w-full text-center transition-colors"
          >
            {polling ? "Pause Real-time Sync" : "Resume Real-time Sync"}
          </button>
          <button
            onClick={async () => {
              if (!window.confirm("Reset the WhatsApp session? This unlinks the current device and requires scanning a new QR code. The worker must restart to complete the reset.")) return;
              try {
                await resetSession();
                toast.success("Session reset requested. Restart the worker, then scan the new QR code.");
                fetchSession();
              } catch (err) {
                toast.error(err?.response?.data?.detail || "Failed to reset session.");
              }
            }}
            className="text-xs font-semibold uppercase tracking-wider text-red-600 hover:text-white hover:bg-red-600 border border-red-200 px-3 py-2 rounded-sm w-full text-center transition-colors"
          >
            Reset WhatsApp Session
          </button>
        </div>
      </div>

      {/* QR Code Container */}
      <div className="lg:col-span-2 bg-white p-6 rounded-md border border-black/[0.06] flex flex-col items-center justify-center min-h-[300px]">
        {status === "authenticated" ? (
          <div className="text-center space-y-3 p-6 max-w-sm">
            <div className="w-16 h-16 bg-emerald-50 rounded-full flex items-center justify-center mx-auto border border-emerald-100">
              <CheckCircle className="w-8 h-8 text-emerald-500" />
            </div>
            <h3 className="text-lg font-semibold">Active Session</h3>
            <p className="text-xs text-black/50">
              The worker process is fully authenticated to WhatsApp Web and listening for jobs. You can safely launch message broadcasts.
            </p>
          </div>
        ) : status === "qr_pending" && qrBase64 ? (
          <div className="text-center space-y-4 max-w-md">
            <h3 className="text-base font-semibold">Link WhatsApp Account</h3>
            <p className="text-xs text-black/50">
              Scan the QR code below using the linked devices option in your WhatsApp mobile application.
            </p>
            <div className="p-4 bg-white border border-black/[0.06] rounded-md inline-block shadow-inner mx-auto">
              <img src={qrBase64} alt="WhatsApp QR Code" className="w-56 h-56 mx-auto" />
            </div>
            <p className="text-[10px] text-black/40 italic">
              QR code updates automatically every 90 seconds.
            </p>
          </div>
        ) : (
          <div className="text-center space-y-3 p-6 max-w-sm">
            <div className="w-16 h-16 bg-amber-50 rounded-full flex items-center justify-center mx-auto border border-amber-100">
              <AlertTriangle className="w-8 h-8 text-amber-500" />
            </div>
            <h3 className="text-base font-semibold">Browser Launch Pending</h3>
            <p className="text-xs text-black/50">
              WhatsApp Web is disconnected or loading. Verify that the worker process is currently active and running.
            </p>
          </div>
        )}
      </div>

    </div>
  );
}

// ==========================================
// 2. CAMPAIGN LAUNCHER (Compose & Launch)
// ==========================================
function WECampaignLauncher() {
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [pipelineSummary, setPipelineSummary] = useState(null);
  const [selectedStages, setSelectedStages] = useState([]);
  
  const [templates, setTemplates] = useState([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [selectedTemplate, setSelectedTemplate] = useState(null);
  
  const [variables, setVariables] = useState({});
  const [mediaUrl, setMediaUrl] = useState("");

  const [recipients, setRecipients] = useState([]);
  const [unresolvable, setUnresolvable] = useState([]);
  const [resolving, setResolving] = useState(false);
  const [dryRunResult, setDryRunResult] = useState(null);
  const [launching, setLaunching] = useState(false);

  // Slice 5-7: unified targeting (PROJECT | CRM | MANUAL) + exclusion + search.
  const [sourceType, setSourceType] = useState("PROJECT");
  const [crmTypes, setCrmTypes] = useState([]);
  const [crmContactType, setCrmContactType] = useState("");
  const [manualText, setManualText] = useState("");
  const [excludedIds, setExcludedIds] = useState(() => new Set());
  const [selectedRowIds, setSelectedRowIds] = useState(() => new Set());
  const [targetSearch, setTargetSearch] = useState("");
  const [projectModalOpen, setProjectModalOpen] = useState(false);
  const [selectedProjectName, setSelectedProjectName] = useState("");

  useEffect(() => {
    const loadInitialData = async () => {
      try {
        const [tempList, types] = await Promise.all([
          getTemplates(), getCrmContactTypes().catch(() => []),
        ]);
        setTemplates(tempList);
        setCrmTypes(types);
      } catch (err) {
        console.error("Failed to load campaign selector options", err);
      }
    };
    loadInitialData();
  }, []);

  // Build the source_params for the unified resolver from the current source.
  const buildSourceParams = () => {
    if (sourceType === "PROJECT") {
      return { project_id: selectedProjectId, pipeline_stages: selectedStages };
    }
    if (sourceType === "CRM") {
      return { contact_type: crmContactType || null, select_all_filtered: true };
    }
    // MANUAL — parse "Name,+phone" lines
    const contacts = manualText.split("\n").map((line) => {
      const [name, phone] = line.split(",");
      return { name: (name || "").trim(), phone: (phone || "").trim() };
    }).filter((c) => c.phone);
    return { contacts };
  };

  const resetResolution = () => {
    setRecipients([]); setUnresolvable([]); setDryRunResult(null);
    setExcludedIds(new Set()); setSelectedRowIds(new Set()); setTargetSearch("");
  };

  const handleProjectChange = async (projectId) => {
    setSelectedProjectId(projectId);
    setPipelineSummary(null);
    setSelectedStages([]);
    setRecipients([]);
    setUnresolvable([]);
    setDryRunResult(null);
    
    if (!projectId) return;

    try {
      const summary = await getPipelineSummary(projectId);
      setPipelineSummary(summary);
    } catch (err) {
      toast.error("Failed to fetch project stages summary");
    }
  };

  const handleStageToggle = (stage) => {
    const updated = selectedStages.includes(stage)
      ? selectedStages.filter(s => s !== stage)
      : [...selectedStages, stage];
    setSelectedStages(updated);
    setRecipients([]);
    setDryRunResult(null);
  };

  const handleTemplateChange = (templateId) => {
    setSelectedTemplateId(templateId);
    setDryRunResult(null);
    const found = templates.find(t => t.id === templateId);
    setSelectedTemplate(found);
    
    if (found) {
      const initialVars = {};
      found.variables.forEach(v => {
        if (v !== "talent_name") {
          initialVars[v] = "";
        }
      });
      setVariables(initialVars);
      setMediaUrl(found.media_url || "");
    } else {
      setVariables({});
      setMediaUrl("");
    }
  };

  const handleResolve = async () => {
    if (sourceType === "PROJECT" && (!selectedProjectId || selectedStages.length === 0)) return;
    setResolving(true);
    try {
      const data = await resolveTargets({
        source_type: sourceType,
        source_params: buildSourceParams(),
        excluded_recipient_ids: [],
      });
      setRecipients(data.recipients || []);
      setUnresolvable(data.unresolvable || []);
      setExcludedIds(new Set());
      setSelectedRowIds(new Set());
      toast.success(`Resolved ${data.counts?.resolved ?? (data.recipients || []).length} recipients`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to resolve recipients");
    } finally {
      setResolving(false);
    }
  };

  // ---- exclusion / search helpers (Feature 3) ----
  const filteredRecipients = recipients.filter((r) => {
    if (!targetSearch.trim()) return true;
    const q = targetSearch.toLowerCase();
    return (r.name || "").toLowerCase().includes(q)
      || (r.phone || "").toLowerCase().includes(q)
      || (r.whatsapp_group_name || "").toLowerCase().includes(q)
      || (r.destination || "").toLowerCase().includes(q);
  });
  const sendingCount = recipients.filter((r) => !excludedIds.has(r.recipient_id)).length;

  const toggleRow = (rid) => {
    setSelectedRowIds((prev) => {
      const next = new Set(prev);
      next.has(rid) ? next.delete(rid) : next.add(rid);
      return next;
    });
  };
  const selectAllFiltered = (checked) => {
    setSelectedRowIds((prev) => {
      const next = new Set(prev);
      filteredRecipients.forEach((r) => checked ? next.add(r.recipient_id) : next.delete(r.recipient_id));
      return next;
    });
  };
  const applyExclusion = (exclude) => {
    setExcludedIds((prev) => {
      const next = new Set(prev);
      selectedRowIds.forEach((rid) => exclude ? next.add(rid) : next.delete(rid));
      return next;
    });
    setSelectedRowIds(new Set());
  };

  const _batchPayload = (isDryRun) => ({
    source_type: sourceType,
    source_params: buildSourceParams(),
    excluded_recipient_ids: Array.from(excludedIds),
    template_id: selectedTemplateId,
    variable_data: variables,
    media_url: mediaUrl || null,
    is_dry_run: isDryRun,
  });

  const handleDryRun = async () => {
    if (!selectedTemplateId) { toast.error("Select a template"); return; }
    setLaunching(true);
    try {
      const res = await createBatch(_batchPayload(true));
      setDryRunResult(res);
      toast.success("Dry run preview compiled successfully");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed compilation of dry run");
    } finally {
      setLaunching(false);
    }
  };

  const handleLaunch = async () => {
    if (!selectedTemplateId) { toast.error("Select a template"); return; }
    const confirm = window.confirm(`Launch this broadcast to ${sendingCount} recipients (${excludedIds.size} excluded)?`);
    if (!confirm) return;
    setLaunching(true);
    try {
      await createBatch(_batchPayload(false));
      toast.success("Campaign launched successfully to worker queue!");
      setSelectedProjectId("");
      setSelectedTemplateId("");
      setSelectedStages([]);
      setManualText("");
      setCrmContactType("");
      resetResolution();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed launching batch campaign");
    } finally {
      setLaunching(false);
    }
  };

  return (
    <div className="space-y-6">
      <ProjectSearchModal
        open={projectModalOpen}
        onClose={() => setProjectModalOpen(false)}
        onSelect={(p) => { handleProjectChange(p.id); setSelectedProjectName(p.name || p.brand_name || ""); }}
      />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* Configurations column */}
        <div className="lg:col-span-2 bg-white p-6 rounded-md border border-black/[0.06] space-y-6">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60 border-b border-black/[0.06] pb-3">Campaign Target & Template</h3>

          {/* Target source selector (Feature 1 / Slices 5-7) */}
          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Target Source</label>
            <div className="flex gap-2">
              {["PROJECT", "CRM", "MANUAL"].map((src) => (
                <button
                  key={src}
                  type="button"
                  onClick={() => { setSourceType(src); resetResolution(); }}
                  className={`text-xs font-semibold px-3 py-2 rounded-sm border transition-all ${
                    sourceType === src ? "bg-black text-white border-black" : "bg-[#f8f8f6] border-black/10 hover:border-black/30"
                  }`}
                  data-testid={`source-${src}`}
                >
                  {src === "PROJECT" ? "Project Pipeline" : src === "CRM" ? "Marketing CRM" : "Manual Contacts"}
                </button>
              ))}
            </div>
          </div>

          {/* CRM source */}
          {sourceType === "CRM" && (
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Contact Type</label>
              <select
                value={crmContactType}
                onChange={(e) => { setCrmContactType(e.target.value); resetResolution(); }}
                className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
              >
                <option value="">All contact types</option>
                {crmTypes.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
          )}

          {/* Manual source */}
          {sourceType === "MANUAL" && (
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Manual Contacts (one per line: Name,+countrycode…)</label>
              <textarea
                value={manualText}
                onChange={(e) => { setManualText(e.target.value); }}
                rows={5}
                placeholder={"Rahul Sharma,+919876543210\nPriya Jain,+919123456789"}
                className="w-full text-xs font-mono bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
                data-testid="manual-contacts-input"
              />
            </div>
          )}

          {/* Project & Stage selection */}
          {sourceType === "PROJECT" && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Casting Project</label>
              <button
                type="button"
                onClick={() => setProjectModalOpen(true)}
                className="w-full text-left text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 hover:border-black/30 flex items-center justify-between"
                data-testid="open-project-search"
              >
                <span className={selectedProjectId ? "text-black" : "text-black/40"}>
                  {selectedProjectName || "Search project…"}
                </span>
                <span className="text-[10px] uppercase tracking-wider text-black/40">{selectedProjectId ? "Change" : "Search"}</span>
              </button>
            </div>

            {selectedProjectId && pipelineSummary && (
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Target Pipeline Stages</label>
                <div className="flex flex-wrap gap-2 p-2 bg-[#f8f8f6] border border-black/10 rounded-sm max-h-[140px] overflow-y-auto">
                  {Object.entries(pipelineSummary.stage_counts).map(([stage, count]) => {
                    const active = selectedStages.includes(stage);
                    return (
                      <button
                        key={stage}
                        type="button"
                        onClick={() => handleStageToggle(stage)}
                        className={`text-xs px-2.5 py-1.5 rounded-sm border font-medium transition-all ${
                          active 
                            ? "bg-black text-white border-black" 
                            : "bg-white text-black/60 border-black/10 hover:border-black/20"
                        }`}
                      >
                        {stage} ({count})
                      </button>
                    );
                  })}
                  {Object.keys(pipelineSummary.stage_counts).length === 0 && (
                    <p className="text-[11px] text-black/40 p-1">No active stages in project.</p>
                  )}
                </div>
              </div>
            )}
          </div>
          )}

          {/* Resolve action — works for all sources (Slices 5-7) */}
          {(sourceType !== "PROJECT" || (selectedProjectId && selectedStages.length > 0)) && (
            <div className="bg-[#f8f8f6] p-4 rounded-sm border border-black/10 flex flex-col md:flex-row md:items-center justify-between gap-3">
              <div className="text-xs space-y-1">
                <span className="font-semibold">Recipients Resolution:</span>
                {recipients.length > 0 ? (
                  <p className="text-black/60">
                    Ready to send to <strong className="text-black">{sendingCount}</strong> targets.
                    {unresolvable.length > 0 && <span className="text-amber-600"> ({unresolvable.length} skipped due to missing identifiers).</span>}
                  </p>
                ) : (
                  <p className="text-black/40">Resolve targets before drafting placeholders.</p>
                )}
              </div>
              <button
                type="button"
                onClick={handleResolve}
                disabled={resolving || (sourceType === "MANUAL" && !manualText.trim())}
                className="text-xs font-semibold uppercase tracking-wider border border-black bg-white hover:bg-[#f8f8f6] disabled:opacity-50 px-3 py-2 rounded-sm"
              >
                {resolving ? "Resolving..." : "Resolve Targets"}
              </button>
            </div>
          )}

          {/* Resolved Targets — search (1), bulk exclusion (2), virtualized rows. */}
          {recipients.length > 0 && (
            <div className="border border-black/10 rounded-sm overflow-hidden">
              <div className="bg-[#f8f8f6] px-4 py-2 flex flex-wrap items-center justify-between gap-2 border-b border-black/10">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-black/60">
                  Resolved Targets ({recipients.length})
                </span>
                <span className="text-[11px] font-mono">
                  <strong className="text-emerald-700">Sending: {sendingCount}</strong>
                  {" · "}
                  <span className="text-red-600">Excluded: {excludedIds.size}</span>
                  {unresolvable.length > 0 && <span className="text-amber-600"> · {unresolvable.length} skipped</span>}
                </span>
              </div>

              {/* Search + bulk actions */}
              <div className="px-3 py-2 flex flex-wrap items-center gap-2 border-b border-black/[0.06]">
                <input
                  value={targetSearch}
                  onChange={(e) => setTargetSearch(e.target.value)}
                  placeholder="Search name / phone / group…"
                  className="flex-1 min-w-[160px] text-xs px-2 py-1.5 border border-black/15 rounded-sm focus:outline-none focus:border-black/40"
                  data-testid="target-search"
                />
                <button onClick={() => applyExclusion(true)} disabled={selectedRowIds.size === 0}
                  className="text-[10px] font-bold uppercase tracking-wider px-2 py-1.5 rounded-sm border border-red-200 text-red-700 hover:bg-red-50 disabled:opacity-40">
                  Exclude Selected
                </button>
                <button onClick={() => applyExclusion(false)} disabled={selectedRowIds.size === 0}
                  className="text-[10px] font-bold uppercase tracking-wider px-2 py-1.5 rounded-sm border border-emerald-200 text-emerald-700 hover:bg-emerald-50 disabled:opacity-40">
                  Include Selected
                </button>
              </div>

              {/* Header row */}
              <div className="flex items-center gap-2 px-3 py-1.5 text-[10px] uppercase tracking-wider text-black/40 border-b border-black/[0.06] bg-white">
                <input type="checkbox"
                  checked={filteredRecipients.length > 0 && filteredRecipients.every((r) => selectedRowIds.has(r.recipient_id))}
                  onChange={(e) => selectAllFiltered(e.target.checked)} />
                <span className="flex-1">Name</span>
                <span className="w-28">Type</span>
                <span className="flex-1">Destination</span>
              </div>

              {/* Virtualized rows — never renders the full list */}
              <VirtualList
                items={filteredRecipients}
                rowHeight={34}
                height={Math.min(filteredRecipients.length, 8) * 34 || 34}
                renderRow={(r) => {
                  const excluded = excludedIds.has(r.recipient_id);
                  const type = r.destination_type === "group" ? "GROUP" : "PHONE";
                  return (
                    <div className={`flex items-center gap-2 px-3 text-xs h-full border-b border-black/[0.03] ${excluded ? "opacity-40 line-through" : "hover:bg-[#f8f8f6]"}`}>
                      <input type="checkbox" checked={selectedRowIds.has(r.recipient_id)} onChange={() => toggleRow(r.recipient_id)} />
                      <span className="flex-1 font-medium truncate">{r.name || "—"}</span>
                      <span className="w-28">
                        <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded-sm ${type === "GROUP" ? "bg-indigo-50 text-indigo-700" : "bg-sky-50 text-sky-700"}`}>{type}</span>
                      </span>
                      <span className="flex-1 font-mono text-[11px] text-black/70 truncate">{r.destination}</span>
                    </div>
                  );
                }}
              />
              {unresolvable.length > 0 && (
                <div className="px-3 py-1.5 text-[10px] text-amber-700 border-t border-black/[0.06]">
                  {unresolvable.length} unresolvable (no phone / group) — excluded automatically.
                </div>
              )}
            </div>
          )}

          {/* Template Selection */}
          <div className="space-y-4 border-t border-black/[0.06] pt-5">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Message Template</label>
                <select
                  value={selectedTemplateId}
                  onChange={(e) => handleTemplateChange(e.target.value)}
                  className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
                >
                  <option value="">Select Template...</option>
                  {templates.map(t => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </div>

              {selectedTemplate && (
                <div className="space-y-2">
                  <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Media Attachment (Cloudinary URL)</label>
                  <input
                    type="url"
                    value={mediaUrl}
                    onChange={(e) => setMediaUrl(e.target.value)}
                    placeholder="https://res.cloudinary.com/..."
                    className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2 focus:outline-none focus:ring-1 focus:ring-black"
                  />
                </div>
              )}
            </div>

            {/* Variable Form Placeholders */}
            {selectedTemplate && Object.keys(variables).length > 0 && (
              <div className="space-y-3 bg-[#f8f8f6] p-4 rounded-sm border border-black/10">
                <h4 className="text-xs font-semibold uppercase tracking-wider text-black/60 border-b border-black/10 pb-2">Inject Custom Variables</h4>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {Object.keys(variables).map(vKey => (
                    <div key={vKey} className="space-y-1">
                      <label className="text-[11px] font-semibold text-black/50">{vKey}</label>
                      <input
                        type="text"
                        value={variables[vKey]}
                        onChange={(e) => setVariables({ ...variables, [vKey]: e.target.value })}
                        className="w-full text-xs bg-white border border-black/10 rounded-sm p-2 focus:outline-none focus:ring-1 focus:ring-black"
                        placeholder={`Value for {{${vKey}}}`}
                        required
                      />
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

        </div>

        {/* Live Draft Preview */}
        <div className="bg-[#1e1e1e] text-white p-6 rounded-md shadow-lg flex flex-col justify-between min-h-[350px]">
          <div className="space-y-4">
            <h3 className="text-xs font-bold uppercase tracking-widest text-white/40 border-b border-white/10 pb-3">Template Preview</h3>
            {selectedTemplate ? (
              <div className="space-y-3">
                {mediaUrl && (
                  <div className="border border-white/10 rounded-sm p-2 bg-white/5 flex items-center gap-2">
                    <Database className="w-4 h-4 text-emerald-400 shrink-0" />
                    <span className="text-[11px] font-mono text-white/60 truncate">{mediaUrl}</span>
                  </div>
                )}
                <div className="bg-[#2a2a2a] p-4 rounded-sm font-mono text-xs text-white/90 whitespace-pre-wrap leading-relaxed">
                  {/* Local template rendering helper */}
                  {selectedTemplate.body_text
                    .replace("{{talent_name}}", "Ayushi Thakur")
                    .replace(/\{\{(\w+)\}\}/g, (match, p1) => variables[p1] || `[${p1}]`)
                  }
                </div>
              </div>
            ) : (
              <p className="text-xs text-white/30 italic">Select template to inspect formatting.</p>
            )}
          </div>

          <div className="pt-6 border-t border-white/10 flex flex-col gap-2">
            <button
              onClick={handleDryRun}
              disabled={launching || !selectedProjectId || !selectedTemplateId || recipients.length === 0}
              className="text-xs font-bold uppercase tracking-wider bg-white text-black py-3 rounded-sm text-center hover:bg-white/90 disabled:opacity-50 transition-colors"
            >
              Compile Dry Run Preview
            </button>
            
            {dryRunResult && (
              <button
                onClick={handleLaunch}
                disabled={launching}
                className="text-xs font-bold uppercase tracking-wider bg-emerald-500 hover:bg-emerald-600 text-white py-3 rounded-sm text-center transition-colors"
              >
                {launching ? "Launching..." : `Launch Batch Broadcast (${recipients.length})`}
              </button>
            )}
          </div>
        </div>

      </div>

      {/* Compile Dry Run Preview Table */}
      {dryRunResult && (
        <div className="bg-white p-6 rounded-md border border-black/[0.06] space-y-4">
          <div className="flex justify-between items-center border-b border-black/[0.06] pb-3">
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Dry Run Auditing</h3>
              <p className="text-xs text-black/40 mt-0.5">Below is the exact output rendering showing simulated delivery destinations.</p>
            </div>
            <div className="flex gap-2">
              <span className="text-xs bg-amber-50 text-amber-700 font-semibold px-2 py-1 border border-amber-100 rounded-sm">Dry Run Only</span>
              <span className="text-xs bg-black text-white font-semibold px-2 py-1 rounded-sm">Total Jobs: {dryRunResult.batch?.total_jobs || 0}</span>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-black/10 text-black/50 font-medium">
                  <th className="py-2.5 font-semibold">Talent Name</th>
                  <th className="py-2.5 font-semibold">Channel Type</th>
                  <th className="py-2.5 font-semibold">Destination Address</th>
                  <th className="py-2.5 font-semibold">Generated Message Body</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-black/[0.04]">
                {dryRunResult.jobs?.map((job) => (
                  <tr key={job.id} className="hover:bg-[#f8f8f6]">
                    <td className="py-2.5 font-medium">{job.talent_name}</td>
                    <td className="py-2.5 capitalize">{job.destination_type}</td>
                    <td className="py-2.5 font-mono text-[11px] text-black/60">{job.destination}</td>
                    <td className="py-2.5 max-w-md truncate font-mono text-[11px] text-black/50">{job.message_body}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ==========================================
// 3. CAMPAIGN HISTORY (Batches & Jobs)
// ==========================================
function WEHistoryPanel() {
  const [batches, setBatches] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedBatchId, setSelectedBatchId] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [loadingJobs, setLoadingJobs] = useState(false);

  const fetchHistory = async () => {
    try {
      const list = await getBatches();
      setBatches(list);
    } catch (err) {
      toast.error("Failed to load campaign history");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHistory();
  }, []);

  const handleBatchClick = async (batchId) => {
    setSelectedBatchId(batchId);
    setLoadingJobs(true);
    try {
      const jobList = await getJobs(batchId);
      setJobs(jobList);
    } catch (err) {
      toast.error("Failed to load delivery tracker details");
    } finally {
      setLoadingJobs(false);
    }
  };

  const handleAction = async (batchId, action) => {
    try {
      await runBatchAction(batchId, action);
      toast.success(`Batch successfully ${action}ed`);
      fetchHistory();
      if (selectedBatchId === batchId) {
        handleBatchClick(batchId);
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Action execution failed");
    }
  };

  const handleRetryJob = async (job) => {
    try {
      await retryJob(job.batch_id, job.id);
      toast.success("Job successfully queued for retry");
      handleBatchClick(job.batch_id);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to retry job");
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center p-12 bg-white rounded-md border border-black/[0.06]">
        <RefreshCw className="w-6 h-6 animate-spin text-black/40" />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
      
      {/* Batches Table */}
      <div className="xl:col-span-1 bg-white p-6 rounded-md border border-black/[0.06] space-y-4">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60 border-b border-black/[0.06] pb-3">Execution Batches</h3>
        
        <div className="space-y-3 max-h-[600px] overflow-y-auto pr-1">
          {batches.map((batch) => {
            const active = selectedBatchId === batch.id;
            // Distinct buckets — UNCONFIRMED is NEVER counted as verified.
            const verified = batch.sent_count || 0;
            const unconfirmed = batch.unconfirmed_count || 0;
            const failed = batch.failed_count || 0;
            const progress = batch.total_jobs > 0
              ? Math.round(((verified + unconfirmed + failed) / batch.total_jobs) * 100)
              : 0;

            return (
              <div
                key={batch.id}
                onClick={() => handleBatchClick(batch.id)}
                className={`p-4 rounded-sm border transition-all cursor-pointer space-y-3 ${
                  active 
                    ? "bg-[#f8f8f6] border-black" 
                    : "bg-white border-black/10 hover:border-black/20"
                }`}
              >
                <div className="flex justify-between items-start gap-2">
                  <div>
                    <div className="flex items-center gap-1.5">
                      <h4 className="font-semibold text-sm truncate max-w-[130px]">{batch.source_label || batch.project_name || "Untitled Broadcast"}</h4>
                      {(() => {
                        const st = batch.source_type || (batch.project_id ? "PROJECT" : "");
                        if (!st) return null;
                        const meta = st === "CRM"
                          ? { label: "CRM", cls: "bg-purple-50 text-purple-700" }
                          : st === "MANUAL"
                            ? { label: "Manual", cls: "bg-teal-50 text-teal-700" }
                            : { label: "Project", cls: "bg-blue-50 text-blue-700" };
                        return <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded-sm ${meta.cls}`}>{meta.label}</span>;
                      })()}
                    </div>
                    <p className="text-[10px] text-black/40 font-mono mt-0.5">{batch.template_slug}</p>
                  </div>
                  <span className={`text-[10px] uppercase tracking-wider px-2 py-0.5 font-bold rounded-sm ${
                    batch.status === "completed" ? "bg-emerald-50 text-emerald-700 border border-emerald-100" :
                    batch.status === "running" ? "bg-sky-50 text-sky-700 border border-sky-100 animate-pulse" :
                    batch.status === "paused" ? "bg-amber-50 text-amber-700 border border-amber-100" :
                    batch.status === "dry_run_complete" ? "bg-purple-50 text-purple-700 border border-purple-100" :
                    "bg-red-50 text-red-700 border border-red-100"
                  }`}>
                    {batch.status.replace("_", " ")}
                  </span>
                </div>

                {/* Progress bar */}
                {batch.status !== "dry_run_complete" && (
                  <div className="space-y-1">
                    <div className="flex justify-between text-[10px] text-black/40 font-medium">
                      <span>Progress</span>
                      <span>{progress}% — {verified} verified · {unconfirmed} unconfirmed · {failed} failed</span>
                    </div>
                    <div className="w-full bg-black/[0.06] h-1.5 rounded-full overflow-hidden">
                      <div className="bg-black h-full transition-all duration-300" style={{ width: `${progress}%` }} />
                    </div>
                  </div>
                )}

                {/* Date & Trigger Actions */}
                <div className="flex justify-between items-center text-[10px] text-black/40 pt-2 border-t border-black/[0.04]">
                  <span>{new Date(batch.created_at).toLocaleString()}</span>
                  
                  {batch.status === "running" && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleAction(batch.id, "pause"); }}
                      className="flex items-center gap-1 text-[10px] font-bold text-amber-700 uppercase hover:underline"
                    >
                      <Pause className="w-3 h-3" /> Pause
                    </button>
                  )}
                  {batch.status === "paused" && (
                    <div className="flex gap-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleAction(batch.id, "resume"); }}
                        className="flex items-center gap-1 text-[10px] font-bold text-sky-700 uppercase hover:underline"
                      >
                        <Play className="w-3 h-3" /> Resume
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleAction(batch.id, "cancel"); }}
                        className="flex items-center gap-1 text-[10px] font-bold text-red-700 uppercase hover:underline"
                      >
                        <Square className="w-3 h-3" /> Cancel
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          {batches.length === 0 && (
            <p className="text-xs text-black/40 p-4 text-center">No broadcast campaigns launched yet.</p>
          )}
        </div>
      </div>

      {/* Jobs Delivery Tracker */}
      <div className="xl:col-span-2 bg-white p-6 rounded-md border border-black/[0.06] space-y-4">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60 border-b border-black/[0.06] pb-3">Delivery Tracker</h3>
        
        {selectedBatchId ? (
          loadingJobs ? (
            <div className="flex justify-center p-12">
              <RefreshCw className="w-6 h-6 animate-spin text-black/40" />
            </div>
          ) : (
            <div className="space-y-4">
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-black/10 text-black/50 font-medium">
                      <th className="py-2.5 font-semibold">Talent</th>
                      <th className="py-2.5 font-semibold">Destination Address</th>
                      <th className="py-2.5 font-semibold">Status</th>
                      <th className="py-2.5 font-semibold">Attempts</th>
                      <th className="py-2.5 font-semibold">Log Error</th>
                      <th className="py-2.5 font-semibold text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-black/[0.04]">
                    {jobs.map((job) => (
                      <tr key={job.id} className="hover:bg-[#f8f8f6]">
                        <td className="py-2.5 font-medium">{job.talent_name}</td>
                        <td className="py-2.5 font-mono text-[11px] text-black/60">{job.destination}</td>
                        <td className="py-2.5">
                          <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded-sm ${
                            job.status === "sent" ? "bg-emerald-50 text-emerald-700" :
                            job.status === "sent_unverified" ? "bg-orange-50 text-orange-700" :
                            job.status === "sending" ? "bg-sky-50 text-sky-700 animate-pulse" :
                            job.status === "pending" ? "bg-amber-50 text-amber-700" :
                            job.status === "dry_run_preview" ? "bg-purple-50 text-purple-700" :
                            "bg-red-50 text-red-700"
                          }`}>
                            {job.status === "sent" ? "Verified" :
                             job.status === "sent_unverified" ? "Delivery Unconfirmed" :
                             job.status === "failed" ? "Failed" :
                             job.status}
                          </span>
                        </td>
                        <td className="py-2.5 font-mono">{job.attempt_count}</td>
                        <td className="py-2.5 max-w-xs truncate text-[11px] text-red-600 font-mono" title={job.error_message}>
                          {job.error_message || "—"}
                        </td>
                        <td className="py-2.5 text-right">
                          {(job.status === "failed" || job.status === "skipped" || job.status === "sent_unverified") && (
                            <button
                              onClick={() => handleRetryJob(job)}
                              className="text-[10px] font-bold text-black uppercase tracking-wider hover:underline border border-black/10 px-2 py-1 rounded-sm bg-white"
                            >
                              Retry
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {jobs.length === 0 && (
                      <tr>
                        <td colSpan="6" className="text-center py-6 text-black/40">No individual message logs found.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )
        ) : (
          <div className="flex flex-col items-center justify-center p-12 text-black/30 border border-dashed border-black/10 rounded-sm">
            <History className="w-8 h-8 opacity-40 mb-2" />
            <p className="text-xs">Select a campaign batch to view delivery breakdowns.</p>
          </div>
        )}
      </div>

    </div>
  );
}

// ==========================================
// 4. TEMPLATE MANAGER (CRUD)
// ==========================================
function WETemplateManager() {
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState(null); // 'new' or templateId

  const [formData, setFormData] = useState({
    name: "",
    slug: "",
    body_text: "",
    variables: [],
    media_type: "none",
    media_url: "",
    is_custom: true
  });

  const fetchTemplates = async () => {
    try {
      const data = await getTemplates();
      setTemplates(data);
    } catch (err) {
      toast.error("Failed to load templates");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTemplates();
  }, []);

  const handleEditClick = (t) => {
    setEditingId(t.id);
    setFormData({
      name: t.name,
      slug: t.slug,
      body_text: t.body_text,
      variables: t.variables || [],
      media_type: t.media_type || "none",
      media_url: t.media_url || "",
      is_custom: t.is_custom !== false
    });
  };

  const handleCreateNewClick = () => {
    setEditingId("new");
    setFormData({
      name: "",
      slug: "",
      body_text: "",
      variables: [],
      media_type: "none",
      media_url: "",
      is_custom: true
    });
  };

  const extractVariables = (text) => {
    const regex = /\{\{(\w+)\}\}/g;
    const matches = [];
    let match;
    while ((match = regex.exec(text)) !== null) {
      if (!matches.includes(match[1])) {
        matches.push(match[1]);
      }
    }
    return matches;
  };

  const handleBodyTextChange = (text) => {
    const vars = extractVariables(text);
    setFormData({ ...formData, body_text: text, variables: vars });
  };

  const handleSave = async (e) => {
    e.preventDefault();
    try {
      if (editingId === "new") {
        await createTemplate(formData);
        toast.success("Template created successfully");
      } else {
        await updateTemplate(editingId, formData);
        toast.success("Template updated successfully");
      }
      setEditingId(null);
      fetchTemplates();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Template save failed");
    }
  };

  const handleDelete = async (id) => {
    const confirm = window.confirm("Are you sure you want to delete this template?");
    if (!confirm) return;

    try {
      await deleteTemplate(id);
      toast.success("Template deleted successfully");
      fetchTemplates();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to delete template");
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center p-12 bg-white rounded-md border border-black/[0.06]">
        <RefreshCw className="w-6 h-6 animate-spin text-black/40" />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
      
      {/* List */}
      <div className="xl:col-span-1 bg-white p-6 rounded-md border border-black/[0.06] space-y-4">
        <div className="flex justify-between items-center border-b border-black/[0.06] pb-3">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Saved Templates</h3>
          <button
            onClick={handleCreateNewClick}
            className="flex items-center gap-1.5 text-xs font-bold bg-black text-white px-2.5 py-1.5 rounded-sm hover:bg-black/90 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" /> Create
          </button>
        </div>

        <div className="space-y-2 max-h-[600px] overflow-y-auto pr-1">
          {templates.map(t => (
            <div
              key={t.id}
              onClick={() => handleEditClick(t)}
              className={`p-4 rounded-sm border cursor-pointer transition-all space-y-2 ${
                editingId === t.id 
                  ? "bg-[#f8f8f6] border-black" 
                  : "bg-white border-black/10 hover:border-black/20"
              }`}
            >
              <div className="flex justify-between items-start gap-2">
                <h4 className="font-semibold text-sm">{t.name}</h4>
                {!t.is_custom && (
                  <span className="text-[9px] font-bold uppercase tracking-wider bg-black/[0.04] px-1.5 py-0.5 rounded-sm text-black/50 border border-black/[0.06]">System</span>
                )}
              </div>
              <p className="text-xs text-black/50 font-mono truncate">{t.slug}</p>
              
              <div className="flex flex-wrap gap-1 mt-2">
                {t.variables?.map(v => (
                  <span key={v} className="text-[9px] font-mono bg-black/[0.04] px-1.5 py-0.5 rounded-sm text-black/60 border border-black/[0.02]">
                    {v}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Editor Panel */}
      <div className="xl:col-span-2 bg-white p-6 rounded-md border border-black/[0.06]">
        {editingId ? (
          <form onSubmit={handleSave} className="space-y-4">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60 border-b border-black/[0.06] pb-3">
              {editingId === "new" ? "New Template Layout" : "Edit Template Layout"}
            </h3>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Template Label</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="e.g. Casting Callback"
                  className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
                  required
                />
              </div>

              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Unique URL Slug Identifier</label>
                <input
                  type="text"
                  value={formData.slug}
                  onChange={(e) => setFormData({ ...formData, slug: e.target.value.toLowerCase().replace(/\s+/g, "_") })}
                  placeholder="e.g. casting_callback"
                  className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
                  required
                  disabled={editingId !== "new" && !formData.is_custom}
                />
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Media Type</label>
                <select
                  value={formData.media_type}
                  onChange={(e) => setFormData({ ...formData, media_type: e.target.value })}
                  className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
                >
                  <option value="none">None (Text Only)</option>
                  <option value="image">Image/Video Upload</option>
                  <option value="document">Document (PDF)</option>
                </select>
              </div>

              {formData.media_type !== "none" && (
                <div className="space-y-2">
                  <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Cloudinary Static Media Attachment Link</label>
                  <input
                    type="url"
                    value={formData.media_url}
                    onChange={(e) => setFormData({ ...formData, media_url: e.target.value })}
                    placeholder="https://res.cloudinary.com/..."
                    className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
                  />
                </div>
              )}
            </div>

            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wider text-black/60 flex justify-between">
                <span>Message Body Structure</span>
                <span className="text-[10px] text-black/40 font-mono normal-case">Wrap placeholders in double curly braces: {"{{var}}"}</span>
              </label>
              <textarea
                value={formData.body_text}
                onChange={(e) => handleBodyTextChange(e.target.value)}
                rows="8"
                placeholder="Hi {{talent_name}}... We are locked for {{project_name}}."
                className="w-full text-xs font-mono bg-[#f8f8f6] border border-black/10 rounded-sm p-3 focus:outline-none focus:ring-1 focus:ring-black leading-relaxed"
                required
              />
            </div>

            {formData.variables.length > 0 && (
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Extracted Dynamic Variables</label>
                <div className="flex flex-wrap gap-1.5">
                  {formData.variables.map(v => (
                    <span key={v} className="text-xs font-mono bg-[#f8f8f6] px-2 py-1 rounded-sm border border-black/10 text-black/70">
                      {v}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="flex justify-between items-center pt-4 border-t border-black/[0.06]">
              {editingId !== "new" && formData.is_custom ? (
                <button
                  type="button"
                  onClick={() => handleDelete(editingId)}
                  className="flex items-center gap-1 text-xs font-bold text-red-600 uppercase hover:underline"
                >
                  <Trash2 className="w-4 h-4" /> Delete Template
                </button>
              ) : <div />}

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setEditingId(null)}
                  className="text-xs font-bold uppercase tracking-wider text-black/60 hover:text-black px-4 py-2 border border-black/10 rounded-sm bg-white"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider bg-black text-white px-4 py-2 rounded-sm hover:bg-black/90 transition-colors"
                >
                  <Save className="w-4 h-4" /> Save Template
                </button>
              </div>
            </div>

          </form>
        ) : (
          <div className="flex flex-col items-center justify-center p-12 text-black/30 border border-dashed border-black/10 rounded-sm min-h-[300px]">
            <Edit className="w-8 h-8 opacity-40 mb-2" />
            <p className="text-xs">Create or select a template to start designing message workflows.</p>
          </div>
        )}
      </div>

    </div>
  );
}

// ==========================================
// 5. CONFIGURATION PANEL (Admin only)
// ==========================================
function WEConfigPanel() {
  const [config, setConfig] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const fetchConfig = async () => {
    try {
      const data = await getWaConfig();
      setConfig(data);
    } catch (err) {
      toast.error("Failed to load WhatsApp safety configurations");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchConfig();
  }, []);

  const handleChange = (key, value) => {
    setConfig({ ...config, [key]: value });
  };

  const handleSave = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await Promise.all(
        Object.entries(config).map(([key, val]) => updateWaConfig(key, val))
      );
      toast.success("Configurations updated successfully");
      fetchConfig();
    } catch (err) {
      toast.error("Failed saving safety variables");
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center p-12 bg-white rounded-md border border-black/[0.06]">
        <RefreshCw className="w-6 h-6 animate-spin text-black/40" />
      </div>
    );
  }

  return (
    <div className="max-w-2xl bg-white p-6 rounded-md border border-black/[0.06] space-y-6">
      <div className="border-b border-black/[0.06] pb-3">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Safety Controls & Anti-Ban Config</h3>
        <p className="text-xs text-black/40 mt-1">Adjust delivery parameters. Slower execution reduces risk of meta banning. ONLY system admins can override these.</p>
      </div>

      <form onSubmit={handleSave} className="space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Minimum Delay (Seconds)</label>
            <input
              type="number"
              value={config.min_delay_sec || 8}
              onChange={(e) => handleChange("min_delay_sec", e.target.value)}
              className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
              min="2"
              required
            />
          </div>

          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Maximum Delay (Seconds)</label>
            <input
              type="number"
              value={config.max_delay_sec || 15}
              onChange={(e) => handleChange("max_delay_sec", e.target.value)}
              className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
              min="3"
              required
            />
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Retry Attempts</label>
            <input
              type="number"
              value={config.max_retries || 3}
              onChange={(e) => handleChange("max_retries", e.target.value)}
              className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
              min="0"
              required
            />
          </div>

          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-black/60">Circuit Breaker Threshold</label>
            <input
              type="number"
              value={config.circuit_breaker_threshold || 5}
              onChange={(e) => handleChange("circuit_breaker_threshold", e.target.value)}
              className="w-full text-sm bg-[#f8f8f6] border border-black/10 rounded-sm p-2.5 focus:outline-none focus:ring-1 focus:ring-black"
              min="1"
              required
            />
            <p className="text-[10px] text-black/40">Number of consecutive failures before auto-pausing.</p>
          </div>
        </div>

        <div className="pt-4 border-t border-black/[0.06] flex justify-end">
          <button
            type="submit"
            disabled={saving}
            className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider bg-black text-white px-5 py-2.5 rounded-sm hover:bg-black/90 transition-colors disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Configurations"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ==========================================
// 6. AUDIT LOG PANEL
// ==========================================
function WEAuditLogPanel() {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit] = useState(100);

  const fetchLogs = async () => {
    try {
      const data = await getAuditLog({ limit });
      setLogs(data);
    } catch (err) {
      toast.error("Failed to load audit trail logs");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, [limit]);

  if (loading) {
    return (
      <div className="flex justify-center p-12 bg-white rounded-md border border-black/[0.06]">
        <RefreshCw className="w-6 h-6 animate-spin text-black/40" />
      </div>
    );
  }

  return (
    <div className="bg-white p-6 rounded-md border border-black/[0.06] space-y-4">
      <div className="flex justify-between items-center border-b border-black/[0.06] pb-3">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Immutable Audit Logs</h3>
          <p className="text-xs text-black/40 mt-0.5">Continuous delivery auditing of automation flows.</p>
        </div>
        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          className="text-xs bg-[#f8f8f6] border border-black/10 rounded-sm p-1.5 focus:outline-none"
        >
          <option value="50">Show 50</option>
          <option value="100">Show 100</option>
          <option value="200">Show 200</option>
        </select>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse text-xs">
          <thead>
            <tr className="border-b border-black/10 text-black/50 font-medium">
              <th className="py-2.5 font-semibold">Timestamp</th>
              <th className="py-2.5 font-semibold">Event Code</th>
              <th className="py-2.5 font-semibold">Destination Address</th>
              <th className="py-2.5 font-semibold">Actor / Node</th>
              <th className="py-2.5 font-semibold">Render Preview</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-black/[0.04] font-mono text-[11px] text-black/80">
            {logs.map((log) => (
              <tr key={log.id} className="hover:bg-[#f8f8f6]">
                <td className="py-2.5 text-black/50">{new Date(log.timestamp).toLocaleString()}</td>
                <td className="py-2.5">
                  <span className={`px-1.5 py-0.5 rounded-sm font-bold uppercase ${
                    log.event_type.includes("failed") ? "bg-red-50 text-red-700" :
                    log.event_type.includes("sent") ? "bg-emerald-50 text-emerald-700" :
                    "bg-black/[0.04] text-black/60"
                  }`}>
                    {log.event_type}
                  </span>
                </td>
                <td className="py-2.5 text-black/60">{log.destination || "—"}</td>
                <td className="py-2.5 text-black/50">{log.actor}</td>
                <td className="py-2.5 max-w-sm truncate text-black/50" title={log.message_preview}>
                  {log.message_preview || "—"}
                </td>
              </tr>
            ))}
            {logs.length === 0 && (
              <tr>
                <td colSpan="5" className="text-center py-6 text-black/40 font-sans">No audit events logged yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
