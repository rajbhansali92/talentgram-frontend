import React from "react";
import { Play, User } from "lucide-react";

/**
 * Simple Assistant-specific talent presentation card. Built fresh from the
 * read-only overview payload — does NOT touch the existing TalentCard
 * component. No actions, no links, no mutation: the only interaction is
 * "play an existing video".
 */
function initials(name) {
    return (name || "?")
        .split(/\s+/)
        .slice(0, 2)
        .map((w) => w[0])
        .join("")
        .toUpperCase();
}

export default function AssistantTalentCard({ talent, projectName, onPlay }) {
    const { name, age, height, gender, photo_url, stage_label, test_status, is_follow_up, media } = talent;
    const sub = [age ? `${age}` : null, height || null, gender ? gender[0].toUpperCase() + gender.slice(1) : null]
        .filter(Boolean)
        .join("  ·  ");

    const takes = media?.takes || [];
    const intro = media?.intro_video || null;

    const badge = is_follow_up
        ? { cls: "follow", txt: "Follow-up" }
        : test_status === "received"
          ? { cls: "recv", txt: "Test received" }
          : null;

    const play = (m, label) =>
        onPlay({
            url: m.url,
            poster_url: m.poster_url,
            label,
            talentName: name,
            title: `${name} — ${label}`,
        });

    return (
        <article className="tga-tcard" data-testid="assistant-talent-card" data-talent-id={talent.talent_id}>
            <div className="tga-tcard-photo">
                {photo_url ? (
                    <img src={photo_url} alt={name} loading="lazy" decoding="async" />
                ) : (
                    <div className="ph"><User size={26} /></div>
                )}
                {badge && <span className={`tga-badge ${badge.cls}`}>{badge.txt}</span>}
            </div>
            <div className="tga-tcard-body">
                <div className="tga-tcard-name">{name}</div>
                {sub && <div className="tga-tcard-sub">{sub}</div>}
                <div className="tga-tcard-stage">{stage_label}{projectName ? ` · ${projectName}` : ""}</div>

                {(intro || takes.length > 0) && (
                    <div className="tga-media-btns">
                        {intro && (
                            <button type="button" className="tga-mbtn" onClick={() => play(intro, "Introduction")}
                                data-testid="assistant-play-intro">
                                <Play size={12} /> Introduction
                            </button>
                        )}
                        {takes.map((t, i) => (
                            <button key={t.id || i} type="button" className="tga-mbtn"
                                onClick={() => play(t, t.label || `Take ${i + 1}`)}
                                data-testid="assistant-play-take">
                                <Play size={12} /> {t.label || `Take ${i + 1}`}
                            </button>
                        ))}
                    </div>
                )}
            </div>
        </article>
    );
}
