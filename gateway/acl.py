"""Gateway ACL v1 store, resolver, and /acl parser.

This is an in-process gateway policy layer, not OS containment. Granting high-risk
capabilities such as ``terminal``, ``file``/write tools, package execution, or
arbitrary code execution grants broad host power unless a stronger sandbox wraps
Hermes outside this process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_constants import get_hermes_home


BUILTIN_GROUPS = frozenset({"default", "admin"})
# Platforms whose events are authenticated by the system itself (HMAC webhooks,
# HASS token, upstream relay auth, local CLI) — never user-chat surfaces.
# Gateway ACL user policy does not apply to them.
ACL_EXEMPT_PLATFORMS = frozenset({
    "local",
    "homeassistant",
    "webhook",
    "msgraph_webhook",
    "api_server",
    "relay",
})
DISCOVERY_SLASH_COMMANDS = frozenset({"help", "whoami"})
DEFAULT_SAFE_TOOL_NAMES = frozenset({"clarify", "todo"})
DEFAULT_SAFE_SLASH_COMMANDS = DISCOVERY_SLASH_COMMANDS
# ``admin`` ACL group gets broad agent/tool use but, by v1 design, does not by
# itself grant `/acl` management. Bootstrap super-admin status adds `/acl`.
# Seeded team-role presets (created once when missing; admin edits persist).
# Runtime tiers (sandbox/WSL/host) arrive in Phase 5; until then ``operator``
# is the only preset holding terminal/code execution and must be treated as
# highly trusted (see docs/superpowers/specs/2026-05-13-gateway-acl-design.md §6).
TEAM_PRESET_GROUPS: "dict[str, tuple[str, ...]]" = {
    "informer": ("chat", "web"),
    "researcher": ("chat", "web", "file_read"),
    "developer": ("chat", "web", "file_read", "file_write"),
    "operator": ("chat", "web", "file_read", "file_write", "terminal", "code_execution"),
}

BUILTIN_ACCESS_CAPABILITIES: "dict[str, frozenset[str]]" = {
    "file_read": frozenset({"read_file", "search_files"}),
    "file_write": frozenset({"write_file", "patch"}),
    # Recipient-scoped outbound DM. A dedicated capability by contract:
    # never granted via generic messaging/communication/delegation names.
    "whisper": frozenset({"whisper"}),
    # Restricted user-owned scheduler: the cronjob tool gated by the
    # dispatch-time argument/ownership checks in gateway/scheduler_policy.py.
    "scheduler_user": frozenset({"cronjob"}),
}

ADMIN_EXTRA_SLASH_COMMANDS = frozenset({
    "help",
    "whoami",
    "status",
    "profile",
    "platforms",
    "gateway",
    "commands",
    "usage",
    "background",
    "queue",
    "stop",
    "resume",
    "clear",
    "model",
})

_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_MENTION_RE = re.compile(r"^<@!?([0-9A-Za-z_.:-]+)>$")
_ROLE_MENTION_RE = re.compile(r"^<@&([0-9A-Za-z_.:-]+)>$")


@dataclass(frozen=True)
class ACLGroup:
    name: str
    builtin: bool = False
    created_at: float = 0.0


@dataclass(frozen=True)
class ACLMembership:
    platform: str
    subject_type: str
    subject_id: str
    group_name: str
    scope: str
    scope_id: Optional[str] = None
    created_at: float = 0.0


@dataclass(frozen=True)
class ACLAuditRow:
    id: int
    ts: float
    action: str
    platform: str = ""
    actor_platform: str = ""
    actor_user_id: str = ""
    subject_type: str = ""
    subject_id: str = ""
    group_name: str = ""
    scope: str = ""
    scope_id: Optional[str] = None
    access_name: str = ""
    allowed: Optional[bool] = None
    reason: str = ""
    details: str = ""


@dataclass(frozen=True)
class ACLRequest:
    platform: str
    user_id: Optional[str]
    role_ids: Iterable[str] = field(default_factory=tuple)
    scope: str = "dm"
    channel_id: Optional[str] = None
    thread_id: Optional[str] = None
    chat_type: Optional[str] = None
    guild_id: Optional[str] = None


@dataclass(frozen=True)
class EffectiveACLPolicy:
    platform: str
    user_id: str
    scope: str
    scope_id: Optional[str]
    can_chat: bool
    groups: set[str]
    allowed_slash_commands: set[str]
    allowed_tool_names: set[str]
    bootstrap_super_admin: bool = False
    denied_reason: Optional[str] = None
    policy_epoch: int = 0

    @property
    def allowed_concrete_tool_names(self) -> set[str]:
        """Backward-friendly alias for callers/tests using exact wording."""
        return set(self.allowed_tool_names)


@dataclass(frozen=True)
class ACLDecisionEvent:
    event_id: str
    ts: float
    capability_type: str
    capability_name: str
    allowed: bool
    reason_code: str
    platform: str
    user_id: str
    guild_id: Optional[str]
    channel_id: Optional[str]
    thread_id: Optional[str]
    session_key: str
    message_id: Optional[str]
    interaction_id: Optional[str]
    role_ids: tuple[str, ...]
    matched_groups: tuple[str, ...]
    bootstrap_super_admin: bool
    policy_epoch: int
    tool_call_id: Optional[str]
    request_id: Optional[str] = None


@dataclass(frozen=True)
class BootstrapSuperAdmins:
    platform_users: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "BootstrapSuperAdmins":
        return cls({})

    def is_super_admin(self, platform: str, user_id: Optional[str]) -> bool:
        if not user_id:
            return False
        return str(user_id) in self.platform_users.get(_norm_platform(platform), frozenset())

    def users_for(self, platform: str) -> frozenset[str]:
        return self.platform_users.get(_norm_platform(platform), frozenset())


@dataclass(frozen=True)
class ACLCommandContext:
    platform: str
    channel_id: Optional[str] = None
    scope: str = "dm"
    thread_id: Optional[str] = None


@dataclass(frozen=True)
class ACLCommand:
    action: str
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    group_name: Optional[str] = None
    access_name: Optional[str] = None
    scope: Optional[str] = None
    scope_id: Optional[str] = None
    event_id: Optional[str] = None
    requires_confirmation: bool = False
    raw: str = ""


class ACLStore:
    """Profile-aware SQLite ACL store under Hermes home by default."""

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = Path(db_path) if db_path is not None else get_hermes_home() / "gateway_acl.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.decision_max_rows = 50_000
        self._audit_degraded = False
        self._audit_degraded_logged_at = 0.0
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    _TABLE_DDL: "dict[str, str]" = {
        "groups": """
            CREATE TABLE groups (
                name TEXT PRIMARY KEY CHECK (name <> ''),
                builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
                created_at REAL NOT NULL
            ) STRICT
        """,
        "group_grants": """
            CREATE TABLE group_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT NOT NULL CHECK (group_name <> ''),
                access_name TEXT NOT NULL CHECK (access_name <> ''),
                created_at REAL NOT NULL,
                UNIQUE(group_name, access_name),
                FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE
            ) STRICT
        """,
        "memberships": """
            CREATE TABLE memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL CHECK (platform <> ''),
                subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'role')),
                subject_id TEXT NOT NULL CHECK (subject_id <> ''),
                group_name TEXT NOT NULL CHECK (group_name <> ''),
                scope TEXT NOT NULL CHECK (scope IN ('dm', 'channel')),
                scope_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(platform, subject_type, subject_id, group_name, scope, scope_id),
                FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE
            ) STRICT
        """,
        "scoped_memberships": """
            CREATE TABLE scoped_memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL CHECK (platform <> ''),
                subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'role')),
                subject_id TEXT NOT NULL CHECK (subject_id <> ''),
                group_name TEXT NOT NULL CHECK (group_name <> ''),
                scope TEXT NOT NULL CHECK (scope IN ('global', 'guild')),
                scope_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(platform, subject_type, subject_id, group_name, scope, scope_id),
                CHECK (scope <> 'global' OR (subject_type = 'user' AND scope_id = '')),
                CHECK (scope <> 'guild' OR (scope_id <> '' AND scope_id <> '*')),
                FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE
            ) STRICT
        """,
        "acl_meta": """
            CREATE TABLE acl_meta (
                key TEXT PRIMARY KEY CHECK (key <> ''),
                value TEXT NOT NULL
            ) STRICT
        """,
        "migration_ledger": """
            CREATE TABLE migration_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_id TEXT NOT NULL CHECK (migration_id <> ''),
                manifest_hash TEXT NOT NULL CHECK (manifest_hash <> ''),
                phase TEXT NOT NULL CHECK (phase IN ('copy', 'cleanup', 'rollback')),
                ts REAL NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '',
                UNIQUE(migration_id, phase)
            ) STRICT
        """,
        "subject_grants": """
            CREATE TABLE subject_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL CHECK (platform <> ''),
                subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'role')),
                subject_id TEXT NOT NULL CHECK (subject_id <> ''),
                access_name TEXT NOT NULL CHECK (access_name <> ''),
                scope TEXT NOT NULL CHECK (scope IN ('dm', 'channel', 'global', 'guild')),
                scope_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL,
                actor_platform TEXT NOT NULL DEFAULT '',
                actor_user_id TEXT NOT NULL DEFAULT '',
                UNIQUE(platform, subject_type, subject_id, access_name, scope, scope_id),
                CHECK (scope <> 'global' OR (subject_type = 'user' AND scope_id = '')),
                CHECK (scope NOT IN ('guild', 'channel') OR (scope_id <> '' AND scope_id <> '*'))
            ) STRICT
        """,
        "access_definitions": """
            CREATE TABLE access_definitions (
                name TEXT PRIMARY KEY CHECK (name <> ''),
                kind TEXT NOT NULL CHECK (kind IN ('tool_glob')),
                spec TEXT NOT NULL CHECK (spec <> ''),
                creation_actor_platform TEXT NOT NULL DEFAULT '',
                creation_actor_user_id TEXT NOT NULL DEFAULT '',
                catalog_digest TEXT NOT NULL DEFAULT '',
                approved_snapshot TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                approved_at REAL
            ) STRICT
        """,
        "applied_proposals": """
            CREATE TABLE applied_proposals (
                digest TEXT PRIMARY KEY CHECK (digest <> ''),
                applied_at REAL NOT NULL,
                actor_platform TEXT NOT NULL DEFAULT '',
                actor_user_id TEXT NOT NULL DEFAULT ''
            ) STRICT
        """,
        "group_flags": """
            CREATE TABLE group_flags (
                group_name TEXT NOT NULL CHECK (group_name <> ''),
                flag_name TEXT NOT NULL CHECK (flag_name <> ''),
                created_at REAL NOT NULL,
                PRIMARY KEY (group_name, flag_name),
                FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE
            ) STRICT
        """,
        "acl_decisions": """
            CREATE TABLE acl_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE CHECK (event_id <> ''),
                ts REAL NOT NULL,
                capability_type TEXT NOT NULL CHECK (capability_type IN (
                    'chat', 'slash', 'tool', 'schema', 'delegation',
                    'scheduler', 'dm_recipient'
                )),
                capability_name TEXT NOT NULL DEFAULT '',
                allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
                reason_code TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                guild_id TEXT,
                channel_id TEXT,
                thread_id TEXT,
                session_key TEXT NOT NULL DEFAULT '',
                message_id TEXT,
                interaction_id TEXT,
                role_ids TEXT NOT NULL DEFAULT '[]',
                matched_groups TEXT NOT NULL DEFAULT '[]',
                bootstrap_super_admin INTEGER NOT NULL DEFAULT 0
                    CHECK (bootstrap_super_admin IN (0, 1)),
                policy_epoch INTEGER NOT NULL DEFAULT 0,
                tool_call_id TEXT,
                request_id TEXT
            ) STRICT
        """,
        "audit_log": """
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                action TEXT NOT NULL CHECK (action <> ''),
                platform TEXT NOT NULL DEFAULT '',
                actor_platform TEXT NOT NULL DEFAULT '',
                actor_user_id TEXT NOT NULL DEFAULT '',
                subject_type TEXT NOT NULL DEFAULT '',
                subject_id TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT '',
                scope_id TEXT,
                access_name TEXT NOT NULL DEFAULT '',
                allowed INTEGER CHECK (allowed IS NULL OR allowed IN (0, 1)),
                reason TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT ''
            ) STRICT
        """,
    }

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            for table, ddl in self._TABLE_DDL.items():
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row is None:
                    conn.execute(ddl)
                elif "STRICT" not in str(row["sql"]):
                    self._rebuild_table_strict(conn, table, ddl)
            now = time.time()
            for name in sorted(BUILTIN_GROUPS):
                conn.execute(
                    "INSERT OR IGNORE INTO groups(name, builtin, created_at) VALUES (?, 1, ?)",
                    (name, now),
                )
            self._seed_preset_groups(conn, now)
            conn.execute(
                "INSERT OR IGNORE INTO acl_meta(key, value) VALUES ('store_id', ?)",
                (uuid.uuid4().hex,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO acl_meta(key, value) VALUES ('policy_epoch', '0')"
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"ACL migration foreign-key violations: {violations}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()

    def _rebuild_table_strict(self, conn: sqlite3.Connection, table: str, ddl: str) -> None:
        """Migrate a pre-STRICT table in place: rename, recreate, copy, drop.

        Runs inside the caller's transaction; legacy rows already satisfy the
        CHECK constraints because the same values were produced by this module's
        validated writers.
        """
        old = f"{table}_legacy_migration"
        conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
        conn.execute(ddl)
        cols = [str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        col_list = ", ".join(cols)
        conn.execute(f"INSERT INTO {table}({col_list}) SELECT {col_list} FROM {old}")
        conn.execute(f"DROP TABLE {old}")

    def _seed_preset_groups(self, conn: sqlite3.Connection, now: float) -> None:
        for group_name, grants in TEAM_PRESET_GROUPS.items():
            row = conn.execute("SELECT 1 FROM groups WHERE name=?", (group_name,)).fetchone()
            if row is not None:
                continue
            conn.execute(
                "INSERT INTO groups(name, builtin, created_at) VALUES (?, 0, ?)",
                (group_name, now),
            )
            for access_name in grants:
                conn.execute(
                    "INSERT OR IGNORE INTO group_grants(group_name, access_name, created_at) VALUES (?, ?, ?)",
                    (group_name, access_name, now),
                )
            self._audit_conn(conn, "group.seed_preset", group_name=group_name, details=",".join(grants))

    def create_group(self, name: str, *, actor_platform: str = "", actor_user_id: str = "") -> None:
        name = _validate_name(name, "group")
        if name in BUILTIN_GROUPS:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO groups(name, builtin, created_at) VALUES (?, 0, ?)",
                (name, time.time()),
            )
            self._audit_conn(conn, "group.create", actor_platform=actor_platform, actor_user_id=actor_user_id, group_name=name)

    def list_groups(self) -> list[ACLGroup]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name, builtin, created_at FROM groups ORDER BY name").fetchall()
        return [ACLGroup(row["name"], bool(row["builtin"]), float(row["created_at"])) for row in rows]

    def grant_group_access(self, group_name: str, access_name: str, *, actor_platform: str = "", actor_user_id: str = "") -> None:
        group_name = _validate_name(group_name, "group")
        access_name = _validate_access_name(access_name)
        with self._connect() as conn:
            self._ensure_group_conn(conn, group_name)
            conn.execute(
                "INSERT OR IGNORE INTO group_grants(group_name, access_name, created_at) VALUES (?, ?, ?)",
                (group_name, access_name, time.time()),
            )
            self._bump_policy_epoch_conn(conn)
            self._audit_conn(
                conn,
                "group.grant_access",
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                group_name=group_name,
                access_name=access_name,
            )

    def revoke_group_access(self, group_name: str, access_name: str, *, actor_platform: str = "", actor_user_id: str = "") -> None:
        group_name = _validate_name(group_name, "group")
        access_name = _validate_access_name(access_name)
        with self._connect() as conn:
            conn.execute("DELETE FROM group_grants WHERE group_name=? AND access_name=?", (group_name, access_name))
            self._bump_policy_epoch_conn(conn)
            self._audit_conn(
                conn,
                "group.revoke_access",
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                group_name=group_name,
                access_name=access_name,
            )

    def list_group_grants(self, group_name: str) -> list[str]:
        group_name = _validate_name(group_name, "group")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT access_name FROM group_grants WHERE group_name=? ORDER BY access_name",
                (group_name,),
            ).fetchall()
        return [str(row["access_name"]) for row in rows]

    def grant_membership(
        self,
        *,
        platform: str,
        subject_type: str,
        subject_id: str,
        group_name: str,
        scope: str,
        scope_id: Optional[str] = None,
        actor_platform: str = "",
        actor_user_id: str = "",
    ) -> None:
        platform = _norm_platform(platform)
        subject_type = _norm_subject_type(subject_type)
        subject_id = _validate_subject_id(subject_id)
        group_name = _validate_name(group_name, "group")
        scope = _norm_scope(scope)
        scope_id = _norm_scope_id(scope, scope_id)
        db_scope_id = scope_id or ""
        with self._connect() as conn:
            self._ensure_group_conn(conn, group_name)
            conn.execute(
                """
                INSERT OR IGNORE INTO memberships(
                    platform, subject_type, subject_id, group_name, scope, scope_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, subject_type, subject_id, group_name, scope, db_scope_id, time.time()),
            )
            self._bump_policy_epoch_conn(conn)
            self._audit_conn(
                conn,
                "membership.grant",
                platform=platform,
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                group_name=group_name,
                scope=scope,
                scope_id=scope_id,
            )

    def revoke_membership(
        self,
        *,
        platform: str,
        subject_type: str,
        subject_id: str,
        group_name: str,
        scope: str,
        scope_id: Optional[str] = None,
        actor_platform: str = "",
        actor_user_id: str = "",
    ) -> None:
        platform = _norm_platform(platform)
        subject_type = _norm_subject_type(subject_type)
        subject_id = _validate_subject_id(subject_id)
        group_name = _validate_name(group_name, "group")
        scope = _norm_scope(scope)
        scope_id = _norm_scope_id(scope, scope_id)
        db_scope_id = scope_id or ""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM memberships
                WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=? AND scope=?
                  AND (scope_id=? OR (scope_id IS NULL AND ?=''))
                """,
                (platform, subject_type, subject_id, group_name, scope, db_scope_id, db_scope_id),
            )
            self._bump_policy_epoch_conn(conn)
            self._audit_conn(
                conn,
                "membership.revoke",
                platform=platform,
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                group_name=group_name,
                scope=scope,
                scope_id=scope_id,
            )

    @property
    def store_id(self) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM acl_meta WHERE key='store_id'").fetchone()
        return str(row["value"]) if row else ""

    @property
    def policy_epoch(self) -> int:
        with self._connect() as conn:
            return self._policy_epoch_conn(conn)

    @staticmethod
    def _policy_epoch_conn(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM acl_meta WHERE key='policy_epoch'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _bump_policy_epoch_conn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE acl_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
            " WHERE key='policy_epoch'"
        )

    def grant_scoped_membership(
        self,
        *,
        platform: str,
        subject_type: str,
        subject_id: str,
        group_name: str,
        scope: str,
        scope_id: Optional[str] = None,
        actor_platform: str = "",
        actor_user_id: str = "",
    ) -> None:
        platform = _norm_platform(platform)
        subject_type = _norm_subject_type(subject_type)
        subject_id = _validate_subject_id(subject_id)
        group_name = _validate_name(group_name, "group")
        scope, db_scope_id = _validate_scoped_scope(scope, scope_id, subject_type)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_group_conn(conn, group_name)
            conn.execute(
                """
                INSERT OR IGNORE INTO scoped_memberships(
                    platform, subject_type, subject_id, group_name, scope, scope_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, subject_type, subject_id, group_name, scope, db_scope_id, time.time()),
            )
            self._bump_policy_epoch_conn(conn)
            self._audit_conn(
                conn,
                "scoped_membership.grant",
                platform=platform,
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                group_name=group_name,
                scope=scope,
                scope_id=db_scope_id or None,
            )

    def revoke_scoped_membership(
        self,
        *,
        platform: str,
        subject_type: str,
        subject_id: str,
        group_name: str,
        scope: str,
        scope_id: Optional[str] = None,
        legacy_rows: Optional[Iterable[Mapping[str, Any]]] = None,
        actor_platform: str = "",
        actor_user_id: str = "",
    ) -> None:
        platform = _norm_platform(platform)
        subject_type = _norm_subject_type(subject_type)
        subject_id = _validate_subject_id(subject_id)
        group_name = _validate_name(group_name, "group")
        scope, db_scope_id = _validate_scoped_scope(scope, scope_id, subject_type)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                DELETE FROM scoped_memberships
                WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
                  AND scope=? AND scope_id=?
                """,
                (platform, subject_type, subject_id, group_name, scope, db_scope_id),
            )
            for row in legacy_rows or ():
                legacy_scope = _norm_scope(str(row.get("scope") or ""))
                if legacy_scope == "channel":
                    legacy_scope_id = _norm_scope_id(legacy_scope, row.get("scope_id")) or ""
                else:
                    legacy_scope_id = ""
                conn.execute(
                    """
                    DELETE FROM memberships
                    WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
                      AND scope=? AND scope_id=?
                    """,
                    (platform, subject_type, subject_id, group_name, legacy_scope, legacy_scope_id),
                )
            self._bump_policy_epoch_conn(conn)
            self._audit_conn(
                conn,
                "scoped_membership.revoke",
                platform=platform,
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                group_name=group_name,
                scope=scope,
                scope_id=db_scope_id or None,
            )

    _DECISION_CAPABILITY_TYPES = frozenset({
        "chat", "slash", "tool", "schema", "delegation", "scheduler", "dm_recipient"
    })
    _DECISION_NAME_MAX = 120
    _DECISION_PRUNE_INTERVAL = 100
    _DECISION_LIST_MAX = 500

    def record_decision(
        self,
        *,
        capability_type: str,
        capability_name: str,
        allowed: bool,
        reason_code: str,
        platform: str,
        user_id: str,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        session_key: str = "",
        message_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
        request_id: Optional[str] = None,
        role_ids: Iterable[str] = (),
        matched_groups: Iterable[str] = (),
        bootstrap_super_admin: bool = False,
        policy_epoch: Optional[int] = None,
        tool_call_id: Optional[str] = None,
    ) -> str:
        capability_type = str(capability_type or "").strip().lower()
        if capability_type not in self._DECISION_CAPABILITY_TYPES:
            raise ValueError(f"unknown ACL decision capability_type: {capability_type!r}")
        reason_code = str(reason_code or "").strip()
        if not reason_code:
            raise ValueError("ACL decisions require a stable reason_code")
        capability_name = str(capability_name or "")
        if not capability_name:
            raise ValueError("ACL decisions require the exact capability_name")
        if len(capability_name) > self._DECISION_NAME_MAX:
            raise ValueError("capability_name exceeds the decision-log bound; pass the capability, not arguments")
        if not str(platform or "").strip():
            raise ValueError("ACL decisions require the platform")
        if not str(user_id or "").strip():
            raise ValueError("ACL decisions require the acting user_id")
        event_id = uuid.uuid4().hex
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO acl_decisions(
                        event_id, ts, capability_type, capability_name, allowed,
                        reason_code, platform, user_id, guild_id, channel_id,
                        thread_id, session_key, message_id, interaction_id,
                        role_ids, matched_groups, bootstrap_super_admin,
                        policy_epoch, tool_call_id, request_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        time.time(),
                        capability_type,
                        capability_name,
                        1 if allowed else 0,
                        reason_code,
                        _norm_platform(platform) if platform else "",
                        str(user_id or ""),
                        str(guild_id) if guild_id else None,
                        str(channel_id) if channel_id else None,
                        str(thread_id) if thread_id else None,
                        str(session_key or ""),
                        str(message_id) if message_id else None,
                        str(interaction_id) if interaction_id else None,
                        json.dumps([str(r) for r in (role_ids or ())]),
                        json.dumps([str(g) for g in (matched_groups or ())]),
                        1 if bootstrap_super_admin else 0,
                        int(policy_epoch) if policy_epoch is not None else self._policy_epoch_conn(conn),
                        str(tool_call_id) if tool_call_id else None,
                        str(request_id) if request_id else None,
                    ),
                )
                if cur.lastrowid and cur.lastrowid % self._DECISION_PRUNE_INTERVAL == 0:
                    self._prune_decisions_conn(conn, self.decision_max_rows)
            self._audit_degraded = False
            return event_id
        except (sqlite3.Error, OSError):
            # Fail-soft by contract: an audit write failure must never break
            # the message turn; the authorization decision itself already
            # happened and fail-closed rules live in the resolver. The sticky
            # degraded flag + rate-limited log make the evidence gap visible.
            self._audit_degraded = True
            now = time.time()
            if now - self._audit_degraded_logged_at > 60.0:
                self._audit_degraded_logged_at = now
                logging.getLogger(__name__).error(
                    "ACL decision audit write failed; decision log is degraded"
                )
            return ""

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> "ACLDecisionEvent":
        def _ids(raw: Any) -> tuple[str, ...]:
            try:
                return tuple(str(x) for x in json.loads(str(raw or "[]")))
            except (ValueError, TypeError):
                return ()

        return ACLDecisionEvent(
            event_id=str(row["event_id"]),
            ts=float(row["ts"]),
            capability_type=str(row["capability_type"]),
            capability_name=str(row["capability_name"]),
            allowed=bool(row["allowed"]),
            reason_code=str(row["reason_code"]),
            platform=str(row["platform"]),
            user_id=str(row["user_id"]),
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            session_key=str(row["session_key"]),
            message_id=row["message_id"],
            interaction_id=row["interaction_id"],
            role_ids=_ids(row["role_ids"]),
            matched_groups=_ids(row["matched_groups"]),
            bootstrap_super_admin=bool(row["bootstrap_super_admin"]),
            policy_epoch=int(row["policy_epoch"]),
            tool_call_id=row["tool_call_id"],
            request_id=row["request_id"],
        )

    def get_decision(self, event_id: str) -> Optional["ACLDecisionEvent"]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM acl_decisions WHERE event_id=?", (str(event_id),)
            ).fetchone()
        return self._decision_from_row(row) if row is not None else None

    def list_decisions(
        self,
        *,
        user_id: Optional[str] = None,
        allowed: Optional[bool] = None,
        capability_type: Optional[str] = None,
        limit: int = 50,
    ) -> list["ACLDecisionEvent"]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(str(user_id))
        if allowed is not None:
            clauses.append("allowed=?")
            params.append(1 if allowed else 0)
        if capability_type is not None:
            clauses.append("capability_type=?")
            params.append(str(capability_type))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(self._DECISION_LIST_MAX, max(1, int(limit))))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM acl_decisions {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decision_from_row(r) for r in rows]

    @property
    def audit_degraded(self) -> bool:
        return self._audit_degraded

    @staticmethod
    def _prune_decisions_conn(conn: sqlite3.Connection, max_rows: int) -> int:
        cur = conn.execute(
            """
            DELETE FROM acl_decisions WHERE id NOT IN (
                SELECT id FROM acl_decisions ORDER BY id DESC LIMIT ?
            )
            """,
            (max(0, int(max_rows)),),
        )
        return int(cur.rowcount or 0)

    def prune_decisions(self, *, max_rows: int) -> int:
        with self._connect() as conn:
            return self._prune_decisions_conn(conn, max_rows)

    def get_decision_audited(
        self,
        event_id: str,
        *,
        actor_platform: str,
        actor_user_id: str,
    ) -> Optional["ACLDecisionEvent"]:
        """Fetch a decision event, recording the read in the same transaction.

        SECURITY: withholds the event when the audit-the-read row cannot be
        persisted, so a trace can never be an unaudited disclosure channel.
        """
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM acl_decisions WHERE event_id=?", (str(event_id),)
                ).fetchone()
                self._audit_conn(
                    conn,
                    "decision.trace",
                    actor_platform=actor_platform,
                    actor_user_id=actor_user_id,
                    access_name=str(event_id),
                    allowed=row is not None,
                )
        except (sqlite3.Error, OSError):
            return None
        return self._decision_from_row(row) if row is not None else None

    def list_memberships(
        self,
        *,
        platform: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
    ) -> list[ACLMembership]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform is not None:
            clauses.append("platform=?")
            params.append(_norm_platform(platform))
        if subject_type is not None:
            clauses.append("subject_type=?")
            params.append(_norm_subject_type(subject_type))
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(str(subject_id).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT platform, subject_type, subject_id, group_name, scope, scope_id, created_at
                FROM memberships {where}
                ORDER BY platform, subject_type, subject_id, scope, scope_id, group_name
                """,
                params,
            ).fetchall()
        return [
            ACLMembership(
                platform=row["platform"],
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                group_name=row["group_name"],
                scope=row["scope"],
                scope_id=row["scope_id"] or None,
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    _GUILD_PLATFORMS = frozenset({"discord"})

    def resolve_memberships(self, request: ACLRequest) -> set[str]:
        return self.resolve_memberships_with_epoch(request)[0]

    def resolve_memberships_with_epoch(self, request: ACLRequest) -> tuple[set[str], int]:
        platform = _norm_platform(request.platform)
        user_id = str(request.user_id).strip() if request.user_id is not None else ""
        role_ids = {str(r).strip() for r in (request.role_ids or []) if str(r).strip()}
        scope = _norm_scope(request.scope or request.chat_type or "dm")
        scope_id = _scope_id_from_request(scope, request)
        guild_id = str(getattr(request, "guild_id", None) or "").strip()
        subjects: list[tuple[str, str]] = []
        if user_id:
            subjects.append(("user", user_id))
        subjects.extend(("role", rid) for rid in sorted(role_ids))
        if not subjects:
            return set(), self.policy_epoch
        if scope == "channel" and not scope_id:
            return set(), self.policy_epoch
        # SECURITY: on guild platforms a channel/thread request without guild
        # identity is contradictory context and fails closed (handoff P0).
        if scope == "channel" and platform in self._GUILD_PLATFORMS and not guild_id:
            return set(), self.policy_epoch

        groups: set[str] = set()
        with self._connect() as conn:
            epoch = self._policy_epoch_conn(conn)
            if user_id:
                rows = conn.execute(
                    """
                    SELECT group_name FROM scoped_memberships
                    WHERE platform=? AND subject_type='user' AND subject_id=?
                      AND scope='global'
                    """,
                    (platform, user_id),
                ).fetchall()
                groups.update(str(row["group_name"]) for row in rows)
            if scope == "channel" and guild_id:
                for subject_type, subject_id in subjects:
                    rows = conn.execute(
                        """
                        SELECT group_name FROM scoped_memberships
                        WHERE platform=? AND subject_type=? AND subject_id=?
                          AND scope='guild' AND scope_id=?
                        """,
                        (platform, subject_type, subject_id, guild_id),
                    ).fetchall()
                    groups.update(str(row["group_name"]) for row in rows)
            for subject_type, subject_id in subjects:
                if scope == "channel":
                    rows = conn.execute(
                        """
                        SELECT group_name FROM memberships
                        WHERE platform=? AND subject_type=? AND subject_id=? AND scope=?
                          AND scope_id=?
                        """,
                        (platform, subject_type, subject_id, scope, scope_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT group_name FROM memberships
                        WHERE platform=? AND subject_type=? AND subject_id=? AND scope=?
                          AND (scope_id IS NULL OR scope_id='')
                        """,
                        (platform, subject_type, subject_id, scope),
                    ).fetchall()
                groups.update(str(row["group_name"]) for row in rows)
        return groups, epoch

    def audit_denial(self, request: ACLRequest, *, reason: str, details: str = "") -> None:
        scope = _norm_scope(request.scope or request.chat_type or "dm")
        self.audit_event(
            "access.denied",
            platform=_norm_platform(request.platform),
            subject_type="user",
            subject_id=str(request.user_id or ""),
            scope=scope,
            scope_id=_scope_id_from_request(scope, request),
            allowed=False,
            reason=reason,
            details=details,
        )

    def audit_event(self, action: str, **kwargs: Any) -> None:
        with self._connect() as conn:
            self._audit_conn(conn, action, **kwargs)

    def audit(
        self,
        *,
        limit: int = 50,
        platform: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
    ) -> list[ACLAuditRow]:
        limit = max(1, min(int(limit), 500))
        where = ""
        params: list[Any] = []
        if subject_id:
            target_clauses = ["subject_id=?"]
            target_params: list[Any] = [str(subject_id)]
            if subject_type:
                target_clauses.append("subject_type=?")
                target_params.append(_norm_subject_type(subject_type))
            if platform:
                target_clauses.append("platform=?")
                target_params.append(_norm_platform(platform))
            branches = [f"({' AND '.join(target_clauses)})"]
            params.extend(target_params)
            if subject_type in {None, "user"}:
                actor_clauses = ["actor_user_id=?"]
                actor_params: list[Any] = [str(subject_id)]
                if platform:
                    actor_clauses.append("actor_platform=?")
                    actor_params.append(_norm_platform(platform))
                branches.append(f"({' AND '.join(actor_clauses)})")
                params.extend(actor_params)
            where = f"WHERE {' OR '.join(branches)}"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, ts, action, platform, actor_platform, actor_user_id,
                       subject_type, subject_id, group_name, scope, scope_id,
                       access_name, allowed, reason, details
                FROM audit_log {where} ORDER BY id DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [_audit_row_from_sql(row) for row in rows]

    def _ensure_group_conn(self, conn: sqlite3.Connection, group_name: str) -> None:
        row = conn.execute("SELECT 1 FROM groups WHERE name=?", (group_name,)).fetchone()
        if row is None:
            raise ValueError(f"unknown ACL group: {group_name}")

    def _audit_conn(self, conn: sqlite3.Connection, action: str, **kwargs: Any) -> None:
        allowed = kwargs.get("allowed")
        conn.execute(
            """
            INSERT INTO audit_log(
                ts, action, platform, actor_platform, actor_user_id, subject_type,
                subject_id, group_name, scope, scope_id, access_name, allowed, reason, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                str(action),
                str(kwargs.get("platform") or ""),
                str(kwargs.get("actor_platform") or ""),
                str(kwargs.get("actor_user_id") or ""),
                str(kwargs.get("subject_type") or ""),
                str(kwargs.get("subject_id") or ""),
                str(kwargs.get("group_name") or ""),
                str(kwargs.get("scope") or ""),
                kwargs.get("scope_id"),
                str(kwargs.get("access_name") or ""),
                None if allowed is None else (1 if allowed else 0),
                str(kwargs.get("reason") or ""),
                str(kwargs.get("details") or ""),
            ),
        )


def resolve_acl(
    store: ACLStore,
    request: ACLRequest,
    *,
    bootstrap: Optional[BootstrapSuperAdmins] = None,
    catalog: Optional[Mapping[str, str]] = None,
) -> EffectiveACLPolicy:
    platform = _norm_platform(request.platform)
    scope = _norm_scope(request.scope or request.chat_type or "dm")
    scope_id = _scope_id_from_request(scope, request)
    user_id = str(request.user_id or "")
    bootstrap = bootstrap or BootstrapSuperAdmins.empty()
    is_bootstrap_admin = bootstrap.is_super_admin(platform, user_id)

    groups, policy_epoch = store.resolve_memberships_with_epoch(request)
    if is_bootstrap_admin:
        groups.add("admin")

    allowed_tools: set[str] = set()
    allowed_slash: set[str] = set(DISCOVERY_SLASH_COMMANDS)

    if not groups:
        policy = EffectiveACLPolicy(
            platform=platform,
            user_id=user_id,
            scope=scope,
            scope_id=scope_id,
            can_chat=False,
            groups=set(),
            allowed_slash_commands=allowed_slash,
            allowed_tool_names=set(),
            bootstrap_super_admin=False,
            denied_reason="no_acl_membership",
            policy_epoch=policy_epoch,
        )
        try:
            store.audit_denial(request, reason="no_acl_membership")
        except Exception:
            pass
        return policy

    for group in sorted(groups):
        if group == "default":
            allowed_tools.update(DEFAULT_SAFE_TOOL_NAMES)
            allowed_slash.update(DEFAULT_SAFE_SLASH_COMMANDS)
        elif group == "admin":
            # SECURITY: ordinary admin gets the catalog-classified
            # all_runtime set only; missing catalog fails closed
            # (owner decision 1 baseline + decision 2 operator split).
            allowed_tools.update(_catalog_all_runtime(catalog))
            allowed_slash.update(ADMIN_EXTRA_SLASH_COMMANDS)
        for access_name in store.list_group_grants(group):
            if _is_slash_access(access_name):
                allowed_slash.add(_slash_name(access_name))
            elif _is_reserved_access(access_name):
                allowed_tools.update(_catalog_all_runtime(catalog))
            else:
                allowed_tools.update(_resolve_access_name(access_name))

    if is_bootstrap_admin:
        allowed_slash.add("acl")

    return EffectiveACLPolicy(
        platform=platform,
        user_id=user_id,
        scope=scope,
        scope_id=scope_id,
        can_chat=True,
        groups=set(groups),
        allowed_slash_commands=allowed_slash,
        allowed_tool_names=allowed_tools,
        bootstrap_super_admin=is_bootstrap_admin,
        denied_reason=None,
        policy_epoch=policy_epoch,
    )


def collect_bootstrap_super_admins(
    platform_configs: Mapping[Any, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
    include_allowlists: bool = True,
) -> BootstrapSuperAdmins:
    env = env or os.environ
    platform_users: dict[str, set[str]] = {}

    def add(platform: str, raw: Any) -> None:
        ids = _coerce_id_list(raw)
        if not ids:
            return
        platform_users.setdefault(_norm_platform(platform), set()).update(ids)

    add("discord", env.get("DISCORD_ACL_SUPER_ADMINS"))
    add("discord", env.get("GATEWAY_ACL_SUPER_ADMINS"))
    # Per the ACL design spec (docs/superpowers/specs/2026-05-13-gateway-acl-
    # design.md): "Users in DISCORD_ALLOWED_USERS or platform allow_from are
    # Hermes ACL super-admins." Without this, every pre-ACL deployment whose
    # owner is only in DISCORD_ALLOWED_USERS is locked out of chat AND of the
    # /acl command needed to fix it. With platform-wide ACL enforcement the
    # same lockout applies on every platform, so the legacy rule is applied
    # to each configured platform's allowlist below. Team deployments where
    # the allowlist is broader than the admin set opt out via
    # ``acl_bootstrap_from_allowlist: false`` (include_allowlists=False);
    # explicit *_ACL_SUPER_ADMINS / acl_super_admins / allow_admin_from
    # sources always count.
    if include_allowlists:
        add("discord", env.get("DISCORD_ALLOWED_USERS"))

    for key, cfg in (platform_configs or {}).items():
        platform = _platform_key_to_name(key)
        if platform in ACL_EXEMPT_PLATFORMS:
            continue
        extra = _platform_extra(cfg)
        add(platform, extra.get("acl_super_admins"))
        add(platform, extra.get("allow_admin_from"))
        add(platform, _flatten_group_mapping(extra.get("group_allow_admin_from")))
        if include_allowlists:
            add(platform, extra.get("allowed_users"))
            add(platform, extra.get("allow_from"))
        if platform != "discord":
            add(platform, env.get(f"{platform.upper()}_ACL_SUPER_ADMINS"))
            if include_allowlists:
                add(platform, env.get(_legacy_allowlist_env_key(platform)))

    return BootstrapSuperAdmins({k: frozenset(v) for k, v in platform_users.items()})


_LEGACY_ALLOWLIST_ENV_ALIASES = {"qqbot": "QQ_ALLOWED_USERS"}


def _legacy_allowlist_env_key(platform: str) -> str:
    return _LEGACY_ALLOWLIST_ENV_ALIASES.get(platform, f"{platform.upper()}_ALLOWED_USERS")


def acl_platform_enforced(platform: Any, enforced_platforms: Optional[Iterable[str]] = None) -> bool:
    if platform is None:
        return False
    name = _norm_platform(platform)
    if not name or name in ACL_EXEMPT_PLATFORMS:
        return False
    if enforced_platforms is None:
        return name == "discord"
    allowed = {str(p).strip().lower() for p in enforced_platforms if str(p).strip()}
    return "*" in allowed or name in allowed


def parse_acl_command(text: str, context: ACLCommandContext) -> ACLCommand:
    raw = text.strip()
    body = raw
    if body.startswith("/acl"):
        body = body[len("/acl"):].strip()
    if not body:
        body = "show"
    parts = body.split()
    if not parts:
        return ACLCommand(action="show", requires_confirmation=False, raw=raw)

    head = parts[0].lower()
    if head == "groups":
        return ACLCommand(action="list_groups", requires_confirmation=False, raw=raw)

    if head == "show":
        if len(parts) == 1:
            return ACLCommand(action="show", requires_confirmation=False, raw=raw)
        if parts[1].lower() == "group":
            if len(parts) < 3:
                raise ValueError("usage: /acl show group <name>")
            return ACLCommand(
                action="show_group",
                group_name=_validate_name(parts[2], "group"),
                requires_confirmation=False,
                raw=raw,
            )
        subject_type, subject_id = _parse_subject(parts[1])
        return ACLCommand(action="show", subject_type=subject_type, subject_id=subject_id, requires_confirmation=False, raw=raw)

    if head == "trace":
        if len(parts) != 2:
            raise ValueError("usage: /acl trace <event-id>")
        event_id = parts[1].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", event_id):
            raise ValueError("event-id must be a 32-char hex decision event id")
        return ACLCommand(
            action="trace",
            event_id=event_id,
            requires_confirmation=False,
            raw=raw,
        )

    if head == "audit":
        if len(parts) >= 2:
            subject_type, subject_id = _parse_subject(parts[1])
            return ACLCommand(
                action="audit",
                subject_type=subject_type,
                subject_id=subject_id,
                requires_confirmation=False,
                raw=raw,
            )
        return ACLCommand(action="audit", requires_confirmation=False, raw=raw)

    if head in {"grant", "revoke"}:
        if len(parts) < 5:
            raise ValueError("usage: /acl grant @user <group> in dm|this channel")
        subject_type, subject_id = _parse_subject(parts[1])
        group_name = _validate_name(parts[2], "group")
        if parts[3].lower() != "in":
            raise ValueError("expected 'in' before ACL scope")
        scope, scope_id = _parse_scope(parts[4:], context)
        return ACLCommand(
            action="grant_membership" if head == "grant" else "revoke_membership",
            subject_type=subject_type,
            subject_id=subject_id,
            group_name=group_name,
            scope=scope,
            scope_id=scope_id,
            requires_confirmation=True,
            raw=raw,
        )

    if head == "group":
        if len(parts) < 3:
            raise ValueError("usage: /acl group create <name> OR /acl group grant <name> <access>")
        sub = parts[1].lower()
        if sub == "create":
            return ACLCommand(
                action="create_group",
                group_name=_validate_name(parts[2], "group"),
                requires_confirmation=True,
                raw=raw,
            )
        if sub in {"grant", "revoke"}:
            if len(parts) < 4:
                raise ValueError(f"usage: /acl group {sub} <name> <access>")
            return ACLCommand(
                action="grant_group_access" if sub == "grant" else "revoke_group_access",
                group_name=_validate_name(parts[2], "group"),
                access_name=_validate_access_name(parts[3]),
                requires_confirmation=True,
                raw=raw,
            )
        raise ValueError(f"unsupported /acl group action: {sub}")

    raise ValueError(f"unsupported /acl action: {head}")


def apply_acl_command(
    store: ACLStore,
    command: ACLCommand,
    *,
    actor_platform: str = "",
    actor_user_id: str = "",
    platform: Optional[str] = None,
) -> str:
    """Apply a parsed, already-confirmed mutation command."""
    if command.action == "create_group":
        store.create_group(command.group_name or "", actor_platform=actor_platform, actor_user_id=actor_user_id)
        return f"ACL group created: {command.group_name}"
    if command.action == "grant_group_access":
        store.grant_group_access(
            command.group_name or "",
            command.access_name or "",
            actor_platform=actor_platform,
            actor_user_id=actor_user_id,
        )
        return f"ACL group grant added: {command.group_name} -> {command.access_name}"
    if command.action == "revoke_group_access":
        store.revoke_group_access(
            command.group_name or "",
            command.access_name or "",
            actor_platform=actor_platform,
            actor_user_id=actor_user_id,
        )
        return f"ACL group grant revoked: {command.group_name} -> {command.access_name}"
    if command.action == "grant_membership":
        store.grant_membership(
            platform=platform or actor_platform,
            subject_type=command.subject_type or "user",
            subject_id=command.subject_id or "",
            group_name=command.group_name or "",
            scope=command.scope or "dm",
            scope_id=command.scope_id,
            actor_platform=actor_platform,
            actor_user_id=actor_user_id,
        )
        return f"ACL membership granted: {command.subject_id} -> {command.group_name} in {_format_scope(command.scope, command.scope_id)}"
    if command.action == "revoke_membership":
        store.revoke_membership(
            platform=platform or actor_platform,
            subject_type=command.subject_type or "user",
            subject_id=command.subject_id or "",
            group_name=command.group_name or "",
            scope=command.scope or "dm",
            scope_id=command.scope_id,
            actor_platform=actor_platform,
            actor_user_id=actor_user_id,
        )
        return f"ACL membership revoked: {command.subject_id} -> {command.group_name} in {_format_scope(command.scope, command.scope_id)}"
    raise ValueError(f"/acl command is not a mutation: {command.action}")


def format_acl_policy(policy: EffectiveACLPolicy) -> str:
    tools = sorted(policy.allowed_tool_names)
    if not tools:
        tool_summary = "none"
    elif len(tools) <= 12:
        tool_summary = ", ".join(tools)
    else:
        high_risk = [t for t in tools if t in {"terminal", "process", "write_file", "patch", "execute_code", "memory", "session_search", "cronjob", "send_message", "discord_admin"}]
        tail = f"; high-risk: {', '.join(sorted(high_risk))}" if high_risk else ""
        tool_summary = f"{len(tools)} tools{tail}"
    return "\n".join(
        [
            f"platform: {policy.platform}",
            f"scope: {policy.scope}{(':' + policy.scope_id) if policy.scope_id else ''}",
            f"user id: {policy.user_id or '(unknown)'}",
            f"groups: {', '.join(sorted(policy.groups)) if policy.groups else 'none'}",
            f"can_chat: {str(policy.can_chat).lower()}",
            f"slash commands: {', '.join(sorted(policy.allowed_slash_commands)) if policy.allowed_slash_commands else 'none'}",
            f"tools: {tool_summary}",
            f"bootstrap_super_admin: {str(policy.bootstrap_super_admin).lower()}",
        ]
    )


def _audit_row_from_sql(row: sqlite3.Row) -> ACLAuditRow:
    allowed_raw = row["allowed"]
    return ACLAuditRow(
        id=int(row["id"]),
        ts=float(row["ts"]),
        action=str(row["action"]),
        platform=str(row["platform"]),
        actor_platform=str(row["actor_platform"]),
        actor_user_id=str(row["actor_user_id"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        group_name=str(row["group_name"]),
        scope=str(row["scope"]),
        scope_id=row["scope_id"],
        access_name=str(row["access_name"]),
        allowed=None if allowed_raw is None else bool(allowed_raw),
        reason=str(row["reason"]),
        details=str(row["details"]),
    )


def _norm_platform(platform: Any) -> str:
    name = _platform_key_to_name(platform)
    return str(name).strip().lower()


def _platform_key_to_name(key: Any) -> str:
    value = getattr(key, "value", key)
    return str(value).strip().lower()


def _platform_extra(platform_config: Any) -> dict[str, Any]:
    if platform_config is None:
        return {}
    extra = getattr(platform_config, "extra", None)
    if isinstance(extra, dict):
        return extra
    if isinstance(platform_config, dict):
        return platform_config
    return {}


def _norm_subject_type(subject_type: str) -> str:
    val = str(subject_type or "").strip().lower()
    if val not in {"user", "role"}:
        raise ValueError("ACL subject_type must be 'user' or 'role'")
    return val


def _norm_scope(scope: str) -> str:
    val = str(scope or "").strip().lower()
    if val in {"direct", "private", "dm"}:
        return "dm"
    if val in {"channel", "group", "guild", "thread"}:
        return "channel"
    raise ValueError("ACL scope must be 'dm' or 'channel'")


def _validate_scoped_scope(
    scope: str, scope_id: Optional[str], subject_type: str
) -> tuple[str, str]:
    value = str(scope or "").strip().lower()
    if value == "global":
        if subject_type != "user":
            raise ValueError("global ACL scope is user-only; role IDs are platform-local")
        if scope_id not in (None, ""):
            raise ValueError("global ACL scope takes no scope_id")
        return "global", ""
    if value == "guild":
        sid = str(scope_id or "").strip()
        if not sid or sid == "*":
            raise ValueError("guild ACL scope requires an explicit guild scope_id")
        return "guild", sid
    raise ValueError("scoped ACL membership scope must be 'global' or 'guild'")


def _norm_scope_id(scope: str, scope_id: Optional[str]) -> Optional[str]:
    if scope == "dm":
        return None
    if scope_id is None:
        raise ValueError("channel ACL scope requires scope_id")
    value = str(scope_id).strip()
    if not value:
        raise ValueError("channel ACL scope requires scope_id")
    if value == "*":
        raise ValueError("channel ACL scope requires explicit scope_id, not '*'")
    return value


def _scope_id_from_request(scope: str, request: ACLRequest) -> Optional[str]:
    if scope == "dm":
        return None
    return str(request.channel_id or "").strip() or None


def _validate_name(name: str, label: str) -> str:
    value = str(name or "").strip().lower()
    if not value or not _VALID_NAME_RE.match(value):
        raise ValueError(f"invalid ACL {label} name")
    return value


def _validate_access_name(access_name: str) -> str:
    value = str(access_name or "").strip()
    if not value or not _VALID_NAME_RE.match(value):
        raise ValueError("invalid ACL access name")
    return value


def _validate_subject_id(subject_id: str) -> str:
    value = str(subject_id or "").strip()
    if not value:
        raise ValueError("empty ACL subject id")
    return value


def _coerce_id_list(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, Mapping):
        raw = _flatten_group_mapping(raw)
    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (part for part in raw.split(",") if part.strip())
    else:
        items = (raw,)
    out = [str(item).strip() for item in items if str(item).strip()]
    return frozenset(out)


def _flatten_group_mapping(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        items: list[Any] = []
        for value in raw.values():
            items.extend(_coerce_id_list(value))
        return items
    return list(_coerce_id_list(raw))


def _is_reserved_access(access_name: str) -> bool:
    from gateway.acl_catalog import RESERVED_ACCESS_NAMES

    return str(access_name or "").strip().lower() in RESERVED_ACCESS_NAMES


def _catalog_all_runtime(catalog: Optional[Mapping[str, str]]) -> frozenset[str]:
    from gateway.acl_catalog import resolve_all_runtime

    return resolve_all_runtime(catalog)


def _resolve_access_name(access_name: str) -> set[str]:
    access = str(access_name or "").strip()
    lower = access.lower()
    if lower in {"all", "all_runtime"}:
        # Reserved computed names: never live-expand outside the
        # catalog-aware resolver path (fail closed everywhere else).
        return set()
    if lower in {"chat", "safe_chat"}:
        return set(DEFAULT_SAFE_TOOL_NAMES)
    if lower in BUILTIN_ACCESS_CAPABILITIES:
        return set(BUILTIN_ACCESS_CAPABILITIES[lower])
    if lower.startswith("tool:"):
        return {access.split(":", 1)[1]}
    if lower.startswith("toolset:"):
        return _resolve_toolset(access.split(":", 1)[1])
    resolved = _resolve_toolset(access)
    if resolved:
        return resolved
    if _is_slash_access(access):
        return set()
    return {access}


def _resolve_toolset(name: str) -> set[str]:
    try:
        from toolsets import resolve_toolset
        return set(resolve_toolset(name))
    except Exception:
        return set()


def _is_slash_access(access_name: str) -> bool:
    lower = str(access_name or "").strip().lower()
    return lower.startswith(("cmd:", "command:", "slash:"))


def _slash_name(access_name: str) -> str:
    return str(access_name).split(":", 1)[1].strip().lstrip("/").replace("_", "-").lower()


def _parse_subject(token: str) -> tuple[str, str]:
    value = token.strip()
    role_match = _ROLE_MENTION_RE.match(value)
    if role_match:
        return "role", role_match.group(1)
    user_match = _MENTION_RE.match(value)
    if user_match:
        return "user", user_match.group(1)
    if value.startswith("@&"):
        return "role", _validate_subject_id(value[2:])
    if value.startswith("@"):
        return "user", _validate_subject_id(value[1:])
    return "user", _validate_subject_id(value)


def _parse_scope(parts: list[str], context: ACLCommandContext) -> tuple[str, Optional[str]]:
    if not parts:
        raise ValueError("missing ACL scope")
    first = parts[0].lower()
    if first in {"dm", "dms", "direct"}:
        return "dm", None
    if first == "this" and len(parts) >= 2 and parts[1].lower() == "channel":
        if not context.channel_id:
            raise ValueError("this channel scope requires channel context")
        return "channel", str(context.channel_id)
    if first in {"channel", "group"}:
        if len(parts) >= 2:
            return "channel", str(parts[1])
        if not context.channel_id:
            raise ValueError("channel scope requires channel id")
        return "channel", str(context.channel_id)
    raise ValueError("scope must be 'dm' or 'this channel'")


def _format_scope(scope: Optional[str], scope_id: Optional[str]) -> str:
    if scope == "channel":
        return f"channel:{scope_id}" if scope_id else "channel"
    return "dm"


__all__ = [
    "ACL_EXEMPT_PLATFORMS",
    "ACLCommand",
    "ACLCommandContext",
    "ACLGroup",
    "ACLMembership",
    "ACLAuditRow",
    "ACLRequest",
    "ACLStore",
    "BootstrapSuperAdmins",
    "DISCOVERY_SLASH_COMMANDS",
    "EffectiveACLPolicy",
    "acl_platform_enforced",
    "apply_acl_command",
    "collect_bootstrap_super_admins",
    "format_acl_policy",
    "parse_acl_command",
    "resolve_acl",
]
