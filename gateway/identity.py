"""Team identity + lightweight per-user profiles, in the gateway ACL SQLite DB.

A ``person`` groups one or more platform accounts and carries a short,
LLM-maintained note (injected once per inbound message so the model knows who
is speaking) plus a fuller profile fetched only on demand. This is
INFORMATIONAL only: it does NOT participate in ACL authorization (Phase A).
Cross-platform account linking is a separate, authorized command (Phase 2).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

SHORT_NOTE_MAX_WORDS = 120
# SECURITY: char ceilings bound the persistent injected prefix / stored profile
# against untrusted note text. The char cap on cap_words stops one whitespace-free
# value (a single "word") from producing an arbitrarily large prefix; the tail-cap
# stops full_profile growth from repeated ``remember`` calls.
SHORT_NOTE_MAX_CHARS = 700
FULL_PROFILE_MAX_CHARS = 8000


def cap_words(
    text: Any, max_words: int = SHORT_NOTE_MAX_WORDS, max_chars: int = SHORT_NOTE_MAX_CHARS
) -> str:
    """Collapse whitespace, then cap to ``max_words`` words and ``max_chars`` chars."""
    capped = " ".join(str(text or "").split()[:max_words])
    if len(capped) > max_chars:
        capped = capped[:max_chars].rstrip()
    return capped


class IdentityStore:
    """SQLite store for people + platform-account links (in gateway_acl.sqlite3)."""

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = (
            Path(db_path) if db_path is not None else get_hermes_home() / "gateway_acl.sqlite3"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    _TABLE_DDL: "dict[str, str]" = {
        "people": """
            CREATE TABLE people (
                person_id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL DEFAULT '',
                short_note TEXT NOT NULL DEFAULT '',
                full_profile TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            ) STRICT
        """,
        "identity_accounts": """
            CREATE TABLE identity_accounts (
                platform TEXT NOT NULL CHECK (platform <> ''),
                account_id TEXT NOT NULL CHECK (account_id <> ''),
                person_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (platform, account_id),
                FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
            ) STRICT
        """,
        "identity_audit": """
            CREATE TABLE identity_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                action TEXT NOT NULL CHECK (action <> ''),
                actor TEXT NOT NULL DEFAULT '',
                person_id INTEGER,
                platform TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT ''
            ) STRICT
        """,
    }

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, ddl in self._TABLE_DDL.items():
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row is None:
                    conn.execute(ddl)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_person_by_account(self, platform: str, account_id: str) -> Optional[dict]:
        """Resolve a platform account to its person row, or None."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.person_id, p.display_name, p.short_note, p.full_profile
                FROM identity_accounts a
                JOIN people p ON p.person_id = a.person_id
                WHERE a.platform = ? AND a.account_id = ?
                """,
                (str(platform), str(account_id)),
            ).fetchone()
        return dict(row) if row else None

    def get_person(self, person_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT person_id, display_name, short_note, full_profile FROM people WHERE person_id=?",
                (int(person_id),),
            ).fetchone()
            accounts = conn.execute(
                "SELECT platform, account_id FROM identity_accounts WHERE person_id=? ORDER BY platform, account_id",
                (int(person_id),),
            ).fetchall()
        if row is None:
            return None
        data = dict(row)
        data["accounts"] = [dict(a) for a in accounts]
        return data

    def upsert_person(self, display_name: str = "", short_note: str = "", *, actor: str = "") -> int:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO people(display_name, short_note, full_profile, created_at, updated_at) VALUES (?,?,?,?,?)",
                (str(display_name), cap_words(short_note), "", now, now),
            )
            person_id = int(cur.lastrowid)
            self._audit(conn, "person.create", actor=actor, person_id=person_id, details=str(display_name))
        return person_id

    def set_short_note(self, person_id: int, note: str, *, actor: str = "") -> str:
        """Replace the short note (capped to SHORT_NOTE_MAX_WORDS); return stored value."""
        capped = cap_words(note)
        with self._connect() as conn:
            conn.execute(
                "UPDATE people SET short_note=?, updated_at=? WHERE person_id=?",
                (capped, time.time(), int(person_id)),
            )
            self._audit(conn, "person.note", actor=actor, person_id=int(person_id))
        return capped

    def append_full_profile(self, person_id: int, text: str, *, actor: str = "") -> None:
        addition = str(text).strip()
        if not addition:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT full_profile FROM people WHERE person_id=?", (int(person_id),)
            ).fetchone()
            existing = str(row["full_profile"]) if row else ""
            merged = f"{existing}\n{addition}".strip() if existing else addition
            if len(merged) > FULL_PROFILE_MAX_CHARS:
                merged = merged[-FULL_PROFILE_MAX_CHARS:]
            conn.execute(
                "UPDATE people SET full_profile=?, updated_at=? WHERE person_id=?",
                (merged, time.time(), int(person_id)),
            )
            self._audit(conn, "person.remember", actor=actor, person_id=int(person_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_or_create_person_for_account(
        self, platform: str, account_id: str, display_name: str = "", *, actor: str = ""
    ) -> dict:
        """Resolve the person for a platform account, creating person+link atomically.

        One ``BEGIN IMMEDIATE`` transaction so two concurrent messages from a
        first-time participant cannot create duplicate people or racing links.
        """
        platform = str(platform)
        account_id = str(account_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT p.person_id, p.display_name, p.short_note, p.full_profile
                FROM identity_accounts a
                JOIN people p ON p.person_id = a.person_id
                WHERE a.platform = ? AND a.account_id = ?
                """,
                (platform, account_id),
            ).fetchone()
            if row is None:
                now = time.time()
                cur = conn.execute(
                    "INSERT INTO people(display_name, short_note, full_profile, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (str(display_name), "", "", now, now),
                )
                person_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO identity_accounts(platform, account_id, person_id, created_at) VALUES (?,?,?,?)",
                    (platform, account_id, person_id, now),
                )
                self._audit(
                    conn, "person.create", actor=actor, person_id=person_id,
                    platform=platform, account_id=account_id, details=str(display_name),
                )
                conn.commit()
                return {"person_id": person_id, "display_name": str(display_name), "short_note": "", "full_profile": ""}
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def link_account(self, platform: str, account_id: str, person_id: int, *, actor: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO identity_accounts(platform, account_id, person_id, created_at) VALUES (?,?,?,?)",
                (str(platform), str(account_id), int(person_id), time.time()),
            )
            self._audit(
                conn, "account.link", actor=actor, person_id=int(person_id),
                platform=str(platform), account_id=str(account_id),
            )

    def _audit(
        self, conn: sqlite3.Connection, action: str, *, actor: str = "",
        person_id: Optional[int] = None, platform: str = "", account_id: str = "", details: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO identity_audit(ts, action, actor, person_id, platform, account_id, details) VALUES (?,?,?,?,?,?,?)",
            (time.time(), str(action), str(actor), person_id, str(platform), str(account_id), str(details)),
        )


def message_prefix_for(
    store: Optional[IdentityStore], platform: str, user_id: str, user_name: str
) -> str:
    """Sender prefix for a shared-thread message.

    Returns ``[Name · note]`` when a profile with a note exists, ``[Name]`` when
    only a name is known, or ``""`` when nothing is known. Fail-soft: any store
    error degrades to the plain ``user_name`` prefix (never breaks delivery).
    """
    name = (user_name or "").strip()
    note = ""
    try:
        if store is not None and platform and user_id:
            person = store.get_person_by_account(str(platform), str(user_id))
            if person:
                name = str(person.get("display_name") or "").strip() or name
                note = cap_words(person.get("short_note") or "")
    except Exception:
        note = ""
    if name and note:
        return f"[{name} · {note}]"
    if name:
        return f"[{name}]"
    return ""
