import db

def test_init_db_creates_tables(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "corridors", "segments", "gcache"} <= names

def test_foreign_keys_enabled(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

def test_segments_has_merged_into_column(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    assert "merged_into" in cols

import json as _json

def test_merge_members_table_exists(tmp_path):
    conn = db.connect(str(tmp_path / "t.db")); db.init_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "merge_members" in names

def test_migration_backfills_from_props(tmp_path):
    p = str(tmp_path / "legacy.db")
    conn = db.connect(p); db.init_db(conn)
    conn.execute("INSERT INTO projects(id,name) VALUES(1,'p')")
    conn.execute("INSERT INTO segments(project_id,uuid,geom,props) VALUES(1,'M','[]',?)",
                 (_json.dumps({"merged_from": ["A", "B", "C"]}),))
    conn.commit()
    db.init_db(conn)   # re-run -> back-fill
    rows = conn.execute("SELECT member_uuid, seq FROM merge_members WHERE merge_uuid='M' ORDER BY seq").fetchall()
    assert [(r["member_uuid"], r["seq"]) for r in rows] == [("A", 0), ("B", 1), ("C", 2)]
    db.init_db(conn)   # idempotent
    assert conn.execute("SELECT COUNT(*) n FROM merge_members").fetchone()["n"] == 3

def test_init_db_idempotent_adds_column_once(tmp_path):
    # Simulate an old DB created without merged_into, then migrate.
    p = str(tmp_path / "old.db")
    conn = db.connect(p)
    conn.executescript("""
      CREATE TABLE segments(id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
        uuid TEXT, corridor_id INTEGER, seq INTEGER, geom TEXT, props TEXT,
        route_name_imported TEXT DEFAULT '', name TEXT DEFAULT '',
        sug_geocode TEXT DEFAULT '', sug_roads TEXT DEFAULT '', twin_uuid TEXT);
    """); conn.commit()
    db.init_db(conn)      # should add the column
    db.init_db(conn)      # running again must not error
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    assert "merged_into" in cols
