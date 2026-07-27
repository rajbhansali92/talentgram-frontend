import React from "react";
import { Video as VideoIcon } from "lucide-react";
import { IMAGE_URL } from "@/lib/api";
import { thumbnailUrl, posterUrl } from "@/lib/mediaUtils";

/**
 * Read-only media grid — extracted from the inline Portfolio/Media grids
 * built in PortalProfile.jsx (Phase 2 item 3) so Project Detail (Phase 3
 * item 2) reuses the same rendering instead of duplicating it. No upload
 * logic, no Cloudinary calls of its own — just IMAGE_URL/thumbnailUrl/
 * posterUrl, exactly as PortalProfile.jsx already used them.
 */
export default function MediaGrid({ items, variant = "image" }) {
    if (!items || items.length === 0) return null;

    if (variant === "video") {
        return (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {items.map((m) => (
                    <div key={m.id} className="aspect-square rounded-lg overflow-hidden bg-black/90 border border-black/5 relative">
                        {posterUrl(m) || thumbnailUrl(m) ? (
                            <img
                                src={posterUrl(m) || thumbnailUrl(m)}
                                alt={m.category || "Video"}
                                className="w-full h-full object-cover opacity-80"
                            />
                        ) : (
                            <div className="w-full h-full flex items-center justify-center">
                                <VideoIcon className="w-6 h-6 text-white/50" />
                            </div>
                        )}
                    </div>
                ))}
            </div>
        );
    }

    return (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {items.map((m) => (
                <div key={m.id} className="aspect-square rounded-lg overflow-hidden bg-black/5 border border-black/5">
                    <img src={IMAGE_URL(m)} alt={m.category || "Image"} className="w-full h-full object-cover" />
                </div>
            ))}
        </div>
    );
}
