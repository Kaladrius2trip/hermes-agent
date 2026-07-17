"""Restricted-user policy core for the user-owned scheduler (P1 handoff).

Pure fail-closed validation: a restricted scheduler user manages only
their own jobs and cannot escape their capability envelope through job
arguments (script execution, agent bypass, workdir, model/provider
override, toolset expansion, foreign context chaining, broadcast
delivery, recursive job creation). Run-time ACL re-resolution, delivery
and storage stay in scheduler wiring; this module only decides.

Ownership is a stable platform-scoped key derived from adapter-verified
identity, stored with the job at creation and never client-supplied.
"""
from __future__ import annotations

from typing import Iterable, Optional

_MANAGE_ACTIONS = frozenset({"update", "pause", "resume", "remove", "run"})
_ACTIONS = frozenset({"create", "list"}) | _MANAGE_ACTIONS
_ALLOWED_DELIVER = frozenset({"origin", "self_dm"})


def owner_key_for(platform: str, user_id: str) -> str:
    """Stable unforgeable job-owner key: platform-scoped verified identity."""
    p = str(platform or "").strip().lower()
    u = str(user_id or "").strip()
    if not p or not u:
        raise ValueError("scheduler owner key requires platform and user_id")
    return f"{p}:{u}"


def validate_restricted_scheduler_action(
    *,
    action: str,
    requester_key: str,
    job_owner_key: Optional[str] = None,
    requester_is_cron_run: bool = False,
    script: Optional[str] = None,
    no_agent: bool = False,
    workdir: Optional[str] = None,
    model_override: bool = False,
    toolsets_requested: Iterable[str] = (),
    context_owner_keys: Iterable[str] = (),
    deliver: Optional[str] = None,
) -> tuple[bool, str]:
    """Return (allowed, stable_reason_code) for a restricted scheduler user."""
    act = str(action or "").strip().lower()
    if act not in _ACTIONS:
        return False, "unknown_scheduler_action"
    requester = str(requester_key or "").strip()
    if not requester:
        return False, "requester_unknown"

    if act in _MANAGE_ACTIONS:
        owner = str(job_owner_key or "").strip()
        if not owner:
            return False, "job_owner_unknown"
        if owner != requester:
            return False, "foreign_job_denied"

    if act in {"create", "update"}:
        if requester_is_cron_run:
            return False, "recursive_creation_denied"
        if script:
            return False, "script_denied"
        if no_agent:
            return False, "no_agent_denied"
        if workdir:
            return False, "workdir_denied"
        if model_override:
            return False, "model_override_denied"
        if tuple(t for t in (toolsets_requested or ()) if str(t).strip()):
            return False, "toolset_expansion_denied"
        for ctx_owner in context_owner_keys or ():
            if str(ctx_owner or "").strip() != requester:
                return False, "foreign_context_denied"
        if deliver is not None and str(deliver).strip().lower() not in _ALLOWED_DELIVER:
            return False, "deliver_target_denied"

    return True, "allowed"
