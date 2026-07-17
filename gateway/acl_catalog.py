"""Capability catalog classification for the dynamic access matrix.

Gateway wiring supplies the live catalog (tool_name -> capability
class). This pure module decides what the reserved computed access
name all_runtime means: exactly the runtime_safe entries. Everything
else - operator tools, control-plane tools, unclassified or unknown
entries, or a missing catalog - is excluded, so new capabilities never
leak into ordinary admins by default (owner decision 1: baseline).
"""
from __future__ import annotations

import hashlib
import json
from typing import Mapping, Optional

CAPABILITY_CLASSES = frozenset({"runtime_safe", "operator", "control_plane", "unclassified"})
RESERVED_ACCESS_NAMES = frozenset({"all", "all_runtime"})


def classify(catalog: Optional[Mapping[str, str]], tool: str) -> str:
    """Return the capability class for a tool; anything unknown fails closed."""
    if not catalog:
        return "unclassified"
    value = str(catalog.get(str(tool), "") or "").strip().lower()
    return value if value in CAPABILITY_CLASSES else "unclassified"


def resolve_all_runtime(catalog: Optional[Mapping[str, str]]) -> frozenset[str]:
    """The reserved all_runtime set: runtime_safe catalog entries only."""
    if not catalog:
        return frozenset()
    return frozenset(
        str(tool) for tool in catalog if classify(catalog, str(tool)) == "runtime_safe"
    )


def catalog_digest(catalog: Optional[Mapping[str, str]]) -> str:
    """Stable digest of the catalog for definition snapshots and audit."""
    canon = json.dumps(
        {str(k): classify(catalog, str(k)) for k in (catalog or {})},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
