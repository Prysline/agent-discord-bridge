"""Adapter-local persistent binding abstraction.

Storage and create/rebind control-plane behavior intentionally live outside this module.
"""

from dataclasses import dataclass
from typing import Iterable, Protocol


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


class InMemoryBindingResolver:
    """Resolve only explicitly configured existing Codex sessions."""

    def __init__(self, bindings: Iterable[ResolvedBinding]) -> None:
        self._bindings: dict[tuple[str, int], ResolvedBinding] = {}
        for binding in bindings:
            if not binding.binding_id.strip() or not binding.thread_id.strip():
                raise ValueError("binding and native thread identifiers must be non-empty")
            if isinstance(binding.generation, bool) or not isinstance(binding.generation, int):
                raise ValueError("binding generation must be an integer")
            key = (binding.binding_id, binding.generation)
            if key in self._bindings:
                raise ValueError("duplicate exact native binding mapping")
            if any(
                value.thread_id == binding.thread_id
                for value in self._bindings.values()
            ):
                raise ValueError("native thread is already managed by another binding")
            self._bindings[key] = binding

    async def resolve(self, binding_id: str, generation: int) -> ResolvedBinding:
        resolved = self._bindings.get((binding_id, generation))
        if resolved is not None:
            return resolved
        if any(key_binding == binding_id for key_binding, _ in self._bindings):
            raise BindingGenerationMismatch("binding generation mismatch")
        raise BindingUnavailable("binding is unavailable")

    def publish(self, binding: ResolvedBinding) -> None:
        self.validate_publish(binding)
        self._bindings[(binding.binding_id, binding.generation)] = binding

    def validate_publish(self, binding: ResolvedBinding) -> None:
        if not binding.binding_id.strip() or not binding.thread_id.strip():
            raise ValueError("binding and native thread identifiers must be non-empty")
        key = (binding.binding_id, binding.generation)
        existing = self._bindings.get(key)
        if existing is not None and existing != binding:
            raise ValueError("exact binding already maps to a different native thread")
        if any(value.thread_id == binding.thread_id and value != binding for value in self._bindings.values()):
            raise ValueError("native thread is already managed by another binding")


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
