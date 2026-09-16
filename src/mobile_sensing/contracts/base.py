"""Strict scalar conventions and canonical scientific identity helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


SCHEMA_VERSION: Literal["2.0"] = "2.0"
CANONICAL_JSON_VERSION = "canonical-json@1"
HASH_ALGORITHM = "sha256"
UINT64_MAX = 2**64 - 1


def _validate_opaque_id(value: str) -> str:
    if value == "":
        raise ValueError("ID must be nonempty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("ID must be valid UTF-8") from exc
    return value


def _validate_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("value must be a lowercase SHA-256 hexadecimal digest")
    return value


def _validate_capability_key(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+@[1-9][0-9]*", value) is None:
        raise ValueError("capability key must have the form category.name@positive_version")
    return value


def _validate_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")
    return value.astimezone(timezone.utc)


OpaqueId: TypeAlias = Annotated[str, AfterValidator(_validate_opaque_id)]
Sha256: TypeAlias = Annotated[str, AfterValidator(_validate_sha256)]
CapabilityKey: TypeAlias = Annotated[str, AfterValidator(_validate_capability_key)]
UtcDateTime: TypeAlias = Annotated[datetime, AfterValidator(_validate_utc_datetime)]
FiniteFloat: TypeAlias = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFloat: TypeAlias = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveFloat: TypeAlias = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]
PositiveInt: TypeAlias = Annotated[int, Field(gt=0)]
UInt64: TypeAlias = Annotated[int, Field(ge=0, le=UINT64_MAX)]
SchemaVersion: TypeAlias = Literal["2.0"]


class ContractModel(BaseModel):
    """Base for immutable, strict, forward-incompatible external contracts."""

    scientific_identity_excluded_fields: ClassVar[frozenset[str]] = frozenset()

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        validate_default=True,
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python", round_trip=True))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        return _validate_utc_datetime(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json_bytes(item))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON rejects non-finite numbers")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__qualname__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a contract deterministically using canonical-json@1."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _scientific_value(value: Any) -> Any:
    if isinstance(value, ContractModel):
        excluded = value.scientific_identity_excluded_fields
        return {
            name: _scientific_value(getattr(value, name))
            for name in type(value).model_fields
            if name not in excluded
        }
    if isinstance(value, Mapping):
        return {key: _scientific_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_scientific_value(item) for item in value]
    return value


def scientific_projection(value: Any) -> Any:
    """Return the canonical JSON value used for scientific identity."""

    return _canonical_value(_scientific_value(value))


def scientific_hash(value: Any) -> Sha256:
    """Hash the versioned scientific projection of a contract or JSON value."""

    preimage = (
        CANONICAL_JSON_VERSION.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(scientific_projection(value))
    )
    return hashlib.sha256(preimage).hexdigest()


def stable_id(namespace: str, identity: Any) -> OpaqueId:
    """Create a stable opaque ID without using row position or object hashes."""

    if re.fullmatch(r"[a-z][a-z0-9_]*", namespace) is None:
        raise ValueError("ID namespace must match [a-z][a-z0-9_]*")
    return f"{namespace}_{scientific_hash({'namespace': namespace, 'identity': identity})[:32]}"
