"""Team-mode MVP — ``hermes team …`` over the existing Kanban (P8 Phase 5).

This module is a small, *additive* layer on top of ``hermes_cli.kanban_db``.
It turns a one-line goal plus a named team template into a deterministic
Kanban dependency graph:

    lead / coordination task  (root, no parents)
        ├── member task A     (parent: lead)
        ├── member task B     (parent: lead)
        └── … one per configured member
    review / handoff task     (parents: every member task)

Design constraints (intentional):

* **Deterministic & template-based** — no LLM, no daemon, no network. The
  same goal + team always produces the same graph.
* **No schema migration** — team metadata (role / category / toolsets /
  read-only / approval guards) rides in the task ``body`` as an embedded
  JSON block plus human-readable prose, and the ``tenant`` column carries
  a ``team:<slug>`` marker so a whole plan can be queried back cheaply.
* **Execution stays with the existing dispatcher** — we only create cards.
  The existing Kanban per-profile caps remain the source of truth; the team
  ``max_parallel`` is an advisory ceiling surfaced in ``hermes team status``.
* **No inbound remote control** — there is no listener, callback, or
  OpenClaw-style remote-trigger surface here by default.
* **Approval guards** — sensitive actions (push / merge / publish /
  send_message) are recorded as approval-gate metadata on every card so a
  worker/operator knows they require explicit human approval.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_cli import config as _config
from hermes_cli import kanban_db as kb

# Marker used to embed/recover the machine-readable team metadata block in a
# task body. Kept as an HTML comment so it renders invisibly in Markdown.
TEAM_META_MARKER = "hermes-team-meta"
TEAM_META_VERSION = 1

# Tenant prefix that scopes every card belonging to a team plan. Lets
# ``team status`` recover a whole plan with a single exact-match query.
TEAM_TENANT_PREFIX = "team:"

# Actions that always require explicit human approval before a worker may
# perform them. Overridable via config ``teams.approval_required``.
DEFAULT_APPROVAL_GUARDS = ["push", "merge", "publish", "send_message"]

# Workspace kinds we accept in a template (mirrors kanban_db.VALID_WORKSPACE_KINDS;
# duplicated as a plain set so plan building stays import-light and gives early
# feedback during ``--dry-run`` before any DB call).
_VALID_WORKSPACES = {"scratch", "worktree", "dir"}


# ---------------------------------------------------------------------------
# Built-in team templates
# ---------------------------------------------------------------------------
#
# A template is a dict with:
#   description: str
#   lead:    member-spec (role forced to "lead")
#   members: list[member-spec] (role forced to "member")
#   review:  member-spec (optional; role forced to "reviewer")
#
# A member-spec is a dict:
#   name:      short role name (used in the title)
#   category:  free-form category tag (coordination / implementation / …)
#   toolsets:  list[str] advisory toolset names (metadata only — NOT skills)
#   readonly:  bool, whether this role should avoid mutating the repo
#   workspace: scratch | worktree | dir
#   profile:   optional Hermes profile to assign (None -> dispatcher default)
#   capability_profile: optional Capability Profile contract for dispatcher
#   skills:    optional list of skill bundle names to force-load
BUILTIN_TEAMS: dict[str, dict[str, Any]] = {
    "coding": {
        "description": "Lead + implementer + tester + reviewer for code work.",
        "lead": {
            "name": "lead", "category": "coordination",
            "toolsets": ["file"], "readonly": True, "workspace": "scratch",
        },
        "members": [
            {
                "name": "implementer", "category": "implementation",
                "toolsets": ["terminal", "file"], "readonly": False,
                "workspace": "worktree",
            },
            {
                "name": "tester", "category": "testing",
                "toolsets": ["terminal", "file"], "readonly": False,
                "workspace": "worktree",
            },
        ],
        "review": {
            "name": "reviewer", "category": "review",
            "toolsets": ["file"], "readonly": True, "workspace": "scratch",
        },
    },
    "research": {
        "description": "Lead + searcher + analyst + reviewer for research.",
        "lead": {
            "name": "lead", "category": "coordination",
            "toolsets": ["file"], "readonly": True, "workspace": "scratch",
        },
        "members": [
            {
                "name": "searcher", "category": "discovery",
                "toolsets": ["web"], "readonly": True, "workspace": "scratch",
            },
            {
                "name": "analyst", "category": "analysis",
                "toolsets": ["file"], "readonly": False, "workspace": "scratch",
            },
        ],
        "review": {
            "name": "reviewer", "category": "synthesis",
            "toolsets": ["file"], "readonly": True, "workspace": "scratch",
        },
    },
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TeamNode:
    """One card in a planned team graph."""

    local_id: str
    role: str  # lead | member | reviewer
    name: str
    title: str
    category: str
    toolsets: list[str]
    readonly: bool
    workspace: str
    profile: Optional[str]
    capability_profile: Optional[str]
    skills: list[str]
    parents_local: list[str]
    approval_guards: list[str]
    body: str = ""
    task_id: Optional[str] = None  # filled in by materialize_plan


@dataclass
class TeamPlan:
    """A fully-specified, not-yet-materialised team graph."""

    goal: str
    team: str
    slug: str
    tenant: str
    max_parallel: int
    approval_guards: list[str]
    nodes: list[TeamNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_teams_config(cfg: Optional[dict] = None) -> dict:
    """Resolve the effective teams config: built-ins merged with user config.

    ``cfg`` is the full Hermes config mapping (as returned by
    ``config.load_config()``); when ``None`` it is loaded lazily. User
    templates under ``teams.templates`` are merged over the built-ins.
    """
    if cfg is None:
        cfg = _config.load_config()
    raw = (cfg or {}).get("teams") or {}

    templates: dict[str, Any] = {k: dict(v) for k, v in BUILTIN_TEAMS.items()}
    for name, tpl in (raw.get("templates") or {}).items():
        templates[name] = tpl

    approval = raw.get("approval_required")
    if not approval:
        approval = list(DEFAULT_APPROVAL_GUARDS)

    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_parallel": int(raw.get("max_parallel") or 0),
        "approval_required": list(approval),
        "templates": templates,
    }


def resolve_team(team_name: str, teams_cfg: dict) -> dict:
    """Return the template dict for ``team_name`` or raise ``ValueError``."""
    templates = teams_cfg.get("templates") or {}
    if team_name not in templates:
        available = ", ".join(sorted(templates)) or "(none)"
        raise ValueError(
            f"unknown team {team_name!r}; available: {available}"
        )
    return templates[team_name]


# ---------------------------------------------------------------------------
# Metadata embedding / recovery
# ---------------------------------------------------------------------------

_META_RE = re.compile(
    r"<!--\s*" + re.escape(TEAM_META_MARKER) + r"\s*\n(.*?)\n-->",
    re.DOTALL,
)


def embed_team_meta(text: str, meta: dict) -> str:
    """Append a machine-readable team-meta block to a body."""
    payload = json.dumps(meta, ensure_ascii=False, sort_keys=True)
    return f"{text}\n\n<!-- {TEAM_META_MARKER}\n{payload}\n-->\n"


def parse_team_meta(body: Optional[str]) -> Optional[dict]:
    """Recover the embedded team-meta dict from a body, or ``None``."""
    if not body:
        return None
    m = _META_RE.search(body)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Plan building (pure — no DB, deterministic)
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return slug or "team"


def _member_spec(spec: dict, role: str) -> dict:
    """Normalise a template member-spec, applying defaults."""
    name = str(spec.get("name") or role).strip() or role
    workspace = str(spec.get("workspace") or "scratch")
    if workspace not in _VALID_WORKSPACES:
        raise ValueError(
            f"invalid workspace {workspace!r} for {name!r}; "
            f"must be one of {sorted(_VALID_WORKSPACES)}"
        )
    toolsets = list(spec.get("toolsets") or [])
    capability_profile = spec.get("capability_profile") or None
    if capability_profile and not toolsets:
        # Fail at plan time: the dispatcher refuses capability_profile cards
        # without explicit non-empty toolsets, so creating such cards only
        # produces spawn failures until the card is auto-blocked.
        raise ValueError(
            f"member {name!r} sets capability_profile {capability_profile!r} "
            "but has no toolsets; capability cards require an explicit "
            "non-empty toolsets list"
        )
    return {
        "name": name,
        "category": str(spec.get("category") or role),
        "toolsets": toolsets,
        "readonly": bool(spec.get("readonly", False)),
        "workspace": workspace,
        "profile": spec.get("profile") or None,
        "capability_profile": capability_profile,
        "skills": list(spec.get("skills") or []),
    }


def _render_body(goal: str, team_name: str, node_view: dict,
                 approval_guards: list[str]) -> str:
    """Render the human prose + embedded meta for one card."""
    role = node_view["role"]
    toolsets = node_view["toolsets"]
    lines = [
        f"# {node_view['title']}",
        "",
        f"**Goal:** {goal}",
        f"**Team:** {team_name}",
        f"**Role:** {role}",
        f"**Category:** {node_view['category']}",
        f"**Capability profile:** {node_view.get('capability_profile') or '(none)'}",
        f"**Toolsets:** {', '.join(toolsets) if toolsets else '(profile default)'}",
        f"**Read-only:** {'yes' if node_view['readonly'] else 'no'}",
        f"**Workspace:** {node_view['workspace']}",
        "",
        "## Mailbox & Handoff",
    ]
    if role == "lead":
        lines += [
            "- You are the coordination lead for this team plan.",
            "- Members post progress and final handoffs as comments on THIS "
            "coordination card.",
            "- Coordinate via comments only; do not edit member task cards "
            "directly.",
        ]
    else:
        lines += [
            "- Work only on THIS task card. Do not mutate sibling task cards.",
            "- Post progress and your final handoff as a comment on the "
            "coordination (lead) card; see the roster comment there for ids.",
        ]
    lines += [
        "",
        "## Approval gates",
        "These actions require explicit human approval before a worker may "
        "perform them:",
    ]
    lines += [f"- {g}" for g in approval_guards]

    text = "\n".join(lines)
    meta = {
        "version": TEAM_META_VERSION,
        "team": team_name,
        "role": role,
        "name": node_view["name"],
        "category": node_view["category"],
        "capability_profile": node_view.get("capability_profile"),
        "toolsets": toolsets,
        "readonly": node_view["readonly"],
        "workspace": node_view["workspace"],
        "workspace_policy": {
            "kind": node_view["workspace"],
            "readonly": node_view["readonly"],
            "mutate": not bool(node_view["readonly"]),
        },
        "approval_required": list(approval_guards),
    }
    return embed_team_meta(text, meta)


def build_team_plan(goal: str, team_name: str, teams_cfg: dict) -> TeamPlan:
    """Build a deterministic team plan. Pure: performs no DB writes.

    Raises ``ValueError`` on an empty goal or an unknown team.
    """
    if not goal or not goal.strip():
        raise ValueError("goal is required")
    goal = goal.strip()
    template = resolve_team(team_name, teams_cfg)

    approval_guards = list(teams_cfg.get("approval_required")
                           or DEFAULT_APPROVAL_GUARDS)
    max_parallel = int(teams_cfg.get("max_parallel") or 0)
    slug = _slugify(team_name)
    plan = TeamPlan(
        goal=goal,
        team=team_name,
        slug=slug,
        tenant=f"{TEAM_TENANT_PREFIX}{slug}",
        max_parallel=max_parallel,
        approval_guards=approval_guards,
    )

    def _add_node(spec: dict, role: str, local_id: str,
                  parents_local: list[str], title: str) -> TeamNode:
        norm = _member_spec(spec, role)
        node_view = {
            "role": role, "name": norm["name"], "title": title,
            "category": norm["category"], "toolsets": norm["toolsets"],
            "readonly": norm["readonly"], "workspace": norm["workspace"],
            "capability_profile": norm["capability_profile"],
        }
        body = _render_body(goal, team_name, node_view, approval_guards)
        node = TeamNode(
            local_id=local_id, role=role, name=norm["name"], title=title,
            category=norm["category"], toolsets=norm["toolsets"],
            readonly=norm["readonly"], workspace=norm["workspace"],
            profile=norm["profile"],
            capability_profile=norm["capability_profile"],
            skills=norm["skills"],
            parents_local=parents_local, approval_guards=approval_guards,
            body=body,
        )
        plan.nodes.append(node)
        return node

    # Lead / coordination root.
    lead_spec = template.get("lead") or {"name": "lead",
                                         "category": "coordination"}
    lead = _add_node(
        lead_spec, "lead", "lead", [],
        f"[{team_name}] Coordinate: {goal}",
    )

    # Member tasks, each gated behind the lead.
    member_specs = list(template.get("members") or [])
    if not member_specs:
        raise ValueError(f"team {team_name!r} defines no members")
    member_local_ids: list[str] = []
    for i, spec in enumerate(member_specs, start=1):
        norm_name = _member_spec(spec, "member")["name"]
        local_id = f"member-{i}-{_slugify(norm_name)}"
        member_local_ids.append(local_id)
        _add_node(
            spec, "member", local_id, [lead.local_id],
            f"[{team_name}] {norm_name}: {goal}",
        )

    # Review / handoff task gated by every member task.
    review_spec = template.get("review")
    if review_spec:
        _add_node(
            review_spec, "reviewer", "review", list(member_local_ids),
            f"[{team_name}] Review & handoff: {goal}",
        )

    return plan


# ---------------------------------------------------------------------------
# Materialisation (DB writes)
# ---------------------------------------------------------------------------

def _registry_comment(plan: TeamPlan) -> str:
    """Build the roster/handoff registry posted on the lead card."""
    lines = [
        "Team roster & handoff registry",
        f"team={plan.team}  goal={plan.goal}",
        f"max_parallel={plan.max_parallel or 'unlimited'}",
        f"approval_required={', '.join(plan.approval_guards)}",
        "",
    ]
    for n in plan.nodes:
        lines.append(
            f"- {n.role}/{n.name} [{n.category}] -> {n.task_id} "
            f"(workspace={n.workspace}, readonly={n.readonly}, "
            f"capability_profile={n.capability_profile or 'none'})"
        )
    return "\n".join(lines)


def materialize_plan(conn, plan: TeamPlan, *, created_by: str = "user",
                     board: Optional[str] = None) -> TeamPlan:
    """Create the Kanban graph for ``plan``. Mutates nodes with real ids.

    Nodes are created in list order (lead, members, review), which is a
    valid topological order, so each node's parents already exist when it
    is created.
    """
    id_map: dict[str, str] = {}
    for node in plan.nodes:
        parents = [id_map[p] for p in node.parents_local]
        task_id = kb.create_task(
            conn,
            title=node.title,
            body=node.body,
            assignee=node.profile,
            created_by=created_by,
            workspace_kind=node.workspace,
            tenant=plan.tenant,
            parents=parents,
            skills=(node.skills or None),
            board=board,
        )
        node.task_id = task_id
        id_map[node.local_id] = task_id

    # Post the handoff registry on the lead card (best-effort mailbox seed).
    lead = next((n for n in plan.nodes if n.role == "lead"), None)
    if lead and lead.task_id:
        kb.add_comment(conn, lead.task_id, "team-planner",
                       _registry_comment(plan))
    return plan


# ---------------------------------------------------------------------------
# Ownership guard — a worker may only mutate its own card
# ---------------------------------------------------------------------------

def allowed_write_task_ids(plan: TeamPlan, actor_local_id: str) -> set[str]:
    """Task ids the given actor is permitted to *mutate*.

    Scoped to the actor's own card only. Coordination happens through
    append-only comments on the lead card, not by editing foreign cards —
    so sibling and lead task ids are intentionally excluded.
    """
    for node in plan.nodes:
        if node.local_id == actor_local_id and node.task_id:
            return {node.task_id}
    return set()


def can_mutate(plan: TeamPlan, actor_local_id: str, target_task_id: str) -> bool:
    """Whether ``actor_local_id`` may mutate ``target_task_id``."""
    return target_task_id in allowed_write_task_ids(plan, actor_local_id)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def effective_max_parallel(teams_cfg: dict, kanban_cfg: dict) -> Optional[int]:
    """Tightest of the team cap and the kanban per-profile cap.

    The existing dispatcher caps remain the source of truth for execution;
    this just reports the effective ceiling. ``None`` means unlimited.
    """
    caps: list[int] = []
    team_cap = teams_cfg.get("max_parallel")
    if team_cap:
        caps.append(int(team_cap))
    kanban_cap = (kanban_cfg or {}).get("max_in_progress_per_profile")
    if kanban_cap:
        caps.append(int(kanban_cap))
    return min(caps) if caps else None


def _team_of(task) -> Optional[str]:
    """Recover the team name for a task from its tenant marker or meta."""
    tenant = getattr(task, "tenant", None)
    meta = parse_team_meta(getattr(task, "body", None))
    if meta and meta.get("team"):
        return str(meta["team"])
    if tenant and tenant.startswith(TEAM_TENANT_PREFIX):
        return tenant[len(TEAM_TENANT_PREFIX):]
    return None


def team_status(conn, *, team: Optional[str] = None) -> dict:
    """Summarise team cards on the board, grouped by team.

    When ``team`` is given, only that team's cards are summarised (via an
    exact tenant match). Otherwise every ``team:*`` card is included.
    """
    if team:
        slug = _slugify(team)
        tasks = kb.list_tasks(conn, tenant=f"{TEAM_TENANT_PREFIX}{slug}")
    else:
        tasks = [
            t for t in kb.list_tasks(conn)
            if (getattr(t, "tenant", None) or "").startswith(TEAM_TENANT_PREFIX)
        ]

    teams: dict[str, dict] = {}
    for t in tasks:
        name = _team_of(t)
        if team and name != team:
            # tenant slug may differ from the display name; fall back to the
            # requested name so a single-team query always reports under it.
            name = team
        if not name:
            continue
        bucket = teams.setdefault(name, {
            "counts": {}, "blockers": [], "approval_guards": set(),
            "tasks": [], "total": 0,
        })
        bucket["total"] += 1
        bucket["counts"][t.status] = bucket["counts"].get(t.status, 0) + 1
        if t.status == "blocked":
            bucket["blockers"].append(t.id)
        meta = parse_team_meta(getattr(t, "body", None)) or {}
        for g in meta.get("approval_required") or []:
            bucket["approval_guards"].add(g)
        bucket["tasks"].append({
            "id": t.id, "title": t.title, "status": t.status,
            "role": meta.get("role"), "category": meta.get("category"),
            "capability_profile": meta.get("capability_profile"),
            "assignee": getattr(t, "assignee", None),
        })

    # Normalise sets to sorted lists for stable JSON.
    for bucket in teams.values():
        bucket["approval_guards"] = sorted(bucket["approval_guards"])

    totals: dict[str, int] = {}
    for bucket in teams.values():
        for status, n in bucket["counts"].items():
            totals[status] = totals.get(status, 0) + n

    return {"teams": teams, "totals": totals}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``team`` subcommand tree. Returns the top-level parser."""
    team_parser = parent_subparsers.add_parser(
        "team",
        help="Plan and track template-based teams over the Kanban board",
        description=(
            "Deterministic, template-based team planning on top of the "
            "existing Kanban. `team plan` materialises a lead -> members -> "
            "review dependency graph; `team status` summarises progress. "
            "No daemon, no network, no inbound remote control."
        ),
    )
    team_parser.add_argument(
        "--board", default=None, metavar="<slug>",
        help="Board slug to operate on (defaults to the current board).",
    )
    sub = team_parser.add_subparsers(dest="team_action")

    # --- plan ---
    p_plan = sub.add_parser(
        "plan", help="Plan a team graph for a goal (use --dry-run to preview)",
    )
    p_plan.add_argument("goal", nargs="+", help="The team goal (free text)")
    p_plan.add_argument("--team", default="coding",
                        help="Team template name (default: coding)")
    p_plan.add_argument("--dry-run", action="store_true",
                        help="Print the planned graph without creating any tasks")
    p_plan.add_argument("--created-by", default="user",
                        help="Author recorded on created tasks (default: user)")
    p_plan.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- status ---
    p_status = sub.add_parser(
        "status", help="Summarise team cards and progress",
    )
    p_status.add_argument("--team", default=None,
                          help="Limit to a single team (default: all teams)")
    p_status.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- list ---
    p_list = sub.add_parser("list", help="List available team templates")
    p_list.add_argument("--json", action="store_true", help="Emit JSON output")

    team_parser.set_defaults(_team_parser=team_parser)
    return team_parser


def plan_to_dict(plan: TeamPlan) -> dict:
    """JSON-serialisable view of a plan (used by --json and --dry-run)."""
    return {
        "goal": plan.goal,
        "team": plan.team,
        "slug": plan.slug,
        "tenant": plan.tenant,
        "max_parallel": plan.max_parallel,
        "approval_guards": list(plan.approval_guards),
        "nodes": [
            {
                "local_id": n.local_id,
                "task_id": n.task_id,
                "role": n.role,
                "name": n.name,
                "title": n.title,
                "category": n.category,
                "toolsets": list(n.toolsets),
                "readonly": n.readonly,
                "workspace": n.workspace,
                "profile": n.profile,
                "capability_profile": n.capability_profile,
                "skills": list(n.skills),
                "parents_local": list(n.parents_local),
            }
            for n in plan.nodes
        ],
    }


def team_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes team …`` argparse dispatch."""
    action = getattr(args, "team_action", None)
    if not action:
        parser = getattr(args, "_team_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: hermes team <plan|status|list> [options]",
                  file=sys.stderr)
        return 0

    handlers = {
        "plan": _cmd_plan,
        "status": _cmd_status,
        "list": _cmd_list,
    }
    handler = handlers.get(action)
    if not handler:
        print(f"team: unknown action {action!r}", file=sys.stderr)
        return 2
    try:
        return int(handler(args) or 0)
    except (ValueError, RuntimeError) as exc:
        print(f"team: {exc}", file=sys.stderr)
        return 1


def _cmd_plan(args: argparse.Namespace) -> int:
    goal_parts = getattr(args, "goal", None) or []
    goal = " ".join(goal_parts) if isinstance(goal_parts, list) else str(goal_parts)
    team_name = getattr(args, "team", "coding")
    dry_run = bool(getattr(args, "dry_run", False))
    use_json = bool(getattr(args, "json", False))
    created_by = getattr(args, "created_by", "user") or "user"
    board = getattr(args, "board", None)

    cfg = _config.load_config()
    teams_cfg = load_teams_config(cfg)
    plan = build_team_plan(goal, team_name, teams_cfg)

    try:
        from tools.delegation_audit import build_team_plan_event, record_audit_event

        record_audit_event(cfg, build_team_plan_event(plan))
    except Exception:
        pass

    if dry_run:
        # No DB access at all on the dry-run path — guarantees no task writes.
        if use_json:
            print(json.dumps({"dry_run": True, "plan": plan_to_dict(plan)},
                             indent=2, ensure_ascii=False))
        else:
            _print_plan(plan, dry_run=True)
        return 0

    kb.init_db(board=board)
    with kb.connect(board=board) as conn:
        materialize_plan(conn, plan, created_by=created_by, board=board)

    if use_json:
        print(json.dumps({"dry_run": False, "plan": plan_to_dict(plan)},
                         indent=2, ensure_ascii=False))
    else:
        _print_plan(plan, dry_run=False)
    return 0


def _print_plan(plan: TeamPlan, *, dry_run: bool) -> None:
    header = "Planned (dry-run, no tasks created)" if dry_run else "Created team plan"
    print(f"{header}: team={plan.team} goal={plan.goal!r}")
    print(f"  tenant={plan.tenant}  max_parallel="
          f"{plan.max_parallel or 'unlimited'}")
    print(f"  approval gates: {', '.join(plan.approval_guards)}")
    for n in plan.nodes:
        dep = (" <- " + ", ".join(n.parents_local)) if n.parents_local else ""
        ident = n.task_id or n.local_id
        print(f"  - [{n.role}] {n.name} [{n.category}] {ident}{dep}")


def _cmd_status(args: argparse.Namespace) -> int:
    team_name = getattr(args, "team", None)
    use_json = bool(getattr(args, "json", False))
    board = getattr(args, "board", None)

    teams_cfg = load_teams_config()
    cfg = _config.load_config()
    kanban_cfg = (cfg or {}).get("kanban") or {}
    cap = effective_max_parallel(teams_cfg, kanban_cfg)

    kb.init_db(board=board)
    with kb.connect(board=board) as conn:
        summary = team_status(conn, team=team_name)
    summary["effective_max_parallel"] = cap

    if use_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if not summary["teams"]:
        print("No team cards found.")
        print(f"effective max_parallel: {cap if cap is not None else 'unlimited'}")
        return 0
    print(f"effective max_parallel: {cap if cap is not None else 'unlimited'}")
    for name, bucket in sorted(summary["teams"].items()):
        counts = ", ".join(f"{k}={v}" for k, v in sorted(bucket["counts"].items()))
        print(f"\n[{name}] {bucket['total']} card(s): {counts}")
        if bucket["blockers"]:
            print(f"  blocked: {', '.join(bucket['blockers'])}")
        if bucket["approval_guards"]:
            print(f"  approval gates: {', '.join(bucket['approval_guards'])}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    use_json = bool(getattr(args, "json", False))
    teams_cfg = load_teams_config()
    templates = teams_cfg["templates"]
    if use_json:
        out = {
            name: {
                "description": tpl.get("description", ""),
                "members": [
                    (m.get("name") or "member")
                    for m in (tpl.get("members") or [])
                ],
            }
            for name, tpl in templates.items()
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    if not templates:
        print("No team templates configured.")
        return 0
    for name in sorted(templates):
        tpl = templates[name]
        members = ", ".join(
            (m.get("name") or "member") for m in (tpl.get("members") or [])
        )
        print(f"- {name}: {tpl.get('description', '')}")
        if members:
            print(f"    members: {members}")
    return 0
