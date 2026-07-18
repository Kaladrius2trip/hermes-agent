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


def test_gateway_catalog_uses_positive_classification(monkeypatch):
    """Unknown and side-effecting tools must never enter all_runtime by default."""
    import toolsets
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        toolsets,
        "resolve_toolset",
        lambda _name: {
            "web_search",
            "jenkins_trigger_build_with_parameters",
            "memory",
            "brand_new_tool",
        },
    )
    runner = object.__new__(GatewayRunner)
    catalog = runner._acl_capability_catalog()

    assert catalog["web_search"] == "runtime_safe"
    assert catalog["jenkins_trigger_build_with_parameters"] == "operator"
    assert catalog["memory"] == "control_plane"
    assert catalog["brand_new_tool"] == "unclassified"


def test_gateway_catalog_refreshes_on_registry_generation(monkeypatch):
    import toolsets
    from gateway.run import GatewayRunner
    from tools.registry import registry

    names = {"web_search"}
    monkeypatch.setattr(toolsets, "resolve_toolset", lambda _name: set(names))
    runner = object.__new__(GatewayRunner)
    first = runner._acl_capability_catalog()
    names.add("brand_new_tool")
    monkeypatch.setattr(registry, "_generation", registry._generation + 1)
    second = runner._acl_capability_catalog()
    assert "brand_new_tool" not in first
    assert second["brand_new_tool"] == "unclassified"


def test_gateway_catalog_excludes_dangerous_tools():
    """Regression: durable/side-effecting tools never classify runtime_safe."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    catalog = runner._acl_capability_catalog()
    for name in (
        "terminal", "execute_code", "computer_use", "delegate_task",
        "cronjob", "memory", "discord_admin", "kanban_create", "identify",
        "write_file", "patch", "skill_manage",
    ):
        assert catalog.get(name) != "runtime_safe", name
