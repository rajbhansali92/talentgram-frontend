import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import CommTimeline from "@/components/CommTimeline";
import { toast } from "sonner";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import {
    Select,
    SelectContent,
    SelectGroup,
    SelectItem,
    SelectLabel,
    SelectSeparator,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    ArrowLeft,
    Upload,
    Trash2,
    Star,
    Loader2,
    X,
    Play,
    Tag,
    Plus,
    AlertTriangle,
} from "lucide-react";
import {
    HEIGHT_OPTIONS,
    GENDER_OPTIONS,
    ETHNICITY_OPTIONS,
    FOLLOWER_TIERS,
} from "@/lib/talentSchema";
import WorkLinksDisplay from "@/components/WorkLinksDisplay";
import { normalizeInstagramHandle } from "@/lib/mediaUtils";
import SkillsSelector from "@/components/SkillsSelector";
import LocationSelector from "@/components/LocationSelector";



const emptyTalent = {
    name: "",
    email: "",
    phone: "",
    alternate_contact_number: "",
    age: "",
    dob: "",
    height: "",
    location: [],
    ethnicity: "",
    gender: "",
    instagram_handle: "",
    instagram_followers: "",
    bio: "",
    work_links: [],
    interested_in: [],
    tags: [],
    skills: [],
    whatsapp_group_name: "",
};

// P1-6: client-side limits MUST match the backend (04_MEDIA_RULES + core.py
// signature validation) so a file that passes here never fails server-side.
//  - image  : 20 MB  (MAX_SUBMISSION_IMAGE_BYTES)
//  - video  : 200 MB (MAX_SUBMISSION_VIDEO_BYTES)
//  - WebM is REJECTED by backend magic-byte validation, so it is excluded here.
const MAX_IMAGE_SIZE = 20 * 1024 * 1024; // 20MB
const MAX_VIDEO_SIZE = 200 * 1024 * 1024; // 200MB
const ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/jpg", "image/heic", "image/heif"];
const ALLOWED_VIDEO_TYPES = ["video/mp4", "video/quicktime"];

function calcAge(dob) {
    if (!dob) return null;
    const [y, m, d] = dob.split("-").map((n) => parseInt(n, 10));
    if (!y || !m || !d) return null;
    const today = new Date();
    let age = today.getFullYear() - y;
    const mm = today.getMonth() + 1;
    const dd = today.getDate();
    if (mm < m || (mm === m && dd < d)) age -= 1;
    return age >= 0 && age <= 120 ? age : null;
}

// getLinkMeta is provided by @/components/WorkLinksDisplay (shared)


// ---------------------------------------------------------------------------
// Work-links helpers
// ---------------------------------------------------------------------------
const WORK_LINK_URL_RE = /https?:\/\/[^\s]+/;

function parseStoredLink(stored) {
    if (typeof stored === "string" && stored.includes(" || ")) {
        const idx = stored.indexOf(" || ");
        const url = stored.slice(idx + 4).trim().replace(/[.,;:!?)\]>]+$/, "");
        return { label: stored.slice(0, idx).trim(), url };
    }
    const url = (stored || "").replace(/[.,;:!?)\]>]+$/, "");
    return { label: "", url };
}


function parseWorkLinksText(text) {
    return text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
            const match = WORK_LINK_URL_RE.exec(line);
            if (!match) return null;
            // Strip trailing punctuation that may have been captured by the greedy [^\s]+ match
            const url = match[0].replace(/[.,;:!?)\]>]+$/, "");
            const before = line.slice(0, match.index).replace(new RegExp("[-:" + "\\s" + "|]+$"), "").trim();
            return before ? `${before} || ${url}` : url;
        })
        .filter(Boolean);
}


function linksToText(links) {
    return (links || [])
        .map((w) => {
            const { label, url } = parseStoredLink(w);
            return label ? `${label} - ${url}` : url;
        })
        .join("\n");
}

function Field({ label, value, onChange, type = "text", ...rest }) {
    return (
        <label className="block">
            <span className="text-[11px] text-black/45 tracking-widest uppercase">
                {label}
            </span>
            <input
                type={type}
                value={value || ""}
                onChange={(e) => onChange(e.target.value)}
                className="mt-2 w-full bg-transparent border-b border-[#eaeaea] focus:border-black/40 outline-none py-2.5 text-sm text-black/85 placeholder:text-black/30"
                {...rest}
            />
        </label>
    );
}

export default function TalentEdit() {
    const { id } = useParams();
    const nav = useNavigate();
    const isEdit = Boolean(id);
    const isAdminRole = isAdmin();
    const [loading, setLoading] = useState(isEdit); // ISSUE 1: Fixed loading state
    const [isEditing, setIsEditing] = useState(!isEdit);
    const [talent, setTalent] = useState(emptyTalent);
    const [linksDraft, setLinksDraft] = useState("");
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(null);
    const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
    const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false);
    const [mediaToRemove, setMediaToRemove] = useState(null);
    // Tag management state
    const [allTags, setAllTags] = useState([]);
    const [tagInput, setTagInput] = useState("");
    const [tagSaving, setTagSaving] = useState(false);
    const [globalTagDeleteTarget, setGlobalTagDeleteTarget] = useState(null); // {id, name}
    const [globalTagDeleteConfirmText, setGlobalTagDeleteConfirmText] = useState("");
    const [originalTalent, setOriginalTalent] = useState(emptyTalent);
    const [tagSearch, setTagSearch] = useState("");
    const [isTagDropdownOpen, setIsTagDropdownOpen] = useState(false);

    // Lightbox states
    const [lightboxIndex, setLightboxIndex] = useState(null);
    const [lightboxCategory, setLightboxCategory] = useState(null);
    
    // File refs
    const fileRefs = useRef({
        indian: null,
        western: null,
        portfolio: null,
        video: null,
    });
    
    const setFileRef = (key) => (el) => {
        fileRefs.current[key] = el;
    };

    const updateTalent = useCallback((patch) => {
        setTalent(prev => ({ ...prev, ...patch }));
    }, []);

    const handleCancel = useCallback(() => {
        setTalent(originalTalent);
        setIsEditing(false);
    }, [originalTalent]);

    const formatDuration = (sec) => {
        if (!sec) return null;
        const s = Math.floor(sec % 60);
        const m = Math.floor(sec / 60);
        return `${m}:${s < 10 ? '0' : ''}${s}`;
    };

    const touchStartX = useRef(0);
    const touchEndX = useRef(0);

    const handleTouchStart = (e) => {
        touchStartX.current = e.targetTouches[0].clientX;
        touchEndX.current = e.targetTouches[0].clientX;
    };

    const handleTouchMove = (e) => {
        touchEndX.current = e.targetTouches[0].clientX;
    };

    const handleTouchEnd = () => {
        if (lightboxIndex === null || lightboxCategory === null) return;
        const diff = touchStartX.current - touchEndX.current;
        const threshold = 50;
        const items = (talent.media || []).filter(m => m.category === lightboxCategory);
        if (diff > threshold) {
            setLightboxIndex((prev) => (prev < items.length - 1 ? prev + 1 : 0));
        } else if (diff < -threshold) {
            setLightboxIndex((prev) => (prev > 0 ? prev - 1 : items.length - 1));
        }
    };

    useEffect(() => {
        if (lightboxIndex === null || lightboxCategory === null) return;

        const handleKeyDown = (e) => {
            const items = (talent.media || []).filter(m => m.category === lightboxCategory);
            if (e.key === "ArrowRight") {
                setLightboxIndex((prev) => (prev < items.length - 1 ? prev + 1 : 0));
            } else if (e.key === "ArrowLeft") {
                setLightboxIndex((prev) => (prev > 0 ? prev - 1 : items.length - 1));
            } else if (e.key === "Escape") {
                setLightboxIndex(null);
                setLightboxCategory(null);
            }
        };

        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [lightboxIndex, lightboxCategory, talent.media]);

    useEffect(() => {
        if (lightboxIndex !== null && lightboxCategory !== null) {
            document.body.style.overflow = "hidden";
        } else {
            document.body.style.overflow = "";
        }
        return () => {
            document.body.style.overflow = "";
        };
    }, [lightboxIndex, lightboxCategory]);

    useEffect(() => {
        if (!isEdit) return;
        (async () => {
            try {
                const { data } = await adminApi.get(`/talents/${id}`);
                setTalent({ ...emptyTalent, ...data });
                setOriginalTalent({ ...emptyTalent, ...data });
                setLinksDraft(linksToText(data.work_links || []));
            } catch {
                toast.error("Failed to load talent");
            } finally {
                setLoading(false); // ISSUE 1: Set loading false after fetch
            }
        })();
    }, [id, isEdit]);

    // Load all global tags on mount
    useEffect(() => {
        (async () => {
            try {
                const { data } = await adminApi.get("/tags");
                setAllTags(data.tags || []);
            } catch {
                // silently ignore — tags are an enhancement
            }
        })();
    }, []);

    const save = async () => {
        setSaving(true);
        try {
            const payload = {
                ...talent,
                instagram_handle: normalizeInstagramHandle(talent.instagram_handle) || null,
                dob: talent.dob || null,
                age: talent.dob
                    ? calcAge(talent.dob)
                    : talent.age
                      ? parseInt(talent.age, 10)
                      : null,
                work_links: (talent.work_links || []).filter(Boolean),
            };
            if (isEdit) {
                await adminApi.put(`/talents/${id}`, payload);
                setOriginalTalent(payload);
                setIsEditing(false);
                toast.success("Saved");
            } else {
                const { data } = await adminApi.post(`/talents`, payload);
                toast.success("Talent created");
                nav(`/admin/talents/${data.id}`);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally {
            setSaving(false);
        }
    };

    const computedAge = useMemo(
        () => calcAge(talent.dob) ?? (talent.age || null),
        [talent.dob, talent.age],
    );

    const deleteTalent = async () => {
        if (!isEdit) return;
        try {
            const res = await adminApi.delete(`/talents/${id}`);
            if (process.env.NODE_ENV === "development") {
                console.info("[delete talent]", id, res?.data);
            }
            toast.success("Talent deleted");
            setConfirmDeleteOpen(false);
            nav("/admin/talents");
        } catch (err) {
            if (process.env.NODE_ENV === "development") {
                console.error("[delete talent] failed", err?.response?.data || err);
            }
            toast.error(
                err?.response?.data?.detail ||
                    err?.message ||
                    "Delete failed — check console for details",
            );
            throw err;
        }
    };

    // P1-6: validation aligned with backend limits/signatures.
    const validateFile = (file, category) => {
        const isVideo = category === "video";
        const maxSize = isVideo ? MAX_VIDEO_SIZE : MAX_IMAGE_SIZE;
        if (file.size > maxSize) {
            const mb = Math.round(maxSize / (1024 * 1024));
            toast.error(`${file.name} is too large. Max size is ${mb}MB`);
            return false;
        }

        const allowedTypes = isVideo ? ALLOWED_VIDEO_TYPES : ALLOWED_IMAGE_TYPES;
        // Some browsers report an empty type for HEIC/HEIF; fall back to the
        // extension so genuine Apple media isn't blocked client-side (the
        // backend still magic-byte validates).
        const okType =
            allowedTypes.includes(file.type) ||
            (!file.type && /\.(heic|heif|mp4|mov|jpe?g|png|webp)$/i.test(file.name || ""));
        if (!okType) {
            const allowed = isVideo ? "MP4, MOV" : "JPEG, PNG, WEBP, HEIC";
            toast.error(`${file.name} has invalid format. Allowed: ${allowed}`);
            return false;
        }

        return true;
    };

    const uploadFiles = async (files, category) => {
        if (!isEdit) {
            toast.error("Save talent first before uploading media");
            return;
        }
        
        // ISSUE 2: Validate all files first
        const validFiles = [];
        for (const file of files) {
            if (validateFile(file, category)) {
                validFiles.push(file);
            }
        }
        
        if (validFiles.length === 0) {
            toast.error("No valid files to upload");
            return;
        }
        
        setUploading(category);
        try {
            const uploadPromises = validFiles.map(async (file) => {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("category", category);
                const { data } = await adminApi.post(`/talents/${id}/media`, fd, {
                    headers: { "Content-Type": "multipart/form-data" },
                });
                return data;
            });
            
            const results = await Promise.all(uploadPromises);
            const latestTalent = results[results.length - 1];
            
            if (latestTalent) {
                updateTalent({ 
                    media: latestTalent.media || [],
                    cover_media_id: latestTalent.cover_media_id ?? talent.cover_media_id,
                    cover_url: latestTalent.cover_url ?? talent.cover_url,
                });
            }
            
            toast.success(`${validFiles.length} upload(s) added`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const removeMedia = async () => {
        if (!mediaToRemove) return;
        
        try {
            await adminApi.delete(`/talents/${id}/media/${mediaToRemove}`);
            const wasCover = talent.cover_media_id === mediaToRemove;
            updateTalent({
                media: (talent.media || []).filter(m => m.id !== mediaToRemove),
                // If the deleted item was the cover, clear both cover fields
                // to stay in sync with the backend $unset operation.
                cover_media_id: wasCover ? null : talent.cover_media_id,
                cover_url: wasCover ? null : talent.cover_url,
            });
            toast.success("Media removed");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Remove failed");
        } finally {
            setConfirmRemoveOpen(false);
            setMediaToRemove(null);
        }
    };

    const setCover = async (mid) => {
        try {
            const { data } = await adminApi.post(`/talents/${id}/cover/${mid}`);
            // Sync both cover_media_id and the denormalized cover_url returned
            // by the updated set_cover endpoint. This keeps local state aligned
            // with the roster list without requiring a full re-fetch.
            updateTalent({
                cover_media_id: mid,
                cover_url: data?.cover_url ?? null,
            });
            toast.success("Cover updated");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to set cover");
        }
    };

    const addWorkLink = () => {
        const parsed = parseWorkLinksText(linksDraft);
        updateTalent({ work_links: parsed });
    };

    // Tag management handlers
    const assignTag = useCallback(async (tag) => {
        if (!isEdit || !id) {
            toast.error("Please save the talent record first.");
            return;
        }
        if (!tag || !tag.id) {
            toast.error("Invalid tag selection.");
            return;
        }
        const already = (talent.tags || []).some(t => t.id === tag.id);
        if (already) return;
        try {
            await adminApi.post(`/talents/${id}/tag/${tag.id}`);
            const updated = [...(talent.tags || []), { id: tag.id, name: tag.name }];
            updateTalent({ tags: updated });
            setOriginalTalent(prev => ({ ...prev, tags: updated }));
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to assign tag");
        }
    }, [id, isEdit, talent.tags, updateTalent]);

    const createAndAssignTag = useCallback(async () => {
        const name = tagInput.trim();
        if (!name) return;
        if (!isEdit || !id) {
            toast.error("Please save the talent record first.");
            return;
        }
        setTagSaving(true);
        try {
            const { data } = await adminApi.post("/tags", { name });
            const tag = data.tag;
            // Update global list if new
            if (data.created) {
                setAllTags(prev => [...prev, tag].sort((a, b) => a.name.localeCompare(b.name)));
            }
            // Assign to current talent
            const already = (talent.tags || []).some(t => t.id === tag.id);
            if (!already) {
                await adminApi.post(`/talents/${id}/tag/${tag.id}`);
                const updated = [...(talent.tags || []), { id: tag.id, name: tag.name }];
                updateTalent({ tags: updated });
                setOriginalTalent(prev => ({ ...prev, tags: updated }));
            }
            setTagInput("");
            toast.success(data.created ? `Tag "${name}" created` : `Tag "${name}" assigned`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to create tag");
        } finally {
            setTagSaving(false);
        }
    }, [id, isEdit, tagInput, talent.tags, updateTalent]);

    const removeTagFromTalent = useCallback(async (tagId) => {
        if (!isEdit || !id) {
            toast.error("Invalid state: Talent not saved.");
            return;
        }
        if (!tagId) {
            toast.error("Invalid tag selection.");
            return;
        }
        try {
            await adminApi.delete(`/talents/${id}/tag/${tagId}`);
            const updated = (talent.tags || []).filter(t => t.id !== tagId);
            updateTalent({ tags: updated });
            setOriginalTalent(prev => ({ ...prev, tags: updated }));
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to remove tag");
        }
    }, [id, isEdit, talent.tags, updateTalent]);

    const confirmDeleteGlobalTag = useCallback(async () => {
        if (!globalTagDeleteTarget || globalTagDeleteConfirmText !== "DELETE") return;
        try {
            await adminApi.delete(`/tags/${globalTagDeleteTarget.id}`);
            setAllTags(prev => prev.filter(t => t.id !== globalTagDeleteTarget.id));
            updateTalent({ tags: (talent.tags || []).filter(t => t.id !== globalTagDeleteTarget.id) });
            toast.success(`Tag "${globalTagDeleteTarget.name}" deleted globally`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to delete tag");
        } finally {
            setGlobalTagDeleteTarget(null);
            setGlobalTagDeleteConfirmText("");
        }
    }, [globalTagDeleteTarget, globalTagDeleteConfirmText, talent.tags, updateTalent]);

    const mediaBy = (cat) =>
        (talent.media || []).filter((m) => m.category === cat);

    const isDirty = useMemo(() => {
        return JSON.stringify(talent) !== JSON.stringify(originalTalent);
    }, [talent, originalTalent]);

    // P1-7: SINGLE unsaved-changes guard covering every navigation vector
    // (tab close / refresh / external nav, browser Back, and in-app links).
    // A shared `promptingRef` ensures only ONE confirm() can be in flight, so
    // the old triple-handler double-prompt and the popstate re-entry loop are
    // both impossible.
    const promptingRef = useRef(false);
    useEffect(() => {
        if (loading || !isDirty) return;

        const MESSAGE = "You have unsaved changes. Leave without saving?";

        // Returns true if the user confirms leaving. Deduped so overlapping
        // events (e.g. an anchor click that also triggers popstate) prompt once.
        const confirmLeave = () => {
            if (promptingRef.current) return false;
            promptingRef.current = true;
            try {
                return window.confirm(MESSAGE);
            } finally {
                promptingRef.current = false;
            }
        };

        // Vector 1: browser/tab close, refresh, hard external navigation.
        const handleBeforeUnload = (e) => {
            e.preventDefault();
            e.returnValue = MESSAGE;
            return e.returnValue;
        };

        // Vector 2: browser Back button. Re-push our entry and only allow the
        // back navigation if the user confirms (no recursive re-trigger because
        // confirmLeave is deduped and we only nav(-1) on an explicit yes).
        const handlePopState = () => {
            if (confirmLeave()) {
                window.removeEventListener("popstate", handlePopState);
                nav(-1);
            } else {
                window.history.pushState(null, "", window.location.pathname);
            }
        };

        // Vector 3: in-app links (React Router <Link> / anchors).
        const handleAnchorClick = (e) => {
            if (e.defaultPrevented) return;
            const anchor = e.target.closest("a");
            if (!anchor) return;
            const href = anchor.getAttribute("href");
            if (!href || href.startsWith("#")) return;
            const internal = href.startsWith("/") || href.includes(window.location.host);
            if (internal && !confirmLeave()) {
                e.preventDefault();
                e.stopPropagation();
            }
        };

        window.addEventListener("beforeunload", handleBeforeUnload);
        window.history.pushState(null, "", window.location.pathname);
        window.addEventListener("popstate", handlePopState);
        document.addEventListener("click", handleAnchorClick, true);

        return () => {
            window.removeEventListener("beforeunload", handleBeforeUnload);
            window.removeEventListener("popstate", handlePopState);
            document.removeEventListener("click", handleAnchorClick, true);
        };
    }, [isDirty, loading, nav]);

    // ISSUE 1: Fixed loading skeleton
    if (loading) {
        return (
            <div className="p-6 md:p-10 max-w-7xl mx-auto">
                <div className="animate-pulse">
                    <div className="h-8 bg-black/5 rounded w-32 mb-6"></div>
                    <div className="h-12 bg-black/5 rounded w-64 mb-8"></div>
                    <div className="space-y-4">
                        <div className="h-64 bg-black/5 rounded-xl"></div>
                        <div className="h-64 bg-black/5 rounded-xl"></div>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto text-black/85"
            data-testid="talent-edit-page"
        >
            <Link
                to="/admin/talents"
                className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6 transition-colors"
            >
                <ArrowLeft className="w-3 h-3" /> Back to roster
            </Link>

            <div className="flex items-end justify-between flex-wrap gap-4 mb-8">
                <div>
                    <p className="eyebrow mb-3">
                        {isEdit ? "Edit Talent" : "New Talent"}
                    </p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        {talent.name || "Untitled"}
                    </h1>
                </div>
                <div className="flex gap-2">
                    {isEdit && isAdminRole && (
                        <button
                            onClick={() => setConfirmDeleteOpen(true)}
                            data-testid="delete-talent-btn"
                            className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#eaeaea] text-black/60 hover:text-red-600 hover:border-red-600/40 rounded-md text-xs transition-colors"
                        >
                            <Trash2 className="w-3 h-3" strokeWidth={1.5} /> Delete
                        </button>
                    )}
                    {isEditing ? (
                        <>
                            <button
                                onClick={handleCancel}
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#eaeaea] text-black/60 hover:bg-black/[0.02] rounded-md text-xs transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={save}
                                disabled={saving}
                                data-testid="save-talent-btn"
                                className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors"
                            >
                                {saving && <Loader2 className="w-3 h-3 animate-spin" />}{" "}
                                {isEdit ? "Save changes" : "Create talent"}
                            </button>
                        </>
                    ) : (
                        <button
                            onClick={() => setIsEditing(true)}
                            className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors"
                        >
                            Edit
                        </button>
                    )}
                </div>
            </div>

            {/* Basic info */}
            <section className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mb-6">
                <p className="eyebrow mb-6">Profile</p>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-x-8 gap-y-6">
                    <Field
                        label="Full Name"
                        value={talent.name}
                        onChange={(v) => updateTalent({ name: v })}
                        disabled={!isEditing}
                    />
                    <Field
                        label="Email"
                        type="email"
                        value={talent.email}
                        onChange={(v) => updateTalent({ email: v })}
                        data-testid="talent-email-input"
                        placeholder="optional"
                        disabled={!isEditing}
                    />
                    <Field
                        label="Phone (WhatsApp)"
                        type="tel"
                        value={talent.phone}
                        onChange={(v) => updateTalent({ phone: v })}
                        data-testid="talent-phone-input"
                        placeholder="optional"
                        disabled={!isEditing}
                    />
                    <Field
                        label="Alternate Contact Number"
                        type="tel"
                        value={talent.alternate_contact_number}
                        onChange={(v) => updateTalent({ alternate_contact_number: v })}
                        data-testid="talent-alt-phone-input"
                        placeholder={isEditing ? "optional" : "Not Provided"}
                        disabled={!isEditing}
                    />
                    {/* FEATURE 1: WhatsApp Group Name — admin-only (view + edit). */}
                    {isAdmin() && (
                        <Field
                            label="WhatsApp Group Name"
                            value={talent.whatsapp_group_name}
                            onChange={(v) => updateTalent({ whatsapp_group_name: v })}
                            data-testid="talent-whatsapp-group-input"
                            placeholder="e.g. Sahal Mansuri x Talentgram"
                            disabled={!isEditing}
                        />
                    )}

                    <label className="block" data-testid="field-dob">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Date of Birth
                        </span>
                        <input
                            type="date"
                            value={talent.dob || ""}
                            onChange={(e) => updateTalent({ dob: e.target.value })}
                            max={new Date().toISOString().split("T")[0]}
                            data-testid="dob-input"
                            disabled={!isEditing}
                            className="mt-2 w-full bg-transparent border-b border-[#eaeaea] focus:border-black/40 outline-none py-2.5 text-sm text-black/85 disabled:opacity-70"
                        />
                    </label>
                    <div data-testid="field-age-auto">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Age (auto)
                        </span>
                        <div className="mt-2 border-b border-[#eaeaea] py-2.5 text-sm flex items-center justify-between">
                            <span
                                data-testid="computed-age"
                                className="font-display text-base text-black/85"
                            >
                                {computedAge ?? "—"}
                            </span>
                            <span className="text-[10px] text-black/40">
                                {talent.dob
                                    ? "auto-computed"
                                    : "set DOB to auto-calc"}
                            </span>
                        </div>
                    </div>

                    <div data-testid="field-gender">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Gender
                        </span>
                        <div className="mt-2 grid grid-cols-2 gap-2">
                            {GENDER_OPTIONS.map((g) => {
                                const active = talent.gender === g.key;
                                return (
                                    <button
                                        key={g.key}
                                        type="button"
                                        disabled={!isEditing}
                                        onClick={() =>
                                            updateTalent({
                                                gender: active ? "" : g.key,
                                            })
                                        }
                                        data-testid={`gender-${g.key}-btn`}
                                        className={`px-3 py-2.5 text-sm rounded-full border transition-colors ${
                                            active
                                                ? "bg-black text-white border-black"
                                                : "border-black/[0.15] hover:border-black/[0.30] text-black/70 hover:text-black"
                                        } disabled:opacity-60`}
                                    >
                                        {g.label}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    <div data-testid="field-height">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Height
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.height || ""}
                                onValueChange={(v) => updateTalent({ height: v })}
                            >
                                <SelectTrigger
                                    data-testid="height-select-trigger"
                                    disabled={!isEditing}
                                    className="bg-transparent border-0 border-b border-[#eaeaea] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5 disabled:opacity-70"
                                >
                                    <SelectValue placeholder="Select height" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl max-h-72">
                                    {HEIGHT_OPTIONS.map((h) => (
                                        <SelectItem
                                            key={h}
                                            value={h}
                                            className="focus:bg-black/5 focus:text-black"
                                            data-testid={`height-option-${h}`}
                                        >
                                            {h}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>

                    <div className="md:col-span-1">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase block mb-2">
                            Location(s)
                        </span>
                        <LocationSelector
                            value={Array.isArray(talent.location) ? talent.location : []}
                            onChange={(arr) => updateTalent({ location: arr })}
                            disabled={!isEditing}
                            testid="form-location"
                        />
                    </div>
                    <div data-testid="field-ethnicity">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Ethnicity
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.ethnicity || ""}
                                onValueChange={(v) => updateTalent({ ethnicity: v })}
                            >
                                <SelectTrigger
                                    data-testid="ethnicity-select-trigger"
                                    disabled={!isEditing}
                                    className="bg-transparent border-0 border-b border-[#eaeaea] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5 disabled:opacity-70"
                                >
                                    <SelectValue placeholder="Select ethnicity" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl max-h-72">
                                    {ETHNICITY_OPTIONS.map((e) => (
                                        <SelectItem
                                            key={e.key}
                                            value={e.key}
                                            className="focus:bg-black/5 focus:text-black"
                                            data-testid={`ethnicity-option-${e.key}`}
                                        >
                                            {e.label}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>
                    <Field
                        label="Instagram Handle"
                        value={talent.instagram_handle}
                        onChange={(v) => updateTalent({ instagram_handle: v })}
                        onBlur={() => {
                            if (talent.instagram_handle) {
                                updateTalent({ instagram_handle: normalizeInstagramHandle(talent.instagram_handle) });
                            }
                        }}
                        placeholder="@username"
                        disabled={!isEditing}
                    />
                    <div data-testid="field-followers">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Instagram Followers
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.instagram_followers || ""}
                                onValueChange={(v) => updateTalent({ instagram_followers: v })}
                            >
                                <SelectTrigger
                                    data-testid="followers-select-trigger"
                                    disabled={!isEditing}
                                    className="bg-transparent border-0 border-b border-[#eaeaea] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5 disabled:opacity-70"
                                >
                                    <SelectValue placeholder="Select range" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl max-h-80">
                                    {FOLLOWER_TIERS.map((tier, i) => (
                                        <React.Fragment key={tier.label}>
                                            {i > 0 && <SelectSeparator className="bg-black/[0.08]" />}
                                            <SelectGroup>
                                                <SelectLabel className="text-[10px] tracking-[0.2em] uppercase text-black/45 font-medium px-2 py-2">
                                                    {tier.label}
                                                </SelectLabel>
                                                {tier.items.map((it) => (
                                                    <SelectItem
                                                        key={it}
                                                        value={it}
                                                        className="focus:bg-black/5 focus:text-black"
                                                        data-testid={`followers-option-${it}`}
                                                    >
                                                        {it}
                                                    </SelectItem>
                                                ))}
                                            </SelectGroup>
                                        </React.Fragment>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>
                </div>
                
                <div className="mt-6">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase block mb-2.5">
                        Skills & Special Abilities
                    </span>
                    <SkillsSelector
                        selectedSkills={talent.skills || []}
                        onChange={(arr) => updateTalent({ skills: arr })}
                        readOnly={!isEditing}
                    />
                </div>

                <div className="mt-6">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase">
                        Bio
                    </span>
                    <textarea
                        value={talent.bio || ""}
                        onChange={(e) => updateTalent({ bio: e.target.value })}
                        disabled={!isEditing}
                        rows={3}
                        className="mt-2 w-full bg-transparent border border-[#eaeaea] focus:border-black/40 outline-none p-4 text-sm text-black/85 rounded-xl resize-none disabled:opacity-75"
                    />
                </div>
                <div className="mt-6">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase">
                        Work Links
                    </span>
                    <p className="text-[10px] text-black/35 mt-0.5 mb-2">
                        One per line. Formats: &ldquo;Label - URL&rdquo;, &ldquo;Label: URL&rdquo;, or bare URL.
                    </p>
                    {isEditing && (
                        <>
                            <textarea
                                value={linksDraft}
                                onChange={(e) => {
                                    const text = e.target.value;
                                    setLinksDraft(text);
                                    updateTalent({ work_links: parseWorkLinksText(text) });
                                }}
                                rows={5}
                                placeholder={
                                    "Puma Campaign - https://instagram.com/reel/abc\n" +
                                    "Pepsi - https://youtu.be/xyz\n" +
                                    "https://vimeo.com/showreel"
                                }
                                className="mt-1 w-full bg-transparent border border-[#eaeaea] focus:border-black/40 outline-none p-3 text-sm text-black/85 rounded-xl resize-y font-mono leading-relaxed placeholder:text-black/25"
                            />
                            <div className="flex items-center gap-2 mt-1.5">
                                <span
                                    className={`text-[10px] font-mono px-2 py-0.5 rounded-full border ${
                                        (talent.work_links || []).length > 0
                                            ? "text-emerald-700 bg-emerald-50 border-emerald-100"
                                            : "text-neutral-400 bg-neutral-50 border-neutral-100"
                                    }`}
                                >
                                    Detected Links: {(talent.work_links || []).length}
                                </span>
                            </div>
                        </>
                    )}
                    <div className="mt-3 space-y-3">
                        <WorkLinksDisplay
                            links={talent.work_links || []}
                            className=""
                            renderExtra={(url, i) =>
                                isEditing ? (
                                    <button
                                        type="button"
                                        onClick={() => {
                                            const next = (talent.work_links || []).filter((_, j) => j !== i);
                                            updateTalent({ work_links: next });
                                            setLinksDraft(linksToText(next));
                                        }}
                                        className="text-black/35 hover:text-red-600 p-2 transition-colors shrink-0"
                                        title="Remove Link"
                                    >
                                        <X className="w-3.5 h-3.5" />
                                    </button>
                                ) : null
                            }
                        />
                    </div>
                </div>
                {/* ── Interested In ────────────────────────────────────── */}
                <div className="mt-8 pt-6 border-t border-black/[0.06]">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase">
                        Interested In
                    </span>
                    <p className="text-xs text-black/40 mt-1 mb-4">
                        Public: talent-selected work categories (visible to casting team).
                    </p>
                    <div className="flex flex-wrap gap-2" data-testid="edit-interested-in">
                        {(() => {
                            const activeOptions = ["Acting", "Modeling", "Influencer Campaigns"];
                            const renderedCategories = Array.from(new Set([
                                ...activeOptions,
                                ...(talent.interested_in || []).filter(Boolean)
                            ]));
                            return renderedCategories.map((cat) => {
                                const active = (talent.interested_in || []).includes(cat);
                                return (
                                    <button
                                        key={cat}
                                        type="button"
                                        disabled={!isEditing}
                                        onClick={() => {
                                            const set = new Set(talent.interested_in || []);
                                            if (active) set.delete(cat); else set.add(cat);
                                            updateTalent({ interested_in: [...set] });
                                        }}
                                        data-testid={`edit-interest-${cat.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}
                                        aria-pressed={active}
                                        className={[
                                            "px-3 py-1.5 rounded-full border text-[11px] tracking-[0.06em] transition-all duration-150 disabled:opacity-60",
                                            active
                                                ? "border-black bg-black text-white"
                                                : "border-black/[0.12] text-black/60 hover:border-black/30 hover:text-black",
                                        ].join(" ")}
                                    >
                                        {cat}
                                    </button>
                                );
                            });
                        })()}
                    </div>
                </div>

                {/* ── Internal Tags ─────────────────────────────────────── */}
                <div className="mt-8 pt-6 border-t border-black/[0.06]">
                    <div className="flex items-center gap-2 mb-1">
                        <Tag className="w-3.5 h-3.5 text-black/40" strokeWidth={1.5} />
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Internal Tags
                        </span>
                    </div>
                    <p className="text-xs text-black/40 mb-4">
                        Private casting labels. Only visible to the team.
                    </p>

                    <div className="space-y-4">
                        {/* Current tags on this talent */}
                        <div className="flex flex-wrap gap-2" data-testid="talent-current-tags">
                            {(talent.tags || []).map((tag) => (
                                <span
                                    key={tag.id}
                                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-black/[0.05] border border-black/[0.06] text-[11px] text-black/75 hover:bg-black/[0.08] transition-colors"
                                    data-testid={`talent-tag-${tag.id}`}
                                >
                                    {tag.name}
                                    {isEditing && (
                                        <button
                                            type="button"
                                            onClick={() => removeTagFromTalent(tag.id)}
                                            className="text-black/30 hover:text-red-500 transition-colors"
                                            title="Remove from this talent"
                                            data-testid={`remove-tag-${tag.id}`}
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    )}
                                    {isEditing && isAdminRole && (
                                        <button
                                            type="button"
                                            onClick={() => {
                                                setGlobalTagDeleteTarget(tag);
                                                setGlobalTagDeleteConfirmText("");
                                            }}
                                            className="text-black/20 hover:text-red-600 transition-colors ml-0.5"
                                            title="Delete globally (admin only)"
                                            data-testid={`delete-tag-global-${tag.id}`}
                                        >
                                            <AlertTriangle className="w-3 h-3" />
                                        </button>
                                    )}
                                </span>
                            ))}
                            {(talent.tags || []).length === 0 && (
                                <p className="text-xs text-black/30 italic">No tags assigned yet.</p>
                            )}
                        </div>

                        {/* Searchable Tag Autocomplete */}
                        {isEditing && (
                            <div className="relative max-w-md">
                                <div className="relative">
                                    <input
                                        type="text"
                                        value={tagSearch}
                                        onChange={(e) => {
                                            setTagSearch(e.target.value);
                                            setIsTagDropdownOpen(true);
                                        }}
                                        onFocus={() => setIsTagDropdownOpen(true)}
                                        placeholder="Search or create tags..."
                                        maxLength={80}
                                        data-testid="tag-input"
                                        className="w-full bg-transparent border-b border-[#eaeaea] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                                    />
                                    {tagSearch && (
                                        <button
                                            type="button"
                                            onClick={() => setTagSearch("")}
                                            className="absolute right-1 top-2.5 text-black/30 hover:text-black"
                                        >
                                            <X className="w-3.5 h-3.5" />
                                        </button>
                                    )}
                                </div>

                                {isTagDropdownOpen && (
                                    <>
                                        <div 
                                            className="fixed inset-0 z-10" 
                                            onClick={() => setIsTagDropdownOpen(false)} 
                                        />
                                        <div className="absolute left-0 right-0 mt-1 max-h-60 overflow-y-auto bg-white border border-[#eaeaea] rounded-xl shadow-xl z-20 divide-y divide-black/[0.04] tg-scroll">
                                            {(() => {
                                                const filtered = allTags
                                                    .filter(t => !(talent.tags || []).some(tt => tt.id === t.id))
                                                    .filter(t => t.name.toLowerCase().includes(tagSearch.toLowerCase()));
                                                    
                                                return (
                                                    <>
                                                        {filtered.map(tag => (
                                                            <button
                                                                key={tag.id}
                                                                type="button"
                                                                onClick={() => {
                                                                    assignTag(tag);
                                                                    setTagSearch("");
                                                                    setIsTagDropdownOpen(false);
                                                                }}
                                                                className="w-full text-left px-4 py-2.5 text-xs text-black/70 hover:bg-black/[0.03] hover:text-black transition-colors flex items-center justify-between"
                                                            >
                                                                <span>{tag.name}</span>
                                                                <span className="text-[10px] text-black/35 font-medium">Add existing</span>
                                                            </button>
                                                        ))}
                                                        {tagSearch.trim() && !allTags.some(t => t.name.toLowerCase() === tagSearch.trim().toLowerCase()) && (
                                                            <button
                                                                type="button"
                                                                onClick={async () => {
                                                                    setTagSaving(true);
                                                                    try {
                                                                        const { data } = await adminApi.post("/tags", { name: tagSearch.trim() });
                                                                        const newTag = data.tag;
                                                                        if (data.created) {
                                                                            setAllTags(prev => [...prev, newTag].sort((a, b) => a.name.localeCompare(b.name)));
                                                                        }
                                                                        await adminApi.post(`/talents/${id}/tag/${newTag.id}`);
                                                                        const updated = [...(talent.tags || []), { id: newTag.id, name: newTag.name }];
                                                                        updateTalent({ tags: updated });
                                                                        setOriginalTalent(prev => ({ ...prev, tags: updated }));
                                                                        toast.success(`Tag "${newTag.name}" created and assigned`);
                                                                        setTagSearch("");
                                                                        setIsTagDropdownOpen(false);
                                                                    } catch (e) {
                                                                        toast.error(e?.response?.data?.detail || "Failed to create tag");
                                                                    } finally {
                                                                        setTagSaving(false);
                                                                    }
                                                                }}
                                                                className="w-full text-left px-4 py-2.5 text-xs font-semibold text-emerald-600 hover:bg-emerald-50 transition-colors flex items-center justify-between"
                                                            >
                                                                <span>Create new: "{tagSearch.trim()}"</span>
                                                                <Plus className="w-3.5 h-3.5" />
                                                            </button>
                                                        )}
                                                        {filtered.length === 0 && !tagSearch.trim() && (
                                                            <div className="px-4 py-3 text-xs text-black/40 italic text-center">
                                                                All existing tags assigned
                                                            </div>
                                                        )}
                                                    </>
                                                );
                                            })()}
                                        </div>
                                    </>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            </section>

            {/* Global tag delete confirmation modal */}
            {globalTagDeleteTarget && (
                <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm" data-testid="global-tag-delete-modal">
                    <div className="bg-white rounded-2xl p-6 md:p-8 max-w-md w-full shadow-2xl border border-[#eaeaea]">
                        <div className="flex items-start gap-3 mb-4">
                            <div className="w-9 h-9 rounded-full bg-red-50 flex items-center justify-center shrink-0">
                                <AlertTriangle className="w-4 h-4 text-red-600" />
                            </div>
                            <div>
                                <p className="font-semibold text-black/90 text-sm">Delete tag globally?</p>
                                <p className="text-xs text-black/55 mt-1 leading-relaxed">
                                    This will permanently delete <strong>"{globalTagDeleteTarget.name}"</strong> from the global tag library and remove it from <em>every talent profile</em>. This cannot be undone.
                                </p>
                            </div>
                        </div>
                        <div className="mb-4">
                            <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2">
                                Type DELETE to confirm
                            </label>
                            <input
                                type="text"
                                value={globalTagDeleteConfirmText}
                                onChange={(e) => setGlobalTagDeleteConfirmText(e.target.value)}
                                placeholder="DELETE"
                                data-testid="global-tag-delete-confirm-input"
                                className="w-full border border-black/[0.12] rounded-lg px-4 py-2.5 text-sm text-black/85 focus:border-red-400 outline-none transition-colors"
                            />
                        </div>
                        <div className="flex gap-2 justify-end">
                            <button
                                type="button"
                                onClick={() => { setGlobalTagDeleteTarget(null); setGlobalTagDeleteConfirmText(""); }}
                                className="px-4 py-2 text-xs border border-[#eaeaea] rounded-lg text-black/60 hover:text-black transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={confirmDeleteGlobalTag}
                                disabled={globalTagDeleteConfirmText !== "DELETE"}
                                data-testid="global-tag-delete-confirm-btn"
                                className="px-4 py-2 text-xs bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-40"
                            >
                                Delete globally
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Media */}

            {isEdit && (
                <>
                    {[
                        {
                            key: "indian",
                            label: "Indian Look Images",
                            accept: "image/jpeg,image/png,image/webp,image/jpg",
                            isVideo: false,
                        },
                        {
                            key: "western",
                            label: "Western Look Images",
                            accept: "image/jpeg,image/png,image/webp,image/jpg",
                            isVideo: false,
                        },
                        {
                            key: "portfolio",
                            label: "Additional Portfolio",
                            accept: "image/jpeg,image/png,image/webp,image/jpg",
                            isVideo: false,
                        },
                        {
                            key: "video",
                            label: "Introduction Video",
                            accept: "video/mp4,video/quicktime,video/webm",
                            isVideo: true,
                        },
                    ].map((cat) => (
                        <section
                            key={cat.key}
                            className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mb-6"
                            data-testid={`media-section-${cat.key}`}
                        >
                            <div className="flex items-center justify-between mb-6">
                                <p className="eyebrow">{cat.label}</p>
                                {cat.key !== "video" && isEditing && (
                                    <button
                                        onClick={() => fileRefs.current[cat.key]?.click()}
                                        disabled={uploading === cat.key}
                                        data-testid={`upload-${cat.key}-btn`}
                                        className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-[#eaeaea] hover:border-[#d4d4d4] rounded-md text-black/70 hover:text-black transition-colors"
                                    >
                                        {uploading === cat.key ? (
                                            <Loader2 className="w-3 h-3 animate-spin" />
                                        ) : (
                                            <Upload className="w-3 h-3" />
                                        )}
                                        Upload
                                    </button>
                                )}
                                <input
                                    ref={setFileRef(cat.key)}
                                    type="file"
                                    accept={cat.accept}
                                    multiple={!cat.isVideo}
                                    className="hidden"
                                    onChange={(e) => {
                                        if (e.target.files?.length)
                                            uploadFiles(
                                                Array.from(e.target.files),
                                                cat.key,
                                            );
                                        e.target.value = "";
                                    }}
                                />
                            </div>
                            {cat.key === "video" ? (
                                (() => {
                                    const videoMedia = mediaBy("video");
                                    if (videoMedia.length === 0) {
                                        return isEditing ? (
                                            <div 
                                                onClick={() => fileRefs.current.video?.click()}
                                                className="border-2 border-dashed border-[#eaeaea] hover:border-black/35 rounded-xl p-8 text-center cursor-pointer transition-colors flex flex-col items-center justify-center gap-2 min-h-[140px]"
                                            >
                                                <div className="w-10 h-10 rounded-full bg-black/5 flex items-center justify-center text-black/50">
                                                    {uploading === "video" ? <Loader2 className="w-5 h-5 animate-spin" /> : <Upload className="w-5 h-5" />}
                                                </div>
                                                <div>
                                                    <p className="text-xs font-semibold text-black/70">Upload Audition / Intro Video</p>
                                                    <p className="text-[10px] text-black/40 mt-1">Supports MP4, MOV up to 200MB</p>
                                                </div>
                                            </div>
                                        ) : (
                                            <div className="border-2 border-dashed border-black/[0.06] rounded-xl p-8 text-center flex flex-col items-center justify-center gap-2 min-h-[140px] opacity-50">
                                                <div className="w-10 h-10 rounded-full bg-black/5 flex items-center justify-center text-black/30">
                                                    <Upload className="w-5 h-5" />
                                                </div>
                                                <p className="text-xs text-black/40">No video uploaded</p>
                                            </div>
                                        );
                                    }
                                    const m = videoMedia[0];
                                    const formattedUploadDate = m.created_at 
                                        ? new Date(m.created_at).toLocaleDateString("en-US", { day: 'numeric', month: 'short', year: 'numeric' })
                                        : "Recently";
                                    return (
                                        <div className="flex flex-col sm:flex-row gap-6 items-start sm:items-center">
                                            {/* Left: Thumbnail Card */}
                                            <div 
                                                onClick={() => {
                                                    setLightboxCategory("video");
                                                    setLightboxIndex(0);
                                                }}
                                                className={`relative w-full sm:w-64 aspect-video border border-[#eaeaea] rounded-xl overflow-hidden cursor-pointer hover:shadow-md transition-shadow group shrink-0 ${!(m.poster_url || m.thumbnail_url) ? "bg-black/5" : ""}`}
                                            >
                                                {m.poster_url || m.thumbnail_url ? (
                                                    <img
                                                        src={m.poster_url || m.thumbnail_url}
                                                        alt="Video Preview"
                                                        loading="lazy"
                                                        className="w-full h-full object-cover"
                                                    />
                                                ) : (
                                                    <div className="w-full h-full flex items-center justify-center bg-black/5">
                                                        <Play className="w-8 h-8 text-black/40" />
                                                    </div>
                                                )}
                                                {/* Play overlay icon */}
                                                <div className="absolute inset-0 flex items-center justify-center bg-black/10 group-hover:bg-black/30 transition-colors">
                                                    <div className="w-10 h-10 rounded-full bg-white/95 flex items-center justify-center shadow-lg transition-transform group-hover:scale-110">
                                                        <Play className="w-4 h-4 fill-black text-black ml-0.5" />
                                                    </div>
                                                </div>
                                                {/* Duration badge */}
                                                {m.duration && (
                                                    <div className="absolute top-2.5 right-2.5 bg-black/75 backdrop-blur-sm text-[10px] text-white font-medium px-2 py-0.5 rounded shadow-sm z-10">
                                                        {formatDuration(m.duration)}
                                                    </div>
                                                )}
                                            </div>

                                            {/* Right: Info Block */}
                                            <div className="flex-1 min-w-0">
                                                <h3 className="font-semibold text-base text-black/85 leading-snug">Introduction Video</h3>
                                                <div className="mt-2 space-y-1 text-xs text-black/50">
                                                    <p><span className="font-medium text-black/40">Duration:</span> {m.duration ? formatDuration(m.duration) : "—"}</p>
                                                    <p><span className="font-medium text-black/40">Uploaded:</span> {formattedUploadDate}</p>
                                                </div>
                                                <div className="mt-4 flex flex-wrap gap-2">
                                                    {isEditing && (
                                                        <button
                                                            type="button"
                                                            onClick={() => fileRefs.current.video?.click()}
                                                            className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] rounded-lg text-xs font-medium text-black/70 hover:text-black transition-colors"
                                                        >
                                                            Replace
                                                        </button>
                                                    )}
                                                    {isEditing && (
                                                        <button
                                                            type="button"
                                                            onClick={() => {
                                                                setMediaToRemove(m.id);
                                                                setConfirmRemoveOpen(true);
                                                            }}
                                                            className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#eaeaea] hover:border-red-600/20 rounded-lg text-xs font-medium text-black/70 hover:border-red-600 hover:text-red-600 transition-colors"
                                                        >
                                                            Delete
                                                        </button>
                                                    )}
                                                    <button
                                                        type="button"
                                                        onClick={() => {
                                                            setLightboxCategory("video");
                                                            setLightboxIndex(0);
                                                        }}
                                                        className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-black text-white hover:bg-black/90 rounded-lg text-xs font-medium transition-colors"
                                                    >
                                                        Preview
                                                    </button>
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })()
                            ) : (
                                <div className="grid grid-cols-3 md:grid-cols-5 lg:grid-cols-6 gap-3">
                                    {mediaBy(cat.key).map((m, idx) => (
                                        <div
                                            key={m.id}
                                            onClick={() => {
                                                setLightboxCategory(cat.key);
                                                setLightboxIndex(idx);
                                            }}
                                            className="relative group aspect-square bg-[#fafaf8] border border-[#eaeaea] rounded-lg overflow-hidden cursor-zoom-in"
                                        >
                                            {(() => {
                                                const isVideo = m.content_type?.startsWith("video") || m.category === "video" || m.resource_type === "video";
                                                return isVideo ? (
                                                    <div className="relative w-full h-full">
                                                        {m.poster_url ? (
                                                        <img
                                                            src={m.poster_url}
                                                            alt="Video Preview"
                                                            loading="lazy"
                                                            className="w-full h-full object-cover"
                                                        />
                                                    ) : (
                                                        <div className="w-full h-full flex items-center justify-center bg-black/5">
                                                            <Play className="w-6 h-6 text-black/40" />
                                                        </div>
                                                    )}
                                                    {/* Play overlay icon */}
                                                    <div className="absolute inset-0 flex items-center justify-center bg-black/10 group-hover:bg-black/30 transition-colors">
                                                        <div className="w-9 h-9 rounded-full bg-white/95 flex items-center justify-center shadow-md transition-transform group-hover:scale-110">
                                                            <Play className="w-3.5 h-3.5 fill-black text-black ml-0.5" />
                                                        </div>
                                                    </div>
                                                    {/* Duration badge */}
                                                    {m.duration && (
                                                        <div className="absolute bottom-1.5 right-1.5 bg-black/70 backdrop-blur-sm text-[9px] text-white font-medium px-1.5 py-0.5 rounded shadow-sm">
                                                            {formatDuration(m.duration)}
                                                        </div>
                                                    )}
                                                </div>
                                            ) : (
                                                <img
                                                    src={m.url}
                                                    alt=""
                                                    loading="lazy" // ISSUE 11: Lazy loading
                                                    className="w-full h-full object-cover"
                                                />
                                            );
                                            })()}
                                            <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-150 bg-black/40 flex items-center justify-center gap-2">
                                                {isEditing && !cat.isVideo && (
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            setCover(m.id);
                                                        }}
                                                        title="Set cover"
                                                        className="p-1.5 bg-white/20 hover:bg-white/30 rounded-md transition-colors"
                                                    >
                                                        <Star
                                                            className={`w-3.5 h-3.5 ${talent.cover_media_id === m.id ? "fill-black text-black" : "text-white"}`}
                                                        />
                                                    </button>
                                                )}
                                                {isEditing && (
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            setMediaToRemove(m.id);
                                                            setConfirmRemoveOpen(true);
                                                        }}
                                                        title="Delete"
                                                        className="p-1.5 bg-white/20 hover:bg-red-600/80 rounded-md transition-colors"
                                                    >
                                                        <Trash2 className="w-3.5 h-3.5 text-white" />
                                                    </button>
                                                )}
                                            </div>
                                            {talent.cover_media_id ===
                                                m.id && (
                                                <div className="absolute top-1 left-1 bg-black text-white text-[9px] px-1.5 py-0.5 tracking-widest uppercase rounded">
                                                    Cover
                                                </div>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </section>
                    ))}
                </>
            )}
            {!isEdit && (
                <p className="text-xs text-black/45 italic">
                    Save this talent first to start uploading media.
                </p>
            )}

            {/* Unified communication timeline (Slice 4 / Feature 2) */}
            {isEdit && id && (
                <div className="mt-8 bg-white rounded-xl border border-[#eaeaea] p-5">
                    <CommTimeline subjectType="TALENT" subjectId={id} />
                </div>
            )}

            <ConfirmDeleteDialog
                open={confirmRemoveOpen}
                title={`Remove this media?`}
                description="This will permanently delete this media file from the talent's portfolio. This action cannot be undone."
                confirmLabel="Remove media"
                typeToConfirm="REMOVE"
                onCancel={() => {
                    setConfirmRemoveOpen(false);
                    setMediaToRemove(null);
                }}
                onConfirm={removeMedia}
            />
            
            <ConfirmDeleteDialog
                open={confirmDeleteOpen}
                title={`Delete "${talent.name || "this talent"}"?`}
                description="This permanently removes the talent record and all their portfolio media. Any submissions linked to this talent remain (they live on the project). This cannot be undone."
                confirmLabel="Delete talent"
                typeToConfirm="DELETE"
                onCancel={() => setConfirmDeleteOpen(false)}
                onConfirm={deleteTalent}
            />

            {/* Sticky Save Changes Bar */}
            {isDirty && (
                <div className="fixed bottom-4 left-4 right-4 z-40 bg-white border border-[#eaeaea] shadow-2xl rounded-xl p-4 flex flex-row items-center justify-between gap-4 max-w-sm md:max-w-none w-[calc(100vw-2rem)] md:w-auto md:fixed md:top-4 md:right-4 md:bottom-auto md:left-auto md:shadow-lg animate-in fade-in slide-in-from-bottom-5 duration-200">
                    <div className="flex flex-col min-w-0 pr-2">
                        <span className="text-xs font-semibold text-black/85">Unsaved Changes</span>
                        <span className="text-[10px] text-black/45 truncate">You have modified this profile</span>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        <button
                            type="button"
                            onClick={() => setTalent(originalTalent)}
                            className="px-3 py-2 text-xs border border-[#eaeaea] hover:bg-black/5 rounded-lg text-black/60 transition-colors"
                        >
                            Discard
                        </button>
                        <button
                            type="button"
                            onClick={save}
                            disabled={saving}
                            className="inline-flex items-center gap-1.5 bg-black text-white px-4 py-2 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors"
                        >
                            {saving && <Loader2 className="w-3 h-3 animate-spin" />}
                            Save
                        </button>
                    </div>
                </div>
            )}

            {/* Fullscreen Lightbox */}
            {lightboxIndex !== null && lightboxCategory !== null && (
                <div
                    className="fixed inset-0 z-50 bg-black/95 backdrop-blur-md flex flex-col items-center justify-between select-none"
                    onClick={() => {
                        setLightboxIndex(null);
                        setLightboxCategory(null);
                    }}
                    onTouchStart={handleTouchStart}
                    onTouchMove={handleTouchMove}
                    onTouchEnd={handleTouchEnd}
                >
                    {/* Header */}
                    <div className="w-full p-4 flex items-center justify-between text-white z-10">
                        <span className="text-xs font-semibold tracking-wider uppercase opacity-65">
                            {lightboxCategory.replace(/_/g, " ")} Look ({lightboxIndex + 1} / {mediaBy(lightboxCategory).length})
                        </span>
                        <button
                            type="button"
                            onClick={() => {
                                setLightboxIndex(null);
                                setLightboxCategory(null);
                            }}
                            className="p-2 hover:bg-white/10 rounded-full transition-colors"
                        >
                            <X className="w-5 h-5" />
                        </button>
                    </div>

                    {/* Main Content */}
                    <div
                        className="relative flex-1 w-full flex items-center justify-center p-4"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {(() => {
                            const items = mediaBy(lightboxCategory);
                            const currentItem = items[lightboxIndex];
                            if (!currentItem) return null;

                            const isVideo = currentItem.content_type?.startsWith("video") || currentItem.category === "video" || currentItem.resource_type === "video";
                            if (isVideo) {
                                return (
                                    <video
                                        src={currentItem.url}
                                        controls
                                        autoPlay
                                        className="max-h-[75vh] max-w-[90vw] rounded-lg shadow-2xl"
                                    />
                                );
                            } else {
                                return (
                                    <img
                                        src={currentItem.url}
                                        alt=""
                                        className="max-h-[75vh] max-w-[90vw] object-contain rounded-lg shadow-2xl"
                                    />
                                );
                            }
                        })()}
                    </div>

                    {/* Navigation Bar */}
                    <div className="w-full p-6 flex items-center justify-between max-w-md mx-auto text-white z-10">
                        <button
                            type="button"
                            onClick={(e) => {
                                e.stopPropagation();
                                const items = mediaBy(lightboxCategory);
                                setLightboxIndex((prev) => (prev > 0 ? prev - 1 : items.length - 1));
                            }}
                            className="px-4 py-2 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-sm transition-colors"
                        >
                            Previous
                        </button>
                        <span className="text-xs opacity-50">Swipe to navigate / Arrows</span>
                        <button
                            type="button"
                            onClick={(e) => {
                                e.stopPropagation();
                                const items = mediaBy(lightboxCategory);
                                setLightboxIndex((prev) => (prev < items.length - 1 ? prev + 1 : 0));
                            }}
                            className="px-4 py-2 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-sm transition-colors"
                        >
                            Next
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
