import React from "react";
import { Clapperboard } from "lucide-react";
import AssistantTalentCard from "./AssistantTalentCard";

const HOT_STAGES = new Set(["locked", "shortlisted", "already_tested"]);

/**
 * One active project, visualised: header + canonical stage stat strip +
 * a horizontal rail of real talent cards. All data comes from the
 * read-only /api/simple-assistant/overview payload.
 */
export default function AssistantProjectPanel({ project, onPlay }) {
    const {
        name, character, requirement, status, shoot_dates, budget, medium_usage,
        image_url, stage_summary = [], tests_received, follow_up_count,
        talents = [], talents_truncated, talent_total,
    } = project;

    const metaBits = [
        character || requirement,
        shoot_dates && `Shoot ${shoot_dates}`,
        budget && `₹${budget}`,
        medium_usage,
    ].filter(Boolean);

    // Build the stat strip: virtual "Tested" + "Follow-up" first, then the
    // canonical stages that have talents in them.
    const stats = [
        { k: "Tested", v: tests_received, hot: tests_received > 0 },
        { k: "Follow-up", v: follow_up_count },
        ...stage_summary.map((s) => ({ k: s.label, v: s.count, hot: HOT_STAGES.has(s.key) })),
    ];

    return (
        <section className="tga-panel" data-testid="assistant-project-panel" data-project-id={project.id}>
            <header className="tga-panel-head">
                {image_url ? (
                    <img className="tga-panel-thumb" src={image_url} alt="" loading="lazy" decoding="async" />
                ) : (
                    <div className="tga-panel-thumb ph"><Clapperboard size={22} /></div>
                )}
                <div style={{ minWidth: 0 }}>
                    <div className="tga-panel-title">{name}</div>
                    {metaBits.length > 0 && (
                        <div className="tga-panel-meta">
                            {metaBits.map((b, i) => <span key={i}>{b}</span>)}
                        </div>
                    )}
                </div>
                <span className="tga-status">{status}</span>
            </header>

            <div className="tga-stats" data-testid="assistant-stage-stats">
                {stats.map((s, i) => (
                    <div key={i} className={`tga-stat${s.hot ? " hot" : ""}`}>
                        <div className="v">{s.v}</div>
                        <div className="k">{s.k}</div>
                    </div>
                ))}
            </div>

            {talents.length > 0 && (
                <div className="tga-rail-wrap">
                    <div className="tga-rail-label">
                        Talent {talents_truncated ? `· showing ${talents.length} of ${talent_total}` : `· ${talent_total}`}
                    </div>
                    <div className="tga-rail" data-testid="assistant-talent-rail">
                        {talents.map((t) => (
                            <AssistantTalentCard
                                key={t.talent_id}
                                talent={t}
                                onPlay={onPlay}
                            />
                        ))}
                    </div>
                </div>
            )}
        </section>
    );
}
