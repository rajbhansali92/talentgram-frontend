# Deployment Rules

## Service Topology

```
+--------------------+     +--------------------+     +--------------------+
| Frontend           |     | Backend            |     | WhatsApp Worker    |
| Vercel             |     | Emergent Platform  |     | Railway            |
| Next.js App Router |     | FastAPI + Motor    |     | Python + Playwright|
| Auto-deploy on push|     |                    |     | Docker             |
+--------------------+     +--------------------+     +--------------------+
         |                          |                          |
         +-------------+------------+--------------------------+
                        |
               +--------v---------+
               | MongoDB Atlas    |
               | (shared DB)      |
               +------------------+
                        |
               +--------v---------+
               | Cloudinary       |
               | (shared storage) |
               +------------------+
```

## GitHub Workflow

### Repository Structure
- Single monorepo: `talentgram-frontend`
- Contains: `frontend/`, `backend/`, `whatsapp-worker/`, test files, scripts
- No CI/CD pipeline (no `.github/workflows/`)
- No automated tests in CI

### Branching Strategy

**Pattern**: Feature branches merged to `main`

**Branch naming conventions**:
- `feature/*` -- New features (e.g., `feature/direct-cloudinary-video-upload`)
- `fix/*` -- Bug fixes (e.g., `fix/application-finalize-blank-wipe`)
- `security/*` -- Security hardening (e.g., `security/portal-auth-and-submitter-token-hardening`)
- Descriptive names for larger efforts (e.g., `frontend-audit-fixes-phase-1`)

**Merge strategy**:
- Merge commits (not squash or rebase)
- Merge commit messages follow pattern: `Merge: branch-name`
- Most work is committed directly to `main` as linear history
- Feature branches used selectively for larger or security-sensitive changes

**No tags, no release branches, no staging branches.**

### Commit Message Conventions
- Conventional commits style: `feat(scope):`, `fix(scope):`, `chore:`, `refactor:`, `docs:`, `style:`
- Scope examples: `audition-video`, `talent-sync`, `security`, `cloudinary`, `backend`, `mobile`, `safari`
- Multi-line descriptions for complex changes
- Phase-based commits for large features: "Phase 1.6", "Phase 1.7"

## Vercel Deployment (Frontend)

### Configuration
- **Project**: `talentgram-frontend` (org: `team_E7loemoUgQyC3rVon9WlSL17`)
- **Framework**: Auto-detected Next.js
- **No `vercel.json`** -- relies on Vercel defaults
- **Auto-deploy**: Pushes to `main` trigger production deploy
- **Preview deploys**: Every push to any branch gets a preview URL

### Environment Variables (Build-time)
| Variable | Purpose |
|---|---|
| `REACT_APP_BACKEND_URL` / `NEXT_PUBLIC_BACKEND_URL` | Backend API URL |
| `REACT_APP_GOOGLE_CLIENT_ID` / `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD` | Feature flag for direct Cloudinary upload |

### Domain Configuration
| Subdomain | Rewrites To |
|---|---|
| `talentgramagency.com` | `/` (landing) |
| `apply.talentgramagency.com` | `/(apply)/apply` |
| `submit.talentgramagency.com` | `/(submit)/submit/[slug]` |
| `review.talentgramagency.com` | `/(review)/admin/[[...catchall]]` |
| `links.talentgramagency.com` | `/(links)/l/[slug]` |

Subdomain routing handled by Next.js Edge Middleware (`src/middleware.ts`).

### CORS Origins (Production)
```
https://talentgramagency.com
https://www.talentgramagency.com
https://apply.talentgramagency.com
https://submit.talentgramagency.com
https://review.talentgramagency.com
https://links.talentgramagency.com
```

Plus regex pattern for Vercel preview deploys.

## Emergent Platform Deployment (Backend)

### Configuration
- **Base image**: `fastapi_react_mongo_shadcn_base_image_cloud_arm:release-17042026-1`
- **Runtime**: Python 3.11 (`backend/runtime.txt`)
- **Entry point**: `backend/server.py` (single-file FastAPI app)
- **Config**: `.emergent/emergent.yml`

### Startup Behavior
On startup, the backend runs migrations:
1. `run_media_duplicate_cleanup_migration` -- dedup media by public_id
2. `run_draft_talent_migration` -- move unsubmitted talents to submission_drafts
3. `run_draft_expiration_and_backfill` -- expire 30-day-old drafts, recalculate metrics
4. Index creation for all collections (idempotent)
5. Seed admin user using `ADMIN_EMAIL` + `ADMIN_PASSWORD` (creates admin if none exists)

### Required Environment Variables
| Variable | Description |
|---|---|
| `MONGO_URL` | MongoDB Atlas connection string |
| `DB_NAME` | Database name (`talentgram`) |
| `JWT_SECRET` | JWT signing secret (MUST be set, startup fails otherwise) |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name |
| `CLOUDINARY_API_KEY` | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret |
| `ADMIN_EMAIL` | Seed admin email (uses `os.environ[]` -- app crashes if unset) |
| `ADMIN_PASSWORD` | Seed admin password (uses `os.environ[]` -- app crashes if unset) |

### Optional Environment Variables
| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `"talentgram"` | App name (used in Cloudinary folder paths) |
| `CORS_ORIGINS` | `""` | Comma-separated allowed origins |
| `DIRECT_VIDEO_UPLOAD` | `"false"` | Feature flag: direct browser-to-Cloudinary video upload |

## Railway Deployment (WhatsApp Worker)

### Configuration
- **Dockerfile**: `whatsapp-worker/Dockerfile`
- **Base**: Python 3.11-slim + Playwright Chromium
- **Entry**: `python worker.py`
- **Persistent volume**: `/app/wa_session` (WhatsApp session storage)

### Required Environment Variables
| Variable | Description |
|---|---|
| `MONGO_URL` | MongoDB connection string (shared with backend) |
| `SESSION_DIR` | Session storage path (default: `/data/wa-session`) |

### Operational Notes
- Requires persistent volume for WhatsApp Web session persistence
- QR code for WhatsApp authentication stored in MongoDB for admin UI display
- Circuit breaker pauses batch after 5 consecutive send failures
- Human-like delays: 8-15 seconds between messages

## Rollback Procedure

### Frontend (Vercel)
1. Go to Vercel dashboard
2. Find the previous successful deployment
3. Click "Promote to Production" on the earlier deployment
4. Vercel instantly serves the previous build

### Backend (Emergent)
1. Identify the last known-good commit
2. Revert to that commit: `git revert <bad-commit>` or `git reset`
3. Push to trigger redeploy
4. Note: Database migrations that ran on startup may need manual reversal

### WhatsApp Worker (Railway)
1. Railway dashboard > Deployments
2. Redeploy from previous successful deployment
3. Verify WhatsApp session is still valid (may need re-scan)

### Database
- No automated rollback for MongoDB changes
- Manual script-based migrations in `backend/migrations/`
- `backend/scripts/backup_db.py` script available for manual backups
- Consider running backup before any migration or major deploy

## Verification Procedure

### After Frontend Deploy
1. Check Vercel deployment status (green checkmark)
2. Visit each subdomain and verify page loads:
   - `talentgramagency.com` -- landing page
   - `apply.talentgramagency.com/apply` -- application form
   - `review.talentgramagency.com/admin` -- admin login
3. Test the specific feature that was deployed
4. Check browser console for errors

### After Backend Deploy
1. Hit health endpoint: `GET /health` (should return `{ok: true}`)
2. Hit API root: `GET /api/` (should return `{app: "talentgram", ok: true}`)
3. Check startup migrations ran without errors (check logs)
4. Test affected API endpoints
5. Verify no CORS issues from frontend

### After WhatsApp Worker Deploy
1. Check Railway deployment logs for startup success
2. Verify MongoDB connection established
3. Check WhatsApp session status via admin UI
4. If session lost, re-scan QR code

## Rules for Production Releases

1. **No CI/CD** -- all deploys are triggered by git push or platform UI
2. **Test locally first** -- there is no staging environment
3. **Feature flags for risky changes** -- use `DIRECT_VIDEO_UPLOAD` pattern for gradual rollout
4. **Security changes get their own branch** -- `security/*` prefix
5. **Database changes are forward-only** -- migrations run on startup, no automatic rollback
6. **Backup before migrations** -- use `backend/scripts/backup_db.py` for data safety
7. **Monitor after deploy** -- check logs, health endpoints, and affected flows
8. **Merge commits preserve history** -- use merge (not squash) so individual commits are traceable
