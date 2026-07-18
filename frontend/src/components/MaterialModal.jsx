import React from "react";
import {
    X,
    FileText,
    Music,
    PlayCircle,
    Trash2,
} from "lucide-react";
import { IMAGE_URL } from "@/lib/api";

/**
 * Full-screen modal viewer for project audition material.
 * Used on both admin ProjectEdit and public SubmissionPage.
 * Theme-aware: respects light/dark mode via shadcn tokens (bg-background, text-foreground, etc.)
 *
 * Props:
 *  - project: { brand_name, materials: [{id, category, url, content_type, original_filename}], video_links: [] }
 *    Each material carries a canonical Cloudinary `url` field (post-migration shape).
 *  - onClose: fn
 *  - onRemove?: (mid) => void   // admin-only; if omitted, trash buttons are hidden
 */
export default function MaterialModal({ project, onClose, onRemove }) {
    const materials = Array.isArray(project?.materials) ? project.materials : [];
    const videos = Array.isArray(project?.video_links) ? project.video_links : [];
    const byCat = (c) => materials.filter((m) => m.category === c);

    // Lock background scrolling on mount and listen for Escape key
    React.useEffect(() => {
        const originalStyle = window.getComputedStyle(document.body).overflow;
        document.body.style.overflow = "hidden";
        
        const handleKeyDown = (e) => {
            if (e.key === "Escape") onClose();
        };
        window.addEventListener("keydown", handleKeyDown);
        
        return () => {
            document.body.style.overflow = originalStyle;
            window.removeEventListener("keydown", handleKeyDown);
        };
    }, [onClose]);

    return (
        <div
            className="fixed inset-0 z-50 bg-background/80 backdrop-blur-xl overflow-y-auto animate-in fade-in duration-200"
            data-testid="audition-material-modal"
            role="dialog"
            aria-modal="true"
            aria-label="Audition Materials Viewer"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-11 h-11 border border-border hover:border-foreground rounded-sm flex items-center justify-center bg-background/80 text-foreground shadow-sm focus:outline-none focus:ring-2 focus:ring-ring"
                aria-label="Close Materials Viewer"
                data-testid="material-modal-close-btn"
            >
                <X className="w-5 h-5" />
            </button>
            <div className="max-w-5xl mx-auto px-5 md:px-12 py-10 md:py-14 text-foreground">
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
                            href={m.url && !m.url.toLowerCase().endsWith(".pdf") ? `${m.url}.pdf` : m.url}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-3 flex-1"
                        >
                            <FileText className="w-5 h-5 text-muted-foreground" />
                            <div className="min-w-0">
                                <div className="text-sm truncate">
                                    {m.original_filename || "script.pdf"}
                                </div>
                                <div className="text-[10px] tg-mono text-muted-foreground">
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
                            href={IMAGE_URL(m)}
                            target="_blank"
                            rel="noreferrer"
                            className="block aspect-square bg-muted overflow-hidden"
                        >
                            <img
                                src={IMAGE_URL(m)}
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
                        <div className="flex items-center gap-3 flex-1 bg-muted p-3 rounded-lg">
                            <Music className="w-5 h-5 text-muted-foreground shrink-0" />
                            <audio
                                src={m.url}
                                controls
                                className="w-full max-w-md"
                            />
                        </div>
                    )}
                />

                <Group
                    title="Reference Videos"
                    items={byCat("video_file")}
                    onRemove={onRemove}
                    render={(m) => (
                        <div className="flex-1 bg-muted p-2 rounded-lg">
                            <video
                                src={m.url}
                                controls
                                preload="metadata"
                                className="w-full rounded-sm bg-black"
                            />
                            {m.original_filename && (
                                <div className="text-[10px] tg-mono text-muted-foreground mt-1.5 truncate">
                                    {m.original_filename}
                                </div>
                            )}
                        </div>
                    )}
                />

                {videos.length > 0 && (
                    <div className="mb-10">
                        <p className="eyebrow mb-4">Video Links</p>
                        <div className="space-y-2">
                            {videos.map((v) => (
                                <a
                                    key={v}
                                    href={v}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="flex items-center gap-3 p-3 border border-border hover:border-foreground/40 transition-all"
                                >
                                    <PlayCircle className="w-5 h-5 text-muted-foreground" />
                                    <span className="text-sm tg-mono truncate">
                                        {v}
                                    </span>
                                </a>
                            ))}
                        </div>
                    </div>
                )}

                {materials.length === 0 && videos.length === 0 && (
                    <p className="text-muted-foreground text-sm">
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
                                : "flex items-center gap-3 p-3 border border-border"
                        }
                    >
                        {render(m)}
                        {onRemove && (
                            <button
                                onClick={() => onRemove(m.id)}
                                aria-label="Delete material"
                                className={
                                    grid
                                        ? "absolute top-2 right-2 opacity-100 lg:opacity-0 lg:group-hover:opacity-100 w-11 h-11 flex items-center justify-center bg-background/90 text-foreground hover:bg-red-600 hover:text-white rounded-sm transition-all shadow-sm focus:outline-none focus:ring-2 focus:ring-red-500"
                                        : "w-11 h-11 flex items-center justify-center text-muted-foreground hover:text-red-600 focus:outline-none focus:ring-2 focus:ring-red-500"
                                }
                            >
                                <Trash2 className="w-4 h-4" />
                            </button>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}
