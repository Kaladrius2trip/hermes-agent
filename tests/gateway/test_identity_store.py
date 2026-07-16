import sqlite3

from gateway.identity import (
    FULL_PROFILE_MAX_CHARS,
    IdentityStore,
    SHORT_NOTE_MAX_CHARS,
    SHORT_NOTE_MAX_WORDS,
    message_prefix_for,
)


def _store(tmp_path):
    return IdentityStore(db_path=tmp_path / "id.sqlite3")


def test_resolve_account_to_person(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Alice", short_note="Backend lead")
    s.link_account("discord", "u123", pid)
    person = s.get_person_by_account("discord", "u123")
    assert person is not None
    assert person["display_name"] == "Alice"
    assert person["short_note"] == "Backend lead"
    assert person["person_id"] == pid


def test_unknown_account_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.get_person_by_account("discord", "nobody") is None


def test_short_note_capped_at_max_words(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Bob")
    long = " ".join(f"w{i}" for i in range(SHORT_NOTE_MAX_WORDS + 50))
    stored = s.set_short_note(pid, long)
    assert len(stored.split()) == SHORT_NOTE_MAX_WORDS
    assert len(s.get_person(pid)["short_note"].split()) == SHORT_NOTE_MAX_WORDS


def test_short_note_capped_at_max_chars(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Ivan")
    stored = s.set_short_note(pid, "x" * (SHORT_NOTE_MAX_CHARS * 3))
    assert len(stored) <= SHORT_NOTE_MAX_CHARS
    assert len(s.get_person(pid)["short_note"]) <= SHORT_NOTE_MAX_CHARS


def test_full_profile_appends(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Carol")
    s.append_full_profile(pid, "First fact.")
    s.append_full_profile(pid, "Second fact.")
    prof = s.get_person(pid)["full_profile"]
    assert "First fact." in prof
    assert "Second fact." in prof


def test_get_person_includes_linked_accounts(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Dave")
    s.link_account("discord", "d1", pid)
    s.link_account("telegram", "t1", pid)
    accts = {(a["platform"], a["account_id"]) for a in s.get_person(pid)["accounts"]}
    assert accts == {("discord", "d1"), ("telegram", "t1")}


def test_relink_account_moves_to_new_person(tmp_path):
    s = _store(tmp_path)
    p1 = s.upsert_person(display_name="P1")
    p2 = s.upsert_person(display_name="P2")
    s.link_account("discord", "shared", p1)
    s.link_account("discord", "shared", p2)
    assert s.get_person_by_account("discord", "shared")["person_id"] == p2


def test_tables_are_strict(tmp_path):
    s = _store(tmp_path)
    conn = sqlite3.connect(s.db_path)
    try:
        for table in ("people", "identity_accounts", "identity_audit"):
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "STRICT" in sql
    finally:
        conn.close()


def test_message_prefix_no_profile_uses_user_name(tmp_path):
    s = _store(tmp_path)
    assert message_prefix_for(s, "discord", "u1", "Alice") == "[Alice]"


def test_message_prefix_with_profile(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Alice", short_note="Backend lead")
    s.link_account("discord", "u1", pid)
    assert message_prefix_for(s, "discord", "u1", "aliasname") == "[Alice · Backend lead]"


def test_message_prefix_empty_when_nothing_known(tmp_path):
    s = _store(tmp_path)
    assert message_prefix_for(s, "discord", "u1", "") == ""


def test_message_prefix_failsoft_on_none_store():
    assert message_prefix_for(None, "discord", "u1", "Bob") == "[Bob]"


def test_get_or_create_creates_person_and_link(tmp_path):
    s = _store(tmp_path)
    person = s.get_or_create_person_for_account("discord", "new1", display_name="Eve")
    assert person["person_id"] >= 1
    assert person["display_name"] == "Eve"
    assert s.get_person_by_account("discord", "new1")["person_id"] == person["person_id"]


def test_get_or_create_is_idempotent_no_duplicate(tmp_path):
    s = _store(tmp_path)
    p1 = s.get_or_create_person_for_account("discord", "same", display_name="Eve")
    p2 = s.get_or_create_person_for_account("discord", "same", display_name="IGNORED")
    assert p1["person_id"] == p2["person_id"]
    conn = sqlite3.connect(s.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM identity_accounts").fetchone()[0] == 1
    finally:
        conn.close()


def test_get_or_create_returns_existing_profile(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Frank", short_note="Ops")
    s.link_account("discord", "f1", pid)
    person = s.get_or_create_person_for_account("discord", "f1", display_name="whatever")
    assert person["person_id"] == pid
    assert person["short_note"] == "Ops"


def test_full_profile_tail_capped(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Grace")
    for _ in range(10):
        s.append_full_profile(pid, "x" * 2000)
    prof = s.get_person(pid)["full_profile"]
    assert 2000 <= len(prof) <= FULL_PROFILE_MAX_CHARS


def test_append_full_profile_ignores_empty(tmp_path):
    s = _store(tmp_path)
    pid = s.upsert_person(display_name="Heidi")
    s.append_full_profile(pid, "   ")
    assert s.get_person(pid)["full_profile"] == ""
