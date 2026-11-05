"""
Agenda -- SQLite layer.

Single-file connection helper plus schema migration on startup.

Schema is small: two tables (projects, todos) with parent_id on todos for
future nested rendering. No Alembic, no migrations directory -- if the
schema needs to change later, add a new IF-NOT-EXISTS or ALTER below the
initial CREATE block.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get(
    "AGENDA_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "agenda.db"),
))

# The default board rows ("priority tiers"). Users can rename, add, reorder,
# and delete rows (see board_rows below); these are only the starting set.
# top_of_mind is special: always first, never deleted, manually ordered.
TOP_ROW = "top_of_mind"
DEFAULT_ROWS: tuple[tuple[str, str], ...] = (
    ("top_of_mind", "Top of Mind"),
    ("near_publication", "Near Publication"),
    ("in_development", "In Development"),
    ("early_stage", "Early Stage"),
    ("side_project", "Side Projects"),
    ("future_ideas", "Future Ideas"),
)
# Default row keys, kept for callers that predate editable rows. Live code
# should use row_keys(), which reflects the user's own rows.
CATEGORIES = tuple(key for key, _ in DEFAULT_ROWS)
ROW_LABEL_MAX = 60

# Stored in SQLite's PRAGMA user_version after init_schema(). Bump it whenever
# a migration is added, so the next launch takes a pre-migration backup first.
#   1: editable board rows (board_rows, projects.category CHECK removed)
#   2: settings.points_enabled (effort points can be switched off)
SCHEMA_VERSION = 2
PRE_MIGRATION_RETENTION = 5

SCHEMA_SQL = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  daily_goal      INTEGER NOT NULL DEFAULT 10,
  vel_goal        REAL    NOT NULL DEFAULT 1.0,
  retire_hours    INTEGER NOT NULL DEFAULT 24,
  ip_effort_limit INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS projects (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT    NOT NULL,
  category        TEXT    NOT NULL,
  display_order   INTEGER NOT NULL DEFAULT 0,
  deadline        TEXT,
  notes           TEXT    NOT NULL DEFAULT '',
  paths_json      TEXT    NOT NULL DEFAULT '[]',
  git_repo        TEXT,
  archived        INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  parent_id       INTEGER REFERENCES todos(id) ON DELETE CASCADE,
  text            TEXT    NOT NULL,
  is_blocker      INTEGER NOT NULL DEFAULT 0,
  for_later       INTEGER NOT NULL DEFAULT 0,
  display_order   INTEGER NOT NULL DEFAULT 0,
  in_progress     INTEGER NOT NULL DEFAULT 0,
  completed       INTEGER NOT NULL DEFAULT 0,
  completed_at    TEXT,
  notes           TEXT    NOT NULL DEFAULT '',
  paths_json      TEXT    NOT NULL DEFAULT '[]',
  title_status    TEXT    NOT NULL DEFAULT '',
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_todos_project      ON todos(project_id);
CREATE INDEX IF NOT EXISTS idx_todos_parent       ON todos(parent_id);
CREATE INDEX IF NOT EXISTS idx_todos_completed_at ON todos(completed_at);

CREATE TABLE IF NOT EXISTS board_rows (
  key             TEXT    PRIMARY KEY,
  label           TEXT    NOT NULL,
  position        INTEGER NOT NULL,
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS day_overrides (
  date TEXT PRIMARY KEY   -- ISO date YYYY-MM-DD; presence = "flipped" from natural state
);
"""


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """Return the lazily-initialized module-level connection."""
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def _migrate_rename_followup_to_for_later(conn: sqlite3.Connection) -> None:
    """Rename the misleading Later-section flag without changing its values."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "is_followup" in cols and "for_later" not in cols:
        conn.execute("ALTER TABLE todos RENAME COLUMN is_followup TO for_later")


def create_backup(
    backup_dir: Path | None = None,
    retention: int | None = None,
    prefix: str = "agenda",
) -> Path:
    """Create and verify a self-contained online backup of the Agenda DB.

    A separate source connection keeps the module-level request connection out
    of the backup thread. The temporary destination is collapsed from WAL to
    DELETE mode before publication so one .db file is sufficient to restore.
    """
    target_dir = backup_dir or Path(
        os.environ.get(
            "AGENDA_BACKUP_DIR",
            str(DB_PATH.parent / "backups"),
        )
    )
    keep = retention if retention is not None else int(
        os.environ.get("AGENDA_BACKUP_RETENTION", "30")
    )
    keep = max(1, keep)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = target_dir / f"{prefix}-{timestamp}.db"

    fd, temporary_name = tempfile.mkstemp(
        prefix=".agenda-backup-",
        suffix=".db",
        dir=target_dir,
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        source = sqlite3.connect(str(DB_PATH), timeout=30)
        destination = sqlite3.connect(str(temporary_path), timeout=30)
        try:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"backup integrity check failed: {integrity!r}"
                )
        finally:
            destination.close()
            source.close()
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    backups = sorted(
        target_dir.glob(f"{prefix}-*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink(missing_ok=True)
    return final_path


def _migrate_add_todo_context(conn: sqlite3.Connection) -> None:
    """Idempotent: add notes + paths_json to existing todos tables."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "notes" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN notes TEXT NOT NULL DEFAULT ''"
        )
    if "paths_json" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN paths_json TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_add_todo_inprogress(conn: sqlite3.Connection) -> None:
    """Idempotent: add in_progress column to existing todos tables."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "in_progress" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN in_progress INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_add_todo_effort(conn: sqlite3.Connection) -> None:
    """Idempotent: add effort column to existing todos tables."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "effort" not in cols:
        conn.execute("ALTER TABLE todos ADD COLUMN effort INTEGER")


def _migrate_add_sync_identity(conn: sqlite3.Connection) -> None:
    """Add stable cross-device identity without changing local integer keys."""
    project_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    todo_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()
    }
    conn.execute("BEGIN")
    try:
        if "stable_uuid" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN stable_uuid TEXT")
        if "sync_deleted_at" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN sync_deleted_at TEXT")
        if "stable_uuid" not in todo_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN stable_uuid TEXT")
        if "sync_deleted_at" not in todo_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN sync_deleted_at TEXT")

        for table in ("projects", "todos"):
            missing = conn.execute(
                f"SELECT id FROM {table} WHERE stable_uuid IS NULL OR stable_uuid = ''"
            ).fetchall()
            conn.executemany(
                f"UPDATE {table} SET stable_uuid = ? WHERE id = ?",
                [(str(uuid.uuid4()), row["id"]) for row in missing],
            )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_stable_uuid "
            "ON projects(stable_uuid)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_stable_uuid "
            "ON todos(stable_uuid)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.execute(
            "INSERT INTO sync_metadata (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("wire_schema_version", "1", now),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _migrate_add_sync_tombstones(conn: sqlite3.Connection) -> None:
    """Add revisioned, recoverable deletion storage without rewriting live rows."""
    project_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    todo_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()
    }
    conn.execute("BEGIN")
    try:
        if "sync_revision" not in project_cols:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN sync_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "sync_revision" not in todo_cols:
            conn.execute(
                "ALTER TABLE todos ADD COLUMN sync_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_tombstones (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_kind         TEXT NOT NULL
                                    CHECK (entity_kind IN ('project', 'todo')),
                stable_uuid         TEXT NOT NULL,
                project_stable_uuid TEXT,
                parent_stable_uuid  TEXT,
                revision            INTEGER NOT NULL CHECK (revision > 0),
                deleted_at          TEXT NOT NULL,
                payload_json        TEXT NOT NULL,
                restored_at         TEXT,
                published_at        TEXT,
                UNIQUE (entity_kind, stable_uuid, revision)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_tombstones_active "
            "ON sync_tombstones(entity_kind, stable_uuid) "
            "WHERE restored_at IS NULL"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _migrate_add_sync_structure_revision(conn: sqlite3.Connection) -> None:
    """Add the project-scoped hierarchy/reorder conflict counter."""
    project_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    if "sync_structure_revision" not in project_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN sync_structure_revision "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_add_sync_recovery(conn: sqlite3.Connection) -> None:
    """Add durable proposal, outcome, conflict, and retry state."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sync_mutations (
            mutation_id       TEXT PRIMARY KEY,
            fingerprint       TEXT NOT NULL,
            device_id         TEXT NOT NULL,
            entity_kind       TEXT NOT NULL
                              CHECK (entity_kind IN ('project', 'todo')),
            entity_uuid       TEXT NOT NULL,
            operation         TEXT NOT NULL,
            payload_json      TEXT NOT NULL,
            received_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_results (
            mutation_id       TEXT PRIMARY KEY
                              REFERENCES sync_mutations(mutation_id),
            fingerprint       TEXT NOT NULL,
            result_json       TEXT NOT NULL,
            created_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            mutation_id            TEXT NOT NULL,
            entity_kind            TEXT NOT NULL,
            entity_uuid            TEXT NOT NULL,
            reason                 TEXT NOT NULL,
            canonical_payload_json TEXT,
            proposed_payload_json  TEXT,
            created_at             TEXT NOT NULL,
            resolved_at            TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sync_conflicts_open
          ON sync_conflicts(entity_kind, entity_uuid)
          WHERE resolved_at IS NULL;
        CREATE TABLE IF NOT EXISTS sync_outbound_records (
            owner             TEXT NOT NULL,
            record_name       TEXT NOT NULL,
            record_type       TEXT NOT NULL,
            record_data       BLOB NOT NULL,
            semantic_hash     TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            acknowledged_at   TEXT,
            PRIMARY KEY (owner, record_name)
        );
        CREATE TABLE IF NOT EXISTS sync_cycle_checkpoints (
            owner             TEXT PRIMARY KEY,
            cycle_id          TEXT NOT NULL,
            phase             TEXT NOT NULL,
            payload_json      TEXT NOT NULL DEFAULT '{}',
            updated_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_account_state (
            owner             TEXT PRIMARY KEY,
            account_marker    TEXT,
            quarantined       INTEGER NOT NULL DEFAULT 0,
            updated_at        TEXT NOT NULL
        );
    """)


def _migrate_add_sync_revision_triggers(conn: sqlite3.Connection) -> None:
    """Advance canonical revisions for ordinary Mac/API edits."""
    conn.executescript("""
        DROP TRIGGER IF EXISTS projects_sync_revision_after_update;
        CREATE TRIGGER projects_sync_revision_after_update
        AFTER UPDATE ON projects
        WHEN NEW.sync_revision = OLD.sync_revision AND (
            NEW.name IS NOT OLD.name OR
            NEW.category IS NOT OLD.category OR
            NEW.display_order IS NOT OLD.display_order OR
            NEW.notes IS NOT OLD.notes OR
            NEW.northstars IS NOT OLD.northstars OR
            NEW.archived IS NOT OLD.archived
        )
        BEGIN
            UPDATE projects
            SET sync_revision = OLD.sync_revision + 1
            WHERE id = NEW.id;
        END;

        DROP TRIGGER IF EXISTS todos_sync_revision_after_update;
        CREATE TRIGGER todos_sync_revision_after_update
        AFTER UPDATE ON todos
        WHEN NEW.sync_revision = OLD.sync_revision AND (
            NEW.project_id IS NOT OLD.project_id OR
            NEW.parent_id IS NOT OLD.parent_id OR
            NEW.text IS NOT OLD.text OR
            NEW.is_blocker IS NOT OLD.is_blocker OR
            NEW.for_later IS NOT OLD.for_later OR
            NEW.in_progress IS NOT OLD.in_progress OR
            NEW.flagged IS NOT OLD.flagged OR
            NEW.awaiting_review IS NOT OLD.awaiting_review OR
            NEW.display_order IS NOT OLD.display_order OR
            NEW.completed IS NOT OLD.completed OR
            NEW.completed_at IS NOT OLD.completed_at OR
            NEW.notes IS NOT OLD.notes OR
            NEW.effort IS NOT OLD.effort OR
            NEW.deadline IS NOT OLD.deadline
        )
        BEGIN
            UPDATE todos
            SET sync_revision = OLD.sync_revision + 1
            WHERE id = NEW.id;
        END;
    """)


def _migrate_rekey_schema2_launch_revisions(
    conn: sqlite3.Connection,
) -> None:
    """Rekey mutable launch-era rows onto fresh immutable canonical IDs once."""
    migration = "rekey-schema2-launch-revisions-v1"
    lower = "2026-08-09T00:00:00+00:00"
    upper = "2026-08-13T19:40:00+00:00"
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM sync_migrations WHERE name = ?",
        (migration,),
    ).fetchone():
        return
    conn.execute("SAVEPOINT rekey_schema2_launch")
    try:
        conn.execute(
            """
            UPDATE projects
            SET sync_revision = sync_revision + 1
            WHERE created_at >= ?
              AND created_at < ?
            """,
            (lower, upper),
        )
        conn.execute(
            """
            UPDATE todos
            SET sync_revision = sync_revision + 1
            WHERE created_at >= ?
              AND created_at < ?
            """,
            (lower, upper),
        )
        conn.execute(
            "INSERT INTO sync_migrations (name, applied_at) "
            "VALUES (?, datetime('now'))",
            (migration,),
        )
        conn.execute("RELEASE rekey_schema2_launch")
    except Exception:
        conn.execute("ROLLBACK TO rekey_schema2_launch")
        conn.execute("RELEASE rekey_schema2_launch")
        raise


def _migrate_add_day_overrides(conn: sqlite3.Connection) -> None:
    """Idempotent: create day_overrides table if it doesn't exist yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS day_overrides (
            date TEXT PRIMARY KEY
        )
    """)


def _migrate_add_title_status(conn: sqlite3.Connection) -> None:
    """Idempotent: add title_status column to todos for LLM-generated titles."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "title_status" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN title_status TEXT NOT NULL DEFAULT ''"
        )


def _migrate_add_suggestions(conn: sqlite3.Connection) -> None:
    """Idempotent: add suggestions columns to todos for LLM sub-item suggestions."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "suggestions_json" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN suggestions_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "suggestions_status" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN suggestions_status TEXT NOT NULL DEFAULT ''"
        )


def _migrate_add_project_git_repo(conn: sqlite3.Connection) -> None:
    """Idempotent: add git_repo column to projects for git-tracked projects."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "git_repo" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN git_repo TEXT")


def _migrate_add_todo_flagged(conn: sqlite3.Connection) -> None:
    """Idempotent: add flagged column to todos for bookmark-style priority flagging."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "flagged" not in cols:
        conn.execute(
            "ALTER TABLE todos ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_add_top_of_mind_category(conn: sqlite3.Connection) -> None:
    """Idempotent: widen projects.category CHECK to include top_of_mind.

    SQLite does not support ALTER TABLE to change CHECK constraints, so we
    recreate the projects table with the widened constraint via the standard
    create-copy-drop-rename pattern.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if row is None or "CHECK (category IN" not in row[0] or "top_of_mind" in row[0]:
        return  # already migrated, or the CHECK is gone (editable rows)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE projects_new (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              name            TEXT    NOT NULL,
              category        TEXT    NOT NULL CHECK (category IN
                                ('top_of_mind','near_publication','in_development',
                                 'early_stage','side_project')),
              display_order   INTEGER NOT NULL DEFAULT 0,
              deadline        TEXT,
              notes           TEXT    NOT NULL DEFAULT '',
              paths_json      TEXT    NOT NULL DEFAULT '[]',
              archived        INTEGER NOT NULL DEFAULT 0,
              created_at      TEXT    NOT NULL,
              updated_at      TEXT    NOT NULL
            )
        """)
        conn.execute("INSERT INTO projects_new SELECT * FROM projects")
        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_new RENAME TO projects")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_add_future_ideas_category(conn: sqlite3.Connection) -> None:
    """Idempotent: widen projects.category CHECK to include future_ideas."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if row is None or "CHECK (category IN" not in row[0] or "future_ideas" in row[0]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE projects_new (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              name            TEXT    NOT NULL,
              category        TEXT    NOT NULL CHECK (category IN
                                ('top_of_mind','near_publication','in_development',
                                 'early_stage','side_project','future_ideas')),
              display_order   INTEGER NOT NULL DEFAULT 0,
              deadline        TEXT,
              notes           TEXT    NOT NULL DEFAULT '',
              paths_json      TEXT    NOT NULL DEFAULT '[]',
              archived        INTEGER NOT NULL DEFAULT 0,
              created_at      TEXT    NOT NULL,
              updated_at      TEXT    NOT NULL
            )
        """)
        conn.execute("INSERT INTO projects_new SELECT * FROM projects")
        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_new RENAME TO projects")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_notes_to_json_array(conn: sqlite3.Connection) -> None:
    """Idempotent: convert notes TEXT blobs to JSON arrays of strings.

    Rows that already contain a valid JSON array are left untouched.
    Rows with a non-empty blob become ["<blob>"].
    Rows with an empty string become [].
    """
    for table in ("projects", "todos"):
        rows = conn.execute(f"SELECT id, notes FROM {table}").fetchall()
        for r in rows:
            raw = r["notes"] or ""
            try:
                val = json.loads(raw)
                if isinstance(val, list):
                    continue
            except Exception:
                pass
            new_val = json.dumps([raw]) if raw.strip() else "[]"
            conn.execute(f"UPDATE {table} SET notes = ? WHERE id = ?", (new_val, r["id"]))


def _migrate_add_settings_table(conn: sqlite3.Connection) -> None:
    """Idempotent: create settings table and ensure the single row exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            daily_goal      INTEGER NOT NULL DEFAULT 10,
            vel_goal        REAL    NOT NULL DEFAULT 1.0,
            retire_hours    INTEGER NOT NULL DEFAULT 24,
            ip_effort_limit INTEGER NOT NULL DEFAULT 100
        )
    """)
    conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")


def _migrate_add_todo_process_statuses(conn: sqlite3.Connection) -> None:
    """Idempotent: add running and awaiting_review status columns to todos."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "running" not in cols:
        conn.execute("ALTER TABLE todos ADD COLUMN running INTEGER NOT NULL DEFAULT 0")
    if "awaiting_review" not in cols:
        conn.execute("ALTER TABLE todos ADD COLUMN awaiting_review INTEGER NOT NULL DEFAULT 0")


def _migrate_add_message_feedback(conn: sqlite3.Connection) -> None:
    """Idempotent: add feedback_rating and feedback_comment to messages."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "feedback_rating" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN feedback_rating TEXT")
    if "feedback_comment" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN feedback_comment TEXT")


def _migrate_add_todo_deadline(conn: sqlite3.Connection) -> None:
    """Idempotent: add deadline column to todos for task-level deadline setting."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "deadline" not in cols:
        conn.execute("ALTER TABLE todos ADD COLUMN deadline TEXT")


def _migrate_project_deadlines_to_todos(conn: sqlite3.Connection) -> None:
    """Idempotent: for each project with a non-null deadline, create a placeholder
    todo carrying that date, then clear the project-level deadline.

    Guard: if no projects have a non-null deadline, there is nothing to do.
    The ALTER that added todos.deadline runs first, so we can safely write to it.
    """
    from datetime import datetime, timezone

    projects_with_deadline = conn.execute(
        "SELECT id, deadline FROM projects WHERE deadline IS NOT NULL"
    ).fetchall()
    if not projects_with_deadline:
        return

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for row in projects_with_deadline:
        conn.execute(
            "INSERT INTO todos "
            "(project_id, parent_id, text, deadline, is_blocker, for_later, "
            " display_order, in_progress, completed, notes, paths_json, "
            " title_status, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 0, 0, -10, 0, 0, '[]', '[]', '', ?, ?)",
            (row["id"], "untitled deadline task from migration", row["deadline"], now, now),
        )
        conn.execute(
            "UPDATE projects SET deadline = NULL WHERE id = ?",
            (row["id"],),
        )


def _migrate_add_project_northstars(conn: sqlite3.Connection) -> None:
    """Idempotent: add northstars column to projects for durable goals + principles."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "northstars" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN northstars TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_notes_to_entries(conn: sqlite3.Connection) -> None:
    """Idempotent: convert notes JSON arrays from bare strings to {ts, author, text} dicts.

    Rows where all entries are already dicts (have a "text" key) are left untouched.
    Bare string entries are wrapped using the row's created_at as the timestamp.
    """
    for table in ("projects", "todos"):
        rows = conn.execute(f"SELECT id, notes, created_at FROM {table}").fetchall()
        for r in rows:
            raw = r["notes"] or "[]"
            try:
                items = json.loads(raw)
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
            # Check if any entry is a bare string (needs wrapping)
            needs_wrap = any(isinstance(item, str) for item in items)
            if not needs_wrap:
                continue
            created_ts = r["created_at"] or ""
            new_items = []
            for item in items:
                if isinstance(item, dict) and "text" in item:
                    new_items.append(item)
                elif isinstance(item, str):
                    new_items.append({"ts": created_ts, "author": "human", "text": item})
                # skip malformed entries
            conn.execute(
                f"UPDATE {table} SET notes = ? WHERE id = ?",
                (json.dumps(new_items), r["id"]),
            )


def _migrate_add_conversation_tables(conn: sqlite3.Connection) -> None:
    """Idempotent: create conversations and messages tables for the assistant."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          title       TEXT    NOT NULL DEFAULT 'New conversation',
          created_at  TEXT    NOT NULL,
          updated_at  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
          id                 INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_id    INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          role               TEXT    NOT NULL,
          content            TEXT,
          tool_calls_json    TEXT,
          tool_call_id       TEXT,
          tool_name          TEXT,
          tool_result_json   TEXT,
          tool_result_status TEXT,
          parent_message_id  INTEGER,
          created_at         TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON messages(conversation_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_parent
        ON messages(parent_message_id)
    """)


def _migrate_add_insight_tables(conn: sqlite3.Connection) -> None:
    """Idempotent: cache grounded insight runs and user dismissal state."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insight_cache (
          mode          TEXT PRIMARY KEY,
          source_hash   TEXT NOT NULL,
          provider      TEXT NOT NULL,
          insights_json TEXT NOT NULL,
          generated_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insight_states (
          insight_id     TEXT PRIMARY KEY,
          dismissed_at   TEXT,
          snoozed_until  TEXT,
          updated_at     TEXT NOT NULL
        )
    """)


def get_settings() -> dict:
    """Return the single settings row as a dict."""
    row = get_conn().execute("SELECT * FROM settings WHERE id = 1").fetchone()
    if row is None:
        return {"daily_goal": 10, "vel_goal": 1.0, "retire_hours": 24, "ip_effort_limit": 100,
                "onboarding_pending": False, "points_enabled": True}
    return {
        "daily_goal": row["daily_goal"],
        "vel_goal": row["vel_goal"],
        "retire_hours": row["retire_hours"],
        "ip_effort_limit": row["ip_effort_limit"],
        "onboarding_pending": bool(row["onboarding_pending"]) if "onboarding_pending" in row.keys() else False,
        "points_enabled": bool(row["points_enabled"]) if "points_enabled" in row.keys() else True,
    }


def patch_settings(data: dict) -> dict:
    """Update zero or more settings fields; return the updated row."""
    conn = get_conn()
    fields: list[str] = []
    values: list = []
    if "daily_goal" in data:
        fields.append("daily_goal = ?")
        values.append(int(data["daily_goal"]))
    if "vel_goal" in data:
        fields.append("vel_goal = ?")
        values.append(float(data["vel_goal"]))
    if "retire_hours" in data:
        fields.append("retire_hours = ?")
        values.append(int(data["retire_hours"]))
    if "ip_effort_limit" in data:
        fields.append("ip_effort_limit = ?")
        values.append(int(data["ip_effort_limit"]))
    if "points_enabled" in data:
        fields.append("points_enabled = ?")
        values.append(1 if data["points_enabled"] else 0)
    if fields:
        conn.execute(
            f"UPDATE settings SET {', '.join(fields)} WHERE id = 1",
            values,
        )
    return get_settings()


class PreMigrationBackupError(RuntimeError):
    """The database needs migrating but couldn't be backed up first."""


def _stored_schema_version(path: Path) -> int | None:
    """user_version of an existing Agenda database, or None if there is none."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        has_projects = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone()
        if not has_projects:
            return None
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def backup_before_migration() -> Path | None:
    """Snapshot an existing database before migrations change it.

    Runs only when the database predates this code's SCHEMA_VERSION. Its
    snapshots (pre-migration-v<old>-to-v<new>-<time>.db) are pruned
    separately from the rolling backups, so routine backups never push them
    out. If the snapshot fails, nothing is migrated: the caller should stop
    rather than change an unprotected database.
    """
    stored = _stored_schema_version(DB_PATH)
    if stored is None or stored >= SCHEMA_VERSION:
        return None
    try:
        return create_backup(
            retention=PRE_MIGRATION_RETENTION,
            prefix=f"pre-migration-v{stored}-to-v{SCHEMA_VERSION}",
        )
    except Exception as exc:
        raise PreMigrationBackupError(
            "Agenda needs to update its database but could not back it up first, "
            f"so nothing was changed. ({exc})"
        ) from exc


def init_schema() -> None:
    """Apply schema (idempotent), backing up first if migrations will run."""
    backup_before_migration()
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    _migrate_rename_followup_to_for_later(conn)
    _migrate_add_todo_context(conn)
    _migrate_add_todo_inprogress(conn)
    _migrate_add_todo_effort(conn)
    _migrate_add_top_of_mind_category(conn)
    _migrate_add_future_ideas_category(conn)
    _migrate_notes_to_json_array(conn)
    _migrate_add_project_northstars(conn)
    _migrate_notes_to_entries(conn)
    _migrate_add_day_overrides(conn)
    _migrate_add_title_status(conn)
    _migrate_add_suggestions(conn)
    _migrate_add_project_git_repo(conn)
    _migrate_add_todo_flagged(conn)
    _migrate_add_settings_table(conn)
    _migrate_add_conversation_tables(conn)
    _migrate_add_insight_tables(conn)
    _migrate_add_todo_process_statuses(conn)
    _migrate_add_message_feedback(conn)
    _migrate_add_todo_deadline(conn)
    _migrate_project_deadlines_to_todos(conn)
    _migrate_add_sync_identity(conn)
    _migrate_add_sync_tombstones(conn)
    _migrate_add_sync_structure_revision(conn)
    _migrate_add_sync_recovery(conn)
    _migrate_add_sync_revision_triggers(conn)
    _migrate_rekey_schema2_launch_revisions(conn)
    _migrate_add_usage_tables(conn)
    _migrate_add_quota_tables(conn)
    _migrate_add_board_rows(conn)
    _migrate_drop_project_category_check(conn)
    _migrate_add_onboarding_flag(conn)
    _migrate_add_points_enabled(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_add_quota_tables(conn: sqlite3.Connection) -> None:
    """Usage-limit pacing observations (Mac-local; never part of CloudKit sync)."""
    import usage_quota  # local import: usage_quota imports db

    usage_quota.ensure_schema(conn)


def _migrate_add_usage_tables(conn: sqlite3.Connection) -> None:
    """AI usage ledger tables (Mac-local; never part of CloudKit sync)."""
    import usage_ledger  # local import: usage_ledger imports db

    usage_ledger.ensure_schema(conn)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Editable board rows ("priority tiers")
# ---------------------------------------------------------------------------

def _migrate_add_board_rows(conn: sqlite3.Connection) -> None:
    """Idempotent: create board_rows and seed it with the default rows.

    Any category already used by a project but missing from the table (only
    possible after a manual edit) is appended, so no project is orphaned.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS board_rows (
          key             TEXT    PRIMARY KEY,
          label           TEXT    NOT NULL,
          position        INTEGER NOT NULL,
          created_at      TEXT    NOT NULL,
          updated_at      TEXT    NOT NULL
        )
    """)
    if conn.execute("SELECT COUNT(*) FROM board_rows").fetchone()[0] == 0:
        now = _row_now()
        conn.executemany(
            "INSERT INTO board_rows (key, label, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(key, label, i, now, now) for i, (key, label) in enumerate(DEFAULT_ROWS)],
        )
    known = {r[0] for r in conn.execute("SELECT key FROM board_rows").fetchall()}
    used = [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM projects ORDER BY category").fetchall()]
    for key in used:
        if key not in known:
            pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM board_rows").fetchone()[0]
            now = _row_now()
            conn.execute(
                "INSERT INTO board_rows (key, label, position, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, key.replace("_", " ").title(), pos, now, now),
            )
            known.add(key)


def _migrate_drop_project_category_check(conn: sqlite3.Connection) -> None:
    """Idempotent: remove the fixed CHECK on projects.category.

    Rows are now user-defined, so valid categories live in board_rows and are
    enforced by the API. SQLite can't drop a CHECK in place, so this rebuilds
    the table from its *live* definition (whatever columns earlier migrations
    added), copies every row, and recreates the table's indexes and triggers.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if row is None or "CHECK (category IN" not in row[0]:
        return
    create_sql = row[0]
    start = create_sql.index("CHECK (category IN")
    depth = 0
    end = None
    for i in range(start + len("CHECK "), len(create_sql)):
        ch = create_sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise RuntimeError("could not parse the projects.category CHECK constraint")
    new_sql = create_sql[:start].rstrip() + create_sql[end:]
    new_sql = 'CREATE TABLE "projects_new" ' + new_sql[new_sql.index("("):]
    extras = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='projects' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type, name"
    ).fetchall()]
    columns = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    col_list = ", ".join(f'"{c}"' for c in columns)

    seq_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'projects'").fetchone()

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN")
        conn.execute(new_sql)
        conn.execute(
            f"INSERT INTO projects_new ({col_list}) SELECT {col_list} FROM projects")
        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_new RENAME TO projects")
        for sql in extras:
            conn.execute(sql)
        if seq_row is not None:
            # Keep AUTOINCREMENT from reusing ids of deleted projects.
            conn.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'projects'",
                (seq_row[0],))
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(
                f"foreign key check failed after rebuilding projects: {len(problems)} rows")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_add_onboarding_flag(conn: sqlite3.Connection) -> None:
    """Idempotent: settings.onboarding_pending (1 = show the welcome tour).

    Defaults to 0, so existing databases never see the tour. seed.py sets it
    on a brand-new database.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "onboarding_pending" not in cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN onboarding_pending INTEGER NOT NULL DEFAULT 0")


def _migrate_add_points_enabled(conn: sqlite3.Connection) -> None:
    """Idempotent: settings.points_enabled (1 = effort points are used).

    Defaults to 1, so boards that already exist keep using their points.
    seed.py turns it off for brand-new boards. Switching it off never touches
    todos.effort; every task simply counts as 1 while it's off.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "points_enabled" not in cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN points_enabled INTEGER NOT NULL DEFAULT 1")


def points_enabled(conn: sqlite3.Connection | None = None) -> bool:
    """Whether effort points are in use (True for boards from before the setting)."""
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT points_enabled FROM settings WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return True
    return True if row is None else bool(row[0])


def _row_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RowError(ValueError):
    """A row edit that can't be applied; the message is shown to the user."""


def list_rows(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Rows in display order, with active/archived project counts."""
    conn = conn or get_conn()
    counts = {
        r["category"]: (r["active"], r["archived"])
        for r in conn.execute(
            "SELECT category, SUM(archived = 0) AS active, SUM(archived = 1) AS archived "
            "FROM projects GROUP BY category"
        ).fetchall()
    }
    out = []
    for r in conn.execute(
        "SELECT key, label, position FROM board_rows ORDER BY position, key"
    ).fetchall():
        active, archived = counts.get(r["key"], (0, 0))
        out.append({
            "key": r["key"],
            "label": r["label"],
            "position": r["position"],
            "locked": r["key"] == TOP_ROW,
            "project_count": int(active or 0),
            "archived_count": int(archived or 0),
        })
    return out


def row_keys(conn: sqlite3.Connection | None = None) -> list[str]:
    conn = conn or get_conn()
    return [r[0] for r in conn.execute(
        "SELECT key FROM board_rows ORDER BY position, key").fetchall()]


def row_labels(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    conn = conn or get_conn()
    return {r[0]: r[1] for r in conn.execute("SELECT key, label FROM board_rows").fetchall()}


def _clean_label(label: object) -> str:
    text = " ".join(str(label or "").split())
    if not text:
        raise RowError("Row names can't be empty.")
    if len(text) > ROW_LABEL_MAX:
        raise RowError(f"Row names can be at most {ROW_LABEL_MAX} characters.")
    return text


def _new_row_key(label: str, taken: set[str]) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower())
    slug = "_".join(part for part in slug.split("_") if part)[:40] or "row"
    key = slug
    n = 2
    while key in taken:
        key = f"{slug}_{n}"
        n += 1
    return key


def apply_row_plan(rows: list[dict], deletions: list[dict] | None = None) -> list[dict]:
    """Apply a whole row-editor session atomically.

    rows: the desired rows, top to bottom. Each is {"key": <existing key>,
      "label": ...} or {"id": "new:<n>", "label": ...} for a new row.
    deletions: [{"key": <existing key>, "move_to": <key or "new:<n>">}]. Every
      existing row missing from `rows` must appear here. Its projects (active
      and archived) move to the end of the target row.

    Raises RowError, changing nothing, when the plan is invalid.
    """
    conn = get_conn()
    existing = {r["key"]: r for r in list_rows(conn)}
    if not isinstance(rows, list) or not rows:
        raise RowError("The board needs at least one row.")

    labels_seen: set[str] = set()
    temp_ids: set[str] = set()
    kept: list[str] = []
    for item in rows:
        label = _clean_label(item.get("label"))
        folded = label.casefold()
        if folded in labels_seen:
            raise RowError(f'Two rows are both named "{label}".')
        labels_seen.add(folded)
        key = item.get("key")
        if key is not None:
            if key not in existing:
                raise RowError(
                    "One of these rows no longer exists. Reopen the editor and try again.")
            if key in kept:
                raise RowError("A row appears twice.")
            kept.append(key)
        else:
            temp = str(item.get("id") or "")
            if not temp.startswith("new:") or temp in temp_ids:
                raise RowError("Each new row needs its own temporary id.")
            temp_ids.add(temp)

    if rows[0].get("key") != TOP_ROW:
        raise RowError("Top of Mind always stays the first row.")

    deleted: dict[str, str] = {}
    for d in deletions or []:
        key = d.get("key")
        target = d.get("move_to")
        if key not in existing:
            raise RowError("One of the rows to delete no longer exists.")
        if key == TOP_ROW:
            raise RowError("The Top of Mind row can't be deleted.")
        if key in kept:
            raise RowError("A row can't be both kept and deleted.")
        if target == key or (target not in kept and target not in temp_ids):
            raise RowError(
                f'Choose where the projects in "{existing[key]["label"]}" should go.')
        deleted[key] = target
    missing = set(existing) - set(kept) - set(deleted)
    if missing:
        names = ", ".join(sorted(existing[k]["label"] for k in missing))
        raise RowError(f"Choose where the projects in {names} should go.")

    now = _row_now()
    with transaction() as tx:
        taken = set(existing)
        temp_to_key: dict[str, str] = {}
        for item in rows:
            if item.get("key") is None:
                label = _clean_label(item["label"])
                key = _new_row_key(label, taken)
                taken.add(key)
                temp_to_key[str(item["id"])] = key
                tx.execute(
                    "INSERT INTO board_rows (key, label, position, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, label, 10_000 + len(temp_to_key), now, now),
                )
        for old_key, target in deleted.items():
            target_key = temp_to_key.get(target, target)
            base = tx.execute(
                "SELECT COALESCE(MAX(display_order), -1) + 1 FROM projects WHERE category = ?",
                (target_key,),
            ).fetchone()[0]
            moving = tx.execute(
                "SELECT id FROM projects WHERE category = ? ORDER BY display_order, id",
                (old_key,),
            ).fetchall()
            for offset, project in enumerate(moving):
                tx.execute(
                    "UPDATE projects SET category = ?, display_order = ?, updated_at = ? "
                    "WHERE id = ?",
                    (target_key, base + offset, now, project["id"]),
                )
            tx.execute("DELETE FROM board_rows WHERE key = ?", (old_key,))
        for position, item in enumerate(rows):
            key = item.get("key") or temp_to_key[str(item["id"])]
            label = _clean_label(item["label"])
            tx.execute(
                "UPDATE board_rows SET "
                "updated_at = CASE WHEN label IS ? AND position IS ? THEN updated_at ELSE ? END, "
                "label = ?, position = ? WHERE key = ?",
                (label, position, now, label, position, key),
            )
    return list_rows(conn)


def set_onboarding_pending(pending: bool) -> None:
    get_conn().execute(
        "UPDATE settings SET onboarding_pending = ? WHERE id = 1", (1 if pending else 0,))
