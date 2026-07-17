"""Manifest-driven scoped-membership migration: copy/cleanup/rollback."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import stat

import pytest

import gateway.acl_migration as acl_migration
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
        "writers_upgraded": True,
        "records": records,
    }
    manifest["approved_hash"] = manifest_hash(manifest)
    return manifest


def _run(store: ACLStore, manifest: dict, **kwargs):
    return run_migration(
        store,
        manifest,
        approved_hash=manifest["approved_hash"],
        **kwargs,
    )


def _guild_req(**kw) -> ACLRequest:
    base = dict(
        platform="discord", user_id="x", role_ids=("team",),
        scope="channel", channel_id="c1", guild_id="g1",
    )
    base.update(kw)
    return ACLRequest(**base)


def test_dry_run_mutates_nothing(tmp_path):
    store = _store(tmp_path)
    epoch = store.policy_epoch
    report = _run(store, _manifest(store), phase="copy", dry_run=True)
    assert report["dry_run"] is True
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    con_groups = store.resolve_memberships(_guild_req(guild_id="g2", channel_id="c1"))
    assert con_groups == {"informer"}
    assert store.policy_epoch == epoch
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scoped_memberships").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM migration_ledger").fetchone()[0] == 0


def test_copy_inserts_target_and_keeps_legacy(tmp_path):
    store = _store(tmp_path)
    e0 = store.policy_epoch
    report = _run(store, _manifest(store), phase="copy", dry_run=False)
    assert report["applied"] == 1
    assert store.policy_epoch > e0
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    assert store.resolve_memberships(_guild_req(guild_id="g2")) == {"informer"}
    assert store.resolve_memberships(_guild_req(guild_id="g2", channel_id="c9")) == set()


def test_copy_idempotent_rerun(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    _run(store, manifest, phase="copy", dry_run=False)
    report = _run(store, manifest, phase="copy", dry_run=False)
    assert report["already_applied"] is True


def test_copy_records_revoke_lineage(tmp_path):
    store = _store(tmp_path)
    _run(store, _manifest(store), phase="copy", dry_run=False)
    store.revoke_scoped_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="guild", scope_id="g1",
    )
    assert store.resolve_memberships(_guild_req()) == set()
    assert store.resolve_memberships(
        ACLRequest(
            platform="discord", user_id="keeper", scope="channel",
            channel_id="c1", guild_id="g1",
        )
    ) == {"default"}


def test_copy_rejects_wrong_store(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    manifest["store_id"] = "deadbeef"
    manifest["approved_hash"] = manifest_hash(manifest)
    with pytest.raises(MigrationError):
        _run(store, manifest, phase="copy", dry_run=False)


def test_copy_rejects_tampered_records(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    manifest["records"][0]["target"]["scope_id"] = "g-evil"
    with pytest.raises(MigrationError):
        _run(store, manifest, phase="copy", dry_run=False)


def test_copy_rejects_tampered_manifest_metadata(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    manifest["migration_id"] = "m-evil"
    with pytest.raises(MigrationError, match="approved hash"):
        _run(store, manifest, phase="copy", dry_run=False)


def test_external_owner_hash_cannot_be_replaced_with_recomputed_embedded_hash(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    owner_approved = manifest["approved_hash"]
    manifest["migration_id"] = "m-evil"
    manifest["approved_hash"] = manifest_hash(manifest)
    with pytest.raises(MigrationError, match="owner-approved hash"):
        run_migration(
            store,
            manifest,
            approved_hash=owner_approved,
            phase="copy",
            dry_run=False,
        )


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
        _run(store, manifest, phase="copy", dry_run=False)


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
        _run(store, manifest, phase="copy", dry_run=False)


def test_cleanup_removes_exact_legacy_only_after_copy(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    with pytest.raises(MigrationError):
        _run(store, manifest, phase="cleanup", dry_run=False)
    _run(store, manifest, phase="copy", dry_run=False)
    report = _run(store, manifest, phase="cleanup", dry_run=False)
    assert report["removed_legacy"] == 2
    assert store.resolve_memberships(_guild_req()) == {"informer"}
    assert store.resolve_memberships(
        ACLRequest(platform="discord", user_id="keeper", scope="channel",
                   channel_id="c1", guild_id="g1")
    ) == {"default"}
    backup_path = Path(report["backup_path"])
    assert backup_path.exists()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM memberships").fetchone()[0] == 3


def test_cleanup_requires_writer_upgrade_attestation(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    _run(store, manifest, phase="copy", dry_run=False)
    manifest["writers_upgraded"] = False
    manifest["approved_hash"] = manifest_hash(manifest)
    with pytest.raises(MigrationError, match="writers_upgraded"):
        _run(store, manifest, phase="cleanup", dry_run=False)


def test_cleanup_rejects_legacy_writer_drift_after_copy(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    _run(store, manifest, phase="copy", dry_run=False)
    store.grant_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="channel", scope_id="c3",
    )
    with pytest.raises(MigrationError, match="writer drift"):
        _run(store, manifest, phase="cleanup", dry_run=False)


def test_cleanup_rejects_missing_manifest_legacy_row_after_copy(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    _run(store, manifest, phase="copy", dry_run=False)
    store.revoke_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="informer", scope="channel", scope_id="c2",
    )
    with pytest.raises(MigrationError, match="writer drift"):
        _run(store, manifest, phase="cleanup", dry_run=False)


def test_cleanup_rejects_corrupt_backup_before_deleting_rows(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    manifest = _manifest(store)
    _run(store, manifest, phase="copy", dry_run=False)
    corrupt = tmp_path / "corrupt-backup.sqlite3"

    def fake_backup(_conn, _store, _tag):
        corrupt.write_bytes(b"not sqlite")
        return str(corrupt)

    monkeypatch.setattr(acl_migration, "_backup_locked_snapshot", fake_backup)
    with pytest.raises(MigrationError, match="backup integrity"):
        _run(store, manifest, phase="cleanup", dry_run=False)

    assert store.resolve_memberships(_guild_req(guild_id="g2")) == {"informer"}
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM migration_ledger WHERE phase='cleanup'"
        ).fetchone()[0] == 0


def test_rollback_restores_legacy_and_removes_target(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(store)
    _run(store, manifest, phase="copy", dry_run=False)
    _run(store, manifest, phase="cleanup", dry_run=False)
    report = _run(store, manifest, phase="rollback", dry_run=False)
    assert report["restored_legacy"] == 2
    assert store.resolve_memberships(
        _guild_req(guild_id="g9", channel_id="c1")
    ) == {"informer"}
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


def test_reserved_all_migration_deduplicates_existing_all_runtime(tmp_path):
    from gateway.acl_migration import migrate_reserved_all_grants

    store = ACLStore(tmp_path / "acl.sqlite3")
    store.grant_group_access("default", "all")
    store.grant_group_access("default", "all_runtime")

    report = migrate_reserved_all_grants(store, actor="operator")

    assert report["converted"] == 1
    assert store.list_group_grants("default") == ["all_runtime"]


def test_reserved_all_migration_noop_on_clean_store(tmp_path):
    from gateway.acl_migration import migrate_reserved_all_grants

    store = ACLStore(tmp_path / "acl.sqlite3")
    report = migrate_reserved_all_grants(store, actor="operator")
    assert report["converted"] == 0


def test_reserved_all_migration_rolls_back_update_and_dedup(tmp_path):
    from gateway.acl_migration import (
        migrate_reserved_all_grants,
        rollback_reserved_all_grants,
    )

    store = ACLStore(tmp_path / "acl.sqlite3")
    store.create_group("legacy")
    store.grant_group_access("default", "all")
    store.grant_group_access("legacy", "all")
    store.grant_group_access("legacy", "all_runtime")

    report = migrate_reserved_all_grants(store, actor="operator")
    assert report["converted"] == 2
    assert report["backup_path"]
    assert store.list_group_grants("default") == ["all_runtime"]
    assert store.list_group_grants("legacy") == ["all_runtime"]

    rollback = rollback_reserved_all_grants(store, actor="operator")
    assert rollback == {"restored": 2, "already_applied": False}
    assert store.list_group_grants("default") == ["all"]
    assert store.list_group_grants("legacy") == ["all", "all_runtime"]
    assert rollback_reserved_all_grants(store)["already_applied"] is True
