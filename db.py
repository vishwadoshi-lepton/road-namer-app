import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT,
  enriched INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS corridors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
  cor_code TEXT, name TEXT DEFAULT '', suggested TEXT DEFAULT '',
  order_index INTEGER,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
  uuid TEXT, corridor_id INTEGER, seq INTEGER,
  geom TEXT, props TEXT,
  route_name_imported TEXT DEFAULT '', name TEXT DEFAULT '',
  sug_geocode TEXT DEFAULT '', sug_roads TEXT DEFAULT '', twin_uuid TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
  FOREIGN KEY(corridor_id) REFERENCES corridors(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS gcache(k TEXT PRIMARY KEY, v TEXT);
"""

def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()
