"""S3 driver: reviewed tool_glob access definitions (owner decision 3)."""
from __future__ import annotations

import pytest

from gateway.acl import ACLStore
from gateway.acl_catalog import catalog_digest

CATALOG_V1 = {
    "jenkins_build_pc": "runtime_safe",
    "jenkins_status": "runtime_safe",
    "web_search": "runtime_safe",
}
CATALOG_V2 = dict(CATALOG_V1, jenkins_deploy="runtime_safe")


def _store(tmp_path) -> ACLStore:
    return ACLStore(tmp_path / "acl.sqlite3")


def test_create_definition_freezes_approved_snapshot(tmp_path):
    store = _store(tmp_path)
    snap = store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
        actor_platform="discord", actor_user_id="owner",
    )
    assert snap == {"jenkins_build_pc", "jenkins_status"}
    assert store.resolve_definition("jenkins-pc") == {"jenkins_build_pc", "jenkins_status"}


def test_new_catalog_matches_stay_pending_until_reapproval(tmp_path):
    store = _store(tmp_path)
    store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
        actor_platform="discord", actor_user_id="owner",
    )
    assert store.resolve_definition("jenkins-pc") == {"jenkins_build_pc", "jenkins_status"}
    pending = store.pending_definition_matches("jenkins-pc", catalog=CATALOG_V2)
    assert pending == {"jenkins_deploy"}
    assert store.resolve_definition("jenkins-pc") == {"jenkins_build_pc", "jenkins_status"}
    snap2 = store.approve_definition_expansion(
        "jenkins-pc", catalog=CATALOG_V2,
        actor_platform="discord", actor_user_id="owner",
    )
    assert snap2 == {"jenkins_build_pc", "jenkins_status", "jenkins_deploy"}
    assert store.resolve_definition("jenkins-pc") == snap2


def test_catalog_digest_recorded_and_epoch_bumped(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    e0 = store.policy_epoch
    store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
        actor_platform="discord", actor_user_id="owner",
    )
    assert store.policy_epoch > e0
    con = sqlite3.connect(store.db_path)
    row = con.execute(
        "select catalog_digest, kind from access_definitions where name='jenkins-pc'"
    ).fetchone()
    assert row == (catalog_digest(CATALOG_V1), "tool_glob")


def test_duplicate_and_invalid_rejected(tmp_path):
    store = _store(tmp_path)
    store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
        actor_platform="discord", actor_user_id="owner",
    )
    with pytest.raises(ValueError):
        store.create_access_definition(
            name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
            actor_platform="discord", actor_user_id="owner",
        )
    with pytest.raises(ValueError):
        store.create_access_definition(
            name="bad name!", spec="x*", catalog=CATALOG_V1,
            actor_platform="discord", actor_user_id="owner",
        )
    with pytest.raises(ValueError):
        store.create_access_definition(
            name="empty-spec", spec="", catalog=CATALOG_V1,
            actor_platform="discord", actor_user_id="owner",
        )


def test_unknown_definition_resolves_empty(tmp_path):
    assert _store(tmp_path).resolve_definition("nope") == set()


def test_definition_grants_resolve_in_policy(tmp_path):
    from gateway.acl import ACLRequest, BootstrapSuperAdmins, resolve_acl

    store = _store(tmp_path)
    store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
        actor_platform="discord", actor_user_id="owner",
    )
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="default", scope="channel", scope_id="c1",
    )
    store.grant_group_access("default", "def:jenkins-pc")
    policy = resolve_acl(
        store,
        ACLRequest(platform="discord", user_id="u1", scope="channel",
                   channel_id="c1", guild_id="g1"),
        bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert {"jenkins_build_pc", "jenkins_status"} <= policy.allowed_tool_names


def test_definition_via_subject_grant(tmp_path):
    from gateway.acl import ACLRequest, BootstrapSuperAdmins, resolve_acl

    store = _store(tmp_path)
    store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog=CATALOG_V1,
        actor_platform="discord", actor_user_id="owner",
    )
    store.grant_subject_access(
        platform="discord", subject_type="user", subject_id="solo",
        access_name="def:jenkins-pc", scope="global",
    )
    policy = resolve_acl(
        store,
        ACLRequest(platform="discord", user_id="solo", scope="dm"),
        bootstrap=BootstrapSuperAdmins.empty(),
    )
    assert {"jenkins_build_pc", "jenkins_status"} <= policy.allowed_tool_names


def test_definition_snapshots_only_reviewed_runtime_safe_tools(tmp_path):
    store = _store(tmp_path)
    catalog = {
        "web_search": "runtime_safe",
        "terminal": "operator",
        "cronjob": "control_plane",
        "mystery": "unclassified",
    }
    snapshot = store.create_access_definition(
        name="safe-only", spec="*", catalog=catalog,
        actor_platform="discord", actor_user_id="owner",
    )
    assert snapshot == {"web_search"}
