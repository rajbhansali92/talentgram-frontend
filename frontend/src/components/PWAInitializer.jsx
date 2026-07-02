'use client';

import React, { useEffect, useState } from "react";
import { Download, X, Share, Plus, Sparkles } from "lucide-react";
import { usePathname } from "next/navigation";

export default function PWAInitializer() {
    const pathname = usePathname();
    const [deferredPrompt, setDeferredPrompt] = useState(null);
    const [showBanner, setShowBanner] = useState(false);
    const [showIosPrompt, setShowIosPrompt] = useState(false);
    const [isStandalone, setIsStandalone] = useState(false);
    const [platform, setPlatform] = useState({ isIos: false, isAndroid: false, isDesktop: false });

    // Helper to identify if the current user session is an authenticated internal user on an internal route
    const checkIsInternalUser = () => {
        if (typeof window === "undefined") return false;
        
        // 1. Route Gating: Must be on an admin/internal route
        const isInternalRoute = pathname.startsWith('/admin');
        if (!isInternalRoute) return false;
        
        // 2. Auth Gating: Must have a valid admin session token and tg_admin details
        const hasAdminToken = localStorage.getItem("tg_admin_token");
        if (!hasAdminToken) return false;
        
        try {
            const adminData = JSON.parse(localStorage.getItem("tg_admin") || "null");
            const role = adminData?.role;
            return role === "admin" || role === "team" || role === "staff";
        } catch (e) {
            return false;
        }
    };

    useEffect(() => {
        if (typeof window === "undefined") return;

        // 1. Register Service Worker globally (always registers for correct app behavior)
        if ("serviceWorker" in navigator) {
            navigator.serviceWorker
                .register("/sw.js")
                .then((reg) => {
                    console.log("[PWA] Service Worker registered with scope:", reg.scope);
                })
                .catch((err) => {
                    console.error("[PWA] Service Worker registration failed:", err);
                });
        }
    }, []);

    useEffect(() => {
        if (typeof window === "undefined") return;

        // Check if the current context belongs to an authenticated internal user
        const isInternal = checkIsInternalUser();
        if (!isInternal) {
            // Suppress installation prompts on public/unauthenticated pages
            setShowBanner(false);
            setShowIosPrompt(false);
            return;
        }

        // 2. Check if running in Standalone Mode (Installed PWA)
        const checkStandalone = () => {
            const isStandaloneMode = 
                window.matchMedia("(display-mode: standalone)").matches || 
                navigator.standalone || 
                document.referrer.includes("android-app://");
            setIsStandalone(isStandaloneMode);
            return isStandaloneMode;
        };

        const standalone = checkStandalone();
        if (standalone) return; // No prompts needed if already installed

        // 3. Detect Platform
        const ua = navigator.userAgent.toLowerCase();
        const isIos = /ipad|iphone|ipod/.test(ua) && !window.MSStream;
        const isAndroid = /android/.test(ua);
        const isDesktop = !isIos && !isAndroid;
        setPlatform({ isIos, isAndroid, isDesktop });

        // 4. Handle Android/Desktop Install Event
        const handleBeforeInstallPrompt = (e) => {
            e.preventDefault();
            
            // Check dismissal rules in localStorage
            const permanentlyDismissed = localStorage.getItem("tg_pwa_dismissed_permanently") === "true";
            const dismissedUntil = localStorage.getItem("tg_pwa_dismissed_until");
            const isDismissedForNow = dismissedUntil && Date.now() < parseInt(dismissedUntil, 10);

            if (permanentlyDismissed || isDismissedForNow) {
                return;
            }

            setDeferredPrompt(e);
            setShowBanner(true);
        };

        window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);

        // 5. Handle iOS Onboarding Prompts
        if (isIos) {
            const iosPromptDismissed = localStorage.getItem("tg_pwa_ios_dismissed") === "true";
            const iosDismissedUntil = localStorage.getItem("tg_pwa_ios_dismissed_until");
            const isIosDismissedForNow = iosDismissedUntil && Date.now() < parseInt(iosDismissedUntil, 10);

            if (!iosPromptDismissed && !isIosDismissedForNow) {
                const timer = setTimeout(() => {
                    setShowIosPrompt(true);
                }, 3000);
                return () => clearTimeout(timer);
            }
        }

        return () => {
            window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
        };
    }, [pathname]);

    // Action: Android / Desktop PWA Trigger
    const handleInstallClick = async () => {
        if (!deferredPrompt) return;
        
        // Show the native install prompt
        deferredPrompt.prompt();
        
        // Wait for the user to respond to the prompt
        const { outcome } = await deferredPrompt.userChoice;
        console.log(`[PWA] User response to the install prompt: ${outcome}`);
        
        // We've used the prompt, and can't use it again
        setDeferredPrompt(null);
        setShowBanner(false);
    };

    // Dismissal: Later (Remind in 7 days)
    const handleLaterDismissal = () => {
        const snoozeTime = Date.now() + 7 * 24 * 60 * 60 * 1000; // 7 days
        localStorage.setItem("tg_pwa_dismissed_until", snoozeTime.toString());
        setShowBanner(false);
    };

    // Dismissal: Never Again
    const handleNeverDismissal = () => {
        localStorage.setItem("tg_pwa_dismissed_permanently", "true");
        setShowBanner(false);
    };

    // Dismissal: iOS Prompt (Later)
    const handleIosLaterDismissal = () => {
        const snoozeTime = Date.now() + 7 * 24 * 60 * 60 * 1000; // 7 days
        localStorage.setItem("tg_pwa_ios_dismissed_until", snoozeTime.toString());
        setShowIosPrompt(false);
    };

    // Dismissal: iOS Prompt (Never)
    const handleIosNeverDismissal = () => {
        localStorage.setItem("tg_pwa_ios_dismissed", "true");
        setShowIosPrompt(false);
    };

    // If running in standalone or nothing to show, render nothing
    if (isStandalone || (!showBanner && !showIosPrompt)) {
        return null;
    }

    return (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 w-[calc(100%-2rem)] max-w-md px-2 pointer-events-none">
            {/* 1. Android & Desktop Install Banner */}
            {showBanner && (
                <div className="pointer-events-auto w-full bg-[#0c0c0ced] backdrop-blur-xl border border-white/12 text-[#f5f5f0] p-5 rounded-2xl shadow-2xl flex flex-col gap-4 animate-in fade-in slide-in-from-bottom-4 duration-300">
                    <div className="flex items-start justify-between gap-3">
                        <div className="flex items-center gap-3">
                            <div className="w-12 h-12 bg-white/5 border border-white/10 rounded-xl flex items-center justify-center overflow-hidden flex-shrink-0">
                                <img src="/apple-touch-icon.png" alt="Talentgram" className="w-9 h-9 object-contain" />
                            </div>
                            <div>
                                <h3 className="text-sm font-semibold tracking-tight">Install Talentgram</h3>
                                <p className="text-[11px] text-[#f5f5f0]/60 mt-0.5 leading-normal">
                                    Enjoy full screen casting feeds and a lightweight native desktop/mobile app.
                                </p>
                            </div>
                        </div>
                        <button 
                            onClick={handleLaterDismissal}
                            className="p-1 hover:bg-white/5 rounded-lg transition-colors text-[#f5f5f0]/50 hover:text-[#f5f5f0]"
                        >
                            <X className="w-4 h-4 stroke-[1.5]" />
                        </button>
                    </div>

                    <div className="flex items-center gap-2 w-full mt-1">
                        <button
                            onClick={handleInstallClick}
                            className="flex-1 inline-flex items-center justify-center gap-1.5 bg-[#f5f5f0] hover:bg-[#e4e4df] text-black text-xs font-semibold px-4 py-2.5 rounded-lg transition-all active:scale-98"
                        >
                            <Download className="w-3.5 h-3.5 stroke-[2]" />
                            Install Now
                        </button>
                        <button
                            onClick={handleLaterDismissal}
                            className="flex-1 inline-flex items-center justify-center text-xs font-medium border border-white/10 hover:bg-white/5 px-4 py-2.5 rounded-lg transition-colors"
                        >
                            Later
                        </button>
                    </div>
                    
                    <button 
                        onClick={handleNeverDismissal}
                        className="text-[10px] text-center text-[#f5f5f0]/30 hover:text-[#f5f5f0]/50 transition-colors uppercase tracking-widest mt-0.5"
                    >
                        Never Ask Again
                    </button>
                </div>
            )}

            {/* 2. iOS Safari Add to Home Screen Onboarding Card */}
            {showIosPrompt && (
                <div className="pointer-events-auto w-full bg-[#0c0c0ced] backdrop-blur-xl border border-white/12 text-[#f5f5f0] p-5 rounded-2xl shadow-2xl flex flex-col gap-4 animate-in fade-in slide-in-from-bottom-4 duration-300">
                    <div className="flex items-start justify-between gap-3">
                        <div className="flex items-center gap-3">
                            <div className="w-12 h-12 bg-white/5 border border-white/10 rounded-xl flex items-center justify-center overflow-hidden flex-shrink-0">
                                <img src="/apple-touch-icon.png" alt="Talentgram" className="w-9 h-9 object-contain" />
                            </div>
                            <div>
                                <h3 className="text-sm font-semibold tracking-tight">Install Talentgram</h3>
                                <p className="text-[11px] text-[#f5f5f0]/60 mt-0.5 leading-normal">
                                    Add Talentgram to your Home Screen for a premium fullscreen casting experience.
                                </p>
                            </div>
                        </div>
                        <button 
                            onClick={handleIosLaterDismissal}
                            className="p-1 hover:bg-white/5 rounded-lg transition-colors text-[#f5f5f0]/50 hover:text-[#f5f5f0]"
                        >
                            <X className="w-4 h-4 stroke-[1.5]" />
                        </button>
                    </div>

                    {/* Step-by-Step iOS Safari Onboarding Instructions */}
                    <div className="bg-white/5 rounded-xl p-3.5 border border-white/5 text-xs flex flex-col gap-3">
                        <div className="flex items-center gap-3">
                            <span className="w-5 h-5 rounded-full bg-white/10 flex items-center justify-center text-[10px] font-bold">1</span>
                            <div className="flex items-center gap-1">
                                <span>Tap the Share button</span>
                                <span className="inline-flex p-1 bg-white/10 rounded-md"><Share className="w-3.5 h-3.5 text-blue-400 stroke-[1.5]" /></span>
                                <span>in Safari.</span>
                            </div>
                        </div>
                        <div className="flex items-center gap-3">
                            <span className="w-5 h-5 rounded-full bg-white/10 flex items-center justify-center text-[10px] font-bold">2</span>
                            <div className="flex items-center gap-1">
                                <span>Scroll down and select</span>
                                <span className="font-semibold text-white/90">Add to Home Screen</span>
                                <span className="inline-flex p-1 bg-white/10 rounded-md"><Plus className="w-3.5 h-3.5 text-[#f5f5f0] stroke-[2]" /></span>
                            </div>
                        </div>
                    </div>

                    <div className="flex items-center gap-2 w-full">
                        <button
                            onClick={handleIosLaterDismissal}
                            className="flex-1 inline-flex items-center justify-center text-xs font-semibold bg-[#f5f5f0] hover:bg-[#e4e4df] text-black px-4 py-2.5 rounded-lg transition-colors"
                        >
                            Got It
                        </button>
                        <button
                            onClick={handleIosNeverDismissal}
                            className="flex-1 inline-flex items-center justify-center text-xs font-medium border border-white/10 hover:bg-white/5 px-4 py-2.5 rounded-lg transition-colors"
                        >
                            Don't Ask Again
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
