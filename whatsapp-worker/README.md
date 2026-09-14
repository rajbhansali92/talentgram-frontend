# WhatsApp Engine Worker Service Setup

This service runs a Python Playwright browser automation worker to send bulk templates and media updates to WhatsApp groups or individual numbers.

## Railway Service Environment Variables

Configure the following environment variables on the Railway service hosting this worker:

| Variable Name | Default Value | Description |
|---|---|---|
| `MONGO_URL` | *Required* | Complete connection URL to MongoDB. Use the **same** value as the backend so the worker writes the session/QR to the database the admin UI reads. |
| `MONGO_DB_NAME` | `talentgram` | The target database inside MongoDB. |
| `WA_SESSION_DIR` | `/data/wa-session` | Path for Playwright persistent context storage. Map this to a Railway Volume to persist WhatsApp Web registration across restarts. (`SESSION_DIR` is also accepted as an alias.) |
| `WHATSAPP_URL` | `https://web.whatsapp.com` | WhatsApp Web entry point URL. |
| `WA_QR_TIMEOUT_MS` | `90000` | Timeout window to scan the generated QR code before throwing a timeout failure. |
| `WA_PAGE_LOAD_MS` | `60000` | General page load timeout (60 seconds). |
| `WA_HEARTBEAT_SEC` | `60` | Polling frequency to verify that the session has not been dropped. |
| `WORKER_ID` | `default` | Multi-worker support — this process's stable identity. Leave UNSET on Worker 1's existing service (it must keep behaving exactly as it always has). A second/Nth worker's Railway service sets this to the `id` returned by `POST /api/whatsapp/workers` (e.g. `wa-a1b2c3d4`). |
| `AGENTS_BACKEND_URL` | `https://api.talentgramagency.com` | Base URL of the shared FastAPI backend — same value for every worker. |
| `AGENTS_INBOUND_SECRET` | *Required for inbound* | Shared secret for the `/api/agents/whatsapp/*` transport endpoints — same value for every worker. |

## Multi-Worker Setup (adding a second WhatsApp number)

Each additional worker is the exact same Docker image/codebase deployed as
its **own** Railway service, with its own volume and a distinct `WORKER_ID`.
Never point two services at the same volume or the same `WA_SESSION_DIR`
path on the same volume — Playwright's persistent browser profile is
exclusive-locked per process, and sharing one would corrupt both sessions.

1. Register the worker's identity first: `POST /api/whatsapp/workers`
   `{"label": "Worker 2"}` (admin auth) on the backend. Note the returned
   `id` (e.g. `wa-a1b2c3d4`).
2. Create a new Railway service from this same repo, with its Root
   Directory set to `whatsapp-worker` (same Dockerfile/railway.json as
   Worker 1 — no code differences between workers).
3. Attach a **new**, separate Volume to this service, mounted at
   `/data/wa-session` (the default `WA_SESSION_DIR` — fine to reuse the
   same mount *path* since it's a different physical volume).
4. Set env vars: `MONGO_URL`, `MONGO_DB_NAME`, `AGENTS_BACKEND_URL`,
   `AGENTS_INBOUND_SECRET` — copied from Worker 1's service (shared
   backend + database is intentional) — plus `WORKER_ID` set to the id
   from step 1. Everything else may keep its default.
5. Deploy, then watch the logs for `worker: starting as worker_id=...`
   followed by the usual QR-capture sequence.
6. Fetch `GET /api/whatsapp/workers/{worker_id}/session` (admin auth) and
   paste its `qr_code_base64` value directly into a browser address bar —
   a `data:image/png;base64,...` URI renders as an image — to view and
   scan the QR with the second number's WhatsApp (Linked Devices → Link a
   Device). No frontend UI is required for this step.
7. Poll the same endpoint until `status` reads `authenticated` and
   `connected_phone_number` is populated.
8. Do not map any real agent groups or send real campaigns through this
   worker until the concurrency/isolation/failure verification checklist
   passes (see the multi-worker implementation plan).

## Persistent Storage Configuration

For WhatsApp Web to remember its session authorization, you **must** configure a persistent storage volume:
1. Create a **Volume** on Railway (e.g. size `1 GB` to store profile data).
2. Mount the volume on the worker container at `/data/wa-session`.
3. Set `WA_SESSION_DIR` to the **same** path, `/data/wa-session` (this is the default, so it works even if unset — but the volume mount path must match).
