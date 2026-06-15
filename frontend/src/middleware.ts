import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(req: NextRequest) {
    const url = req.nextUrl.clone();
    const hostname = req.headers.get('host') || '';

    // Ignore static assets, APIs, and Next.js internal calls or opengraph-image endpoints
    if (
        url.pathname.startsWith('/_next') ||
        url.pathname.startsWith('/api') ||
        url.pathname.includes('.') ||
        url.pathname.endsWith('/opengraph-image')
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

    // Strip optional www. prefix for subdomain matching
    let cleanHostname = hostname;
    if (cleanHostname.startsWith('www.')) {
        cleanHostname = cleanHostname.substring(4);
    }

    // Determine subdomain prefix
    const subdomains = ['apply', 'submit', 'review', 'links'];
    let subdomain = '';

    for (const sub of subdomains) {
        if (cleanHostname.startsWith(`${sub}.`)) {
            subdomain = sub;
            break;
        }
    }

    // Rewrite based on subdomain (using actual URL paths, avoiding route-group folders in rewrites)
    if (subdomain === 'apply') {
        let cleanPath = url.pathname;
        if (cleanPath === '/apply') {
            cleanPath = '/';
        }
        url.pathname = `/apply${cleanPath}`;
    } else if (subdomain === 'submit') {
        let cleanPath = url.pathname;
        if (cleanPath.startsWith('/submit/')) {
            cleanPath = cleanPath.substring('/submit'.length);
        }
        url.pathname = `/submit${cleanPath}`;
    } else if (subdomain === 'review') {
        let cleanPath = url.pathname;
        if (cleanPath === '/') {
            cleanPath = '/admin';
        }
        url.pathname = `${cleanPath}`;
    } else if (subdomain === 'links') {
        let cleanPath = url.pathname;
        if (cleanPath.startsWith('/l/')) {
            cleanPath = cleanPath.substring('/l'.length);
        }
        url.pathname = `/l${cleanPath}`;
    } else {
        // Root / Landing domain
        url.pathname = `${url.pathname}`;
    }

    return NextResponse.rewrite(url);
}
