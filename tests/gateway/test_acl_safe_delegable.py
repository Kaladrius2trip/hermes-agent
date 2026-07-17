"""S5 driver: delegated safe-action gate (owner decision 6B)."""
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
)

NOW = time.time()


def _store(tmp_path) -> ACLStore:
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.set_group_safe_delegable("informer")
    return store


def _proposal(store, steps) -> ACLProposal:
    return ACLProposal(
        steps=tuple(steps),
        requester_platform="discord",
        requester_user_id="lead",
        session_key="agent:main:discord:dm:lead",
        created_at=NOW,
        expires_at=NOW + 300,
        policy_epoch=store.policy_epoch,
    )


def _add_step(group="informer", **kw) -> ACLProposalStep:
    base = dict(
        op="grant_membership", platform="discord", subject_type="user",
        subject_id="newbie", group_name=group, scope="channel", scope_id="c1",
    )
    base.update(kw)
    return ACLProposalStep(**base)


def _apply(store, proposal, *, bootstrap: bool):
    return apply_proposal(
        store, proposal, digest=proposal_digest(proposal),
        actor_platform="discord", actor_user_id="lead", actor_session_key=proposal.session_key, now=NOW,
        actor_is_bootstrap=bootstrap,
        actor_can_delegate=not bootstrap,
    )


def test_flag_helpers(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite3")
    assert store.is_safe_delegable("informer") is False
    before = store.policy_epoch
    store.set_group_safe_delegable("informer")
    assert store.is_safe_delegable("informer") is True
    assert store.policy_epoch > before
    flagged = store.policy_epoch
    store.clear_group_safe_delegable("informer")
    assert store.is_safe_delegable("informer") is False
    assert store.policy_epoch > flagged


def test_delegated_add_to_safe_group_allowed(tmp_path):
    store = _store(tmp_path)
    report = _apply(store, _proposal(store, [_add_step()]), bootstrap=False)
    assert report["applied"] == 1


def test_non_bootstrap_without_current_delegate_authority_rejected(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store, [_add_step()])
    with pytest.raises(PlannerError, match="delegation authority"):
        apply_proposal(
            store,
            proposal,
            digest=proposal_digest(proposal),
            actor_platform="discord",
            actor_user_id="lead",
            actor_session_key=proposal.session_key,
            now=NOW,
            actor_is_bootstrap=False,
        )


def test_delegated_scoped_add_to_safe_group_allowed(tmp_path):
    store = _store(tmp_path)
    step = ACLProposalStep(
        op="grant_scoped_membership", platform="discord", subject_type="user",
        subject_id="newbie", group_name="informer", scope="global",
    )
    assert _apply(store, _proposal(store, [step]), bootstrap=False)["applied"] == 1


def test_delegated_revoke_rejected(tmp_path):
    store = _store(tmp_path)
    step = _add_step(op="revoke_membership")
    with pytest.raises(PlannerError):
        _apply(store, _proposal(store, [step]), bootstrap=False)


def test_delegated_add_to_unsafe_group_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(PlannerError):
        _apply(store, _proposal(store, [_add_step(group="operator")]), bootstrap=False)


def test_delegated_definition_and_user_grant_rejected(tmp_path):
    store = _store(tmp_path)
    definition = ACLProposalStep(
        op="create_access_definition", access_name="jx", spec="jenkins_*"
    )
    with pytest.raises(PlannerError):
        _apply(store, _proposal(store, [definition]), bootstrap=False)
    user_grant = ACLProposalStep(
        op="grant_user_access", platform="discord", subject_type="user",
        subject_id="x", access_name="web", scope="global",
    )
    with pytest.raises(PlannerError):
        _apply(store, _proposal(store, [user_grant]), bootstrap=False)


def test_mixed_proposal_rejected_even_with_safe_add(tmp_path):
    store = _store(tmp_path)
    steps = [_add_step(), _add_step(op="revoke_membership", subject_id="other")]
    with pytest.raises(PlannerError):
        _apply(store, _proposal(store, steps), bootstrap=False)


def test_bootstrap_actor_unrestricted(tmp_path):
    store = _store(tmp_path)
    steps = [
        ACLProposalStep(op="create_group", group_name="fresh"),
        _add_step(group="fresh"),
    ]
    assert _apply(store, _proposal(store, steps), bootstrap=True)["applied"] == 2
