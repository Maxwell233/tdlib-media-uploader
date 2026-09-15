"""Small immutable configuration view shared by future package services.

The legacy loader still exposes its validated module-level values.  This
model provides a dependency-light package type for new code without making
the Phase 2 migration change configuration semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ConfigModel:
    """Read-only view over the parsed TOML document."""

    values: Mapping[str, Any]

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.values.get(name, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"配置段必须是表格：{name}")
        return value


__all__ = ["ConfigModel"]
