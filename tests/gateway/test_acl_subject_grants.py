"""S2 driver: selector-based direct grants (user/role) with scopes + expiry."""
from __future__ import annotations

import time

import pytest

from gateway.acl import ACLRequest, ACLStore

NOW = time.time()


def _store(tmp_path) -> ACLStore:
    return ACLStore(tmp_path / "acl.sqlite3")


def _req(**kw) -> ACLRequest:
    base = dict(
        platform="discord", user_id="u1", role_ids=(), scope="channel",
        channel_id="c1", guild_id="g1",
    )
    base.update(kw)
    return ACLRequest(**base)


def test_user_global_grant_applies_everywhere(tmp_path):
    store = _store(tmp_path)
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="web", scope="global",
    )
    assert "web" in store.resolve_subject_access(_req())
    assert "web" in store.resolve_subject_access(
        _req(scope="dm", channel_id=None, guild_id=None)
    )


def test_role_guild_grant_only_inside_guild_never_dm(tmp_path):
    store = _store(tmp_path)
    store.grant_subject_access(
        platform="discord", subject_type="role", subject_id="team",
        access_name="jenkins-pc", scope="guild", scope_id="g1",
    )
    assert "jenkins-pc" in store.resolve_subject_access(
        _req(user_id="x", role_ids=("team",))
    )
    assert store.resolve_subject_access(
        _req(user_id="x", role_ids=("team",), guild_id="g2")
    ) == set()
    assert store.resolve_subject_access(
        _req(user_id="x", role_ids=("team",), scope="dm", channel_id=None, guild_id="g1")
    ) == set()


def test_role_global_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.grant_subject_access(
            platform="discord", subject_type="role", subject_id="team",
            access_name="web", scope="global",
        )


def test_expiry_fail_closed(tmp_path):
    store = _store(tmp_path)
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="web", scope="global", expires_at=NOW - 1,
    )
    assert store.resolve_subject_access(_req(), now=NOW) == set()
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="search", scope="global", expires_at=NOW,
    )
    assert store.resolve_subject_access(_req(), now=NOW) == set()
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="tts", scope="global", expires_at=NOW + 60,
    )
    assert store.resolve_subject_access(_req(), now=NOW) == {"tts"}


def test_revoke_and_epoch(tmp_path):
    store = _store(tmp_path)
    e0 = store.policy_epoch
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="web", scope="global",
    )
    assert store.policy_epoch > e0
    e1 = store.policy_epoch
    store.revoke_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="web", scope="global",
    )
    assert store.policy_epoch > e1
    assert store.resolve_subject_access(_req()) == set()


def test_resolve_acl_unions_subject_grants(tmp_path):
    from gateway.acl import BootstrapSuperAdmins, resolve_acl

    store = _store(tmp_path)
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="default", scope="channel", scope_id="c1",
    )
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="tool:special_tool", scope="global",
    )
    policy = resolve_acl(store, _req(), bootstrap=BootstrapSuperAdmins.empty())
    assert "special_tool" in policy.allowed_tool_names
