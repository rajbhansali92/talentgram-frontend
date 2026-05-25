/**
 * Centralized utility helpers for media transformation, poster/thumbnail resolution,
 * and media type detection across the frontend.
 */

/**
 * Resolves the optimal lightweight thumbnail URL for a media item.
 * Falls back to the raw URL if no optimized thumbnail exists.
 */
export function thumbnailUrl(media) {
    return media?.thumbnail_url || media?.url || null;
}

/**
 * Resolves the optimized poster thumbnail URL for a video media item.
 */
export function posterUrl(media) {
    return media?.poster_url || null;
}

/**
 * Returns true if the media item represents a video.
 */
export function isVideo(media) {
    return media?.resource_type === "video" || media?.content_type?.startsWith("video/") || false;
}
