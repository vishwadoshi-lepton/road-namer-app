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
