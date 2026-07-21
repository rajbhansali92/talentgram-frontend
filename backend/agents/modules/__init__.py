"""Domain modules for the WhatsApp Agent Platform.

Each file here registers exactly one AgentDefinition and owns exactly one
backend module (per the "each agent only has access to the module it
owns" requirement). Importing this package triggers every module's
`register()` call — see `agents.modules.register_all()`, invoked once at
app startup from server.py.
"""
from . import crm

_MODULES = [crm]


def register_all() -> None:
    for mod in _MODULES:
        mod.register()
