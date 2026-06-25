import React, { useCallback, useRef, useState } from "react";
import { adminApi } from "@/lib/api";
import { useNavigate } from "react-router-dom";
import {
    X,
    UploadCloud,
    Loader2,
    Sparkles,
    AlertTriangle,
    CheckCircle2,
    Trash2,
    Image as ImageIcon,
} from "lucide-react";
import { toast } from "sonner";

const ACCEPTED = ["image/png", "image/jpeg", "image/webp"];
const ACCEPT_ATTR = ".png,.jpg,.jpeg,.webp";
const MAX_FILES = 6;

// Field rows shown in the review table, in display order.
const FIELD_ROWS = [
    { key: "full_name", label: "Name" },
    { key: "instagram_username", label: "Instagram Username" },
    { key: "instagram_url", label: "Instagram URL" },
    { key: "phone_number", label: "Phone" },
    { key: "followers_count", label: "Followers" },
    { key: "category", label: "Category" },
    { key: "location", label: "Location" },
    { key: "manager_name", label: "Manager" },
    { key: "manager_phone", label: "Manager Phone" },
    { key: "scouting_notes", label: "Scouting Notes", multiline: true },
];

function bandStyle(band) {
    if (band === "high") return "bg-green-50 text-green-700 border-green-200";
    if (band === "medium") return "bg-amber-50 text-amber-700 border-amber-200";
    return "bg-red-50 text-red-600 border-red-200";
}

export default function ScoutCaptureModal({ onClose, onSaved, onRefresh }) {
    const navigate = useNavigate();
    const fileInputRef = useRef(null);

    const [files, setFiles] = useState([]); // {file, url}
    const [stage, setStage] = useState("upload"); // upload | processing | review | saving
    const [result, setResult] = useState(null); // server response
    const [form, setForm] = useState(null); // editable field values
    const [dragOver, setDragOver] = useState(false);
    const [forceCreate, setForceCreate] = useState(false);

    // -- file selection -----------------------------------------------------
    const addFiles = useCallback(
        (incoming) => {
            const list = Array.from(incoming || []);
            const valid = [];
            for (const f of list) {
                if (!ACCEPTED.includes(f.type) && !/\.(png|jpe?g|webp)$/i.test(f.name)) {
                    toast.error(`${f.name}: only PNG, JPG, JPEG, WEBP allowed`);
                    continue;
                }
                if (f.size > 10 * 1024 * 1024) {
                    toast.error(`${f.name}: exceeds 10 MB`);
                    continue;
                }
                valid.push({ file: f, url: URL.createObjectURL(f) });
            }
            setFiles((prev) => {
                const merged = [...prev, ...valid].slice(0, MAX_FILES);
                if (prev.length + valid.length > MAX_FILES) {
                    toast.error(`At most ${MAX_FILES} screenshots`);
                }
                return merged;
            });
        },
        []
    );

    const removeFile = (idx) => {
        setFiles((prev) => {
            const next = [...prev];
            const [gone] = next.splice(idx, 1);
            if (gone) URL.revokeObjectURL(gone.url);
            return next;
        });
    };

    // -- extraction ---------------------------------------------------------
    const runExtraction = async () => {
        if (!files.length) {
            toast.error("Add at least one screenshot");
            return;
        }
        setStage("processing");
        try {
            const fd = new FormData();
            files.forEach(({ file }) => fd.append("files", file));
            const { data } = await adminApi.post("/workflow/scouting/ai-capture", fd, {
                headers: { "Content-Type": "multipart/form-data" },
                timeout: 60000,
            });
            setResult(data);
            setForm(buildForm(data));
            setForceCreate(false);
            setStage("review");
        } catch (e) {
            const msg = e?.response?.data?.detail || "AI capture failed";
            toast.error(msg);
            setStage("upload");
        }
    };

    // Build editable form, preferring normalized canonical values.
    const buildForm = (data) => {
        const f = data.fields || {};
        const n = data.normalized || {};
        const val = (k) => (f[k]?.value ?? "") || "";
        return {
            full_name: n.full_name || val("full_name"),
            instagram_username: n.instagram_username || val("instagram_username"),
            instagram_url: n.instagram_url || val("instagram_url"),
            phone_number: n.phone_number || val("phone_number"),
            followers_count:
                n.followers_count != null ? String(n.followers_count) : val("followers_count"),
            category: n.category || val("category"),
            location: n.location || val("location"),
            manager_name: n.manager_name || val("manager_name"),
            manager_phone: n.manager_phone || val("manager_phone"),
            scouting_notes: n.scouting_notes || val("scouting_notes"),
        };
    };

    const setField = (key, v) => setForm((p) => ({ ...p, [key]: v }));

    // -- save ---------------------------------------------------------------
    const handleSave = async () => {
        if (!form.instagram_url && !form.instagram_username && !form.phone_number) {
            toast.error("Need at least an Instagram link or phone number to save");
            return;
        }
        setStage("saving");
        try {
            const igUrl =
                form.instagram_url ||
                (form.instagram_username
                    ? `https://www.instagram.com/${form.instagram_username}`
                    : "");
            const followers = parseInt(form.followers_count, 10);
            const payload = {
                instagram_link: igUrl,
                phone: form.phone_number || "",
                name: form.full_name || "",
                notes: form.scouting_notes || "",
                instagram_username: form.instagram_username || null,
                followers_count: Number.isFinite(followers) ? followers : null,
                category: form.category || null,
                location: form.location || null,
                manager_name: form.manager_name || null,
                manager_phone: form.manager_phone || null,
                capture_audit_id: result?.audit_id || null,
            };
            const { data } = await adminApi.post("/workflow/scouting", payload);
            toast.success("Scout logged from AI capture");
            onSaved?.(data);
            onClose();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to save scout");
            setStage("review");
        }
    };

    const openExisting = () => {
        const dup = result?.duplicate;
        if (!dup) return;
        if (dup.source === "talent" && dup.id) {
            navigate(`/admin/talents/${dup.id}`);
            onClose();
        } else {
            toast.info("This profile is already in the scouting queue below.");
            onClose();
        }
    };

    // Merge the captured fields into the existing record. For a canonical talent
    // match we never auto-mutate the source of truth — we send the admin to the
    // talent edit screen instead.
    const updateExisting = async () => {
        const dup = result?.duplicate;
        if (!dup) return;
        if (dup.source === "talent") {
            toast.info("Review and apply changes on the talent profile.");
            navigate(`/admin/talents/${dup.id}`);
            onClose();
            return;
        }
        setStage("saving");
        try {
            const followers = parseInt(form.followers_count, 10);
            const patch = {
                name: form.full_name || undefined,
                phone: form.phone_number || undefined,
                notes: form.scouting_notes || undefined,
                instagram_username: form.instagram_username || undefined,
                followers_count: Number.isFinite(followers) ? followers : undefined,
                category: form.category || undefined,
                location: form.location || undefined,
                manager_name: form.manager_name || undefined,
                manager_phone: form.manager_phone || undefined,
            };
            await adminApi.put(`/workflow/scouting/${dup.id}`, patch);
            toast.success("Existing scout updated");
            onRefresh?.();
            onClose();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to update existing scout");
            setStage("review");
        }
    };

    const dup = result?.duplicate;
    const duplicateBlocksSave = dup && !forceCreate;

    return (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4">
            <div className="bg-white w-full max-w-2xl max-h-[90vh] rounded-lg shadow-xl flex flex-col overflow-hidden">
                {/* Header */}
                <div className="flex items-center justify-between px-5 py-3.5 border-b border-black/[0.06]">
                    <div className="flex items-center gap-2">
                        <Sparkles className="w-4 h-4 text-black/70" />
                        <h2 className="text-sm font-semibold tracking-wide uppercase text-black/85">
                            AI Scout Capture
                        </h2>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1 text-black/40 hover:text-black focus:outline-none"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="overflow-y-auto px-5 py-4 space-y-4">
                    {/* Upload stage */}
                    {(stage === "upload" || stage === "processing") && (
                        <>
                            <div
                                onDragOver={(e) => {
                                    e.preventDefault();
                                    setDragOver(true);
                                }}
                                onDragLeave={() => setDragOver(false)}
                                onDrop={(e) => {
                                    e.preventDefault();
                                    setDragOver(false);
                                    addFiles(e.dataTransfer.files);
                                }}
                                onClick={() => fileInputRef.current?.click()}
                                className={`border-2 border-dashed rounded-md p-6 text-center cursor-pointer transition-colors ${
                                    dragOver ? "border-black/40 bg-black/[0.02]" : "border-black/15"
                                }`}
                            >
                                <UploadCloud className="w-6 h-6 mx-auto text-black/40" />
                                <p className="text-xs text-black/60 mt-2">
                                    Drop screenshots here or click to upload
                                </p>
                                <p className="text-[10px] text-black/35 mt-0.5">
                                    Instagram / WhatsApp / Facebook · PNG, JPG, WEBP · up to {MAX_FILES}
                                </p>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    accept={ACCEPT_ATTR}
                                    multiple
                                    className="hidden"
                                    onChange={(e) => addFiles(e.target.files)}
                                />
                            </div>

                            {files.length > 0 && (
                                <div className="grid grid-cols-3 gap-2">
                                    {files.map((f, i) => (
                                        <div
                                            key={i}
                                            className="relative group border border-black/[0.06] rounded overflow-hidden aspect-[3/4] bg-black/[0.02]"
                                        >
                                            <img
                                                src={f.url}
                                                alt={`shot ${i + 1}`}
                                                className="w-full h-full object-cover"
                                            />
                                            {stage === "upload" && (
                                                <button
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        removeFile(i);
                                                    }}
                                                    className="absolute top-1 right-1 p-0.5 bg-white/90 rounded-sm text-black/60 hover:text-red-500"
                                                >
                                                    <Trash2 className="w-3 h-3" />
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}

                            {stage === "processing" ? (
                                <div className="flex items-center justify-center gap-2 py-3 text-xs text-black/60">
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                    Extracting name, contact & profile details…
                                </div>
                            ) : (
                                <button
                                    onClick={runExtraction}
                                    disabled={!files.length}
                                    className="w-full bg-black text-white py-2 rounded-sm text-xs font-semibold uppercase tracking-wider disabled:opacity-40 focus:outline-none inline-flex items-center justify-center gap-1.5"
                                >
                                    <Sparkles className="w-3.5 h-3.5" />
                                    Extract Details
                                </button>
                            )}
                        </>
                    )}

                    {/* Review stage */}
                    {(stage === "review" || stage === "saving") && form && (
                        <>
                            {/* Duplicate banner */}
                            {dup && (
                                <div className="border border-amber-300 bg-amber-50 rounded-md p-3 space-y-2">
                                    <div className="flex items-center gap-1.5 text-amber-800">
                                        <AlertTriangle className="w-4 h-4" />
                                        <span className="text-xs font-semibold uppercase tracking-wide">
                                            Potential Match Found
                                        </span>
                                    </div>
                                    <div className="text-[11px] text-amber-900 space-y-0.5">
                                        <div>
                                            <span className="text-amber-700">Existing {dup.source}:</span>{" "}
                                            <span className="font-semibold">{dup.name || "—"}</span>
                                        </div>
                                        {dup.instagram_username && (
                                            <div>
                                                <span className="text-amber-700">Instagram:</span>{" "}
                                                {dup.instagram_username}
                                            </div>
                                        )}
                                        <div>
                                            <span className="text-amber-700">Matched on:</span>{" "}
                                            {dup.matched_on?.replace(/_/g, " ")}
                                        </div>
                                        {dup.created_at && (
                                            <div>
                                                <span className="text-amber-700">Created:</span>{" "}
                                                {String(dup.created_at).slice(0, 10)}
                                            </div>
                                        )}
                                    </div>
                                    <div className="flex flex-wrap gap-1.5 pt-0.5">
                                        <button
                                            onClick={openExisting}
                                            className="px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide rounded-sm bg-amber-700 text-white hover:bg-amber-800 focus:outline-none"
                                        >
                                            Open Existing
                                        </button>
                                        <button
                                            onClick={updateExisting}
                                            disabled={stage === "saving"}
                                            className="px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide rounded-sm border border-amber-300 bg-white text-amber-800 hover:bg-amber-100 disabled:opacity-50 focus:outline-none"
                                        >
                                            Update Existing
                                        </button>
                                        <button
                                            onClick={() => setForceCreate(true)}
                                            className={`px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide rounded-sm border focus:outline-none ${
                                                forceCreate
                                                    ? "bg-black text-white border-black"
                                                    : "bg-white text-amber-800 border-amber-300 hover:bg-amber-100"
                                            }`}
                                        >
                                            Create Anyway
                                        </button>
                                    </div>
                                </div>
                            )}

                            {/* Field review table */}
                            <div className="border border-black/[0.06] rounded-md overflow-hidden">
                                <div className="grid grid-cols-[110px_1fr_70px] bg-black/[0.02] text-[9px] uppercase font-bold text-black/45 tracking-wider px-3 py-1.5">
                                    <span>Field</span>
                                    <span>Value</span>
                                    <span className="text-right">Conf.</span>
                                </div>
                                {FIELD_ROWS.map((row) => {
                                    const meta = result?.fields?.[row.key];
                                    const conf = meta?.confidence ?? 0;
                                    const band = meta?.band || "low";
                                    return (
                                        <div
                                            key={row.key}
                                            className="grid grid-cols-[110px_1fr_70px] items-start gap-2 px-3 py-2 border-t border-black/[0.04]"
                                        >
                                            <label className="text-[10px] font-semibold text-black/55 pt-1.5">
                                                {row.label}
                                            </label>
                                            {row.multiline ? (
                                                <textarea
                                                    value={form[row.key] || ""}
                                                    onChange={(e) => setField(row.key, e.target.value)}
                                                    rows={3}
                                                    className="text-xs border border-black/[0.08] rounded-sm px-2 py-1 focus:outline-none focus:border-black/30 resize-y"
                                                />
                                            ) : (
                                                <input
                                                    type="text"
                                                    value={form[row.key] || ""}
                                                    onChange={(e) => setField(row.key, e.target.value)}
                                                    placeholder="Not found"
                                                    className="text-xs border border-black/[0.08] rounded-sm px-2 py-1 focus:outline-none focus:border-black/30"
                                                />
                                            )}
                                            <div className="flex justify-end pt-1">
                                                {meta && (form[row.key] || conf > 0) ? (
                                                    <span
                                                        className={`text-[9px] font-bold px-1.5 py-0.5 rounded-sm border ${bandStyle(
                                                            band
                                                        )}`}
                                                        title={`${band} confidence`}
                                                    >
                                                        {Math.round(conf)}%
                                                    </span>
                                                ) : (
                                                    <span className="text-[9px] text-black/30">—</span>
                                                )}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>

                            <p className="text-[10px] text-black/40 flex items-center gap-1">
                                <ImageIcon className="w-3 h-3" />
                                Extracted from {result?.processing_ms ? `${result.processing_ms} ms` : "screenshots"} ·
                                edit any field before saving.
                            </p>

                            <div className="flex items-center gap-2 pt-1">
                                <button
                                    onClick={() => {
                                        setStage("upload");
                                        setResult(null);
                                        setForm(null);
                                    }}
                                    className="px-3 py-2 text-xs font-semibold text-black/60 hover:text-black focus:outline-none"
                                >
                                    Back
                                </button>
                                <button
                                    onClick={handleSave}
                                    disabled={stage === "saving" || duplicateBlocksSave}
                                    className="flex-1 bg-black text-white py-2 rounded-sm text-xs font-semibold uppercase tracking-wider disabled:opacity-40 focus:outline-none inline-flex items-center justify-center gap-1.5"
                                >
                                    {stage === "saving" ? (
                                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                    ) : (
                                        <CheckCircle2 className="w-3.5 h-3.5" />
                                    )}
                                    {duplicateBlocksSave ? "Resolve match to save" : "Save Scout"}
                                </button>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
