"""Adapter-local existing Antigravity conversation bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


class BindingUnavailable(RuntimeError):
    pass


class BindingGenerationMismatch(BindingUnavailable):
    pass


@dataclass(frozen=True)
class ResolvedBinding:
    binding_id: str
    generation: int
    conversation_id: str


class BindingResolver(Protocol):
    async def resolve(self, binding_id: str, generation: int) -> ResolvedBinding: ...


class InMemoryBindingResolver:
    def __init__(self, bindings: Iterable[ResolvedBinding]) -> None:
        self._bindings: dict[tuple[str, int], ResolvedBinding] = {}
        for binding in bindings:
            if not binding.binding_id.strip() or not binding.conversation_id.strip():
                raise ValueError("binding and native conversation identifiers must be non-empty")
            if isinstance(binding.generation, bool) or not isinstance(binding.generation, int):
                raise ValueError("binding generation must be an integer")
            key = (binding.binding_id, binding.generation)
            if key in self._bindings:
                raise ValueError("duplicate exact native binding mapping")
            if any(
                value.conversation_id == binding.conversation_id
                for value in self._bindings.values()
            ):
                raise ValueError("native conversation is already managed by another binding")
            self._bindings[key] = binding

    async def resolve(self, binding_id: str, generation: int) -> ResolvedBinding:
        value = self._bindings.get((binding_id, generation))
        if value is not None:
            return value
        if any(key[0] == binding_id for key in self._bindings):
            raise BindingGenerationMismatch("binding generation mismatch")
        raise BindingUnavailable("binding unavailable")

    def publish(self, binding: ResolvedBinding) -> None:
        self.validate_publish(binding)
        self._bindings[(binding.binding_id, binding.generation)] = binding

    def validate_publish(self, binding: ResolvedBinding) -> None:
        if not binding.binding_id.strip() or not binding.conversation_id.strip():
            raise ValueError("binding and native conversation identifiers must be non-empty")
        key = (binding.binding_id, binding.generation)
        existing = self._bindings.get(key)
        if existing is not None and existing != binding:
            raise ValueError("exact binding already maps to a different native conversation")
        if any(value.conversation_id == binding.conversation_id and value != binding for value in self._bindings.values()):
            raise ValueError("native conversation is already managed by another binding")


async def resolve_existing(
    resolver: BindingResolver, binding_id: str, generation: int
) -> ResolvedBinding:
    value = await resolver.resolve(binding_id, generation)
    if not isinstance(value, ResolvedBinding) or value.binding_id != binding_id:
        raise BindingUnavailable("binding resolver returned a different binding")
    if value.generation != generation:
        raise BindingGenerationMismatch("binding generation mismatch")
    if not value.conversation_id:
        raise BindingUnavailable("binding has no conversation")
    return value
