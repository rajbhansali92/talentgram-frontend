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
        # group_members (2026-08-27, Command Enhancement P0/P1 — explicit
        # product decision) — was "allowlist" restricted to one phone
        # number; every number currently in (or later added to) this group
        # may now command the agent, matching casting-agent/crm-agent's
        # existing boundary. The agent's own WhatsApp number
        # (+91 93212 90688) remains the agent identity elsewhere in the
        # architecture (Gunwanti mark-based UPLOAD/SEND) — unrelated to who
        # may issue commands here.
        security_mode="group_members",
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
        # Shared Interactive Disambiguation Engine (Sprint 1, 2026-08-09) —
        # single-slot per (agent_id, phone), same shape as whatsapp_
        # conversations, since a phone can only have one pending choice at
        # a time.
        await db["whatsapp_agent_disambiguation"].create_index(
            [("agent_id", 1), ("phone", 1)], unique=True, name="agent_phone_unique"
        )
        await db["whatsapp_agent_disambiguation"].create_index(
            "expires_at", expireAfterSeconds=0, name="disambiguation_ttl"
        )
    except Exception:
        logger.exception("whatsapp agent platform index creation failed (non-fatal)")

    # Production incident (2026-08-25) — casting-agent's config had
    # group_names=[] for an unknown period, which resolve_agent_for_group
    # treats as "no groups configured", so no message from any group
    # (including its real, intended one) could ever route to it. Nothing
    # logged or flagged this — the admin only found out when a real
    # message produced no reply at all. This check makes that state
    # impossible to miss on every future startup.
    try:
        broken = await registry.find_agents_with_empty_group_names()
        if broken:
            logger.error(
                "WhatsApp agent platform: %d active agent(s) have EMPTY group_names "
                "and cannot receive ANY WhatsApp message: %s — set whatsapp_agent_config."
                "group_names for these agent(s) or they will silently drop every command.",
                len(broken), broken,
            )
    except Exception:
        logger.exception("whatsapp agent platform empty-group-names health check failed (non-fatal)")

    logger.info("WhatsApp agent platform ready: %d agent(s) registered", len(registry.list_agents()))
