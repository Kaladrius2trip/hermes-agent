"""Tests for the privacy-safe local delegation audit bundle (P10 Phase 7).

The audit layer is a *local-only* observability surface: it records safe,
redacted event dicts as JSONL so failed team/delegation runs can be debugged
without ever persisting prompts, goals, credentials, or env values.

Invariants exercised here:

* disabled by default — no file is created and the recorder is a no-op;
* when enabled, a delegation-run event is written as one JSON line carrying
  category / recipe / fallback / provider / model / toolsets / timeouts /
  result metadata;
* redaction is recursive and replaces secret-like values (api keys, tokens,
  passwords, env values, URL query strings) with ``[REDACTED]``;
* an MCP env-stripping decision records names / counts / decision, never the
  values;
* a team-plan/handoff event carries role / category / toolsets / approval
  guards but never the raw goal or card body.
"""

import json
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from tools import delegation_audit as audit
from tools.delegation_categories import resolve_delegation_category


def _mock_parent_agent():
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "parent-secret-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.enabled_toolsets = ["file", "terminal", "web"]
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.session_id = "parent-session"
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


# ---------------------------------------------------------------------------
# Disabled-by-default behaviour
# ---------------------------------------------------------------------------

def test_audit_disabled_by_default():
    assert audit.is_audit_enabled({}) is False
    assert audit.is_audit_enabled({"delegation": {}}) is False
    assert audit.is_audit_enabled({"delegation": {"audit": {}}}) is False


def test_record_is_noop_when_disabled(tmp_path):
    cfg = {"delegation": {"audit": {"enabled": False, "dir": str(tmp_path)}}}
    event = audit.build_delegation_run_event(
        session_id="sess-disabled", status="completed"
    )
    result = audit.record_audit_event(cfg, event)
    assert result is None
    # No audit bundle may be created while auditing is disabled.
    assert not audit.audit_log_path(cfg).exists()


def test_audit_config_accepts_full_or_delegation_section(tmp_path):
    """delegate_task already works with the delegation subsection only."""
    delegation_cfg = {"audit": {"enabled": True, "dir": str(tmp_path)}}
    assert audit.audit_config({"delegation": delegation_cfg}) == {
        "enabled": True,
        "dir": str(tmp_path),
    }
    assert audit.audit_config(delegation_cfg) == {
        "enabled": True,
        "dir": str(tmp_path),
    }


# ---------------------------------------------------------------------------
# Enabled: JSONL event shape
# ---------------------------------------------------------------------------

def test_enabled_writes_jsonl_with_expected_shape(tmp_path):
    cfg = {"delegation": {"audit": {"enabled": True, "dir": str(tmp_path)}}}

    deleg = {
        "categories": {
            "deep": {
                "provider": "openrouter",
                "model": "x/y-pro",
                "recipe": "focused-executor",
                "toolsets": ["web", "file"],
                "max_iterations": 30,
                "child_timeout_seconds": 120,
                "fallback_chain": [{"provider": "anthropic", "model": "claude"}],
            }
        }
    }
    resolved = resolve_delegation_category(deleg, category="deep")

    event = audit.build_delegation_run_event(
        session_id="sess-1",
        category_requested="deep",
        resolved_category=resolved,
        base_url="https://host/v1?api_key=zzz",
        status="completed",
        result_summary="did the thing",
        task_index=0,
        task_count=1,
    )

    # Builder fills metadata from the resolved category.
    assert event["event_type"] == "delegation_run"
    assert event["category_requested"] == "deep"
    assert event["category_resolved"] == "deep"
    assert event["recipe"] == "focused-executor"
    assert event["provider"] == "openrouter"
    assert event["model"] == "x/y-pro"
    assert event["toolsets"] == ["web", "file"]
    assert event["child_timeout_seconds"] == 120
    assert event["max_iterations"] == 30
    assert event["fallback_metadata"]["enabled"] is True
    assert event["fallback_metadata"]["count"] == 1
    assert event["status"] == "completed"
    assert event["task_index"] == 0
    assert event["task_count"] == 1

    path = audit.record_audit_event(cfg, event)
    assert path is not None
    assert path.exists()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written["event_type"] == "delegation_run"
    assert written["session_id"] == "sess-1"
    # base_url query string is redacted on write.
    assert written["base_url"] == "https://host/v1?[REDACTED]"
    assert "zzz" not in lines[0]
    # A timestamp is stamped at write time when absent.
    assert isinstance(written["timestamp"], (int, float))


def test_build_delegation_run_event_tolerates_non_mapping_category():
    event = audit.build_delegation_run_event(
        session_id="sess-string-category",
        resolved_category=cast(Any, "research"),
        status="completed",
    )

    assert event["category_resolved"] == ""
    assert event["fallback_metadata"] == {"enabled": False, "count": 0}


def test_delegate_task_records_runtime_audit_event_without_prompt_or_credentials(tmp_path):
    """Enabled delegation auditing records route metadata, not prompts/secrets."""
    from tools.delegate_tool import delegate_task

    cfg = {
        "audit": {"enabled": True, "dir": str(tmp_path)},
        "max_iterations": 45,
        "categories": {
            "quick": {
                "provider": "local-lmstudio",
                "model": "qwen/qwen3.6-35b-a3b",
                "recipe": "focused-executor",
                "toolsets": ["file", "terminal"],
                "toolsets_mode": "intersect",
                "max_iterations": 20,
                "child_timeout_seconds": 300,
                "fallback_chain": [
                    {"provider": "openrouter", "model": "google/gemini-3-flash"},
                ],
            },
        },
    }

    def fake_creds(runtime_cfg, _parent):
        return {
            "model": runtime_cfg.get("model"),
            "provider": runtime_cfg.get("provider"),
            "base_url": "https://runtime.example.test/v1?api_key=secret-query",
            "api_key": "test-runtime-key",
            "api_mode": "chat_completions",
        }

    parent = _mock_parent_agent()
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=fake_creds),
        patch("run_agent.AIAgent") as MockAgent,
    ):
        child = MagicMock()
        child.model = "qwen/qwen3.6-35b-a3b"
        child.session_id = "child-session"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        child.run_conversation.return_value = {
            "final_response": "safe summary",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }
        MockAgent.return_value = child

        result = json.loads(
            delegate_task(
                goal="SECRET_GOAL_DO_NOT_LOG",
                context="SECRET_CONTEXT_DO_NOT_LOG",
                category="quick",
                parent_agent=parent,
            )
        )

    assert result["results"][0]["status"] == "completed"
    written = json.loads(audit.audit_log_path(cfg).read_text(encoding="utf-8").splitlines()[0])
    assert written["event_type"] == "delegation_run"
    assert written["parent_session_id"] == "parent-session"
    assert written["session_id"] == "child-session"
    assert written["category_requested"] == "quick"
    assert written["category_resolved"] == "quick"
    assert written["recipe"] == "focused-executor"
    assert written["provider"] == "local-lmstudio"
    assert written["model"] == "qwen/qwen3.6-35b-a3b"
    assert written["toolsets"] == ["file", "terminal"]
    assert written["child_timeout_seconds"] == 300
    assert written["max_iterations"] == 20
    assert written["fallback_metadata"]["enabled"] is True
    assert written["status"] == "completed"
    assert written["result_summary"] == "safe summary"

    blob = json.dumps(written, ensure_ascii=False)
    for forbidden in (
        "SECRET_GOAL_DO_NOT_LOG",
        "SECRET_CONTEXT_DO_NOT_LOG",
        "test-runtime-key",
        "parent-secret-key",
        "secret-query",
    ):
        assert forbidden not in blob


def test_appends_one_line_per_event(tmp_path):
    cfg = {"delegation": {"audit": {"enabled": True, "dir": str(tmp_path)}}}
    for i in range(3):
        audit.record_audit_event(
            cfg,
            audit.build_delegation_run_event(session_id=f"s{i}", status="completed"),
        )
    path = audit.audit_log_path(cfg)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


# ---------------------------------------------------------------------------
# Recursive redaction
# ---------------------------------------------------------------------------

def test_redaction_removes_secrets_recursively():
    raw = {
        "provider": "openrouter",
        "api_key": "sk-sec...6789",
        "password": "hunter2pass",
        "token": "tok-xyz-value",
        "headers": {"Authorization": "Bearer abc.def.ghijklmnop"},
        "base_url": "https://host/v1?api_key=topsecretvalue&x=1",
        "userinfo_url": "https://user:pass@host/v1",
        "env": {"OPENAI_API_KEY": "sk-liv...e123", "SAFE_VAR": "ok"},
        # A counter named "tokens" must NOT be mistaken for a secret token.
        "tokens": {"input": 10, "output": 20},
        "toolsets": ["web", "file"],
        "nested": [{"client_secret": "cs-abcdef123456"}],
    }
    out = audit.redact_audit_value(raw)

    assert out["api_key"] == "[REDACTED]"
    assert out["password"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["env"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert out["env"]["SAFE_VAR"] == "ok"
    assert out["nested"][0]["client_secret"] == "[REDACTED]"
    # Non-secret values pass through untouched.
    assert out["provider"] == "openrouter"
    assert out["toolsets"] == ["web", "file"]
    assert out["tokens"] == {"input": 10, "output": 20}
    # URL query strings are stripped to [REDACTED].
    assert out["base_url"] == "https://host/v1?[REDACTED]"
    assert out["userinfo_url"] == "https://[REDACTED]@host/v1"

    blob = json.dumps(out)
    for secret in (
        "sk-sec...6789",
        "hunter2pass",
        "tok-xyz-value",
        "topsecretvalue",
        "user:pass",
        "sk-liv...e123",
        "cs-abcdef123456",
    ):
        assert secret not in blob


def test_redaction_catches_bare_token_prefix_in_strings():
    out = audit.redact_audit_value({"note": "leaked sk-abc...mnop here"})
    assert "sk-abc...mnop" not in out["note"]
    assert "[REDACTED]" in out["note"]


def test_result_summary_and_error_are_bounded():
    event = audit.build_delegation_run_event(
        session_id="sess-long",
        status="failed",
        result_summary="s" * 1500,
        error="e" * 1500,
    )
    assert len(event["result_summary"]) <= audit.MAX_AUDIT_TEXT_CHARS + 1
    assert len(event["error"]) <= audit.MAX_AUDIT_TEXT_CHARS + 1
    assert event["result_summary"].endswith("…")
    assert event["error"].endswith("…")


# ---------------------------------------------------------------------------
# MCP env-stripping decision event
# ---------------------------------------------------------------------------

def test_mcp_env_decision_records_names_not_values():
    event = audit.build_mcp_env_decision_event(
        allowed_names=["OPENAI_API_KEY", "GITHUB_TOKEN"],
        stripped_count=5,
        decision="user-source-allowed",
        skill="my-skill",
        source="user",
        session_id="sess-mcp",
    )
    assert event["event_type"] == "mcp_env_decision"
    # Names are recorded (sorted) and survive redaction — they are not values.
    assert event["env_allowed_names"] == ["GITHUB_TOKEN", "OPENAI_API_KEY"]
    assert event["env_stripped_count"] == 5
    assert event["decision"] == "user-source-allowed"
    assert event["values_redacted"] is True

    safe = audit.redact_audit_value(event)
    assert safe["env_allowed_names"] == ["GITHUB_TOKEN", "OPENAI_API_KEY"]
    assert safe["env_stripped_count"] == 5


def test_skill_mcp_registration_records_env_decision_without_values(tmp_path, monkeypatch):
    from tools import mcp_tool
    import hermes_cli.config as hermes_config

    cfg = {
        "skills": {"mcp": {"enabled": True}},
        "delegation": {"audit": {"enabled": True, "dir": str(tmp_path)}},
    }
    monkeypatch.setattr(hermes_config, "load_config", lambda: cfg)
    monkeypatch.setattr(
        mcp_tool.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "XDG_CACHE_HOME": "/tmp/cache",
            "SKILL_MCP_TOKEN": "literal-secret-value",
            "UNRELATED_SECRET": "must-not-pass",
        },
    )
    monkeypatch.setenv("SKILL_MCP_TOKEN", "literal-secret-value")
    monkeypatch.setattr(mcp_tool, "register_mcp_servers", lambda servers: list(servers))

    registered = mcp_tool.register_skill_mcp_servers(
        "github-triage",
        {
            "servers": {
                "github-readonly": {
                    "command": "gh",
                    "env_allowlist": ["SKILL_MCP_TOKEN"],
                }
            }
        },
        skill_source="user",
    )

    assert registered == ["skill:github_triage:github_readonly"]
    written = json.loads(audit.audit_log_path(cfg).read_text(encoding="utf-8").splitlines()[0])
    assert written["event_type"] == "mcp_env_decision"
    assert written["skill"] == "github-triage"
    assert written["source"] == "user"
    assert written["decision"] == "registered"
    assert written["env_allowed_names"] == ["SKILL_MCP_TOKEN"]
    assert written["env_stripped_count"] == 1
    assert written["values_redacted"] is True
    assert "literal-secret-value" not in json.dumps(written, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Team-plan / handoff metadata event
# ---------------------------------------------------------------------------

def test_team_plan_event_excludes_goal_and_body():
    team = pytest.importorskip("hermes_cli.team")
    cfg = team.load_teams_config({})
    secret_goal = "ship dark mode SUPERSECRETGOAL"
    plan = team.build_team_plan(secret_goal, "coding", cfg)

    event = audit.build_team_plan_event(plan, session_id="sess-team")
    assert event["event_type"] == "team_plan"
    assert event["team"] == "coding"
    assert event["approval_guards"]  # non-empty

    roles = {node["role"] for node in event["nodes"]}
    assert {"lead", "member", "reviewer"} <= roles

    node = event["nodes"][0]
    for key in ("role", "name", "category", "toolsets", "readonly"):
        assert key in node
    # The rendered card body (which embeds the goal) must never be included.
    assert "body" not in node

    blob = json.dumps(event)
    assert secret_goal not in blob


def test_team_plan_command_records_audit_event_without_goal(tmp_path, monkeypatch, capsys):
    team = pytest.importorskip("hermes_cli.team")

    cfg = {"delegation": {"audit": {"enabled": True, "dir": str(tmp_path)}}}
    monkeypatch.setattr(team._config, "load_config", lambda: cfg)
    args = SimpleNamespace(
        team_action="plan",
        goal=["SUPERSECRETTEAMGOAL"],
        team="coding",
        dry_run=True,
        json=True,
        created_by="user",
        board=None,
    )

    assert team.team_command(args) == 0
    capsys.readouterr()
    written = json.loads(audit.audit_log_path(cfg).read_text(encoding="utf-8").splitlines()[0])
    assert written["event_type"] == "team_plan"
    assert written["team"] == "coding"
    assert written["nodes"]
    assert "body" not in written["nodes"][0]
    assert "SUPERSECRETTEAMGOAL" not in json.dumps(written, ensure_ascii=False)
