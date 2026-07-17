"""Deterministic grant-path recommendation engine (dynamic access matrix).

The backend owns facts, ordering, blast-radius math and mandatory
warnings; the conversational model may narrate these options or propose
new variants, but any variant must be converted to typed proposal steps
and re-ranked here before confirmation. Discord role cardinality is
never guessed: without a roster provider it reports 'unknown'.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from gateway.acl import ACLRequest, ACLStore
from gateway.acl_catalog import RESERVED_ACCESS_NAMES

RosterProvider = Callable[[str], int]


def _request_for(subject: Mapping[str, Any], scope_ctx: Mapping[str, Any]) -> ACLRequest:
    return ACLRequest(
        platform=str(subject.get("platform") or ""),
        user_id=str(subject.get("user_id") or "") or None,
        role_ids=tuple(subject.get("role_ids") or ()),
        scope=str(scope_ctx.get("scope") or "dm"),
        channel_id=scope_ctx.get("channel_id"),
        thread_id=scope_ctx.get("thread_id"),
        guild_id=scope_ctx.get("guild_id"),
    )


def _groups_carrying(store: ACLStore, access_name: str) -> dict[str, set[str]]:
    """group_name -> full grant set, for groups holding the exact access."""
    carrying: dict[str, set[str]] = {}
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT group_name, access_name FROM group_grants"
        ).fetchall()
    grants: dict[str, set[str]] = {}
    for row in rows:
        grants.setdefault(str(row["group_name"]), set()).add(str(row["access_name"]))
    for group, names in grants.items():
        if access_name in names:
            carrying[group] = names
    return carrying


def _member_counts(
    store: ACLStore, groups: set[str]
) -> dict[str, tuple[int, list[str]]]:
    """Bulk member counts across generations; two queries regardless of N."""
    if not groups:
        return {}
    users: dict[str, set[str]] = {group: set() for group in groups}
    roles: dict[str, set[str]] = {group: set() for group in groups}
    placeholders = ",".join("?" for _ in groups)
    params = tuple(sorted(groups))
    with store._connect() as conn:
        for table in ("memberships", "scoped_memberships"):
            rows = conn.execute(
                f"SELECT group_name, subject_type, subject_id FROM {table} "
                f"WHERE group_name IN ({placeholders})",
                params,
            ).fetchall()
            for row in rows:
                group = str(row["group_name"])
                if str(row["subject_type"]) == "user":
                    users[group].add(str(row["subject_id"]))
                else:
                    roles[group].add(str(row["subject_id"]))
    return {
        group: (len(users[group]), sorted(roles[group]))
        for group in groups
    }


def recommend_grant_paths(
    store: ACLStore,
    subject: Mapping[str, Any],
    access_name: str,
    scope_ctx: Mapping[str, Any],
    *,
    catalog: Optional[Mapping[str, str]] = None,
    roster_provider: Optional[RosterProvider] = None,
) -> list[dict[str, Any]]:
    access_name = str(access_name or "").strip()
    if not access_name or access_name.lower() in RESERVED_ACCESS_NAMES:
        raise ValueError("reserved or empty access cannot be recommended")
    concrete_name = (
        access_name.split(":", 1)[1]
        if access_name.lower().startswith("tool:")
        else access_name
    )
    capability_class = str((catalog or {}).get(concrete_name) or "unclassified")
    if capability_class != "runtime_safe":
        raise ValueError(
            f"access {access_name!r} is not a reviewed runtime_safe capability"
        )
    request = _request_for(subject, scope_ctx)
    subject_groups = store.resolve_memberships(request)
    direct_access = store.resolve_subject_access(request)
    carrying = _groups_carrying(store, access_name)

    options: list[dict[str, Any]] = []
    rank = 0

    effective_via_group = sorted(set(carrying) & subject_groups)
    if effective_via_group or access_name in direct_access:
        source = (
            {"type": "group", "name": effective_via_group[0]}
            if effective_via_group
            else {"type": "direct_grant", "name": access_name}
        )
        options.append({
            "kind": "already_effective",
            "rank": rank,
            "source": source,
            "blast_radius": 0,
            "warnings": [],
        })
        rank += 1

    options.append({
        "kind": "direct_user_grant",
        "rank": rank,
        "subject_id": str(subject.get("user_id") or ""),
        "blast_radius": 1,
        "warnings": [],
    })
    rank += 1

    for group in sorted(set(carrying) - subject_groups):
        options.append({
            "kind": "join_group",
            "rank": rank,
            "group_name": group,
            "blast_radius": 1,
            "excess_privileges": max(0, len(carrying[group]) - 1),
            "warnings": (
                ["joining grants every other access this group carries"]
                if len(carrying[group]) > 1 else []
            ),
        })
        rank += 1

    options.append({
        "kind": "create_group",
        "rank": rank,
        "blast_radius": 1,
        "warnings": ["prefer a group when this access will recur for a cohort"],
    })
    rank += 1

    candidate_groups = set(subject_groups) - set(carrying)
    member_counts = _member_counts(store, candidate_groups)
    for group in sorted(subject_groups):
        if group in carrying:
            continue
        user_count, role_ids = member_counts[group]
        blast: Any
        warnings = ["changes ALL effective members of this group"]
        if role_ids:
            if roster_provider is None:
                blast = "unknown"
                warnings.append(
                    "role members make all effective members unbounded without a roster"
                )
            else:
                blast = user_count + sum(
                    max(0, int(roster_provider(rid))) for rid in role_ids
                )
        else:
            blast = user_count
        options.append({
            "kind": "grant_to_existing_group",
            "rank": rank,
            "group_name": group,
            "blast_radius": blast,
            "warnings": warnings,
        })
        rank += 1

    return options
