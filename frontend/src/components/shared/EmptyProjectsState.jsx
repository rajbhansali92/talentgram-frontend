import React from "react";
import { Briefcase } from "lucide-react";

/**
 * Extracted verbatim from PortalHome.jsx and PortalProjects.jsx — both
 * rendered the exact same "No Synced Projects" markup/copy independently
 * (Dashboard UX audit, Phase 3 item 7). Same empty state, one component,
 * no behavior change.
 */
export default function EmptyProjectsState({ className = "my-4" }) {
    return (
        <div className={`bg-white border border-black/5 rounded-2xl p-12 text-center flex flex-col items-center gap-4 max-w-lg mx-auto ${className}`}>
            <Briefcase className="w-10 h-10 text-black/25" strokeWidth={1.5} />
            <h3 className="font-semibold text-lg text-black">No Synced Projects</h3>
            <p className="text-sm text-black/50 leading-relaxed">
                You haven't started any project submissions yet. When an agency invites you or you apply to open briefs, they will show up here dynamically.
            </p>
        </div>
    );
}
