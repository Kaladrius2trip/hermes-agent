"""P1 user-owned scheduler: restricted-user policy core.

Pure fail-closed validation from the office handoff: restricted users
manage only their own jobs and cannot escape their capability envelope
through job arguments. Run-time ACL re-resolution and delivery stay in
scheduler wiring.
"""
from __future__ import annotations

import pytest

from gateway.scheduler_policy import (
    owner_key_for,
    validate_restricted_scheduler_action,
)


def _validate(**kw):
    base = dict(
        action="create",
        requester_key=owner_key_for("discord", "u1"),
        job_owner_key=None,
        requester_is_cron_run=False,
        script=None,
        no_agent=False,
        workdir=None,
        model_override=False,
        toolsets_requested=(),
        context_owner_keys=(),
        deliver=None,
    )
    base.update(kw)
    return validate_restricted_scheduler_action(**base)


def test_owner_key_stable_and_platform_scoped():
    assert owner_key_for("discord", "u1") == owner_key_for("discord", "u1")
    assert owner_key_for("discord", "u1") != owner_key_for("telegram", "u1")
    with pytest.raises(ValueError):
        owner_key_for("", "u1")
    with pytest.raises(ValueError):
        owner_key_for("discord", "")


def test_plain_create_allowed():
    allowed, reason = _validate()
    assert allowed is True and reason == "allowed"


def test_manage_own_job_allowed():
    me = owner_key_for("discord", "u1")
    for action in ("update", "pause", "resume", "remove", "run"):
        allowed, reason = _validate(action=action, job_owner_key=me)
        assert allowed is True, (action, reason)


def test_foreign_job_denied():
    other = owner_key_for("discord", "u2")
    for action in ("update", "pause", "resume", "remove", "run"):
        allowed, reason = _validate(action=action, job_owner_key=other)
        assert allowed is False and reason == "foreign_job_denied", action


def test_manage_requires_known_owner():
    allowed, reason = _validate(action="remove", job_owner_key=None)
    assert allowed is False and reason == "job_owner_unknown"


def test_script_and_no_agent_denied():
    assert _validate(script="watch.sh") == (False, "script_denied")
    assert _validate(no_agent=True) == (False, "no_agent_denied")


def test_workdir_denied():
    assert _validate(workdir="/etc") == (False, "workdir_denied")


def test_model_override_denied():
    assert _validate(model_override=True) == (False, "model_override_denied")


def test_toolset_expansion_denied():
    assert _validate(toolsets_requested=("terminal",)) == (False, "toolset_expansion_denied")


def test_foreign_context_denied():
    me = owner_key_for("discord", "u1")
    other = owner_key_for("discord", "u2")
    allowed, reason = _validate(context_owner_keys=(me, other))
    assert allowed is False and reason == "foreign_context_denied"
    allowed, reason = _validate(context_owner_keys=(me,))
    assert allowed is True


def test_deliver_targets_restricted():
    assert _validate(deliver="origin")[0] is True
    assert _validate(deliver="self_dm")[0] is True
    assert _validate(deliver="all") == (False, "deliver_target_denied")
    assert _validate(deliver="telegram") == (False, "deliver_target_denied")


def test_recursive_creation_denied():
    allowed, reason = _validate(requester_is_cron_run=True)
    assert allowed is False and reason == "recursive_creation_denied"


def test_unknown_action_fails_closed():
    allowed, reason = _validate(action="hatch")
    assert allowed is False and reason == "unknown_scheduler_action"


def test_scheduler_capability_is_dormant_until_dispatch_wiring():
    from gateway.acl import _resolve_access_name

    assert _resolve_access_name("scheduler_user") == set()
