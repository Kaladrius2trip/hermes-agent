from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import time

import pytest

from gateway.acl import ACLStore, BootstrapSuperAdmins
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from tools import slash_confirm


class _NoButtonAdapter:
    def __init__(self):
        self._pending_messages = {}
        self.sent_messages = []

    async def send_slash_confirm(self, **kwargs):
        class Result:
            success = False
        return Result()

    async def _send_with_retry(self, **kwargs):
        self.sent_messages.append(kwargs)


def _source(
    user_id: str = "u1",
    *,
    channel_id: str = "c1",
    thread_id: str | None = None,
    parent_chat_id: str | None = None,
    roles: list[str] | None = None,
) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=channel_id,
        chat_type="channel",
        thread_id=thread_id,
        parent_chat_id=parent_chat_id,
        user_id=user_id,
        user_name=user_id,
        user_role_ids=roles or [],
        guild_id="g1",
    )


def _runner(tmp_path, *, bootstrap_users: set[str] | None = None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={"allowed_users": sorted(bootstrap_users or set())},
            )
        }
    )
    runner.acl_store = ACLStore(tmp_path / "gateway_acl.sqlite3")
    runner._acl_bootstrap_super_admins = BootstrapSuperAdmins(
        {"discord": frozenset(bootstrap_users or set())}
    )
    runner.adapters = {Platform.DISCORD: _NoButtonAdapter()}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner._agent_cache = OrderedDict()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._is_user_authorized = lambda _source: True
    runner.session_store = SimpleNamespace()
    runner._slash_confirm_counter = iter(range(1, 1000))
    return runner


class _FakeRunningAgent:
    def __init__(self, valid_tool_names: set[str] | None = None):
        self.valid_tool_names = valid_tool_names or set()
        self.steered_texts: list[str] = []
        self.interrupts: list[str | None] = []

    def steer(self, text: str) -> bool:
        self.steered_texts.append(text)
        return True

    def interrupt(self, text: str | None = None) -> None:
        self.interrupts.append(text)

    def get_activity_summary(self) -> dict:
        return {"seconds_since_activity": 0, "last_activity_desc": "test"}


def test_gateway_acl_denies_unknown_discord_chat_but_allows_discovery_commands(tmp_path):
    runner = _runner(tmp_path)
    source = _source("stranger")

    policy = runner._resolve_acl_policy_for_source(source)
    assert policy.can_chat is False
    assert policy.allowed_tool_names == set()

    assert runner._check_acl_access(source, None, policy=policy) == (
        "⛔ Chat denied by ACL: no ACL membership for this Discord scope. "
        "Ask an owner to grant access."
    )
    assert runner._check_acl_access(source, "help", policy=policy) is None
    assert runner._check_acl_access(source, "whoami", policy=policy) is None
    denied_status = runner._check_acl_access(source, "status", policy=policy)
    assert "missing slash command capability" in denied_status


def test_gateway_acl_store_recovery_failure_returns_denied_policy(monkeypatch, tmp_path):
    runner = _runner(tmp_path)
    runner.acl_store = None

    import gateway.acl as acl_module

    class BrokenACLStore:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("db unavailable")

    monkeypatch.setattr(acl_module, "ACLStore", BrokenACLStore)

    policy = runner._resolve_acl_policy_for_source(_source("u1"))

    assert policy.can_chat is False
    assert policy.allowed_tool_names == set()
    assert policy.denied_reason == "acl_store_unavailable"


def test_gateway_acl_thread_without_parent_channel_does_not_use_thread_id_as_scope(tmp_path):
    runner = _runner(tmp_path)
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="channel",
        scope_id="thread-123",
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-123",
        chat_type="channel",
        thread_id="thread-123",
        parent_chat_id="",
        user_id="u1",
        user_role_ids=[],
    )

    policy = runner._resolve_acl_policy_for_source(source)

    assert policy.can_chat is False
    assert policy.scope_id is None
    assert policy.groups == set()


def test_gateway_acl_membership_allows_chat_and_filters_tools_by_effective_policy(tmp_path):
    runner = _runner(tmp_path)
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )
    source = _source("u1")

    policy = runner._resolve_acl_policy_for_source(source)
    assert policy.can_chat is True
    assert policy.groups == {"default"}
    assert runner._check_acl_access(source, None, policy=policy) is None
    assert policy.allowed_tool_names == {"clarify", "todo"}
    assert "terminal" not in runner._acl_agent_cache_keys(policy)["acl.allowed_tool_names"]

    denied_status = runner._check_acl_access(source, "status", policy=policy)
    assert "missing slash command capability" in denied_status


def test_acl_agent_cache_keys_change_with_allowed_tool_names(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    assert runner.acl_store is not None
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="default-user",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )

    default_policy = runner._resolve_acl_policy_for_source(_source("default-user"))
    admin_policy = runner._resolve_acl_policy_for_source(_source("owner"))

    default_keys = runner._acl_agent_cache_keys(default_policy)
    admin_keys = runner._acl_agent_cache_keys(admin_policy)

    assert default_keys["acl.allowed_tool_names"] != admin_keys["acl.allowed_tool_names"]
    assert "terminal" not in default_keys["acl.allowed_tool_names"]
    assert "terminal" in admin_keys["acl.allowed_tool_names"]


@pytest.mark.asyncio
async def test_acl_cold_path_denies_non_bootstrap_acl_command(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    assert runner.acl_store is not None
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="plain-admin",
        group_name="admin",
        scope="channel",
        scope_id="c1",
    )
    called = False

    async def fake_handle_acl(_event):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(runner, "_handle_acl_command", fake_handle_acl)
    event = MessageEvent(
        text="/acl list",
        message_type=MessageType.TEXT,
        source=_source("plain-admin"),
    )

    result = await runner._handle_message(event)

    assert isinstance(result, str)
    assert "missing slash command capability" in result
    assert called is False


def test_gateway_acl_role_membership_resolves_for_discord_roles_without_super_admin(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    runner.acl_store.create_group("developer")
    runner.acl_store.grant_group_access("developer", "web")
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="role",
        subject_id="role-dev",
        group_name="developer",
        scope="channel",
        scope_id="c1",
    )

    source = _source("ordinary", roles=["role-dev"])
    policy = runner._resolve_acl_policy_for_source(source)

    assert policy.can_chat is True
    assert policy.bootstrap_super_admin is False
    assert "developer" in policy.groups
    assert "web_search" in policy.allowed_tool_names
    assert "acl" not in policy.allowed_slash_commands


@pytest.mark.asyncio
async def test_acl_command_requires_bootstrap_admin_and_requester_bound_confirmation(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    owner = _source("owner")
    stranger_event = MessageEvent(
        text="/acl grant <@u1> default in this channel",
        message_type=MessageType.TEXT,
        source=_source("stranger"),
    )

    denied = await runner._handle_acl_command(stranger_event)
    assert "bootstrap super-admin" in denied

    event = MessageEvent(
        text="/acl grant <@u1> default in this channel",
        message_type=MessageType.TEXT,
        source=owner,
    )
    prompt = await runner._handle_acl_command(event)
    assert "Confirm /acl" in prompt
    assert "requester-only" in prompt

    session_key = runner._session_key_for_source(owner)
    pending = slash_confirm.get_pending(session_key)
    assert pending is not None

    wrong = await slash_confirm.resolve(
        session_key,
        pending["confirm_id"],
        "once",
        requester_platform="discord",
        requester_user_id="intruder",
    )
    assert "Only the requester" in wrong
    assert runner.acl_store.list_memberships(
        platform="discord", subject_type="user", subject_id="u1"
    ) == []
    assert slash_confirm.get_pending(session_key) is not None

    approved = await slash_confirm.resolve(
        session_key,
        pending["confirm_id"],
        "once",
        requester_platform="discord",
        requester_user_id="owner",
    )
    assert "ACL membership granted" in approved
    memberships = runner.acl_store.list_memberships(
        platform="discord", subject_type="user", subject_id="u1"
    )
    assert [(m.group_name, m.scope, m.scope_id) for m in memberships] == [
        ("default", "channel", "c1")
    ]


def test_gateway_acl_class_default_store_absence_fails_closed_for_chat():
    runner = object.__new__(GatewayRunner)
    source = _source("stranger")

    assert runner._check_acl_access(source, "help") is None
    denied = runner._check_acl_access(source, None)
    assert denied is not None
    assert "ACL store unavailable" in denied


@pytest.mark.asyncio
async def test_run_agent_denies_discord_when_acl_store_unavailable_before_model_call(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.acl_store = None
    runner._get_proxy_url = lambda: None
    called = False

    class BombAgent:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("AIAgent must not be constructed after ACL denial")

    monkeypatch.setattr("run_agent.AIAgent", BombAgent)

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=_source("stranger"),
        session_id="sid",
        session_key="sk",
    )

    assert called is False
    assert result["api_calls"] == 0
    assert result["messages"] == []
    assert result["tools"] == []
    assert "ACL store unavailable" in result["final_response"]


@pytest.mark.asyncio
async def test_requester_bound_confirmation_requires_requester_identity(tmp_path):
    runner = _runner(tmp_path)
    source = _source("")
    source.user_id = None
    event = MessageEvent(text="/acl grant <@u1> default", message_type=MessageType.TEXT, source=source)

    async def handler(choice):
        return "should not run"

    result = await runner._request_slash_confirm(
        event=event,
        command="acl",
        title="/acl",
        message="confirm",
        handler=handler,
        requester_bound=True,
    )

    assert result is not None
    assert "requester identity unavailable" in result
    assert slash_confirm.get_pending(runner._session_key_for_source(source)) is None


@pytest.mark.asyncio
async def test_whoami_includes_effective_acl_state_without_unrelated_users(tmp_path):
    runner = _runner(tmp_path)
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )
    event = MessageEvent(text="/whoami", message_type=MessageType.TEXT, source=_source("u1"))

    output = await runner._handle_whoami_command(event)

    assert "ACL" in output
    assert "platform: discord" in output
    assert "scope: channel:c1" in output
    assert "groups: default" in output
    assert "can_chat: true" in output
    assert "tools: clarify, todo" in output
    assert "owner" not in output


@pytest.mark.asyncio
async def test_handle_message_denies_unknown_discord_natural_chat_by_acl(tmp_path):
    runner = _runner(tmp_path)
    event = MessageEvent(text="please run a terminal command", message_type=MessageType.TEXT, source=_source("stranger"))

    output = await runner._handle_message(event)

    assert output == (
        "⛔ Chat denied by ACL: no ACL membership for this Discord scope. "
        "Ask an owner to grant access."
    )


@pytest.mark.asyncio
async def test_handle_message_uses_acl_for_slash_commands_not_legacy_only(tmp_path):
    runner = _runner(tmp_path)
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="u1",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )
    event = MessageEvent(text="/status", message_type=MessageType.TEXT, source=_source("u1"))

    output = await runner._handle_message(event)

    assert "missing slash command capability" in output
    assert "status" in output


@pytest.mark.asyncio
async def test_handle_message_denies_running_agent_plain_text_without_acl_membership(tmp_path):
    runner = _runner(tmp_path)
    source = _source("stranger")
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner._running_agents_ts[session_key] = 0

    output = await runner._handle_message(
        MessageEvent(text="plain text", message_type=MessageType.TEXT, source=source)
    )

    assert output == (
        "⛔ Chat denied by ACL: no ACL membership for this Discord scope. "
        "Ask an owner to grant access."
    )


@pytest.mark.asyncio
async def test_active_busy_path_denies_unknown_user_before_steer(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    runner._busy_input_mode = "steer"
    owner_source = _source("owner", thread_id="t1", parent_chat_id="c1")
    stranger_source = _source("stranger", thread_id="t1", parent_chat_id="c1")
    session_key = runner._session_key_for_source(owner_source)
    assert session_key == runner._session_key_for_source(stranger_source)

    agent = _FakeRunningAgent(valid_tool_names={"terminal", "todo", "clarify"})
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time()
    adapter = runner.adapters[Platform.DISCORD]
    assert isinstance(adapter, _NoButtonAdapter)

    handled = await runner._handle_active_session_busy_message(
        MessageEvent(
            text="use admin-only terminal now",
            message_type=MessageType.TEXT,
            source=stranger_source,
            message_id="m1",
        ),
        session_key,
    )

    assert handled is True
    assert agent.steered_texts == []
    assert adapter._pending_messages == {}
    assert adapter.sent_messages
    assert adapter.sent_messages[-1]["content"].startswith("⛔ Chat denied by ACL")


@pytest.mark.asyncio
async def test_active_busy_path_queues_lower_policy_user_instead_of_steering_admin_run(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    runner._busy_input_mode = "steer"
    assert runner.acl_store is not None
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="limited",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )
    owner_source = _source("owner", thread_id="t1", parent_chat_id="c1")
    limited_source = _source("limited", thread_id="t1", parent_chat_id="c1")
    session_key = runner._session_key_for_source(owner_source)
    assert session_key == runner._session_key_for_source(limited_source)

    agent = _FakeRunningAgent(valid_tool_names={"terminal", "todo", "clarify"})
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time()
    adapter = runner.adapters[Platform.DISCORD]
    assert isinstance(adapter, _NoButtonAdapter)

    handled = await runner._handle_active_session_busy_message(
        MessageEvent(
            text="continue but do not inherit admin tools",
            message_type=MessageType.TEXT,
            source=limited_source,
            message_id="m2",
        ),
        session_key,
    )

    assert handled is True
    assert agent.steered_texts == []
    pending = adapter._pending_messages[session_key]
    assert pending.text == "continue but do not inherit admin tools"
    assert pending.source.user_id == "limited"
    assert adapter.sent_messages
    assert adapter.sent_messages[-1]["content"].startswith("⏳ Queued for the next turn")


@pytest.mark.asyncio
async def test_slash_steer_queues_lower_policy_user_instead_of_steering_admin_run(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    assert runner.acl_store is not None
    runner.acl_store.grant_membership(
        platform="discord",
        subject_type="user",
        subject_id="limited",
        group_name="default",
        scope="channel",
        scope_id="c1",
    )
    runner.acl_store.grant_group_access("default", "cmd:steer")
    owner_source = _source("owner", thread_id="t1", parent_chat_id="c1")
    limited_source = _source("limited", thread_id="t1", parent_chat_id="c1")
    session_key = runner._session_key_for_source(owner_source)
    assert session_key == runner._session_key_for_source(limited_source)

    agent = _FakeRunningAgent(valid_tool_names={"terminal", "todo", "clarify"})
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time()
    adapter = runner.adapters[Platform.DISCORD]
    assert isinstance(adapter, _NoButtonAdapter)

    output = await runner._handle_message(
        MessageEvent(
            text="/steer use admin terminal now",
            message_type=MessageType.TEXT,
            source=limited_source,
            message_id="m3",
        )
    )

    assert agent.steered_texts == []
    pending = adapter._pending_messages[session_key]
    assert pending.text == "use admin terminal now"
    assert pending.source.user_id == "limited"
    assert isinstance(output, str)
    assert "queued for the next turn" in output


def test_pending_event_merge_replaces_cross_user_event_to_preserve_requester_acl():
    owner_source = _source("owner", thread_id="t1", parent_chat_id="c1")
    limited_source = _source("limited", thread_id="t1", parent_chat_id="c1")
    session_key = build_session_key(
        owner_source,
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    assert session_key == build_session_key(
        limited_source,
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    pending = {}

    merge_pending_message_event(
        pending,
        session_key,
        MessageEvent(
            text="admin image",
            message_type=MessageType.PHOTO,
            source=owner_source,
            message_id="m-admin",
            media_urls=["admin.png"],
            media_types=["image/png"],
        ),
    )
    merge_pending_message_event(
        pending,
        session_key,
        MessageEvent(
            text="limited asks for restricted action",
            message_type=MessageType.TEXT,
            source=limited_source,
            message_id="m-limited",
        ),
    )

    merged = pending[session_key]
    assert merged.source.user_id == "limited"
    assert merged.text == "limited asks for restricted action"
    assert merged.media_urls == []


@pytest.mark.asyncio
async def test_gateway_text_approve_for_acl_confirm_is_requester_bound(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    owner_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="c1",
        chat_type="channel",
        thread_id="t1",
        parent_chat_id="c1",
        user_id="owner",
        user_name="owner",
        user_role_ids=[],
        guild_id="g1",
    )
    intruder_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="c1",
        chat_type="channel",
        thread_id="t1",
        parent_chat_id="c1",
        user_id="intruder",
        user_name="intruder",
        user_role_ids=[],
        guild_id="g1",
    )
    assert runner._session_key_for_source(owner_source) == runner._session_key_for_source(intruder_source)

    prompt = await runner._handle_acl_command(
        MessageEvent(
            text="/acl grant <@u1> default in this channel",
            message_type=MessageType.TEXT,
            source=owner_source,
        )
    )
    assert "Confirm /acl" in prompt
    pending = slash_confirm.get_pending(runner._session_key_for_source(owner_source))
    assert pending is not None

    output = await runner._handle_message(
        MessageEvent(text="/approve", message_type=MessageType.TEXT, source=intruder_source)
    )

    assert isinstance(output, str)
    # The intruder is now stopped by the pre-intercept ACL gate (audit
    # acl-003) before requester binding even gets a chance to reject —
    # either denial is acceptable as long as the pending confirm survives.
    assert "Only the requester" in output or "denied by ACL" in output
    assert runner.acl_store is not None
    assert runner.acl_store.list_memberships(
        platform="discord", subject_type="user", subject_id="u1"
    ) == []
    assert slash_confirm.get_pending(runner._session_key_for_source(owner_source)) is not None


@pytest.mark.asyncio
async def test_handle_message_allows_bootstrap_acl_when_legacy_slash_gate_enabled(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    runner.config.platforms[Platform.DISCORD].extra["group_allow_admin_from"] = ["legacy-admin"]

    event = MessageEvent(text="/acl show", message_type=MessageType.TEXT, source=_source("owner"))

    output = await runner._handle_message(event)

    assert isinstance(output, str)
    assert "ACL effective policy" in output
    assert "bootstrap_super_admin: true" in output
    assert "admin-only" not in output


@pytest.mark.asyncio
async def test_acl_command_is_discord_only_v1(tmp_path):
    runner = _runner(tmp_path, bootstrap_users={"owner"})
    event = MessageEvent(
        text="/acl show",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="slack-channel",
            chat_type="channel",
            user_id="owner",
            user_name="owner",
        ),
    )

    output = await runner._handle_acl_command(event)

    assert output == "⛔ /acl is Discord-only in ACL v1."
