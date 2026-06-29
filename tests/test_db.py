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
