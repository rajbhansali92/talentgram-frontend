# Talentgram — Portfolio Link Engine (PRD)

## Original Problem
Build a production-grade client-review system for Talentgram (talent agency). Core function: Admin picks talents → system generates a shareable web link ("Talentgram x {Brand/Talent}") → client opens link → reviews portfolios → takes actions (Shortlist / Interested / Not for this / Not sure) + comments → system tracks everything (views, unique viewers, downloads). Tone: "Netflix meets Casting — Luxury portfolio meets decision dashboard."

## User Personas
- **Admin (Talentgram team)** — creates/edits talents, uploads media, generates links with visibility toggles, reviews results
- **Client (Brand / Casting / Agency)** — opens the secure link, identifies self (name+email hard gate), browses curated talents, makes decisions

## Architecture
- **Backend**: FastAPI + Motor (async MongoDB), JWT (admin + viewer roles), bcrypt, Emergent Object Storage (S3-compatible) — single `/app/backend/server.py`
- **Frontend**: React 19 + react-router-dom v7 + Tailwind + shadcn/ui + sonner — pages under `/app/frontend/src/pages`
- **Auth**: Seeded admin `admin@talentgram.com` / `Admin@123` at startup; viewer tokens issued per slug on identify

## Status — Phase 1 MVP (Implemented 2026-04)
### Done
- ✅ Admin JWT login, seeded admin account
- ✅ Talent CRUD + media management (categories: indian / western / portfolio / video) with cover selection
- ✅ Emergent Object Storage integration with CDN-style `/api/files/{path}` serving
- ✅ Link generator with 11 visibility toggles + multi-select talents + `Talentgram x ...` naming
- ✅ Link history: list, open, copy, WhatsApp share (wa.me), duplicate, delete
- ✅ Client view: hard identity gate (name+email → viewer JWT), Tetris-grid portfolio, detail overlay with slider, intro video, instagram, work links, downloads
- ✅ Per-talent actions (shortlist/interested/not_for_this/not_sure) + comments, upsert semantics
- ✅ Download tracking (who/what/when), gated by visibility.download toggle
- ✅ Results dashboard: viewers list, download log, per-talent breakdown with action counts + comments
- ✅ Analytics: total views, unique viewers per link
- ✅ Mobile-responsive dark UI (Obsidian/Pearl palette, Outfit + Manrope fonts)
- ✅ Testing: 100% backend pytest + 100% frontend Playwright E2E

## Phase 2 — Backlog
### P1 (high-value)
- [ ] Private link permissions (per-email ACL, password-protected links)
- [ ] Budget form per project (editable pricing per talent)
- [ ] Project-based auto talent selection (tags/categories → auto multi-select)
- [ ] Pagination for talents/links (once lists grow beyond 1000)
- [ ] Server-side validation on media uploads (max size, MIME allowlist)

### P2 (nice-to-have)
- [ ] Advanced analytics charts (daily views, action funnel)
- [ ] Bulk import talents (CSV)
- [ ] Email notification when client completes review
- [ ] Presigned/tokenized file URLs for stricter privacy
- [ ] Activity timeline on Results page

## Test Credentials
See `/app/memory/test_credentials.md`.
