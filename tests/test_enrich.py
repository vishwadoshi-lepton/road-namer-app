import db, enrich

def _mem():
    c = db.connect(":memory:"); db.init_db(c); return c

def test_geocode_caches_and_normalises(monkeypatch):
    calls = []
    def fake_http(url, params):
        calls.append(url)
        return {"results": [{"address_components": [
            {"types": ["route"], "long_name": "G.S. Rd"}]}]}
    monkeypatch.setattr(enrich, "_http", fake_http)
    cache = enrich.Cache(_mem(), offline=False)
    assert enrich.road_geocode(25.56, 91.88, "KEY", cache) == "G.S. Road"
    # second call served from cache, no new HTTP
    enrich.road_geocode(25.56, 91.88, "KEY", cache)
    assert len(calls) == 1

def test_offline_returns_empty_without_http(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network in offline mode")
    monkeypatch.setattr(enrich, "_http", boom)
    cache = enrich.Cache(_mem(), offline=True)
    assert enrich.road_geocode(25.56, 91.88, "KEY", cache) == ""

def test_road_via_roads_snaps_then_geocodes(monkeypatch):
    def fake_http(url, params):
        if "nearestRoads" in url:
            return {"snappedPoints": [{"location": {"latitude": 25.561, "longitude": 91.881}}]}
        return {"results": [{"address_components": [
            {"types": ["route"], "long_name": "Jail Rd"}]}]}
    monkeypatch.setattr(enrich, "_http", fake_http)
    cache = enrich.Cache(_mem(), offline=False)
    assert enrich.road_via_roads(91.88, 25.56, "KEY", cache) == "Jail Road"

def test_road_via_roads_empty_snapped_returns_blank(monkeypatch):
    monkeypatch.setattr(enrich, "_http", lambda url, params: {})
    cache = enrich.Cache(_mem(), offline=False)
    assert enrich.road_via_roads(91.88, 25.56, "KEY", cache) == ""

def test_road_via_roads_offline_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network in offline mode")
    monkeypatch.setattr(enrich, "_http", boom)
    cache = enrich.Cache(_mem(), offline=True)
    assert enrich.road_via_roads(91.88, 25.56, "KEY", cache) == ""

import json, importer

def _seed_project(conn):
    pid = conn.execute("INSERT INTO projects(name,created_at) VALUES('t','now')").lastrowid
    cid = conn.execute("INSERT INTO corridors(project_id,cor_code,order_index) VALUES(?,?,0)",
                       (pid, "cor_001")).lastrowid
    for k, uuid in enumerate(["A", "B"]):
        conn.execute("""INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props)
                        VALUES(?,?,?,?,?,?)""",
                     (pid, uuid, cid, k, json.dumps([[91.88, 25.56], [91.89, 25.57]]), "{}"))
    conn.commit(); return pid

def test_run_fills_suggestions_and_corridor(monkeypatch, tmp_path):
    path = str(tmp_path / "e.db"); conn = db.connect(path); db.init_db(conn)
    pid = _seed_project(conn); conn.close()
    monkeypatch.setattr(enrich, "road_geocode", lambda *a, **k: "G.S. Road")
    monkeypatch.setattr(enrich, "road_via_roads", lambda *a, **k: "Jail Road")
    res = enrich.run(pid, "KEY", path)
    assert res["leaves"] == 2
    conn = db.connect(path)
    segs = conn.execute("SELECT sug_geocode,sug_roads FROM segments WHERE project_id=?", (pid,)).fetchall()
    assert all(s["sug_geocode"] == "G.S. Road" and s["sug_roads"] == "Jail Road" for s in segs)
    cor = conn.execute("SELECT suggested FROM corridors WHERE project_id=?", (pid,)).fetchone()
    assert cor["suggested"] in ("G.S. Road", "Jail Road")
    assert conn.execute("SELECT enriched FROM projects WHERE id=?", (pid,)).fetchone()[0] == 1
