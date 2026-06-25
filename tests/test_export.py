import json, export

def test_build_export_shape():
    project = {"id": 1, "name": "p"}
    corridors = [{"id": 10, "cor_code": "cor_001", "name": "G.S. Road"}]
    segments = [
        {"uuid": "A", "corridor_id": 10, "name": "G.S. Road", "geom": json.dumps([[91.1, 25.1], [91.2, 25.2]]), "props": "{}"},
        {"uuid": "S", "corridor_id": None, "name": "Standalone Rd", "geom": json.dumps([[91.3, 25.3], [91.4, 25.4]]), "props": "{}"},
    ]
    out = export.build_export(project, corridors, segments)
    assert out["leaves"]["type"] == "FeatureCollection"
    feats = {f["properties"]["uuid"]: f for f in out["leaves"]["features"]}
    assert feats["A"]["properties"]["name"] == "G.S. Road"
    assert feats["A"]["geometry"]["type"] == "LineString"
    assert out["corridors"] == [{"id": "cor_001", "name": "G.S. Road", "segment_uuids": ["A"]}]
    # standalone 'S' is in leaves but in no corridor
    assert "S" in feats and all("S" not in c["segment_uuids"] for c in out["corridors"])
