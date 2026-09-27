"""Adapter-local persistent binding abstraction.

Storage and create/rebind control-plane behavior intentionally live outside this module.
"""

from dataclasses import dataclass
from typing import Protocol


class BindingUnavailable(RuntimeError):
    """The requested adapter-local binding does not exist or cannot be used."""


class BindingGenerationMismatch(BindingUnavailable):
    """The binding exists, but not at the request's generation."""


@dataclass(frozen=True)
class ResolvedBinding:
    binding_id: str
    generation: int
    thread_id: str


class BindingResolver(Protocol):
    """Resolve an existing binding without creating or rebinding it."""

    async def resolve(self, binding_id: str, generation: int) -> ResolvedBinding:
        """Return the exact existing binding or raise a BindingUnavailable subtype."""


async def resolve_existing(
    resolver: BindingResolver,
    binding_id: str,
    generation: int,
) -> ResolvedBinding:
    """Defend against a resolver returning a different binding or generation."""
    resolved = await resolver.resolve(binding_id, generation)
    if not isinstance(resolved, ResolvedBinding):
        raise BindingUnavailable("binding resolver returned an invalid result")
    if resolved.binding_id != binding_id:
        raise BindingUnavailable("binding resolver returned a different binding")
    if resolved.generation != generation:
        raise BindingGenerationMismatch("binding generation mismatch")
    if not resolved.thread_id:
        raise BindingUnavailable("binding has no thread")
    return resolved
