"""Pipeline data types. This module imports nothing from the package.

Only the type that the published modules need is kept here.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Contact:
    kind: str        # "email" | "telegram" | "linkedin" | "phone"
    value: str       # as it appeared in the text
    value_norm: str  # normalized form, used as the dedup key
