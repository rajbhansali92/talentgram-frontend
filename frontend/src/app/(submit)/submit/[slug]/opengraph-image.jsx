import { ImageResponse } from 'next/og';
import { logoBlackBase64 } from '@/lib/logoBlackBase64';

export const runtime = 'edge';

// We fetch project data from the Railway backend dynamically
async function getProject(slug) {
    try {
        const res = await fetch(`https://talentgram-app-production.up.railway.app/api/public/projects/${slug}`, {
            next: { revalidate: 60 } // Cache project details for 60 seconds
        });
        if (!res.ok) return null;
        return await res.json();
    } catch (e) {
        console.error("Error fetching project in opengraph-image", e);
        return null;
    }
}

export default async function Image({ params }) {
    const { slug } = await params;
    const project = await getProject(slug);

    const projectName = project?.title || "Casting Call";
    const brandName = project?.brand_name || "Premium Brand";
    const titleText = `${brandName.toUpperCase()} — ${projectName.toUpperCase()}`;

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

                {/* Project Specific Name */}
                <div
                    style={{
                        fontSize: '20px',
                        letterSpacing: '0.1em',
                        textTransform: 'uppercase',
                        color: 'rgba(0, 0, 0, 0.7)',
                        marginBottom: '40px',
                        fontWeight: '400',
                        textAlign: 'center',
                    }}
                >
                    {titleText}
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
