# Deployment Rules

## Service Topology

```
+--------------------+     +--------------------+     +--------------------+
| Frontend           |     | Backend API        |     | WhatsApp Worker    |
| Vercel             |     | Railway            |     | Railway            |
| Next.js App Router |     | FastAPI + Motor    |     | Python + Playwright|
| Auto-deploy on push|     | uvicorn / /backend |     | Docker / /whatsapp-|
|                    |     | (Nixpacks)         |     | worker             |
+--------------------+     +--------------------+     +--------------------+
         |                          |                          |
         +-------------+------------+--------------------------+
                        |
       +----------------+----------------+-----------------+
       |                |                |                 |
+------v-------+  +-----v---------+  +---v----------+  +---v----------+
| MongoDB Atlas |  | Cloudinary    |  | Cloudflare   |  | Cloudflare R2|
| (shared DB)   |  | (images, PDFs |  | Stream       |  | (raw uploads |
|               |  | legacy video) |  | (HLS video)  |  | for Stream)  |
+---------------+  +---------------+  +--------------+  +--------------+
```

Two Railway services in the `pacific-art` project:

| Service | Root dir | Start command | Purpose |
|---|---|---|---|
| `talentgram-railway` | `/backend` | `uvicorn server:app --host 0.0.0.0 --port $PORT` | FastAPI backend API |
| `talentgram-frontend - whatsapp` | `/whatsapp-worker` | `python worker.py` (via Dockerfile) | Playwright WhatsApp automation |

**Historical note**: An earlier `Emergent Platform` deployment target is documented by the stale `.emergent/emergent.yml` file (dated 2026-05-16). It is not the live backend and can be treated as historical.

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

## Railway Deployment (Backend API)

### Configuration
- **Service name**: `talentgram-railway`
- **Public URL**: `https://talentgram-app-production.up.railway.app`
- **Repository**: `rajbhansali92/talentgram-frontend`, branch `main`, root directory `/backend`
- **Builder**: Nixpacks (no `Dockerfile`, no `railway.json` at `/backend`)
- **Runtime**: Python 3.11 (`backend/runtime.txt`)
- **Entry point**: `backend/server.py` (single-file FastAPI app)
- **Start command** (configured in Railway dashboard): `uvicorn server:app --host 0.0.0.0 --port $PORT`
- **Plan**: pro

### Health Endpoint
- `GET /health` returns `{"status": "ok", "ocr": {...}}` — always returns 200 as long as the app boots. OCR readiness is best-effort and does not gate the probe.
- `GET /api/` returns `{"app": "talentgram", "ok": true}`.

### Startup Behavior
On startup, the backend runs migrations:
1. `run_media_duplicate_cleanup_migration` -- dedup media by public_id
2. `run_draft_talent_migration` -- move unsubmitted talents to submission_drafts
3. `run_draft_expiration_and_backfill` -- expire 30-day-old drafts, recalculate metrics
4. Index creation for all collections (idempotent)
5. Seed admin user using `ADMIN_EMAIL` + `ADMIN_PASSWORD` (creates admin if none exists)
6. EasyOCR model warmup (best-effort background task; do not gate on it)

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
| Cloudflare Stream / R2 credentials | -- | Optional; used for HLS video pipeline. See 04_MEDIA_RULES. |

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
- **Dialog handling**: `modals.py` sweeps for blocking `aria-modal` dialogs at login, pre-open, search, and pre-send. Known-benign dialogs are auto-dismissed (Escape → Close → whitelist button). Unknown dialogs are captured to `whatsapp_dom_snapshots` and the send fails gracefully (retryable). Check logs for `UNKNOWN_DIALOG` events after WhatsApp Web updates.

## Rollback Procedure

### Frontend (Vercel)
1. Go to Vercel dashboard
2. Find the previous successful deployment
3. Click "Promote to Production" on the earlier deployment
4. Vercel instantly serves the previous build

### Backend (Railway)
1. Identify the last known-good commit
2. Redeploy that specific commit from Railway's dashboard **or** the CLI:
   ```
   railway redeploy --service talentgram-railway --from-source --yes
   ```
   `--from-source` pulls the latest commit from the configured GitHub source. To roll back further, revert on `main` and redeploy.
3. Confirm the running commit via `railway status --json` (look for `commitHash` under the `talentgram-railway` service).
4. Note: database migrations that ran on startup may need manual reversal.

### WhatsApp Worker (Railway)
1. Redeploy from CLI:
   ```
   railway redeploy --service "talentgram-frontend - whatsapp" --from-source --yes
   ```
2. Verify WhatsApp session is still valid (may need re-scan)

### KNOWN OPERATIONAL ISSUE: GitHub → Railway Auto-Deploy — Status Uncertain

As of 2026-07-04, auto-deploy may have reconnected. Earlier (2026-07-02–03) pushes did NOT trigger Railway deploys, but the 2026-07-04 push appeared to auto-deploy both services. Verification needed on the next push (see [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md) Issue #0).

If auto-deploy is still disconnected, you must **manually redeploy after every backend push**:

```
railway redeploy --service talentgram-railway --from-source --yes
railway redeploy --service "talentgram-frontend - whatsapp" --from-source --yes
```

See [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md) for the tracked action item.

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
1. Verify the running commit hash matches `main`:
   ```
   railway status --json | grep commitHash
   ```
   Both `talentgram-railway` and `talentgram-frontend - whatsapp` should show the same commit as `git rev-parse origin/main`.
2. Hit health endpoint: `GET /health` (should return `{"status": "ok", "ocr": {...}}`)
3. Hit API root: `GET /api/` (should return `{app: "talentgram", ok: true}`)
4. Check Railway logs for startup migration errors and OCR warmup crashes
5. Test affected API endpoints
6. Verify no CORS issues from frontend

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
