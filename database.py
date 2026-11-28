import json
import os
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


IS_VERCEL = os.environ.get("VERCEL") == "1"
DB_PATH = os.environ.get("DATABASE_PATH", "/tmp/qslide.db" if IS_VERCEL else "qslide.db")


def _normalize_database_url(value):
    url = str(value or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    try:
        parsed = urlparse(url)
        if parsed.scheme.startswith("postgres") and "sslmode" not in dict(parse_qsl(parsed.query)):
            query = urlencode([*parse_qsl(parsed.query), ("sslmode", "require")])
            url = urlunparse(parsed._replace(query=query))
    except ValueError:
        return url
    return url


DATABASE_URL = _normalize_database_url(os.environ.get("DATABASE_URL", ""))


def using_postgres():
    return bool(DATABASE_URL)


def _sqlite_connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _postgres_connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


class DatabaseConnection:
    def __init__(self):
        self.is_postgres = using_postgres()
        self.connection = _postgres_connect() if self.is_postgres else _sqlite_connect()

    def execute(self, sql, params=()):
        if self.is_postgres:
            sql = sql.replace("?", "%s")
        return self.connection.execute(sql, params)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


def get_db():
    return DatabaseConnection()


def json_param(value):
    if using_postgres():
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    return json.dumps(value)


def parse_json_field(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _ensure_column(db, table, column, definition):
    if using_postgres():
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        return

    existing = {
        row[1]
        for row in db.connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        db.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not using_postgres():
        os.makedirs(db_dir, exist_ok=True)

    db = get_db()
    if using_postgres():
        statements = (
            """
            CREATE TABLE IF NOT EXISTS tutors (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                pin_hash    TEXT NOT NULL,
                created_at  TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id          TEXT PRIMARY KEY,
                tutor_id    TEXT NOT NULL REFERENCES tutors(id),
                title       TEXT,
                questions   JSONB,
                time_limit  INTEGER,
                expires_at  TEXT,
                created_at  TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id           BIGSERIAL PRIMARY KEY,
                quiz_id      TEXT REFERENCES quizzes(id),
                student_name TEXT,
                answers      JSONB,
                score        NUMERIC,
                total        NUMERIC,
                submitted_at TEXT,
                screen_change_count INTEGER DEFAULT 0,
                proctoring_status TEXT DEFAULT 'clear'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS screen_captures (
                id              BIGSERIAL PRIMARY KEY,
                quiz_id         TEXT REFERENCES quizzes(id),
                admission_no    TEXT,
                student_name    TEXT,
                reason          TEXT,
                detection_count INTEGER DEFAULT 0,
                image           BYTEA,
                created_at      TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS screen_captures_quiz_idx ON screen_captures (quiz_id, admission_no)",
            "ALTER TABLE screen_captures ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE tutors ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE quizzes ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE submissions ENABLE ROW LEVEL SECURITY",
        )
        for statement in statements:
            db.execute(statement)
    else:
        db.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tutors (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                pin_hash    TEXT NOT NULL,
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS quizzes (
                id          TEXT PRIMARY KEY,
                tutor_id    TEXT NOT NULL,
                title       TEXT,
                questions   TEXT,
                time_limit  INTEGER,
                expires_at  TEXT,
                created_at  TEXT,
                FOREIGN KEY (tutor_id) REFERENCES tutors(id)
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id      TEXT,
                student_name TEXT,
                answers      TEXT,
                score        INTEGER,
                total        INTEGER,
                submitted_at TEXT,
                screen_change_count INTEGER DEFAULT 0,
                proctoring_status TEXT DEFAULT 'clear'
            );
            CREATE TABLE IF NOT EXISTS screen_captures (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id         TEXT,
                admission_no    TEXT,
                student_name    TEXT,
                reason          TEXT,
                detection_count INTEGER DEFAULT 0,
                image           BLOB,
                created_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS screen_captures_quiz_idx ON screen_captures (quiz_id, admission_no);
            """
        )
    _ensure_column(db, "submissions", "screen_change_count", "INTEGER DEFAULT 0")
    _ensure_column(db, "submissions", "proctoring_status", "TEXT DEFAULT 'clear'")
    _ensure_column(db, "quizzes", "secure_exam", "INTEGER DEFAULT 0")
    _ensure_column(db, "submissions", "admission_no", "TEXT")
    _ensure_column(db, "submissions", "section", "TEXT")
    db.commit()
    db.close()
