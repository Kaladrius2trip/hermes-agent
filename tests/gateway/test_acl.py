from __future__ import annotations

import os
import sqlite3

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from gateway.acl import (
    ACLCommandContext,
    ACLRequest,
    ACLStore,
    BootstrapSuperAdmins,
    apply_acl_command,
    collect_bootstrap_super_admins,
    parse_acl_command,
    resolve_acl,
)


def test_store_migrates_legacy_tables_to_strict_and_preserves_rows(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE groups (name TEXT PRIMARY KEY, builtin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE TABLE group_grants (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, access_name TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(group_name, access_name), FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE);
            CREATE TABLE memberships (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, group_name TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, UNIQUE(platform, subject_type, subject_id, group_name, scope, scope_id), FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE);
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '', actor_platform TEXT NOT NULL DEFAULT '', actor_user_id TEXT NOT NULL DEFAULT '', subject_type TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '', scope_id TEXT, access_name TEXT NOT NULL DEFAULT '', allowed INTEGER, reason TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '');
            INSERT INTO groups(name, builtin, created_at) VALUES ('legacy', 0, 1.0);
            INSERT INTO group_grants(group_name, access_name, created_at) VALUES ('legacy', 'web', 1.0);
            INSERT INTO memberships(platform, subject_type, subject_id, group_name, scope, scope_id, created_at) VALUES ('discord', 'user', 'u1', 'legacy', 'channel', 'c1', 1.0);
            """
        )

    store = ACLStore(db_path)

    assert "legacy" in {group.name for group in store.list_groups()}
    assert store.list_group_grants("legacy") == ["web"]
    assert store.list_memberships(subject_id="u1")[0].group_name == "legacy"
    with sqlite3.connect(db_path) as conn:
        strict_by_table = {row[1]: row[5] for row in conn.execute("PRAGMA table_list")}
        assert all(strict_by_table[name] == 1 for name in ("groups", "group_grants", "memberships", "audit_log"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memberships(platform, subject_type, subject_id, group_name, scope, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("discord", "invalid", "u2", "legacy", "dm", 1.0),
            )
        conn.rollback()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM groups WHERE name='legacy'")
        assert conn.execute("SELECT COUNT(*) FROM group_grants WHERE group_name='legacy'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memberships WHERE group_name='legacy'").fetchone()[0] == 0


def test_store_rolls_back_failed_strict_migration(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE groups (name TEXT PRIMARY KEY, builtin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO groups VALUES ('legacy', 0, 1.0)")

    class FailingMigrationStore(ACLStore):
        def _rebuild_table_strict(self, conn, table, ddl):
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy_migration")
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        FailingMigrationStore(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT name FROM groups").fetchone()[0] == "legacy"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='groups_legacy_migration'"
        ).fetchone()[0] == 0


def test_store_seeds_team_presets_once_and_resolves_concrete_tools(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    groups = {group.name for group in store.list_groups()}
    assert {"informer", "researcher", "developer", "operator"} <= groups

    store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="researcher-user",
        group_name="researcher",
        scope="channel",
        scope_id="c1",
    )
    policy = resolve_acl(
        store,
        ACLRequest(platform="discord", user_id="researcher-user", scope="channel", channel_id="c1", guild_id="g1"),
    )
    assert {"read_file", "search_files"} <= policy.allowed_tool_names
    assert {"write_file", "patch"}.isdisjoint(policy.allowed_tool_names)

    store.revoke_group_access("researcher", "web")
    ACLStore(store.db_path)
    assert "web" not in store.list_group_grants("researcher")


def test_acl_parser_and_apply_support_group_revoke_show_groups_and_filtered_audit(tmp_path):
    ctx = ACLCommandContext(platform="discord", channel_id="c1", scope="channel")
    store = ACLStore(tmp_path / "acl.sqlite3")

    assert parse_acl_command("/acl groups", ctx).action == "list_groups"
    show = parse_acl_command("/acl show group researcher", ctx)
    assert (show.action, show.group_name) == ("show_group", "researcher")
    audit = parse_acl_command("/acl audit @u1", ctx)
    assert (audit.action, audit.subject_id) == ("audit", "u1")

    revoke = parse_acl_command("/acl group revoke researcher web", ctx)
    assert revoke.action == "revoke_group_access"
    assert revoke.requires_confirmation is True
    result = apply_acl_command(store, revoke, actor_platform="discord", actor_user_id="owner")
    assert "revoked" in result
    assert "web" not in store.list_group_grants("researcher")
    rows = store.audit(subject_id="owner")
    assert any(row.action == "group.revoke_access" for row in rows)


def test_audit_subject_filter_is_platform_and_type_scoped(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    for platform, subject_type in (("discord", "user"), ("telegram", "user"), ("discord", "role")):
        store.audit_event(
            "membership.probe",
            platform=platform,
            subject_type=subject_type,
            subject_id="same-id",
        )

    rows = store.audit(platform="discord", subject_type="role", subject_id="same-id")
    assert len(rows) == 1
    assert (rows[0].platform, rows[0].subject_type) == ("discord", "role")


def test_bootstrap_allowlist_split_keeps_explicit_admin_sources():
    configs = {
        Platform.DISCORD: PlatformConfig(
            enabled=True,
            extra={"allowed_users": ["member"], "acl_super_admins": ["admin"]},
        ),
        Platform.TELEGRAM: PlatformConfig(enabled=True, extra={"allow_from": ["tm-member"]}),
    }
    env = {
        "DISCORD_ALLOWED_USERS": "legacy-owner",
        "TELEGRAM_ALLOWED_USERS": "tm-legacy",
        "TELEGRAM_ACL_SUPER_ADMINS": "tm-admin",
    }
    bootstrap = collect_bootstrap_super_admins(configs, env=env, include_allowlists=False)

    assert bootstrap.is_super_admin("discord", "admin")
    assert bootstrap.is_super_admin("telegram", "tm-admin")
    for platform, user_id in (
        ("discord", "member"),
        ("discord", "legacy-owner"),
        ("telegram", "tm-member"),
        ("telegram", "tm-legacy"),
    ):
        assert not bootstrap.is_super_admin(platform, user_id)


def test_gateway_config_parses_acl_bootstrap_split_from_gateway_section():
    config = GatewayConfig.from_dict(
        {
            "gateway": {
                "acl_enforced_platforms": ["*"],
                "acl_bootstrap_from_allowlist": False,
            }
        }
    )

    assert config.acl_enforced_platforms == ["*"]
    assert config.acl_bootstrap_from_allowlist is False


def test_load_gateway_config_preserves_nested_acl_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "gateway:\n  acl_enforced_platforms: ['*']\n  acl_bootstrap_from_allowlist: false\n",
        encoding="utf-8",
    )

    config = load_gateway_config()

    assert config.acl_enforced_platforms == ["*"]
    assert config.acl_bootstrap_from_allowlist is False


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
         guild_id="g1"),
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
         guild_id="g1"),
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
         guild_id="g1"),
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
         guild_id="g1"),
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
         guild_id="g1"),
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
         guild_id="g1"),
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
         guild_id="g1"),
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
        ACLRequest(platform="discord", user_id="u1", role_ids=[], scope="channel", channel_id="c1", guild_id="g1"),
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
         guild_id="g1"),
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
         guild_id="g1"),
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
        ACLRequest(platform="discord", user_id="u1", scope="channel", channel_id="c1", guild_id="g1"),
        bootstrap=BootstrapSuperAdmins.empty(),
    )

    assert {"set-home", "sethome"} & policy.allowed_slash_commands
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


def test_acl_platform_enforced_defaults_and_exemptions():
    from gateway.acl import ACL_EXEMPT_PLATFORMS, acl_platform_enforced

    assert acl_platform_enforced("discord") is True
    for platform in ("telegram", "slack", "whatsapp", "matrix"):
        assert acl_platform_enforced(platform) is False
        assert acl_platform_enforced(platform, ["*"]) is True
    for platform in ACL_EXEMPT_PLATFORMS:
        assert acl_platform_enforced(platform) is False
        assert acl_platform_enforced(platform, ["*"]) is False
    assert acl_platform_enforced("") is False
    assert acl_platform_enforced(None) is False

    assert acl_platform_enforced("telegram", ["discord"]) is False
    assert acl_platform_enforced("discord", ["discord"]) is True
    assert acl_platform_enforced("telegram", ["*"]) is True
    assert acl_platform_enforced("telegram", ["TELEGRAM "]) is True
    assert acl_platform_enforced(Platform.TELEGRAM, ["telegram"]) is True


def test_bootstrap_super_admins_collected_for_all_platforms():
    configs = {
        Platform.DISCORD: PlatformConfig(enabled=True, extra={"allowed_users": ["d1"]}),
        Platform.TELEGRAM: PlatformConfig(enabled=True, extra={"allow_from": ["t1"]}),
        Platform.SLACK: PlatformConfig(enabled=True, extra={"acl_super_admins": ["s1"]}),
        Platform.WEBHOOK: PlatformConfig(enabled=True, extra={"allowed_users": ["w1"]}),
    }
    env = {
        "TELEGRAM_ALLOWED_USERS": "t2",
        "QQ_ALLOWED_USERS": "q1",
        "SLACK_ACL_SUPER_ADMINS": "s2",
    }
    bootstrap = collect_bootstrap_super_admins(configs, env=env)

    assert bootstrap.is_super_admin("discord", "d1")
    assert bootstrap.is_super_admin("telegram", "t1")
    assert bootstrap.is_super_admin("telegram", "t2")
    assert bootstrap.is_super_admin("slack", "s1")
    assert bootstrap.is_super_admin("slack", "s2")
    assert not bootstrap.is_super_admin("webhook", "w1")
    assert not bootstrap.is_super_admin("qqbot", "q1")


def test_bootstrap_env_allowlist_needs_platform_config():
    configs = {
        Platform.QQBOT: PlatformConfig(enabled=True, extra={}),
    }
    env = {"QQ_ALLOWED_USERS": "q1"}
    bootstrap = collect_bootstrap_super_admins(configs, env=env)

    assert bootstrap.is_super_admin("qqbot", "q1")


def test_dm_scope_requires_explicit_dm_membership(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.grant_membership(
        platform="telegram",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )

    dm_policy = resolve_acl(
        store,
        ACLRequest(platform="telegram", user_id="u1", scope="dm"),
    )
    assert dm_policy.can_chat is False
    assert dm_policy.denied_reason == "no_acl_membership"

    channel_policy = resolve_acl(
        store,
        ACLRequest(platform="telegram", user_id="u1", scope="channel", channel_id="c1"),
    )
    assert channel_policy.can_chat is True

    store.grant_membership(
        platform="telegram",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="dm",
    )
    dm_after_grant = resolve_acl(
        store,
        ACLRequest(platform="telegram", user_id="u1", scope="dm"),
    )
    assert dm_after_grant.can_chat is True


def test_admin_group_includes_session_slash_commands():
    from gateway.acl import ADMIN_EXTRA_SLASH_COMMANDS

    assert {"background", "queue", "stop", "resume", "clear", "model"} <= ADMIN_EXTRA_SLASH_COMMANDS
    assert "steer" not in ADMIN_EXTRA_SLASH_COMMANDS
    assert "acl" not in ADMIN_EXTRA_SLASH_COMMANDS
