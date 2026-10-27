"""SQLite storage. One connection, guarded by a lock; the station is small."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '',
  fs_uuid TEXT NOT NULL DEFAULT '',
  fstype TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  flow_id INTEGER,
  registered INTEGER NOT NULL DEFAULT 0,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  last_slot TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS flows (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100,
  graph TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS destinations (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- 'local' | 'rclone'
  provider TEXT NOT NULL,        -- local, nextcloud, drive, dropbox, webdav, sftp, smb, s3, rclone
  config TEXT NOT NULL,          -- JSON: {path} or {remote, root}
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY,
  card_id INTEGER NOT NULL,
  flow_id INTEGER,
  flow_name TEXT NOT NULL DEFAULT '',
  flow_snapshot TEXT NOT NULL DEFAULT '{}',
  slot TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL,
  files_total INTEGER NOT NULL DEFAULT 0,
  bytes_total INTEGER NOT NULL DEFAULT 0,
  files_new INTEGER NOT NULL DEFAULT 0,
  files_deleted INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL,
  card_id INTEGER NOT NULL,
  rel_path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '',
  md5 TEXT NOT NULL DEFAULT '',
  sha1 TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',   -- pending, secured, deleted, kept, failed
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS files_by_import ON files(import_id);
CREATE INDEX IF NOT EXISTS files_by_card_path ON files(card_id, rel_path, size, mtime);
CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  destination_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  method TEXT NOT NULL,
  import_id INTEGER NOT NULL,
  verified_at REAL NOT NULL,
  UNIQUE(sha256, destination_id, path)
);
CREATE INDEX IF NOT EXISTS receipts_by_hash ON receipts(sha256, destination_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  card_id INTEGER,
  import_id INTEGER
);
"""


class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    # settings ---------------------------------------------------------------
    def get(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def event(self, level, message, card_id=None, import_id=None):
        self.x("INSERT INTO events(ts,level,message,card_id,import_id) VALUES(?,?,?,?,?)",
               (time.time(), level, message, card_id, import_id))
        self.x("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 2000")
