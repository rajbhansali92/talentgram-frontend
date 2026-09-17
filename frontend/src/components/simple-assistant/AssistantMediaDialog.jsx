import React, { useEffect } from "react";
import HlsVideo from "@/components/HlsVideo";

/**
 * Plays an EXISTING media item (introduction / audition take) inside the
 * Command Centre. Read-only: it renders the existing HlsVideo player
 * against the URL already stored on the submission. No upload, no
 * transform, no mutation.
 */
export default function AssistantMediaDialog({ media, onClose }) {
    useEffect(() => {
        if (!media) return undefined;
        const onKey = (e) => e.key === "Escape" && onClose();
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [media, onClose]);

    if (!media) return null;

    return (
        <div
            className="tga-modal"
            role="dialog"
            aria-modal="true"
            aria-label={media.title || "Media player"}
            data-testid="assistant-media-dialog"
            onClick={(e) => {
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <div className="tga-modal-card">
                <div className="tga-modal-head">
                    <span>{[media.talentName, media.label].filter(Boolean).join(" — ")}</span>
                    <button
                        type="button"
                        className="tga-modal-close"
                        onClick={onClose}
                        aria-label="Close player"
                        data-testid="assistant-media-close"
                    >
                        ✕
                    </button>
                </div>
                <div className="tga-modal-body">
                    <HlsVideo
                        src={media.url}
                        poster={media.poster_url || undefined}
                        controls
                        autoPlay
                        playsInline
                        data-testid="assistant-media-video"
                    />
                </div>
            </div>
        </div>
    );
}
