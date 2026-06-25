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
