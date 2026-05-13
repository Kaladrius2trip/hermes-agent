"""Tests for Discord channel_prompts resolution and injection."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = types.ModuleType("discord")
    discord_mod.Intents = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    init_calls = []

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        type(self).init_calls.append(dict(kwargs))
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_adapter():
    _ensure_discord_mock()
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter.config = MagicMock()
    adapter.config.extra = {}
    return adapter


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = "Global prompt"
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="12345",
        chat_type="thread",
        user_id="user-1",
    )


def _patch_run_agent_runtime(monkeypatch, tmp_path, toolsets, system_prompt="Global private prompt"):
    (tmp_path / "config.yaml").write_text(
        f"agent:\n  system_prompt: {system_prompt}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: toolsets)


class TestResolveChannelPrompts:
    def test_no_prompt_returns_none(self):
        adapter = _make_adapter()
        assert adapter._resolve_channel_prompt("123") is None

    def test_match_by_channel_id(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"100": "Research mode"}}
        assert adapter._resolve_channel_prompt("100") == "Research mode"

    def test_numeric_yaml_keys_normalized_at_config_load(self):
        """Numeric YAML keys are normalized to strings by config bridging.

        The resolver itself expects string keys (config.py handles normalization),
        so raw numeric keys will not match — this is intentional.
        """
        adapter = _make_adapter()
        # Simulates post-bridging state: keys are already strings
        adapter.config.extra = {"channel_prompts": {"100": "Research mode"}}
        assert adapter._resolve_channel_prompt("100") == "Research mode"
        # Pre-bridging numeric key would not match (bridging is responsible)
        adapter.config.extra = {"channel_prompts": {100: "Research mode"}}
        assert adapter._resolve_channel_prompt("100") is None

    def test_match_by_parent_id(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"200": "Forum prompt"}}
        assert adapter._resolve_channel_prompt("999", parent_id="200") == "Forum prompt"

    def test_exact_channel_overrides_parent(self):
        adapter = _make_adapter()
        adapter.config.extra = {
            "channel_prompts": {
                "999": "Thread override",
                "200": "Forum prompt",
            }
        }
        assert adapter._resolve_channel_prompt("999", parent_id="200") == "Thread override"

    def test_build_message_event_sets_channel_prompt(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"321": "Command prompt"}}
        adapter.build_source = MagicMock(return_value=SimpleNamespace())

        interaction = SimpleNamespace(
            channel_id=321,
            channel=SimpleNamespace(name="general", guild=None, parent_id=None),
            user=SimpleNamespace(id=1, display_name="Brenner"),
        )
        adapter._get_effective_topic = MagicMock(return_value=None)

        event = adapter._build_slash_event(interaction, "/retry")

        assert event.channel_prompt == "Command prompt"

    def test_build_message_event_passes_discord_role_ids(self):
        adapter = _make_adapter()
        adapter.build_source = MagicMock(return_value=SimpleNamespace())

        interaction = SimpleNamespace(
            channel_id=321,
            channel=SimpleNamespace(name="general", guild=None, parent_id=None),
            user=SimpleNamespace(
                id=1,
                display_name="Brenner",
                roles=[SimpleNamespace(id=111), SimpleNamespace(id=222)],
            ),
        )
        adapter._get_effective_topic = MagicMock(return_value=None)

        adapter._build_slash_event(interaction, "/retry")

        assert adapter.build_source.call_args.kwargs["user_role_ids"] == ["111", "222"]

    @pytest.mark.asyncio
    async def test_dispatch_thread_session_inherits_parent_channel_prompt(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"200": "Parent prompt"}}
        adapter.build_source = MagicMock(return_value=SimpleNamespace())
        adapter._get_effective_topic = MagicMock(return_value=None)
        adapter.handle_message = AsyncMock()

        interaction = SimpleNamespace(
            guild=SimpleNamespace(name="Wetlands"),
            channel=SimpleNamespace(id=200, parent=None),
            user=SimpleNamespace(id=1, display_name="Brenner"),
        )

        await adapter._dispatch_thread_session(interaction, "999", "new-thread", "hello")

        dispatched_event = adapter.handle_message.await_args.args[0]
        assert dispatched_event.channel_prompt == "Parent prompt"

    def test_blank_prompts_are_ignored(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"100": "   "}}
        assert adapter._resolve_channel_prompt("100") is None


@pytest.mark.asyncio
async def test_retry_preserves_channel_prompt(monkeypatch):
    runner = _make_runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1", last_prompt_tokens=10),
        load_transcript=lambda session_id: [
            {"role": "user", "content": "original message"},
            {"role": "assistant", "content": "old reply"},
        ],
        rewrite_transcript=MagicMock(),
    )
    runner._handle_message = AsyncMock(return_value="ok")

    event = MessageEvent(
        text="/retry",
        message_type=gateway_run.MessageType.COMMAND,
        source=_make_source(),
        raw_message=SimpleNamespace(),
        channel_prompt="Channel prompt",
    )

    result = await runner._handle_retry_command(event)

    assert result == "ok"
    retried_event = runner._handle_message.await_args.args[0]
    assert retried_event.channel_prompt == "Channel prompt"


@pytest.mark.asyncio
async def test_run_agent_appends_channel_prompt_to_ephemeral_system_prompt(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    (tmp_path / "config.yaml").write_text("agent:\n  system_prompt: Global prompt\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None
    event = MessageEvent(
        text="hi",
        source=_make_source(),
        message_id="m1",
        channel_prompt="Channel prompt",
    )
    result = await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:discord:thread:12345",
        channel_prompt=event.channel_prompt,
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["ephemeral_system_prompt"] == (
        "Context prompt\n\nChannel prompt\n\nGlobal prompt"
    )


@pytest.mark.asyncio
async def test_private_context_restricts_non_admin_memory_soul_and_toolsets(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={
                    "private_context_admin_only": True,
                    "group_allow_admin_from": ["owner-1"],
                },
            )
        }
    )

    (tmp_path / "config.yaml").write_text("agent:\n  system_prompt: Global private prompt\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {
            "web",
            "browser",
            "memory",
            "session_search",
            "terminal",
            "file",
            "skills",
            "discord_admin",
        },
    )

    _CapturingAgent.last_init = None
    source = _make_source()
    source.user_id = "guest-1"
    await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:discord:thread:12345:user:guest-1",
        channel_prompt="Channel private prompt",
    )

    assert _CapturingAgent.last_init["skip_memory"] is True
    assert _CapturingAgent.last_init["skip_context_files"] is True
    assert set(_CapturingAgent.last_init["enabled_toolsets"]) == {"web"}
    assert "Global private prompt" not in _CapturingAgent.last_init["ephemeral_system_prompt"]
    assert "Channel private prompt" not in _CapturingAgent.last_init["ephemeral_system_prompt"]


@pytest.mark.asyncio
async def test_allowed_roles_do_not_grant_private_context_admin(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={
                    "private_context_admin_only": True,
                    "allowed_roles": ["role-guest"],
                    "group_allow_admin_from": ["owner-1"],
                },
            )
        }
    )

    (tmp_path / "config.yaml").write_text("agent:\n  system_prompt: Global private prompt\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"web", "memory", "terminal"},
    )

    _CapturingAgent.last_init = None
    source = _make_source()
    source.user_id = "guest-1"
    source.guild_id = "guild-1"
    source.user_role_ids = ["role-guest"]
    await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:discord:thread:12345:user:guest-1",
        channel_prompt="Channel private prompt",
    )

    assert _CapturingAgent.last_init["skip_memory"] is True
    assert _CapturingAgent.last_init["skip_context_files"] is True
    assert set(_CapturingAgent.last_init["enabled_toolsets"]) == {"web"}
    assert "Global private prompt" not in _CapturingAgent.last_init["ephemeral_system_prompt"]
    assert "Channel private prompt" not in _CapturingAgent.last_init["ephemeral_system_prompt"]


@pytest.mark.asyncio
async def test_private_context_keeps_full_access_for_admin(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={
                    "private_context_admin_only": True,
                    "group_allow_admin_from": ["owner-1"],
                },
            )
        }
    )

    (tmp_path / "config.yaml").write_text("agent:\n  system_prompt: Global private prompt\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    toolsets = {"web", "memory", "session_search", "terminal", "file"}
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: toolsets)

    _CapturingAgent.last_init = None
    source = _make_source()
    source.user_id = "owner-1"
    await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:discord:thread:12345:user:owner-1",
        channel_prompt="Channel private prompt",
    )

    assert _CapturingAgent.last_init.get("skip_memory") is not True
    assert _CapturingAgent.last_init.get("skip_context_files") is not True
    assert set(_CapturingAgent.last_init["enabled_toolsets"]) == toolsets
    assert _CapturingAgent.last_init["ephemeral_system_prompt"] == (
        "Context prompt\n\nChannel private prompt\n\nGlobal prompt"
    )


@pytest.mark.asyncio
async def test_private_context_agent_cache_isolated_by_admin_status(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    _CapturingAgent.init_calls = []
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={
                    "private_context_admin_only": True,
                    "group_allow_admin_from": ["owner-1"],
                },
            )
        }
    )
    _patch_run_agent_runtime(monkeypatch, tmp_path, {"web", "memory"})

    admin_source = _make_source()
    admin_source.user_id = "owner-1"
    await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=admin_source,
        session_id="session-1",
        session_key="agent:main:discord:thread:shared",
        channel_prompt="Channel private prompt",
    )

    guest_source = _make_source()
    guest_source.user_id = "guest-1"
    await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=guest_source,
        session_id="session-1",
        session_key="agent:main:discord:thread:shared",
        channel_prompt="Channel private prompt",
    )

    assert len(_CapturingAgent.init_calls) == 2
    assert _CapturingAgent.init_calls[0].get("skip_memory") is not True
    assert _CapturingAgent.init_calls[1]["skip_memory"] is True
    assert set(_CapturingAgent.init_calls[1]["enabled_toolsets"]) == {"web"}


@pytest.mark.asyncio
async def test_private_context_applies_to_discord_background_tasks(monkeypatch):
    _install_fake_agent(monkeypatch)
    _CapturingAgent.last_init = None
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={
                    "private_context_admin_only": True,
                    "group_allow_admin_from": ["owner-1"],
                },
            )
        }
    )

    adapter = SimpleNamespace(
        send=AsyncMock(),
        extract_media=lambda text: ([], text),
        extract_images=lambda text: ([], text),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._thread_metadata_for_source = lambda source, event_message_id=None: None
    runner._resolve_session_agent_runtime = lambda **kwargs: (
        "gpt-5.4",
        {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    runner._resolve_session_reasoning_config = lambda **kwargs: None
    runner._load_service_tier = lambda: None
    runner._resolve_turn_agent_config = lambda prompt, model, runtime_kwargs: {
        "model": model,
        "runtime": runtime_kwargs,
    }
    runner._cleanup_agent_resources = lambda agent: None

    async def run_sync_now(fn):
        return fn()

    runner._run_in_executor_with_context = run_sync_now
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"web", "memory", "terminal"},
    )

    source = _make_source()
    source.user_id = "guest-1"
    await runner._run_background_task("background prompt", source, "task-1")

    assert _CapturingAgent.last_init["skip_memory"] is True
    assert _CapturingAgent.last_init["skip_context_files"] is True
    assert set(_CapturingAgent.last_init["enabled_toolsets"]) == {"web"}
    adapter.send.assert_called()


def test_session_source_preserves_discord_role_ids():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="group",
        user_id="user-1",
        user_role_ids=["role-1", "role-2"],
    )

    restored = SessionSource.from_dict(source.to_dict())

    assert restored.user_role_ids == ["role-1", "role-2"]


def test_build_source_accepts_discord_role_ids():
    adapter = _make_adapter()
    adapter.platform = Platform.DISCORD

    source = adapter.build_source(
        chat_id="channel-1",
        chat_type="group",
        user_id="user-1",
        user_role_ids=["role-1", "role-2"],
    )

    assert source.user_role_ids == ["role-1", "role-2"]


def test_discord_default_config_exposes_privacy_gate_keys():
    from hermes_cli.config import DEFAULT_CONFIG

    discord_cfg = DEFAULT_CONFIG["discord"]

    assert "allowed_users" in discord_cfg
    assert "allowed_roles" in discord_cfg
    assert discord_cfg["private_context_admin_only"] is False
    assert discord_cfg["private_context_safe_toolsets"]
    assert "group_allow_admin_from" in discord_cfg
    assert "group_user_allowed_commands" in discord_cfg


def test_config_bridges_discord_admin_and_privacy_keys(monkeypatch, tmp_path):
    import os
    import yaml

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "discord": {
                    "allowed_users": "owner-1",
                    "allowed_roles": ["role-1", "role-2"],
                    "private_context_admin_only": True,
                    "private_context_safe_toolsets": "web,search,todo,terminal",
                    "group_allow_admin_from": "owner-1",
                    "group_user_allowed_commands": "help,status",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_ROLES", raising=False)

    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    extra = config.platforms[Platform.DISCORD].extra

    assert os.getenv("DISCORD_ALLOWED_USERS") == "owner-1"
    assert os.getenv("DISCORD_ALLOWED_ROLES") == "role-1,role-2"
    assert extra["allowed_users"] == "owner-1"
    assert extra["allowed_roles"] == ["role-1", "role-2"]
    assert extra["private_context_admin_only"] is True
    assert extra["private_context_safe_toolsets"] == ["web", "search", "todo", "terminal"]
    assert extra["group_allow_admin_from"] == "owner-1"
    assert extra["group_user_allowed_commands"] == "help,status"
