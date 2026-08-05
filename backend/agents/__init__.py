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
    await registry.seed_agent_config(
        "casting-agent",
        group_names=["Talentgram Casting Pipeline"],
        allowed_senders=[],
        # Anyone currently in the WhatsApp group can approve a move — an
        # explicit choice for this agent, not the allowlist default.
        security_mode="group_members",
    )
    await registry.seed_agent_config(
        "whatsapp-campaign-agent",
        group_names=["Talentgram WhatsApp Agent"],
        allowed_senders=[],
        # allowlist, not group_members — a launched campaign sends real
        # messages to real recipients, so this stays fail-closed until an
        # admin explicitly authorizes specific phone numbers (same generic
        # PUT /api/agents/whatsapp/config/{agent_id} endpoint every agent
        # already uses; no new endpoint needed).
        security_mode="allowlist",
    )

    try:
        await db["whatsapp_conversations"].create_index(
            [("agent_id", 1), ("phone", 1)], unique=True, name="agent_phone_unique"
        )
        await db["whatsapp_conversations"].create_index(
            "expires_at", expireAfterSeconds=0, name="conversations_ttl"
        )
        await db["whatsapp_agent_sessions"].create_index(
            [("agent_id", 1), ("phone", 1)], unique=True, name="agent_phone_unique"
        )
        await db["whatsapp_agent_sessions"].create_index(
            "expires_at", expireAfterSeconds=0, name="sessions_ttl"
        )
        await db["whatsapp_agent_undo"].create_index(
            [("agent_id", 1), ("phone", 1)], unique=True, name="agent_phone_unique"
        )
        await db["whatsapp_agent_undo"].create_index(
            "expires_at", expireAfterSeconds=0, name="undo_ttl"
        )
        await db["whatsapp_agent_audit_log"].create_index([("timestamp", -1)])
        await db["whatsapp_agent_audit_log"].create_index([("agent_id", 1), ("timestamp", -1)])
        await db[registry.CONFIG_COLLECTION].create_index("agent_id", unique=True)
        # Concurrent Task Engine (2026-08-05) — MANY docs per (agent_id,
        # phone), unlike whatsapp_conversations' single-slot unique index
        # above, so this index is deliberately non-unique.
        await db["whatsapp_agent_tasks"].create_index([("agent_id", 1), ("phone", 1)])
        await db["whatsapp_agent_tasks"].create_index(
            [("agent_id", 1), ("operation_id", 1)], unique=True, name="agent_operation_unique"
        )
        # The reply-to-message resolver's lookup path — sparse since most
        # tasks won't have a confirmation_message_id yet at creation time.
        await db["whatsapp_agent_tasks"].create_index(
            [("agent_id", 1), ("confirmation_message_id", 1)],
            sparse=True, name="agent_confirmation_message_id",
        )
        await db["whatsapp_agent_tasks"].create_index(
            "expires_at", expireAfterSeconds=0, name="tasks_ttl"
        )
    except Exception:
        logger.exception("whatsapp agent platform index creation failed (non-fatal)")

    logger.info("WhatsApp agent platform ready: %d agent(s) registered", len(registry.list_agents()))
