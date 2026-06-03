#!/usr/bin/env python3
"""P13.6 capability-profile canary.

Runs a local-only canary in an isolated Hermes home.  It exercises the pure
capability-profile resolver/prompt renderer, deterministic team planning,
fallback metadata, skill-scoped MCP env hardening, and redacted audit evidence.
It never copies live .env/auth material and never starts providers, MCP servers,
or gateways.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REQUIRED_CHECKS = (
    "profile_resolution",
    "prompt_render",
    "team_plan",
    "delegate_fallback",
    "mcp_env_hardening",
    "audit_redaction",
)

_SECRET_SENTINELS = (
    "literal-canary-secret",
    "api_key=canary-secret",
    "canary-secret",
    "profile canary smoke",
    "SUPERSECRET",
)


class CanaryFailure(RuntimeError):
    """A precise canary blocker."""

    def __init__(self, blocker: str, *, checks: list[dict[str, Any]] | None = None):
        super().__init__(blocker)
        self.blocker = blocker
        self.checks = list(checks or [])


def _resolve_home(home: str | Path | None) -> tuple[Path, bool]:
    if home is None:
        return Path(tempfile.mkdtemp(prefix="hermes-capability-canary.")).resolve(), True
    return Path(home).expanduser().resolve(), False


def _live_home() -> Path:
    return (Path.home() / ".hermes").resolve()


def _validate_canary_home(home: Path) -> None:
    if home == _live_home():
        raise CanaryFailure(f"refusing live HERMES_HOME: {home}")
    if home in _live_home().parents:
        raise CanaryFailure(f"refusing parent of live HERMES_HOME: {home}")
    if _live_home() in home.parents:
        raise CanaryFailure(f"refusing nested path inside live HERMES_HOME: {home}")
    if (home / ".env").exists() or (home / "auth.json").exists():
        raise CanaryFailure(f"refusing canary home with credential files: {home}")


def _base_config(home: Path) -> dict[str, Any]:
    return {
        "model": {"provider": "canary-local", "default": "canary/implementation"},
        "providers": {
            "canary-local": {
                "name": "Canary Local Provider",
                "base_url": "https://canary.invalid/v1",
                "api_mode": "chat_completions",
                "default_model": "canary/implementation",
            }
        },
        "skills": {"mcp": {"enabled": True}},
        "delegation": {
            "audit": {"enabled": True, "dir": str(home / "logs")},
            "categories": {
                "implementation": {
                    "provider": "canary-local",
                    "model": "canary/implementation",
                    "recipe": "deep-worker",
                    "toolsets": ["file", "terminal"],
                    "max_iterations": 3,
                    "child_timeout_seconds": 30,
                },
            },
        },
        "capabilities": {
            "default_profile": "canary-implementation",
            "profiles": {
                "canary-review": {
                    "extends": "review",
                    "provider": "canary-local",
                    "model": "canary/review",
                    "allowed_toolsets": ["file"],
                    "approval_gates": ["merge"],
                },
                "canary-implementation": {
                    "extends": "implementation",
                    "responsibility": "Run an isolated capability-profile canary with evidence-backed handoff.",
                    "category": "implementation",
                    "provider": "canary-local",
                    "model": "canary/implementation",
                    "allowed_toolsets": ["file", "terminal"],
                    "approval_gates": ["push", "merge"],
                    "budget": {"max_iterations": 3, "child_timeout_seconds": 30},
                    "verification_policy": {
                        "require_evidence": True,
                        "on_unverifiable": "fail",
                        "commands": ["capability canary local checks"],
                    },
                    "fallbacks": [
                        {
                            "profile": "canary-review",
                            "provider": "canary-local",
                            "model": "canary/fallback",
                            "allowed_toolsets": ["file"],
                        }
                    ],
                },
            },
        },
    }


def _write_isolated_config(home: Path, cfg: dict[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    # JSON is valid YAML and avoids importing yaml in the canary.
    (home / "config.yaml").write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")


def _evidence_id(name: str) -> str:
    return "canary-" + name.replace("_", "-")


def _passed(checks: list[dict[str, Any]], name: str, **details: Any) -> str:
    evidence_id = _evidence_id(name)
    checks.append({"name": name, "status": "passed", "evidence_id": evidence_id, **details})
    return evidence_id


def _runtime_audit_metadata(resolved: dict[str, Any]) -> dict[str, Any]:
    prompt_sections = resolved.get("prompt_sections") or {}
    budget = resolved.get("budget") or {}
    return {
        "category": resolved.get("category", ""),
        "profile": resolved.get("profile", ""),
        "recipe": prompt_sections.get("recipe", ""),
        "provider": resolved.get("provider", ""),
        "model": resolved.get("model", ""),
        "toolsets": list(resolved.get("toolsets") or []),
        "child_timeout_seconds": budget.get("child_timeout_seconds"),
        "max_iterations": budget.get("max_iterations"),
        "fallback_metadata": resolved.get("fallback_metadata") or {"enabled": False, "count": 0},
    }


def _assert_no_forbidden_text(blob: str, forbidden: Iterable[str], checks: list[dict[str, Any]]) -> None:
    for value in forbidden:
        if value and value in blob:
            raise CanaryFailure(f"audit redaction failed; found forbidden text: {value}", checks=checks)


def run_canary(
    *,
    home: str | Path | None = None,
    cleanup: bool | None = None,
) -> dict[str, Any]:
    """Run the local canary and return a structured result.

    ``home`` must be a non-live Hermes home.  When omitted, a temporary home is
    created and removed after the run.  When supplied, it is kept by default so
    operators can inspect the audit bundle unless ``cleanup=True`` is passed.
    """
    canary_home, generated_home = _resolve_home(home)
    do_cleanup = generated_home if cleanup is None else bool(cleanup)
    checks: list[dict[str, Any]] = []
    cfg: dict[str, Any] = {}
    audit_path = canary_home / "logs" / "delegation-audit.jsonl"
    old_home_env = os.environ.get("HERMES_HOME")
    old_canary_env = os.environ.get("CANARY_MCP_TOKEN")
    cleanup_allowed = generated_home

    try:
        os.environ["CANARY_MCP_TOKEN"] = "literal-canary-secret"
        _validate_canary_home(canary_home)
        existing_entries = list(canary_home.iterdir()) if canary_home.exists() else []
        cleanup_allowed = generated_home or not existing_entries
        os.environ["HERMES_HOME"] = str(canary_home)
        cfg = _base_config(canary_home)
        _write_isolated_config(canary_home, cfg)

        from hermes_cli import team
        from tools import mcp_tool
        from tools.capability_profiles import (
            render_capability_profile_prompt,
            resolve_capability_profile,
        )
        from tools.delegation_audit import (
            build_delegation_run_event,
            build_mcp_env_decision_event,
            build_team_plan_event,
            record_audit_event,
        )

        resolved = resolve_capability_profile(
            cfg,
            profile="canary-implementation",
            delegation_config=cfg["delegation"],
            parent_toolsets=["file", "terminal", "web"],
            requested_toolsets=["file", "terminal"],
        )
        if resolved.get("profile") != "canary-implementation":
            raise CanaryFailure("profile resolution returned wrong profile", checks=checks)
        if resolved.get("category") != "implementation":
            raise CanaryFailure("profile resolution returned wrong category", checks=checks)
        if not (resolved.get("fallback_metadata") or {}).get("enabled"):
            raise CanaryFailure("profile resolution did not expose fallback metadata", checks=checks)
        _passed(
            checks,
            "profile_resolution",
            profile=resolved.get("profile"),
            category=resolved.get("category"),
        )

        prompt = render_capability_profile_prompt(
            resolved,
            goal="profile canary smoke SUPERSECRET_GOAL",
            context="literal-canary-secret context",
        )
        if "Capability Profile: canary-implementation" not in prompt:
            raise CanaryFailure("prompt render missing capability profile header", checks=checks)
        _assert_no_forbidden_text(prompt, ("SUPERSECRET_GOAL", "literal-canary-secret"), checks)
        _passed(checks, "prompt_render", prompt_chars=len(prompt))

        teams_cfg = team.load_teams_config(cfg)
        plan = team.build_team_plan("profile canary smoke SUPERSECRET_TEAM_GOAL", "coding", teams_cfg)
        if not plan.nodes:
            raise CanaryFailure("team plan contains no nodes", checks=checks)
        team_event = build_team_plan_event(plan)
        team_event["evidence_id"] = _passed(checks, "team_plan", nodes=len(plan.nodes))
        record_audit_event(cfg, team_event)

        runtime_metadata = _runtime_audit_metadata(resolved)
        delegation_event = build_delegation_run_event(
            session_id="canary-child-session",
            parent_session_id="canary-parent-session",
            category_requested="implementation",
            profile_requested="canary-implementation",
            resolved_category=runtime_metadata,
            base_url="https://canary.invalid/v1?api_key=canary-secret&x=1",
            status="completed",
            result_summary="canary evidence ok",
        )
        delegation_event["approval_gates"] = list(resolved.get("approval_gates") or [])
        delegation_event["evidence_id"] = _passed(
            checks,
            "delegate_fallback",
            fallback_count=runtime_metadata["fallback_metadata"].get("count", 0),
        )
        record_audit_event(cfg, delegation_event)

        project_manifest = {
            "servers": {
                "blocked": {"command": "true", "env_allowlist": ["CANARY_MCP_TOKEN"]}
            }
        }
        try:
            mcp_tool.build_skill_mcp_servers(
                "canary-project-skill", project_manifest, skill_source="project"
            )
        except ValueError as exc:
            if "project skills cannot request MCP env_allowlist: CANARY_MCP_TOKEN" not in str(exc):
                raise CanaryFailure(f"unexpected MCP project-source blocker: {exc}", checks=checks) from exc
        else:
            raise CanaryFailure("project-source MCP env_allowlist was not blocked", checks=checks)

        user_manifest = {
            "servers": {
                "readonly": {"command": "true", "env_allowlist": ["CANARY_MCP_TOKEN"]}
            }
        }
        user_servers = mcp_tool.build_skill_mcp_servers(
            "canary-user-skill", user_manifest, skill_source="user"
        )
        allowed_names = sorted(
            name
            for server_cfg in user_servers.values()
            for name in (server_cfg.get("env") or {}).keys()
        )
        if allowed_names != ["CANARY_MCP_TOKEN"]:
            raise CanaryFailure("user-source MCP env allowlist did not resolve expected names", checks=checks)
        mcp_event = build_mcp_env_decision_event(
            allowed_names=allowed_names,
            stripped_count=0,
            decision="user-source-allowed",
            skill="canary-user-skill",
            source="user",
        )
        mcp_event["evidence_id"] = _passed(checks, "mcp_env_hardening", allowed_names=allowed_names)
        record_audit_event(cfg, mcp_event)

        if not audit_path.exists():
            raise CanaryFailure("audit bundle was not written", checks=checks)
        audit_blob = audit_path.read_text(encoding="utf-8")
        _assert_no_forbidden_text(audit_blob, _SECRET_SENTINELS, checks)
        if "https://canary.invalid/v1?[REDACTED]" not in audit_blob:
            raise CanaryFailure("audit bundle did not redact URL query string", checks=checks)
        _passed(checks, "audit_redaction", audit_bytes=len(audit_blob.encode("utf-8")))

        if set(REQUIRED_CHECKS) - {check["name"] for check in checks}:
            missing = sorted(set(REQUIRED_CHECKS) - {check["name"] for check in checks})
            raise CanaryFailure("missing canary checks: " + ", ".join(missing), checks=checks)

        cleanup_info = _cleanup_home(canary_home, do_cleanup and cleanup_allowed)
        return {
            "status": "passed",
            "home": str(canary_home),
            "home_env": os.environ.get("HERMES_HOME"),
            "audit_path": str(audit_path),
            "checks": checks,
            "cleanup": cleanup_info,
        }
    except CanaryFailure:
        if do_cleanup and cleanup_allowed:
            _cleanup_home(canary_home, True)
        raise
    except Exception as exc:
        if do_cleanup and cleanup_allowed:
            _cleanup_home(canary_home, True)
        raise CanaryFailure(f"unexpected canary failure: {exc}", checks=checks) from exc
    finally:
        if old_canary_env is None:
            os.environ.pop("CANARY_MCP_TOKEN", None)
        else:
            os.environ["CANARY_MCP_TOKEN"] = old_canary_env
        if old_home_env is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home_env


def _cleanup_home(home: Path, cleanup: bool) -> dict[str, Any]:
    if cleanup:
        shutil.rmtree(home, ignore_errors=True)
        return {
            "home_removed": True,
            "note": f"temporary HERMES_HOME removed: {home}",
        }
    return {
        "home_removed": False,
        "note": f"temporary HERMES_HOME kept for inspection; remove with: rm -rf {home}",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run P13.6 capability profile canary")
    parser.add_argument("--home", help="isolated HERMES_HOME to use; defaults to mkdtemp")
    parser.add_argument("--cleanup", action="store_true", help="remove --home after the run")
    parser.add_argument("--keep-home", action="store_true", help="keep generated temp home for inspection")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cleanup: bool | None
    if args.cleanup and args.keep_home:
        print("capability canary: --cleanup and --keep-home are mutually exclusive", file=sys.stderr)
        return 2
    if args.cleanup:
        cleanup = True
    elif args.keep_home:
        cleanup = False
    else:
        cleanup = None

    try:
        result = run_canary(home=args.home, cleanup=cleanup)
    except CanaryFailure as exc:
        payload = {
            "status": "failed",
            "blocker": exc.blocker,
            "checks": exc.checks,
            "message": "capability profile canary failed; see blocker",
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"FAILED: {exc.blocker}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"PASSED: audit_path={result['audit_path']}")
        print(result["cleanup"]["note"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
