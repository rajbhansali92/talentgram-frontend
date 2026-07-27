import React from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { getEngagementStatusDetails } from "@/lib/engagementStatus";

/**
 * Extracted verbatim from PortalHome.jsx (Phase 2 item 2 — Dashboard/
 * Projects content separation, see docs/TALENT_DASHBOARD_ARCHITECTURE.md).
 * Status/theme mapping now lives in lib/engagementStatus.js, shared with
 * ProjectDetail.jsx (Phase 2 item 4) instead of duplicated.
 *
 * Phase 2 item 4: the card now opens the new internal Project Detail page
 * (/portal/projects/{slug}) instead of jumping straight to /submit/{slug} —
 * Project Detail is "the place from which that [submit] experience is
 * launched," per that task's explicit scope. /submit/{slug} itself is
 * unchanged and still reachable (from inside Project Detail, and from every
 * external link — WhatsApp, invite links — exactly as before).
 */
export default function ProjectCard({ project, theme }) {
    const statusDetails = getEngagementStatusDetails(project, theme);
    const formattedDate = project.updated_at
        ? new Date(project.updated_at).toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
              year: "numeric",
          })
        : "N/A";

    return (
        <div className="bg-white border border-black/5 hover:border-black/15 hover:shadow-[0_4px_20px_rgba(0,0,0,0.02)] transition-all duration-200 rounded-xl p-5 flex flex-col justify-between gap-4">
            <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                    <span className="text-[10px] text-black/45 tracking-wider uppercase font-medium">
                        Audition
                    </span>
                    <span className="text-[10px] text-black/45">{formattedDate}</span>
                </div>
                <h3 className="font-semibold text-base text-black tracking-tight mt-0.5">
                    {project.project_title}
                </h3>
                <div className="flex items-center gap-2 mt-1">
                    <span className={`w-2 h-2 rounded-full ${statusDetails.color}`} />
                    <span className="text-xs text-black/60 font-medium">{statusDetails.text}</span>
                </div>
            </div>

            <Link
                to={`/portal/projects/${project.project_slug}`}
                className="w-full inline-flex items-center justify-center gap-1 bg-[#fafafa] border border-black/10 hover:bg-black hover:text-white hover:border-black py-2.5 px-4 rounded-lg text-xs font-medium transition-all duration-150"
            >
                View Project
                <ArrowUpRight className="w-3.5 h-3.5" />
            </Link>
        </div>
    );
}
