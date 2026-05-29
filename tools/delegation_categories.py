"""Pure resolver for named delegation categories.

A *delegation category* bundles a provider/model, a runtime budget, and a
toolset scope under a short intent name ("quick", "deep", …).  Callers
delegate by intent instead of wiring providers ad hoc.  This module is
deliberately pure — no I/O, no provider clients, no child spawning — so the
resolution (and its validation errors) can be exercised cheaply and *before*
any child agent is launched.

Resolution rules (see ``resolve_delegation_category``):

* ``category`` of ``None``/``""`` falls back to
  ``delegation_config["default_category"]``.  If that is also empty the
  category layer is inactive and an inert (legacy) result is returned.
* An unknown category raises :class:`DelegationCategoryConfigError` with
  ``code="unknown_category"`` *before* any spawn, exposing the sorted list of
  valid category names.
* A category with ``enabled: false`` raises with ``code="disabled_category"``.
* Toolsets can only ever *narrow*: start from the category's toolsets, then
  intersect with the parent's toolsets, then with the caller's requested
  toolsets.  Category order is preserved throughout.
* ``toolsets_mode`` defaults to ``"intersect"``.  Any other mode is rejected
  (``code="invalid_toolsets_mode"``) rather than silently allowing a category
  to *escalate* the toolset scope.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class DelegationCategoryConfigError(ValueError):
    """Raised when a delegation category cannot be resolved.

    Carries machine-readable context so callers can surface a precise message
    without parsing the string form:

    * ``code`` — stable error code (e.g. ``"unknown_category"``).
    * ``category`` — the offending category name (may be ``None``).
    * ``valid_categories`` — sorted list of configured category names.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        category: Optional[str] = None,
        valid_categories: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.valid_categories = list(valid_categories or [])

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        base = super().__str__()
        if self.valid_categories:
            return f"{base} (valid categories: {', '.join(self.valid_categories)})"
        return base


def _intersect_preserving_order(base: List[str], allowed: List[str]) -> List[str]:
    """Return items of *base* (in order) that also appear in *allowed*."""
    allowed_set = set(allowed)
    return [item for item in base if item in allowed_set]


def _fallback_metadata(fallback_chain: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build non-secret metadata describing the fallback chain.

    Exposes only the provider/model identifiers — never API keys or base URLs —
    so it is safe to log or echo back to a caller.
    """
    providers = [str(entry.get("provider", "")) for entry in fallback_chain]
    models = [str(entry.get("model", "")) for entry in fallback_chain]
    return {
        "enabled": len(fallback_chain) > 0,
        "count": len(fallback_chain),
        "providers": providers,
        "models": models,
    }


def _inactive_result(parent_toolsets, requested_toolsets) -> Dict[str, Any]:
    """Result when the category layer is inactive (no category selected).

    Preserves the legacy single-provider delegation behaviour: nothing is
    forced, toolset scope is whatever narrowing the caller already requested.
    """
    toolsets: Optional[List[str]] = None
    if parent_toolsets is not None:
        toolsets = list(parent_toolsets)
    if requested_toolsets is not None:
        base = toolsets if toolsets is not None else list(requested_toolsets)
        toolsets = _intersect_preserving_order(base, list(requested_toolsets))
    return {
        "category": "",
        "provider": "",
        "model": "",
        "reasoning_effort": "",
        "toolsets": toolsets,
        "max_iterations": None,
        "child_timeout_seconds": None,
        "fallback_chain": [],
        "fallback_metadata": _fallback_metadata([]),
    }


def resolve_delegation_category(
    delegation_config: Dict[str, Any],
    category: Optional[str] = None,
    parent_toolsets: Optional[List[str]] = None,
    requested_toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Resolve a delegation category into a concrete runtime spec.

    Args:
        delegation_config: the ``config["delegation"]`` mapping.
        category: explicit category name, or ``None`` to use the configured
            ``default_category``.
        parent_toolsets: toolsets the parent agent currently has — an upper
            bound the resolved scope cannot exceed.
        requested_toolsets: toolsets the caller asked the child to use — a
            further narrowing on top of the category + parent scope.

    Returns:
        A dict with keys ``category``, ``provider``, ``model``,
        ``reasoning_effort``, ``toolsets``, ``max_iterations``,
        ``child_timeout_seconds``, ``fallback_chain`` and ``fallback_metadata``.

    Raises:
        DelegationCategoryConfigError: on unknown category, disabled category,
            or a malformed ``toolsets_mode``.
    """
    delegation_config = delegation_config or {}
    categories = delegation_config.get("categories") or {}

    name = category
    if not name:
        name = delegation_config.get("default_category") or ""

    # No category selected and no default — the layer is inactive.
    if not name:
        return _inactive_result(parent_toolsets, requested_toolsets)

    valid_categories = sorted(categories.keys())

    if name not in categories:
        raise DelegationCategoryConfigError(
            f"Unknown delegation category {name!r}",
            code="unknown_category",
            category=name,
            valid_categories=valid_categories,
        )

    spec = categories[name] or {}

    # ``enabled`` defaults to True; only an explicit False disables.
    if spec.get("enabled", True) is False:
        raise DelegationCategoryConfigError(
            f"Delegation category {name!r} is disabled",
            code="disabled_category",
            category=name,
            valid_categories=valid_categories,
        )

    toolsets_mode = spec.get("toolsets_mode", "intersect")
    if toolsets_mode != "intersect":
        raise DelegationCategoryConfigError(
            f"Delegation category {name!r} has unsupported toolsets_mode "
            f"{toolsets_mode!r}; only 'intersect' is allowed (categories may "
            f"only narrow toolset scope, never escalate it)",
            code="invalid_toolsets_mode",
            category=name,
            valid_categories=valid_categories,
        )

    # Toolset scope can only narrow: category -> parent -> requested.
    category_toolsets = spec.get("toolsets")
    if category_toolsets is not None:
        toolsets: Optional[List[str]] = list(category_toolsets)
    elif parent_toolsets is not None:
        toolsets = list(parent_toolsets)
    else:
        toolsets = None

    if toolsets is not None and parent_toolsets is not None:
        toolsets = _intersect_preserving_order(toolsets, list(parent_toolsets))
    if toolsets is not None and requested_toolsets is not None:
        toolsets = _intersect_preserving_order(toolsets, list(requested_toolsets))

    fallback_chain = list(spec.get("fallback_chain") or [])

    return {
        "category": name,
        "provider": spec.get("provider", ""),
        "model": spec.get("model", ""),
        "reasoning_effort": spec.get("reasoning_effort", ""),
        "toolsets": toolsets,
        "max_iterations": spec.get("max_iterations"),
        "child_timeout_seconds": spec.get("child_timeout_seconds"),
        "fallback_chain": fallback_chain,
        "fallback_metadata": _fallback_metadata(fallback_chain),
    }
