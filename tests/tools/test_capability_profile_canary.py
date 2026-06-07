"""P13.6 profile canary recipe tests.

The canary is a local-only smoke path: it must exercise the capability profile
runtime/audit surfaces in an isolated HERMES_HOME without copying secrets or
claiming synthetic success when a blocker is found.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_profile_canary_runs_in_isolated_home_and_records_redacted_evidence(tmp_path, monkeypatch):
    from scripts.capability_profile_canary import REQUIRED_CHECKS, run_canary

    ambient_home = tmp_path / "ambient-live-home"
    canary_home = tmp_path / "canary-home"
    monkeypatch.setenv("HERMES_HOME", str(ambient_home))
    monkeypatch.setenv("CANARY_MCP_TOKEN", "literal-canary-secret")

    result = run_canary(home=canary_home, cleanup=False)

    assert result["status"] == "passed"
    assert Path(result["home"]) == canary_home
    assert result["home_env"] == str(canary_home)
    assert result["cleanup"]["home_removed"] is False
    assert "rm -rf" in result["cleanup"]["note"]
    assert not ambient_home.exists()
    assert not (canary_home / ".env").exists()
    assert not (canary_home / "auth.json").exists()
    assert not (canary_home / "sessions").exists()
    assert not (canary_home / "memories").exists()
    assert not (canary_home / "cron").exists()

    checks = {check["name"]: check for check in result["checks"]}
    assert set(REQUIRED_CHECKS) <= set(checks)
    for check in checks.values():
        assert check["status"] == "passed"
        assert check["evidence_id"].startswith("canary-")

    audit_path = Path(result["audit_path"])
    assert audit_path == canary_home / "logs" / "delegation-audit.jsonl"
    events = _jsonl(audit_path)
    event_types = {event["event_type"] for event in events}
    assert {"delegation_run", "team_plan", "mcp_env_decision"} <= event_types
    assert all(event.get("evidence_id", "").startswith("canary-") for event in events)

    delegation_event = next(event for event in events if event["event_type"] == "delegation_run")
    assert delegation_event["profile_resolved"] == "canary-implementation"
    assert delegation_event["category_resolved"] == "implementation"
    assert delegation_event["approval_gates"] == ["push", "merge"]
    assert delegation_event["fallback_metadata"] == {
        "enabled": True,
        "count": 1,
        "providers": ["canary-local"],
        "models": ["canary/fallback"],
        "profiles": ["canary-review"],
    }

    team_event = next(event for event in events if event["event_type"] == "team_plan")
    assert team_event["team"] == "coding"
    assert team_event["approval_guards"]
    assert team_event["nodes"]

    mcp_event = next(event for event in events if event["event_type"] == "mcp_env_decision")
    assert mcp_event["env_allowed_names"] == ["CANARY_MCP_TOKEN"]
    assert mcp_event["values_redacted"] is True

    audit_blob = audit_path.read_text(encoding="utf-8")
    for forbidden in (
        "literal-canary-secret",
        "api_key=canary-secret",
        "canary-secret",
        "profile canary smoke",
        "SUPERSECRET",
    ):
        assert forbidden not in audit_blob
    assert "https://canary.invalid/v1?[REDACTED]" in audit_blob


def test_profile_canary_cli_script_runs_from_repo_root(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    canary_home = tmp_path / "cli-canary-home"

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/capability_profile_canary.py",
            "--home",
            str(canary_home),
            "--cleanup",
            "--json",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "passed"
    assert payload["cleanup"]["home_removed"] is True


def test_profile_canary_failure_does_not_cleanup_rejected_home(tmp_path, capsys):
    from scripts.capability_profile_canary import main

    rejected_home = tmp_path / "rejected-home"
    rejected_home.mkdir()
    credential_file = rejected_home / ".env"
    credential_file.write_text("CANARY_SHOULD_SURVIVE=1\n", encoding="utf-8")

    code = main(["--home", str(rejected_home), "--cleanup", "--json"])

    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["blocker"] == f"refusing canary home with credential files: {rejected_home.resolve()}"
    assert credential_file.read_text(encoding="utf-8") == "CANARY_SHOULD_SURVIVE=1\n"


def test_profile_canary_failure_output_names_exact_blocker(capsys):
    from scripts.capability_profile_canary import main

    live_home = Path.home() / ".hermes"
    code = main(["--home", str(live_home), "--json"])

    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["blocker"] == f"refusing live HERMES_HOME: {live_home}"
    assert payload["checks"] == []
    assert "passed" not in payload.get("message", "").lower()
