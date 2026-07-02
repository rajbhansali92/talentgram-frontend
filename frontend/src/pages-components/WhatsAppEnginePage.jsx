import React, { useState, useEffect, useRef } from "react";
import { 
  Play, Pause, Square, QrCode, RefreshCw, AlertTriangle, CheckCircle, 
  Settings, Clock, ShieldAlert, History, Edit, Send, Save, Plus, Trash2, Database, AlertCircle,
  ChevronLeft, ChevronRight, Laptop, Smartphone, Search, Filter, Info, User
} from "lucide-react";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";

import {
  getTemplates, createTemplate, updateTemplate, deleteTemplate,
  getPipelineSummary,
  createBatch, getBatches, runBatchAction,
  getJobs, retryJob,
  getSessionStatus, clearQrCode, resetSession,
  getWaConfig, updateWaConfig,
  getAuditLog,
  resolveTargets, getCrmContactTypes, validateManual,
  getContactLists, createContactList, updateContactList, deleteContactList,
  // TEMP TEST TOOL / REMOVE AFTER WHATSAPP VALIDATION
  testInternalNotification,
} from "@/lib/whatsappApi";
import VirtualList from "@/components/VirtualList";
import ProjectSearchModal from "@/components/ProjectSearchModal";

// ── Template variable catalog (mirrors backend GET /whatsapp/variables) ──────
// Powers the editor's click-to-insert "Available Variables" panel (Part 5) and
// tells the launcher which placeholders are auto-resolved so they're hidden from
// "Inject Custom Variables" (Part 2). Keep in sync with whatsapp.py VARIABLE_CATALOG.
const WA_VARIABLE_CATALOG = [
  { category: "Talent", variables: ["first_name", "full_name", "talent_name", "phone", "instagram"] },
  { category: "Project", variables: ["project_name", "shoot_dates", "budget", "location", "submission_link"] },
  { category: "Sender", variables: ["sender_name", "sender_email"] },
  { category: "System", variables: ["current_date", "current_time"] },
];
// Resolved automatically regardless of source (recipient name/phone, sender, date).
const WA_AUTO_ALWAYS = new Set([
  "talent_name", "full_name", "first_name", "phone",
  "sender_name", "sender_email", "current_date", "current_time",
]);
// Resolved automatically only when the source is Project Pipeline.
const WA_AUTO_PROJECT = new Set(["project_name", "shoot_dates", "budget", "submission_link"]);

// True when a placeholder is auto-resolved for the given source (so the launcher
// should NOT prompt the admin to type it).
function waIsAutoResolved(key, sourceType) {
  if (WA_AUTO_ALWAYS.has(key)) return true;
  if (sourceType === "PROJECT" && WA_AUTO_PROJECT.has(key)) return true;
  return false;
}

export default function WhatsAppEnginePage() {
  const [activeTab, setActiveTab] = useState("campaigns"); // campaigns | templates | analytics | settings
  const [campaignSubTab, setCampaignSubTab] = useState("launch"); // launch | history
  const [settingsSubTab, setSettingsSubTab] = useState("status"); // status | safety

  return (
    <div className="min-h-screen bg-[#F8F8F7] px-8 py-12 text-[#111111] font-sans antialiased">
      <div className="max-w-7xl mx-auto space-y-12">
        
        {/* Header */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight font-display text-[#111111]">WhatsApp Engine</h1>
            <p className="text-[#6B7280] text-sm mt-1">
              Broadcast template updates, status notifications, and media attachments directly via automated browser execution.
            </p>
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="flex flex-wrap gap-2 border-b border-black/[0.04] pb-px">
          {[
            { id: "campaigns", label: "Campaigns", icon: Send },
            { id: "contact-lists", label: "Contact Lists", icon: Database },
            { id: "templates", label: "Templates", icon: Edit },
            { id: "analytics", label: "Analytics", icon: ShieldAlert },
            { id: "settings", label: "Settings", icon: Settings },
          ].map((tab) => {
            const Icon = tab.icon;
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-5 py-3 text-xs font-semibold uppercase tracking-widest border-b-2 transition-all duration-150 ${
                  active 
                    ? "border-[#111111] text-[#111111]" 
                    : "border-transparent text-[#6B7280] hover:text-[#111111] hover:border-[#111111]/20"
                }`}
              >
                <Icon className="w-4 h-4" />
                {tab.label}
              </button>
            );
          })}
        </div>

        {/* Tab Contents */}
        <div className="mt-8 transition-transform duration-200">
          {activeTab === "campaigns" && (
            <div className="space-y-8">
              {/* Campaigns Sub-navigation */}
              <div className="flex bg-[#111111]/[0.03] p-0.5 rounded-full w-fit">
                <button
                  onClick={() => setCampaignSubTab("launch")}
                  className={`px-4 py-1.5 rounded-full text-xs font-semibold uppercase tracking-wider transition-all duration-150 ${
                    campaignSubTab === "launch" ? "bg-white text-black shadow-sm" : "text-[#6B7280] hover:text-[#111111]"
                  }`}
                >
                  Launch Campaign
                </button>
                <button
                  onClick={() => setCampaignSubTab("history")}
                  className={`px-4 py-1.5 rounded-full text-xs font-semibold uppercase tracking-wider transition-all duration-150 ${
                    campaignSubTab === "history" ? "bg-white text-black shadow-sm" : "text-[#6B7280] hover:text-[#111111]"
                  }`}
                >
                  Campaign History
                </button>
              </div>

              {campaignSubTab === "launch" ? <WECampaignLauncher /> : <WEHistoryPanel />}
            </div>
          )}

          {activeTab === "contact-lists" && <WEContactListManager />}
          {activeTab === "templates" && <WETemplateManager />}
          {activeTab === "analytics" && <WEAuditLogPanel />}

          {activeTab === "settings" && (
            <div className="space-y-8">
              {/* Settings Sub-navigation */}
              <div className="flex bg-[#111111]/[0.03] p-0.5 rounded-full w-fit">
                <button
                  onClick={() => setSettingsSubTab("status")}
                  className={`px-4 py-1.5 rounded-full text-xs font-semibold uppercase tracking-wider transition-all duration-150 ${
                    settingsSubTab === "status" ? "bg-white text-black shadow-sm" : "text-[#6B7280] hover:text-[#111111]"
                  }`}
                >
                  Session Status
                </button>
                <button
                  onClick={() => setSettingsSubTab("safety")}
                  className={`px-4 py-1.5 rounded-full text-xs font-semibold uppercase tracking-wider transition-all duration-150 ${
                    settingsSubTab === "safety" ? "bg-white text-black shadow-sm" : "text-[#6B7280] hover:text-[#111111]"
                  }`}
                >
                  Safety Configuration
                </button>
              </div>

              {settingsSubTab === "status" ? <WESessionPanel /> : <WEConfigPanel />}
            </div>
          )}
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
      <div className="flex flex-col items-center justify-center p-8 bg-white rounded-2xl min-h-[300px] shadow-sm">
        <RefreshCw className="w-8 h-8 animate-spin text-[#111111]/30 mb-3" />
        <p className="text-[#6B7280] text-sm font-medium">Querying WhatsApp Web session state...</p>
      </div>
    );
  }

  const status = session?.status || "disconnected";
  const qrBase64 = session?.qr_code_base64;
  const errMsg = session?.error_message;
  const heartbeat = session?.last_heartbeat;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
      
      {/* Status Summary */}
      <div className="bg-white p-8 rounded-2xl space-y-6 shadow-sm">
        <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Connection Status</h3>
        
        <div className="flex items-center gap-3">
          <div className={`w-3.5 h-3.5 rounded-full ${
            status === "authenticated" ? "bg-emerald-500 animate-pulse" :
            status === "qr_pending" ? "bg-amber-500 animate-pulse" : "bg-red-500"
          }`} />
          <span className="text-lg font-semibold capitalize text-[#111111]">{status.replace("_", " ")}</span>
        </div>

        <div className="pt-4 space-y-3 text-xs border-t border-black/[0.04]">
          <div className="flex justify-between">
            <span className="text-[#6B7280] font-medium">Session Instance</span>
            <span className="font-mono text-[#111111]">default</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[#6B7280] font-medium">Last Heartbeat</span>
            <span className="text-[#111111] font-mono">
              {heartbeat ? new Date(heartbeat).toLocaleTimeString() : "Never"}
            </span>
          </div>
          {session?.authenticated_at && (
            <div className="flex justify-between">
              <span className="text-[#6B7280] font-medium">Authenticated At</span>
              <span className="text-[#111111]">
                {new Date(session.authenticated_at).toLocaleString()}
              </span>
            </div>
          )}
        </div>

        {errMsg && (
          <div className="p-4 bg-red-500/10 rounded-xl text-red-700 text-xs flex gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5 text-red-600" />
            <div>
              <p className="font-semibold">Session Error</p>
              <p className="mt-0.5 opacity-90 leading-relaxed">{errMsg}</p>
            </div>
          </div>
        )}

        <div className="pt-4 space-y-3">
          <button
            onClick={() => setPolling(!polling)}
            className="text-xs font-semibold uppercase tracking-widest text-[#111111] border border-black/10 hover:border-black px-4 py-3 rounded-lg w-full text-center transition-colors h-[48px] active:scale-[0.98] duration-120"
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
                toast.error(formatErrorDetail(err, "Failed to reset session."));
              }
            }}
            className="text-xs font-semibold uppercase tracking-widest text-red-600 hover:text-white hover:bg-red-600 border border-red-200/50 px-4 py-3 rounded-lg w-full text-center transition-colors h-[48px] active:scale-[0.98] duration-120"
          >
            Reset WhatsApp Session
          </button>
        </div>
      </div>

      {/* QR Code Container */}
      <div className="lg:col-span-2 bg-white p-8 rounded-2xl flex flex-col items-center justify-center min-h-[300px] shadow-sm">
        {status === "authenticated" ? (
          <div className="text-center space-y-4 p-6 max-w-sm">
            <div className="w-16 h-16 bg-[#0D8A5F]/10 rounded-full flex items-center justify-center mx-auto border border-[#0D8A5F]/20">
              <CheckCircle className="w-8 h-8 text-[#0D8A5F]" />
            </div>
            <h3 className="text-lg font-semibold text-[#111111]">Active Session</h3>
            <p className="text-xs text-[#6B7280] leading-relaxed">
              The worker process is fully authenticated to WhatsApp Web and listening for jobs. You can safely launch message broadcasts.
            </p>
          </div>
        ) : status === "qr_pending" && qrBase64 ? (
          <div className="text-center space-y-5 max-w-md">
            <h3 className="text-lg font-semibold text-[#111111]">Link WhatsApp Account</h3>
            <p className="text-xs text-[#6B7280]">
              Scan the QR code below using the linked devices option in your WhatsApp mobile application.
            </p>
            <div className="p-4 bg-white border border-black/[0.04] rounded-2xl inline-block shadow-inner mx-auto">
              <img src={qrBase64} alt="WhatsApp QR Code" className="w-56 h-56 mx-auto" />
            </div>
            <p className="text-[10px] text-[#6B7280] italic">
              QR code updates automatically every 90 seconds.
            </p>
          </div>
        ) : (
          <div className="text-center space-y-4 p-6 max-w-sm">
            <div className="w-16 h-16 bg-amber-500/10 rounded-full flex items-center justify-center mx-auto border border-amber-500/20">
              <AlertTriangle className="w-8 h-8 text-amber-600" />
            </div>
            <h3 className="text-lg font-semibold text-[#111111]">Browser Launch Pending</h3>
            <p className="text-xs text-[#6B7280] leading-relaxed">
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

  const [sourceType, setSourceType] = useState("PROJECT");
  const [crmTypes, setCrmTypes] = useState([]);
  const [crmContactType, setCrmContactType] = useState("");
  const [manualText, setManualText] = useState("");
  const [excludedIds, setExcludedIds] = useState(() => new Set());
  const [selectedRowIds, setSelectedRowIds] = useState(() => new Set());
  const [targetSearch, setTargetSearch] = useState("");
  const [projectModalOpen, setProjectModalOpen] = useState(false);
  const [selectedProjectName, setSelectedProjectName] = useState("");

  // Saved Lists targeting states
  const [contactLists, setContactLists] = useState([]);
  const [selectedListIds, setSelectedListIds] = useState([]);

  // Swipe preview states
  const [previewTargetIndex, setPreviewTargetIndex] = useState(0);

  useEffect(() => {
    const loadInitialData = async () => {
      try {
        const [tempList, types, lists] = await Promise.all([
          getTemplates(),
          getCrmContactTypes().catch(() => []),
          getContactLists().catch(() => []),
        ]);
        setTemplates(tempList);
        setCrmTypes(types);
        setContactLists(lists || []);
      } catch (err) {
        console.error("Failed to load campaign selector options", err);
      }
    };
    loadInitialData();
  }, []);

  const buildSourceParams = () => {
    if (sourceType === "PROJECT") {
      return { project_id: selectedProjectId, pipeline_stages: selectedStages };
    }
    if (sourceType === "CRM") {
      return { contact_type: crmContactType || null, select_all_filtered: true };
    }
    if (sourceType === "SAVED_LISTS") {
      return { contact_list_ids: selectedListIds };
    }
    const contacts = manualText.split("\n").map((line) => {
      const [name, phone] = line.split(",");
      return { name: (name || "").trim(), phone: (phone || "").trim() };
    }).filter((c) => c.phone);
    return { contacts };
  };

  const resetResolution = () => {
    setRecipients([]); setUnresolvable([]); setDryRunResult(null);
    setExcludedIds(new Set()); setSelectedRowIds(new Set()); setTargetSearch("");
    setPreviewTargetIndex(0);
    setSelectedListIds([]);
  };

  // AUTO RESOLVE (P0 Task 1.2)
  // Whenever the targeting criteria change, trigger resolve automatically
  useEffect(() => {
    const autoResolve = async () => {
      // Guard criteria based on source types
      if (sourceType === "PROJECT" && (!selectedProjectId || selectedStages.length === 0)) {
        resetResolution();
        return;
      }
      if (sourceType === "CRM" && !crmContactType) {
        // Option to not auto resolve if CRM parameters are unselected
      }
      if (sourceType === "SAVED_LISTS" && selectedListIds.length === 0) {
        setRecipients([]); setUnresolvable([]); setDryRunResult(null);
        setExcludedIds(new Set()); setSelectedRowIds(new Set());
        setPreviewTargetIndex(0);
        return;
      }
      if (sourceType === "MANUAL" && !manualText.trim()) {
        resetResolution();
        return;
      }

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
        setPreviewTargetIndex(0);
      } catch (err) {
        console.error("Failed auto resolve:", err);
      } finally {
        setResolving(false);
      }
    };

    const delayDebounce = setTimeout(() => {
      autoResolve();
    }, 400); // Debounce to allow quick changes without multiple requests

    return () => clearTimeout(delayDebounce);
  }, [sourceType, selectedProjectId, selectedStages, crmContactType, manualText, selectedListIds]);

  const handleProjectChange = async (projectId) => {
    setSelectedProjectId(projectId);
    setPipelineSummary(null);
    setSelectedStages([]);
    resetResolution();
    
    if (!projectId) return;

    try {
      const summary = await getPipelineSummary(projectId);
      setPipelineSummary(summary);
    } catch (err) {
      toast.error("Failed to fetch project stages summary");
    }
  };

  const handleSaveAsListClick = async () => {
    const listName = prompt("Enter a name for this Contact List:");
    if (!listName) return;
    if (!listName.trim()) {
      toast.error("List name cannot be empty");
      return;
    }
    const contacts = manualText.split("\n").map((line) => {
      const [name, phone] = line.split(",");
      return { name: (name || "").trim(), phone: (phone || "").trim() };
    }).filter((c) => c.phone);

    if (contacts.length === 0) {
      toast.error("No valid contacts found in textarea");
      return;
    }

    try {
      const newList = await createContactList({
        name: listName.trim(),
        description: "Created from manual contacts input",
        contacts
      });
      toast.success(`Saved list "${listName.trim()}" successfully!`);
      setContactLists(prev => [newList, ...prev]);
    } catch (err) {
      toast.error(formatErrorDetail(err, "Failed to save contact list"));
    }
  };

  const handleStageToggle = (stage) => {
    const updated = selectedStages.includes(stage)
      ? selectedStages.filter(s => s !== stage)
      : [...selectedStages, stage];
    setSelectedStages(updated);
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
      toast.error(formatErrorDetail(err, "Failed compilation of dry run"));
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
      toast.error(formatErrorDetail(err, "Failed launching batch campaign"));
    } finally {
      setLaunching(false);
    }
  };

  // Get active target name for preview (Task 8.2)
  const activePreviewRecipient = filteredRecipients[previewTargetIndex] || null;

  return (
    <div className="space-y-8">
      <ProjectSearchModal
        open={projectModalOpen}
        onClose={() => setProjectModalOpen(false)}
        onSelect={(p) => { handleProjectChange(p.id); setSelectedProjectName(p.name || p.brand_name || ""); }}
      />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-start">

        {/* Configurations column */}
        <div className="lg:col-span-2 bg-white p-8 rounded-2xl space-y-8 shadow-sm">
          <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Campaign Target & Template</h3>

          {/* Target source selector */}
          <div className="space-y-3">
            <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Target Source</label>
            <div className="flex flex-wrap gap-2">
              {["PROJECT", "SAVED_LISTS", "CRM", "MANUAL"].map((src) => (
                <button
                  key={src}
                  type="button"
                  onClick={() => { setSourceType(src); resetResolution(); }}
                  className={`text-xs font-semibold px-4 py-2.5 rounded-lg border transition-all duration-150 select-none active:scale-[0.98] ${
                    sourceType === src ? "bg-black text-white border-black" : "bg-[#f8f8f7] border-black/10 hover:border-black/30"
                  }`}
                  data-testid={`source-${src}`}
                >
                  {src === "PROJECT" ? "Project Pipeline" : src === "SAVED_LISTS" ? "Saved Lists" : src === "CRM" ? "Marketing CRM" : "Manual Contacts"}
                </button>
              ))}
            </div>
          </div>

          {/* Saved Lists source */}
          {sourceType === "SAVED_LISTS" && (
            <div className="space-y-4">
              <div className="space-y-1">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Select Saved Lists</label>
                <p className="text-xs text-[#6B7280]">Select one or more contact lists. Recipients will be automatically merged and deduplicated.</p>
              </div>

              <div className="space-y-3">
                <div className="relative">
                  <Search className="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-black/40" />
                  <input
                    type="text"
                    placeholder="Search contact lists..."
                    value={targetSearch}
                    onChange={(e) => setTargetSearch(e.target.value)}
                    className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg pl-10 pr-4 py-3 focus:outline-none focus:ring-1 focus:ring-black h-[50px] transition-all"
                  />
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 max-h-[220px] overflow-y-auto p-1 bg-white border border-black/[0.04] rounded-xl scrollbar-thin">
                  {contactLists
                    .filter(lst => lst.name.toLowerCase().includes(targetSearch.toLowerCase()))
                    .map((lst) => {
                      const selected = selectedListIds.includes(lst.id);
                      return (
                        <div
                          key={lst.id}
                          onClick={() => {
                            const updated = selected
                              ? selectedListIds.filter(id => id !== lst.id)
                              : [...selectedListIds, lst.id];
                            setSelectedListIds(updated);
                          }}
                          className={`flex items-center justify-between p-3.5 rounded-xl border cursor-pointer select-none transition-all duration-150 ${
                            selected
                              ? "bg-black border-black text-white"
                              : "bg-[#f8f8f7] border-black/5 text-[#111111] hover:border-black/20"
                          }`}
                        >
                          <div className="space-y-0.5">
                            <div className="text-xs font-semibold">{lst.name}</div>
                            <div className={`text-[10px] ${selected ? "text-white/70" : "text-[#6B7280]"}`}>
                              {lst.contacts?.length || 0} contacts
                            </div>
                          </div>
                          <div className={`w-4 h-4 rounded border flex items-center justify-center transition-all ${
                            selected ? "bg-white border-white text-black" : "border-black/20 bg-white"
                          }`}>
                            {selected && <div className="w-1.5 h-1.5 bg-black rounded-sm" />}
                          </div>
                        </div>
                      );
                    })}
                  {contactLists.filter(lst => lst.name.toLowerCase().includes(targetSearch.toLowerCase())).length === 0 && (
                    <div className="col-span-full text-center py-6 text-xs text-[#6B7280]">
                      No contact lists match your search.
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* CRM source */}
          {sourceType === "CRM" && (
            <div className="space-y-3">
              <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Contact Type</label>
              <select
                value={crmContactType}
                onChange={(e) => { setCrmContactType(e.target.value); resetResolution(); }}
                className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px] transition-all"
              >
                <option value="">All contact types</option>
                {crmTypes.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
          )}

          {/* Manual source */}
          {sourceType === "MANUAL" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Manual Contacts (Name,+countrycode per line)</label>
                <button
                  type="button"
                  onClick={handleSaveAsListClick}
                  disabled={!manualText.trim()}
                  className="text-[10px] font-bold uppercase tracking-wider text-black hover:underline disabled:opacity-40"
                >
                  Save As Contact List
                </button>
              </div>
              <textarea
                value={manualText}
                onChange={(e) => { setManualText(e.target.value); }}
                rows={5}
                placeholder={"Rahul Sharma,+919876543210\nPriya Jain,+919123456789"}
                className="w-full text-xs font-mono bg-[#f8f8f7] border border-black/10 rounded-lg p-3 focus:outline-none focus:ring-1 focus:ring-black transition-all"
                data-testid="manual-contacts-input"
              />
            </div>
          )}

          {/* Project & Stage selection */}
          {sourceType === "PROJECT" && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-3">
              <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Casting Project</label>
              <button
                type="button"
                onClick={() => setProjectModalOpen(true)}
                className="w-full text-left text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 hover:border-black/30 flex items-center justify-between h-[56px] transition-all"
                data-testid="open-project-search"
              >
                <span className={selectedProjectId ? "text-black" : "text-[#6B7280]"}>
                  {selectedProjectName || "Search project…"}
                </span>
                <span className="text-[10px] uppercase tracking-wider text-black/40">{selectedProjectId ? "Change" : "Search"}</span>
              </button>
            </div>

            {selectedProjectId && pipelineSummary && (
              <div className="space-y-3">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Target Pipeline Stages</label>
                <div className="flex flex-wrap gap-2 p-2.5 bg-[#f8f8f7] border border-black/10 rounded-lg max-h-[140px] overflow-y-auto">
                  {Object.entries(pipelineSummary.stage_counts).map(([stage, count]) => {
                    const active = selectedStages.includes(stage);
                    return (
                      <button
                        key={stage}
                        type="button"
                        onClick={() => handleStageToggle(stage)}
                        className={`text-xs px-3 py-2 rounded-lg border font-medium transition-all duration-120 active:scale-[0.98] ${
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
                    <p className="text-[11px] text-[#6B7280] p-1">No active stages in project.</p>
                  )}
                </div>
              </div>
            )}
          </div>
          )}

          {/* Auto Resolve count indicator (Task 1.2 button replacement) */}
          {((sourceType === "PROJECT" && selectedProjectId && selectedStages.length > 0) ||
            (sourceType === "SAVED_LISTS" && selectedListIds.length > 0) ||
            (sourceType === "CRM") ||
            (sourceType === "MANUAL")) && (
            <div className="bg-[#f8f8f7] p-4 rounded-xl border border-black/5 flex flex-col md:flex-row md:items-center justify-between gap-3 select-none">
              <div className="text-xs space-y-1">
                <span className="font-semibold text-black">Recipients:</span>
                {resolving ? (
                  <p className="text-[#6B7280] animate-pulse">Resolving target parameters...</p>
                ) : recipients.length > 0 ? (
                  <div className="space-y-1 mt-1">
                    <p className="text-[#6B7280]">
                      Ready to send to <strong className="text-black">{sendingCount}</strong> targets.
                      {unresolvable.length > 0 && <span className="text-amber-600"> ({unresolvable.length} skipped due to missing numbers).</span>}
                    </p>
                    {sourceType === "SAVED_LISTS" && (
                      <div className="pt-2 border-t border-black/[0.04] mt-2 space-y-1">
                        <div className="text-[10px] font-bold uppercase tracking-wider text-black/50">Lists Included:</div>
                        <div className="flex flex-wrap gap-2">
                          {contactLists
                            .filter(lst => selectedListIds.includes(lst.id))
                            .map(lst => (
                              <span key={lst.id} className="inline-flex items-center px-2 py-0.5 rounded bg-black/5 text-black/70 text-[10px] font-medium">
                                {lst.name}: {lst.contacts?.length || 0}
                              </span>
                            ))}
                        </div>
                        <div className="text-[10px] font-medium text-[#6B7280]">
                          Total Unique Recipients: <strong className="text-black">{recipients.length}</strong>
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="text-[#6B7280]">No recipients matches criteria.</p>
                )}
              </div>
              <div className="text-xs font-semibold uppercase tracking-wider text-[#6B7280]">
                {resolving ? (
                  <span className="flex items-center gap-1.5"><RefreshCw className="w-3.5 h-3.5 animate-spin" /> Resolving...</span>
                ) : recipients.length > 0 ? (
                  <span className="text-[#0D8A5F]">✓ {recipients.length} resolved</span>
                ) : (
                  <span>Ready</span>
                )}
              </div>
            </div>
          )}

          {/* Shimmer Recipient Skeleton (P0 Task 2.1) */}
          {resolving && (
            <div className="space-y-3" data-testid="shimmer-recipients">
              {[...Array(4)].map((_, idx) => (
                <div key={idx} className="h-[56px] w-full bg-[#f8f8f7] animate-pulse rounded-xl flex items-center justify-between px-4 border border-black/[0.02]">
                  <div className="h-4 bg-[#111111]/5 rounded w-1/3 shimmer"></div>
                  <div className="h-4 bg-[#111111]/5 rounded w-1/4 shimmer"></div>
                </div>
              ))}
            </div>
          )}

          {/* Resolved Targets list (Task 3.1 Border Reduction) */}
          {!resolving && recipients.length > 0 && (
            <div className="border border-black/[0.06] rounded-xl overflow-hidden shadow-sm bg-white">
              <div className="bg-[#f8f8f7] px-5 py-3 flex flex-wrap items-center justify-between gap-2 border-b border-black/[0.04]">
                <span className="text-[11px] font-bold uppercase tracking-widest text-[#6B7280]">
                  Resolved Targets ({recipients.length})
                </span>
                <span className="text-[11px] font-mono text-xs">
                  <strong className="text-emerald-700">Sending: {sendingCount}</strong>
                  {" · "}
                  <span className="text-red-600">Excluded: {excludedIds.size}</span>
                </span>
              </div>

              {/* Search + bulk actions */}
              <div className="px-4 py-3 flex flex-wrap items-center gap-2 border-b border-black/[0.04]">
                <input
                  value={targetSearch}
                  onChange={(e) => setTargetSearch(e.target.value)}
                  placeholder="Search name / phone / group…"
                  className="flex-1 min-w-[180px] text-xs px-3 py-2 border border-black/10 rounded-lg focus:outline-none focus:border-black/40 h-[40px] bg-[#f8f8f7]"
                />
                <button onClick={() => applyExclusion(true)} disabled={selectedRowIds.size === 0}
                  className="text-[10px] font-bold uppercase tracking-wider px-3 py-2 rounded-lg border border-red-200/50 text-red-600 hover:bg-red-500/10 disabled:opacity-40 transition-colors h-[40px] active:scale-[0.98] duration-120">
                  Exclude
                </button>
                <button onClick={() => applyExclusion(false)} disabled={selectedRowIds.size === 0}
                  className="text-[10px] font-bold uppercase tracking-wider px-3 py-2 rounded-lg border border-emerald-200/50 text-[#0D8A5F] hover:bg-[#0D8A5F]/10 disabled:opacity-40 transition-colors h-[40px] active:scale-[0.98] duration-120">
                  Include
                </button>
              </div>

              {/* Header row */}
              <div className="flex items-center gap-2 px-5 py-2 text-[10px] uppercase tracking-wider text-[#6B7280] bg-[#fafafa]">
                <input type="checkbox"
                  checked={filteredRecipients.length > 0 && filteredRecipients.every((r) => selectedRowIds.has(r.recipient_id))}
                  onChange={(e) => selectAllFiltered(e.target.checked)} />
                <span className="flex-1">Name</span>
                <span className="w-28">Type</span>
                <span className="flex-1">Destination</span>
              </div>

              {/* Virtualized rows (removed table row borders to satisfy Task 3.1) */}
              <VirtualList
                items={filteredRecipients}
                rowHeight={40}
                height={Math.min(filteredRecipients.length, 6) * 40 || 40}
                renderRow={(r, idx) => {
                  const excluded = excludedIds.has(r.recipient_id);
                  const type = r.destination_type === "group" ? "GROUP" : "PHONE";
                  const isSelected = selectedRowIds.has(r.recipient_id);
                  return (
                    <div className={`flex items-center gap-2 px-5 text-xs h-full transition-colors ${
                      excluded ? "opacity-45 line-through" : isSelected ? "bg-black/[0.02]" : "hover:bg-black/[0.01]"
                    }`}>
                      <input type="checkbox" checked={isSelected} onChange={() => toggleRow(r.recipient_id)} />
                      <span className="flex-1 font-medium truncate text-[#111111]">{r.name || "—"}</span>
                      <span className="w-28">
                        <span className={`text-[9px] font-bold uppercase px-2 py-0.5 rounded-full ${
                          type === "GROUP" ? "bg-indigo-500/10 text-indigo-700" : "bg-sky-500/10 text-sky-700"
                        }`}>{type}</span>
                      </span>
                      <span className="flex-1 font-mono text-[11px] text-[#6B7280] truncate">{r.destination}</span>
                    </div>
                  );
                }}
              />
            </div>
          )}

          {/* Template Selection */}
          <div className="space-y-4 border-t border-black/[0.04] pt-6">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-3">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Message Template</label>
                <select
                  value={selectedTemplateId}
                  onChange={(e) => handleTemplateChange(e.target.value)}
                  className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px] transition-all"
                >
                  <option value="">Select Template...</option>
                  {templates.map(t => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </div>

              {selectedTemplate && (
                <div className="space-y-3">
                  <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Media Attachment Link</label>
                  <input
                    type="url"
                    value={mediaUrl}
                    onChange={(e) => setMediaUrl(e.target.value)}
                    placeholder="https://res.cloudinary.com/..."
                    className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px] transition-all"
                  />
                </div>
              )}
            </div>

            {/* Variable Form Placeholders — only those NOT auto-resolved for
                the current source (Part 2). Auto-resolved vars (project fields
                for a Project source, sender, date, recipient name) are filled in
                by the backend, so the admin is never asked to type them. */}
            {selectedTemplate && Object.keys(variables).filter(k => !waIsAutoResolved(k, sourceType)).length > 0 && (
              <div className="space-y-4 bg-[#f8f8f7] p-5 rounded-2xl border border-black/5">
                <h4 className="text-xs font-bold uppercase tracking-widest text-[#6B7280] pb-2 border-b border-black/[0.04]">Inject Custom Variables</h4>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {Object.keys(variables).filter(k => !waIsAutoResolved(k, sourceType)).map(vKey => (
                    <div key={vKey} className="space-y-1">
                      <label className="text-[10px] font-bold uppercase tracking-wider text-[#6B7280]">{vKey}</label>
                      <input
                        type="text"
                        value={variables[vKey]}
                        onChange={(e) => setVariables({ ...variables, [vKey]: e.target.value })}
                        className="w-full text-xs bg-white border border-black/15 rounded-lg p-3 focus:outline-none focus:ring-1 focus:ring-black h-[40px] transition-all"
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

        {/* WhatsApp Mobile Shell Preview Column (Task 4.1 & Task 8.2) */}
        <div className="flex flex-col items-center justify-start space-y-6">
          
          {/* Smart Phone Container Mockup */}
          <div className="w-[340px] h-[580px] bg-[#1a1a1a] rounded-[48px] p-3 shadow-2xl relative border-4 border-[#333333] flex flex-col overflow-hidden">
            {/* Status bar notch */}
            <div className="absolute top-2 left-1/2 -translate-x-1/2 w-32 h-4 bg-black rounded-full z-20 flex items-center justify-between px-4">
              <span className="w-1.5 h-1.5 rounded-full bg-white/20"></span>
              <span className="w-8 h-1 bg-white/20 rounded-full"></span>
            </div>

            {/* WhatsApp Interface inside phone */}
            <div className="flex-1 bg-[#efeae2] rounded-[36px] overflow-hidden flex flex-col relative">
              
              {/* WhatsApp Header bar */}
              <div className="bg-[#075e54] text-white pt-6 pb-2 px-4 flex items-center gap-2.5 z-10 shrink-0">
                <div className="w-8 h-8 rounded-full bg-white/10 flex items-center justify-center overflow-hidden border border-white/20">
                  {selectedTemplate?.media_url ? (
                    <img src={selectedTemplate.media_url} className="w-full h-full object-cover" />
                  ) : (
                    <User className="w-4 h-4 text-white/50" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-[12px] font-bold leading-tight truncate">
                    {activePreviewRecipient ? activePreviewRecipient.name : "Casting Assistant"}
                  </p>
                  <p className="text-[9px] text-white/60 leading-none">Online</p>
                </div>
              </div>

              {/* Chat Conversation messages area */}
              <div className="flex-1 p-3 overflow-y-auto space-y-3 flex flex-col justify-end">
                
                {/* Auto Resolve Skeleton placeholder */}
                {resolving && (
                  <div className="max-w-[85%] self-start bg-white rounded-lg p-2.5 shadow-sm space-y-2 border border-black/5 animate-pulse w-3/4">
                    <div className="h-3 bg-black/10 rounded w-5/6"></div>
                    <div className="h-3 bg-black/10 rounded w-2/3"></div>
                  </div>
                )}

                {/* Main WhatsApp preview message bubble */}
                {!resolving && selectedTemplate ? (
                  <div className="max-w-[85%] self-start bg-white rounded-xl p-3 shadow-sm border border-black/5 space-y-2.5 text-[#111111]">
                    {/* Media render if Cloudinary attachment exists */}
                    {mediaUrl && (
                      <div className="rounded-lg overflow-hidden border border-black/5 bg-[#fafaf9] max-h-36 flex items-center justify-center">
                        <img src={mediaUrl} className="w-full h-full object-cover" onError={(e) => e.target.style.display='none'} />
                      </div>
                    )}
                    <div className="text-[11px] leading-relaxed whitespace-pre-wrap select-text font-sans">
                      {selectedTemplate.body_text
                        .replace("{{talent_name}}", activePreviewRecipient ? activePreviewRecipient.name : "Ayushi Thakur")
                        .replace(/\{\{(\w+)\}\}/g, (match, p1) => variables[p1] || `[${p1}]`)
                      }
                    </div>
                    <div className="text-[8px] text-[#6B7280] text-right mt-1 leading-none">
                      {new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  </div>
                ) : (
                  !resolving && (
                    <div className="text-center py-12 text-[#6B7280]/60 italic text-xs w-full">
                      No template selected.
                    </div>
                  )
                )}

                {/* Typing indicator skeleton if resolving */}
                {resolving && (
                  <div className="self-start bg-white/70 px-3 py-1.5 rounded-full flex gap-1 items-center animate-bounce">
                    <span className="w-1.5 h-1.5 bg-black/30 rounded-full animate-ping"></span>
                    <span className="w-1.5 h-1.5 bg-black/30 rounded-full"></span>
                    <span className="w-1.5 h-1.5 bg-black/30 rounded-full"></span>
                  </div>
                )}

              </div>

              {/* Typing box bottom bar */}
              <div className="bg-[#f0f0f0] p-2 flex items-center gap-2 border-t border-black/5 shrink-0">
                <div className="flex-1 bg-white rounded-full h-7 px-3 flex items-center text-[10px] text-black/30">
                  Type a message...
                </div>
                <div className="w-7 h-7 rounded-full bg-[#075e54] flex items-center justify-center text-white">
                  <Send className="w-3.5 h-3.5" />
                </div>
              </div>

            </div>
          </div>

          {/* Swipe Preview cycle buttons (Task 8.2) */}
          {recipients.length > 1 && (
            <div className="flex items-center gap-4 bg-white px-4 py-2 rounded-full border border-black/[0.06] shadow-sm select-none">
              <button
                onClick={() => setPreviewTargetIndex((prev) => (prev > 0 ? prev - 1 : filteredRecipients.length - 1))}
                className="p-1 hover:bg-black/5 rounded-full transition-colors active:scale-90"
                title="Previous Recipient"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <span className="text-[10px] font-bold uppercase tracking-wider text-[#6B7280]">
                {previewTargetIndex + 1} of {filteredRecipients.length}
              </span>
              <button
                onClick={() => setPreviewTargetIndex((prev) => (prev < filteredRecipients.length - 1 ? prev + 1 : 0))}
                className="p-1 hover:bg-black/5 rounded-full transition-colors active:scale-90"
                title="Next Recipient"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          )}

          {/* Launch Controls Container */}
          <div className="w-[340px] flex flex-col gap-2">
            <button
              onClick={handleDryRun}
              disabled={launching || !selectedTemplateId || recipients.length === 0}
              className="text-xs font-semibold uppercase tracking-widest bg-black text-white hover:opacity-90 active:scale-[0.98] duration-120 py-3.5 rounded-lg text-center disabled:opacity-50 transition-all shadow-sm w-full h-[52px] flex items-center justify-center"
            >
              Compile Dry Run Preview
            </button>
            
            {dryRunResult && (
              <button
                onClick={handleLaunch}
                disabled={launching}
                className="text-xs font-semibold uppercase tracking-widest bg-[#0D8A5F] hover:bg-[#0D8A5F]/95 text-white active:scale-[0.98] duration-120 py-3.5 rounded-lg text-center transition-all shadow-sm w-full h-[52px] flex items-center justify-center"
              >
                {launching ? "Launching..." : `Launch Broadcast (${recipients.length})`}
              </button>
            )}
          </div>
        </div>

      </div>

      {/* Compile Dry Run Preview Table */}
      {dryRunResult && (
        <div className="bg-white p-8 rounded-2xl space-y-6 shadow-sm">
          <div className="flex justify-between items-center border-b border-black/[0.04] pb-4">
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Dry Run Auditing</h3>
              <p className="text-xs text-[#6B7280] mt-0.5">Below is the exact output rendering showing simulated delivery destinations.</p>
            </div>
            <div className="flex gap-2">
              <span className="text-[10px] font-bold uppercase bg-amber-500/10 text-amber-700 px-2.5 py-1 border border-amber-500/10 rounded-full">Dry Run Only</span>
              <span className="text-[10px] font-bold uppercase bg-black/5 text-[#111111] px-2.5 py-1 rounded-full">Total Jobs: {dryRunResult.batch?.total_jobs || 0}</span>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-black/10 text-[#6B7280] font-semibold uppercase tracking-wider text-[10px]">
                  <th className="py-3">Talent Name</th>
                  <th className="py-3">Channel</th>
                  <th className="py-3">Address</th>
                  <th className="py-3">Rendered Message Body</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-black/[0.03]">
                {dryRunResult.jobs?.map((job) => (
                  <tr key={job.id} className="hover:bg-[#f8f8f7] transition-colors">
                    <td className="py-3 font-semibold text-[#111111]">{job.talent_name}</td>
                    <td className="py-3 capitalize text-[#6B7280]">{job.destination_type}</td>
                    <td className="py-3 font-mono text-[11px] text-[#6B7280]">{job.destination}</td>
                    <td className="py-3 max-w-md truncate font-mono text-[11px] text-[#6B7280]/80">{job.message_body}</td>
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
// 3. CAMPAIGN HISTORY
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
      toast.success(`Action: '${action}' executed successfully.`);
      fetchHistory();
      if (selectedBatchId === batchId) {
        handleBatchClick(batchId);
      }
    } catch (err) {
      toast.error(formatErrorDetail(err, "Execution failed"));
    }
  };

  const handleRetryJob = async (job) => {
    try {
      await retryJob(job.id);
      toast.success("Job re-queued successfully");
      if (selectedBatchId) {
        const jobList = await getJobs(selectedBatchId);
        setJobs(jobList);
      }
    } catch (err) {
      toast.error("Retry failed");
    }
  };

  if (loading) {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div className="lg:col-span-1 space-y-4">
          {[...Array(3)].map((_, idx) => (
            <div key={idx} className="bg-white p-6 rounded-2xl space-y-3 shadow-sm animate-pulse border border-black/[0.02]">
              <div className="h-4 bg-[#111111]/5 rounded w-2/3"></div>
              <div className="h-3 bg-[#111111]/5 rounded w-1/2"></div>
            </div>
          ))}
        </div>
        <div className="lg:col-span-2 bg-white rounded-2xl h-96 animate-pulse"></div>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
      
      {/* Left List of Campaigns batches */}
      <div className="lg:col-span-1 bg-white p-6 rounded-2xl space-y-4 shadow-sm max-h-[600px] overflow-y-auto">
        <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280] pb-2 border-b border-black/[0.04]">Campaign Batches</h3>
        <div className="space-y-3">
          {batches.map((b) => {
            const isSelected = selectedBatchId === b.id;
            const completed = b.processed_jobs || 0;
            const total = b.total_jobs || 0;
            const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
            return (
              <div
                key={b.id}
                onClick={() => handleBatchClick(b.id)}
                className={`p-5 rounded-xl border cursor-pointer transition-all duration-150 space-y-3 select-none ${
                  isSelected ? "bg-[#f8f8f7] border-black" : "bg-white border-black/[0.06] hover:border-black/20"
                }`}
              >
                <div className="flex justify-between items-start gap-2">
                  <h4 className="font-semibold text-sm text-[#111111]">{b.template_name}</h4>
                  <span className={`text-[9px] font-mono font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-full ${
                    b.status === "processing" ? "bg-indigo-500/10 text-indigo-700 animate-pulse" :
                    b.status === "completed" ? "bg-[#0D8A5F]/10 text-[#0D8A5F]" :
                    b.status === "paused" ? "bg-amber-500/10 text-amber-700" :
                    "bg-black/5 text-[#6B7280]"
                  }`}>
                    {b.status}
                  </span>
                </div>

                <div className="text-[10px] text-[#6B7280] font-mono flex justify-between">
                  <span>Source: {b.source_type}</span>
                  <span>{new Date(b.created_at).toLocaleDateString()}</span>
                </div>

                {/* Progress bar */}
                <div className="space-y-1">
                  <div className="flex justify-between text-[10px] text-[#6B7280]">
                    <span>Delivered: {completed}/{total}</span>
                    <span>{percent}%</span>
                  </div>
                  <div className="w-full bg-black/[0.04] h-1.5 rounded-full overflow-hidden">
                    <div className="bg-[#111111] h-full" style={{ width: `${percent}%` }} />
                  </div>
                </div>

                {/* Actions quick triggers */}
                <div className="flex gap-1.5 pt-1 border-t border-black/[0.04] select-none">
                  {b.status === "paused" && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleAction(b.id, "resume"); }}
                      className="text-[9px] font-bold uppercase tracking-wider border border-[#0D8A5F]/20 text-[#0D8A5F] hover:bg-[#0D8A5F]/10 px-2 py-1 rounded-lg"
                    >
                      Resume
                    </button>
                  )}
                  {b.status === "processing" && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleAction(b.id, "pause"); }}
                      className="text-[9px] font-bold uppercase tracking-wider border border-amber-200 text-amber-700 hover:bg-amber-500/10 px-2 py-1 rounded-lg"
                    >
                      Pause
                    </button>
                  )}
                  {b.status !== "completed" && b.status !== "cancelled" && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleAction(b.id, "cancel"); }}
                      className="text-[9px] font-bold uppercase tracking-wider border border-red-200 text-red-600 hover:bg-red-500/10 px-2 py-1 rounded-lg"
                    >
                      Cancel
                    </button>
                  )}
                </div>
              </div>
            );
          })}
          {batches.length === 0 && (
            <p className="text-xs text-[#6B7280] text-center py-6 italic">No campaign runs registered.</p>
          )}
        </div>
      </div>

      {/* Right Jobs audit list */}
      <div className="lg:col-span-2 bg-white p-6 rounded-2xl shadow-sm min-h-[400px]">
        {selectedBatchId ? (
          loadingJobs ? (
            <div className="flex flex-col items-center justify-center p-12 h-full">
              <RefreshCw className="w-6 h-6 animate-spin text-[#111111]/30 mb-2" />
              <p className="text-xs text-[#6B7280]">Loading delivery breakdowns...</p>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="border-b border-black/[0.04] pb-3">
                <h3 className="text-sm font-semibold uppercase tracking-wider text-black/60">Jobs Audit tracker</h3>
                <p className="text-xs text-[#6B7280] mt-0.5">Execution logs for batch: <code className="text-xs font-mono">{selectedBatchId.slice(0, 8)}</code></p>
              </div>

              <div className="overflow-x-auto max-h-[500px] overflow-y-auto pr-1">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-black/10 text-[#6B7280] font-semibold uppercase tracking-wider text-[10px]">
                      <th className="py-2.5">Talent</th>
                      <th className="py-2.5">Destination</th>
                      <th className="py-2.5">Status</th>
                      <th className="py-2.5">Retries</th>
                      <th className="py-2.5">Error Message</th>
                      <th className="py-2.5 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-black/[0.03]">
                    {jobs.map((job) => (
                      <tr key={job.id} className="hover:bg-[#f8f8f7] transition-colors">
                        <td className="py-2.5 font-semibold text-[#111111]">{job.talent_name}</td>
                        <td className="py-2.5 font-mono text-[11px] text-[#6B7280]">{job.destination}</td>
                        <td className="py-2.5">
                          <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-full ${
                            job.status === "sent" ? "bg-[#0D8A5F]/10 text-[#0D8A5F]" :
                            job.status === "failed" ? "bg-red-500/10 text-red-600" :
                            job.status === "skipped" ? "bg-amber-500/10 text-amber-700" :
                            "bg-black/5 text-[#6B7280]"
                          }`}>
                            {job.status}
                          </span>
                        </td>
                        <td className="py-2.5 font-mono text-[#6B7280]">{job.retry_count || 0}</td>
                        <td className="py-2.5 max-w-xs truncate text-[11px] text-red-600 font-mono" title={job.error_message}>
                          {job.error_message || "—"}
                        </td>
                        <td className="py-2.5 text-right select-none">
                          {(job.status === "failed" || job.status === "skipped") && (
                            <button
                              onClick={() => handleRetryJob(job)}
                              className="text-[9px] font-bold text-black uppercase tracking-widest hover:underline border border-black/10 px-2 py-1 rounded-lg bg-white active:scale-95 duration-100"
                            >
                              Retry
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {jobs.length === 0 && (
                      <tr>
                        <td colSpan="6" className="text-center py-6 text-[#6B7280]">No individual message logs found.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )
        ) : (
          <div className="flex flex-col items-center justify-center p-12 text-black/30 border border-dashed border-black/10 rounded-2xl min-h-[300px] h-full">
            <History className="w-8 h-8 opacity-30 mb-2" />
            <p className="text-xs text-[#6B7280]">Select a campaign batch to view delivery breakdowns.</p>
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

  // Part 5 — insert {{key}} at the cursor (or replace the selection) in the body
  // textarea, then re-extract variables and restore focus/caret.
  const bodyRef = useRef(null);
  const insertVariable = (key) => {
    const token = `{{${key}}}`;
    const el = bodyRef.current;
    const body = formData.body_text || "";
    const start = el ? el.selectionStart : body.length;
    const end = el ? el.selectionEnd : body.length;
    const next = body.slice(0, start) + token + body.slice(end);
    handleBodyTextChange(next);
    // Restore caret just after the inserted token on the next paint.
    requestAnimationFrame(() => {
      if (!el) return;
      el.focus();
      const pos = start + token.length;
      try { el.setSelectionRange(pos, pos); } catch (_) {}
    });
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
      toast.error(formatErrorDetail(err, "Template save failed"));
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
      toast.error(formatErrorDetail(err, "Failed to delete template"));
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center p-12 bg-white rounded-2xl border border-black/[0.06] shadow-sm">
        <RefreshCw className="w-6 h-6 animate-spin text-[#111111]/30" />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-8">
      
      {/* List */}
      <div className="xl:col-span-1 bg-white p-6 rounded-2xl shadow-sm space-y-4">
        <div className="flex justify-between items-center border-b border-black/[0.04] pb-3">
          <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Saved Templates</h3>
          <button
            onClick={handleCreateNewClick}
            className="flex items-center gap-1.5 text-xs font-bold bg-black text-white px-3 py-2 rounded-lg hover:opacity-90 active:scale-[0.98] duration-120 transition-all"
          >
            <Plus className="w-3.5 h-3.5" /> Create
          </button>
        </div>

        <div className="space-y-3 max-h-[600px] overflow-y-auto pr-1">
          {templates.map(t => (
            <div
              key={t.id}
              onClick={() => handleEditClick(t)}
              className={`p-5 rounded-xl border cursor-pointer transition-all duration-150 space-y-2 select-none ${
                editingId === t.id 
                  ? "bg-[#f8f8f7] border-black" 
                  : "bg-white border-black/[0.06] hover:border-black/25"
              }`}
            >
              <div className="flex justify-between items-start gap-2">
                <h4 className="font-semibold text-sm text-[#111111]">{t.name}</h4>
                {!t.is_custom && (
                  <span className="text-[9px] font-bold uppercase tracking-wider bg-black/[0.04] px-1.5 py-0.5 rounded-full text-black/50 border border-black/[0.06]">System</span>
                )}
              </div>
              <p className="text-xs text-[#6B7280] font-mono truncate">{t.slug}</p>
              
              <div className="flex flex-wrap gap-1 mt-2">
                {t.variables?.map(v => (
                  <span key={v} className="text-[9px] font-mono bg-black/[0.04] px-2 py-0.5 rounded-full text-black/60 border border-black/[0.02]">
                    {v}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Editor Panel */}
      <div className="xl:col-span-2 bg-white p-8 rounded-2xl shadow-sm">
        {editingId ? (
          <form onSubmit={handleSave} className="space-y-6">
            <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280] border-b border-black/[0.04] pb-3">
              {editingId === "new" ? "New Template Layout" : "Edit Template Layout"}
            </h3>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-2">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Template Label</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="e.g. Casting Callback"
                  className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
                  required
                />
              </div>

              <div className="space-y-2">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Unique URL Slug Identifier</label>
                <input
                  type="text"
                  value={formData.slug}
                  onChange={(e) => setFormData({ ...formData, slug: e.target.value.toLowerCase().replace(/\s+/g, "_") })}
                  placeholder="e.g. casting_callback"
                  className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
                  required
                  disabled={editingId !== "new" && !formData.is_custom}
                />
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-2">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Media Type</label>
                <select
                  value={formData.media_type}
                  onChange={(e) => setFormData({ ...formData, media_type: e.target.value })}
                  className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
                >
                  <option value="none">None (Text Only)</option>
                  <option value="image">Image/Video Upload</option>
                  <option value="document">Document (PDF)</option>
                </select>
              </div>

              {formData.media_type !== "none" && (
                <div className="space-y-2">
                  <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Cloudinary Static Media Attachment Link</label>
                  <input
                    type="url"
                    value={formData.media_url}
                    onChange={(e) => setFormData({ ...formData, media_url: e.target.value })}
                    placeholder="https://res.cloudinary.com/..."
                    className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
                  />
                </div>
              )}
            </div>

            <div className="space-y-2">
              <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280] flex justify-between">
                <span>Message Body Structure</span>
                <span className="text-[10px] text-[#6B7280] font-mono normal-case">Wrap placeholders in double curly braces: {"{{var}}"}</span>
              </label>
              <textarea
                ref={bodyRef}
                value={formData.body_text}
                onChange={(e) => handleBodyTextChange(e.target.value)}
                rows="8"
                placeholder="Hi {{talent_name}}... We are locked for {{project_name}}."
                className="w-full text-xs font-mono bg-[#f8f8f7] border border-black/10 rounded-lg p-3 focus:outline-none focus:ring-1 focus:ring-black leading-relaxed"
                required
              />
            </div>

            {/* Part 5 — Available Variables: click to insert at the cursor. */}
            <div className="space-y-2.5">
              <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Available Variables</label>
              <div className="space-y-3 bg-[#f8f8f7] p-4 rounded-2xl border border-black/5">
                {WA_VARIABLE_CATALOG.map(group => (
                  <div key={group.category} className="space-y-1.5">
                    <p className="text-[10px] font-bold uppercase tracking-wider text-[#6B7280]/70">{group.category}</p>
                    <div className="flex flex-wrap gap-1.5">
                      {group.variables.map(v => (
                        <button
                          key={v}
                          type="button"
                          onClick={() => insertVariable(v)}
                          title={`Insert {{${v}}}`}
                          className="text-xs font-mono bg-white px-2.5 py-1 rounded-full border border-black/10 text-black/70 font-semibold hover:bg-black hover:text-white transition-colors active:scale-95 duration-100"
                        >
                          {`{{${v}}}`}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {formData.variables.length > 0 && (
              <div className="space-y-2">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Extracted Dynamic Variables</label>
                <div className="flex flex-wrap gap-1.5">
                  {formData.variables.map(v => (
                    <span key={v} className="text-xs font-mono bg-[#f8f8f7] px-2.5 py-1 rounded-full border border-black/10 text-black/70 font-semibold">
                      {v}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="flex justify-between items-center pt-4 border-t border-black/[0.04] select-none">
              {editingId !== "new" && formData.is_custom ? (
                <button
                  type="button"
                  onClick={() => handleDelete(editingId)}
                  className="flex items-center gap-1.5 text-xs font-bold text-red-600 uppercase hover:underline"
                >
                  <Trash2 className="w-4 h-4" /> Delete Template
                </button>
              ) : <div />}

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setEditingId(null)}
                  className="text-xs font-semibold uppercase tracking-widest text-black/60 hover:text-black px-4 py-3 border border-black/15 rounded-lg bg-white h-[48px] active:scale-95 duration-120"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-widest bg-black text-white px-5 py-3 rounded-lg hover:opacity-90 transition-colors h-[48px] active:scale-[0.98] duration-120"
                >
                  <Save className="w-4 h-4" /> Save Template
                </button>
              </div>
            </div>

          </form>
        ) : (
          <div className="flex flex-col items-center justify-center p-12 text-[#6B7280] border border-dashed border-black/10 rounded-2xl min-h-[300px]">
            <Edit className="w-8 h-8 opacity-30 mb-2" />
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
  const [savingGroup, setSavingGroup] = useState(false);

  // TEMP TEST TOOL / REMOVE AFTER WHATSAPP VALIDATION
  const [testingNotification, setTestingNotification] = useState(false);
  const [testResult, setTestResult] = useState(null);

  // TEMP TEST TOOL / REMOVE AFTER WHATSAPP VALIDATION
  const handleTestNotification = async () => {
    setTestingNotification(true);
    setTestResult(null);
    try {
      const res = await testInternalNotification();
      setTestResult(res);
      toast.success("Test notification queued successfully");
    } catch (err) {
      toast.error(formatErrorDetail(err, "Failed to trigger test notification"));
    } finally {
      setTestingNotification(false);
    }
  };

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
      const safetyConfig = { ...config };
      delete safetyConfig.internal_notification_group_name;
      await Promise.all(
        Object.entries(safetyConfig).map(([key, val]) => updateWaConfig(key, val))
      );
      toast.success("Configurations updated successfully");
      fetchConfig();
    } catch (err) {
      toast.error("Failed saving safety variables");
    } finally {
      setSaving(false);
    }
  };

  const handleSaveInternalGroup = async (e) => {
    e.preventDefault();
    setSavingGroup(true);
    try {
      const val = config.internal_notification_group_name || "";
      await updateWaConfig("internal_notification_group_name", val);
      toast.success("Internal notification group updated successfully");
      fetchConfig();
    } catch (err) {
      toast.error("Failed saving group name configuration");
    } finally {
      setSavingGroup(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center p-12 bg-white rounded-2xl border border-black/[0.06] shadow-sm">
        <RefreshCw className="w-6 h-6 animate-spin text-[#111111]/30" />
      </div>
    );
  }

  return (
    <div className="max-w-2xl bg-white p-8 rounded-2xl shadow-sm space-y-8">
      <div className="border-b border-black/[0.04] pb-4">
        <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Safety Controls & Anti-Ban</h3>
        <p className="text-xs text-[#6B7280] mt-1.5 leading-relaxed">Adjust delivery parameters. Slower execution reduces risk of meta banning. ONLY system admins can override these.</p>
      </div>

      <form onSubmit={handleSave} className="space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-2">
            <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Minimum Delay (Seconds)</label>
            <input
              type="number"
              value={config.min_delay_sec || 8}
              onChange={(e) => handleChange("min_delay_sec", e.target.value)}
              className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
              min="2"
              required
            />
          </div>

          <div className="space-y-2">
            <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Maximum Delay (Seconds)</label>
            <input
              type="number"
              value={config.max_delay_sec || 15}
              onChange={(e) => handleChange("max_delay_sec", e.target.value)}
              className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
              min="3"
              required
            />
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-2">
            <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Retry Attempts</label>
            <input
              type="number"
              value={config.max_retries || 3}
              onChange={(e) => handleChange("max_retries", e.target.value)}
              className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
              min="0"
              required
            />
          </div>

          <div className="space-y-2">
            <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Circuit Breaker Threshold</label>
            <input
              type="number"
              value={config.circuit_breaker_threshold || 5}
              onChange={(e) => handleChange("circuit_breaker_threshold", e.target.value)}
              className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
              min="1"
              required
            />
            <p className="text-[10px] text-[#6B7280]">Number of consecutive failures before auto-pausing.</p>
          </div>
        </div>

        <div className="pt-4 border-t border-black/[0.04] flex justify-end select-none">
          <button
            type="submit"
            disabled={saving}
            className="flex items-center justify-center gap-1.5 text-xs font-semibold uppercase tracking-widest bg-black text-white px-6 py-3.5 rounded-lg hover:opacity-90 transition-colors disabled:opacity-50 h-[52px] active:scale-[0.98] duration-120"
          >
            {saving ? "Saving..." : "Save Configurations"}
          </button>
        </div>
      </form>

      {/* Internal Notifications Settings */}
      <div className="border-t border-black/[0.04] pt-6 space-y-6">
        <div className="space-y-1">
          <h4 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Internal Notifications</h4>
          <p className="text-xs text-[#6B7280]">
            Configure the target WhatsApp group name for system alerts and notifications.
          </p>
        </div>

        <form onSubmit={handleSaveInternalGroup} className="space-y-4">
          <div className="space-y-2">
            <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Internal Notification Group Name</label>
            <input
              type="text"
              value={config.internal_notification_group_name || ""}
              onChange={(e) => handleChange("internal_notification_group_name", e.target.value)}
              className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
              placeholder="e.g. Talentgram Operations"
              required
            />
          </div>

          <div className="flex justify-end select-none">
            <button
              type="submit"
              disabled={savingGroup}
              className="flex items-center justify-center gap-1.5 text-xs font-semibold uppercase tracking-widest bg-black text-white px-6 py-3.5 rounded-lg hover:opacity-90 transition-colors disabled:opacity-50 h-[52px] active:scale-[0.98] duration-120"
            >
              {savingGroup ? "Saving..." : "Save Configuration"}
            </button>
          </div>
        </form>
      </div>

      {/* TEMP TEST TOOL / REMOVE AFTER WHATSAPP VALIDATION */}
      <div className="border-t border-black/[0.04] pt-6 space-y-4">
        <div className="space-y-1">
          <h4 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">WhatsApp Internal Notification Test</h4>
          <p className="text-xs text-[#6B7280]">
            Verify backend connection to the queue and the dedicated internal WhatsApp group.
          </p>
        </div>
        <div className="flex flex-col gap-3">
          <button
            type="button"
            onClick={handleTestNotification}
            disabled={testingNotification}
            className="w-full flex items-center justify-center gap-1.5 text-xs font-semibold uppercase tracking-widest bg-red-600 hover:bg-red-700 text-white px-6 py-3.5 rounded-lg transition-colors disabled:opacity-50 h-[52px] active:scale-[0.98] duration-120"
          >
            {testingNotification ? "Sending Test..." : "Send Internal Notification Test"}
          </button>
          {testResult && (
            <div className="bg-[#f8f8f7] border border-black/5 p-4 rounded-xl space-y-2 text-xs">
              <p className="font-semibold text-emerald-700">✓ Test notification queued successfully</p>
              <div className="grid grid-cols-2 gap-2 text-[11px] font-mono text-[#6B7280]">
                <div>
                  <span className="font-bold text-[#111111]">Batch ID:</span> {testResult.batch_id}
                </div>
                <div>
                  <span className="font-bold text-[#111111]">Job ID:</span> {testResult.job_id}
                </div>
                <div className="col-span-2">
                  <span className="font-bold text-[#111111]">Group:</span> {testResult.group_name}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
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
      <div className="flex justify-center p-12 bg-white rounded-2xl border border-black/[0.06] shadow-sm">
        <RefreshCw className="w-6 h-6 animate-spin text-[#111111]/30" />
      </div>
    );
  }

  return (
    <div className="bg-white p-8 rounded-2xl shadow-sm space-y-6">
      <div className="flex justify-between items-center border-b border-black/[0.04] pb-4 select-none">
        <div>
          <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Immutable Audit Logs</h3>
          <p className="text-xs text-[#6B7280] mt-0.5">Continuous delivery auditing of automation flows.</p>
        </div>
        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          className="text-xs bg-[#f8f8f7] border border-black/10 rounded-lg p-2 focus:outline-none"
        >
          <option value="50">Show 50</option>
          <option value="100">Show 100</option>
          <option value="200">Show 200</option>
        </select>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse text-xs">
          <thead>
            <tr className="border-b border-black/10 text-[#6B7280] font-semibold uppercase tracking-wider text-[10px]">
              <th className="py-3">Timestamp</th>
              <th className="py-3">Event Code</th>
              <th className="py-3">Destination</th>
              <th className="py-3">Actor / Node</th>
              <th className="py-3">Render Preview</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-black/[0.03] font-mono text-[11px] text-[#111111]">
            {logs.map((log) => (
              <tr key={log.id} className="hover:bg-[#f8f8f7] transition-colors">
                <td className="py-3 text-[#6B7280]">{new Date(log.timestamp).toLocaleString()}</td>
                <td className="py-3">
                  <span className={`px-2 py-0.5 rounded-full font-bold uppercase text-[9px] ${
                    log.event_type.includes("failed") ? "bg-red-500/10 text-red-700" :
                    log.event_type.includes("sent") ? "bg-[#0D8A5F]/10 text-[#0D8A5F]" :
                    "bg-black/5 text-[#6B7280]"
                  }`}>
                    {log.event_type}
                  </span>
                </td>
                <td className="py-3 text-[#6B7280]">{log.destination || "—"}</td>
                <td className="py-3 text-[#6B7280]">{log.actor}</td>
                <td className="py-3 max-w-sm truncate text-[#6B7280]" title={log.message_preview}>
                  {log.message_preview || "—"}
                </td>
              </tr>
            ))}
            {logs.length === 0 && (
              <tr>
                <td colSpan="5" className="text-center py-6 text-[#6B7280] font-sans">No audit events logged yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}


// ==========================================
// 7. CONTACT LIST MANAGER
// ==========================================
function WEContactListManager() {
  const [lists, setLists] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState(null); // 'new' or listId

  const [formData, setFormData] = useState({
    name: "",
    description: "",
    contacts: []
  });
  const [bulkText, setBulkText] = useState("");

  const fetchLists = async () => {
    try {
      const data = await getContactLists();
      setLists(data);
    } catch (err) {
      toast.error("Failed to load contact lists");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLists();
  }, []);

  const handleEditClick = (lst) => {
    setEditingId(lst.id);
    setFormData({
      name: lst.name,
      description: lst.description || "",
      contacts: lst.contacts || []
    });
    setBulkText("");
  };

  const handleCreateNewClick = () => {
    setEditingId("new");
    setFormData({
      name: "",
      description: "",
      contacts: []
    });
    setBulkText("");
  };

  const handleAddContactRow = () => {
    setFormData(prev => ({
      ...prev,
      contacts: [...prev.contacts, { name: "", phone: "" }]
    }));
  };

  const handleContactChange = (index, field, val) => {
    const updated = [...formData.contacts];
    updated[index] = { ...updated[index], [field]: val };
    setFormData(prev => ({ ...prev, contacts: updated }));
  };

  const handleRemoveContactRow = (index) => {
    setFormData(prev => ({
      ...prev,
      contacts: prev.contacts.filter((_, idx) => idx !== index)
    }));
  };

  const handleBulkImport = () => {
    if (!bulkText.trim()) {
      toast.error("Bulk import text is empty");
      return;
    }
    const parsed = bulkText.split("\n").map(line => {
      const parts = line.split(",");
      let name = "";
      let phone = "";
      if (parts.length > 1) {
        name = parts[0].trim();
        phone = parts.slice(1).join(",").trim();
      } else {
        phone = parts[0].trim();
      }
      return { name, phone };
    }).filter(c => c.phone);

    if (parsed.length === 0) {
      toast.error("No valid contacts found in bulk import text");
      return;
    }

    // De-duplicate and normalize locally
    const normRe = /^\+?\d{7,15}$/;
    const addedContacts = [];
    const seen = new Set(formData.contacts.map(c => c.phone.replace(/\D/g, "")));

    parsed.forEach(c => {
      const rawPhone = c.phone.trim();
      const plus = rawPhone.startsWith("+");
      const digits = rawPhone.replace(/\D/g, "");
      const normPhone = plus ? "+" + digits : digits;

      if (!digits || !normRe.test(normPhone)) {
        return;
      }
      if (seen.has(digits)) {
        return;
      }
      seen.add(digits);
      addedContacts.push({ name: c.name, phone: normPhone });
    });

    if (addedContacts.length === 0) {
      toast.info("No new unique valid contacts were found");
      return;
    }

    setFormData(prev => ({
      ...prev,
      contacts: [...prev.contacts, ...addedContacts]
    }));
    setBulkText("");
    toast.success(`Imported ${addedContacts.length} contacts successfully!`);
  };

  const handleSave = async (e) => {
    e.preventDefault();
    if (!formData.name.trim()) {
      toast.error("List name is required");
      return;
    }
    const validContacts = formData.contacts.filter(c => c.phone.trim());

    try {
      if (editingId === "new") {
        await createContactList({
          name: formData.name.trim(),
          description: formData.description.trim(),
          contacts: validContacts
        });
        toast.success("Contact list created successfully");
      } else {
        await updateContactList(editingId, {
          name: formData.name.trim(),
          description: formData.description.trim(),
          contacts: validContacts
        });
        toast.success("Contact list updated successfully");
      }
      setEditingId(null);
      fetchLists();
    } catch (err) {
      toast.error(formatErrorDetail(err, "Failed to save contact list"));
    }
  };

  const handleDelete = async () => {
    if (!window.confirm("Are you sure you want to delete this contact list?")) return;
    try {
      await deleteContactList(editingId);
      toast.success("Contact list deleted successfully");
      setEditingId(null);
      fetchLists();
    } catch (err) {
      toast.error("Failed to delete contact list");
    }
  };

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-8 items-start">
      {/* Sidebar List */}
      <div className="bg-white p-8 rounded-2xl shadow-sm space-y-6">
        <div className="flex justify-between items-center border-b border-black/[0.04] pb-4">
          <div>
            <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Saved Lists</h3>
            <p className="text-[11px] text-[#6B7280] mt-0.5">Manage reusable recipient contacts</p>
          </div>
          <button
            onClick={handleCreateNewClick}
            className="flex items-center justify-center w-8 h-8 rounded-full bg-black text-white hover:opacity-90 transition-opacity active:scale-95"
            title="Create Contact List"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>

        {loading ? (
          <div className="flex justify-center p-8"><RefreshCw className="w-5 h-5 animate-spin text-black/50" /></div>
        ) : lists.length === 0 ? (
          <div className="text-center py-12 border border-dashed border-black/10 rounded-xl space-y-2">
            <Database className="w-6 h-6 mx-auto text-black/20" />
            <p className="text-xs text-[#6B7280]">No contact lists created yet.</p>
          </div>
        ) : (
          <div className="space-y-3 max-h-[600px] overflow-y-auto pr-1">
            {lists.map(lst => (
              <div
                key={lst.id}
                onClick={() => handleEditClick(lst)}
                className={`p-5 rounded-xl border cursor-pointer transition-all duration-150 space-y-1.5 select-none ${
                  editingId === lst.id 
                    ? "bg-[#f8f8f7] border-black shadow-sm" 
                    : "bg-white border-black/[0.06] hover:border-black/25"
                }`}
              >
                <div className="flex justify-between items-center">
                  <h4 className="font-semibold text-xs text-[#111111]">{lst.name}</h4>
                  <span className="text-[10px] font-medium bg-black/[0.04] px-2 py-0.5 rounded-full text-black/60 border border-black/[0.02]">
                    {lst.contacts?.length || 0} contacts
                  </span>
                </div>
                {lst.description && (
                  <p className="text-[11px] text-[#6B7280] line-clamp-1">{lst.description}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Editor Panel */}
      <div className="xl:col-span-2 bg-white p-8 rounded-2xl shadow-sm min-h-[450px] flex flex-col">
        {editingId ? (
          <form onSubmit={handleSave} className="space-y-6 flex-1 flex flex-col justify-between">
            <div className="space-y-6">
              <div className="flex justify-between items-center border-b border-black/[0.04] pb-3">
                <h3 className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">
                  {editingId === "new" ? "Create Contact List" : "Edit Contact List"}
                </h3>
                {editingId !== "new" && (
                  <button
                    type="button"
                    onClick={handleDelete}
                    className="text-[10px] font-bold uppercase tracking-wider text-red-500 hover:text-red-600 flex items-center gap-1"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Delete List
                  </button>
                )}
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="space-y-2">
                  <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">List Name</label>
                  <input
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    placeholder="e.g. Mumbai Casting Directors"
                    className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
                    required
                  />
                </div>

                <div className="space-y-2">
                  <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">Description</label>
                  <input
                    type="text"
                    value={formData.description}
                    onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                    placeholder="Optional description of this list..."
                    className="w-full text-sm bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black h-[56px]"
                  />
                </div>
              </div>

              {/* Contacts Table */}
              <div className="space-y-3 pt-4 border-t border-black/[0.04]">
                <div className="flex justify-between items-center">
                  <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">
                    Contacts ({formData.contacts.length})
                  </label>
                  <button
                    type="button"
                    onClick={handleAddContactRow}
                    className="text-[10px] font-bold uppercase tracking-wider text-black border border-black/10 hover:border-black bg-[#f8f8f7] px-3 py-1.5 rounded-lg flex items-center gap-1 transition-colors"
                  >
                    <Plus className="w-3 h-3" /> Add Contact Row
                  </button>
                </div>

                <div className="border border-black/[0.06] rounded-xl overflow-hidden shadow-sm bg-white max-h-[300px] overflow-y-auto scrollbar-thin">
                  <table className="w-full text-left text-xs border-collapse">
                    <thead>
                      <tr className="bg-[#f8f8f7] border-b border-black/[0.04] text-[#6B7280] font-bold uppercase tracking-wider select-none">
                        <th className="px-5 py-3.5 w-1/2">Name</th>
                        <th className="px-5 py-3.5 w-5/12">Phone Number</th>
                        <th className="px-5 py-3.5 text-center w-1/12">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {formData.contacts.map((c, index) => (
                        <tr key={index} className="border-b border-black/[0.03] last:border-b-0 hover:bg-black/[0.01]">
                          <td className="px-5 py-2.5">
                            <input
                              type="text"
                              value={c.name}
                              onChange={(e) => handleContactChange(index, "name", e.target.value)}
                              placeholder="e.g. John Doe"
                              className="w-full bg-[#f8f8f7] border border-black/5 focus:border-black focus:outline-none rounded px-2.5 py-1.5 text-xs transition-colors"
                            />
                          </td>
                          <td className="px-5 py-2.5">
                            <input
                              type="text"
                              value={c.phone}
                              onChange={(e) => handleContactChange(index, "phone", e.target.value)}
                              placeholder="e.g. +919876543210"
                              className="w-full bg-[#f8f8f7] border border-black/5 focus:border-black focus:outline-none rounded px-2.5 py-1.5 text-xs font-mono transition-colors"
                            />
                          </td>
                          <td className="px-5 py-2.5 text-center">
                            <button
                              type="button"
                              onClick={() => handleRemoveContactRow(index)}
                              className="text-black/40 hover:text-red-500 p-1.5 transition-colors"
                              title="Delete Row"
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </td>
                        </tr>
                      ))}
                      {formData.contacts.length === 0 && (
                        <tr>
                          <td colSpan={3} className="px-5 py-8 text-center text-[#6B7280]">
                            No contacts added yet. Use the bulk import below or add a contact row manually.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Bulk Import */}
              <div className="space-y-3 pt-4 border-t border-black/[0.04]">
                <label className="text-xs font-bold uppercase tracking-widest text-[#6B7280]">
                  Bulk Import Contacts (Name, Phone per line)
                </label>
                <div className="flex gap-4 items-end">
                  <textarea
                    value={bulkText}
                    onChange={(e) => setBulkText(e.target.value)}
                    rows={3}
                    placeholder={"Casting Director A,+919999999999\n+918888888888"}
                    className="flex-1 text-xs font-mono bg-[#f8f8f7] border border-black/10 rounded-lg p-3.5 focus:outline-none focus:ring-1 focus:ring-black transition-all"
                  />
                  <button
                    type="button"
                    onClick={handleBulkImport}
                    className="bg-black text-white hover:opacity-90 select-none text-xs font-semibold px-4 py-3.5 h-[56px] rounded-lg transition-opacity active:scale-[0.98]"
                  >
                    Process Import
                  </button>
                </div>
              </div>
            </div>

            <div className="flex justify-end gap-3 pt-8 border-t border-black/[0.04] mt-8">
              <button
                type="button"
                onClick={() => setEditingId(null)}
                className="text-xs font-semibold uppercase tracking-widest border border-black/10 hover:border-black bg-white text-black px-6 py-3.5 rounded-lg active:scale-[0.98] transition-all"
              >
                Cancel
              </button>
              <button
                type="submit"
                className="flex items-center justify-center gap-1.5 text-xs font-semibold uppercase tracking-widest bg-black text-white px-6 py-3.5 rounded-lg hover:opacity-90 transition-colors active:scale-[0.98] duration-120 h-[52px]"
              >
                <Save className="w-4 h-4" /> Save List
              </button>
            </div>
          </form>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-center space-y-3 border border-dashed border-black/10 rounded-2xl p-8">
            <div className="w-12 h-12 rounded-full bg-[#f8f8f7] flex items-center justify-center border border-black/[0.04]">
              <Database className="w-6 h-6 text-black/30" />
            </div>
            <div>
              <h4 className="font-semibold text-sm">No List Selected</h4>
              <p className="text-xs text-[#6B7280] max-w-xs mt-1">
                Select a contact list from the sidebar to view details, or click create icon to add a new reusable list.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
