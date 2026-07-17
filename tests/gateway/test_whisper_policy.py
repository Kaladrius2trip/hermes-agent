"""P1 whisper: recipient-scoped outbound DM policy core.

Pure fail-closed recipient validation from the office handoff. Name
resolution, live role verification and delivery are adapter wiring; this
module decides, given adapter-verified candidate facts, whether exactly
one approved human recipient exists.
"""
from __future__ import annotations

import time

from gateway.whisper_policy import WhisperCandidate, resolve_whisper_recipient

NOW = time.time()


def _cand(**kw) -> WhisperCandidate:
    base = dict(
        user_id="u1",
        is_bot=False,
        guild_id="g1",
        role_names=("team",),
        roles_verified_at=NOW,
        roles_are_managed=(),
    )
    base.update(kw)
    return WhisperCandidate(**base)


def _resolve(candidates, **kw):
    base = dict(
        trusted_guild_id="g1",
        approved_role_names=("team", "Dev", "QA"),
        now=NOW,
    )
    base.update(kw)
    return resolve_whisper_recipient(candidates, **base)


def test_allows_single_approved_human():
    recipient, reason = _resolve([_cand()])
    assert recipient is not None and recipient.user_id == "u1"
    assert reason == "allowed"


def test_denies_no_candidates():
    recipient, reason = _resolve([])
    assert recipient is None and reason == "recipient_not_found"


def test_denies_ambiguous_resolution():
    recipient, reason = _resolve([_cand(), _cand(user_id="u2")])
    assert recipient is None and reason == "ambiguous_recipient"


def test_denies_bot_user():
    recipient, reason = _resolve([_cand(is_bot=True)])
    assert recipient is None and reason == "recipient_is_bot"


def test_denies_cross_guild():
    recipient, reason = _resolve([_cand(guild_id="g2")])
    assert recipient is None and reason == "untrusted_guild"
    recipient, reason = _resolve([_cand(guild_id="")])
    assert recipient is None and reason == "untrusted_guild"


def test_denies_without_approved_role():
    recipient, reason = _resolve([_cand(role_names=("randomrole",))])
    assert recipient is None and reason == "no_approved_role"


def test_role_match_is_exact_not_case_folded():
    recipient, reason = _resolve([_cand(role_names=("TEAM",))])
    assert recipient is None and reason == "no_approved_role"


def test_denies_managed_role_only_match():
    recipient, reason = _resolve(
        [_cand(role_names=("team",), roles_are_managed=("team",))]
    )
    assert recipient is None and reason == "no_approved_role"


def test_denies_stale_role_snapshot():
    recipient, reason = _resolve([_cand(roles_verified_at=NOW - 3600)])
    assert recipient is None and reason == "stale_role_membership"


def test_denies_unconfigured_policy():
    recipient, reason = _resolve([_cand()], approved_role_names=())
    assert recipient is None and reason == "whisper_not_configured"
    recipient, reason = _resolve([_cand()], trusted_guild_id="")
    assert recipient is None and reason == "whisper_not_configured"


def test_whisper_access_capability_registered():
    from gateway.acl import _resolve_access_name

    assert _resolve_access_name("whisper") == {"whisper"}
