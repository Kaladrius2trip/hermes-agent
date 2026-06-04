"""Tests for the team-mode MVP (hermes_cli.team).

Team mode is a deterministic, template-based planner that materialises a
small Kanban dependency graph (lead -> members -> review) over the existing
``hermes_cli.kanban_db`` layer. No daemon, no network, no schema migration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import team


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors kanban tests)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Config / templates
# ---------------------------------------------------------------------------

def test_load_teams_config_exposes_builtin_templates():
    cfg = team.load_teams_config({})
    assert "coding" in cfg["templates"]
    assert "research" in cfg["templates"]
    # Approval guards default to the four sensitive actions.
    assert set(cfg["approval_required"]) == {
        "push", "merge", "publish", "send_message"
    }


def test_builtin_templates_use_canonical_toolset_names():
    cfg = team.load_teams_config({})
    known_toolsets = {"file", "terminal", "web"}
    for template in cfg["templates"].values():
        specs = [template.get("lead") or {}, template.get("review") or {}]
        specs.extend(template.get("members") or [])
        for spec in specs:
            assert set(spec.get("toolsets") or []) <= known_toolsets


def test_user_templates_override_builtins():
    user_cfg = {
        "teams": {
            "templates": {
                "custom": {
                    "description": "x",
                    "lead": {"name": "lead", "category": "coordination",
                             "toolsets": [], "readonly": True,
                             "workspace": "scratch"},
                    "members": [
                        {"name": "w", "category": "impl", "toolsets": [],
                         "readonly": False, "workspace": "scratch"},
                    ],
                }
            }
        }
    }
    cfg = team.load_teams_config(user_cfg)
    assert "custom" in cfg["templates"]
    # Built-ins still present alongside the user template.
    assert "coding" in cfg["templates"]


# ---------------------------------------------------------------------------
# Plan building (pure, deterministic, no DB)
# ---------------------------------------------------------------------------

def test_build_plan_is_pure_and_deterministic():
    cfg = team.load_teams_config({})
    p1 = team.build_team_plan("ship dark mode", "coding", cfg)
    p2 = team.build_team_plan("ship dark mode", "coding", cfg)
    d1 = team.plan_to_dict(p1)
    d2 = team.plan_to_dict(p2)
    # No DB ids assigned by a pure build.
    assert all(n["task_id"] is None for n in d1["nodes"])
    # Deterministic structure.
    assert d1 == d2


def test_build_plan_has_lead_members_review_with_gating_edges():
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("build api", "coding", cfg)
    roles = [n.role for n in plan.nodes]
    assert roles[0] == "lead"
    assert "member" in roles
    assert roles[-1] == "reviewer"

    lead = next(n for n in plan.nodes if n.role == "lead")
    members = [n for n in plan.nodes if n.role == "member"]
    review = next(n for n in plan.nodes if n.role == "reviewer")

    # Lead is the root.
    assert lead.parents_local == []
    # Every member depends on the lead/coordination task.
    for m in members:
        assert lead.local_id in m.parents_local
    # Review is gated by every member task.
    assert sorted(review.parents_local) == sorted(m.local_id for m in members)
    # Category metadata is present on every node.
    assert all(n.category for n in plan.nodes)


def test_build_plan_unknown_team_raises():
    cfg = team.load_teams_config({})
    with pytest.raises(ValueError):
        team.build_team_plan("goal", "no-such-team", cfg)


def test_build_plan_requires_goal():
    cfg = team.load_teams_config({})
    with pytest.raises(ValueError):
        team.build_team_plan("   ", "coding", cfg)


# ---------------------------------------------------------------------------
# Generated bodies: mailbox / handoff contract + approval guards + meta
# ---------------------------------------------------------------------------

def test_generated_bodies_carry_handoff_contract_and_meta():
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("translate docs", "coding", cfg)
    member = next(n for n in plan.nodes if n.role == "member")

    assert "Mailbox" in member.body
    assert "Handoff" in member.body or "handoff" in member.body
    assert "coordination" in member.body.lower()

    meta = team.parse_team_meta(member.body)
    assert meta is not None
    assert meta["role"] == "member"
    assert meta["category"] == member.category
    assert meta["team"] == "coding"


def test_team_templates_assign_capability_profiles_into_metadata():
    cfg = team.load_teams_config({
        "teams": {
            "templates": {
                "profiled": {
                    "description": "profile bridge smoke",
                    "lead": {
                        "name": "lead",
                        "category": "coordination",
                        "toolsets": ["file"],
                        "readonly": True,
                        "workspace": "scratch",
                        "capability_profile": "quick",
                    },
                    "members": [
                        {
                            "name": "builder",
                            "category": "implementation",
                            "toolsets": ["terminal", "file"],
                            "readonly": False,
                            "workspace": "worktree",
                            "capability_profile": "deep",
                        },
                    ],
                    "review": {
                        "name": "reviewer",
                        "category": "review",
                        "toolsets": ["file"],
                        "readonly": True,
                        "workspace": "scratch",
                        "capability_profile": "review",
                    },
                }
            }
        }
    })
    plan = team.build_team_plan("bridge profiles", "profiled", cfg)
    builder = next(n for n in plan.nodes if n.name == "builder")

    assert builder.capability_profile == "deep"
    assert team.plan_to_dict(plan)["nodes"][1]["capability_profile"] == "deep"

    meta = team.parse_team_meta(builder.body)
    assert meta is not None
    assert meta["capability_profile"] == "deep"
    assert meta["category"] == "implementation"
    assert meta["readonly"] is False
    assert meta["workspace_policy"] == {
        "kind": "worktree", "readonly": False, "mutate": True,
    }
    assert "**Capability profile:** deep" in builder.body


def test_approval_guard_metadata_present_in_every_body():
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("ship feature", "coding", cfg)
    for node in plan.nodes:
        meta = team.parse_team_meta(node.body)
        assert meta is not None
        assert set(meta["approval_required"]) == {
            "push", "merge", "publish", "send_message"
        }
        # The guard list is also human-visible.
        assert "Approval" in node.body


# ---------------------------------------------------------------------------
# Dry-run must not write anything
# ---------------------------------------------------------------------------

def test_dry_run_creates_no_tasks(kanban_home, capsys):
    args = SimpleNamespace(
        team_action="plan", goal=["ship", "dark", "mode"], team="coding",
        dry_run=True, json=True, created_by="user", board=None,
    )
    rc = team.team_command(args)
    assert rc == 0
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Non-dry plan creates the graph with parent gating
# ---------------------------------------------------------------------------

def test_plan_creates_graph_with_parent_gating(kanban_home):
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("build widget", "coding", cfg)
    with kb.connect() as conn:
        team.materialize_plan(conn, plan, created_by="user")

    # Every node got a real task id.
    assert all(n.task_id for n in plan.nodes)

    with kb.connect() as conn:
        lead = next(n for n in plan.nodes if n.role == "lead")
        members = [n for n in plan.nodes if n.role == "member"]
        review = next(n for n in plan.nodes if n.role == "reviewer")

        lead_task = kb.get_task(conn, lead.task_id)
        # Root has no parents -> ready immediately.
        assert lead_task.status == "ready"
        assert kb.parent_ids(conn, lead.task_id) == []

        for m in members:
            mt = kb.get_task(conn, m.task_id)
            # Gated behind the (not-yet-done) lead task.
            assert mt.status == "todo"
            assert lead.task_id in kb.parent_ids(conn, m.task_id)

        rt = kb.get_task(conn, review.task_id)
        assert rt.status == "todo"
        assert sorted(kb.parent_ids(conn, review.task_id)) == sorted(
            m.task_id for m in members
        )


def test_materialize_tags_tenant_and_posts_roster_comment(kanban_home):
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("a goal", "coding", cfg)
    with kb.connect() as conn:
        team.materialize_plan(conn, plan, created_by="user")

    with kb.connect() as conn:
        lead = next(n for n in plan.nodes if n.role == "lead")
        lead_task = kb.get_task(conn, lead.task_id)
        assert lead_task.tenant == plan.tenant
        assert plan.tenant.startswith("team:")
        # A roster/handoff registry comment is posted on the lead card.
        comments = kb.list_comments(conn, lead.task_id)
        assert any("roster" in c.body.lower() for c in comments)
        # Registry references each member task id.
        joined = "\n".join(c.body for c in comments)
        for m in (n for n in plan.nodes if n.role == "member"):
            assert m.task_id in joined


# ---------------------------------------------------------------------------
# Worker cannot mutate foreign task IDs
# ---------------------------------------------------------------------------

def test_worker_cannot_mutate_foreign_task_ids(kanban_home):
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("isolate", "coding", cfg)
    with kb.connect() as conn:
        team.materialize_plan(conn, plan, created_by="user")

    members = [n for n in plan.nodes if n.role == "member"]
    assert len(members) >= 2
    actor = members[0]
    foreign = members[1]
    lead = next(n for n in plan.nodes if n.role == "lead")

    allowed = team.allowed_write_task_ids(plan, actor.local_id)
    assert actor.task_id in allowed
    # Sibling and lead cards are NOT writable by this member.
    assert foreign.task_id not in allowed
    assert lead.task_id not in allowed

    assert team.can_mutate(plan, actor.local_id, actor.task_id) is True
    assert team.can_mutate(plan, actor.local_id, foreign.task_id) is False


# ---------------------------------------------------------------------------
# Status summary
# ---------------------------------------------------------------------------

def test_team_status_summarizes_counts_and_gates(kanban_home):
    cfg = team.load_teams_config({})
    plan = team.build_team_plan("status goal", "coding", cfg)
    with kb.connect() as conn:
        team.materialize_plan(conn, plan, created_by="user")

    with kb.connect() as conn:
        summary = team.team_status(conn, team="coding")

    assert "coding" in summary["teams"]
    t = summary["teams"]["coding"]
    assert t["counts"]["ready"] == 1  # the lead
    assert t["counts"]["todo"] >= 2   # members + review
    assert t["total"] == len(plan.nodes)
    assert set(t["approval_guards"]) == {
        "push", "merge", "publish", "send_message"
    }


def test_team_status_reports_profile_category_and_role(kanban_home):
    cfg = team.load_teams_config({
        "teams": {
            "templates": {
                "profiled": {
                    "lead": {
                        "name": "lead",
                        "category": "coordination",
                        "toolsets": ["file"],
                        "readonly": True,
                        "workspace": "scratch",
                        "capability_profile": "quick",
                    },
                    "members": [
                        {
                            "name": "builder",
                            "category": "implementation",
                            "toolsets": ["terminal", "file"],
                            "readonly": False,
                            "workspace": "worktree",
                            "capability_profile": "deep",
                        },
                    ],
                    "review": {
                        "name": "reviewer",
                        "category": "review",
                        "toolsets": ["file"],
                        "readonly": True,
                        "workspace": "scratch",
                        "capability_profile": "review",
                    },
                }
            }
        }
    })
    plan = team.build_team_plan("status bridge", "profiled", cfg)
    with kb.connect() as conn:
        team.materialize_plan(conn, plan, created_by="user")
        summary = team.team_status(conn, team="profiled")

    tasks = summary["teams"]["profiled"]["tasks"]
    builder = next(t for t in tasks if t["role"] == "member")
    assert builder["capability_profile"] == "deep"
    assert builder["category"] == "implementation"
    assert builder["role"] == "member"


def test_effective_max_parallel_takes_tightest_cap():
    teams_cfg = {"max_parallel": 4}
    # Kanban per-profile cap tighter -> wins.
    assert team.effective_max_parallel(teams_cfg, {"max_in_progress_per_profile": 2}) == 2
    # Team cap tighter -> wins.
    assert team.effective_max_parallel({"max_parallel": 2}, {"max_in_progress_per_profile": 5}) == 2
    # Both unlimited -> None.
    assert team.effective_max_parallel({"max_parallel": 0}, {"max_in_progress_per_profile": None}) is None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_build_parser_parses_plan_and_status():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    team.build_parser(sub)

    ns = parser.parse_args(
        ["team", "plan", "ship", "it", "--team", "coding", "--dry-run"]
    )
    assert ns.team_action == "plan"
    assert ns.goal == ["ship", "it"]
    assert ns.team == "coding"
    assert ns.dry_run is True

    ns2 = parser.parse_args(["team", "status", "--team", "coding", "--json"])
    assert ns2.team_action == "status"
    assert ns2.team == "coding"
    assert ns2.json is True


def test_cli_plan_then_status_roundtrip(kanban_home, capsys):
    plan_args = SimpleNamespace(
        team_action="plan", goal=["round", "trip"], team="coding",
        dry_run=False, json=False, created_by="user", board=None,
    )
    assert team.team_command(plan_args) == 0

    status_args = SimpleNamespace(
        team_action="status", team="coding", json=True, board=None,
    )
    assert team.team_command(status_args) == 0
    out = capsys.readouterr().out
    assert "coding" in out


def test_startup_discovery_skips_builtin_team_command(monkeypatch):
    """`hermes team ...` is a built-in, so plugin discovery must stay skipped."""
    from hermes_cli import main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", "team", "list"])

    assert main_mod._plugin_cli_discovery_needed() is False


def test_main_dispatches_team_command(monkeypatch):
    """Top-level CLI wires `hermes team ...` to hermes_cli.team.team_command."""
    from hermes_cli import main as main_mod

    captured = {}

    def fake_cmd_team(args):
        captured["command"] = args.command
        captured["team_action"] = args.team_action
        return 0

    monkeypatch.setattr(main_mod, "cmd_team", fake_cmd_team, raising=False)
    monkeypatch.setattr(main_mod, "_plugin_cli_discovery_needed", lambda: False)
    monkeypatch.setattr(sys, "argv", ["hermes", "team", "list"])

    main_mod.main()

    assert captured == {"command": "team", "team_action": "list"}
