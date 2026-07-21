"""Talentgram WhatsApp Agent Platform.

Generic infrastructure for building group-scoped WhatsApp command agents:
registry, conversation state, parsing, confirmation, dispatch, and audit
logging. No module in this package (other than `modules/`) knows anything
about a specific domain (CRM, Projects, Casting, ...) — domain logic lives
entirely in `agents/modules/*.py`, each registering one `AgentDefinition`.

See `docs/claude/` (or the agent platform design doc) for the full
architecture. Entry point for inbound messages: `dispatcher.handle_inbound_message`.
"""
import logging

logger = logging.getLogger(__name__)


async def ensure_agents_ready() -> None:
    """Called once at app startup (mirrors whatsapp.ensure_whatsapp_ready):
    registers every domain module's AgentDefinition, seeds default DB
    routing config for any agent that doesn't have one yet, and creates
    indexes for the platform's own collections. Safe to call on every
    boot — registration and config-seeding are both idempotent."""
    from core import db
    from agents import modules, registry

    modules.register_all()

    await registry.seed_agent_config(
        "crm-agent",
        group_names=["Talentgram CRM"],
        allowed_senders=[],  # intentionally empty — an admin must explicitly allowlist senders
    )

    try:
        await db["whatsapp_conversations"].create_index(
            [("agent_id", 1), ("phone", 1)], unique=True, name="agent_phone_unique"
        )
        await db["whatsapp_conversations"].create_index(
            "expires_at", expireAfterSeconds=0, name="conversations_ttl"
        )
        await db["whatsapp_agent_audit_log"].create_index([("timestamp", -1)])
        await db["whatsapp_agent_audit_log"].create_index([("agent_id", 1), ("timestamp", -1)])
        await db[registry.CONFIG_COLLECTION].create_index("agent_id", unique=True)
    except Exception:
        logger.exception("whatsapp agent platform index creation failed (non-fatal)")

    logger.info("WhatsApp agent platform ready: %d agent(s) registered", len(registry.list_agents()))
