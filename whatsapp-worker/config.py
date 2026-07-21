"""
WhatsApp Worker — Configuration
Reads all settings from environment variables with safe defaults.
"""
import os


# MongoDB connection
MONGO_URL: str = os.environ["MONGO_URL"]
MONGO_DB_NAME: str = os.environ.get("MONGO_DB_NAME", "talentgram")

# Playwright session storage (Railway persistent volume).
# Accept either env var name so deployment can't silently lose the session:
# the code historically read WA_SESSION_DIR while the README documented
# SESSION_DIR. Either now works; default matches the Railway volume mount.
SESSION_DIR: str = (
    os.environ.get("WA_SESSION_DIR")
    or os.environ.get("SESSION_DIR")
    or "/data/wa-session"
)

# Worker behaviour defaults (overridden by whatsapp_config collection at runtime)
DEFAULT_MIN_DELAY: int = int(os.environ.get("WA_MIN_DELAY_SEC", "8"))
DEFAULT_MAX_DELAY: int = int(os.environ.get("WA_MAX_DELAY_SEC", "15"))
DEFAULT_MAX_RETRIES: int = int(os.environ.get("WA_MAX_RETRIES", "3"))
DEFAULT_CIRCUIT_BREAKER: int = int(os.environ.get("WA_CIRCUIT_BREAKER", "5"))

# Job poll interval when queue is empty (seconds)
IDLE_POLL_SEC: int = int(os.environ.get("WA_IDLE_POLL_SEC", "5"))

# Session heartbeat interval (seconds)
HEARTBEAT_SEC: int = int(os.environ.get("WA_HEARTBEAT_SEC", "60"))

# If a job has status="sending" for longer than this, treat it as orphaned
ORPHAN_TIMEOUT_SEC: int = int(os.environ.get("WA_ORPHAN_TIMEOUT_SEC", "300"))

# WhatsApp Web URL
WHATSAPP_URL: str = "https://web.whatsapp.com"

# QR code display timeout (ms) — how long to wait for admin to scan
QR_SCAN_TIMEOUT_MS: int = int(os.environ.get("WA_QR_TIMEOUT_MS", "90000"))

# Page load timeout (ms)
PAGE_LOAD_TIMEOUT_MS: int = int(os.environ.get("WA_PAGE_LOAD_MS", "60000"))

# Log level
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# --- Inbound Agent Platform transport adapter -------------------------------
# How often to poll each known agent-mapped group for new messages.
INBOUND_POLL_SEC: int = int(os.environ.get("WA_INBOUND_POLL_SEC", "8"))

# How often to refresh the list of agent-mapped group names from the backend.
INBOUND_GROUPS_REFRESH_SEC: int = int(os.environ.get("WA_INBOUND_GROUPS_REFRESH_SEC", "120"))

# How long a processed message id is remembered for dedup (survives worker
# restarts via a Mongo TTL index — see whatsapp_inbound_seen).
INBOUND_DEDUP_TTL_SEC: int = int(os.environ.get("WA_INBOUND_DEDUP_TTL_SEC", str(48 * 3600)))

# Base backend URL + shared secret for POSTing to the Agent Platform.
AGENTS_BACKEND_URL: str = os.environ.get("AGENTS_BACKEND_URL", "https://api.talentgramagency.com")
AGENTS_INBOUND_SECRET: str = os.environ.get("AGENTS_INBOUND_SECRET", "")

# The connected WhatsApp account's own display name, as WhatsApp Web renders
# it inside data-pre-plain-text on the account's own messages (confirmed live
# 2026-07-21: "Talentgram Team"). Used as a message-direction fallback for
# messages that lack a tail/aria-label marker (consecutive messages from the
# same sender with nothing in between don't render a fresh tail).
WA_SELF_DISPLAY_NAME: str = os.environ.get("WA_SELF_DISPLAY_NAME", "Talentgram Team")

# Set to "false" to disable the inbound listener entirely (outbound sending
# keeps working) — an emergency kill switch that needs no redeploy of the
# Agent Platform or Marketing module, just a worker restart.
INBOUND_LISTENER_ENABLED: bool = os.environ.get("WA_INBOUND_LISTENER_ENABLED", "true").lower() not in ("false", "0", "no")
