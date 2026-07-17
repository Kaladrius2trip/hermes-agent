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
