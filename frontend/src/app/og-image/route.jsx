import { ImageResponse } from 'next/og';
import { logoBase64 } from '@/lib/logoBase64';

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
                    padding: '80px',
                    boxSizing: 'border-box',
                }}
            >
                {/* Logo Image */}
                <img
                    src={logoBase64}
                    alt="Talentgram Logo"
                    style={{
                        width: '280px',
                        height: 'auto',
                        marginBottom: '40px',
                    }}
                />

                {/* Brand Title */}
                <div
                    style={{
                        fontSize: '36px',
                        fontWeight: 'bold',
                        letterSpacing: '0.15em',
                        textTransform: 'uppercase',
                        marginBottom: '24px',
                        fontFamily: 'serif',
                    }}
                >
                    TALENTGRAM AGENCY
                </div>

                {/* Subtitle / Pillars */}
                <div
                    style={{
                        fontSize: '18px',
                        letterSpacing: '0.3em',
                        textTransform: 'uppercase',
                        color: 'rgba(255, 255, 255, 0.7)',
                        marginBottom: '40px',
                        fontWeight: '300',
                    }}
                >
                    WE SCOUT | WE MANAGE
                </div>

                {/* Region */}
                <div
                    style={{
                        fontSize: '14px',
                        letterSpacing: '0.4em',
                        textTransform: 'uppercase',
                        color: 'rgba(255, 255, 255, 0.5)',
                        borderTop: '1px solid rgba(255, 255, 255, 0.15)',
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
