import React from "react";

/**
 * The assistant's visual core — an abstract Talentgram-navy energy form.
 * Not a robot, not JARVIS artwork: concentric rings + a glowing core + one
 * orbiting node.
 *
 * `state` is structured for the full lifecycle even though Phase 2 only
 * drives a few values:
 *   idle | waking | listening | thinking | speaking | executing | success | error
 *
 * All motion is CSS (transform/opacity only) and defined in
 * assistantStyles.js under `.tg-assistant`. Honours prefers-reduced-motion.
 */
export default function AssistantCore({ state = "idle", size = 132, className = "" }) {
    return (
        <div
            className={`tga-core ${className}`}
            data-state={state}
            data-testid="assistant-core"
            style={{ "--tga-core-size": `${size}px` }}
            role="img"
            aria-label={`Assistant ${state}`}
        >
            <span className="tga-core-glow" aria-hidden="true" />
            <span className="tga-core-ring r1" aria-hidden="true" />
            <span className="tga-core-ring r2" aria-hidden="true" />
            <span className="tga-core-ring r3" aria-hidden="true" />
            <span className="tga-core-disc" aria-hidden="true" />
            <span className="tga-core-pulse" aria-hidden="true" />
            <span className="tga-core-orbit" aria-hidden="true" />
        </div>
    );
}
