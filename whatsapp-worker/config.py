"""
WhatsApp Worker — Configuration
Reads all settings from environment variables with safe defaults.
"""
import os


# MongoDB connection
MONGO_URL: str = os.environ["MONGO_URL"]
MONGO_DB_NAME: str = os.environ.get("MONGO_DB_NAME", "talentgram")

# Playwright session storage (Railway persistent volume)
SESSION_DIR: str = os.environ.get("WA_SESSION_DIR", "/data/wa-session")

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
