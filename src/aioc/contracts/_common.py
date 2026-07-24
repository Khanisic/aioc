"""Shared building blocks for the schema layer.

`StrictModel` forbids unknown fields so a typo or a drifted payload fails loudly rather
than being silently dropped - the contract (docs/CONTRACTS.md) is meant to be enforced,
not best-effort parsed.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every contract type. Unknown fields are a validation error."""

    model_config = ConfigDict(extra="forbid")


def is_other(value: Any) -> bool:
    """True when `value` is the enum member (or raw string) ``other``."""
    if isinstance(value, Enum):
        return bool(value.value == "other")
    return bool(value == "other")


def check_other_detail(
    value: Any,
    detail: str | None,
    *,
    field: str,
    detail_field: str,
) -> None:
    """Enforce the ``other`` enum pattern from CONTRACTS.md sec 1.

    ``detail`` must be non-null exactly when the enum value is ``other``, and null
    otherwise. Both directions are validated.
    """
    if is_other(value):
        if detail is None:
            raise ValueError(f"{detail_field} must be non-null when {field} == 'other'")
    elif detail is not None:
        raise ValueError(f"{detail_field} must be null when {field} != 'other'")
