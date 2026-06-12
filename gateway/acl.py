"""Gateway ACL v1 store, resolver, and /acl parser.

This is an in-process gateway policy layer, not OS containment. Granting high-risk
capabilities such as ``terminal``, ``file``/write tools, package execution, or
arbitrary code execution grants broad host power unless a stronger sandbox wraps
Hermes outside this process.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_constants import get_hermes_home


BUILTIN_GROUPS = frozenset({"default", "admin"})
DISCOVERY_SLASH_COMMANDS = frozenset({"help", "whoami"})
DEFAULT_SAFE_TOOL_NAMES = frozenset({"clarify", "todo"})
DEFAULT_SAFE_SLASH_COMMANDS = DISCOVERY_SLASH_COMMANDS
# ``admin`` ACL group gets broad agent/tool use but, by v1 design, does not by
# itself grant `/acl` management. Bootstrap super-admin status adds `/acl`.
ADMIN_EXTRA_SLASH_COMMANDS = frozenset({
    "help",
    "whoami",
    "status",
    "profile",
    "platforms",
    "gateway",
    "commands",
    "usage",
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

    @property
    def allowed_concrete_tool_names(self) -> set[str]:
        """Backward-friendly alias for callers/tests using exact wording."""
        return set(self.allowed_tool_names)


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
    requires_confirmation: bool = False
    raw: str = ""


class ACLStore:
    """Profile-aware SQLite ACL store under Hermes home by default."""

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = Path(db_path) if db_path is not None else get_hermes_home() / "gateway_acl.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    name TEXT PRIMARY KEY,
                    builtin INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    access_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(group_name, access_name),
                    FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(platform, subject_type, subject_id, group_name, scope, scope_id),
                    FOREIGN KEY(group_name) REFERENCES groups(name) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    action TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    actor_platform TEXT NOT NULL DEFAULT '',
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    subject_type TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT '',
                    scope_id TEXT,
                    access_name TEXT NOT NULL DEFAULT '',
                    allowed INTEGER,
                    reason TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT ''
                );
                """
            )
            now = time.time()
            for name in sorted(BUILTIN_GROUPS):
                conn.execute(
                    "INSERT OR IGNORE INTO groups(name, builtin, created_at) VALUES (?, 1, ?)",
                    (name, now),
                )

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

    def resolve_memberships(self, request: ACLRequest) -> set[str]:
        platform = _norm_platform(request.platform)
        user_id = str(request.user_id).strip() if request.user_id is not None else ""
        role_ids = {str(r).strip() for r in (request.role_ids or []) if str(r).strip()}
        scope = _norm_scope(request.scope or request.chat_type or "dm")
        scope_id = _scope_id_from_request(scope, request)
        subjects: list[tuple[str, str]] = []
        if user_id:
            subjects.append(("user", user_id))
        subjects.extend(("role", rid) for rid in sorted(role_ids))
        if not subjects:
            return set()
        if scope == "channel" and not scope_id:
            return set()

        groups: set[str] = set()
        with self._connect() as conn:
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
        return groups

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

    def audit(self, *, limit: int = 50) -> list[ACLAuditRow]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, action, platform, actor_platform, actor_user_id,
                       subject_type, subject_id, group_name, scope, scope_id,
                       access_name, allowed, reason, details
                FROM audit_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
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
) -> EffectiveACLPolicy:
    platform = _norm_platform(request.platform)
    scope = _norm_scope(request.scope or request.chat_type or "dm")
    scope_id = _scope_id_from_request(scope, request)
    user_id = str(request.user_id or "")
    bootstrap = bootstrap or BootstrapSuperAdmins.empty()
    is_bootstrap_admin = bootstrap.is_super_admin(platform, user_id)

    groups = store.resolve_memberships(request)
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
            allowed_tools.update(_resolve_access_name("all"))
            allowed_slash.update(ADMIN_EXTRA_SLASH_COMMANDS)
        for access_name in store.list_group_grants(group):
            if _is_slash_access(access_name):
                allowed_slash.add(_slash_name(access_name))
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
    )


def collect_bootstrap_super_admins(
    platform_configs: Mapping[Any, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
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
    # /acl command needed to fix it.
    add("discord", env.get("DISCORD_ALLOWED_USERS"))

    for key, cfg in (platform_configs or {}).items():
        platform = _platform_key_to_name(key)
        extra = _platform_extra(cfg)
        if platform == "discord":
            add(platform, extra.get("acl_super_admins"))
            add(platform, extra.get("allow_admin_from"))
            add(platform, _flatten_group_mapping(extra.get("group_allow_admin_from")))
            # Legacy bootstrap authority (see spec note above).
            add(platform, extra.get("allowed_users"))
            add(platform, extra.get("allow_from"))
        else:
            # Other platforms can opt into the same explicit ACL admin naming.
            # Chat allowlists and roles are intentionally ignored for bootstrap
            # super-admins.
            env_key = f"{platform.upper()}_ACL_SUPER_ADMINS"
            add(platform, env.get(env_key))
            add(platform, extra.get("acl_super_admins"))
            add(platform, extra.get("allow_admin_from"))
            add(platform, _flatten_group_mapping(extra.get("group_allow_admin_from")))

    return BootstrapSuperAdmins({k: frozenset(v) for k, v in platform_users.items()})


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
    if head == "show":
        if len(parts) == 1:
            return ACLCommand(action="show", requires_confirmation=False, raw=raw)
        subject_type, subject_id = _parse_subject(parts[1])
        return ACLCommand(action="show", subject_type=subject_type, subject_id=subject_id, requires_confirmation=False, raw=raw)

    if head == "audit":
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
        if sub == "grant":
            if len(parts) < 4:
                raise ValueError("usage: /acl group grant <name> <access>")
            return ACLCommand(
                action="grant_group_access",
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


def _resolve_access_name(access_name: str) -> set[str]:
    access = str(access_name or "").strip()
    lower = access.lower()
    if lower in {"chat", "safe_chat"}:
        return set(DEFAULT_SAFE_TOOL_NAMES)
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
    "apply_acl_command",
    "collect_bootstrap_super_admins",
    "format_acl_policy",
    "parse_acl_command",
    "resolve_acl",
]
