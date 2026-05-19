import json
import os
import sqlite3


IS_VERCEL = os.environ.get("VERCEL") == "1"
DB_PATH = os.environ.get("DATABASE_PATH", "/tmp/qslide.db" if IS_VERCEL else "qslide.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


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
                submitted_at TEXT
            )
            """,
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
                submitted_at TEXT
            );
            """
        )
    db.commit()
    db.close()
