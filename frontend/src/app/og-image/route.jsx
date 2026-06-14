import { ImageResponse } from 'next/og';

export const runtime = 'edge';

export async function GET() {
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
                    backgroundColor: '#0B1F3A',
                    color: '#FFFFFF',
                    fontFamily: 'serif',
                    padding: '60px',
                    boxSizing: 'border-box',
                }}
            >
                {/* Minimalist Premium Border */}
                <div
                    style={{
                        position: 'absolute',
                        top: '40px',
                        left: '40px',
                        right: '40px',
                        bottom: '40px',
                        border: '1px solid rgba(255, 255, 255, 0.15)',
                        display: 'flex',
                    }}
                />

                {/* Content Container */}
                <div
                    style={{
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        zIndex: 10,
                    }}
                >
                    {/* Logo/Brand Title */}
                    <div
                        style={{
                            fontSize: '64px',
                            fontWeight: 'bold',
                            letterSpacing: '0.15em',
                            textTransform: 'uppercase',
                            marginBottom: '16px',
                            fontFamily: 'serif',
                        }}
                    >
                        TALENTGRAM
                    </div>

                    {/* Subtitle */}
                    <div
                        style={{
                            fontSize: '20px',
                            letterSpacing: '0.25em',
                            textTransform: 'uppercase',
                            color: 'rgba(255, 255, 255, 0.7)',
                            marginBottom: '64px',
                            fontWeight: '300',
                        }}
                    >
                        PREMIUM TALENT NETWORK
                    </div>

                    {/* Pillars */}
                    <div
                        style={{
                            display: 'flex',
                            fontSize: '14px',
                            letterSpacing: '0.3em',
                            textTransform: 'uppercase',
                            color: 'rgba(255, 255, 255, 0.5)',
                            marginBottom: '40px',
                            gap: '12px',
                        }}
                    >
                        <span>SCOUT</span>
                        <span>•</span>
                        <span>MANAGE</span>
                        <span>•</span>
                        <span>SUBMIT</span>
                        <span>•</span>
                        <span>REVIEW</span>
                        <span>•</span>
                        <span>PLACE</span>
                    </div>

                    {/* Region */}
                    <div
                        style={{
                            fontSize: '12px',
                            letterSpacing: '0.4em',
                            textTransform: 'uppercase',
                            color: 'rgba(255, 255, 255, 0.4)',
                            borderTop: '1px solid rgba(255, 255, 255, 0.2)',
                            paddingTop: '16px',
                            width: '200px',
                            textAlign: 'center',
                        }}
                    >
                        INDIA ↔ UAE
                    </div>
                </div>
            </div>
        ),
        {
            width: 1200,
            height: 630,
        }
    );
}
