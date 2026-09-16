"""Bounded installed-capability registry contracts."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, JsonValue, field_validator, model_validator

from mobile_sensing.contracts.base import CapabilityKey, ContractModel, PositiveInt


class CapabilityUnavailableError(LookupError):
    """Raised when a requested capability is absent or explicitly unavailable."""


class CapabilityDescriptor(ContractModel):
    key: CapabilityKey
    available: bool
    implementation_version: str | None
    parameter_schema: dict[str, JsonValue]
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available and self.implementation_version is None:
            raise ValueError("available capability requires an implementation version")
        if self.available and self.unavailable_reason is not None:
            raise ValueError("available capability cannot have an unavailable reason")
        if not self.available and self.unavailable_reason in (None, ""):
            raise ValueError("unavailable capability requires a reason")
        return self


class CapabilityRegistry(ContractModel):
    registry_version: str
    max_entries: PositiveInt = 256
    capabilities: tuple[CapabilityDescriptor, ...]

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(
        cls, value: tuple[CapabilityDescriptor, ...]
    ) -> tuple[CapabilityDescriptor, ...]:
        keys = [capability.key for capability in value]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("capabilities must be sorted by unique key")
        return value

    @model_validator(mode="after")
    def validate_bound(self) -> Self:
        if len(self.capabilities) > self.max_entries:
            raise ValueError("capability registry exceeds its declared bound")
        return self

    def get(self, key: str) -> CapabilityDescriptor | None:
        return next((item for item in self.capabilities if item.key == key), None)

    def require(self, key: str) -> CapabilityDescriptor:
        capability = self.get(key)
        if capability is None:
            raise CapabilityUnavailableError(f"capability is not installed: {key}")
        if not capability.available:
            raise CapabilityUnavailableError(
                f"capability is unavailable: {key}: {capability.unavailable_reason}"
            )
        return capability


def descriptor_from_model(
    *,
    key: CapabilityKey,
    model: type[BaseModel],
    implementation_version: str,
) -> CapabilityDescriptor:
    """Derive capability parameters and advertised schema from one model."""

    return CapabilityDescriptor(
        key=key,
        available=True,
        implementation_version=implementation_version,
        parameter_schema=model.model_json_schema(mode="validation"),
    )
