"""S2 driver: selector-based direct grants (user/role) with scopes + expiry."""
from __future__ import annotations

import sqlite3
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


def test_acl_connection_context_releases_file_descriptor(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    with store._connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


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


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_expiry_rejected(tmp_path, expires_at):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="finite"):
        store.grant_subject_access(
            platform="discord",
            subject_type="user",
            subject_id="u1",
            access_name="web",
            scope="global",
            expires_at=expires_at,
        )


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
    assert "direct_grant:tool:special_tool" in policy.matched_sources


def test_regrant_converges_expiry_without_replacing_created_at(tmp_path):
    store = _store(tmp_path)
    params = dict(
        platform="discord", subject_type="user", subject_id="u1",
        access_name="web", scope="global",
    )
    store.grant_subject_access(**params, expires_at=NOW + 600)
    with store._connect() as conn:
        before = conn.execute(
            "SELECT created_at FROM subject_grants WHERE subject_id='u1' AND access_name='web'"
        ).fetchone()["created_at"]
    store.grant_subject_access(**params, expires_at=NOW + 10)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT created_at, expires_at FROM subject_grants"
            " WHERE subject_id='u1' AND access_name='web'"
        ).fetchone()
    assert row["created_at"] == before
    assert row["expires_at"] == NOW + 10


def test_role_dm_and_dm_scope_id_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="user-only"):
        store.grant_subject_access(
            platform="discord", subject_type="role", subject_id="team",
            access_name="web", scope="dm",
        )
    with pytest.raises(ValueError, match="no scope_id"):
        store.grant_subject_access(
            platform="discord", subject_type="user", subject_id="u1",
            access_name="web", scope="dm", scope_id="unexpected",
        )


@pytest.mark.parametrize("access_name", ["whisper", "scheduler_user"])
def test_unwired_access_grants_rejected(tmp_path, access_name):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="dispatch wiring"):
        store.grant_subject_access(
            platform="discord", subject_type="user", subject_id="u1",
            access_name=access_name, scope="global",
        )
    with pytest.raises(ValueError, match="dispatch wiring"):
        store.grant_group_access("default", access_name)
