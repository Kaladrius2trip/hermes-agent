"""S1 driver: catalog-classified admin baseline (owner decision 1+2)."""
from __future__ import annotations

from types import SimpleNamespace

from gateway.acl import ACLRequest, ACLStore, BootstrapSuperAdmins, resolve_acl

CATALOG = {
    "web_search": "runtime_safe",
    "todo": "runtime_safe",
    "clarify": "runtime_safe",
    "terminal": "operator",
    "code_execution": "operator",
    "config_edit": "control_plane",
    "skills_install": "control_plane",
    "brand_new_tool": "unclassified",
}


def _store(tmp_path) -> ACLStore:
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="boss",
        group_name="admin", scope="channel", scope_id="c1",
    )
    return store


def _req(user_id="boss") -> ACLRequest:
    return ACLRequest(
        platform="discord", user_id=user_id, scope="channel",
        channel_id="c1", guild_id="g1",
    )


def test_admin_baseline_excludes_operator_and_control_plane(tmp_path):
    policy = resolve_acl(
        _store(tmp_path), _req(), bootstrap=BootstrapSuperAdmins.empty(),
        catalog=CATALOG,
    )
    assert {"web_search", "todo", "clarify"} <= policy.allowed_tool_names
    assert "terminal" not in policy.allowed_tool_names
    assert "code_execution" not in policy.allowed_tool_names
    assert "config_edit" not in policy.allowed_tool_names
    assert "brand_new_tool" not in policy.allowed_tool_names


def test_admin_fails_closed_without_catalog(tmp_path):
    policy = resolve_acl(
        _store(tmp_path), _req(), bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert policy.can_chat is True
    assert "web_search" not in policy.allowed_tool_names
    assert "terminal" not in policy.allowed_tool_names


def test_explicit_control_plane_grant_still_resolves(tmp_path):
    store = _store(tmp_path)
    store.grant_group_access("admin", "config_edit")
    policy = resolve_acl(
        store, _req(), bootstrap=BootstrapSuperAdmins.empty(), catalog=CATALOG,
    )
    assert "config_edit" in policy.allowed_tool_names


def test_reserved_all_never_live_expands(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.create_group("legacyall")
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="legacyall", scope="channel", scope_id="c1",
    )
    store.grant_group_access("legacyall", "all")
    policy = resolve_acl(
        store, _req(user_id="u1"), bootstrap=BootstrapSuperAdmins.empty(),
        catalog=CATALOG,
    )
    assert policy.allowed_tool_names == {"web_search", "todo", "clarify"}
    policy_none = resolve_acl(
        store, _req(user_id="u1"), bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert policy_none.allowed_tool_names == set()
