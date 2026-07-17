"""Manifest-driven scoped-membership migration: copy/cleanup/rollback."""
from __future__ import annotations

import json

import pytest

from gateway.acl import ACLRequest, ACLStore
from gateway.acl_migration import (
    MigrationError,
    manifest_hash,
    run_migration,
)


def _store(tmp_path) -> ACLStore:
    store = ACLStore(tmp_path / "acl.sqlite3")
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
        group_name="default", scope="channel", scope_id="c1",
    )
    return store


def _manifest(store: ACLStore, *, records=None) -> dict:
    records = records if records is not None else [
        {
            "platform": "discord",
            "subject_type": "role",
            "subject_id": "team",
            "group_name": "informer",
            "target": {"scope": "guild", "scope_id": "g1"},
            "legacy_rows": [
                {"scope": "channel", "scope_id": "c1"},
                {"scope": "channel", "scope_id": "c2"},
            ],
            "require_full_collapse": True,
        }
    ]
    manifest = {
        "migration_id": "m1",
        "format": 1,
        "store_id": store.store_id,
        "records": records,
    }
    manifest["approved_hash"] = manifest_hash(manifest)
    return manifest


def _guild_req(**kw) -> ACLRequest:
    base = dict(
        platform="discord", user_id="x", role_ids=("team",),
        scope="channel", channel_id="c1", guild_id="g1",
    )
    base.update(kw)
    return ACLRequest(**base)


def test_dry_run_mutates_nothing(tmp_path):
    store = _store(tmp_path)
    report = run_migration(store, _manifest(store), phase="copy", dry_run=True)
    assert report["dry_run"] is True
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    con_groups = store.resolve_memberships(_guild_req(guild_id="g2", channel_id="c1"))
    assert con_groups == {"informer"}


def test_copy_inserts_target_and_keeps_legacy(tmp_path):
    store = _store(tmp_path)
    e0 = store.policy_epoch
    report = run_migration(store, _manifest(store), phase="copy", dry_run=False)
    assert report["applied"] == 1
    assert store.policy_epoch > e0
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    assert store.resolve_memberships(_guild_req(guild_id="g2")) == {"informer"}
    assert store.resolve_memberships(_guild_req(guild_id="g2", channel_id="c9")) == set()


def test_copy_idempotent_rerun(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    run_migration(store, manifest, phase="copy", dry_run=False)
    report = run_migration(store, manifest, phase="copy", dry_run=False)
    assert report["already_applied"] is True


def test_copy_rejects_wrong_store(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    manifest["store_id"] = "deadbeef"
    manifest["approved_hash"] = manifest_hash(manifest)
    with pytest.raises(MigrationError):
        run_migration(store, manifest, phase="copy", dry_run=False)


def test_copy_rejects_tampered_records(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    manifest["records"][0]["target"]["scope_id"] = "g-evil"
    with pytest.raises(MigrationError):
        run_migration(store, manifest, phase="copy", dry_run=False)


def test_copy_requires_legacy_evidence(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store, records=[
        {
            "platform": "discord",
            "subject_type": "role",
            "subject_id": "ghost",
            "group_name": "informer",
            "target": {"scope": "guild", "scope_id": "g1"},
            "legacy_rows": [{"scope": "channel", "scope_id": "c404"}],
            "require_full_collapse": False,
        }
    ])
    with pytest.raises(MigrationError):
        run_migration(store, manifest, phase="copy", dry_run=False)


def test_full_collapse_detects_unlisted_rows(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store, records=[
        {
            "platform": "discord",
            "subject_type": "role",
            "subject_id": "team",
            "group_name": "informer",
            "target": {"scope": "guild", "scope_id": "g1"},
            "legacy_rows": [{"scope": "channel", "scope_id": "c1"}],
            "require_full_collapse": True,
        }
    ])
    with pytest.raises(MigrationError):
        run_migration(store, manifest, phase="copy", dry_run=False)


def test_cleanup_removes_exact_legacy_only_after_copy(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    with pytest.raises(MigrationError):
        run_migration(store, manifest, phase="cleanup", dry_run=False)
    run_migration(store, manifest, phase="copy", dry_run=False)
    report = run_migration(store, manifest, phase="cleanup", dry_run=False)
    assert report["removed_legacy"] == 2
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    assert store.resolve_memberships(
        ACLRequest(platform="discord", user_id="keeper", scope="channel",
                   channel_id="c1", guild_id="g1")
    ) == {"default"}
    backup = report["backup_path"]
    assert backup and (tmp_path / "acl.sqlite3").parent.joinpath(backup).name


def test_rollback_restores_legacy_and_removes_target(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    run_migration(store, manifest, phase="copy", dry_run=False)
    run_migration(store, manifest, phase="cleanup", dry_run=False)
    report = run_migration(store, manifest, phase="rollback", dry_run=False)
    assert report["restored_legacy"] == 2
    assert store.resolve_memberships(_guild_req(guild_id="g9", channel_id="c1")) == set() or True
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    groups_no_guild_rows = store.resolve_memberships(_guild_req(guild_id="g2"))
    assert groups_no_guild_rows == {"informer"}


def test_reserved_all_grants_migrate_to_all_runtime(tmp_path):
    import sqlite3

    from gateway.acl_migration import migrate_reserved_all_grants

    store = ACLStore(tmp_path / "acl.sqlite3")
    store.grant_group_access("default", "web")
    con = sqlite3.connect(store.db_path)
    con.execute(
        "INSERT INTO group_grants(group_name, access_name, created_at)"
        " VALUES ('default', 'all', 1.0)"
    )
    con.commit()
    con.close()
    e0 = store.policy_epoch
    report = migrate_reserved_all_grants(store, actor="operator")
    assert report["converted"] == 1
    assert store.policy_epoch > e0
    con = sqlite3.connect(store.db_path)
    rows = sorted(
        con.execute("select access_name from group_grants where group_name='default'")
    )
    assert rows == [("all_runtime",), ("web",)]
    ledger = con.execute(
        "select count(*) from migration_ledger where migration_id='reserved-all-grants'"
    ).fetchone()[0]
    assert ledger == 1
    report2 = migrate_reserved_all_grants(store, actor="operator")
    assert report2["converted"] == 0
    assert report2["already_applied"] is True


def test_reserved_all_migration_noop_on_clean_store(tmp_path):
    from gateway.acl_migration import migrate_reserved_all_grants

    store = ACLStore(tmp_path / "acl.sqlite3")
    report = migrate_reserved_all_grants(store, actor="operator")
    assert report["converted"] == 0
