# Project Context

## What Talentgram Is

Talentgram is a casting and talent agency platform ("Netflix meets Casting") that manages the full lifecycle of talent discovery, audition, and client presentation. It enables a talent agency to:

- Onboard talent through self-service application forms
- Manage a canonical talent database with portfolio media
- Run casting projects with audition submissions
- Present curated talent packages to clients via shareable review links
- Track client interest and feedback
- Broadcast messages to talent via WhatsApp

## Business Purpose

Talentgram replaces manual casting workflows (spreadsheets, email chains, WeTransfer links) with a structured platform. The agency manages talent profiles, creates casting projects, collects audition submissions, and shares curated shortlists with brand/agency clients through password-protected review links.

## User Types

### Admin (role: `admin`)
- Full platform access including user management, storage admin, and destructive operations
- Can approve/reject applications and submissions
- Can create projects, links, and manage all talent data
- Source of truth for talent profile decisions

### Team (role: `team`)
- Same as admin but without user management, storage admin, bulk delete, or talent deletion
- Can create/edit talents, projects, submissions
- Cannot approve applications or make destructive changes

### Talent (no login -- token-based access)
- Accesses the platform through invite links or self-service application
- Authenticated via email OTP or Google OAuth
- Gets a submitter token (short-lived JWT backed by persistent opaque access_token)
- Can submit applications, audition takes, and portfolio media
- Self-service portal for profile updates (`/portal`)

### Client / Viewer (no login -- token-based access)
- Accesses curated talent presentations through shared links
- Authenticated by identifying with name/email on the link page
- Gets a viewer token scoped to that specific link
- Can express interest, shortlist, reject, or request tests
- Can leave text or voice feedback
- Sees privatized names ("First L.") for confidentiality

## Technology Stack

### Frontend
- **Framework**: Next.js (App Router) with React 19
- **Styling**: Tailwind CSS + shadcn/ui (Radix primitives)
- **State**: React Context (UploadManagerContext) + local component state + localStorage persistence
- **Routing**: Next.js App Router for top-level pages; React Router for admin SPA and portal SPA
- **Subdomain routing**: Edge Middleware rewrites `apply.`, `submit.`, `review.`, `links.` subdomains to internal paths
- **Deployment**: Vercel

### Backend
- **Framework**: FastAPI (Python 3.11)
- **Database**: MongoDB Atlas via Motor (async driver)
- **Object Storage**:
  - **Cloudinary** (images, PDFs, and legacy videos)
  - **Cloudflare Stream** (HLS/`.m3u8` audition videos)
  - **Cloudflare R2** (raw uploads that feed Stream)
- **Email**: Resend > SendGrid > AWS SES (cascade fallback)
- **Auth**: JWT (HS256) + email OTP + Google OAuth2
- **Deployment**: Railway (service `talentgram-railway`, root `/backend`, `uvicorn server:app`)

### WhatsApp Worker
- **Runtime**: Python 3.11 + Playwright/Chromium
- **Purpose**: Bulk WhatsApp messaging via browser automation
- **Deployment**: Railway (Docker, persistent volume for session)

### Database
- **Engine**: MongoDB Atlas
- **DB Name**: `talentgram`
- **Driver**: Motor (async) with connection pool (maxPoolSize=50)

## Environments

| Environment | Frontend | Backend | Domain |
|---|---|---|---|
| Production | Vercel | Railway (`talentgram-railway`) | `talentgramagency.com` |
| Preview | Vercel preview deploys | Same production Railway backend | `*.vercel.app` |

There is no staging backend. See [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md).

### Production Subdomains
- `talentgramagency.com` -- Landing page
- `apply.talentgramagency.com` -- Talent application form
- `submit.talentgramagency.com` -- Project audition submission
- `review.talentgramagency.com` -- Admin dashboard
- `links.talentgramagency.com` -- Client review links

### Backend API
- Production: `https://talentgram-app-production.up.railway.app/api`

## Key Modules

### Backend Routers
| Router | Prefix | Purpose |
|---|---|---|
| `auth.py` | `/api/auth` | Login, OAuth, OTP, file upload |
| `password.py` | `/api/auth`, `/api/public` | Password reset/change |
| `talents.py` | `/api/talents` | Talent CRUD, media, bulk ops, tags |
| `applications.py` | `/api/applications`, `/api/public/apply` | Application lifecycle |
| `submissions.py` | `/api/submissions`, `/api/public` | Submission lifecycle, direct upload |
| `projects.py` | `/api/projects` | Project CRUD |
| `links.py` | `/api/links`, `/api/public/links` | Client link management and viewing |
| `casting_pipeline.py` | `/api/projects/{pid}/pipeline` | Kanban pipeline |
| `portal.py` | `/api/portal` | Talent self-service portal |
| `users.py` | `/api/users`, `/api/public/signup` | User/team management |
| `feedback.py` | `/api/public/links/{slug}/feedback` | Client feedback |
| `marketing.py` | `/api/marketing` | CRM clients and interactions |
| `workflow.py` | `/api/workflow` | Internal tasks and scouting |
| `whatsapp.py` | `/api/whatsapp` | WhatsApp broadcast engine |
| `notifications.py` | `/api/notifications` | In-app notifications |
| `cloudinary_admin.py` | `/api/admin/cloudinary` | Storage analytics and management |
| `drive_admin.py` | `/api/admin/drive` | Google Drive backup |

### Frontend Route Groups
| Group | Subdomain | Purpose |
|---|---|---|
| `(apply)` | `apply.*` | Talent application form |
| `(submit)` | `submit.*` | Project audition submission |
| `(review)` | `review.*` | Admin SPA (React Router inside Next.js) |
| `(links)` | `links.*` | Client review links |
| `portal` | any | Talent self-service portal |
| `(landing)` | root | Brand landing page |

## Project Goals

1. **Single source of truth**: Admin-managed talent profiles in `db.talents` are canonical
2. **Self-service onboarding**: Talent can apply and submit auditions without admin intervention
3. **Client presentation**: Curated, privacy-respecting talent packages via shareable links
4. **Media integrity**: Cloudinary-backed media with deduplication and sync rules
5. **Audit trail**: All profile changes, uploads, and decisions are logged
6. **Mobile-first**: Full responsive support including iOS Safari keyboard handling
