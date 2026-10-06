'use client';

import React from "react";
import Logo from "@/components/Logo";

/**
 * Page chrome shared by every Roster state (gate, errors, deck): a clean sticky Talentgram
 * header — logo, the roster title (visible at every width) and an optional right-hand slot.
 */
export default function RosterShell({ title, right, children, bare }) {
    return (
        <div className="min-h-screen bg-[#f6f6f4] text-black/85" data-testid="roster-page">
            {!bare && (
                <header className="sticky top-0 z-30 bg-white/95 backdrop-blur border-b border-black/[0.06]">
                    <div className="max-w-[1400px] mx-auto px-3 sm:px-6 h-[57px] grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3">
                        <Logo size={26} forceVariant="black" />
                        <div className="text-center text-[10px] tracking-[0.22em] uppercase text-black/45 truncate" data-testid="roster-header-title">{title}</div>
                        <div className="flex items-center justify-end gap-1.5 min-w-[40px]">{right}</div>
                    </div>
                </header>
            )}
            {children}
        </div>
    );
}
