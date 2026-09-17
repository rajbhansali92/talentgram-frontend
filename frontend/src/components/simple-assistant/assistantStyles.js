/**
 * All Simple Assistant styling. Injected once via a <style> tag by
 * SimpleAssistant.jsx. EVERY selector is scoped under `.tg-assistant` and
 * every @keyframes name is prefixed `tga-`, so nothing here can leak into
 * or collide with the rest of the Talentgram admin app.
 *
 * Palette + type deliberately match the existing design system
 * (navy #0B1F3A, white, Manrope). The "futuristic" feel is motion, glow
 * and depth — not a new brand.
 */
export const ASSISTANT_CSS = `
.tg-assistant {
  --tga-navy: #0B1F3A;
  --tga-navy-2: #12325c;
  --tga-ink: #0a0a0a;
  --tga-muted: #5a5a5a;
  --tga-line: #e7ebf0;
  --tga-bg: #f6f8fb;
  --tga-surface: #ffffff;
  --tga-glow: rgba(11, 31, 58, 0.14);
  --tga-accent: #3b82f6;
  font-family: Manrope, ui-sans-serif, system-ui, sans-serif;
  color: var(--tga-ink);
  background:
    radial-gradient(1100px 520px at 78% -8%, rgba(59,130,246,0.08), transparent 60%),
    radial-gradient(760px 440px at 6% 4%, rgba(11,31,58,0.06), transparent 55%),
    var(--tga-bg);
  min-height: 100%;
}
.tg-assistant *,
.tg-assistant *::before,
.tg-assistant *::after { box-sizing: border-box; }

.tg-assistant .tga-shell {
  max-width: 1240px;
  margin: 0 auto;
  padding: 40px 24px 96px;
}
@media (max-width: 640px) { .tg-assistant .tga-shell { padding: 24px 14px 72px; } }

/* ---------- wake-up / entrance ---------- */
.tg-assistant .tga-fade {
  opacity: 0;
  transform: translateY(10px);
  animation: tga-rise 620ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
}
.tg-assistant .tga-d1 { animation-delay: 120ms; }
.tg-assistant .tga-d2 { animation-delay: 260ms; }
.tg-assistant .tga-d3 { animation-delay: 420ms; }
.tg-assistant .tga-d4 { animation-delay: 600ms; }
@keyframes tga-rise { to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) {
  .tg-assistant .tga-fade { animation: none; opacity: 1; transform: none; }
  .tg-assistant .tga-core,
  .tg-assistant .tga-core-glow,
  .tg-assistant .tga-core-ring,
  .tg-assistant .tga-core-orbit,
  .tg-assistant .tga-core-pulse { animation: none !important; }
}

/* ---------- assistant core ---------- */
.tg-assistant .tga-core {
  position: relative;
  width: var(--tga-core-size, 132px);
  height: var(--tga-core-size, 132px);
  flex: none;
  display: grid;
  place-items: center;
  animation: tga-wake 820ms cubic-bezier(0.22, 1, 0.36, 1);
}
.tg-assistant .tga-core-glow {
  position: absolute; inset: -22%;
  border-radius: 50%;
  background: radial-gradient(circle at 50% 45%, rgba(59,130,246,0.30), rgba(11,31,58,0.05) 62%, transparent 72%);
  filter: blur(6px);
  animation: tga-breathe 5.4s ease-in-out infinite;
}
.tg-assistant .tga-core-ring {
  position: absolute; inset: 0;
  border-radius: 50%;
  border: 1.5px solid rgba(11,31,58,0.16);
}
.tg-assistant .tga-core-ring.r2 { inset: 14%; border-color: rgba(11,31,58,0.22); animation: tga-spin 18s linear infinite; }
.tg-assistant .tga-core-ring.r3 { inset: 26%; border-style: dashed; border-color: rgba(59,130,246,0.34); animation: tga-spin 26s linear infinite reverse; }
.tg-assistant .tga-core-disc {
  position: absolute; inset: 34%;
  border-radius: 50%;
  background: radial-gradient(circle at 38% 34%, #1b3f74, var(--tga-navy) 70%);
  box-shadow: 0 10px 30px -10px var(--tga-glow), inset 0 0 18px rgba(255,255,255,0.14);
}
.tg-assistant .tga-core-pulse {
  position: absolute; inset: 34%;
  border-radius: 50%;
  border: 1px solid rgba(59,130,246,0.55);
  animation: tga-pulse 2.8s ease-out infinite;
}
.tg-assistant .tga-core-orbit {
  position: absolute; inset: 0;
  animation: tga-spin 12s linear infinite;
}
.tg-assistant .tga-core-orbit::after {
  content: ""; position: absolute; top: 4%; left: 50%;
  width: 7px; height: 7px; margin-left: -3.5px;
  border-radius: 50%;
  background: var(--tga-accent);
  box-shadow: 0 0 10px 2px rgba(59,130,246,0.55);
}

/* states */
.tg-assistant .tga-core[data-state="waking"] { animation: tga-wake 900ms cubic-bezier(0.22,1,0.36,1); }
.tg-assistant .tga-core[data-state="thinking"] .tga-core-ring.r2 { animation-duration: 6s; }
.tg-assistant .tga-core[data-state="thinking"] .tga-core-ring.r3 { animation-duration: 8s; }
.tg-assistant .tga-core[data-state="thinking"] .tga-core-pulse { animation-duration: 1.4s; }
.tg-assistant .tga-core[data-state="success"] .tga-core-disc { background: radial-gradient(circle at 38% 34%, #1f7a4d, #0f5132 72%); }
.tg-assistant .tga-core[data-state="error"] .tga-core-disc { background: radial-gradient(circle at 38% 34%, #b4432f, #7f1d1d 72%); }
.tg-assistant .tga-core[data-state="error"] .tga-core-pulse { border-color: rgba(220,38,38,0.6); }
.tg-assistant .tga-core[data-state="partial_failure"] .tga-core-disc { background: radial-gradient(circle at 38% 34%, #b7791f, #7c4a03 72%); }
.tg-assistant .tga-core[data-state="sending"] .tga-core-ring.r2,
.tg-assistant .tga-core[data-state="sending"] .tga-core-ring.r3 { animation-duration: 4s; }

@keyframes tga-spin { to { transform: rotate(360deg); } }
@keyframes tga-breathe { 0%,100% { transform: scale(1); opacity: 0.85; } 50% { transform: scale(1.06); opacity: 1; } }
@keyframes tga-pulse { 0% { transform: scale(0.9); opacity: 0.9; } 70% { opacity: 0; } 100% { transform: scale(1.5); opacity: 0; } }
@keyframes tga-wake { from { transform: scale(0.6); opacity: 0; filter: blur(6px); } to { transform: scale(1); opacity: 1; filter: blur(0); } }

/* ---------- header / greeting ---------- */
.tg-assistant .tga-hero {
  display: flex; align-items: center; gap: 28px;
  padding: 8px 0 30px;
}
@media (max-width: 640px) { .tg-assistant .tga-hero { flex-direction: column; text-align: center; gap: 18px; } }
.tg-assistant .tga-eyebrow {
  text-transform: uppercase; letter-spacing: 0.18em; font-size: 11px;
  color: var(--tga-muted); font-weight: 600;
}
.tg-assistant .tga-greeting {
  font-size: clamp(26px, 4vw, 40px); font-weight: 300; letter-spacing: -0.01em;
  margin: 6px 0 4px; color: var(--tga-ink);
}
.tg-assistant .tga-greeting b { font-weight: 600; }
.tg-assistant .tga-subline { color: var(--tga-muted); font-size: 15px; }
.tg-assistant .tga-subline b { color: var(--tga-navy); font-weight: 600; }

/* ---------- summary chips ---------- */
.tg-assistant .tga-chips { display: flex; flex-wrap: wrap; gap: 10px; margin: 4px 0 34px; }
.tg-assistant .tga-chip {
  display: inline-flex; align-items: baseline; gap: 8px;
  background: var(--tga-surface); border: 1px solid var(--tga-line);
  border-radius: 999px; padding: 9px 16px;
  box-shadow: 0 1px 0 rgba(11,31,58,0.03);
}
.tg-assistant .tga-chip .n { font-size: 17px; font-weight: 700; color: var(--tga-navy); }
.tg-assistant .tga-chip .l { font-size: 12.5px; color: var(--tga-muted); }

/* ---------- project panels ---------- */
.tg-assistant .tga-projects { display: grid; gap: 20px; }
.tg-assistant .tga-panel {
  position: relative;
  background: var(--tga-surface);
  border: 1px solid var(--tga-line);
  border-radius: 18px;
  overflow: hidden;
  transition: box-shadow 220ms ease, transform 220ms ease, border-color 220ms ease;
}
.tg-assistant .tga-panel:hover {
  box-shadow: 0 24px 60px -30px var(--tga-glow);
  border-color: #d4deeb;
}
.tg-assistant .tga-panel-head {
  display: flex; gap: 18px; align-items: flex-start;
  padding: 20px 22px;
  border-bottom: 1px solid var(--tga-line);
  background: linear-gradient(180deg, rgba(11,31,58,0.035), transparent);
}
.tg-assistant .tga-panel-thumb {
  width: 56px; height: 56px; border-radius: 12px; flex: none;
  object-fit: cover; background: #eef2f7;
  border: 1px solid var(--tga-line);
}
.tg-assistant .tga-panel-thumb.ph {
  display: grid; place-items: center; color: var(--tga-navy); font-weight: 700; font-size: 18px;
}
.tg-assistant .tga-panel-title { font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }
.tg-assistant .tga-panel-meta { font-size: 12.5px; color: var(--tga-muted); margin-top: 3px; display: flex; flex-wrap: wrap; gap: 4px 12px; }
.tg-assistant .tga-status {
  margin-left: auto; flex: none;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--tga-navy); background: rgba(11,31,58,0.08);
  border-radius: 999px; padding: 5px 11px; font-weight: 600;
}

/* stage stat strip */
.tg-assistant .tga-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
  gap: 1px;
  background: var(--tga-line);
}
.tg-assistant .tga-stat {
  background: var(--tga-surface);
  padding: 14px 14px 12px;
}
.tg-assistant .tga-stat .v { font-size: 20px; font-weight: 700; color: var(--tga-navy); line-height: 1; }
.tg-assistant .tga-stat .k { font-size: 11px; color: var(--tga-muted); margin-top: 5px; text-transform: uppercase; letter-spacing: 0.06em; }
.tg-assistant .tga-stat.hot .v { color: var(--tga-accent); }

/* talent rail */
.tg-assistant .tga-rail-wrap { padding: 18px 22px 22px; }
.tg-assistant .tga-rail-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--tga-muted); font-weight: 600; margin-bottom: 12px;
}
.tg-assistant .tga-rail {
  display: grid; grid-auto-flow: column; grid-auto-columns: 184px;
  gap: 14px; overflow-x: auto; padding-bottom: 8px;
  scrollbar-width: thin;
}
.tg-assistant .tga-rail::-webkit-scrollbar { height: 8px; }
.tg-assistant .tga-rail::-webkit-scrollbar-thumb { background: rgba(11,31,58,0.14); border-radius: 8px; }
@media (max-width: 640px) { .tg-assistant .tga-rail { grid-auto-columns: 158px; } }

/* talent card */
.tg-assistant .tga-tcard {
  background: var(--tga-surface);
  border: 1px solid var(--tga-line);
  border-radius: 14px;
  overflow: hidden;
  display: flex; flex-direction: column;
  transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
}
.tg-assistant .tga-tcard:hover {
  transform: translateY(-3px);
  box-shadow: 0 18px 40px -22px var(--tga-glow);
  border-color: #d4deeb;
}
.tg-assistant .tga-tcard-photo {
  position: relative; aspect-ratio: 3 / 3.5; background: #eef2f7; overflow: hidden;
}
.tg-assistant .tga-tcard-photo img { width: 100%; height: 100%; object-fit: cover; display: block; }
.tg-assistant .tga-tcard-photo .ph {
  width: 100%; height: 100%; display: grid; place-items: center;
  color: #9aa7b8; font-size: 26px; font-weight: 700;
}
.tg-assistant .tga-badge {
  position: absolute; left: 8px; top: 8px;
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
  padding: 4px 8px; border-radius: 999px;
  background: rgba(255,255,255,0.94); color: var(--tga-navy);
  box-shadow: 0 2px 8px rgba(11,31,58,0.16);
}
.tg-assistant .tga-badge.recv { background: #0f5132; color: #fff; }
.tg-assistant .tga-badge.follow { background: #7c4a03; color: #fff; }
.tg-assistant .tga-tcard-body { padding: 10px 11px 12px; }
.tg-assistant .tga-tcard-name { font-size: 13.5px; font-weight: 600; line-height: 1.2; }
.tg-assistant .tga-tcard-sub { font-size: 11.5px; color: var(--tga-muted); margin-top: 2px; }
.tg-assistant .tga-tcard-stage { font-size: 11px; color: var(--tga-navy); font-weight: 600; margin-top: 6px; }
.tg-assistant .tga-media-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.tg-assistant .tga-mbtn {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; font-weight: 600;
  border: 1px solid var(--tga-line); background: #f7f9fc; color: var(--tga-navy);
  border-radius: 8px; padding: 5px 8px; cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease, transform 120ms ease;
}
.tg-assistant .tga-mbtn:hover { background: #eef3fa; border-color: #cdd9e8; }
.tg-assistant .tga-mbtn:active { transform: scale(0.96); }

/* ---------- player dialog ---------- */
.tg-assistant .tga-modal {
  position: fixed; inset: 0; z-index: 60;
  display: grid; place-items: center;
  background: rgba(6, 14, 27, 0.62);
  backdrop-filter: blur(6px);
  animation: tga-rise 200ms ease forwards;
  padding: 20px;
}
.tg-assistant .tga-modal-card {
  width: min(880px, 96vw);
  background: #0b1220;
  border: 1px solid rgba(255,255,255,0.12);
  border-radius: 16px; overflow: hidden;
  box-shadow: 0 40px 120px -30px rgba(0,0,0,0.7);
}
.tg-assistant .tga-modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; color: #eef2f7; font-size: 13px; font-weight: 600;
  border-bottom: 1px solid rgba(255,255,255,0.08);
}
.tg-assistant .tga-modal-close {
  background: rgba(255,255,255,0.08); border: none; color: #eef2f7;
  width: 28px; height: 28px; border-radius: 8px; cursor: pointer; font-size: 15px;
}
.tg-assistant .tga-modal-close:hover { background: rgba(255,255,255,0.16); }
.tg-assistant .tga-modal-body { background: #000; }
.tg-assistant .tga-modal-body video { width: 100%; max-height: 72vh; display: block; }

/* ---------- command bar ---------- */
.tg-assistant .tga-cmd {
  margin: 4px 0 22px;
}
.tg-assistant .tga-cmd-box {
  display: flex; align-items: flex-end; gap: 10px;
  background: var(--tga-surface);
  border: 1px solid var(--tga-line);
  border-radius: 14px;
  padding: 10px 10px 10px 16px;
  transition: border-color 160ms ease, box-shadow 160ms ease;
}
.tg-assistant .tga-cmd-box:focus-within {
  border-color: var(--tga-navy);
  box-shadow: 0 0 0 3px rgba(11,31,58,0.08);
}
.tg-assistant .tga-cmd-box textarea {
  flex: 1; border: none; outline: none; resize: none;
  font: inherit; font-size: 14.5px; line-height: 1.5;
  background: transparent; color: var(--tga-ink);
  max-height: 140px; padding: 6px 0;
}
.tg-assistant .tga-cmd-box textarea::placeholder { color: #9aa7b8; }
.tg-assistant .tga-send {
  flex: none;
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--tga-navy); color: #fff;
  border: none; border-radius: 10px; padding: 9px 14px;
  font-size: 13px; font-weight: 600; cursor: pointer;
  transition: opacity 140ms ease, transform 120ms ease;
}
.tg-assistant .tga-send:disabled { opacity: 0.45; cursor: default; }
.tg-assistant .tga-send:not(:disabled):hover { opacity: 0.92; }
.tg-assistant .tga-send:not(:disabled):active { transform: scale(0.97); }

/* Phase 4C — audition file attach */
.tg-assistant .tga-attach {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; margin-bottom: 2px;
  border: 1px solid var(--tga-line); background: #f7f9fc; color: var(--tga-navy);
  border-radius: 9px; cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease;
}
.tg-assistant .tga-attach:hover:not(:disabled) { background: #eef3fa; border-color: #cdd9e8; }
.tg-assistant .tga-attach:disabled { opacity: 0.45; cursor: default; }
.tg-assistant .tga-file-chip {
  display: inline-flex; align-items: center; gap: 8px; margin-top: 10px;
  font-size: 12px; color: var(--tga-navy);
  background: #eef3fa; border: 1px solid #cdd9e8; border-radius: 999px;
  padding: 5px 6px 5px 12px; max-width: 100%;
}
.tg-assistant .tga-file-chip .fn { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.tg-assistant .tga-file-chip .fs { color: #5a6b80; }
.tg-assistant .tga-file-chip button {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border: none; border-radius: 999px;
  background: rgba(11,31,58,0.08); color: var(--tga-navy); cursor: pointer;
}
.tg-assistant .tga-file-chip button:hover { background: rgba(11,31,58,0.16); }

.tg-assistant .tga-chips-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.tg-assistant .tga-qc {
  font-size: 12px; font-weight: 500;
  border: 1px solid var(--tga-line); background: #f7f9fc; color: var(--tga-navy);
  border-radius: 999px; padding: 6px 12px; cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease;
}
.tg-assistant .tga-qc:hover { background: #eef3fa; border-color: #cdd9e8; }

/* processing line */
.tg-assistant .tga-processing {
  display: flex; align-items: center; gap: 10px;
  font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--tga-navy); font-weight: 600;
  padding: 10px 2px;
}
.tg-assistant .tga-processing .dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--tga-accent);
  animation: tga-pp 1s ease-in-out infinite;
}
@keyframes tga-pp { 0%,100% { opacity: 0.25; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1); } }
.tg-assistant .tga-spin { animation: tga-spin-kf 0.8s linear infinite; }
@keyframes tga-spin-kf { to { transform: rotate(360deg); } }

/* ---------- console / turns ---------- */
.tg-assistant .tga-console { display: grid; gap: 14px; margin-bottom: 30px; }
.tg-assistant .tga-turn { display: grid; gap: 8px; }
.tg-assistant .tga-turn-you {
  align-self: flex-end; justify-self: end;
  max-width: 90%;
  background: var(--tga-navy); color: #fff;
  border-radius: 12px 12px 4px 12px;
  padding: 9px 14px; font-size: 13.5px; line-height: 1.45;
}
.tg-assistant .tga-turn-a {
  background: var(--tga-surface);
  border: 1px solid var(--tga-line);
  border-radius: 4px 12px 12px 12px;
  padding: 14px 16px;
  animation: tga-rise 260ms ease forwards;
}
.tg-assistant .tga-turn-a .lead { font-size: 14px; line-height: 1.55; color: var(--tga-ink); }
.tg-assistant .tga-turn-a .lead.err { color: var(--tga-danger); }
.tg-assistant .tga-turn-a .lead.ok { color: #0f5132; }

/* plan / change table */
.tg-assistant .tga-plan { margin-top: 12px; border: 1px solid var(--tga-line); border-radius: 10px; overflow: hidden; }
.tg-assistant .tga-plan-h {
  background: rgba(11,31,58,0.04); padding: 8px 12px;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--tga-muted); font-weight: 700;
}
.tg-assistant .tga-crow {
  display: grid; grid-template-columns: 1fr auto 16px auto; gap: 10px; align-items: center;
  padding: 10px 12px; border-top: 1px solid var(--tga-line); font-size: 13px;
}
.tg-assistant .tga-crow:first-of-type { border-top: none; }
.tg-assistant .tga-crow .who { font-weight: 600; }
.tg-assistant .tga-pill {
  font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px;
  background: #eef2f7; color: var(--tga-muted); white-space: nowrap;
}
.tg-assistant .tga-pill.to { background: rgba(11,31,58,0.10); color: var(--tga-navy); }
.tg-assistant .tga-pill.rm { background: #fdecea; color: var(--tga-danger); }
.tg-assistant .tga-pill.add { background: #e7f4ec; color: #0f5132; }
.tg-assistant .tga-arrow { color: #9aa7b8; text-align: center; }
.tg-assistant .tga-fields { display: grid; gap: 6px; padding: 10px 12px; }
.tg-assistant .tga-fields .f { display: flex; gap: 8px; font-size: 13px; }
.tg-assistant .tga-fields .f b { min-width: 130px; color: var(--tga-muted); font-weight: 600; }
.tg-assistant .tga-warn { padding: 8px 12px; border-top: 1px solid var(--tga-line); font-size: 12px; color: #7c4a03; background: #fff8ec; }

/* clarification */
.tg-assistant .tga-opts { display: grid; gap: 6px; margin-top: 10px; }
.tg-assistant .tga-opt {
  display: flex; align-items: center; gap: 10px;
  border: 1px solid var(--tga-line); background: #f7f9fc;
  border-radius: 9px; padding: 9px 12px; cursor: pointer; text-align: left;
  font-size: 13px; transition: background 130ms ease, border-color 130ms ease;
}
.tg-assistant .tga-opt:hover { background: #eef3fa; border-color: #cdd9e8; }
.tg-assistant .tga-opt .num {
  width: 20px; height: 20px; border-radius: 6px; flex: none;
  background: var(--tga-navy); color: #fff; font-size: 11px; font-weight: 700;
  display: grid; place-items: center;
}

/* action buttons */
.tg-assistant .tga-actions { display: flex; gap: 8px; margin-top: 14px; }
.tg-assistant .tga-btn {
  font-size: 13px; font-weight: 600; border-radius: 9px; padding: 8px 16px;
  cursor: pointer; border: 1px solid var(--tga-line); background: #fff; color: var(--tga-navy);
  transition: background 130ms ease, opacity 130ms ease, transform 110ms ease;
}
.tg-assistant .tga-btn:active { transform: scale(0.97); }
.tg-assistant .tga-btn.primary { background: var(--tga-navy); color: #fff; border-color: var(--tga-navy); }
.tg-assistant .tga-btn.primary:hover { opacity: 0.92; }
.tg-assistant .tga-btn.ghost:hover { background: #f3f5f8; }
.tg-assistant .tga-btn:disabled { opacity: 0.4; cursor: default; }
.tg-assistant .tga-confirmed {
  margin-top: 12px; padding: 10px 12px; border-radius: 9px;
  background: #e7f4ec; color: #0f5132; font-size: 13px; font-weight: 600;
}

/* execution result */
.tg-assistant .tga-exec { margin-top: 12px; border: 1px solid var(--tga-line); border-radius: 10px; overflow: hidden; }
.tg-assistant .tga-exec-rows { display: grid; }
.tg-assistant .tga-exec-row {
  display: flex; align-items: center; gap: 9px;
  padding: 9px 12px; border-top: 1px solid var(--tga-line); font-size: 13px;
}
.tg-assistant .tga-exec-row:first-child { border-top: none; }
.tg-assistant .tga-exec-row .who { font-weight: 600; }
.tg-assistant .tga-exec-row .detail { color: var(--tga-muted); font-size: 12px; }
.tg-assistant .tga-exec-row.ok { background: #f2faf5; }
.tg-assistant .tga-exec-row.ok svg { color: #16a34a; }
.tg-assistant .tga-exec-row.warn { background: #fff8ec; }
.tg-assistant .tga-exec-row.warn svg { color: #b45309; }
.tg-assistant .tga-exec-row.warn .detail { color: #7c4a03; }
.tg-assistant .tga-exec-row.bad { background: #fdecea; }
.tg-assistant .tga-exec-row.bad svg { color: var(--tga-danger); }
.tg-assistant .tga-exec-row.bad .detail { color: var(--tga-danger); }
.tg-assistant .tga-exec-row.muted svg { color: #9aa7b8; }

.tg-assistant .tga-stale {
  margin-top: 12px; padding: 12px 14px; border-radius: 10px;
  background: #fff8ec; border: 1px solid #f2dfb8;
  color: #7c4a03; font-size: 13px; line-height: 1.5;
}

/* ---------- communication preview (Phase 4A) ---------- */
.tg-assistant .tga-comm {
  margin-top: 12px; border: 1px solid var(--tga-line); border-radius: 12px; overflow: hidden;
  background: var(--tga-surface);
}
.tg-assistant .tga-comm-h {
  background: linear-gradient(180deg, rgba(11,31,58,0.06), transparent);
  padding: 10px 14px; font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase;
  font-weight: 700; color: var(--tga-navy); border-bottom: 1px solid var(--tga-line);
}
.tg-assistant .tga-comm-body { padding: 12px 14px; }
.tg-assistant .tga-comm-rows { display: grid; gap: 6px; }
.tg-assistant .tga-comm-rows > div { display: flex; gap: 10px; font-size: 13px; align-items: center; }
.tg-assistant .tga-comm-rows .k { min-width: 92px; color: var(--tga-muted); font-size: 12px; }
.tg-assistant .tga-comm-rows .v { font-weight: 600; display: inline-flex; align-items: center; gap: 4px; }
.tg-assistant .tga-comm-link {
  display: flex; align-items: center; gap: 8px; margin-top: 10px;
  font-size: 12.5px; padding: 8px 10px; background: #f7f9fc; border: 1px solid var(--tga-line); border-radius: 8px;
}
.tg-assistant .tga-comm-link a { color: var(--tga-accent); word-break: break-all; }
.tg-assistant .tga-comm-media { margin-top: 12px; }
.tg-assistant .tga-comm-media .mh {
  font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--tga-muted);
  font-weight: 700; margin-bottom: 8px;
}
.tg-assistant .tga-comm-media .mrow { margin-bottom: 8px; }
.tg-assistant .tga-comm-media .mbtn {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12.5px; font-weight: 600; color: var(--tga-navy);
  border: 1px solid var(--tga-line); background: #f7f9fc; border-radius: 8px;
  padding: 6px 10px; cursor: pointer;
}
.tg-assistant .tga-comm-media .mbtn:hover { background: #eef3fa; }
.tg-assistant .tga-comm-media .mplayer { margin-top: 8px; border-radius: 10px; overflow: hidden; background: #000; }
.tg-assistant .tga-comm-media .mplayer video { width: 100%; max-height: 320px; display: block; }
.tg-assistant .tga-comm-media .mplayer img { width: 100%; max-height: 320px; object-fit: contain; display: block; background: #0b1220; }

/* answer table */
.tg-assistant .tga-ans { margin-top: 12px; }
.tg-assistant .tga-ans table { width: 100%; border-collapse: collapse; font-size: 13px; }
.tg-assistant .tga-ans th, .tg-assistant .tga-ans td {
  text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--tga-line);
}
.tg-assistant .tga-ans th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--tga-muted); }
.tg-assistant .tga-ans-scroll { overflow-x: auto; }

/* ---------- states: loading / error / empty ---------- */
.tg-assistant .tga-skel {
  background: linear-gradient(90deg, #eef2f7 25%, #f6f9fc 37%, #eef2f7 63%);
  background-size: 400% 100%;
  animation: tga-shimmer 1.4s ease infinite;
  border-radius: 12px;
}
@keyframes tga-shimmer { 0% { background-position: 100% 0; } 100% { background-position: -100% 0; } }
.tg-assistant .tga-note {
  border: 1px solid var(--tga-line); background: var(--tga-surface);
  border-radius: 14px; padding: 26px; text-align: center; color: var(--tga-muted);
}
.tg-assistant .tga-retry {
  margin-top: 12px; border: 1px solid var(--tga-line); background: #f7f9fc;
  color: var(--tga-navy); font-weight: 600; font-size: 13px;
  border-radius: 9px; padding: 8px 16px; cursor: pointer;
}
.tg-assistant .tga-retry:hover { background: #eef3fa; }
`;
