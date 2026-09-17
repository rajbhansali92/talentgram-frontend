import React, { useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import { SIMPLE_ASSISTANT_ENABLED } from "@/lib/simpleAssistant";
import { useAssistantOverview } from "@/hooks/useAssistantOverview";
import { useAssistantCommand } from "@/hooks/useAssistantCommand";
import { ASSISTANT_CSS } from "@/components/simple-assistant/assistantStyles";
import AssistantCore from "@/components/simple-assistant/AssistantCore";
import CommandBar from "@/components/simple-assistant/CommandBar";
import AssistantConsole from "@/components/simple-assistant/AssistantConsole";
import AssistantProjectPanel from "@/components/simple-assistant/AssistantProjectPanel";
import AssistantMediaDialog from "@/components/simple-assistant/AssistantMediaDialog";

/**
 * SIMPLE ASSISTANT — visual Command Centre.
 *
 * Phase 1: read-only situational awareness (GET /overview).
 * Phase 2: deterministic conversational command layer — DRY-RUN ONLY.
 *          Commands are understood, resolved, and previewed as an action
 *          plan; "Confirm" acknowledges the plan but changes nothing.
 *
 * Isolation: own route, own component tree, own <style> scoped to
 * `.tg-assistant`. No writes, no WhatsApp, no Cloudinary, no LLM.
 */
export default function SimpleAssistant() {
    if (!SIMPLE_ASSISTANT_ENABLED) return <Navigate to="/admin" replace />;
    return <CommandCentre />;
}

function CommandCentre() {
    const { data, loading, error, refresh, applyExternal } = useAssistantOverview();
    // The execute endpoint returns a freshly-built overview — apply it so
    // the Command Centre reflects the new pipeline state immediately.
    const cmd = useAssistantCommand({ onExecuted: (fresh) => applyExternal(fresh) });
    const [activeMedia, setActiveMedia] = useState(null);

    const lastResponse = cmd.turns[cmd.turns.length - 1]?.response;
    const coreState = useMemo(() => {
        if (cmd.busy) return "thinking";
        if (lastResponse?.state === "error" || lastResponse?.state === "blocked") return "error";
        if (lastResponse?.state === "stale" || lastResponse?.state === "failed") return "error";
        if (lastResponse?.state === "partial") return "partial_failure";
        if (lastResponse?.state === "clarification") return "listening";
        if (["executed", "queued", "attached"].includes(lastResponse?.state)) {
            const c = lastResponse.result?.counts || {};
            return c.failed || c.stale ? "error" : "success";
        }
        if (lastResponse) return "success";
        if (error && !data) return "error";
        if (loading && !data) return "thinking";
        if (data) return "success";
        return "waking";
    }, [cmd.busy, lastResponse, error, loading, data]);

    const firstName = data?.user?.first_name || "there";
    const greeting = data?.greeting || "Hello";
    const projects = data?.projects || [];
    const s = data?.summary;

    return (
        <div className="tg-assistant" data-testid="simple-assistant">
            <style>{ASSISTANT_CSS}</style>

            <div className="tga-shell">
                <div className="tga-hero">
                    <AssistantCore state={coreState} />
                    <div className="tga-fade tga-d1">
                        <div className="tga-eyebrow">Talentgram · Command Centre</div>
                        <h1 className="tga-greeting" data-testid="assistant-greeting">
                            {greeting}, <b>{firstName}</b>.
                        </h1>
                        <p className="tga-subline" data-testid="assistant-subline">
                            {loading && !data && "Reading the room…"}
                            {error && !data && "I couldn't reach Talentgram just now."}
                            {data && (
                                <>
                                    You have <b>{s.active_projects}</b>{" "}
                                    active {s.active_projects === 1 ? "project" : "projects"} in motion.
                                </>
                            )}
                        </p>
                    </div>
                </div>

                <CommandBar onSubmit={cmd.send} busy={cmd.busy} step={cmd.step} />

                <AssistantConsole
                    turns={cmd.turns}
                    busy={cmd.busy}
                    onConfirm={cmd.confirm}
                    onCancel={cmd.cancel}
                    onPick={(opt) => cmd.send(String(opt.index))}
                />

                {data && (
                    <div className="tga-chips tga-fade tga-d2" data-testid="assistant-summary">
                        <span className="tga-chip"><span className="n">{s.active_projects}</span><span className="l">Active projects</span></span>
                        <span className="tga-chip"><span className="n">{s.talents_in_pipeline}</span><span className="l">Talents in pipelines</span></span>
                        <span className="tga-chip"><span className="n">{s.tests_received}</span><span className="l">Tests received</span></span>
                        <span className="tga-chip"><span className="n">{s.follow_ups}</span><span className="l">Awaiting follow-up</span></span>
                        <button type="button" className="tga-chip" onClick={refresh} data-testid="assistant-refresh"
                            style={{ cursor: "pointer" }} title="Refresh">
                            <RefreshCw size={13} /> <span className="l">Refresh</span>
                        </button>
                    </div>
                )}

                {loading && !data && (
                    <div className="tga-projects" aria-hidden="true">
                        {[0, 1, 2].map((i) => (
                            <div key={i} className="tga-skel" style={{ height: 220 }} />
                        ))}
                    </div>
                )}

                {error && !data && (
                    <div className="tga-note" data-testid="assistant-error">
                        {error}
                        <div>
                            <button type="button" className="tga-retry" onClick={refresh}>Try again</button>
                        </div>
                    </div>
                )}

                {data && projects.length === 0 && (
                    <div className="tga-note" data-testid="assistant-empty">
                        No active projects right now. When a project goes live, it will appear here.
                    </div>
                )}

                {data && projects.length > 0 && (
                    <div className="tga-projects tga-fade tga-d3" data-testid="assistant-projects">
                        {projects.map((p) => (
                            <AssistantProjectPanel key={p.id} project={p} onPlay={setActiveMedia} />
                        ))}
                    </div>
                )}
            </div>

            <AssistantMediaDialog media={activeMedia} onClose={() => setActiveMedia(null)} />
        </div>
    );
}
