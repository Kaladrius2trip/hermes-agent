"""Manifest-driven migration of legacy dm/channel ACL rows to scoped rows.

Implements the owner-approved migration contract from the ACL P0 handoff:
copy phase inserts scoped target rows while legacy rows keep working
(dual-read window); cleanup deletes the exact manifest-listed legacy rows
after every ACL writer is upgraded; rollback restores the pre-migration
state. Every phase is transactional, idempotent, audited in the
migration_ledger, and gated on the manifest hash the owner approved.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Mapping, Optional

from gateway.acl import (
    ACLStore,
    _norm_platform,
    _norm_scope,
    _norm_scope_id,
    _norm_subject_type,
    _validate_name,
    _validate_scoped_scope,
    _validate_subject_id,
)

_PHASES = ("copy", "cleanup", "rollback")


class MigrationError(RuntimeError):
    """Raised when a migration precondition or invariant fails."""


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    canon = json.dumps(manifest.get("records"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _normalize_record(rec: Mapping[str, Any]) -> dict[str, Any]:
    platform = _norm_platform(rec.get("platform"))
    subject_type = _norm_subject_type(str(rec.get("subject_type") or ""))
    subject_id = _validate_subject_id(str(rec.get("subject_id") or ""))
    group_name = _validate_name(str(rec.get("group_name") or ""), "group")
    target = rec.get("target") or {}
    scope, scope_id = _validate_scoped_scope(
        str(target.get("scope") or ""), target.get("scope_id"), subject_type
    )
    legacy_rows: list[dict[str, str]] = []
    for row in rec.get("legacy_rows") or ():
        legacy_scope = _norm_scope(str(row.get("scope") or ""))
        if legacy_scope == "channel":
            legacy_scope_id = _norm_scope_id(legacy_scope, row.get("scope_id")) or ""
        else:
            legacy_scope_id = ""
        legacy_rows.append({"scope": legacy_scope, "scope_id": legacy_scope_id})
    return {
        "platform": platform,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "group_name": group_name,
        "scope": scope,
        "scope_id": scope_id,
        "legacy_rows": legacy_rows,
        "require_full_collapse": bool(rec.get("require_full_collapse")),
    }


def _backup_store(store: ACLStore, tag: str) -> str:
    dest_path = f"{store.db_path}.backup-{tag}-{int(time.time())}"
    src = sqlite3.connect(store.db_path)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_path


def _ledger_rows(conn: sqlite3.Connection, migration_id: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT phase, manifest_hash, payload FROM migration_ledger WHERE migration_id=?",
        (migration_id,),
    ).fetchall()
    return {str(r["phase"]): r for r in rows}


def _write_ledger(
    conn: sqlite3.Connection,
    *,
    migration_id: str,
    approved_hash: str,
    phase: str,
    actor: str,
    payload: Mapping[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO migration_ledger(migration_id, manifest_hash, phase, ts, actor, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            migration_id,
            approved_hash,
            phase,
            time.time(),
            actor,
            json.dumps(payload, sort_keys=True),
        ),
    )


def run_migration(
    store: ACLStore,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    dry_run: bool = True,
    actor: str = "",
) -> dict[str, Any]:
    if phase not in _PHASES:
        raise MigrationError(f"unknown migration phase: {phase!r}")
    if manifest.get("format") != 1:
        raise MigrationError("unsupported manifest format")
    migration_id = str(manifest.get("migration_id") or "").strip()
    if not migration_id:
        raise MigrationError("manifest missing migration_id")
    if str(manifest.get("store_id") or "") != store.store_id:
        raise MigrationError("manifest store_id does not match this store")
    approved = str(manifest.get("approved_hash") or "")
    if approved != manifest_hash(manifest):
        raise MigrationError("manifest records do not match the approved hash")
    records = [_normalize_record(r) for r in (manifest.get("records") or ())]
    if not records:
        raise MigrationError("manifest has no records")

    backup_path: Optional[str] = None
    if phase == "cleanup" and not dry_run:
        backup_path = _backup_store(store, f"{migration_id}-cleanup")

    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = _ledger_rows(conn, migration_id)
        for row in prior.values():
            if str(row["manifest_hash"]) != approved:
                raise MigrationError("ledger hash mismatch for this migration_id")

        if phase == "copy":
            result = _phase_copy(conn, records, prior, dry_run)
        elif phase == "cleanup":
            result = _phase_cleanup(conn, records, prior, dry_run)
        else:
            result = _phase_rollback(conn, records, prior, dry_run)

        if dry_run or result.get("already_applied"):
            conn.rollback()
        else:
            _write_ledger(
                conn,
                migration_id=migration_id,
                approved_hash=approved,
                phase=phase,
                actor=actor,
                payload=result.get("payload") or {},
            )
            ACLStore._bump_policy_epoch_conn(conn)
            conn.commit()
        result.pop("payload", None)
        result["dry_run"] = dry_run
        result["migration_id"] = migration_id
        result["phase"] = phase
        if backup_path is not None:
            result["backup_path"] = backup_path
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _select_legacy(conn: sqlite3.Connection, rec: Mapping[str, Any], row: Mapping[str, str]):
    return conn.execute(
        """
        SELECT id, scope, scope_id FROM memberships
        WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
          AND scope=? AND scope_id=?
        """,
        (
            rec["platform"], rec["subject_type"], rec["subject_id"],
            rec["group_name"], row["scope"], row["scope_id"],
        ),
    ).fetchone()


def _target_exists(conn: sqlite3.Connection, rec: Mapping[str, Any]) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM scoped_memberships
        WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
          AND scope=? AND scope_id=?
        """,
        (
            rec["platform"], rec["subject_type"], rec["subject_id"],
            rec["group_name"], rec["scope"], rec["scope_id"],
        ),
    ).fetchone() is not None


def _phase_copy(conn, records, prior, dry_run):
    if "copy" in prior:
        return {"already_applied": True, "applied": 0}
    preimages = []
    for rec in records:
        for row in rec["legacy_rows"]:
            if _select_legacy(conn, rec, row) is None:
                raise MigrationError(
                    "legacy evidence row missing for "
                    f"{rec['subject_type']}:{rec['subject_id']} {row}"
                )
        if rec["require_full_collapse"]:
            listed = {(r["scope"], r["scope_id"]) for r in rec["legacy_rows"]}
            actual = conn.execute(
                """
                SELECT scope, scope_id FROM memberships
                WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
                """,
                (
                    rec["platform"], rec["subject_type"],
                    rec["subject_id"], rec["group_name"],
                ),
            ).fetchall()
            unlisted = {(str(r["scope"]), str(r["scope_id"])) for r in actual} - listed
            if unlisted:
                raise MigrationError(
                    f"full collapse requested but unlisted legacy rows exist: {sorted(unlisted)}"
                )
        if _target_exists(conn, rec):
            raise MigrationError(
                "target scoped row already exists outside this migration for "
                f"{rec['subject_type']}:{rec['subject_id']}"
            )
        preimages.append({k: rec[k] for k in (
            "platform", "subject_type", "subject_id", "group_name",
            "scope", "scope_id", "legacy_rows",
        )})
    if dry_run:
        return {"applied": len(records), "already_applied": False}
    now = time.time()
    for rec in records:
        try:
            conn.execute(
                """
                INSERT INTO scoped_memberships(
                    platform, subject_type, subject_id, group_name, scope, scope_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["platform"], rec["subject_type"], rec["subject_id"],
                    rec["group_name"], rec["scope"], rec["scope_id"], now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MigrationError(f"target insert conflict: {exc}") from exc
    return {
        "applied": len(records),
        "already_applied": False,
        "payload": {"targets": preimages},
    }


def _phase_cleanup(conn, records, prior, dry_run):
    if "copy" not in prior:
        raise MigrationError("cleanup requires a completed copy phase")
    if "cleanup" in prior:
        return {"already_applied": True, "removed_legacy": 0}
    deleted = []
    for rec in records:
        if not _target_exists(conn, rec):
            raise MigrationError(
                "target scoped row missing before cleanup for "
                f"{rec['subject_type']}:{rec['subject_id']}"
            )
        for row in rec["legacy_rows"]:
            found = _select_legacy(conn, rec, row)
            if found is not None:
                deleted.append({
                    "platform": rec["platform"],
                    "subject_type": rec["subject_type"],
                    "subject_id": rec["subject_id"],
                    "group_name": rec["group_name"],
                    "scope": row["scope"],
                    "scope_id": row["scope_id"],
                })
    if dry_run:
        return {"removed_legacy": len(deleted), "already_applied": False}
    for row in deleted:
        conn.execute(
            """
            DELETE FROM memberships
            WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
              AND scope=? AND scope_id=?
            """,
            (
                row["platform"], row["subject_type"], row["subject_id"],
                row["group_name"], row["scope"], row["scope_id"],
            ),
        )
    return {
        "removed_legacy": len(deleted),
        "already_applied": False,
        "payload": {"deleted_legacy": deleted},
    }


def _phase_rollback(conn, records, prior, dry_run):
    if "copy" not in prior:
        raise MigrationError("nothing to roll back: no copy phase recorded")
    if "rollback" in prior:
        return {"already_applied": True, "restored_legacy": 0}
    cleanup_payload: dict[str, Any] = {}
    if "cleanup" in prior:
        cleanup_payload = json.loads(str(prior["cleanup"]["payload"]) or "{}")
    restored = list(cleanup_payload.get("deleted_legacy") or ())
    if dry_run:
        return {"restored_legacy": len(restored), "already_applied": False}
    now = time.time()
    for rec in records:
        conn.execute(
            """
            DELETE FROM scoped_memberships
            WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
              AND scope=? AND scope_id=?
            """,
            (
                rec["platform"], rec["subject_type"], rec["subject_id"],
                rec["group_name"], rec["scope"], rec["scope_id"],
            ),
        )
    for row in restored:
        conn.execute(
            """
            INSERT OR IGNORE INTO memberships(
                platform, subject_type, subject_id, group_name, scope, scope_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["platform"], row["subject_type"], row["subject_id"],
                row["group_name"], row["scope"], row["scope_id"], now,
            ),
        )
    return {
        "restored_legacy": len(restored),
        "already_applied": False,
        "payload": {"restored_legacy": restored},
    }


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ACL scoped-membership migration")
    parser.add_argument("--store", required=True, help="Path to gateway_acl.sqlite3")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--phase", required=True, choices=_PHASES)
    parser.add_argument("--execute", action="store_true", help="Apply (default dry-run)")
    parser.add_argument("--actor", default="operator")
    args = parser.parse_args(argv)
    with open(args.manifest, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    store = ACLStore(args.store)
    report = run_migration(
        store, manifest, phase=args.phase, dry_run=not args.execute, actor=args.actor
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def migrate_reserved_all_grants(store: ACLStore, *, actor: str = "") -> dict[str, Any]:
    """Convert persisted reserved 'all' group grants to 'all_runtime'.

    'all' no longer live-expands anywhere; stored grants must become the
    catalog-computed all_runtime instead (owner decision 1 baseline).
    Idempotent: a second run is a ledger-guarded no-op. Empty stores are
    a clean no-op without a ledger row.
    """
    migration_id = "reserved-all-grants"
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT 1 FROM migration_ledger WHERE migration_id=? AND phase='copy'",
            (migration_id,),
        ).fetchone()
        if prior is not None:
            conn.rollback()
            return {"converted": 0, "already_applied": True}
        rows = conn.execute(
            "SELECT id, group_name FROM group_grants WHERE lower(access_name)='all'"
        ).fetchall()
        if not rows:
            conn.rollback()
            return {"converted": 0, "already_applied": False}
        for row in rows:
            conn.execute(
                "UPDATE group_grants SET access_name='all_runtime' WHERE id=?",
                (int(row["id"]),),
            )
        _write_ledger(
            conn,
            migration_id=migration_id,
            approved_hash=hashlib.sha256(b"reserved-all-grants-v1").hexdigest(),
            phase="copy",
            actor=actor,
            payload={
                "converted": [
                    {"id": int(r["id"]), "group_name": str(r["group_name"])}
                    for r in rows
                ]
            },
        )
        ACLStore._bump_policy_epoch_conn(conn)
        conn.commit()
        return {"converted": len(rows), "already_applied": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
