"""Tests for the 2026-06-12 kanban observability additions.

- read_worker_log_chunk: incremental offset reads for live log tailing.
- record_delegation_event: worker-side delegation telemetry into task_events.
"""

import json

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return tmp_path


class TestReadWorkerLogChunk:
    def test_missing_log_returns_none(self, board):
        assert kb.read_worker_log_chunk("t_none") is None

    def test_incremental_reads_advance_offset(self, board):
        path = kb.worker_log_path("t_inc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("hello ", encoding="utf-8")

        first = kb.read_worker_log_chunk("t_inc", offset=0)
        assert first["content"] == "hello "
        assert first["offset"] == 6
        assert first["rotated"] is False

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("world")
        second = kb.read_worker_log_chunk("t_inc", offset=first["offset"])
        assert second["content"] == "world"
        assert second["offset"] == 11

        third = kb.read_worker_log_chunk("t_inc", offset=second["offset"])
        assert third["content"] == ""
        assert third["offset"] == 11

    def test_rotation_resets_to_start(self, board):
        path = kb.worker_log_path("t_rot")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fresh", encoding="utf-8")

        chunk = kb.read_worker_log_chunk("t_rot", offset=10_000)

        assert chunk["rotated"] is True
        assert chunk["content"] == "fresh"
        assert chunk["offset"] == 5

    def test_max_bytes_caps_chunk(self, board):
        path = kb.worker_log_path("t_cap")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 100, encoding="utf-8")

        chunk = kb.read_worker_log_chunk("t_cap", offset=0, max_bytes=10)

        assert len(chunk["content"]) == 10
        assert chunk["offset"] == 10
        assert chunk["size"] == 100


class TestRecordDelegationEvent:
    def test_event_row_written_and_readable(self, board):
        with kb.connect_closing() as conn:
            with conn:
                task_id = kb.create_task(conn, title="card", body="", assignee="coder")
        task_id = getattr(task_id, "id", task_id)

        ok = kb.record_delegation_event(
            task_id,
            {"agents": [{"subagent_id": "sa-1", "goal_preview": "review", "status": "completed"}], "count": 1},
        )
        assert ok is True

        with kb.connect_closing() as conn:
            row = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id=? AND kind='delegation'",
                (task_id,),
            ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["agents"][0]["subagent_id"] == "sa-1"

    def test_failure_is_swallowed(self, board, monkeypatch):
        monkeypatch.setattr(kb, "connect_closing", lambda *a, **k: (_ for _ in ()).throw(OSError("no db")))
        assert kb.record_delegation_event("t_x", {"agents": []}) is False
