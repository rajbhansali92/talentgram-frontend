import React, { useMemo, useState } from "react";
import { toast } from "sonner";
import { adminApi, FILE_URL } from "@/lib/api";
import VisibilityToggles, {
    DEFAULT_VISIBILITY,
} from "@/components/VisibilityToggles";
import {
    X,
    Check,
    Loader2,
    Image as ImageIcon,
    Video,
    Sparkles,
} from "lucide-react";

/**
 * Modal for creating a client-ready portfolio link from APPROVED submissions.
 * Multi-select + reuse existing visibility toggles + server-side talent creation.
 */
export default function ForwardToLinkModal({
    project,
    submissions,
    onClose,
    onDone,
}) {
    const approved = useMemo(
        () =>
            (submissions || []).filter(
                (s) => s.decision === "approved" && s.status === "submitted",
            ),
        [submissions],
    );
    const [selected, setSelected] = useState(() => new Set(approved.map((s) => s.id)));
    const [visibility, setVisibility] = useState({ ...DEFAULT_VISIBILITY });
    const [saving, setSaving] = useState(false);

    const toggle = (id) => {
        const n = new Set(selected);
        if (n.has(id)) n.delete(id);
        else n.add(id);
        setSelected(n);
    };

    const submit = async () => {
        if (selected.size === 0) {
            toast.error("Select at least one talent");
            return;
        }
        setSaving(true);
        try {
            const { data } = await adminApi.post(
                `/projects/${project.id}/forward-to-link`,
                {
                    submission_ids: Array.from(selected),
                    visibility,
                },
            );
            toast.success("Client link generated");
            onDone?.(data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to generate link");
        } finally {
            setSaving(false);
        }
    };

    const thumbOf = (s) => {
        const img = (s.media || []).find((m) => m.category === "image");
        return img ? FILE_URL(img.storage_path) : null;
    };

    return (
        <div
            className="fixed inset-0 z-50 bg-black/95 backdrop-blur-xl overflow-y-auto"
            data-testid="forward-to-link-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50"
            >
                <X className="w-4 h-4" />
            </button>
            <div className="max-w-6xl mx-auto px-5 md:px-12 py-10 md:py-14">
                <p className="eyebrow mb-3">Forward to Client</p>
                <h2 className="font-display text-3xl md:text-5xl tracking-tight mb-2">
                    Talentgram × {project.brand_name}
                </h2>
                <p className="text-white/50 text-sm mb-10">
                    Select approved submissions to push into a client-ready
                    portfolio link. Only approved talents appear below.
                </p>

                {approved.length === 0 ? (
                    <div className="border border-white/10 p-10 text-center text-white/50 text-sm">
                        No approved submissions yet. Review submissions and
                        mark as approved first.
                    </div>
                ) : (
                    <div className="grid lg:grid-cols-3 gap-6">
                        {/* Selection grid */}
                        <div className="lg:col-span-2">
                            <div className="flex items-center justify-between mb-4">
                                <p className="eyebrow">
                                    Approved · {selected.size}/{approved.length}{" "}
                                    selected
                                </p>
                                <button
                                    onClick={() =>
                                        setSelected(
                                            selected.size === approved.length
                                                ? new Set()
                                                : new Set(
                                                      approved.map((s) => s.id),
                                                  ),
                                        )
                                    }
                                    className="text-xs text-white/60 hover:text-white"
                                >
                                    {selected.size === approved.length
                                        ? "Clear all"
                                        : "Select all"}
                                </button>
                            </div>
                            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                                {approved.map((s) => {
                                    const active = selected.has(s.id);
                                    const thumb = thumbOf(s);
                                    const imgCount = (s.media || []).filter(
                                        (m) => m.category === "image",
                                    ).length;
                                    const hasIntro = (s.media || []).some(
                                        (m) => m.category === "intro_video",
                                    );
                                    return (
                                        <button
                                            key={s.id}
                                            onClick={() => toggle(s.id)}
                                            data-testid={`forward-select-${s.id}`}
                                            className={`relative text-left border transition-all ${active ? "border-white" : "border-white/10 hover:border-white/30"}`}
                                        >
                                            <div className="aspect-[3/4] bg-[#0a0a0a] overflow-hidden">
                                                {thumb ? (
                                                    <img
                                                        src={thumb}
                                                        alt={s.talent_name}
                                                        loading="lazy"
                                                        className="w-full h-full object-cover"
                                                    />
                                                ) : (
                                                    <div className="w-full h-full flex items-center justify-center text-white/20">
                                                        <ImageIcon className="w-6 h-6" />
                                                    </div>
                                                )}
                                            </div>
                                            <div className="p-3">
                                                <div className="font-display text-base truncate">
                                                    {s.talent_name}
                                                </div>
                                                <div className="flex items-center gap-3 mt-1 text-[10px] tg-mono text-white/50">
                                                    <span className="inline-flex items-center gap-1">
                                                        <Video className="w-3 h-3" />
                                                        {hasIntro ? "intro" : "no intro"}
                                                    </span>
                                                    <span className="inline-flex items-center gap-1">
                                                        <ImageIcon className="w-3 h-3" />
                                                        {imgCount}
                                                    </span>
                                                </div>
                                            </div>
                                            {active && (
                                                <div className="absolute top-2 right-2 w-6 h-6 bg-white text-black flex items-center justify-center rounded-sm">
                                                    <Check className="w-3.5 h-3.5" />
                                                </div>
                                            )}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>

                        {/* Visibility + submit */}
                        <div className="lg:col-span-1 space-y-6">
                            <div className="border border-white/10 p-5">
                                <p className="eyebrow mb-4">
                                    Client Visibility
                                </p>
                                <VisibilityToggles
                                    value={visibility}
                                    onChange={setVisibility}
                                />
                            </div>
                            <button
                                onClick={submit}
                                disabled={saving || selected.size === 0}
                                data-testid="confirm-forward-btn"
                                className="w-full bg-white text-black py-4 rounded-sm text-sm font-medium hover:opacity-90 disabled:opacity-40 inline-flex items-center justify-center gap-2 min-h-[52px]"
                            >
                                {saving ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Sparkles className="w-4 h-4" />
                                )}
                                Generate Client Link ({selected.size})
                            </button>
                            <p className="text-[11px] text-white/40 text-center tg-mono">
                                Link will be titled "Talentgram × {project.brand_name}"
                            </p>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
