"""User-installable Hermes memory-provider probe."""

from .provider import AgentMemoryProbeProvider


def register(ctx) -> None:
    ctx.register_memory_provider(AgentMemoryProbeProvider())
