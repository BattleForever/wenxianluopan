import sqlite3
from contextlib import contextmanager

from .config import DB_PATH, DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openalex_id TEXT UNIQUE,
    doi TEXT UNIQUE,
    title TEXT,
    title_zh TEXT,
    norm_title TEXT,
    authors TEXT,
    year INTEGER,
    venue TEXT,
    abstract TEXT,
    abstract_zh TEXT,
    type TEXT,
    cited_by_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'candidate',
    oa_pdf_url TEXT,
    local_pdf_path TEXT,
    source TEXT,
    tags TEXT,
    meta_json TEXT,
    fetched_at TEXT,
    added_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_norm_title ON papers(norm_title);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);

CREATE TABLE IF NOT EXISTS citations (
    from_id INTEGER NOT NULL,
    to_id INTEGER NOT NULL,
    src TEXT,
    PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_citations_to ON citations(to_id);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL,
    paper_id INTEGER NOT NULL,
    PRIMARY KEY (collection_id, paper_id)
);

CREATE TABLE IF NOT EXISTS fetch_cache (
    key TEXT PRIMARY KEY,
    url TEXT,
    status INTEGER,
    body TEXT,
    fetched_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS venues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_norm TEXT UNIQUE,
    kind TEXT,
    impact_factor TEXT,
    jcr_zone TEXT,
    cas_zone TEXT,
    cas_top TEXT,
    cas_sub TEXT,
    ccf_tier TEXT,
    source TEXT,
    extra_json TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_venues_norm ON venues(name_norm);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：补齐新增列（列已存在时报 OperationalError，忽略即可）
        try:
            conn.execute("ALTER TABLE papers ADD COLUMN updated_at TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE papers ADD COLUMN title_zh TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE papers ADD COLUMN abstract_zh TEXT")
        except sqlite3.OperationalError:
            pass
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
            )


def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_bool_setting(key, default=False):
    v = get_setting(key)
    if v is None:
        return default
    return v == "1"
