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
