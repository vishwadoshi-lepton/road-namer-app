import io, json, os
from fastapi.testclient import TestClient

os.environ["ROADNAMER_DB"] = ":memory:"  # overridden per-test below

def make_client(tmp_path):
    os.environ["ROADNAMER_DB"] = str(tmp_path / "api.db")
    import importlib, app
    importlib.reload(app)
    return TestClient(app.app)

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.geojson")

def upload(client):
    data = open(FIX, "rb").read()
    return client.post("/api/projects",
                       files={"file": ("sample.geojson", io.BytesIO(data), "application/geo+json")})

def test_import_creates_workset(tmp_path):
    c = make_client(tmp_path)
    r = upload(c); assert r.status_code == 200
    body = r.json()
    assert body["leaves"] == 6      # P1-S1,P1-S2,P2-S1,SA1,TW,TWR
    assert body["corridors"] == 1

def test_get_project_marks_unnamed_and_twin(tmp_path):
    c = make_client(tmp_path)
    pid = upload(c).json()["project_id"]
    full = c.get(f"/api/projects/{pid}").json()
    segs = {s["uuid"]: s for s in full["segments"]}
    assert segs["TW"]["named"] is False
    assert segs["TW"]["twin_name"] is None  # twin unnamed yet
    assert "coords" in segs["TW"] and "mid" in segs["TW"]

def test_delete_project(tmp_path):
    c = make_client(tmp_path)
    pid = upload(c).json()["project_id"]
    assert c.delete(f"/api/projects/{pid}").json()["ok"] is True
    assert c.get(f"/api/projects/{pid}").status_code == 404

def test_patch_segment_sets_named(tmp_path):
    c = make_client(tmp_path)
    pid = upload(c).json()["project_id"]
    sid = c.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    r = c.patch(f"/api/segments/{sid}", json={"name": "G.S. Road"})
    assert r.json() == {"ok": True, "named": True}
    seg = [s for s in c.get(f"/api/projects/{pid}").json()["segments"] if s["id"] == sid][0]
    assert seg["name"] == "G.S. Road" and seg["named"] is True

def test_patch_segment_blank_is_unnamed(tmp_path):
    c = make_client(tmp_path)
    pid = upload(c).json()["project_id"]
    sid = c.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    assert c.patch(f"/api/segments/{sid}", json={"name": "  "}).json()["named"] is False

def test_patch_corridor_name(tmp_path):
    c = make_client(tmp_path)
    pid = upload(c).json()["project_id"]
    cid = c.get(f"/api/projects/{pid}").json()["corridors"][0]["id"]
    assert c.patch(f"/api/corridors/{cid}", json={"name": "Main Road"}).json()["ok"] is True

def test_config_returns_maps_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_JS_KEY", "MAPS123")
    c = make_client(tmp_path)
    assert c.get("/api/config").json()["maps_key"] == "MAPS123"


# ── Merge segments ─────────────────────────────────────────────────────
FIX_MERGE = os.path.join(os.path.dirname(__file__), "fixtures", "merge_sample.geojson")

def upload_merge(client):
    data = open(FIX_MERGE, "rb").read()
    return client.post("/api/projects",
                       files={"file": ("merge_sample.geojson", io.BytesIO(data), "application/geo+json")})

def _ids_by_uuid(client, pid):
    full = client.get(f"/api/projects/{pid}").json()
    return {s["uuid"]: s["id"] for s in full["segments"]}

def test_merge_whole_corridor_becomes_standalone(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    r = c.post(f"/api/projects/{pid}/merge",
               json={"segment_ids": [ids["PA-S1"], ids["PA-S2"], ids["PA-S3"]], "name": "Route A"})
    assert r.status_code == 200, r.text
    full = c.get(f"/api/projects/{pid}").json()
    uuids = {s["uuid"] for s in full["segments"]}
    assert not ({"PA-S1", "PA-S2", "PA-S3"} & uuids)        # originals hidden
    merged = [s for s in full["segments"] if s["id"] == r.json()["merged_segment_id"]][0]
    assert merged["corridor_id"] is None                    # collapsed to standalone
    assert merged["name"] == "Route A"
    assert len(merged["coords"]) == 4                        # junction-deduped chain
    # corridor cor_001 now has no live segments -> excluded
    assert all(co["cor_code"] != "cor_001" for co in full["corridors"])

def test_merge_subset_stays_in_corridor(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    r = c.post(f"/api/projects/{pid}/merge",
               json={"segment_ids": [ids["PA-S1"], ids["PA-S2"]]})
    assert r.status_code == 200, r.text
    full = c.get(f"/api/projects/{pid}").json()
    cid = [co["id"] for co in full["corridors"] if co["cor_code"] == "cor_001"][0]
    merged = [s for s in full["segments"] if s["id"] == r.json()["merged_segment_id"]][0]
    assert merged["corridor_id"] == cid                     # stays in corridor
    s3 = [s for s in full["segments"] if s["uuid"] == "PA-S3"][0]
    assert s3["corridor_id"] == cid                         # PA-S3 still live in corridor

def test_merge_cross_corridor_and_standalone_is_standalone(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    # PA-S3 (corridor) + SC (standalone) are connected -> result standalone
    r = c.post(f"/api/projects/{pid}/merge",
               json={"segment_ids": [ids["PA-S3"], ids["SC"]]})
    assert r.status_code == 200, r.text
    merged = [s for s in c.get(f"/api/projects/{pid}").json()["segments"]
              if s["id"] == r.json()["merged_segment_id"]][0]
    assert merged["corridor_id"] is None

def test_merge_provenance_and_export(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    mu = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids": [ids["SB1"], ids["SB2"]], "name": "Route B"}).json()["merged_uuid"]
    exp = c.get(f"/api/projects/{pid}/export").json()
    feats = {f["properties"]["uuid"]: f for f in exp["leaves"]["features"]}
    assert "SB1" not in feats and "SB2" not in feats          # originals gone from export
    assert mu in feats
    assert feats[mu]["properties"]["merged_from"] == ["SB1", "SB2"]

def test_merge_anti_parallel_rejected(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    r = c.post(f"/api/projects/{pid}/merge",
               json={"segment_ids": [ids["TWa"], ids["TWb"]]})
    assert r.status_code == 400
    assert "opposite" in r.json()["detail"].lower()

def test_merge_too_few_rejected(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    assert c.post(f"/api/projects/{pid}/merge",
                  json={"segment_ids": [ids["SB1"]]}).status_code == 400

def test_list_projects_counts_ignore_merged(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    before = [p for p in c.get("/api/projects").json() if p["id"] == pid][0]["seg_count"]
    ids = _ids_by_uuid(c, pid)
    c.post(f"/api/projects/{pid}/merge", json={"segment_ids": [ids["SB1"], ids["SB2"]]})
    after = [p for p in c.get("/api/projects").json() if p["id"] == pid][0]["seg_count"]
    assert after == before - 1     # 2 merged away, 1 new


# ── Unmerge ────────────────────────────────────────────────────────────
def test_get_project_exposes_is_merged(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    mid = c.post(f"/api/projects/{pid}/merge",
                 json={"segment_ids": [ids["SB1"], ids["SB2"]]}).json()["merged_segment_id"]
    segs = {s["id"]: s for s in c.get(f"/api/projects/{pid}").json()["segments"]}
    assert segs[mid]["is_merged"] is True
    assert segs[mid]["merged_from"] == ["SB1", "SB2"]
    other = [s for s in segs.values() if s["uuid"] == "SC"][0]
    assert other["is_merged"] is False and other["merged_from"] is None

def test_unmerge_rejects_non_merged(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    sid = _ids_by_uuid(c, pid)["SC"]
    assert c.post(f"/api/segments/{sid}/unmerge").status_code == 400

def test_unmerge_revives_collapsed_corridor(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    mid = c.post(f"/api/projects/{pid}/merge",
                 json={"segment_ids": [ids["PA-S1"], ids["PA-S2"], ids["PA-S3"]], "name": "A"}
                 ).json()["merged_segment_id"]
    assert all(co["cor_code"] != "cor_001" for co in c.get(f"/api/projects/{pid}").json()["corridors"])
    r = c.post(f"/api/segments/{mid}/unmerge")
    assert r.status_code == 200 and r.json()["count"] == 3
    full = c.get(f"/api/projects/{pid}").json()
    uuids = {s["uuid"] for s in full["segments"]}
    assert {"PA-S1", "PA-S2", "PA-S3"} <= uuids
    assert any(co["cor_code"] == "cor_001" for co in full["corridors"])   # corridor back
    assert all(s["id"] != mid for s in full["segments"])                  # M gone

def test_unmerge_one_level_recursive(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    m1 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids": [ids["PA-S1"], ids["PA-S2"], ids["PA-S3"]], "name": "A"}
                ).json()["merged_segment_id"]
    sc = ids["SC"]   # SC starts where the M1 chain ends -> connects
    m2 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids": [m1, sc], "name": "AC"}).json()["merged_segment_id"]
    # unmerge M2 -> M1 (still merged) + SC live
    r = c.post(f"/api/segments/{m2}/unmerge")
    assert r.status_code == 200 and r.json()["count"] == 2
    segs = {s["id"]: s for s in c.get(f"/api/projects/{pid}").json()["segments"]}
    assert m1 in segs and segs[m1]["is_merged"] is True
    assert any(s["uuid"] == "SC" for s in segs.values())
    assert m2 not in segs
    # unmerge M1 -> the three originals
    r2 = c.post(f"/api/segments/{m1}/unmerge")
    assert r2.status_code == 200 and r2.json()["count"] == 3
    uuids = {s["uuid"] for s in c.get(f"/api/projects/{pid}").json()["segments"]}
    assert {"PA-S1", "PA-S2", "PA-S3"} <= uuids
