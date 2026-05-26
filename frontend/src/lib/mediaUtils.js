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

/**
 * Resolves the optimal talent cover/thumbnail media object following the strict priority:
 * 1. profile/headshot image (using cover_media_id matching in media list, or category "profile"/"headshot")
 * 2. first portfolio image (category "portfolio")
 * 3. Indian look image (category "indian")
 * 4. Western look image (category "western")
 * 5. elegant fallback url (talent.cover_url or talent.image_url)
 */
export function resolveTalentCover(talent) {
    if (!talent) return null;
    const media = talent.media || [];
    
    // 1. profile/headshot image
    if (talent.cover_media_id) {
        const coverMedia = media.find(m => m.id === talent.cover_media_id);
        if (coverMedia?.url) return coverMedia;
    }
    
    const profileMedia = media.find(m => m.category === "profile" || m.category === "headshot");
    if (profileMedia?.url) return profileMedia;
    
    // 2. first portfolio image
    const portfolioMedia = media.find(m => m.category === "portfolio");
    if (portfolioMedia?.url) return portfolioMedia;
    
    // 3. Indian look image
    const indianMedia = media.find(m => m.category === "indian");
    if (indianMedia?.url) return indianMedia;
    
    // 4. Western look image
    const westernMedia = media.find(m => m.category === "western");
    if (westernMedia?.url) return westernMedia;
    
    // Fallback: any other image media item
    const fallbackMedia = media.find(m => m.content_type?.startsWith?.("image/") || m.category === "image");
    if (fallbackMedia?.url) return fallbackMedia;

    // Check direct properties if media list doesn't yield an image (e.g. backend pre-resolved cover)
    if (talent.cover_url) {
        return { url: talent.cover_url, thumbnail_url: talent.cover_url };
    }
    if (talent.image_url) {
        return { url: talent.image_url, thumbnail_url: talent.image_url };
    }
    
    return null;
}
