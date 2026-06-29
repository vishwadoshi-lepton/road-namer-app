import sqlite3, json

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
  merged_into TEXT DEFAULT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
  FOREIGN KEY(corridor_id) REFERENCES corridors(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS merge_members(
  project_id INTEGER, merge_uuid TEXT, member_uuid TEXT, seq INTEGER,
  PRIMARY KEY(project_id, merge_uuid, member_uuid),
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS gcache(k TEXT PRIMARY KEY, v TEXT);
"""

def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    # idempotent migration: add merged_into to DBs created before this column existed
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE segments ADD COLUMN merged_into TEXT DEFAULT NULL")
    # Back-fill merge_members from legacy props.merged_from (idempotent, additive).
    for r in conn.execute("SELECT project_id, uuid, props FROM segments"):
        try:
            mf = json.loads(r["props"] or "{}").get("merged_from")
        except Exception:
            mf = None
        if mf:
            for seq, cu in enumerate(mf):
                conn.execute(
                    "INSERT OR IGNORE INTO merge_members(project_id,merge_uuid,member_uuid,seq) VALUES(?,?,?,?)",
                    (r["project_id"], r["uuid"], cu, seq))
    conn.commit()
