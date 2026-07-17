"""Deterministic core for the natural-language /acl planner (P1 handoff).

The model may only produce an ACLProposal: an ordered tuple of TYPED
steps with exact subjects, groups and scopes. There is no free-form
command or SQL surface, so raw model-generated mutations are impossible
by construction. Deterministic code validates every step against the
same normalizers the ACL store uses (unknown semantics fail closed),
renders the exact diff for owner confirmation, and the apply gate
revalidates requester binding, expiry and the confirmed digest before
applying all steps in one transaction with begin/commit audit rows.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from gateway.acl import (
    ACLStore,
    _norm_platform,
    _norm_scope,
    _norm_scope_id,
    _norm_subject_type,
    _validate_access_name,
    _validate_name,
    _validate_scoped_scope,
    _validate_subject_id,
)

_MEMBERSHIP_OPS = frozenset({"grant_membership", "revoke_membership"})
_SCOPED_OPS = frozenset({"grant_scoped_membership", "revoke_scoped_membership"})
_GRANT_OPS = frozenset({"grant_group_access", "revoke_group_access"})
_USER_ACCESS_OPS = frozenset({"grant_user_access", "revoke_user_access"})
_DEFINITION_OPS = frozenset({"create_access_definition", "approve_definition_expansion"})
_OPS = (
    _MEMBERSHIP_OPS | _SCOPED_OPS | _GRANT_OPS | _USER_ACCESS_OPS
    | _DEFINITION_OPS | {"create_group"}
)


class PlannerError(RuntimeError):
    """Raised when a proposal fails validation or an apply gate."""


def _reject_reserved(access_name: str) -> str:
    from gateway.acl_catalog import RESERVED_ACCESS_NAMES

    if str(access_name).strip().lower() in RESERVED_ACCESS_NAMES:
        raise PlannerError("reserved access names cannot be granted by proposals")
    return access_name


@dataclass(frozen=True)
class ACLProposalStep:
    op: str
    platform: Optional[str] = None
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    group_name: Optional[str] = None
    scope: Optional[str] = None
    scope_id: Optional[str] = None
    access_name: Optional[str] = None
    spec: Optional[str] = None
    expires_at: Optional[float] = None


@dataclass(frozen=True)
class ACLProposal:
    steps: tuple[ACLProposalStep, ...]
    requester_platform: str
    requester_user_id: str
    session_key: str
    created_at: float
    expires_at: float
    policy_epoch: Optional[int] = None


def _normalized_step(step: ACLProposalStep) -> dict[str, Any]:
    op = str(step.op or "").strip().lower()
    if op not in _OPS:
        raise PlannerError(f"unknown proposal op: {step.op!r}")
    out: dict[str, Any] = {"op": op}
    if op == "create_group":
        out["group_name"] = _validate_name(step.group_name or "", "group")
        return out
    if op in _DEFINITION_OPS:
        out["access_name"] = _validate_name(step.access_name or "", "definition")
        if op == "create_access_definition":
            spec = str(step.spec or "").strip()
            if not spec:
                raise PlannerError("create_access_definition requires a spec")
            out["spec"] = spec
        return out
    if op in _USER_ACCESS_OPS:
        out["platform"] = _norm_platform(step.platform)
        out["subject_type"] = _norm_subject_type(str(step.subject_type or ""))
        out["subject_id"] = _validate_subject_id(str(step.subject_id or ""))
        out["access_name"] = _reject_reserved(_validate_access_name(step.access_name or ""))
        scope = str(step.scope or "").strip().lower()
        if scope == "global":
            if out["subject_type"] != "user":
                raise PlannerError("global subject grants are user-only")
            out["scope"], out["scope_id"] = "global", ""
        elif scope in {"guild", "channel"}:
            sid = str(step.scope_id or "").strip()
            if not sid or sid == "*":
                raise PlannerError(f"{scope} subject grants require an explicit scope_id")
            out["scope"], out["scope_id"] = scope, sid
        elif scope == "dm":
            out["scope"], out["scope_id"] = "dm", ""
        else:
            raise PlannerError(f"unsupported subject grant scope: {step.scope!r}")
        out["expires_at"] = float(step.expires_at) if step.expires_at else None
        return out
    out["group_name"] = _validate_name(step.group_name or "", "group")
    if op in _GRANT_OPS:
        out["access_name"] = _validate_access_name(step.access_name or "")
        if op == "grant_group_access":
            _reject_reserved(out["access_name"])
        return out
    out["platform"] = _norm_platform(step.platform)
    out["subject_type"] = _norm_subject_type(str(step.subject_type or ""))
    out["subject_id"] = _validate_subject_id(str(step.subject_id or ""))
    if op in _SCOPED_OPS:
        scope, scope_id = _validate_scoped_scope(
            str(step.scope or ""), step.scope_id, out["subject_type"]
        )
        out["scope"], out["scope_id"] = scope, scope_id
    else:
        scope = _norm_scope(str(step.scope or ""))
        out["scope"] = scope
        out["scope_id"] = (_norm_scope_id(scope, step.scope_id) or "") if scope == "channel" else ""
    return out


def _normalized_steps(proposal: ACLProposal) -> list[dict[str, Any]]:
    try:
        steps = [_normalized_step(s) for s in proposal.steps]
    except (ValueError, TypeError) as exc:
        raise PlannerError(str(exc)) from exc
    if not steps:
        raise PlannerError("proposal has no steps")
    return steps


def proposal_digest(proposal: ACLProposal) -> str:
    canon = json.dumps(
        {
            "steps": _normalized_steps(proposal),
            "requester_platform": str(proposal.requester_platform or ""),
            "requester_user_id": str(proposal.requester_user_id or ""),
            "session_key": str(proposal.session_key or ""),
            "expires_at": float(proposal.expires_at or 0),
            "policy_epoch": proposal.policy_epoch,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def validate_proposal(store: ACLStore, proposal: ACLProposal) -> list[dict[str, Any]]:
    """Deterministically validate every step; unknown semantics fail closed."""
    steps = _normalized_steps(proposal)
    # SECURITY: the rendered diff must equal the applied diff - membership
    # grants may only target groups that exist or are created by an earlier
    # step of this same proposal (no implicit creation at apply time).
    known_groups = {
        str(row["name"])
        for row in store._connect().execute("SELECT name FROM groups").fetchall()
    }
    for step in steps:
        if step["op"] == "create_group":
            known_groups.add(step["group_name"])
        elif step["op"].startswith("grant") and "membership" in step["op"]:
            if step["group_name"] not in known_groups:
                raise PlannerError(
                    f"group does not exist and is not created by this proposal: "
                    f"{step['group_name']}"
                )
    if not str(proposal.requester_platform or "").strip():
        raise PlannerError("proposal missing requester_platform")
    if not str(proposal.requester_user_id or "").strip():
        raise PlannerError("proposal missing requester_user_id")
    if not str(proposal.session_key or "").strip():
        raise PlannerError("proposal missing session_key")
    return steps


def render_proposal(proposal: ACLProposal) -> str:
    """Exact grants/revokes/subjects/scope diff for owner confirmation."""
    lines: list[str] = []
    for step in _normalized_steps(proposal):
        op = step["op"]
        verb = "grant" if op.startswith(("grant", "create")) else "revoke"
        if op == "create_group":
            lines.append(f"+ create group '{step['group_name']}'")
            continue
        if op in _DEFINITION_OPS:
            spec_txt = f" spec '{step.get('spec')}'" if step.get("spec") else ""
            lines.append(f"+ {op} '{step['access_name']}'{spec_txt}")
            continue
        if op in _USER_ACCESS_OPS:
            sign = "+" if op.startswith("grant") else "-"
            verb = "grant" if sign == "+" else "revoke"
            scope_txt = step["scope"] if not step["scope_id"] else f"{step['scope']}:{step['scope_id']}"
            lines.append(
                f"{sign} {verb} access '{step['access_name']}' to "
                f"{step['subject_type']}:{step['subject_id']} [{step['platform']} {scope_txt}]"
            )
            continue
        if op in _GRANT_OPS:
            sign = "+" if verb == "grant" else "-"
            lines.append(
                f"{sign} {verb} access '{step['access_name']}' on group '{step['group_name']}'"
            )
            continue
        scope = step["scope"]
        scope_txt = scope if not step["scope_id"] else f"{scope}:{step['scope_id']}"
        sign = "+" if verb == "grant" else "-"
        lines.append(
            f"{sign} {verb} {step['subject_type']}:{step['subject_id']} -> "
            f"group '{step['group_name']}' [{step['platform']} {scope_txt}]"
        )
    return "\n".join(lines)


def _definition_spec_conn(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT spec FROM access_definitions WHERE name=?", (str(name),)
    ).fetchone()
    if row is None:
        raise PlannerError(f"unknown access definition: {name}")
    return str(row["spec"])


def _apply_step_conn(
    conn: sqlite3.Connection,
    step: Mapping[str, Any],
    now: float,
    *,
    catalog: Optional[Mapping[str, str]] = None,
) -> None:
    op = step["op"]
    if op in _DEFINITION_OPS:
        if not catalog:
            raise PlannerError("definition operations require the capability catalog")
        from gateway.acl_catalog import catalog_digest as _cat_digest

        snapshot = sorted(ACLStore._match_definition_spec(
            str(step.get("spec") or _definition_spec_conn(conn, step["access_name"])),
            catalog,
        ))
        if op == "create_access_definition":
            row = conn.execute(
                "SELECT 1 FROM access_definitions WHERE name=?", (step["access_name"],)
            ).fetchone()
            if row is not None:
                raise PlannerError(f"access definition already exists: {step['access_name']}")
            conn.execute(
                """
                INSERT INTO access_definitions(
                    name, kind, spec, catalog_digest, approved_snapshot,
                    created_at, approved_at
                ) VALUES (?, 'tool_glob', ?, ?, ?, ?, ?)
                """,
                (
                    step["access_name"], step["spec"], _cat_digest(catalog),
                    json.dumps(snapshot), now, now,
                ),
            )
        else:
            cur = conn.execute(
                "UPDATE access_definitions SET approved_snapshot=?, catalog_digest=?,"
                " approved_at=? WHERE name=?",
                (json.dumps(snapshot), _cat_digest(catalog), now, step["access_name"]),
            )
            if not cur.rowcount:
                raise PlannerError(f"unknown access definition: {step['access_name']}")
        return
    if op in _USER_ACCESS_OPS:
        if op == "grant_user_access":
            conn.execute(
                """
                INSERT OR IGNORE INTO subject_grants(
                    platform, subject_type, subject_id, access_name, scope,
                    scope_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step["platform"], step["subject_type"], step["subject_id"],
                    step["access_name"], step["scope"], step["scope_id"], now,
                    step.get("expires_at"),
                ),
            )
        else:
            conn.execute(
                """
                DELETE FROM subject_grants
                WHERE platform=? AND subject_type=? AND subject_id=?
                  AND access_name=? AND scope=? AND scope_id=?
                """,
                (
                    step["platform"], step["subject_type"], step["subject_id"],
                    step["access_name"], step["scope"], step["scope_id"],
                ),
            )
        return
    if op == "create_group":
        conn.execute(
            "INSERT OR IGNORE INTO groups(name, builtin, created_at) VALUES (?, 0, ?)",
            (step["group_name"], now),
        )
        return
    if op in _GRANT_OPS:
        row = conn.execute(
            "SELECT 1 FROM groups WHERE name=?", (step["group_name"],)
        ).fetchone()
        if row is None:
            raise PlannerError(f"group does not exist: {step['group_name']}")
        if op == "grant_group_access":
            conn.execute(
                "INSERT OR IGNORE INTO group_grants(group_name, access_name, created_at)"
                " VALUES (?, ?, ?)",
                (step["group_name"], step["access_name"], now),
            )
        else:
            conn.execute(
                "DELETE FROM group_grants WHERE group_name=? AND access_name=?",
                (step["group_name"], step["access_name"]),
            )
        return
    table = "scoped_memberships" if step["op"] in _SCOPED_OPS else "memberships"
    if op.startswith("grant"):
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {table}(
                platform, subject_type, subject_id, group_name, scope, scope_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step["platform"], step["subject_type"], step["subject_id"],
                step["group_name"], step["scope"], step["scope_id"], now,
            ),
        )
    else:
        conn.execute(
            f"""
            DELETE FROM {table}
            WHERE platform=? AND subject_type=? AND subject_id=? AND group_name=?
              AND scope=? AND scope_id=?
            """,
            (
                step["platform"], step["subject_type"], step["subject_id"],
                step["group_name"], step["scope"], step["scope_id"],
            ),
        )


def apply_proposal(
    store: ACLStore,
    proposal: ACLProposal,
    *,
    digest: str,
    actor_platform: str,
    actor_user_id: str,
    now: Optional[float] = None,
    catalog: Optional[Mapping[str, str]] = None,
    actor_is_bootstrap: bool = False,
) -> dict[str, Any]:
    """Apply a confirmed proposal transactionally after revalidating gates."""
    moment = time.time() if now is None else float(now)
    steps = validate_proposal(store, proposal)
    if not actor_is_bootstrap:
        # SECURITY (owner decision 6B): a non-bootstrap actor may apply a
        # proposal ONLY when every step is a membership ADD to a group
        # explicitly flagged safe_delegable. Positive enumeration - any
        # other op shape requires bootstrap authority.
        for step in steps:
            if step["op"] not in {"grant_membership", "grant_scoped_membership"}:
                raise PlannerError("this proposal requires bootstrap authority")
            if not store.is_safe_delegable(step["group_name"]):
                raise PlannerError(
                    f"group '{step['group_name']}' is not delegable; "
                    "bootstrap authority required"
                )
    if moment >= float(proposal.expires_at or 0):
        raise PlannerError("proposal confirmation expired")
    # SECURITY: the confirming actor must be the requester the proposal was
    # bound to; a confirmation can never be replayed by another principal.
    if (
        _norm_platform(actor_platform) != _norm_platform(proposal.requester_platform)
        or str(actor_user_id or "") != str(proposal.requester_user_id or "")
    ):
        raise PlannerError("confirmation actor does not match the proposal requester")
    if proposal_digest(proposal) != str(digest or ""):
        raise PlannerError("proposal digest mismatch; re-render and re-confirm")
    if proposal.policy_epoch is None or int(proposal.policy_epoch) != store.policy_epoch:
        raise PlannerError("policy changed since this proposal was rendered; re-render")

    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO applied_proposals(digest, applied_at, actor_platform, actor_user_id)"
                " VALUES (?, ?, ?, ?)",
                (str(digest), moment, str(actor_platform or ""), str(actor_user_id or "")),
            )
        except sqlite3.IntegrityError as exc:
            raise PlannerError("proposal confirmation already used") from exc
        store._audit_conn(
            conn,
            "proposal.apply.begin",
            actor_platform=actor_platform,
            actor_user_id=actor_user_id,
            details=json.dumps({"digest": digest, "steps": len(steps)}),
        )
        for step in steps:
            _apply_step_conn(conn, step, moment, catalog=catalog)
            store._audit_conn(
                conn,
                f"proposal.{step['op']}",
                platform=step.get("platform", ""),
                actor_platform=actor_platform,
                actor_user_id=actor_user_id,
                subject_type=step.get("subject_type", ""),
                subject_id=step.get("subject_id", ""),
                group_name=step.get("group_name", ""),
                scope=step.get("scope", ""),
                scope_id=step.get("scope_id") or None,
                access_name=step.get("access_name", ""),
            )
        ACLStore._bump_policy_epoch_conn(conn)
        store._audit_conn(
            conn,
            "proposal.apply.commit",
            actor_platform=actor_platform,
            actor_user_id=actor_user_id,
            details=json.dumps({"digest": digest}),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"applied": len(steps), "digest": digest}
