"""Talentgram AI layer.

Deliberately tiny. One LLM provider (Anthropic), one entry point
(`ai.client.call_tool_json`), and thin task-specific modules on top
(`ai.casting_requirement` for the AI Casting Desk's Gate 1 parser).

No agent framework, no memory store, no vector DB, no provider abstraction
beyond a single ``CASTING_DESK_MODEL`` env swap. If a second AI feature
needs the LLM later it imports ``ai.client`` directly and adds its own
task module here — same as ``agents/modules/*`` sit on the WhatsApp agent
platform.
"""
