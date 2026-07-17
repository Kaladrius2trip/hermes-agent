"""S4 driver: planner confirmation-integrity hardening (4 Oracle gaps)."""
from __future__ import annotations

import time

import pytest

from gateway.acl import ACLStore
from gateway.acl_planner import (
    ACLProposal,
    ACLProposalStep,
    PlannerError,
    apply_proposal,
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
                       actor_platform="discord", actor_user_id="owner", now=NOW)


def test_single_use_confirmation(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store)
    digest = proposal_digest(proposal)
    apply_proposal(store, proposal, digest=digest,
                   actor_platform="discord", actor_user_id="owner", now=NOW)
    fresh = _proposal(store)
    assert proposal_digest(fresh) != digest
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=digest,
                       actor_platform="discord", actor_user_id="owner", now=NOW)
