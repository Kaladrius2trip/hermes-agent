import json

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import get_session_env
from tools.identify_tool import identify_tool


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    return runner


def _src(user_id: str, user_name: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100",
        chat_type="group",
        user_id=user_id,
        user_name=user_name,
    )


def test_bind_session_env_for_source_sets_current_speaker():
    runner = _runner()
    runner._bind_session_env_for_source(_src("A", "Alice"), "sk")
    assert get_session_env("HERMES_SESSION_USER_ID") == "A"
    assert get_session_env("HERMES_SESSION_USER_NAME") == "Alice"
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"


def test_queued_followup_attributes_identify_to_new_sender(tmp_path):
    # Regression for Oracle #2: a queued follow-up from user B must not
    # read/write user A's profile. Emulates the rebind the queued-drain path
    # performs around the recursive _run_agent call.
    runner = _runner()

    runner._bind_session_env_for_source(_src("A", "Alice"), "sk")
    identify_tool("note", "Alice backend lead")

    # Queued follow-up from Bob rebinds identity to Bob.
    runner._bind_session_env_for_source(_src("B", "Bob"), "sk")
    who_b = json.loads(identify_tool("who"))
    assert who_b["person"] is None  # Bob has no profile yet; NOT Alice's

    identify_tool("note", "Bob frontend")
    who_b2 = json.loads(identify_tool("who"))
    assert who_b2["person"]["short_note"].startswith("Bob")

    # Restoring A's binding leaves Alice's profile intact and separate.
    runner._bind_session_env_for_source(_src("A", "Alice"), "sk")
    who_a = json.loads(identify_tool("who"))
    assert who_a["person"]["short_note"].startswith("Alice")
