"""Handler subpackage — importing this module registers all command handlers."""

# Each sub-module uses @handler decorator to self-register into
# agent_windows.daemon.router.HANDLERS on import.
from agent_windows.daemon.handlers import (  # noqa: F401
    batch,
    input,
    observe,
    screenshot,
    system,
    tree,
    vision,
)
