import { ImageResponse } from 'next/og';
import { logoBase64 } from '@/lib/logoBase64';

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
    const rawName = talent.name || "Artist Portfolio";

    // Format Name to: AARAV M. (First name + space + first letter of last name, uppercase)
    const nameParts = rawName.trim().split(/\s+/);
    let displayName = rawName.toUpperCase();
    if (nameParts.length > 1) {
        const first = nameParts[0].toUpperCase();
        const lastInitial = nameParts[nameParts.length - 1].charAt(0).toUpperCase();
        displayName = `${first} ${lastInitial}.`;
    } else if (nameParts.length === 1) {
        displayName = nameParts[0].toUpperCase();
    }

    const category = talent.skills?.join(' | ') || "Featured Talent";
    
    // Defensive profile headshot retrieval
    let headshotUrl = talent.image_url || "";
    let headshotLoaded = false;
    let headshotBase64 = null;

    if (headshotUrl) {
        try {
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
    const initials = displayName.slice(0, 2);

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
                    {/* Top Bar with Logo Asset */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                        <img
                            src={logoBase64}
                            alt="Talentgram Logo"
                            style={{
                                width: '150px',
                                height: 'auto',
                            }}
                        />
                        <div style={{ fontSize: '20px', fontWeight: 'bold', letterSpacing: '0.15em' }}>TALENTGRAM AGENCY</div>
                    </div>

                    {/* Content Area */}
                    <div style={{ display: 'flex', flexDirection: 'column', marginTop: '40px', marginBottom: '40px' }}>
                        <div style={{ fontSize: '48px', fontWeight: 'bold', letterSpacing: '0.02em', marginBottom: '16px', lineHeight: 1.1 }}>
                            {displayName}
                        </div>
                        <div style={{ fontSize: '18px', color: 'rgba(255,255,255,0.7)', letterSpacing: '0.1em', textTransform: 'uppercase' }}>
                            {category}
                        </div>
                    </div>

                    {/* Region */}
                    <div style={{ display: 'flex', fontSize: '12px', color: 'rgba(255,255,255,0.4)', letterSpacing: '0.2em' }}>
                        <span>INDIA — UAE</span>
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
