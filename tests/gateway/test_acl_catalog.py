"""S1 core: capability catalog classification and all_runtime resolution.

The catalog is supplied by gateway wiring (tool_name -> class). The
module must fail closed: no catalog means no dynamic capability set,
and unclassified entries never enter all_runtime.
"""
from __future__ import annotations

from gateway.acl_catalog import (
    CAPABILITY_CLASSES,
    RESERVED_ACCESS_NAMES,
    catalog_digest,
    classify,
    resolve_all_runtime,
)

CATALOG = {
    "web_search": "runtime_safe",
    "todo": "runtime_safe",
    "terminal": "operator",
    "code_execution": "operator",
    "config_edit": "control_plane",
    "mystery_tool": "unclassified",
    "typo_class_tool": "banana",
}


def test_classes_and_reserved_names():
    assert CAPABILITY_CLASSES == {"runtime_safe", "operator", "control_plane", "unclassified"}
    assert RESERVED_ACCESS_NAMES == {"all", "all_runtime"}


def test_classify_defaults_to_unclassified():
    assert classify(CATALOG, "web_search") == "runtime_safe"
    assert classify(CATALOG, "terminal") == "operator"
    assert classify(CATALOG, "missing_tool") == "unclassified"
    assert classify(CATALOG, "typo_class_tool") == "unclassified"
    assert classify(None, "web_search") == "unclassified"


def test_all_runtime_is_runtime_safe_only():
    resolved = resolve_all_runtime(CATALOG)
    assert resolved == frozenset({"web_search", "todo"})
    assert "terminal" not in resolved
    assert "config_edit" not in resolved
    assert "mystery_tool" not in resolved


def test_all_runtime_fails_closed_without_catalog():
    assert resolve_all_runtime(None) == frozenset()
    assert resolve_all_runtime({}) == frozenset()


def test_catalog_digest_stable_and_order_independent():
    a = catalog_digest({"x": "runtime_safe", "y": "operator"})
    b = catalog_digest({"y": "operator", "x": "runtime_safe"})
    c = catalog_digest({"x": "runtime_safe", "y": "control_plane"})
    assert a == b
    assert a != c
    assert len(a) == 64
