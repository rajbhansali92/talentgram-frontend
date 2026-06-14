import { ImageResponse } from 'next/og';
import { logoBlackBase64 } from '@/lib/logoBlackBase64';

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

    return new ImageResponse(
        (
            <div
                style={{
                    width: '100%',
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    backgroundColor: '#FFFFFF',
                    color: '#000000',
                    fontFamily: 'serif',
                    padding: '80px',
                    boxSizing: 'border-box',
                }}
            >
                {/* Original Black Logo */}
                <img
                    src={logoBlackBase64}
                    alt="Talentgram Logo"
                    style={{
                        width: '280px',
                        height: 'auto',
                        marginBottom: '32px',
                    }}
                />

                {/* Brand Title */}
                <div
                    style={{
                        fontSize: '36px',
                        fontWeight: 'bold',
                        letterSpacing: '0.15em',
                        textTransform: 'uppercase',
                        marginBottom: '20px',
                        fontFamily: 'serif',
                    }}
                >
                    TALENTGRAM AGENCY
                </div>

                {/* Talent Specific Name */}
                <div
                    style={{
                        fontSize: '24px',
                        letterSpacing: '0.1em',
                        textTransform: 'uppercase',
                        color: 'rgba(0, 0, 0, 0.8)',
                        marginBottom: '40px',
                        fontWeight: '500',
                        textAlign: 'center',
                    }}
                >
                    {displayName}
                </div>

                {/* Region */}
                <div
                    style={{
                        fontSize: '14px',
                        letterSpacing: '0.4em',
                        textTransform: 'uppercase',
                        color: 'rgba(0, 0, 0, 0.4)',
                        borderTop: '1px solid rgba(0, 0, 0, 0.15)',
                        paddingTop: '20px',
                        width: '240px',
                        textAlign: 'center',
                    }}
                >
                    INDIA — UAE
                </div>
            </div>
        ),
        {
            width: 1200,
            height: 630,
        }
    );
}
