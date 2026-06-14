import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(req: NextRequest) {
    const url = req.nextUrl.clone();
    const hostname = req.headers.get('host') || '';

    // Ignore static assets, APIs, and Next.js internal calls
    if (
        url.pathname.startsWith('/_next') ||
        url.pathname.startsWith('/api') ||
        url.pathname.includes('.')
    ) {
        return NextResponse.next();
    }

    // Bypass rewrites for common shared pages
    const isCommonRoute = 
        url.pathname.startsWith('/signup') ||
        url.pathname.startsWith('/forgot-password') ||
        url.pathname.startsWith('/reset-password') ||
        url.pathname.startsWith('/google-callback') ||
        url.pathname.startsWith('/portal');

    if (isCommonRoute) {
        return NextResponse.next();
    }

    // Determine subdomain prefix
    const subdomains = ['apply', 'submit', 'review', 'links'];
    let subdomain = '';

    for (const sub of subdomains) {
        if (hostname.startsWith(`${sub}.`)) {
            subdomain = sub;
            break;
        }
    }

    // Route requests to respective Next.js Route Groups
    if (subdomain === 'apply') {
        let cleanPath = url.pathname;
        if (cleanPath === '/apply') {
            cleanPath = '/';
        }
        url.pathname = `/(apply)/apply-portal${cleanPath}`;
    } else if (subdomain === 'submit') {
        let cleanPath = url.pathname;
        if (cleanPath.startsWith('/submit/')) {
            cleanPath = cleanPath.substring('/submit'.length);
        }
        url.pathname = `/(submit)/submit-portal${cleanPath}`;
    } else if (subdomain === 'review') {
        let cleanPath = url.pathname;
        if (cleanPath === '/') {
            cleanPath = '/admin';
        }
        url.pathname = `/(review)${cleanPath}`;
    } else if (subdomain === 'links') {
        let cleanPath = url.pathname;
        if (cleanPath.startsWith('/l/')) {
            cleanPath = cleanPath.substring('/l'.length);
        }
        url.pathname = `/(links)/links-portal${cleanPath}`;
    } else {
        url.pathname = `/(landing)${url.pathname}`;
    }

    return NextResponse.rewrite(url);
}
