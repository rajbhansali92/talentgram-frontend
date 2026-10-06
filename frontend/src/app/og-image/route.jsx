import { NextResponse } from 'next/server';
import { OG_IMAGE_URL } from '@/lib/ogBrand';

// Legacy URL. Pages no longer reference /og-image (they use the static canonical asset in
// lib/ogBrand.js), but link previews already cached by WhatsApp/Meta may still point here —
// send them to the canonical Talentgram logo instead of rendering a second, different image.
export function GET(req) {
    return NextResponse.redirect(new URL(OG_IMAGE_URL, req.url), 307);
}
