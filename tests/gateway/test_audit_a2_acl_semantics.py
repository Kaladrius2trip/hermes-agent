"""Regression tests for the 2026-06-11 fork-audit A2 ACL semantics fixes.

Covers:
- agent-gateway-acl-004: DISCORD_ALLOWED_USERS / allowed_users / allow_from
  are bootstrap super-admins per the fork's ACL design spec (pre-ACL
  deployments must not be locked out of chat and /acl).
- agent-gateway-acl-006: GATEWAY_ALLOWED_USERS (cross-platform chat
  allowlist) does not confer Discord private-context admin status.
- agent-gateway-acl-008: a cross-requester pending-event replacement returns
  the displaced event so the gateway can notify the displaced user.
"""

import os

from gateway.acl import collect_bootstrap_super_admins
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.session import SessionSource


def test_legacy_discord_allowlists_are_bootstrap_super_admins(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "legacy-owner")
    cfg = PlatformConfig(enabled=True, extra={"allowed_users": "cfg-owner", "allow_from": "af-user"})

    bootstrap = collect_bootstrap_super_admins({Platform.DISCORD: cfg}, env=os.environ)

    assert bootstrap.is_super_admin("discord", "legacy-owner")
    assert bootstrap.is_super_admin("discord", "cfg-owner")
    assert bootstrap.is_super_admin("discord", "af-user")
    assert not bootstrap.is_super_admin("discord", "stranger")


def test_gateway_allowed_users_env_does_not_grant_discord_private_context(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "cross-platform-user")
    runner = object.__new__(GatewayRunner)

    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="cross-platform-user",
        chat_id="c1",
        chat_type="group",
    )

    assert runner._discord_source_is_admin(source, {}) is False


def test_explicit_acl_admin_sources_grant_discord_private_context(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ACL_SUPER_ADMINS", raising=False)
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="acl-admin",
        chat_id="c1",
        chat_type="group",
    )

    assert runner._discord_source_is_admin(source, {"acl_super_admins": "acl-admin"}) is True
    assert runner._discord_source_is_admin(source, {"allowed_users": "acl-admin"}) is True
    assert runner._discord_source_is_admin(source, {}) is False


def _event(user_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id=user_id,
            chat_id="c1",
            chat_type="group",
        ),
    )


def test_cross_requester_replacement_returns_displaced_event():
    pending = {}
    first = _event("owner", "queued instruction with context")
    assert merge_pending_message_event(pending, "k", first) is None

    displaced = merge_pending_message_event(pending, "k", _event("other", "hi"))

    assert displaced is first  # caller can notify the displaced user
    assert pending["k"].source.user_id == "other"


def test_same_requester_merge_returns_none():
    pending = {}
    merge_pending_message_event(pending, "k", _event("owner", "part one"))
    displaced = merge_pending_message_event(pending, "k", _event("owner", "part two"), merge_text=True)

    assert displaced is None
