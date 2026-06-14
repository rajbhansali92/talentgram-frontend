'use client';

import React, { useEffect } from 'react';

export default function ErrorBoundary({ error, reset }) {
    useEffect(() => {
        console.error('Captured by global error boundary:', error);
    }, [error]);

    return (
        <div className="min-h-screen bg-[#ffffff] text-black flex flex-col items-center justify-center px-6 py-12">
            <div className="max-w-md w-full border border-black/15 rounded-xl p-8 bg-[#fdfdfd] shadow-sm">
                <div className="flex items-center gap-2 mb-6">
                    <span className="w-2.5 h-2.5 bg-red-600 rounded-full animate-pulse"></span>
                    <span className="text-[11px] tracking-[0.1em] uppercase text-black/50 font-semibold">
                        System Diagnostic
                    </span>
                </div>
                
                <h1 className="text-xl font-semibold tracking-tight text-black mb-3">
                    Rendering Error Detected
                </h1>
                
                <p className="text-sm text-black/60 leading-relaxed mb-6">
                    The portal encountered a rendering error. Please try resetting the view or contact administrator if the issue persists.
                </p>

                {error?.message && (
                    <div className="bg-black/5 rounded-lg p-4 font-mono text-[11px] text-black/80 break-all mb-6 max-h-40 overflow-y-auto border border-black/5">
                        {error.message}
                    </div>
                )}

                <div className="flex flex-col gap-2">
                    <button
                        onClick={() => reset()}
                        className="w-full inline-flex items-center justify-center bg-black text-white py-2.5 px-4 rounded-lg text-sm font-medium hover:opacity-95 transition-opacity"
                    >
                        Try Again
                    </button>
                    <a
                        href="/"
                        className="w-full inline-flex items-center justify-center border border-black/15 text-black py-2.5 px-4 rounded-lg text-sm font-medium hover:bg-black/5 transition-colors"
                    >
                        Return to Home
                    </a>
                </div>
            </div>
        </div>
    );
}
