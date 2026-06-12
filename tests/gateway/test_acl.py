from __future__ import annotations

import os

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.acl import (
    ACLCommandContext,
    ACLRequest,
    ACLStore,
    BootstrapSuperAdmins,
    collect_bootstrap_super_admins,
    parse_acl_command,
    resolve_acl,
)


def test_store_initializes_builtin_groups_and_persists_custom_state(tmp_path):
    db_path = tmp_path / "gateway_acl.sqlite3"

    store = ACLStore(db_path)

    groups = {group.name: group for group in store.list_groups()}
    assert set(groups) >= {"default", "admin"}
    assert groups["default"].builtin is True
    assert groups["admin"].builtin is True

    store.create_group("developer")
    store.grant_group_access("developer", "web")
    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="developer",
        scope="channel",
        scope_id="c1",
        actor_platform="discord",
        actor_user_id="owner",
    )

    reopened = ACLStore(db_path)
    assert "developer" in {group.name for group in reopened.list_groups()}
    assert "web" in reopened.list_group_grants("developer")
    memberships = reopened.list_memberships(
        platform="discord",
        subject_type="user",
        subject_id="u1",
    )
    assert [(m.group_name, m.scope, m.scope_id) for m in memberships] == [
        ("developer", "channel", "c1")
    ]

    audit = reopened.audit(limit=10)
    assert any(row.action == "membership.grant" for row in audit)
    assert any(row.actor_user_id == "owner" for row in audit)


def test_membership_grants_are_idempotent_for_dm_scope(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")

    for _ in range(2):
        store.grant_membership(
            platform="discord",
            subject_type="user",
            subject_id="u1",
            group_name="default",
            scope="dm",
        )

    memberships = store.list_memberships(
        platform="discord",
        subject_type="user",
        subject_id="u1",
    )
    assert [(m.group_name, m.scope, m.scope_id) for m in memberships] == [
        ("default", "dm", None)
    ]


def test_channel_membership_requires_explicit_scope_id(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")

    with pytest.raises(ValueError, match="scope_id"):
        store.grant_membership(
            platform="discord",
            subject_type="user",
            subject_id="u1",
            group_name="default",
            scope="channel",
        )

    with pytest.raises(ValueError, match="explicit scope_id"):
        store.grant_membership(
            platform="discord",
            subject_type="user",
            subject_id="u1",
            group_name="default",
            scope="channel",
            scope_id="*",
        )


def test_resolution_keeps_dm_and_channel_memberships_independent_and_uses_roles(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.create_group("developer")
    store.grant_group_access("developer", "web")
    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="dm",
    )
    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="developer",
        scope="channel",
        scope_id="c1",
    )
    store.grant_membership(
        platform="discord",
        subject_type="role",
        subject_id="r-dev",
        group_name="developer",
        scope="channel",
        scope_id="c2",
    )

    dm = resolve_acl(
        store,
        ACLRequest(platform="discord", user_id="u1", role_ids=[], scope="dm"),
        bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert dm.can_chat is True
    assert dm.groups == {"default"}
    assert {"clarify", "todo"}.issubset(dm.allowed_tool_names)
    assert "web_search" not in dm.allowed_tool_names

    channel = resolve_acl(
        store,
        ACLRequest(
            platform="discord",
            user_id="u1",
            role_ids=[],
            scope="channel",
            channel_id="c1",
        ),
        bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert channel.can_chat is True
    assert channel.groups == {"developer"}
    assert {"web_search", "web_extract"}.issubset(channel.allowed_tool_names)
    assert "terminal" not in channel.allowed_tool_names

    other_channel = resolve_acl(
        store,
        ACLRequest(
            platform="discord",
            user_id="u1",
            role_ids=[],
            scope="channel",
            channel_id="c9",
        ),
        bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert other_channel.can_chat is False
    assert other_channel.denied_reason == "no_acl_membership"

    via_role = resolve_acl(
        store,
        ACLRequest(
            platform="discord",
            user_id="u2",
            role_ids=["r-dev"],
            scope="channel",
            channel_id="c2",
        ),
        bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert via_role.can_chat is True
    assert via_role.groups == {"developer"}
    assert "web_search" in via_role.allowed_tool_names


def test_unknown_user_denied_except_harmless_discovery_commands(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")

    policy = resolve_acl(
        store,
        ACLRequest(
            platform="discord",
            user_id="stranger",
            role_ids=[],
            scope="channel",
            channel_id="c1",
        ),
        bootstrap=BootstrapSuperAdmins.empty(),
    )

    assert policy.can_chat is False
    assert policy.groups == set()
    assert policy.allowed_tool_names == set()
    assert policy.allowed_slash_commands == {"help", "whoami"}
    assert "acl" not in policy.allowed_slash_commands
    assert policy.denied_reason == "no_acl_membership"


def test_bootstrap_super_admins_include_legacy_allowlists_per_spec(monkeypatch, tmp_path):
    """Per docs/superpowers/specs/2026-05-13-gateway-acl-design.md, users in
    DISCORD_ALLOWED_USERS or platform allow_from ARE bootstrap super-admins —
    otherwise every pre-ACL deployment is locked out of chat and of /acl
    (audit agent-gateway-acl-004). Roles still never confer admin."""
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "env-owner, env-admin")
    monkeypatch.setenv("DISCORD_ACL_SUPER_ADMINS", "env-acl-admin")
    monkeypatch.setenv("DISCORD_ALLOWED_ROLES", "role-should-not-count")
    discord_cfg = PlatformConfig(
        enabled=True,
        extra={
            "allowed_users": "cfg-owner,cfg-admin",
            "allow_from": "chat-user",
            "allowed_roles": "role1,role2",
            "allow_admin_from": ["slash-admin"],
            "group_allow_admin_from": {"guild1": ["group-admin"]},
        },
    )

    bootstrap = collect_bootstrap_super_admins(
        {Platform.DISCORD: discord_cfg},
        env=os.environ,
    )

    assert bootstrap.is_super_admin("discord", "env-acl-admin")
    assert bootstrap.is_super_admin("discord", "slash-admin")
    assert bootstrap.is_super_admin("discord", "group-admin")
    assert bootstrap.is_super_admin("discord", "env-owner")
    assert bootstrap.is_super_admin("discord", "cfg-owner")
    assert bootstrap.is_super_admin("discord", "chat-user")
    assert not bootstrap.is_super_admin("discord", "role1")
    assert not bootstrap.is_super_admin("discord", "role-should-not-count")

    policy = resolve_acl(
        ACLStore(tmp_path / "acl.sqlite3"),
        ACLRequest(
            platform="discord",
            user_id="env-acl-admin",
            role_ids=["role1"],
            scope="channel",
            channel_id="c1",
        ),
        bootstrap=bootstrap,
    )
    assert policy.can_chat is True
    assert policy.bootstrap_super_admin is True
    assert "admin" in policy.groups
    assert "terminal" in policy.allowed_tool_names
    assert "acl" in policy.allowed_slash_commands

    chat_allowlisted = resolve_acl(
        ACLStore(tmp_path / "acl-chat.sqlite3"),
        ACLRequest(
            platform="discord",
            user_id="cfg-owner",
            role_ids=[],
            scope="channel",
            channel_id="c1",
        ),
        bootstrap=bootstrap,
    )
    # Legacy allowed_users entries are bootstrap super-admins per the spec.
    assert chat_allowlisted.can_chat is True
    assert chat_allowlisted.bootstrap_super_admin is True
    assert "acl" in chat_allowlisted.allowed_slash_commands

    role_only = resolve_acl(
        ACLStore(tmp_path / "acl2.sqlite3"),
        ACLRequest(
            platform="discord",
            user_id="ordinary",
            role_ids=["role1"],
            scope="channel",
            channel_id="c1",
        ),
        bootstrap=bootstrap,
    )
    assert role_only.can_chat is False
    assert role_only.bootstrap_super_admin is False
    assert "acl" not in role_only.allowed_slash_commands


def test_channel_resolution_does_not_honor_empty_scope_id_as_wildcard(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO memberships(platform, subject_type, subject_id, group_name, scope, scope_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("discord", "user", "u1", "default", "channel", "", 1.0),
        )

    policy = resolve_acl(
        store,
        ACLRequest(platform="discord", user_id="u1", role_ids=[], scope="channel", channel_id="c1"),
        bootstrap=BootstrapSuperAdmins.empty(),
    )

    assert policy.can_chat is False
    assert policy.groups == set()


def test_channel_scope_resolution_uses_parent_channel_not_thread_id(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="channel",
        scope_id="parent-channel",
    )

    policy = resolve_acl(
        store,
        ACLRequest(
            platform="discord",
            user_id="u1",
            role_ids=[],
            scope="channel",
            channel_id="parent-channel",
            thread_id="thread-123",
        ),
        bootstrap=BootstrapSuperAdmins.empty(),
    )

    assert policy.can_chat is True
    assert policy.scope_id == "parent-channel"


def test_group_grants_resolve_toolsets_and_exact_tool_names(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.create_group("researcher")
    store.grant_group_access("researcher", "web")
    store.grant_group_access("researcher", "tool:clarify")
    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="researcher",
        scope="channel",
        scope_id="c1",
    )

    policy = resolve_acl(
        store,
        ACLRequest(
            platform="discord",
            user_id="u1",
            role_ids=[],
            scope="channel",
            channel_id="c1",
        ),
        bootstrap=BootstrapSuperAdmins.empty(),
    )

    assert policy.can_chat is True
    assert {"web_search", "web_extract", "clarify"}.issubset(policy.allowed_tool_names)
    assert "memory" not in policy.allowed_tool_names
    assert "terminal" not in policy.allowed_tool_names


def test_slash_grants_normalize_underscores_like_runtime_commands(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.create_group("operators")
    store.grant_group_access("operators", "cmd:set_home")
    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="operators",
        scope="channel",
        scope_id="c1",
    )

    policy = resolve_acl(
        store,
        ACLRequest(platform="discord", user_id="u1", scope="channel", channel_id="c1"),
        bootstrap=BootstrapSuperAdmins.empty(),
    )

    assert "set-home" in policy.allowed_slash_commands
    assert "set_home" not in policy.allowed_slash_commands


def test_acl_parser_handles_supported_v1_commands():
    ctx = ACLCommandContext(platform="discord", channel_id="c1", scope="channel")

    show = parse_acl_command("/acl show", ctx)
    assert show.action == "show"
    assert show.requires_confirmation is False

    show_user = parse_acl_command("/acl show <@123>", ctx)
    assert show_user.action == "show"
    assert show_user.subject_type == "user"
    assert show_user.subject_id == "123"

    grant_dm = parse_acl_command("/acl grant <@123> default in dm", ctx)
    assert grant_dm.action == "grant_membership"
    assert grant_dm.subject_type == "user"
    assert grant_dm.subject_id == "123"
    assert grant_dm.group_name == "default"
    assert grant_dm.scope == "dm"
    assert grant_dm.scope_id is None
    assert grant_dm.requires_confirmation is True

    grant_channel = parse_acl_command("/acl grant <@123> default in this channel", ctx)
    assert grant_channel.action == "grant_membership"
    assert grant_channel.scope == "channel"
    assert grant_channel.scope_id == "c1"
    assert grant_channel.requires_confirmation is True

    revoke_channel = parse_acl_command("/acl revoke <@123> default in this channel", ctx)
    assert revoke_channel.action == "revoke_membership"
    assert revoke_channel.scope == "channel"
    assert revoke_channel.scope_id == "c1"
    assert revoke_channel.requires_confirmation is True

    create = parse_acl_command("/acl group create developer", ctx)
    assert create.action == "create_group"
    assert create.group_name == "developer"
    assert create.requires_confirmation is True

    group_grant = parse_acl_command("/acl group grant developer web", ctx)
    assert group_grant.action == "grant_group_access"
    assert group_grant.group_name == "developer"
    assert group_grant.access_name == "web"
    assert group_grant.requires_confirmation is True

    audit = parse_acl_command("/acl audit", ctx)
    assert audit.action == "audit"
    assert audit.requires_confirmation is False


@pytest.mark.parametrize(
    "text",
    [
        "/acl grant <@123> default in this channel",
        "/acl revoke <@123> default in this channel",
    ],
)
def test_acl_parser_requires_channel_context_for_this_channel(text):
    ctx = ACLCommandContext(platform="discord", channel_id=None, scope="dm")

    with pytest.raises(ValueError, match="channel"):
        parse_acl_command(text, ctx)
