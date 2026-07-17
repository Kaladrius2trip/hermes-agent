"""P0 scoped memberships: platform-global users + guild-scoped subjects.

Executable contract for the additive scoped_memberships generation
(office security handoff P0). Legacy dm/channel rows keep working in
parallel during the dual-read compatibility window.
"""
from __future__ import annotations

import sqlite3

import pytest

from gateway.acl import ACLRequest, ACLStore


def _store(tmp_path) -> ACLStore:
    return ACLStore(tmp_path / "acl.sqlite3")


def _req(
    *,
    platform: str = "discord",
    user_id: str = "u1",
    roles: tuple[str, ...] = (),
    scope: str = "channel",
    channel_id: str | None = "c1",
    thread_id: str | None = None,
    guild_id: str | None = "g1",
) -> ACLRequest:
    return ACLRequest(
        platform=platform,
        user_id=user_id,
        role_ids=roles,
        scope=scope,
        channel_id=channel_id,
        thread_id=thread_id,
        guild_id=guild_id,
    )


# --- schema -----------------------------------------------------------------

def test_schema_is_additive_on_existing_store(tmp_path):
    path = tmp_path / "acl.sqlite3"
    first = ACLStore(path)
    first.grant_membership(
        platform="discord", subject_type="user", subject_id="legacy",
        group_name="default", scope="channel", scope_id="c9",
    )
    second = ACLStore(path)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {
        "memberships", "scoped_memberships", "scoped_membership_legacy_links",
        "acl_meta", "migration_ledger",
    } <= tables
    rows = con.execute("select subject_id from memberships").fetchall()
    assert rows == [("legacy",)]
    assert second.resolve_memberships(
        _req(user_id="legacy", channel_id="c9")
    ) == {"default"}


def test_store_identity_and_epoch_exist(tmp_path):
    store = _store(tmp_path)
    assert store.store_id
    assert isinstance(store.policy_epoch, int)


# --- grant validation -------------------------------------------------------

def test_grant_global_user(tmp_path):
    store = _store(tmp_path)
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="admin", scope="global",
    )
    assert store.resolve_memberships(_req(scope="dm", channel_id=None, guild_id=None)) == {"admin"}


def test_grant_global_role_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.grant_scoped_membership(
            platform="discord", subject_type="role", subject_id="r1",
            group_name="admin", scope="global",
        )


def test_grant_guild_requires_scope_id(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.grant_scoped_membership(
            platform="discord", subject_type="role", subject_id="r1",
            group_name="informer", scope="guild",
        )
    with pytest.raises(ValueError):
        store.grant_scoped_membership(
            platform="discord", subject_type="role", subject_id="r1",
            group_name="informer", scope="guild", scope_id="*",
        )


def test_grant_unknown_scope_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.grant_scoped_membership(
            platform="discord", subject_type="user", subject_id="u1",
            group_name="admin", scope="channel", scope_id="c1",
        )


# --- resolver union: security matrix ---------------------------------------

def test_global_user_applies_in_dm_channel_and_thread(tmp_path):
    store = _store(tmp_path)
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="developer", scope="global",
    )
    assert store.resolve_memberships(_req(scope="dm", channel_id=None, guild_id=None)) == {"developer"}
    assert store.resolve_memberships(_req()) == {"developer"}
    assert store.resolve_memberships(_req(thread_id="t1")) == {"developer"}


def test_guild_role_applies_only_inside_that_guild(tmp_path):
    store = _store(tmp_path)
    store.grant_scoped_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="guild", scope_id="g1",
    )
    assert store.resolve_memberships(_req(user_id="x", roles=("team",))) == {"informer"}
    assert store.resolve_memberships(_req(user_id="x", roles=("team",), guild_id="g2")) == set()


def test_guild_role_does_not_apply_in_dm(tmp_path):
    store = _store(tmp_path)
    store.grant_scoped_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="guild", scope_id="g1",
    )
    assert store.resolve_memberships(
        _req(user_id="x", roles=("team",), scope="dm", channel_id=None, guild_id="g1")
    ) == set()


def test_guild_user_membership_applies_only_inside_that_guild(tmp_path):
    store = _store(tmp_path)
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="operator", scope="guild", scope_id="g1",
    )
    assert store.resolve_memberships(_req()) == {"operator"}
    assert store.resolve_memberships(_req(guild_id="g2")) == set()
    assert store.resolve_memberships(_req(scope="dm", channel_id=None, guild_id=None)) == set()


def test_discord_channel_without_guild_id_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="default", scope="channel", scope_id="c1",
    )
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="developer", scope="global",
    )
    assert store.resolve_memberships(_req(guild_id=None)) == set()


def test_non_guild_platform_channel_unaffected_by_missing_guild(tmp_path):
    store = _store(tmp_path)
    store.grant_membership(
        platform="telegram", subject_type="user", subject_id="u1",
        group_name="default", scope="channel", scope_id="c1",
    )
    assert store.resolve_memberships(
        _req(platform="telegram", guild_id=None)
    ) == {"default"}


def test_legacy_channel_rows_still_union_with_scoped(tmp_path):
    store = _store(tmp_path)
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="default", scope="channel", scope_id="c1",
    )
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="developer", scope="global",
    )
    assert store.resolve_memberships(_req()) == {"default", "developer"}


# --- epoch + dual-generation revoke -----------------------------------------

def test_epoch_bumps_on_scoped_mutations(tmp_path):
    store = _store(tmp_path)
    e0 = store.policy_epoch
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="admin", scope="global",
    )
    e1 = store.policy_epoch
    assert e1 > e0
    store.revoke_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="admin", scope="global",
    )
    assert store.policy_epoch > e1


def test_revoke_scoped_also_removes_exact_legacy_rows(tmp_path):
    store = _store(tmp_path)
    store.grant_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="channel", scope_id="c1",
    )
    store.grant_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="channel", scope_id="c2",
    )
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="keeper",
        group_name="informer", scope="channel", scope_id="c1",
    )
    store.grant_scoped_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="guild", scope_id="g1",
        legacy_rows=[
            {"scope": "channel", "scope_id": "c1"},
            {"scope": "channel", "scope_id": "c2"},
        ],
    )
    store.revoke_scoped_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="guild", scope_id="g1",
    )
    assert store.resolve_memberships(_req(user_id="x", roles=("team",))) == set()
    assert store.resolve_memberships(
        _req(user_id="x", roles=("team",), guild_id=None, scope="channel")
    ) == set()
    assert store.resolve_memberships(
        _req(user_id="keeper", platform="discord")
    ) == {"informer"}
    rows = store.list_memberships(platform="discord")
    kept = [m for m in rows if m.subject_id == "keeper"]
    assert len(kept) == 1


def test_revoke_scoped_refuses_unlinked_legacy_rows(tmp_path):
    store = _store(tmp_path)
    store.grant_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="channel", scope_id="c1",
    )
    store.grant_scoped_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="guild", scope_id="g1",
        legacy_rows=[{"scope": "channel", "scope_id": "c1"}],
    )
    store.grant_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="channel", scope_id="c2",
    )
    with pytest.raises(ValueError, match="partial scoped revoke"):
        store.revoke_scoped_membership(
            platform="discord", subject_type="role", subject_id="team",
            group_name="informer", scope="guild", scope_id="g1",
        )
    assert store.resolve_memberships(
        _req(user_id="x", roles=("team",))
    ) == {"informer"}


def test_resolver_epoch_snapshot_in_policy(tmp_path):
    from gateway.acl import BootstrapSuperAdmins, resolve_acl

    store = _store(tmp_path)
    store.grant_scoped_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="developer", scope="global",
    )
    policy = resolve_acl(store, _req(), bootstrap=BootstrapSuperAdmins.empty())
    assert policy.policy_epoch == store.policy_epoch
    assert policy.can_chat is True
