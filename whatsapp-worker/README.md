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

## Persistent Storage Configuration

For WhatsApp Web to remember its session authorization, you **must** configure a persistent storage volume:
1. Create a **Volume** on Railway (e.g. size `1 GB` to store profile data).
2. Mount the volume on the worker container at `/data/wa-session`.
3. Set `WA_SESSION_DIR` to the **same** path, `/data/wa-session` (this is the default, so it works even if unset — but the volume mount path must match).
