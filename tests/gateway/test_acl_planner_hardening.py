"""S4 driver: planner confirmation-integrity hardening (4 Oracle gaps)."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, cast

import pytest

from gateway.acl import ACLStore
from gateway.acl_planner import (
    ACLProposal,
    ACLProposalStep,
    PlannerError,
    apply_proposal,
    bind_proposal_catalog,
    proposal_digest,
    validate_proposal,
)

NOW = time.time()


def _store(tmp_path) -> ACLStore:
    return ACLStore(tmp_path / "acl.sqlite3")


def _step(**kw) -> ACLProposalStep:
    base = dict(
        op="grant_membership", platform="discord", subject_type="user",
        subject_id="u1", group_name="informer", scope="channel", scope_id="c1",
    )
    base.update(kw)
    return ACLProposalStep(**base)


def _proposal(store=None, steps=None, **kw) -> ACLProposal:
    base = dict(
        steps=tuple(steps if steps is not None else [_step()]),
        requester_platform="discord",
        requester_user_id="owner",
        session_key="agent:main:discord:dm:owner",
        created_at=NOW,
        expires_at=NOW + 300,
        policy_epoch=store.policy_epoch if store is not None else 0,
    )
    base.update(kw)
    return ACLProposal(**base)


def test_definition_catalog_change_requires_reconfirmation(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(
        store,
        [ACLProposalStep(
            op="create_access_definition", access_name="jenkins-pc", spec="jenkins_*"
        )],
    )
    original = {"jenkins_status": "runtime_safe"}
    proposal = bind_proposal_catalog(store, proposal, catalog=original)
    changed = {**original, "jenkins_build": "runtime_safe"}
    with pytest.raises(PlannerError, match="catalog changed"):
        apply_proposal(
            store,
            proposal,
            digest=proposal_digest(proposal),
            actor_platform="discord",
            actor_user_id="owner",
            actor_session_key=proposal.session_key,
            now=NOW,
            actor_is_bootstrap=True,
            catalog=changed,
        )


def test_digest_binds_requester_session_expiry_epoch():
    base = _proposal()
    assert proposal_digest(base) != proposal_digest(
        _proposal(requester_user_id="mallory")
    )
    assert proposal_digest(base) != proposal_digest(
        _proposal(session_key="agent:other")
    )
    assert proposal_digest(base) != proposal_digest(
        _proposal(expires_at=NOW + 999)
    )
    assert proposal_digest(base) != proposal_digest(
        _proposal(created_at=NOW - 1)
    )
    assert proposal_digest(base) != proposal_digest(_proposal(policy_epoch=77))


def test_no_implicit_group_creation(tmp_path):
    store = _store(tmp_path)
    ghost = _proposal(store, steps=[_step(group_name="ghostgroup")])
    with pytest.raises(PlannerError):
        validate_proposal(store, ghost)
    ok = _proposal(store, steps=[
        ACLProposalStep(op="create_group", group_name="ghostgroup"),
        _step(group_name="ghostgroup"),
    ])
    validate_proposal(store, ok)


def test_stale_policy_epoch_rejected(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store)
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="x",
        group_name="default", scope="channel", scope_id="c9",
    )
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=proposal_digest(proposal),
                       actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)


def test_single_use_confirmation(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store)
    digest = proposal_digest(proposal)
    apply_proposal(store, proposal, digest=digest,
                   actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)
    fresh = _proposal(store)
    assert proposal_digest(fresh) != digest
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=digest,
                       actor_platform="discord", actor_user_id="owner",
                       actor_session_key=proposal.session_key,
                       now=NOW, actor_is_bootstrap=True)
    denied = [row for row in store.audit(limit=20) if row.action == "proposal.apply.denied"]
    assert len(denied) == 1
    assert denied[0].reason == "policy_epoch_changed"


def test_apply_requires_exact_requester_session(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store)
    with pytest.raises(PlannerError, match="session"):
        apply_proposal(
            store,
            proposal,
            digest=proposal_digest(proposal),
            actor_platform="discord",
            actor_user_id="owner",
            actor_session_key="agent:other-session",
            now=NOW,
            actor_is_bootstrap=True,
        )


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_proposal_expiry_rejected(tmp_path, expires_at):
    store = _store(tmp_path)
    proposal = _proposal(store, expires_at=expires_at)
    with pytest.raises(PlannerError, match="finite"):
        apply_proposal(
            store,
            proposal,
            digest=proposal_digest(proposal),
            actor_platform="discord",
            actor_user_id="owner",
            actor_session_key=proposal.session_key,
            now=NOW,
            actor_is_bootstrap=True,
        )


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_subject_grant_step_expiry_rejected(tmp_path, expires_at):
    store = _store(tmp_path)
    proposal = _proposal(
        store,
        steps=[
            ACLProposalStep(
                op="grant_user_access",
                platform="discord",
                subject_type="user",
                subject_id="u1",
                access_name="web",
                scope="global",
                expires_at=expires_at,
            )
        ],
    )
    with pytest.raises(PlannerError, match="finite"):
        validate_proposal(store, proposal)


def test_policy_epoch_rechecked_inside_write_transaction(tmp_path, monkeypatch):
    store = _store(tmp_path)
    proposal = _proposal(store)
    original_connect = store._connect
    raced = False

    class RacingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            nonlocal raced
            if str(sql).strip().upper() == "BEGIN IMMEDIATE" and not raced:
                raced = True
                with original_connect() as race_conn:
                    ACLStore._bump_policy_epoch_conn(race_conn)
            return self.connection.execute(sql, *args)

    monkeypatch.setattr(store, "_connect", lambda: RacingConnection(original_connect()))
    with pytest.raises(PlannerError, match="policy changed"):
        apply_proposal(
            store,
            proposal,
            digest=proposal_digest(proposal),
            actor_platform="discord",
            actor_user_id="owner",
            actor_session_key=proposal.session_key,
            now=NOW,
            actor_is_bootstrap=True,
        )


def test_serialized_definition_snapshot_envelope_applies(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(
        store,
        [ACLProposalStep(
            op="create_access_definition", access_name="jenkins-pc", spec="jenkins_*"
        )],
    )
    catalog = {"jenkins_status": "runtime_safe"}
    bound = bind_proposal_catalog(store, proposal, catalog=catalog)
    serialized_snapshots = json.loads(json.dumps(bound.definition_snapshots))
    reconstructed = replace(
        bound,
        definition_snapshots=cast(Any, serialized_snapshots),
    )

    result = apply_proposal(
        store,
        reconstructed,
        digest=proposal_digest(reconstructed),
        actor_platform="discord",
        actor_user_id="owner",
        actor_session_key=reconstructed.session_key,
        now=NOW,
        catalog=catalog,
        actor_is_bootstrap=True,
    )
    assert result["applied"] == 1
    assert store.resolve_definition("jenkins-pc") == {"jenkins_status"}
