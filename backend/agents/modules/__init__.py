"""Domain modules for the WhatsApp Agent Platform.

Each file here registers exactly one AgentDefinition and owns exactly one
backend module (per the "each agent only has access to the module it
owns" requirement). Importing this package triggers every module's
`register()` call — see `agents.modules.register_all()`, invoked once at
app startup from server.py.
"""
from . import crm, casting_pipeline, whatsapp_campaign_agent

_MODULES = [crm, casting_pipeline, whatsapp_campaign_agent]


def register_all() -> None:
    for mod in _MODULES:
        mod.register()
