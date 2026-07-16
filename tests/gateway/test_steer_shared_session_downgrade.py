"""Steer identity attribution (Oracle steer B).

In a shared multi-user session, mid-run steer would splice a foreign sender's
text into whichever turn is running (bound to the ORIGINAL sender's identity
contextvars), so ``identify`` would read/write the wrong person. We downgrade
shared-session steer to the queue, where each sender's message runs as its own
identity-bound turn. Single-user (DM) steer is unchanged.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)
from gateway.run import GatewayRunner  # noqa: E402


def _event(chat_type: str, user_id: str, chat_id: str = "123") -> MessageEvent:
    source = SessionSource(
        platform=MagicMock(value="telegram"),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )
    return MessageEvent(text="steer this", message_type=MessageType.TEXT, source=source, message_id="m1")


def _runner(busy_mode: str = "steer", group_per_user: bool = False) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = group_per_user
    runner.config.thread_sessions_per_user = False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._is_user_authorized = lambda _s: True
    runner._busy_input_mode = busy_mode
    runner._session_db = MagicMock()
    runner._session_db._db = MagicMock()
    runner._session_db._db.get_compression_lock_holder.return_value = None
    return runner


def _adapter() -> MagicMock:
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value="telegram")
    return adapter


def _agent() -> MagicMock:
    agent = MagicMock()
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.steer.return_value = True
    return agent


def test_predicate_dm_not_shared_group_shared_when_not_per_user():
    runner = _runner()
    assert runner._is_shared_multi_user_source(_event("dm", "u1").source) is False
    assert runner._is_shared_multi_user_source(_event("group", "u1").source) is True
    runner.config.group_sessions_per_user = True
    assert runner._is_shared_multi_user_source(_event("group", "u1").source) is False


def test_predicate_case1_no_participant_group_is_shared():
    # group_sessions_per_user=True but NO participant id: build_session_key can't
    # append a participant, so the key is chat-level shared (Oracle rev.4 case 1).
    runner = _runner(group_per_user=True)
    src = SessionSource(platform=MagicMock(value="telegram"), chat_id="c", chat_type="group", user_id="")
    assert runner._is_shared_multi_user_source(src) is True


def test_predicate_case2_shared_thread_when_group_isolated():
    # group_sessions_per_user=False forces isolate_user off, so the thread is
    # shared even with thread_sessions_per_user=True + a participant (case 2).
    runner = _runner(group_per_user=False)
    runner.config.thread_sessions_per_user = True
    src = SessionSource(
        platform=MagicMock(value="telegram"), chat_id="c", chat_type="group", thread_id="t1", user_id="u1"
    )
    assert runner._is_shared_multi_user_source(src) is True


@pytest.mark.asyncio
async def test_shared_group_steer_downgrades_to_queue():
    runner = _runner("steer", group_per_user=False)
    adapter = _adapter()
    event = _event("group", "user2")
    sk = build_session_key(event.source)
    agent = _agent()
    runner._running_agents[sk] = agent
    runner.adapters[event.source.platform] = adapter
    runner._queue_or_replace_pending_event = MagicMock()

    with patch("gateway.run.merge_pending_message_event"):
        handled = await runner._handle_active_session_busy_message(event, sk)

    assert handled is True
    agent.steer.assert_not_called()
    runner._queue_or_replace_pending_event.assert_called_once()
    assert runner._queue_or_replace_pending_event.call_args.args[1] is event


@pytest.mark.asyncio
async def test_dm_steer_still_steers_in_place():
    runner = _runner("steer", group_per_user=False)
    adapter = _adapter()
    event = _event("dm", "user1")
    sk = build_session_key(event.source)
    agent = _agent()
    runner._running_agents[sk] = agent
    runner.adapters[event.source.platform] = adapter
    runner._queue_or_replace_pending_event = MagicMock()

    with patch("gateway.run.merge_pending_message_event"):
        await runner._handle_active_session_busy_message(event, sk)

    agent.steer.assert_called_once()
    runner._queue_or_replace_pending_event.assert_not_called()


@pytest.mark.asyncio
async def test_shared_group_interrupt_downgrades_to_queue():
    # Oracle interrupt design: in a shared session a co-member's message must
    # NOT abort/discard another member's turn; it downgrades to the queue.
    runner = _runner("interrupt", group_per_user=False)
    adapter = _adapter()
    event = _event("group", "user2")
    sk = build_session_key(event.source)
    agent = _agent()
    runner._running_agents[sk] = agent
    runner.adapters[event.source.platform] = adapter
    runner._queue_or_replace_pending_event = MagicMock()

    with patch("gateway.run.merge_pending_message_event"):
        handled = await runner._handle_active_session_busy_message(event, sk)

    assert handled is True
    agent.interrupt.assert_not_called()
    runner._queue_or_replace_pending_event.assert_called_once()


@pytest.mark.asyncio
async def test_dm_interrupt_still_interrupts_in_place():
    # Single-user (DM) interrupt is unchanged: it still aborts the running turn.
    runner = _runner("interrupt", group_per_user=False)
    adapter = _adapter()
    event = _event("dm", "user1")
    sk = build_session_key(event.source)
    agent = _agent()
    runner._running_agents[sk] = agent
    runner.adapters[event.source.platform] = adapter
    runner._queue_or_replace_pending_event = MagicMock()

    with patch("gateway.run.merge_pending_message_event"):
        await runner._handle_active_session_busy_message(event, sk)

    agent.interrupt.assert_called_once()
