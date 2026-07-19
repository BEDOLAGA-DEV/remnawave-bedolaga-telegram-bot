from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


DELETE_GUARD_SQL = """
CREATE TRIGGER trg_guard_open_grace_subscription_delete
BEFORE DELETE ON subscriptions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM grace_access_sessions
    WHERE subscription_id = OLD.id
      AND state IN ('pending', 'active', 'restoring')
)
BEGIN
    SELECT RAISE(ABORT, 'subscription has an open grace-access session');
END
"""


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=0)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=0')
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY,
            updated_at TEXT
        );
        CREATE TABLE grace_access_sessions (
            id TEXT PRIMARY KEY,
            subscription_id INTEGER NOT NULL
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            state TEXT NOT NULL
        );
        """
    )
    connection.execute(DELETE_GUARD_SQL)
    connection.commit()


def test_sqlite_delete_guard_preserves_open_snapshot_and_cascades_completed_history(tmp_path: Path) -> None:
    database_path = tmp_path / 'grace-delete-guard.sqlite3'
    connection = _connect(database_path)
    _create_schema(connection)
    connection.execute('INSERT INTO subscriptions(id) VALUES (1)')
    connection.execute("INSERT INTO grace_access_sessions(id, subscription_id, state) VALUES ('open', 1, 'active')")
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match='open grace-access session'):
        connection.execute('DELETE FROM subscriptions WHERE id = 1')

    assert connection.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 1
    assert connection.execute('SELECT COUNT(*) FROM grace_access_sessions').fetchone()[0] == 1

    connection.execute("UPDATE grace_access_sessions SET state = 'completed' WHERE id = 'open'")
    connection.execute('DELETE FROM subscriptions WHERE id = 1')
    connection.commit()

    assert connection.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 0
    assert connection.execute('SELECT COUNT(*) FROM grace_access_sessions').fetchone()[0] == 0
    connection.close()


def test_sqlite_predelete_noop_write_blocks_a_concurrent_pending_insert(tmp_path: Path) -> None:
    database_path = tmp_path / 'grace-delete-race.sqlite3'
    guard_connection = _connect(database_path)
    _create_schema(guard_connection)
    guard_connection.execute("INSERT INTO subscriptions(id, updated_at) VALUES (1, 'now')")
    guard_connection.commit()
    candidate_connection = _connect(database_path)

    guard_connection.execute('BEGIN')
    guard_connection.execute('UPDATE subscriptions SET updated_at = updated_at WHERE id = 1')

    with pytest.raises(sqlite3.OperationalError, match='locked'):
        candidate_connection.execute(
            "INSERT INTO grace_access_sessions(id, subscription_id, state) VALUES ('race', 1, 'pending')"
        )

    guard_connection.rollback()
    candidate_connection.execute(
        "INSERT INTO grace_access_sessions(id, subscription_id, state) VALUES ('after', 1, 'pending')"
    )
    candidate_connection.commit()

    candidate_connection.close()
    guard_connection.close()


def test_sqlite_user_lock_blocks_a_new_subscription_during_full_delete(tmp_path: Path) -> None:
    database_path = tmp_path / 'grace-user-delete-race.sqlite3'
    guard_connection = _connect(database_path)
    guard_connection.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
        );
        INSERT INTO users(id) VALUES (1);
        """
    )
    guard_connection.commit()
    creator_connection = _connect(database_path)

    guard_connection.execute('BEGIN')
    guard_connection.execute('UPDATE users SET id = id WHERE id = 1')

    with pytest.raises(sqlite3.OperationalError, match='locked'):
        creator_connection.execute('INSERT INTO subscriptions(id, user_id) VALUES (1, 1)')

    guard_connection.rollback()
    creator_connection.execute('INSERT INTO subscriptions(id, user_id) VALUES (2, 1)')
    creator_connection.commit()

    creator_connection.close()
    guard_connection.close()


def test_sqlite_delete_guard_also_blocks_user_cascade(tmp_path: Path) -> None:
    database_path = tmp_path / 'grace-user-cascade.sqlite3'
    connection = _connect(database_path)
    connection.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE grace_access_sessions (
            id TEXT PRIMARY KEY,
            subscription_id INTEGER NOT NULL
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            state TEXT NOT NULL
        );
        INSERT INTO users(id) VALUES (1);
        INSERT INTO subscriptions(id, user_id) VALUES (1, 1);
        INSERT INTO grace_access_sessions(id, subscription_id, state)
            VALUES ('open', 1, 'restoring');
        """
    )
    connection.execute(DELETE_GUARD_SQL)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match='open grace-access session'):
        connection.execute('DELETE FROM users WHERE id = 1')

    assert connection.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
    assert connection.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 1
    assert connection.execute('SELECT COUNT(*) FROM grace_access_sessions').fetchone()[0] == 1
    connection.close()
