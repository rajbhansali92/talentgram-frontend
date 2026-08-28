import React, { useEffect, useState } from "react";
import { X, FolderKanban, Loader2, AlertTriangle } from "lucide-react";
import { adminApi } from "@/lib/api";
import { getStageLabel, normaliseStage } from "@/components/pipeline/constants";

// Read-only "which ongoing projects is this talent in, and at what stage"
// popover — same modal/backdrop/click-outside-to-close shell as TagPopover,
// but with no edit affordances at all (this is a status check, not a data
// entry point). Data is fetched lazily on mount, only for the one talent
// whose button was clicked — never embedded in the roster list payload.
export default function ProjectsPopover({ talent, onClose }) {
    const [state, setState] = useState("loading"); // loading | ready | error
    const [projects, setProjects] = useState([]);

    useEffect(() => {
        let isMounted = true;
        (async () => {
            try {
                const { data } = await adminApi.get(`/talents/${talent.id}/ongoing-projects`);
                if (isMounted) {
                    setProjects(data.data || []);
                    setState("ready");
                }
            } catch (err) {
                console.error("Failed to load talent's ongoing projects", err);
                if (isMounted) setState("error");
            }
        })();
        return () => { isMounted = false; };
    }, [talent.id]);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm" onClick={onClose}>
            <div
                className="bg-white rounded-2xl p-5 max-w-sm w-full shadow-2xl border border-black/[0.08] text-black relative flex flex-col max-h-[85vh]"
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between mb-4 shrink-0">
                    <div className="flex items-center gap-2">
                        <FolderKanban className="w-4 h-4 text-black/50" />
                        <h3 className="font-semibold text-sm text-neutral-800 truncate max-w-[200px]">Projects: {talent.name}</h3>
                    </div>
                    <button onClick={onClose} className="p-1 rounded-full hover:bg-black/5 transition-colors">
                        <X className="w-4 h-4 text-black/40 hover:text-black" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto">
                    {state === "loading" && (
                        <div className="flex items-center justify-center gap-2 py-8 text-xs text-black/40">
                            <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading projects…
                        </div>
                    )}

                    {state === "error" && (
                        <div className="flex items-center justify-center gap-2 py-8 text-xs text-red-500">
                            <AlertTriangle className="w-3.5 h-3.5" /> Unable to load project status.
                        </div>
                    )}

                    {state === "ready" && projects.length === 0 && (
                        <div className="py-8 text-center text-xs text-black/30 italic">
                            Not associated with any ongoing projects.
                        </div>
                    )}

                    {state === "ready" && projects.length > 0 && (
                        <div className="divide-y divide-black/[0.04] border border-black/[0.06] rounded-lg overflow-hidden">
                            {projects.map((p) => (
                                <div key={p.project_id} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
                                    <span className="text-[12.5px] font-medium text-neutral-800 truncate">{p.project_name}</span>
                                    <span className="shrink-0 px-2 py-0.5 rounded-full text-[10px] font-semibold tracking-wide bg-black/[0.05] text-black/60 border border-black/[0.06]">
                                        {getStageLabel(normaliseStage(p.stage))}
                                    </span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
