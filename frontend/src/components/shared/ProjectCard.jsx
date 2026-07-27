import React from "react";
import { ArrowUpRight } from "lucide-react";

/**
 * Extracted verbatim from PortalHome.jsx (Phase 2 item 2 — Dashboard/
 * Projects content separation, see docs/TALENT_DASHBOARD_ARCHITECTURE.md).
 * Business logic (status/theme mapping, date formatting, submission link)
 * unchanged — only relocated so both the Dashboard's "Recent projects
 * summary" and the Projects page's full list can reuse the same card
 * instead of duplicating it.
 */
export default function ProjectCard({ project, theme }) {
    const getStatusDetails = () => {
        if (theme === "shortlisted") {
            return { color: "bg-amber-500", text: "Shortlisted" };
        }
        if (theme === "completed") {
            return { color: "bg-green-600", text: "Completed" };
        }

        // Ongoing statuses
        if (project.status === "draft") {
            return { color: "bg-black/25", text: "Draft / Continuation" };
        }
        if (project.status === "submitted" || project.status === "updated") {
            return { color: "bg-blue-500", text: "Awaiting Review" };
        }
        return { color: "bg-black/40", text: "Active" };
    };

    const statusDetails = getStatusDetails();
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

            <a
                href={`/submit/${project.project_slug}`}
                className="w-full inline-flex items-center justify-center gap-1 bg-[#fafafa] border border-black/10 hover:bg-black hover:text-white hover:border-black py-2.5 px-4 rounded-lg text-xs font-medium transition-all duration-150"
            >
                Open Project Submission
                <ArrowUpRight className="w-3.5 h-3.5" />
            </a>
        </div>
    );
}
