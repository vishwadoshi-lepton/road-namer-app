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
