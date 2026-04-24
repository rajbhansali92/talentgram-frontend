import React from "react";
import { FILE_URL } from "@/lib/api";
import {
    X,
    FileText,
    Music,
    PlayCircle,
    Trash2,
} from "lucide-react";

/**
 * Full-screen modal viewer for project audition material.
 * Used on both admin ProjectEdit and public SubmissionPage.
 *
 * Props:
 *  - project: { brand_name, materials: [{id, category, storage_path, content_type, original_filename}], video_links: [] }
 *  - onClose: fn
 *  - onRemove?: (mid) => void   // admin-only; if omitted, trash buttons are hidden
 */
export default function MaterialModal({ project, onClose, onRemove }) {
    const materials = project?.materials || [];
    const videos = project?.video_links || [];
    const byCat = (c) => materials.filter((m) => m.category === c);

    return (
        <div
            className="fixed inset-0 z-50 bg-black/90 backdrop-blur-xl overflow-y-auto"
            data-testid="audition-material-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50"
                aria-label="Close"
                data-testid="material-modal-close-btn"
            >
                <X className="w-4 h-4" />
            </button>
            <div className="max-w-5xl mx-auto px-5 md:px-12 py-10 md:py-14">
                <p className="eyebrow mb-3">Audition Material</p>
                <h2 className="font-display text-3xl md:text-5xl tracking-tight mb-8 md:mb-10">
                    {project?.brand_name}
                </h2>

                <Group
                    title="Script (PDF)"
                    items={byCat("script")}
                    onRemove={onRemove}
                    render={(m) => (
                        <a
                            href={FILE_URL(m.storage_path)}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-3 flex-1"
                        >
                            <FileText className="w-5 h-5 text-white/60" />
                            <div className="min-w-0">
                                <div className="text-sm truncate">
                                    {m.original_filename || "script.pdf"}
                                </div>
                                <div className="text-[10px] tg-mono text-white/40">
                                    Open PDF →
                                </div>
                            </div>
                        </a>
                    )}
                />

                <Group
                    title="Images"
                    items={byCat("image")}
                    onRemove={onRemove}
                    grid
                    render={(m) => (
                        <a
                            href={FILE_URL(m.storage_path)}
                            target="_blank"
                            rel="noreferrer"
                            className="block aspect-square bg-[#0a0a0a] overflow-hidden"
                        >
                            <img
                                src={FILE_URL(m.storage_path)}
                                alt=""
                                loading="lazy"
                                className="w-full h-full object-cover"
                            />
                        </a>
                    )}
                />

                <Group
                    title="Audio Notes"
                    items={byCat("audio")}
                    onRemove={onRemove}
                    render={(m) => (
                        <div className="flex items-center gap-3 flex-1">
                            <Music className="w-5 h-5 text-white/60 shrink-0" />
                            <audio
                                src={FILE_URL(m.storage_path)}
                                controls
                                className="w-full max-w-md"
                            />
                        </div>
                    )}
                />

                {videos.length > 0 && (
                    <div className="mb-10">
                        <p className="eyebrow mb-4">Video Links</p>
                        <div className="space-y-2">
                            {videos.map((v, i) => (
                                <a
                                    key={i}
                                    href={v}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="flex items-center gap-3 p-3 border border-white/10 hover:border-white/30 transition-all"
                                >
                                    <PlayCircle className="w-5 h-5 text-white/60" />
                                    <span className="text-sm tg-mono truncate">
                                        {v}
                                    </span>
                                </a>
                            ))}
                        </div>
                    </div>
                )}

                {materials.length === 0 && videos.length === 0 && (
                    <p className="text-white/40 text-sm">
                        No audition materials yet.
                    </p>
                )}
            </div>
        </div>
    );
}

function Group({ title, items, onRemove, render, grid }) {
    if (!items || items.length === 0) return null;
    return (
        <div className="mb-10">
            <p className="eyebrow mb-4">{title}</p>
            <div
                className={
                    grid
                        ? "grid grid-cols-2 md:grid-cols-4 gap-3"
                        : "space-y-2"
                }
            >
                {items.map((m) => (
                    <div
                        key={m.id}
                        className={
                            grid
                                ? "relative group"
                                : "flex items-center gap-3 p-3 border border-white/10"
                        }
                    >
                        {render(m)}
                        {onRemove && (
                            <button
                                onClick={() => onRemove(m.id)}
                                className={
                                    grid
                                        ? "absolute top-2 right-2 opacity-0 group-hover:opacity-100 p-1.5 bg-black/70 hover:bg-[var(--tg-danger)] rounded-sm transition-all"
                                        : "text-white/40 hover:text-[var(--tg-danger)]"
                                }
                            >
                                <Trash2 className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}
