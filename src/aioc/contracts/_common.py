"""Shared building blocks for the schema layer.

`StrictModel` forbids unknown fields so a typo or a drifted payload fails loudly rather
than being silently dropped - the contract (docs/CONTRACTS.md) is meant to be enforced,
not best-effort parsed.
"""

import re
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


_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def check_schema_version(value: str, supported: str) -> str:
    """Enforce the consumer half of CONTRACTS.md sec 0: read ``schema_version`` off the
    payload and fail loudly on a major mismatch rather than best-effort parsing.

    A payload from an older or newer *minor* of the same major is accepted - minor bumps
    are additive by the sec 0 rules, and anything a newer minor adds that this code does
    not know (a new enum member, a new required field) already fails loudly on its own
    through the models. A different major, or a version that is not ``MAJOR.MINOR.PATCH``
    at all, is refused here, before any field is interpreted.
    """
    parsed = _SEMVER.fullmatch(value)
    if parsed is None:
        raise ValueError(f"schema_version {value!r} is not MAJOR.MINOR.PATCH (CONTRACTS.md sec 0)")
    ours = supported.split(".", 1)[0]
    if parsed.group(1) != ours:
        raise ValueError(
            f"schema_version {value} is major {parsed.group(1)}; this consumer implements "
            f"{supported} (major {ours}) and refuses to best-effort parse a payload across a "
            "major version (CONTRACTS.md sec 0)"
        )
    return value


def references_field(blocks_field: str | None, path: str) -> bool:
    """True when a Gap's ``blocks_field`` names ``path`` or a field inside it.

    Matching is on field boundaries: ``findings.root_cause.value`` references
    ``findings.root_cause``, but ``findings.root_cause_elsewhere`` does not, and
    ``findings.contributing_factors[10]`` does not reference ``[1]``. A bare string
    prefix accepted both, so a gap about one field could stand in for another's null.
    """
    if blocks_field is None:
        return False
    if blocks_field == path:
        return True
    return blocks_field.startswith(path) and blocks_field[len(path)] in ".["
