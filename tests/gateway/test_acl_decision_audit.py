"""P1 structured ACL decision audit: one event per authorization decision.

Contract from the office handoff: every decision event carries request
correlation IDs, full principal context, exact capability, stable reason
code, matched groups, bootstrap status and the policy epoch. Emission is
fail-soft (an audit failure must never break the message turn) and normal
views never contain tokens or full tool arguments.
"""
from __future__ import annotations

import json

import pytest

from gateway.acl import ACLStore


def _store(tmp_path) -> ACLStore:
    return ACLStore(tmp_path / "acl.sqlite3")


def _record(store: ACLStore, **kw) -> str:
    base = dict(
        capability_type="slash",
        capability_name="background",
        allowed=False,
        reason_code="missing_slash_command_capability",
        platform="discord",
        user_id="u1",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        session_key="agent:main:discord:channel:c1",
        message_id="m1",
        interaction_id=None,
        role_ids=("team",),
        matched_groups=("informer",),
        bootstrap_super_admin=False,
        tool_call_id=None,
    )
    base.update(kw)
    return store.record_decision(**base)


def test_record_and_fetch_decision(tmp_path):
    store = _store(tmp_path)
    event_id = _record(store)
    assert event_id
    event = store.get_decision(event_id)
    assert event is not None
    assert event.capability_type == "slash"
    assert event.capability_name == "background"
    assert event.allowed is False
    assert event.reason_code == "missing_slash_command_capability"
    assert event.platform == "discord"
    assert event.user_id == "u1"
    assert event.guild_id == "g1"
    assert event.channel_id == "c1"
    assert event.session_key == "agent:main:discord:channel:c1"
    assert event.message_id == "m1"
    assert tuple(event.role_ids) == ("team",)
    assert tuple(event.matched_groups) == ("informer",)
    assert event.bootstrap_super_admin is False
    assert event.policy_epoch == store.policy_epoch
    assert event.ts > 0


def test_unknown_event_returns_none(tmp_path):
    assert _store(tmp_path).get_decision("nope") is None


def test_list_decisions_filters(tmp_path):
    store = _store(tmp_path)
    _record(store, user_id="alice", allowed=True, reason_code="allowed")
    _record(store, user_id="bob")
    _record(store, user_id="bob", capability_type="tool", capability_name="terminal",
            reason_code="tool_not_in_policy")
    assert len(store.list_decisions()) == 3
    assert len(store.list_decisions(user_id="bob")) == 2
    assert len(store.list_decisions(allowed=False)) == 2
    assert len(store.list_decisions(capability_type="tool")) == 1
    assert len(store.list_decisions(limit=1)) == 1


def test_capability_type_validated(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _record(store, capability_type="weird")


def test_reason_code_required_for_deny(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _record(store, reason_code="")


def test_retention_prunes_oldest(tmp_path):
    store = _store(tmp_path)
    for i in range(30):
        _record(store, message_id=f"m{i}")
    store.prune_decisions(max_rows=10)
    remaining = store.list_decisions(limit=100)
    assert len(remaining) == 10
    assert remaining[0].message_id == "m29"


def test_fail_soft_on_broken_store(tmp_path):
    store = _store(tmp_path)
    store.db_path = tmp_path / "missing" / "nope.sqlite3"
    event_id = _record(store)
    assert event_id == ""


def test_overlong_capability_name_rejected(tmp_path):
    store = _store(tmp_path)
    secret = "tok-" + "x" * 500
    with pytest.raises(ValueError):
        _record(store, capability_name="terminal " + secret)


def test_mandatory_fields_enforced(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _record(store, platform="")
    with pytest.raises(ValueError):
        _record(store, user_id="")
    with pytest.raises(ValueError):
        _record(store, capability_name="")
    with pytest.raises(ValueError):
        _record(store, allowed=True, reason_code="")


def test_request_id_and_passed_epoch_roundtrip(tmp_path):
    store = _store(tmp_path)
    event_id = _record(store, request_id="req-9", policy_epoch=41)
    event = store.get_decision(event_id)
    assert event.request_id == "req-9"
    assert event.policy_epoch == 41


def test_legacy_mutations_bump_epoch(tmp_path):
    store = _store(tmp_path)
    e0 = store.policy_epoch
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="default", scope="channel", scope_id="c1",
    )
    e1 = store.policy_epoch
    assert e1 > e0
    store.grant_group_access("default", "web")
    assert store.policy_epoch > e1


def test_emission_autocaps_rows(tmp_path):
    store = _store(tmp_path)
    store.decision_max_rows = 10
    for i in range(205):
        _record(store, message_id=f"m{i}")
    assert len(store.list_decisions(limit=500)) <= 110


def test_list_limit_clamped(tmp_path):
    store = _store(tmp_path)
    assert store.list_decisions(limit=999999) == []


def test_audit_degraded_flag(tmp_path):
    store = _store(tmp_path)
    assert store.audit_degraded is False
    store.db_path = tmp_path / "missing" / "nope.sqlite3"
    assert _record(store) == ""
    assert store.audit_degraded is True


def test_audited_read_records_trace(tmp_path):
    import sqlite3 as _sq

    store = _store(tmp_path)
    event_id = _record(store)
    event = store.get_decision_audited(
        event_id, actor_platform="discord", actor_user_id="owner"
    )
    assert event is not None and event.event_id == event_id
    con = _sq.connect(store.db_path)
    row = con.execute(
        "select action, actor_user_id, access_name from audit_log order by id desc limit 1"
    ).fetchone()
    assert row == ("decision.trace", "owner", event_id)


def test_audited_read_withholds_on_broken_store(tmp_path):
    store = _store(tmp_path)
    event_id = _record(store)
    store.db_path = tmp_path / "missing" / "nope.sqlite3"
    assert store.get_decision_audited(
        event_id, actor_platform="discord", actor_user_id="owner"
    ) is None


def test_parse_acl_trace_command(tmp_path):
    from gateway.acl import ACLCommandContext, parse_acl_command

    ctx = ACLCommandContext(platform="discord", channel_id="c1", scope="channel")
    event_id = "a" * 32
    cmd = parse_acl_command(f"/acl trace {event_id}", ctx)
    assert cmd.action == "trace"
    assert cmd.event_id == event_id
    assert cmd.access_name is None
    assert cmd.requires_confirmation is False
    with pytest.raises(ValueError):
        parse_acl_command("/acl trace", ctx)
    with pytest.raises(ValueError):
        parse_acl_command("/acl trace not-hex", ctx)
