import { ImageResponse } from 'next/og';

export const runtime = 'edge';

async function getTalentPortfolio(slug) {
    try {
        const res = await fetch(`https://talentgram-app-production.up.railway.app/api/public/links/${slug}`, {
            next: { revalidate: 60 }
        });
        if (!res.ok) return null;
        return await res.json();
    } catch (e) {
        console.error("Error fetching talent in opengraph-image", e);
        return null;
    }
}

export default async function Image({ params }) {
    const { slug } = await params;
    const portfolio = await getTalentPortfolio(slug);
    
    // Extrapolate primary talent profile details
    const talent = portfolio?.talents?.[0] || {};
    const name = talent.name || "Artist Portfolio";
    const category = talent.skills?.join(' | ') || "Featured Talent";
    const location = talent.location || "India ↔ UAE";
    
    // Defensive profile headshot retrieval
    let headshotUrl = talent.image_url || "";
    let headshotLoaded = false;
    let headshotBase64 = null;

    if (headshotUrl) {
        try {
            // Add a timeout helper to prevent slow images from halting Edge function
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 2500);

            const imageRes = await fetch(headshotUrl, {
                signal: controller.signal,
                headers: { 'Accept': 'image/*' }
            });
            clearTimeout(timeoutId);

            if (imageRes.ok) {
                const arrayBuffer = await imageRes.arrayBuffer();
                const buffer = Buffer.from(arrayBuffer);
                headshotBase64 = `data:${imageRes.headers.get('content-type') || 'image/jpeg'};base64,${buffer.toString('base64')}`;
                headshotLoaded = true;
            }
        } catch (e) {
            console.error("Remote talent headshot failed to load. Falling back to high-end typography layout.", e);
        }
    }

    // Dynamic Initials generation for typographic layout fallback
    const initials = name
        .split(' ')
        .map(n => n.charAt(0))
        .join('')
        .toUpperCase()
        .slice(0, 2) || "TG";

    return new ImageResponse(
        (
            <div
                style={{
                    width: '100%',
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'row',
                    backgroundColor: '#0B1F3A',
                    color: '#FFFFFF',
                    fontFamily: 'serif',
                    boxSizing: 'border-box',
                }}
            >
                {/* Left side text details */}
                <div
                    style={{
                        width: headshotLoaded ? '55%' : '100%',
                        height: '100%',
                        display: 'flex',
                        flexDirection: 'column',
                        justifyContent: 'space-between',
                        padding: '80px',
                        boxSizing: 'border-box',
                    }}
                >
                    {/* Top bar header */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        <div style={{ fontSize: '24px', fontWeight: 'bold', letterSpacing: '0.15em' }}>TALENTGRAM</div>
                        <div style={{ fontSize: '10px', letterSpacing: '0.3em', color: 'rgba(255,255,255,0.4)', textTransform: 'uppercase' }}>PREMIUM TALENT NETWORK</div>
                    </div>

                    {/* Content Area */}
                    <div style={{ display: 'flex', flexDirection: 'column', marginTop: '40px', marginBottom: '40px' }}>
                        <div style={{ fontSize: '48px', fontWeight: 'bold', letterSpacing: '0.02em', marginBottom: '16px', lineHeight: 1.1 }}>
                            {name}
                        </div>
                        <div style={{ fontSize: '16px', color: 'rgba(255,255,255,0.7)', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: '8px' }}>
                            {category}
                        </div>
                        <div style={{ fontSize: '14px', color: 'rgba(255,255,255,0.5)', letterSpacing: '0.05em' }}>
                            {location}
                        </div>
                    </div>

                    {/* Footer region indicator */}
                    <div style={{ display: 'flex', fontSize: '11px', color: 'rgba(255,255,255,0.4)', letterSpacing: '0.2em' }}>
                        <span>EXCLUSIVE PORTFOLIO</span>
                    </div>
                </div>

                {/* Right side headshot layout or typography fallback */}
                <div
                    style={{
                        width: headshotLoaded ? '45%' : '0%',
                        height: '100%',
                        display: headshotLoaded ? 'flex' : 'none',
                        backgroundImage: headshotLoaded ? `url(${headshotBase64})` : 'none',
                        backgroundSize: 'cover',
                        backgroundPosition: 'center',
                    }}
                />

                {!headshotLoaded && (
                    <div
                        style={{
                            width: '40%',
                            height: '100%',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            backgroundColor: 'rgba(255, 255, 255, 0.03)',
                            borderLeft: '1px solid rgba(255, 255, 255, 0.05)',
                        }}
                    >
                        <div style={{ fontSize: '120px', fontWeight: 'light', letterSpacing: '0.1em', color: 'rgba(255, 255, 255, 0.1)', fontFamily: 'serif' }}>
                            {initials}
                        </div>
                    </div>
                )}
            </div>
        ),
        {
            width: 1200,
            height: 630,
        }
    );
}
