'use client';

import React, { useEffect } from "react";
import { WifiOff, RotateCw } from "lucide-react";
import Logo from "@/components/Logo";

export default function OfflinePage() {
    const handleRetry = () => {
        if (typeof window !== "undefined") {
            window.location.reload();
        }
    };

    // The service worker serves this page in place of whatever the user
    // was actually navigating to (the address bar still shows the
    // original URL), so a reload here correctly re-attempts that original
    // page. Mirrors ClientView.jsx's own `online` reconnect handling —
    // recover automatically once connectivity returns instead of leaving
    // the user stuck behind a manual "Retry Connection" click.
    useEffect(() => {
        if (typeof window === "undefined") return;
        window.addEventListener("online", handleRetry);
        return () => window.removeEventListener("online", handleRetry);
    }, []);

    return (
        <div className="min-h-screen bg-[#ffffff] dark:bg-[#0c0c0c] text-black dark:text-[#f5f5f0] flex flex-col justify-between p-6 md:p-12 transition-colors duration-150">
            {/* Header */}
            <header className="flex justify-between items-center w-full">
                <Logo size={36} forceVariant={undefined} />
                <span className="text-[10px] uppercase tracking-widest text-black/40 dark:text-[#f5f5f0]/40">Offline Mode</span>
            </header>

            {/* Main Content */}
            <main className="flex-1 flex flex-col items-center justify-center text-center max-w-md mx-auto my-12">
                <div className="relative mb-8">
                    {/* Pulsing indicator background */}
                    <div className="absolute inset-0 bg-red-500/10 dark:bg-red-500/5 rounded-full blur-xl scale-150 animate-pulse"></div>
                    <div className="relative bg-black/5 dark:bg-white/5 border border-black/10 dark:border-white/10 p-6 rounded-2xl">
                        <WifiOff className="w-12 h-12 text-black/60 dark:text-[#f5f5f0]/60 stroke-[1.5]" />
                    </div>
                </div>

                <h1 className="text-2xl font-semibold tracking-tight mb-3 font-sans">
                    Connection Unavailable
                </h1>
                
                <p className="text-sm text-black/50 dark:text-[#f5f5f0]/50 mb-8 leading-relaxed">
                    Talentgram is a live talent & casting platform. You are currently offline. Please check your network and try again.
                </p>

                <button
                    onClick={handleRetry}
                    className="inline-flex items-center gap-2 bg-black dark:bg-[#f5f5f0] text-white dark:text-black px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 active:scale-98 transition-all duration-150 shadow-sm"
                >
                    <RotateCw className="w-4 h-4 stroke-[1.5]" />
                    Retry Connection
                </button>
            </main>

            {/* Footer */}
            <footer className="w-full text-center text-[10px] tracking-[0.08em] uppercase text-black/35 dark:text-[#f5f5f0]/35">
                © Talentgram · Phase 1
            </footer>
        </div>
    );
}
