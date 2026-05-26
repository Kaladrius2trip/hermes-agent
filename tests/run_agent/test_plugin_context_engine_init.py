"""Tests that plugin context engines get update_model() called during init.

Regression test for #9071 — plugin engines were never initialized with
context_length, causing the CLI status bar to show 'ctx --'.
"""

from unittest.mock import MagicMock, patch

from agent.context_engine import ContextEngine


class _StubEngine(ContextEngine):
    """Minimal concrete context engine for testing."""

    @property
    def name(self) -> str:
        return "stub"

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None):
        return messages


class _StubEngineWithTool(_StubEngine):
    def get_tool_schemas(self):
        return [
            {
                "name": "lcm_grep",
                "description": "search context",
                "parameters": {"type": "object", "properties": {}},
            }
        ]


class _StubMemoryProvider:
    name = "stub"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.session_id = session_id

    def get_tool_schemas(self):
        return [
            {
                "name": "honcho_search",
                "description": "search memory",
                "parameters": {"type": "object", "properties": {}},
            }
        ]


def test_plugin_engine_gets_context_length_on_init():
    """Plugin context engine should have context_length set during AIAgent init."""
    engine = _StubEngine()
    assert engine.context_length == 0  # ABC default before fix

    cfg = {"context": {"engine": "stub"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.context_engine.load_context_engine", return_value=engine),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent.context_compressor is engine
    assert engine.context_length == 204_800
    assert engine.threshold_tokens == int(204_800 * engine.threshold_percent)


def test_plugin_engine_update_model_args():
    """Verify update_model() receives model, context_length, base_url, api_key, provider."""
    engine = _StubEngine()
    engine.update_model = MagicMock()

    cfg = {"context": {"engine": "stub"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.context_engine.load_context_engine", return_value=engine),
        patch("agent.model_metadata.get_model_context_length", return_value=131_072),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="openrouter/auto",
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    engine.update_model.assert_called_once()
    kw = engine.update_model.call_args.kwargs
    assert kw["context_length"] == 131_072
    assert "model" in kw
    assert "provider" in kw
    assert "api_mode" in kw


def test_allowed_tool_names_filters_plugin_context_engine_tools():
    engine = _StubEngineWithTool()
    cfg = {"context": {"engine": "stub"}, "agent": {}}
    base_tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.context_engine.load_context_engine", return_value=engine),
        patch("agent.model_metadata.get_model_context_length", return_value=131_072),
        patch("run_agent.get_tool_definitions", return_value=base_tools.copy()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="openrouter/auto",
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            allowed_tool_names=["read_file"],
        )

    assert {tool["function"]["name"] for tool in getattr(agent, "tools")} == {"read_file"}
    assert getattr(agent, "valid_tool_names") == {"read_file"}


def test_allowed_tool_names_allows_plugin_context_engine_tools_and_valid_names():
    engine = _StubEngineWithTool()
    cfg = {"context": {"engine": "stub"}, "agent": {}}
    base_tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.context_engine.load_context_engine", return_value=engine),
        patch("agent.model_metadata.get_model_context_length", return_value=131_072),
        patch("run_agent.get_tool_definitions", return_value=base_tools.copy()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="openrouter/auto",
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            allowed_tool_names=["read_file", "lcm_grep"],
        )

    assert {tool["function"]["name"] for tool in getattr(agent, "tools")} == {"read_file", "lcm_grep"}
    assert getattr(agent, "valid_tool_names") == {"read_file", "lcm_grep"}
    assert getattr(agent, "_context_engine_tool_names") == {"lcm_grep"}


def test_allowed_tool_names_filters_memory_provider_tools():
    provider = _StubMemoryProvider()
    cfg = {"memory": {"provider": "stub"}, "agent": {}}
    base_tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=131_072),
        patch("run_agent.get_tool_definitions", return_value=base_tools.copy()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="openrouter/auto",
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            allowed_tool_names=["read_file"],
        )

    assert {tool["function"]["name"] for tool in getattr(agent, "tools")} == {"read_file"}
    assert getattr(agent, "valid_tool_names") == {"read_file"}


def test_allowed_tool_names_allows_memory_provider_tools_and_valid_names():
    provider = _StubMemoryProvider()
    cfg = {"memory": {"provider": "stub"}, "agent": {}}
    base_tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=131_072),
        patch("run_agent.get_tool_definitions", return_value=base_tools.copy()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="openrouter/auto",
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            allowed_tool_names=["read_file", "honcho_search"],
        )

    assert {tool["function"]["name"] for tool in getattr(agent, "tools")} == {"read_file", "honcho_search"}
    assert getattr(agent, "valid_tool_names") == {"read_file", "honcho_search"}
