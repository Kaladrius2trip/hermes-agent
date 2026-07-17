"""P1 NL /acl planner core: strict structured proposals, deterministic apply.

Contract from the office handoff: the model may only produce a strict
structured proposal; deterministic code resolves subjects/capabilities/
scopes; unknown semantics fail closed; the exact diff is rendered for
owner confirmation; the apply gate revalidates requester binding, expiry
and proposal digest, applies transactionally and audits before/after.
Raw model SQL or free-form mutation commands are impossible by design:
steps are typed fields, never strings of commands.
"""
from __future__ import annotations

import time

import pytest

from gateway.acl import ACLStore
from gateway.acl_planner import (
    ACLProposal,
    ACLProposalStep,
    PlannerError,
    apply_proposal,
    bind_proposal_catalog,
    proposal_digest,
    render_proposal,
    validate_proposal,
)

NOW = time.time()


def _store(tmp_path) -> ACLStore:
    return ACLStore(tmp_path / "acl.sqlite3")


def _step(**kw) -> ACLProposalStep:
    base = dict(
        op="grant_membership",
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="informer",
        scope="channel",
        scope_id="c1",
    )
    base.update(kw)
    return ACLProposalStep(**base)


def _proposal(steps=None, store=None, **kw) -> ACLProposal:
    base = dict(
        steps=tuple(steps if steps is not None else [_step()]),
        requester_platform="discord",
        requester_user_id="owner",
        session_key="agent:main:discord:dm:owner",
        created_at=NOW,
        expires_at=NOW + 300,
        policy_epoch=store.policy_epoch if store is not None else None,
    )
    base.update(kw)
    return ACLProposal(**base)


# --- digest ---------------------------------------------------------------

def test_digest_stable_and_order_sensitive():
    p1 = _proposal([_step(), _step(subject_id="u2")])
    p2 = _proposal([_step(), _step(subject_id="u2")])
    p3 = _proposal([_step(subject_id="u2"), _step()])
    assert proposal_digest(p1) == proposal_digest(p2)
    assert proposal_digest(p1) != proposal_digest(p3)


# --- deterministic validation --------------------------------------------

def test_validate_good_proposal(tmp_path):
    validate_proposal(_store(tmp_path), _proposal())


def test_validate_unknown_op_fails(tmp_path):
    with pytest.raises(PlannerError):
        validate_proposal(_store(tmp_path), _proposal([_step(op="drop_table")]))


def test_validate_bad_names_fail(tmp_path):
    with pytest.raises(PlannerError):
        validate_proposal(_store(tmp_path), _proposal([_step(group_name="bad name!")]))
    with pytest.raises(PlannerError):
        validate_proposal(_store(tmp_path), _proposal([_step(subject_id="")]))


def test_validate_unsupported_scope_fails_closed(tmp_path):
    with pytest.raises(PlannerError):
        validate_proposal(_store(tmp_path), _proposal([_step(scope="repo", scope_id="kingmakers")]))


def test_validate_role_global_fails(tmp_path):
    with pytest.raises(PlannerError):
        validate_proposal(
            _store(tmp_path),
            _proposal([_step(op="grant_scoped_membership", subject_type="role",
                             scope="global", scope_id=None)]),
        )


def test_validate_empty_proposal_fails(tmp_path):
    with pytest.raises(PlannerError):
        validate_proposal(_store(tmp_path), _proposal([]))


# --- render ---------------------------------------------------------------

def test_render_exact_diff():
    text = render_proposal(_proposal([
        _step(),
        _step(op="revoke_membership", subject_id="u2", scope="dm", scope_id=None),
        _step(op="grant_scoped_membership", scope="guild", scope_id="g1"),
    ]))
    assert "grant" in text and "revoke" in text
    assert "user:u1" in text and "user:u2" in text
    assert "informer" in text
    assert "guild:g1" in text and "channel:c1" in text and "dm" in text


# --- apply gates ----------------------------------------------------------

def test_apply_happy_path(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store, steps=[
        _step(),
        _step(op="grant_scoped_membership", scope="guild", scope_id="g1"),
    ])
    e0 = store.policy_epoch
    digest = proposal_digest(proposal)
    report = apply_proposal(
        store, proposal, digest=digest,
        actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True,
    )
    assert report["applied"] == 2
    assert store.policy_epoch > e0
    from gateway.acl import ACLRequest
    groups = store.resolve_memberships(ACLRequest(
        platform="discord", user_id="u1", scope="channel",
        channel_id="c1", guild_id="g1",
    ))
    assert groups == {"informer"}


def test_apply_rejects_expired(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store, expires_at=NOW - 1)
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=proposal_digest(proposal),
                       actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)


def test_apply_rejects_digest_mismatch(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store)
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest="0" * 64,
                       actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)


def test_apply_rejects_foreign_actor(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store)
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=proposal_digest(proposal),
                       actor_platform="discord", actor_user_id="mallory", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=proposal_digest(proposal),
                       actor_platform="telegram", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)


def test_apply_is_transactional(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store, steps=[
        _step(),
        _step(op="revoke_group_access", group_name="ghost_group",
              subject_type=None, subject_id=None, scope=None, scope_id=None,
              access_name="web"),
    ])
    # second step targets a group that does not exist -> whole proposal rolls back
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=proposal_digest(proposal),
                       actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)
    assert store.list_memberships(platform="discord") == []


def test_apply_audits_before_and_after(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    proposal = _proposal(store=store)
    apply_proposal(store, proposal, digest=proposal_digest(proposal),
                   actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)
    con = sqlite3.connect(store.db_path)
    actions = [r[0] for r in con.execute(
        "select action from audit_log order by id desc limit 4"
    )]
    assert "proposal.apply.begin" in actions
    assert "proposal.apply.commit" in actions


# --- definition ops (S3) -----------------------------------------------------

CATALOG = {"jenkins_build_pc": "runtime_safe", "jenkins_status": "runtime_safe"}


def test_definition_ops_validate_render_apply(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store, steps=[
        ACLProposalStep(op="create_access_definition", access_name="jenkins-pc",
                        spec="jenkins_*"),
    ])
    proposal = bind_proposal_catalog(store, proposal, catalog=CATALOG)
    text = render_proposal(proposal)
    assert 'tools=["jenkins_build_pc","jenkins_status"]' in text
    assert "jenkins-pc" in text and "jenkins_*" in text
    apply_proposal(store, proposal, digest=proposal_digest(proposal),
                   actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True,
                   catalog=CATALOG)
    assert store.resolve_definition("jenkins-pc") == {"jenkins_build_pc", "jenkins_status"}


def test_definition_expansion_op(tmp_path):
    store = _store(tmp_path)
    store.create_access_definition(
        name="jenkins-pc", spec="jenkins_*", catalog={"jenkins_status": "runtime_safe"},
        actor_platform="discord", actor_user_id="owner",
    )
    proposal = _proposal(store=store, steps=[
        ACLProposalStep(op="approve_definition_expansion", access_name="jenkins-pc"),
    ])
    proposal = bind_proposal_catalog(store, proposal, catalog=CATALOG)
    apply_proposal(store, proposal, digest=proposal_digest(proposal),
                   actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True,
                   catalog=CATALOG)
    assert store.resolve_definition("jenkins-pc") == {"jenkins_build_pc", "jenkins_status"}


def test_definition_op_requires_catalog(tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store=store, steps=[
        ACLProposalStep(op="create_access_definition", access_name="jenkins-pc",
                        spec="jenkins_*"),
    ])
    with pytest.raises(PlannerError):
        apply_proposal(store, proposal, digest=proposal_digest(proposal),
                       actor_platform="discord", actor_user_id="owner", actor_session_key=proposal.session_key, now=NOW, actor_is_bootstrap=True)


def test_planner_refuses_reserved_all(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(PlannerError):
        validate_proposal(store, _proposal(store=store, steps=[
            ACLProposalStep(op="grant_group_access", group_name="informer",
                            access_name="all"),
        ]))
    with pytest.raises(PlannerError):
        validate_proposal(store, _proposal(store=store, steps=[
            ACLProposalStep(op="grant_user_access", platform="discord",
                            subject_type="user", subject_id="u1",
                            access_name="all_runtime", scope="global"),
        ]))


# --- user-grant ops (S2) -----------------------------------------------------

def test_user_access_ops_apply(tmp_path):
    from gateway.acl import ACLRequest

    store = _store(tmp_path)
    grant = _proposal(store=store, steps=[
        ACLProposalStep(op="grant_user_access", platform="discord",
                        subject_type="user", subject_id="solo",
                        access_name="tool:special_tool", scope="global"),
    ])
    apply_proposal(store, grant, digest=proposal_digest(grant),
                   actor_platform="discord", actor_user_id="owner", actor_session_key=grant.session_key, now=NOW, actor_is_bootstrap=True)
    req = ACLRequest(platform="discord", user_id="solo", scope="dm")
    assert store.resolve_subject_access(req) == {"tool:special_tool"}
    revoke = _proposal(store=store, steps=[
        ACLProposalStep(op="revoke_user_access", platform="discord",
                        subject_type="user", subject_id="solo",
                        access_name="tool:special_tool", scope="global"),
    ])
    apply_proposal(store, revoke, digest=proposal_digest(revoke),
                   actor_platform="discord", actor_user_id="owner", actor_session_key=revoke.session_key, now=NOW, actor_is_bootstrap=True)
    assert store.resolve_subject_access(req) == set()


def test_user_access_role_global_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(PlannerError):
        validate_proposal(store, _proposal(store=store, steps=[
            ACLProposalStep(op="grant_user_access", platform="discord",
                            subject_type="role", subject_id="team",
                            access_name="web", scope="global"),
        ]))
