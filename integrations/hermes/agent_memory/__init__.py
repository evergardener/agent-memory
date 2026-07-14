"""Hermes user-installable Agent Memory provider."""


def register(ctx) -> None:
    from .provider import AgentMemoryProvider

    ctx.register_memory_provider(AgentMemoryProvider())
