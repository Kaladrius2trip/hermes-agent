import json

import pytest

from tools.identify_tool import identify_tool


@pytest.fixture
def _sess(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "u1")
    monkeypatch.setenv("HERMES_SESSION_USER_NAME", "Alice")


def test_identify_note_creates_then_who_reads(_sess):
    r = json.loads(identify_tool("note", "Backend lead, owns payments"))
    assert r["success"] is True
    assert r["short_note"].startswith("Backend lead")
    who = json.loads(identify_tool("who"))
    assert who["person"]["short_note"].startswith("Backend lead")
    assert who["person"]["display_name"] == "Alice"


def test_identify_who_no_profile_yet(_sess):
    r = json.loads(identify_tool("who"))
    assert r["success"] is True
    assert r["person"] is None


def test_identify_remember_appends_to_profile(_sess):
    identify_tool("note", "Lead")
    identify_tool("remember", "Prefers async standups.")
    who = json.loads(identify_tool("who"))
    assert "Prefers async standups." in who["person"]["full_profile"]


def test_identify_no_current_user_is_graceful(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    r = json.loads(identify_tool("who"))
    assert r["success"] is False


def test_identify_note_requires_text(_sess):
    r = json.loads(identify_tool("note", ""))
    assert r["success"] is False
