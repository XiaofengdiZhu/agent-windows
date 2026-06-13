"""Handler subpackage — importing this module registers all command handlers."""

# Import order matters: each sub-module uses the @handler decorator to
# self-register into agent_windows.daemon.router.HANDLERS on import.
# observe.py dispatches to HANDLERS["snapshot"] and HANDLERS["screenshot"] at
# runtime (not import time), so it does not matter that those handlers may be
# registered before or after observe — the lookups resolve whenever a request
# is actually handled.
from agent_windows.daemon.handlers import (  # noqa: F401
    batch,
    input,
    observe,
    screenshot,
    system,
    tree,
    vision,
)
