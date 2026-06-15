// Simple sanitizer to clean Open Graph metadata strings of script tags, control codes, and HTML
export function sanitizeMetadata(str) {
    if (!str || typeof str !== 'string') return '';
    return str
        .replace(/<script[^>]*>([\s\S]*?)<\/script>/gi, '') // Strip script tags
        .replace(/<\/?[^>]+(>|$)/g, '')                     // Strip other HTML elements
        .replace(/[\x00-\x1F\x7F-\x9F]/g, '')                // Remove ASCII/unicode control characters
        .trim();
}

export function formatTalentLocation(location) {
    if (!location) return "";
    
    if (typeof location === "string") {
        return location.trim();
    }
    
    if (Array.isArray(location)) {
        return location
            .map(loc => formatTalentLocation(loc))
            .filter(Boolean)
            .join("; ");
    }
    
    if (typeof location === "object" && location !== null) {
        const { city, country } = location;
        const parts = [city, country].map(s => (s || "").trim()).filter(Boolean);
        return parts.join(", ");
    }
    
    return "";
}

export function formatLocation(location) {
    return formatTalentLocation(location);
}

