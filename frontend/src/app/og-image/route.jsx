import { ImageResponse } from 'next/og';
import { logoBlackBase64 } from '@/lib/logoBlackBase64';

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
                        marginBottom: '40px',
                        fontFamily: 'serif',
                    }}
                >
                    TALENTGRAM AGENCY
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
