"use client";

import React, { createContext, useContext, useState } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";
import FloatingUploadManager from "../components/shared/FloatingUploadManager";
import axios from "axios";


const UploadManagerContext = createContext(null);

export function useUploadManager() {
    const context = useContext(UploadManagerContext);
    if (!context) {
        throw new Error("useUploadManager must be used within an UploadManagerProvider");
    }
    return context;
}

export function UploadManagerProvider({ children }) {
    const [activeUploads, setActiveUploads] = useState({});
    const [retryQueue, setRetryQueue] = useState({});

    const uploadFile = async (file, category, label, options = {}) => {
        const { endpoint, token, onSuccess, onBeforeUpload } = options;

        if (!endpoint && !onBeforeUpload) {
            toast.error("Upload endpoint is missing.");
            return;
        }

        // Size & type validation
        const isVideoSlot = ["intro_video", "take", "take_1", "take_2", "take_3"].includes(category);
        const CAP_MB = isVideoSlot ? 200 : 20;

        if (file) {
            const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
            if (isVideoSlot) {
                if (file.size > CAP_MB * 1024 * 1024) {
                    toast.error(`Video is too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
                    return;
                }
                const allowedVideoExts = [".mp4", ".mov", ".avi", ".webm", ".mkv", ".3gp"];
                if (!allowedVideoExts.includes(ext) && !file.type.startsWith("video/")) {
                    toast.error(`Unsupported video format. Please upload MP4, MOV, or WEBM.`);
                    return;
                }
            } else {
                if (file.size > CAP_MB * 1024 * 1024) {
                    toast.error(`Image too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
                    return;
                }
                if ([".bmp", ".tiff"].includes(ext) || ["image/bmp", "image/tiff"].includes(file.type)) {
                    toast.error(`BMP and TIFF formats are not supported. Please upload JPEG, PNG, or HEIC.`);
                    return;
                }
                if (!file.type.startsWith("image/") && ![".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"].includes(ext)) {
                    toast.error(`Unsupported image format. Please upload JPG, PNG, WEBP, or HEIC.`);
                    return;
                }
            }
        }

        // Run any custom hooks before starting (e.g. creating/saving profile drafts)
        let dynamicToken = token;
        let dynamicEndpoint = endpoint;
        if (onBeforeUpload) {
            const result = await onBeforeUpload();
            if (!result) return; // Hook failed or returned falsy
            if (result.token) dynamicToken = result.token;
            if (result.endpoint) dynamicEndpoint = result.endpoint;
        }

        const slotKey = label ? `${category}:${label}` : category;

        setActiveUploads((prev) => ({
            ...prev,
            [slotKey]: {
                status: "uploading",
                pct: 0,
                fileName: file.name,
                category,
                label,
                file,
                options
            }
        }));

        setRetryQueue((q) => ({
            ...q,
            [slotKey]: { category, label, attempt: 0, fileName: file.name, fileSize: file.size, file, options }
        }));

        const MAX_ATTEMPTS = 3;
        let lastErr = null;

        for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try {
                // 1. Get upload signature from backend
                const headers = {};
                if (dynamicToken) {
                    headers["Authorization"] = `Bearer ${dynamicToken}`;
                }
                const signRes = await api.post(`${dynamicEndpoint}/sign`, {
                    category,
                    filename: file.name
                }, { headers });
                const signData = signRes.data;

                // 2. Build signed upload FormData for Cloudinary
                const fd = new FormData();
                fd.append("file", file);
                fd.append("api_key", signData.api_key);
                fd.append("timestamp", signData.timestamp);
                fd.append("signature", signData.signature);
                fd.append("folder", signData.folder);
                fd.append("public_id", signData.public_id);
                if (signData.eager) {
                    fd.append("eager", signData.eager);
                }
                if (signData.transformation) {
                    fd.append("transformation", signData.transformation);
                }

                // 3. Upload directly to Cloudinary
                const cloudinaryUrl = `https://api.cloudinary.com/v1_1/${signData.cloud_name}/${signData.resource_type}/upload`;
                const uploadRes = await axios.post(cloudinaryUrl, fd, {
                    headers: {
                        "Content-Type": "multipart/form-data",
                    },
                    timeout: 0,
                    onUploadProgress: (e) => {
                        if (e.total) {
                            const pct = Math.round((e.loaded / e.total) * 100);
                            setActiveUploads((prev) => {
                                if (!prev[slotKey]) return prev;
                                return {
                                    ...prev,
                                    [slotKey]: {
                                        ...prev[slotKey],
                                        status: pct >= 100 ? "processing" : "uploading",
                                        pct
                                    }
                                };
                            });
                        }
                    }
                });

                // 4. Submit completed metadata to backend to save
                const completeRes = await api.post(`${dynamicEndpoint}/complete`, {
                    media_id: signData.media_id,
                    category,
                    label: label && category === "take" ? label : undefined,
                    public_id: signData.public_id,
                    url: uploadRes.data.secure_url,
                    bytes: uploadRes.data.bytes,
                    duration: uploadRes.data.duration,
                    content_type: file.type,
                    original_filename: file.name,
                    eager: uploadRes.data.eager
                }, { headers });

                if (onSuccess) onSuccess(completeRes.data);

                setRetryQueue((q) => {
                    const n = { ...q };
                    delete n[slotKey];
                    return n;
                });

                setActiveUploads((prev) => ({
                    ...prev,
                    [slotKey]: {
                        ...prev[slotKey],
                        status: "completed",
                        pct: 100
                    }
                }));

                setTimeout(() => {
                    setActiveUploads((prev) => {
                        const next = { ...prev };
                        if (next[slotKey]?.status === "completed") {
                            delete next[slotKey];
                        }
                        return next;
                    });
                }, 3000);

                if (attempt > 1) toast.success(`Recovered after ${attempt} attempts`);
                return;
            } catch (err) {
                lastErr = err;
                const isNetwork = !err?.response;
                if (!isNetwork || attempt === MAX_ATTEMPTS) break;

                const wait = 1000 * Math.pow(2, attempt - 1);
                toast.message(`Network blip — retrying in ${wait / 1000}s (attempt ${attempt}/${MAX_ATTEMPTS})`);

                setRetryQueue((q) => ({
                    ...q,
                    [slotKey]: { ...(q[slotKey] || {}), attempt }
                }));

                await new Promise((r) => setTimeout(r, wait));
            }
        }

        // Failed after all retries
        setRetryQueue((q) => ({
            ...q,
            [slotKey]: { ...(q[slotKey] || {}), failed: true, error: lastErr?.response?.data?.detail || "Upload failed" }
        }));

        setActiveUploads((prev) => ({
            ...prev,
            [slotKey]: {
                ...prev[slotKey],
                status: "failed",
                error: lastErr?.response?.data?.detail || "Upload failed"
            }
        }));

        toast.error(lastErr?.response?.data?.detail || "Upload failed — tap Retry to try again");
    };

    const retryUpload = async (slotKey) => {
        const entry = retryQueue[slotKey];
        if (!entry?.file) {
            toast.error("Re-select the file to retry");
            return;
        }
        await uploadFile(entry.file, entry.category, entry.label, entry.options);
    };

    const dismissUpload = (slotKey) => {
        setActiveUploads((prev) => {
            const n = { ...prev };
            delete n[slotKey];
            return n;
        });
    };

    return (
        <UploadManagerContext.Provider value={{ activeUploads, retryQueue, uploadFile, retryUpload, dismissUpload }}>
            {children}
            <FloatingUploadManager
                activeUploads={activeUploads}
                onRetry={retryUpload}
                onDismiss={dismissUpload}
            />
        </UploadManagerContext.Provider>
    );
}
