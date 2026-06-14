import { ImageResponse } from 'next/og';

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
    const campaignType = project?.campaign_type || "Campaign";
    const statusText = project?.status === "active" ? "APPLICATIONS OPEN" : "IN REVIEW";
    const locationText = project?.location || "India ↔ UAE";

    return new ImageResponse(
        (
            <div
                style={{
                    width: '100%',
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'space-between',
                    backgroundColor: '#0B1F3A',
                    color: '#FFFFFF',
                    fontFamily: 'serif',
                    padding: '80px',
                    boxSizing: 'border-box',
                }}
            >
                {/* Elegant luxury framing */}
                <div
                    style={{
                        position: 'absolute',
                        top: '40px',
                        left: '40px',
                        right: '40px',
                        bottom: '40px',
                        border: '1px solid rgba(255, 255, 255, 0.1)',
                        display: 'flex',
                    }}
                />

                {/* Top Bar */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', zIndex: 10 }}>
                    <div style={{ fontSize: '24px', fontWeight: 'bold', letterSpacing: '0.1em' }}>TALENTGRAM</div>
                    <div style={{
                        fontSize: '12px',
                        letterSpacing: '0.2em',
                        border: '1px solid rgba(255,255,255,0.3)',
                        padding: '6px 16px',
                        borderRadius: '2px',
                        fontWeight: '600'
                    }}>
                        {statusText}
                    </div>
                </div>

                {/* Main Content */}
                <div style={{ display: 'flex', flexDirection: 'column', zIndex: 10 }}>
                    <div style={{ fontSize: '20px', letterSpacing: '0.15em', color: 'rgba(255,255,255,0.6)', textTransform: 'uppercase', marginBottom: '16px' }}>
                        {brandName}
                    </div>
                    <div style={{ fontSize: '56px', fontWeight: 'bold', letterSpacing: '0.02em', marginBottom: '24px', lineHeight: 1.1 }}>
                        {projectName}
                    </div>
                    <div style={{ display: 'flex', gap: '24px', fontSize: '16px', color: 'rgba(255,255,255,0.7)', letterSpacing: '0.05em' }}>
                        <span>{campaignType}</span>
                        <span>•</span>
                        <span>{locationText}</span>
                    </div>
                </div>

                {/* Footer bar */}
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px', color: 'rgba(255,255,255,0.4)', letterSpacing: '0.2em', zIndex: 10 }}>
                    <span>TALENTGRAM AGENCY</span>
                    <span>INDIA ↔ UAE ➜ GLOBAL</span>
                </div>
            </div>
        ),
        {
            width: 1200,
            height: 630,
        }
    );
}
