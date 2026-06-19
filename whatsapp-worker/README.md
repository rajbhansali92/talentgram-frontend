# WhatsApp Engine Worker Service Setup

This service runs a Python Playwright browser automation worker to send bulk templates and media updates to WhatsApp groups or individual numbers.

## Railway Service Environment Variables

Configure the following environment variables on the Railway service hosting this worker:

| Variable Name | Default Value | Description |
|---|---|---|
| `MONGO_URL` | *Required* | Complete connection URL to MongoDB (e.g. `mongodb+srv://...` or `mongodb://...`). |
| `MONGO_DB_NAME` | `talentgram` | The target database inside MongoDB. |
| `SESSION_DIR` | `/app/wa_session` | Path for Playwright persistent context storage. Map this to a Railway Volume to persist WhatsApp Web QR registration across restarts. |
| `WHATSAPP_URL` | `https://web.whatsapp.com` | WhatsApp Web entry point URL. |
| `QR_SCAN_TIMEOUT_MS` | `180000` | Timeout window (3 minutes) to scan the generated QR code before throwing a timeout failure. |
| `PAGE_LOAD_TIMEOUT_MS` | `60000` | General page load timeout (60 seconds). |
| `HEARTBEAT_SEC` | `15` | Polling frequency to verify that the session has not been dropped. |

## Persistent Storage Configuration

For WhatsApp Web to remember its session authorization, you **must** configure a persistent storage volume:
1. Create a **Volume** on Railway (e.g. size `1 GB` to store profile data).
2. Mount the volume on the worker container at `/app/wa_session`.
3. Set the `SESSION_DIR` environment variable to `/app/wa_session`.
