"""Additive STRICT schema for the dynamic access matrix (S2-S5 storage)."""
from __future__ import annotations

import sqlite3

from gateway.acl import ACLStore

NEW_TABLES = ("subject_grants", "access_definitions", "applied_proposals", "group_flags")


def test_new_tables_created_strict(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    con = sqlite3.connect(store.db_path)
    for table in NEW_TABLES:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None, table
        assert "STRICT" in row[0], table


def test_init_idempotent_and_existing_untouched(tmp_path):
    path = tmp_path / "acl.sqlite3"
    first = ACLStore(path)
    first.grant_membership(
        platform="discord", subject_type="user", subject_id="legacy",
        group_name="default", scope="channel", scope_id="c1",
    )
    ACLStore(path)
    con = sqlite3.connect(path)
    assert con.execute("select count(*) from memberships").fetchone()[0] == 1
    for table in NEW_TABLES:
        assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0


def test_subject_grants_constraints(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    con = sqlite3.connect(store.db_path)
    con.execute("PRAGMA foreign_keys=ON")
    import pytest as _p
    with _p.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO subject_grants(platform, subject_type, subject_id, access_name,"
            " scope, scope_id, created_at) VALUES ('discord','alien','x','web','global','',1.0)"
        )
    with _p.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO subject_grants(platform, subject_type, subject_id, access_name,"
            " scope, scope_id, created_at) VALUES ('discord','role','r1','web','global','',1.0)"
        )


def test_pre_strict_acl_decisions_rebuild_copies_column_intersection(tmp_path):
    path = tmp_path / "acl.sqlite3"
    store = ACLStore(path)
    store.record_decision(
        capability_type="chat", capability_name="message", allowed=True,
        reason_code="allowed", platform="discord", user_id="legacy-user",
    )
    con = sqlite3.connect(path)
    columns = [
        row[1] for row in con.execute("PRAGMA table_info(acl_decisions)").fetchall()
        if row[1] != "matched_sources"
    ]
    column_list = ", ".join(columns)
    con.execute(
        f"CREATE TABLE acl_decisions_old AS"
        f" SELECT {column_list} FROM acl_decisions"
    )
    con.execute("DROP TABLE acl_decisions")
    con.execute("ALTER TABLE acl_decisions_old RENAME TO acl_decisions")
    con.commit()
    con.close()

    rebuilt = ACLStore(path)
    events = rebuilt._list_decisions_unchecked(limit=5)
    assert len(events) == 1
    assert events[0].user_id == "legacy-user"
    assert events[0].matched_sources == ()
