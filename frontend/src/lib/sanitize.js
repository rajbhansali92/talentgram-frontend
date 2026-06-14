// Simple sanitizer to clean Open Graph metadata strings of script tags, control codes, and HTML
export function sanitizeMetadata(str) {
    if (!str || typeof str !== 'string') return '';
    return str
        .replace(/<script[^>]*>([\s\S]*?)<\/script>/gi, '') // Strip script tags
        .replace(/<\/?[^>]+(>|$)/g, '')                     // Strip other HTML elements
        .replace(/[\x00-\x1F\x7F-\x9F]/g, '')                // Remove ASCII/unicode control characters
        .trim();
}
