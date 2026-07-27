import React from "react";
import { Link } from "react-router-dom";
import { Briefcase, ArrowUpRight } from "lucide-react";

/**
 * Placeholder for the dedicated Projects view (Invited/Active/Submitted/
 * Shortlisted/Completed — see docs/TALENT_DASHBOARD_ARCHITECTURE.md's
 * information architecture). The project list itself already lives on
 * PortalHome.jsx (rendered under the Dashboard nav item) — splitting it out
 * into this route with its own grouped presentation is a follow-up Phase 2
 * increment, not done in this step (see Phase 2 item 1 report).
 */
export default function PortalProjects() {
    return (
        <div className="flex-1 flex items-center justify-center px-6 py-16">
            <div className="max-w-md w-full bg-white border border-black/5 rounded-2xl p-10 text-center flex flex-col items-center gap-4">
                <Briefcase className="w-8 h-8 text-black/25" strokeWidth={1.5} />
                <h1 className="text-lg font-semibold text-black">Projects</h1>
                <p className="text-sm text-black/50 leading-relaxed">
                    Your project list currently lives on the Dashboard tab. A dedicated
                    Projects view (grouped by status) is coming in a future update.
                </p>
                <Link
                    to="/portal/home"
                    className="inline-flex items-center gap-1.5 text-xs font-medium text-black hover:opacity-70 transition-opacity duration-150"
                >
                    Go to Dashboard
                    <ArrowUpRight className="w-3.5 h-3.5" />
                </Link>
            </div>
        </div>
    );
}
