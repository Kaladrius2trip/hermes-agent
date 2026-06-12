"""Regression tests for the 2026-06-11 fork-audit A6 misc fixes.

Covers:
- security-sweep-1: code_execution removed from the Discord private-context
  default safe toolsets (it defeated the gate's file/secret stripping).
- exec-misc-2: preflight pointer/commondir classification normalizes paths
  ('..' segments cannot produce a false PASS).
- exec-misc-4: a hung subprocess surfaces as a failed check, not a traceback.
- team-kanban-01: dispatcher capability metadata requires operator opt-in.
- team-kanban-03: capability_profile without toolsets fails at plan time.
"""

import subprocess

import pytest


def test_code_execution_not_in_private_context_safe_sets():
    from gateway.run import _DISCORD_PRIVATE_CONTEXT_DEFAULT_SAFE_TOOLSETS

    assert "code_execution" not in _DISCORD_PRIVATE_CONTEXT_DEFAULT_SAFE_TOOLSETS

    from hermes_cli.config import DEFAULT_CONFIG

    discord_cfg = DEFAULT_CONFIG["discord"]
    assert "code_execution" not in discord_cfg["private_context_safe_toolsets"]


class TestPreflightPathNormalization:
    def test_dotdot_pointer_into_root_path_fails(self):
        from pathlib import Path

        from scripts.claude_yolo_worktree_preflight import classify_pointer

        result = classify_pointer(
            "/home/claude-yolo/workspaces/x-live-git/../../../../root/.config/x/.git",
            Path("/home/claude-yolo/workspaces/x-live-git"),
            root_prefixes=["/root/"],
        )

        assert result.status == "FAIL"

    def test_absolute_commondir_is_resolved(self, tmp_path):
        from scripts.claude_yolo_worktree_preflight import resolve_commondir

        target = tmp_path / "real"
        target.mkdir()
        resolved = resolve_commondir(
            tmp_path / "gitdir", str(tmp_path / "sub" / ".." / "real")
        )

        assert resolved == target

    def test_runner_timeout_becomes_failed_result(self, monkeypatch):
        from scripts.claude_yolo_worktree_preflight import _default_runner

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["sleep"], timeout=60)

        monkeypatch.setattr(subprocess, "run", boom)

        result = _default_runner(["sleep", "999"])

        assert result.returncode != 0
        assert "timed out" in result.stderr


def test_dispatcher_capability_metadata_requires_opt_in(monkeypatch):
    from hermes_cli import config as hermes_config
    from hermes_cli import team
    from hermes_cli.kanban_db import Task, _resolve_task_capability_profile

    monkeypatch.setattr(hermes_config, "load_config", lambda: {"capabilities": {}})
    body = team.embed_team_meta("body", {
        "version": team.TEAM_META_VERSION,
        "team": "t",
        "role": "member",
        "category": "implementation",
        "capability_profile": "review",
        "toolsets": ["file"],
    })
    task = Task(
        id="t_optin", title="x", body=body, assignee="coder",
        status="ready", priority=0, created_by=None, created_at=0,
        started_at=None, completed_at=None, workspace_kind="worktree",
        workspace_path="/tmp/ws", claim_lock=None, claim_expires=None, tenant=None,
    )

    with pytest.raises(RuntimeError, match="not enabled"):
        _resolve_task_capability_profile(task)


def test_team_plan_rejects_capability_profile_without_toolsets():
    from hermes_cli.team import _member_spec

    with pytest.raises(ValueError, match="non-empty toolsets"):
        _member_spec({"capability_profile": "review", "toolsets": []}, role="reviewer")

    spec = _member_spec(
        {"capability_profile": "review", "toolsets": ["file"]}, role="reviewer"
    )
    assert spec["capability_profile"] == "review"
