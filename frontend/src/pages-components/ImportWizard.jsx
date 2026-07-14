import React, { useState, useEffect } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { 
    Upload, FileText, ArrowRight, ArrowLeft, CheckCircle2, 
    AlertTriangle, XCircle, RotateCcw, Play, RefreshCw, Layers,
    Save, BookOpen, Database, BarChart2, Check, HelpCircle,
    Settings, List, Plus, Trash2
} from "lucide-react";

const MONGO_FIELDS = [
    { key: "name", label: "Full Name", required: true },
    { key: "email", label: "Email Address", required: false },
    { key: "phone", label: "Phone (WhatsApp)", required: true },
    { key: "alternate_contact_number", label: "Alternate Phone", required: false },
    { key: "age", label: "Age", required: false },
    { key: "dob", label: "Date of Birth", required: false },
    { key: "gender", label: "Gender", required: false },
    { key: "height", label: "Height", required: false },
    { key: "location", label: "Location", required: false },
    { key: "ethnicity", label: "Ethnicity", required: false },
    { key: "instagram_handle", label: "Instagram Handle", required: false },
    { key: "instagram_followers", label: "Instagram Followers", required: false },
    { key: "bio", label: "Bio", required: false },
    { key: "work_links", label: "Work Links", required: false },
    { key: "skills", label: "Skills / Abilities", required: false },
    { key: "tags", label: "Tags", required: false }
];

export default function ImportWizard() {
    // Navigation/Tab state: "ingest" or "rules" or "media_config"
    const [activeTab, setActiveTab] = useState("ingest");
    
    // Stepper State
    const [step, setStep] = useState(1);
    const [loading, setLoading] = useState(false);
    
    // File upload data
    const [fileMeta, setFileMeta] = useState(null);
    const [rawRows, setRawRows] = useState([]);
    const [headers, setHeaders] = useState([]);
    const [fileChecksum, setFileChecksum] = useState("");
    const [isDuplicateFile, setIsDuplicateFile] = useState(false);
    const [dupFilename, setDupFilename] = useState("");
    const [forceUpload, setForceUpload] = useState(false);
    
    // Mapping Configuration & Presets
    const [fieldMapping, setFieldMapping] = useState({});
    const [presets, setPresets] = useState([]);
    const [selectedPreset, setSelectedPreset] = useState("");
    const [newPresetName, setNewPresetName] = useState("");
    
    // Preview Options
    const [previewData, setPreviewData] = useState([]);
    const [selectedPreviewIndex, setSelectedPreviewIndex] = useState(0);
    
    // Validation / Duplicates Check
    const [validationReport, setValidationReport] = useState(null);
    const [dupActions, setDupActions] = useState({});
    const [globalDupAction, setGlobalDupAction] = useState("skip");
    
    // Import Session Status Polling
    const [activeSessionId, setActiveSessionId] = useState(null);
    const [sessionProgress, setSessionProgress] = useState(null);
    const [importHistory, setImportHistory] = useState([]);
    
    // Rules Manager state
    const [labelRules, setLabelRules] = useState([]);
    const [newRuleField, setNewRuleField] = useState("location");
    const [newRuleOp, setNewRuleOp] = useState("city_equals");
    const [newRuleVal, setNewRuleVal] = useState("");
    const [newRuleLabel, setNewRuleLabel] = useState("");
    
    // Media configuration state
    const [maxSizeBytes, setMaxSizeBytes] = useState(200 * 1024 * 1024);
    const [allowedMimeTypes, setAllowedMimeTypes] = useState(["image/", "video/", "application/pdf"]);
    
    // Pagination / Virtual Table state
    const [currentPage, setCurrentPage] = useState(1);
    const rowsPerPage = 20;

    // Load presets, history, rules, and configs on mount
    useEffect(() => {
        loadPresets();
        loadHistory();
        loadLabelRules();
        loadMediaConfig();
    }, []);

    // Load state from localStorage on mount
    useEffect(() => {
        const saved = localStorage.getItem("talentgram_import_wizard_state");
        if (saved) {
            try {
                const parsed = JSON.parse(saved);
                // Guard against a stale/out-of-range persisted step (e.g. from
                // an earlier iteration of this wizard). An invalid value here
                // matches none of the `step === N` render blocks below, which
                // leaves the stepper header visible but the entire step body
                // blank — clamp to the valid 1-5 range, defaulting to Step 1.
                const restoredStep = Number(parsed.step);
                setStep(restoredStep >= 1 && restoredStep <= 5 ? restoredStep : 1);
                setFileMeta(parsed.fileMeta || null);
                setRawRows(parsed.rawRows || []);
                setHeaders(parsed.headers || []);
                setFieldMapping(parsed.fieldMapping || {});
                setPreviewData(parsed.previewData || []);
                setValidationReport(parsed.validationReport || null);
                setDupActions(parsed.dupActions || {});
                setGlobalDupAction(parsed.globalDupAction || "skip");
                setActiveSessionId(parsed.activeSessionId || null);
            } catch (e) {
                console.error("Failed to load import wizard state", e);
            }
        }
    }, []);

    // Save state to localStorage on changes
    useEffect(() => {
        const state = {
            step, fileMeta, rawRows, headers, fieldMapping, 
            previewData, validationReport, dupActions, globalDupAction, activeSessionId
        };
        localStorage.setItem("talentgram_import_wizard_state", JSON.stringify(state));
    }, [step, fileMeta, rawRows, headers, fieldMapping, previewData, validationReport, dupActions, globalDupAction, activeSessionId]);

    // Active session progress polling
    useEffect(() => {
        if (!activeSessionId) return;
        
        const poll = async () => {
            try {
                const { data } = await adminApi.get(`/admin/imports/sessions/${activeSessionId}`);
                setSessionProgress(data);
                
                if (data.status === "completed" || data.status === "failed") {
                    setActiveSessionId(null);
                    toast.success("Import background job completed!");
                    loadHistory();
                    setStep(5);
                }
            } catch (err) {
                console.error("Failed to poll session progress", err);
            }
        };
        
        poll();
        const timer = setInterval(poll, 1500);
        return () => clearInterval(timer);
    }, [activeSessionId]);

    const loadPresets = async () => {
        try {
            const { data } = await adminApi.get("/admin/imports/presets");
            setPresets(data);
        } catch (err) {
            console.error("Failed to load presets", err);
        }
    };

    const loadHistory = async () => {
        try {
            const { data } = await adminApi.get("/admin/imports/history");
            setImportHistory(data);
        } catch (err) {
            console.error("Failed to load history", err);
        }
    };

    const loadLabelRules = async () => {
        try {
            const { data } = await adminApi.get("/admin/imports/label-rules");
            setLabelRules(data);
        } catch (err) {
            console.error("Failed to load label rules", err);
        }
    };

    const loadMediaConfig = async () => {
        try {
            const { data } = await adminApi.get("/admin/imports/media-config");
            setMaxSizeBytes(data.max_size_bytes);
            setAllowedMimeTypes(data.allowed_mime_types);
        } catch (err) {
            console.error("Failed to load media config", err);
        }
    };

    const handleSavePreset = async () => {
        if (!newPresetName) return toast.warning("Please enter a preset name");
        try {
            await adminApi.post("/admin/imports/presets", {
                name: newPresetName,
                field_mapping: fieldMapping
            });
            toast.success("Preset saved successfully");
            setNewPresetName("");
            loadPresets();
        } catch (err) {
            toast.error("Failed to save preset");
        }
    };

    const handleApplyPreset = (presetId) => {
        const preset = presets.find(p => p.id === presetId);
        if (preset) {
            setFieldMapping(preset.field_mapping);
            toast.info(`Applied preset: ${preset.name}`);
        }
    };

    const handleAddRule = async () => {
        if (!newRuleVal || !newRuleLabel) return toast.warning("Please fill all rule fields");
        try {
            await adminApi.post("/admin/imports/label-rules", {
                field: newRuleField,
                operator: newRuleOp,
                value: newRuleVal,
                label: newRuleLabel
            });
            toast.success("Auto-label rule created successfully");
            setNewRuleVal("");
            setNewRuleLabel("");
            loadLabelRules();
        } catch (err) {
            toast.error("Failed to create rule");
        }
    };

    const handleDeleteRule = async (id) => {
        try {
            await adminApi.delete(`/admin/imports/label-rules/${id}`);
            toast.success("Rule deleted");
            loadLabelRules();
        } catch (err) {
            toast.error("Failed to delete rule");
        }
    };

    const handleUpdateMediaConfig = async () => {
        try {
            await adminApi.post("/admin/imports/media-config", {
                max_size_bytes: maxSizeBytes,
                allowed_mime_types: allowedMimeTypes
            });
            toast.success("Media validation config updated successfully");
            loadMediaConfig();
        } catch (err) {
            toast.error("Failed to update media config");
        }
    };

    const resetWizard = () => {
        localStorage.removeItem("talentgram_import_wizard_state");
        setStep(1);
        setFileMeta(null);
        setRawRows([]);
        setHeaders([]);
        setFieldMapping({});
        setPreviewData([]);
        setValidationReport(null);
        setDupActions({});
        setGlobalDupAction("skip");
        setActiveSessionId(null);
        setSessionProgress(null);
        setFileChecksum("");
        setIsDuplicateFile(false);
        setForceUpload(false);
        setCurrentPage(1);
        toast.info("Import session reset");
    };

    // Step 1: Upload CSV/XLSX
    const handleFileUpload = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        
        setLoading(true);
        const formData = new FormData();
        formData.append("file", file);
        
        try {
            const { data } = await adminApi.post("/admin/imports/upload", formData, {
                headers: { "Content-Type": "multipart/form-data" }
            });
            
            setFileMeta({
                filename: data.filename,
                size: data.size,
                row_count: data.row_count
            });
            setHeaders(data.headers);
            setRawRows(data.raw_rows);
            setFieldMapping(data.detected_mapping || {});
            setFileChecksum(data.file_checksum);
            setIsDuplicateFile(data.is_duplicate_file);
            setDupFilename(data.duplicate_filename || "");
            
            if (data.is_duplicate_file) {
                toast.warning(`Warning: A file with identical content was previously imported!`);
            } else {
                toast.success("File uploaded and hashed successfully");
                setStep(2);
            }
        } catch (err) {
            toast.error(err.response?.data?.detail || "Upload failed");
        } finally {
            setLoading(false);
        }
    };

    // Step 2: Mapping
    const handleMapField = (mongoField, header) => {
        setFieldMapping(prev => ({
            ...prev,
            [mongoField]: header || null
        }));
    };

    // Generate Preview
    const loadPreview = async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.post("/admin/imports/preview", {
                raw_rows: rawRows.slice(0, 100),
                field_mapping: fieldMapping
            });
            setPreviewData(data);
            setSelectedPreviewIndex(0);
            setStep(3);
        } catch (err) {
            toast.error("Failed to generate transformation preview");
        } finally {
            setLoading(false);
        }
    };

    // Step 3: Run Validation & Duplicate check
    const runValidation = async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.post("/admin/imports/validate", {
                raw_rows: rawRows,
                field_mapping: fieldMapping
            });
            setValidationReport(data);
            
            const actions = {};
            data.duplicates.forEach(d => {
                actions[d.duplicate.existing_talent_id] = globalDupAction;
            });
            setDupActions(actions);
            
            setStep(4);
        } catch (err) {
            toast.error("Validation failed");
        } finally {
            setLoading(false);
        }
    };

    const handleDuplicateActionChange = (id, action) => {
        setDupActions(prev => ({
            ...prev,
            [id]: action
        }));
    };

    const applyGlobalDuplicateAction = (action) => {
        setGlobalDupAction(action);
        const updated = {};
        validationReport.duplicates.forEach(d => {
            updated[d.duplicate.existing_talent_id] = action;
        });
        setDupActions(updated);
    };

    // Run actual Ingestion import
    const executeImport = async () => {
        setLoading(true);
        try {
            const allRecords = [
                ...validationReport.valid,
                ...validationReport.warnings,
                ...validationReport.duplicates
            ];
            
            const { data } = await adminApi.post("/admin/imports/import", {
                filename: fileMeta.filename,
                records: allRecords,
                dup_actions: dupActions,
                file_checksum: fileChecksum
            });
            
            setActiveSessionId(data.import_id);
            toast.success("Background worker pipeline claimed this import!");
        } catch (err) {
            toast.error(err.response?.data?.detail || "Ingestion pipeline locked or execution failed");
        } finally {
            setLoading(false);
        }
    };

    // Rollback Import
    const handleRollback = async (importId) => {
        if (!window.confirm("Are you sure you want to rollback this import? This will restore updated talents and delete new ones!")) return;
        
        setLoading(true);
        try {
            const { data } = await adminApi.post(`/admin/imports/${importId}/rollback`);
            toast.success(data.message);
            loadHistory();
            resetWizard();
        } catch (err) {
            toast.error("Rollback failed");
        } finally {
            setLoading(false);
        }
    };

    const getPaginatedItems = (items) => {
        const start = (currentPage - 1) * rowsPerPage;
        return items.slice(start, start + rowsPerPage);
    };

    return (
        <div className="p-6 max-w-7xl mx-auto space-y-6 font-sans">
            {/* Header Tabs */}
            <div className="flex items-center justify-between border-b border-black/[0.06] pb-4">
                <div>
                    <h1 className="text-xl font-bold tracking-tight text-neutral-900 font-Outfit">Data Hub</h1>
                    <p className="text-xs text-neutral-500 font-mono mt-1">Multi-source pipeline dashboard, auto-label rules, and media configuration console.</p>
                </div>
                <div className="flex items-center gap-2">
                    <button 
                        onClick={() => setActiveTab("ingest")}
                        className={`px-3 py-1.5 text-xs font-bold rounded-lg border transition-all inline-flex items-center gap-1.5 ${
                            activeTab === "ingest" ? "bg-black text-white border-black" : "bg-white text-neutral-600 border-black/[0.08] hover:bg-neutral-50"
                        }`}
                    >
                        <Layers className="w-3.5 h-3.5" />
                        Pipelines
                    </button>
                    <button 
                        onClick={() => setActiveTab("rules")}
                        className={`px-3 py-1.5 text-xs font-bold rounded-lg border transition-all inline-flex items-center gap-1.5 ${
                            activeTab === "rules" ? "bg-black text-white border-black" : "bg-white text-neutral-600 border-black/[0.08] hover:bg-neutral-50"
                        }`}
                    >
                        <List className="w-3.5 h-3.5" />
                        Auto-Label Rules
                    </button>
                    <button 
                        onClick={() => setActiveTab("config")}
                        className={`px-3 py-1.5 text-xs font-bold rounded-lg border transition-all inline-flex items-center gap-1.5 ${
                            activeTab === "config" ? "bg-black text-white border-black" : "bg-white text-neutral-600 border-black/[0.08] hover:bg-neutral-50"
                        }`}
                    >
                        <Settings className="w-3.5 h-3.5" />
                        Validators Settings
                    </button>
                </div>
            </div>

            {/* Ingestion Pipelines Tab */}
            {activeTab === "ingest" && (
                <div className="space-y-6">
                    {/* Stepper Header */}
                    <div className="flex items-center justify-between bg-neutral-50 p-4 border border-black/[0.04] rounded-xl overflow-x-auto gap-4">
                        {[
                            { s: 1, label: "Upload File" },
                            { s: 2, label: "Schema Mapping" },
                            { s: 3, label: "JSON preview" },
                            { s: 4, label: "Conflicts & Rules" },
                            { s: 5, label: "Ingest Summary" }
                        ].map((item) => (
                            <div key={item.s} className="flex items-center gap-2 whitespace-nowrap">
                                <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold ${
                                    step >= item.s ? "bg-black text-white" : "bg-neutral-200 text-neutral-500"
                                }`}>
                                    {item.s}
                                </span>
                                <span className={`text-xs font-semibold ${
                                    step === item.s ? "text-neutral-900" : "text-neutral-500"
                                }`}>
                                    {item.label}
                                </span>
                                {item.s < 5 && <span className="text-neutral-300">/</span>}
                            </div>
                        ))}
                    </div>

                    {/* Step 1: Upload View */}
                    {step === 1 && (
                        <div className="space-y-6">
                            <div className="bg-white border border-black/[0.06] rounded-xl p-8 text-center max-w-xl mx-auto shadow-sm space-y-4">
                                {isDuplicateFile && !forceUpload ? (
                                    <div className="border border-amber-100 bg-amber-50 p-6 rounded-lg text-center space-y-4">
                                        <AlertTriangle className="w-10 h-10 text-amber-500 mx-auto" />
                                        <h3 className="text-sm font-bold text-amber-900">Duplicate Checksum Detected!</h3>
                                        <p className="text-xs text-amber-700/80 leading-relaxed">
                                            A file matching this exact content hash was already imported under session/filename <strong>"{dupFilename}"</strong>.
                                        </p>
                                        <div className="flex items-center justify-center gap-2">
                                            <button 
                                                onClick={resetWizard}
                                                className="px-3 py-1.5 bg-white border border-amber-200 text-neutral-700 text-xs font-semibold rounded-lg hover:bg-neutral-50"
                                            >
                                                Abort
                                            </button>
                                            <button 
                                                onClick={() => {
                                                    setForceUpload(true);
                                                    setStep(2);
                                                }}
                                                className="px-3 py-1.5 bg-amber-600 text-white text-xs font-bold rounded-lg hover:bg-amber-700"
                                            >
                                                Force Re-Import
                                            </button>
                                        </div>
                                    </div>
                                ) : (
                                    <div className="flex flex-col items-center justify-center border-2 border-dashed border-neutral-200 rounded-lg p-10 hover:border-black/30 transition-colors bg-[#fafaf9]/30">
                                        <Upload className="w-8 h-8 text-neutral-400 mb-4" />
                                        <h3 className="text-sm font-semibold text-neutral-900">Upload CSV or XLSX file</h3>
                                        <p className="text-[11px] text-neutral-400 mt-1 mb-6 font-mono">Max size 10MB. File SHA256 checksum is computed.</p>
                                        
                                        <label className="inline-flex items-center justify-center px-4 py-2 bg-black hover:bg-black/90 text-white text-xs font-semibold rounded-lg shadow-sm cursor-pointer transition-all">
                                            {loading ? <RefreshCw className="w-3.5 h-3.5 animate-spin mr-1.5" /> : null}
                                            Select Spreadsheet File
                                            <input 
                                                type="file" 
                                                accept=".csv,.xlsx" 
                                                onChange={handleFileUpload} 
                                                className="hidden" 
                                                disabled={loading}
                                            />
                                        </label>
                                    </div>
                                )}
                            </div>

                            {/* Past Imports History */}
                            {importHistory.length > 0 && (
                                <div className="bg-white border border-black/[0.06] rounded-xl p-6 shadow-sm space-y-4">
                                    <div className="flex items-center justify-between border-b border-black/[0.04] pb-2">
                                        <h3 className="text-xs font-bold uppercase tracking-wider text-neutral-500 flex items-center gap-1.5">
                                            <Database className="w-3.5 h-3.5" />
                                            Data Hub History & Rollbacks
                                        </h3>
                                    </div>
                                    <div className="overflow-x-auto">
                                        <table className="w-full text-left text-xs">
                                            <thead>
                                                <tr className="border-b border-black/[0.04] text-neutral-400">
                                                    <th className="py-2 font-semibold">Date</th>
                                                    <th className="py-2 font-semibold">File</th>
                                                    <th className="py-2 font-semibold">Imported</th>
                                                    <th className="py-2 font-semibold">Updated</th>
                                                    <th className="py-2 font-semibold">Status</th>
                                                    <th className="py-2 font-semibold text-right">Actions</th>
                                                </tr>
                                            </thead>
                                            <tbody className="divide-y divide-black/[0.02]">
                                                {importHistory.map((h) => (
                                                    <tr key={h.id} className="hover:bg-neutral-50/50">
                                                        <td className="py-2 text-neutral-500">{new Date(h.created_at).toLocaleDateString()}</td>
                                                        <td className="py-2 font-semibold">{h.filename}</td>
                                                        <td className="py-2 text-emerald-600 font-bold">{h.imported}</td>
                                                        <td className="py-2 text-indigo-600 font-bold">{h.updated}</td>
                                                        <td className="py-2">
                                                            <span className={`px-2 py-0.5 rounded-full text-[9px] font-bold uppercase ${
                                                                h.status === "completed" ? "bg-emerald-50 text-emerald-700" :
                                                                h.status === "rolled_back" ? "bg-amber-50 text-amber-700" : "bg-red-50 text-red-700"
                                                            }`}>
                                                                {h.status}
                                                            </span>
                                                        </td>
                                                        <td className="py-2 text-right">
                                                            {h.status === "completed" && (
                                                                <button 
                                                                    onClick={() => handleRollback(h.id)}
                                                                    className="text-[10px] font-bold text-red-600 hover:underline inline-flex items-center gap-1"
                                                                >
                                                                    <RotateCcw className="w-2.5 h-2.5" />
                                                                    Rollback State
                                                                </button>
                                                            )}
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 2: Mapping View */}
                    {step === 2 && (
                        <div className="bg-white border border-black/[0.06] rounded-xl p-6 shadow-sm space-y-6">
                            <div className="flex items-center justify-between border-b border-black/[0.04] pb-3">
                                <div>
                                    <h2 className="text-base font-bold text-neutral-900">Schema Mapping</h2>
                                    <p className="text-xs text-neutral-400 font-mono mt-1">Select friendly sheet headers to map directly to model schema.</p>
                                </div>
                                <div className="flex items-center gap-2">
                                    <select 
                                        value={selectedPreset}
                                        onChange={(e) => {
                                            setSelectedPreset(e.target.value);
                                            handleApplyPreset(e.target.value);
                                        }}
                                        className="text-xs border border-black/[0.08] rounded-md px-2 py-1 bg-white outline-none w-48 font-semibold"
                                    >
                                        <option value="">-- Apply Profile --</option>
                                        {presets.map(p => (
                                            <option key={p.id} value={p.id}>{p.name}</option>
                                        ))}
                                    </select>
                                </div>
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <div className="space-y-4">
                                    <h3 className="text-xs font-bold uppercase tracking-wider text-neutral-500">Database Schema Target</h3>
                                    <div className="space-y-3 max-h-[400px] overflow-y-auto pr-2 border border-black/[0.03] p-3 rounded-lg bg-neutral-50/20">
                                        {MONGO_FIELDS.map((field) => (
                                            <div key={field.key} className="flex items-center justify-between border-b border-black/[0.02] pb-2">
                                                <div className="flex items-center gap-1.5">
                                                    <span className="text-xs font-semibold text-neutral-800">{field.label}</span>
                                                    {field.required && <span className="text-[9px] text-red-500 font-bold bg-red-50 px-1.5 py-0.5 rounded">Required</span>}
                                                </div>
                                                <select 
                                                    value={fieldMapping[field.key] || ""}
                                                    onChange={(e) => handleMapField(field.key, e.target.value)}
                                                    className="text-xs border border-black/[0.08] rounded-md px-2 py-1 bg-white outline-none w-52 focus:border-black/40 font-semibold"
                                                >
                                                    <option value="">-- Ignored --</option>
                                                    {headers.map(h => (
                                                        <option key={h} value={h}>{h}</option>
                                                    ))}
                                                </select>
                                            </div>
                                        ))}
                                    </div>
                                </div>

                                <div className="border-l border-black/[0.06] pl-6 space-y-4 flex flex-col justify-between">
                                    <div className="space-y-3">
                                        <h3 className="text-xs font-bold uppercase tracking-wider text-neutral-500">Save Configuration Preset</h3>
                                        <p className="text-[11px] text-neutral-400">Save this mapping configuration to quick-ingest similar sheets later.</p>
                                        <div className="flex gap-2">
                                            <input 
                                                type="text" 
                                                placeholder="Profile Name (e.g. Casting CD Roster)" 
                                                value={newPresetName}
                                                onChange={(e) => setNewPresetName(e.target.value)}
                                                className="text-xs border border-black/[0.08] rounded-md px-3 py-1.5 flex-1 outline-none focus:border-black/40"
                                            />
                                            <button 
                                                onClick={handleSavePreset}
                                                className="px-3 py-1.5 bg-neutral-900 hover:bg-neutral-800 text-white text-xs font-bold rounded-lg inline-flex items-center gap-1"
                                            >
                                                <Save className="w-3.5 h-3.5" />
                                                Save
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <div className="flex justify-between border-t border-black/[0.06] pt-4">
                                <button onClick={() => setStep(1)} className="px-4 py-2 border border-black/[0.08] text-xs font-bold rounded-lg hover:bg-neutral-50">Back</button>
                                <button 
                                    onClick={loadPreview}
                                    disabled={MONGO_FIELDS.some(f => f.required && !fieldMapping[f.key])}
                                    className="px-4 py-2 bg-black hover:bg-black/90 text-white text-xs font-bold rounded-lg inline-flex items-center gap-1.5 disabled:opacity-50"
                                >
                                    Next: JSON document preview
                                    <ArrowRight className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        </div>
                    )}

                    {/* Step 3: Transformation & JSON Document Preview */}
                    {step === 3 && (
                        <div className="bg-white border border-black/[0.06] rounded-xl p-6 shadow-sm space-y-6">
                            <div>
                                <h2 className="text-base font-bold text-neutral-900">Transformation Engine & JSON Document Preview</h2>
                                <p className="text-xs text-neutral-400 font-mono mt-1">Review the full MongoDB JSON document shape that will be stored in database.</p>
                            </div>

                            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                                <div className="lg:col-span-1 border border-black/[0.06] rounded-lg overflow-y-auto max-h-[400px] divide-y divide-black/[0.03]">
                                    {previewData.map((row, idx) => (
                                        <button 
                                            key={row.row_number}
                                            onClick={() => setSelectedPreviewIndex(idx)}
                                            className={`w-full text-left p-3 text-xs flex items-center justify-between transition-colors ${
                                                selectedPreviewIndex === idx ? "bg-neutral-100 font-bold" : "hover:bg-neutral-50/50"
                                            }`}
                                        >
                                            <span>Row {row.row_number} - {row.transformed.name || "Unnamed"}</span>
                                            <ArrowRight className="w-3.5 h-3.5 text-neutral-400" />
                                        </button>
                                    ))}
                                </div>

                                {previewData[selectedPreviewIndex] && (
                                    <div className="lg:col-span-2 space-y-4">
                                        <div className="border border-black/[0.06] rounded-lg p-4 bg-neutral-50/30">
                                            <h4 className="text-xs font-bold uppercase tracking-wider text-neutral-500 mb-2">Original Row Data</h4>
                                            <div className="grid grid-cols-2 gap-2 text-[11px] font-mono">
                                                {Object.entries(previewData[selectedPreviewIndex].original).map(([k, v]) => (
                                                    <div key={k} className="border-b border-black/[0.02] py-1 truncate">
                                                        <span className="text-neutral-400">{k}:</span> <span className="text-neutral-800 font-bold">{v}</span>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                        <div className="border border-black/[0.06] rounded-lg p-4 bg-[#050505] text-emerald-400 font-mono text-[11px] overflow-x-auto max-h-[300px]">
                                            <div className="flex items-center justify-between border-b border-white/10 pb-2 mb-2 text-[10px] text-white/50 uppercase tracking-wider font-bold">
                                                <span>Constructed MongoDB JSON Document</span>
                                                <Database className="w-3.5 h-3.5 text-white/40" />
                                            </div>
                                            <pre>{JSON.stringify(previewData[selectedPreviewIndex].mongo_document, null, 2)}</pre>
                                        </div>
                                    </div>
                                )}
                            </div>

                            <div className="flex justify-between border-t border-black/[0.06] pt-4">
                                <button onClick={() => setStep(2)} className="px-4 py-2 border border-black/[0.08] text-xs font-bold rounded-lg hover:bg-neutral-50">Back</button>
                                <button 
                                    onClick={runValidation}
                                    className="px-4 py-2 bg-black hover:bg-black/90 text-white text-xs font-bold rounded-lg inline-flex items-center gap-1.5"
                                >
                                    Next: Conflicts & Rules
                                    <ArrowRight className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        </div>
                    )}

                    {/* Step 4: Validation / Duplicates / Execution */}
                    {step === 4 && (
                        <div className="bg-white border border-black/[0.06] rounded-xl p-6 shadow-sm space-y-6">
                            <div>
                                <h2 className="text-base font-bold text-neutral-900">Validation & Duplicate Resolution</h2>
                                <p className="text-xs text-neutral-400 font-mono mt-1">Review validation results and select actions for database collisions.</p>
                            </div>

                            {/* Summary Cards */}
                            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                                {[
                                    { label: "Valid Rows", val: validationReport.valid.length, color: "text-emerald-700 bg-emerald-50 border-emerald-100" },
                                    { label: "Errors (E.g. Broken URLs)", val: validationReport.errors.length, color: "text-red-700 bg-red-50 border-red-100" },
                                    { label: "Warnings", val: validationReport.warnings.length, color: "text-amber-700 bg-amber-50 border-amber-100" },
                                    { label: "Duplicates", val: validationReport.duplicates.length, color: "text-indigo-700 bg-indigo-50 border-indigo-100" },
                                    { label: "Ready to Ingest", val: validationReport.valid.length + validationReport.warnings.length + (validationReport.duplicates.length - Object.values(dupActions).filter(a => a === "skip").length), color: "text-neutral-900 bg-neutral-100 border-neutral-200" }
                                ].map((card, i) => (
                                    <div key={i} className={`p-4 border rounded-xl flex flex-col justify-between ${card.color}`}>
                                        <span className="text-[10px] font-semibold uppercase tracking-wider opacity-85">{card.label}</span>
                                        <span className="text-xl font-bold mt-2">{card.val}</span>
                                    </div>
                                ))}
                            </div>

                            {/* Active Ingest Worker Monitor */}
                            {activeSessionId && sessionProgress && (
                                <div className="border border-neutral-200 bg-neutral-50 rounded-xl p-6 space-y-4 shadow-inner">
                                    <div className="flex items-center justify-between border-b border-black/[0.04] pb-2">
                                        <h3 className="text-xs font-bold uppercase tracking-wider text-neutral-700 flex items-center gap-2">
                                            <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                                            Data Hub Worker Queue Processing...
                                        </h3>
                                        <span className="text-xs font-mono font-bold text-neutral-900">{sessionProgress.processed_rows} / {sessionProgress.total_rows} rows</span>
                                    </div>
                                    
                                    <div className="w-full bg-neutral-200 h-2.5 rounded-full overflow-hidden">
                                        <div 
                                            className="bg-black h-2.5 transition-all duration-300"
                                            style={{ width: `${(sessionProgress.processed_rows / sessionProgress.total_rows) * 100}%` }}
                                        />
                                    </div>

                                    <div className="grid grid-cols-2 gap-4 text-xs font-mono">
                                        <div>Processed Offset: <span className="font-bold">{sessionProgress.processed_rows}</span></div>
                                        <div>Successful Rows: <span className="font-bold text-emerald-600">{sessionProgress.successful_rows}</span></div>
                                    </div>
                                </div>
                            )}

                            {/* Duplicates Settings */}
                            {validationReport.duplicates.length > 0 && (
                                <div className="border border-indigo-100 bg-indigo-50/20 rounded-xl p-4 space-y-3">
                                    <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
                                        <div>
                                            <h3 className="text-xs font-bold text-indigo-900">Bulk Duplicate Resolution Settings</h3>
                                            <p className="text-[10px] text-indigo-700/80 mt-0.5">Apply action preset to all database duplicate collisions.</p>
                                        </div>
                                        <div className="flex flex-wrap gap-1.5">
                                            {[
                                                { key: "skip", label: "Skip" },
                                                { key: "overwrite", label: "Replace All" },
                                                { key: "merge_blanks", label: "Update Blanks" },
                                                { key: "merge_arrays", label: "Merge Arrays" },
                                                { key: "create", label: "Create Anyway" }
                                            ].map(a => (
                                                <button 
                                                    key={a.key}
                                                    onClick={() => applyGlobalDuplicateAction(a.key)}
                                                    className={`px-3 py-1.5 border text-[10px] font-bold rounded-lg transition-colors uppercase ${
                                                        globalDupAction === a.key 
                                                            ? "bg-indigo-600 text-white border-indigo-600 shadow-sm" 
                                                            : "bg-white text-indigo-800 border-indigo-200 hover:bg-indigo-50"
                                                    }`}
                                                >
                                                    {a.label}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* Validation Errors log */}
                            {validationReport.errors.length > 0 && (
                                <div className="space-y-3">
                                    <h3 className="text-xs font-bold uppercase tracking-wider text-red-500">Errors & Broken URLs ({validationReport.errors.length})</h3>
                                    <div className="border border-red-100 rounded-xl overflow-hidden divide-y divide-red-50 bg-red-50/10 max-h-[300px] overflow-y-auto">
                                        {validationReport.errors.map(err => (
                                            <div key={err.row_number} className="p-3 text-xs flex gap-3">
                                                <XCircle className="w-4 h-4 text-red-500 shrink-0" />
                                                <div>
                                                    <div className="font-bold text-red-800">Row {err.row_number} - {err.data.name || "Unnamed"}</div>
                                                    <div className="text-red-700/80 mt-1 font-mono text-[11px]">{JSON.stringify(err.errors)}</div>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            <div className="flex justify-between border-t border-black/[0.06] pt-4">
                                <button onClick={() => setStep(3)} className="px-4 py-2 border border-black/[0.08] text-xs font-bold rounded-lg hover:bg-neutral-50" disabled={!!activeSessionId}>Back</button>
                                <button 
                                    onClick={executeImport}
                                    disabled={!!activeSessionId}
                                    className="px-4 py-2 bg-black hover:bg-black/90 text-white text-xs font-bold rounded-lg inline-flex items-center gap-1.5 shadow-md disabled:opacity-50"
                                >
                                    {loading ? <RefreshCw className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Play className="w-3.5 h-3.5 mr-1" />}
                                    Trigger Resumable Ingest
                                </button>
                            </div>
                        </div>
                    )}

                    {/* Step 5: Summary Report & Rollback View */}
                    {step === 5 && sessionProgress && (
                        <div className="bg-white border border-black/[0.06] rounded-xl p-8 shadow-sm text-center max-w-xl mx-auto space-y-6">
                            <div className="flex flex-col items-center">
                                <CheckCircle2 className="w-12 h-12 text-emerald-500 mb-4 animate-scaleUp" />
                                <h2 className="text-lg font-bold text-neutral-900">Ingestion Session Completed!</h2>
                                <p className="text-xs text-neutral-500 font-mono mt-1">Processed {sessionProgress.processed_rows} rows.</p>
                            </div>

                            <div className="grid grid-cols-2 gap-3 border border-black/[0.04] p-4 rounded-xl bg-[#fafaf9]/20">
                                <div className="text-center p-2 border-r border-black/[0.04]">
                                    <div className="text-[10px] font-bold text-neutral-400 uppercase">Successful Ingests</div>
                                    <div className="text-lg font-bold text-emerald-600 mt-1">{sessionProgress.successful_rows}</div>
                                </div>
                                <div className="text-center p-2">
                                    <div className="text-[10px] font-bold text-neutral-400 uppercase">Failed / Warnings</div>
                                    <div className="text-lg font-bold text-red-600 mt-1">{sessionProgress.failed_rows.length}</div>
                                </div>
                            </div>

                            <div className="pt-4 border-t border-black/[0.06] flex items-center justify-center gap-3">
                                <button 
                                    onClick={() => handleRollback(sessionProgress._id)}
                                    className="inline-flex items-center gap-1.5 px-4 py-2 border border-red-200 text-red-600 hover:bg-red-50 text-xs font-bold rounded-lg transition-colors shadow-sm"
                                >
                                    <RotateCcw className="w-3.5 h-3.5" />
                                    Rollback State
                                </button>
                                <button 
                                    onClick={resetWizard}
                                    className="inline-flex items-center gap-1.5 px-4 py-2 bg-black hover:bg-black/90 text-white text-xs font-bold rounded-lg shadow-md transition-all"
                                >
                                    Ingest Another File
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Auto-Label Rules Tab */}
            {activeTab === "rules" && (
                <div className="bg-white border border-black/[0.06] rounded-xl p-6 shadow-sm space-y-6">
                    <div>
                        <h2 className="text-base font-bold text-neutral-900">Custom Auto-Labeling Rules Engine</h2>
                        <p className="text-xs text-neutral-500 font-mono mt-1">Configure criteria to dynamically apply internal search category tags/labels on ingestion.</p>
                    </div>

                    {/* Rule Creator */}
                    <div className="border border-black/[0.06] rounded-xl p-4 bg-neutral-50/30 grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
                        <div>
                            <label className="block text-[10px] font-bold text-neutral-400 uppercase mb-1.5">Target Field</label>
                            <select 
                                value={newRuleField}
                                onChange={(e) => {
                                    setNewRuleField(e.target.value);
                                    if (e.target.value === "location") setNewRuleOp("city_equals");
                                    else if (e.target.value === "height") setNewRuleOp("height_greater_than");
                                    else setNewRuleOp("equals");
                                }}
                                className="text-xs border border-black/[0.08] rounded-md px-2 py-1.5 bg-white outline-none w-full font-semibold focus:border-black/40"
                            >
                                <option value="location">Location</option>
                                <option value="gender">Gender</option>
                                <option value="height">Height</option>
                                <option value="skills">Skills</option>
                                <option value="ethnicity">Ethnicity</option>
                            </select>
                        </div>
                        <div>
                            <label className="block text-[10px] font-bold text-neutral-400 uppercase mb-1.5">Operator</label>
                            <select 
                                value={newRuleOp}
                                onChange={(e) => setNewRuleOp(e.target.value)}
                                className="text-xs border border-black/[0.08] rounded-md px-2 py-1.5 bg-white outline-none w-full font-semibold focus:border-black/40"
                            >
                                {newRuleField === "location" ? (
                                    <>
                                        <option value="city_equals">City Equals</option>
                                        <option value="country_equals">Country Equals</option>
                                    </>
                                ) : newRuleField === "height" ? (
                                    <option value="height_greater_than">Height Greater Than</option>
                                ) : (
                                    <>
                                        <option value="equals">Equals</option>
                                        <option value="contains">Contains</option>
                                    </>
                                )}
                            </select>
                        </div>
                        <div>
                            <label className="block text-[10px] font-bold text-neutral-400 uppercase mb-1.5">Rule Value</label>
                            <input 
                                type="text" 
                                placeholder={"E.g. Mumbai or 5'8\""}
                                value={newRuleVal}
                                onChange={(e) => setNewRuleVal(e.target.value)}
                                className="text-xs border border-black/[0.08] rounded-md px-3 py-1.5 w-full outline-none focus:border-black/40"
                            />
                        </div>
                        <div>
                            <label className="block text-[10px] font-bold text-neutral-400 uppercase mb-1.5">Tag/Label to Apply</label>
                            <input 
                                type="text" 
                                placeholder="E.g. Tall or Mumbai"
                                value={newRuleLabel}
                                onChange={(e) => setNewRuleLabel(e.target.value)}
                                className="text-xs border border-black/[0.08] rounded-md px-3 py-1.5 w-full outline-none focus:border-black/40 font-bold text-neutral-800"
                            />
                        </div>
                        <button 
                            onClick={handleAddRule}
                            className="bg-black hover:bg-black/90 text-white text-xs font-bold rounded-lg py-2 px-4 inline-flex items-center justify-center gap-1 h-9 shadow-sm"
                        >
                            <Plus className="w-3.5 h-3.5" />
                            Add Rule
                        </button>
                    </div>

                    {/* Rules List */}
                    <div className="space-y-2">
                        <h3 className="text-xs font-bold uppercase tracking-wider text-neutral-500">Active Rules</h3>
                        <div className="border border-black/[0.06] rounded-xl overflow-hidden divide-y divide-black/[0.04]">
                            {labelRules.length === 0 ? (
                                <div className="p-6 text-center text-xs text-neutral-400 font-mono">No auto-labeling rules configured. Default rules will be seeded.</div>
                            ) : (
                                labelRules.map((rule) => (
                                    <div key={rule.id} className="p-3 text-xs flex justify-between items-center bg-[#fafaf9]/20 hover:bg-neutral-50/50 transition-colors">
                                        <div className="flex items-center gap-2">
                                            <span className="font-bold text-neutral-700 capitalize font-mono">{rule.field}</span>
                                            <span className="text-[10px] bg-neutral-100 text-neutral-500 px-1.5 py-0.5 rounded font-bold uppercase">{rule.operator.replace("_", " ")}</span>
                                            <span className="font-semibold text-neutral-900">"{rule.value}"</span>
                                            <span className="text-neutral-300">→</span>
                                            <span className="font-bold text-indigo-700 bg-indigo-50 border border-indigo-100 px-2 py-0.5 rounded-full">{rule.label}</span>
                                        </div>
                                        <button 
                                            onClick={() => handleDeleteRule(rule.id)}
                                            className="text-red-500 hover:text-red-700 p-1 rounded-md hover:bg-red-50 transition-colors"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Validators Settings Tab */}
            {activeTab === "config" && (
                <div className="bg-white border border-black/[0.06] rounded-xl p-6 shadow-sm space-y-6">
                    <div>
                        <h2 className="text-base font-bold text-neutral-900">Validator Configurations Settings</h2>
                        <p className="text-xs text-neutral-500 font-mono mt-1">Configure limits and constraints for mapped files and media validators.</p>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 border-b border-black/[0.04] pb-6">
                        <div className="space-y-2">
                            <label className="block text-xs font-bold text-neutral-700">Maximum Allowed File Size (Bytes)</label>
                            <p className="text-[10px] text-neutral-400">Specify maximum acceptable byte size for media links (e.g. PDFs, images, audition takes).</p>
                            <div className="flex gap-2 items-center">
                                <input 
                                    type="number" 
                                    value={maxSizeBytes}
                                    onChange={(e) => setMaxSizeBytes(parseInt(e.target.value) || 0)}
                                    className="text-xs border border-black/[0.08] rounded-md px-3 py-1.5 w-64 outline-none focus:border-black/40 font-mono font-bold"
                                />
                                <span className="text-xs text-neutral-500">Bytes (~{(maxSizeBytes / (1024*1024)).toFixed(0)} MB)</span>
                            </div>
                        </div>

                        <div className="space-y-2">
                            <label className="block text-xs font-bold text-neutral-700">Allowed Content MIME Types</label>
                            <p className="text-[10px] text-neutral-400">Comma-separated prefixes of allowed file types (e.g. image/, video/).</p>
                            <input 
                                type="text" 
                                value={allowedMimeTypes.join(", ")}
                                onChange={(e) => setAllowedMimeTypes(e.target.value.split(",").map(s => s.trim()))}
                                className="text-xs border border-black/[0.08] rounded-md px-3 py-1.5 w-full outline-none focus:border-black/40 font-mono"
                            />
                        </div>
                    </div>

                    <button 
                        onClick={handleUpdateMediaConfig}
                        className="bg-black hover:bg-black/90 text-white text-xs font-bold rounded-lg py-2 px-4 inline-flex items-center gap-1.5 shadow-sm"
                    >
                        <Save className="w-3.5 h-3.5" />
                        Save Validation Settings
                    </button>
                </div>
            )}
        </div>
    );
}
