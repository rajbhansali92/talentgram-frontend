import React from "react";
import { Settings } from "lucide-react";

/**
 * Placeholder for the Settings screen (contact prefs, WhatsApp opt-in,
 * session management — see docs/TALENT_DASHBOARD_ARCHITECTURE.md). Reserved
 * as a nav destination for Phase 2 item 1 (shell + hierarchy); content is a
 * follow-up increment.
 */
export default function PortalSettings() {
    return (
        <div className="flex-1 flex items-center justify-center px-6 py-16">
            <div className="max-w-md w-full bg-white border border-black/5 rounded-2xl p-10 text-center flex flex-col items-center gap-4">
                <Settings className="w-8 h-8 text-black/25" strokeWidth={1.5} />
                <h1 className="text-lg font-semibold text-black">Settings</h1>
                <p className="text-sm text-black/50 leading-relaxed">
                    Account and notification settings are coming in a future update.
                </p>
            </div>
        </div>
    );
}
