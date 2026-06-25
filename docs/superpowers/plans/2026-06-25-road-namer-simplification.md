# Road Namer — Phase 1 Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Road Corridor Namer into a focused tool where a non-technical traffic-police inspector names roads (one synced leaf at a time), with corridors derived directly from the source data and two precomputed Google suggestions per leaf.

**Architecture:** Evolve the existing FastAPI + SQLite + vanilla-JS app. Pure, unit-tested Python modules handle import-filtering, corridor derivation, enrichment, and export; a thin FastAPI layer wires them to HTTP; a single-page Google Maps frontend handles naming. Corridors come from `parent_route_id` + `segment_order` (no geometry inference). All Google calls happen in a one-time admin batch and are cached.

**Tech Stack:** Python 3.9+, FastAPI, uvicorn, SQLite (stdlib `sqlite3`), `requests`, pytest + httpx (tests), vanilla JS + Google Maps JavaScript API (frontend).

## Global Constraints

- **Source of truth for structure:** corridors = `GROUP BY parent_route_id ORDER BY segment_order`. Never infer corridors geometrically. The old `build_corridors()` is removed.
- **Import filter (verbatim):** keep a leaf iff `has_children == 0` AND `sync_status == "synced"`. A parent forms a corridor iff **all** its original children are synced; otherwise its synced children become standalone.
- **uuid is the join key** to Trafficure. It is preserved end-to-end and never edited.
- **"Named" = a human assigned a real name**, derived as `name != ""`. Imported `route_name` values are placeholders shown as reference only — they never pre-fill the editable box.
- **No carriageway / `divided` field.** Removed from model and UI.
- **Two suggestions per leaf:** `sug_geocode` and `sug_roads`; merge to one chip when identical. No Nearby Search / POI-to-POI. No Gemini (Phase 2).
- **Reversed twins:** named independently. A detected twin's name is offered only as an optional extra suggestion (never auto-applied).
- **Two Google keys, both config-time, never entered by the inspector:** enrichment key (batch, env `GOOGLE_ENRICH_KEY`) and Maps JS key (browser, env `GOOGLE_MAPS_JS_KEY`).
- **DB path** overridable via env `ROADNAMER_DB` (default `roadnamer.db`).
- **Export** = single JSON with `leaves` (FeatureCollection, uuid+name) and `corridors` (`cor_code → [uuids]`).

---

## File Structure

```
road-namer-app/
├── app.py              # FastAPI wiring + static mount + entrypoint (thin)
├── db.py               # schema + connection
├── importer.py         # pure: parse, filter, derive corridors, detect twins
├── enrich.py           # Google cache + geocode/roads helpers + batch CLI
├── export.py           # pure: build export JSON
├── static/index.html   # whole frontend (Google Maps + panel)
├── requirements.txt    # + pytest, httpx
├── tests/
│   ├── conftest.py
│   ├── fixtures/sample.geojson
│   ├── test_importer.py
│   ├── test_api.py
│   ├── test_enrich.py
│   └── test_export.py
└── docs/superpowers/...
```

Responsibilities: `importer.py`/`export.py` are pure (no DB, no network) → fast unit tests. `enrich.py` isolates all Google I/O behind a cache so the network is mockable. `app.py` only translates HTTP ↔ module calls. `db.py` owns schema so tests can spin up a temp DB.

### Data model (SQLite)

```sql
CREATE TABLE projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT,
  enriched INTEGER DEFAULT 0);

CREATE TABLE corridors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
  cor_code TEXT,                       -- "cor_001" stable export id
  name TEXT DEFAULT '',                -- inspector's corridor name
  suggested TEXT DEFAULT '',           -- auto from children (set at enrich)
  order_index INTEGER,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);

CREATE TABLE segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
  uuid TEXT,                           -- Trafficure join key (preserved)
  corridor_id INTEGER,                 -- NULL => standalone
  seq INTEGER,                         -- = source segment_order (display order)
  geom TEXT, props TEXT,               -- geom JSON [[lng,lat],...]; props = original
  route_name_imported TEXT DEFAULT '', -- placeholder, reference only
  name TEXT DEFAULT '',                -- inspector's final value ('' => unnamed)
  sug_geocode TEXT DEFAULT '',
  sug_roads TEXT DEFAULT '',
  twin_uuid TEXT,                      -- detected reversed twin (optional suggestion)
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
  FOREIGN KEY(corridor_id) REFERENCES corridors(id) ON DELETE SET NULL);

CREATE TABLE gcache(k TEXT PRIMARY KEY, v TEXT);
```

`status` is **derived** (`named = name != ''`), not stored, to avoid drift.

---

## Task 1: Project scaffolding, dependencies, DB schema

**Files:**
- Modify: `requirements.txt`
- Create: `db.py`
- Create: `tests/conftest.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.connect(path: str) -> sqlite3.Connection` (row_factory=Row, foreign_keys ON); `db.init_db(conn) -> None` (creates all tables idempotently); `db.SCHEMA_SQL: str`.

- [ ] **Step 1: Add test/dev deps to `requirements.txt`**

Append:
```
python-dotenv==1.0.1
pytest==8.3.2
httpx==0.27.2
```

- [ ] **Step 2: Write the failing test**

`tests/test_db.py`:
```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db'`.

- [ ] **Step 4: Implement `db.py`**

```python
import sqlite3

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
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
  FOREIGN KEY(corridor_id) REFERENCES corridors(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS gcache(k TEXT PRIMARY KEY, v TEXT);
"""

def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()
```

`tests/conftest.py`:
```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt db.py tests/conftest.py tests/test_db.py
git commit -m "feat: SQLite schema + connection for road namer rebuild"
```

---

## Task 2: Import filtering + corridor derivation + twin detection (pure)

**Files:**
- Create: `importer.py`
- Create: `tests/fixtures/sample.geojson`
- Test: `tests/test_importer.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `importer.build_workset(features: list[dict]) -> dict` returning
    `{"corridors": [{"cor_code": str, "segments": [seg, ...]}], "standalone": [seg, ...]}`
    where each `seg` is `{"uuid","coords","props","route_name","parent_route_id","segment_order"}`, corridor segments ordered by `segment_order`, `cor_code` like `"cor_001"`.
  - `importer.detect_twins(segs: list[dict]) -> dict` mapping `uuid -> twin_uuid`.
  - Helper `importer.normalise(name: str) -> str` (strip house-number prefix, expand Rd/St/Ave/Hwy).

`seg` is the canonical shape consumed by Task 3 (persistence) and Task 6 (enrichment).

- [ ] **Step 1: Create the test fixture**

`tests/fixtures/sample.geojson` — covers: one intact corridor (parent P1, two synced children), one broken parent (P2: one synced child + one unsynced child → the synced one becomes standalone), one genuine standalone synced leaf, one reversed twin pair (standalone), and one unsynced standalone leaf (must be dropped):
```json
{"type":"FeatureCollection","features":[
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.880,25.561],[91.882,25.563]]},
  "properties":{"uuid":"P1","route_name":"Route 1","parent_route_id":null,"has_children":1,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.880,25.561],[91.881,25.562]]},
  "properties":{"uuid":"P1-S1","route_name":"Route 1 - Segment 1","parent_route_id":"P1","has_children":0,"sync_status":"synced","segment_order":1}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.881,25.562],[91.882,25.563]]},
  "properties":{"uuid":"P1-S2","route_name":"Route 1 - Segment 2","parent_route_id":"P1","has_children":0,"sync_status":"synced","segment_order":2}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.900,25.570],[91.901,25.571]]},
  "properties":{"uuid":"P2-S1","route_name":"Route 2 - Segment 1","parent_route_id":"P2","has_children":0,"sync_status":"synced","segment_order":1}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.901,25.571],[91.902,25.572]]},
  "properties":{"uuid":"P2-S2","route_name":"Route 2 - Segment 2","parent_route_id":"P2","has_children":0,"sync_status":"invalid","segment_order":2}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.910,25.580],[91.912,25.582]]},
  "properties":{"uuid":"SA1","route_name":"Route 3","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.920,25.590],[91.922,25.592]]},
  "properties":{"uuid":"TW","route_name":"Route 4","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.922,25.592],[91.920,25.590]]},
  "properties":{"uuid":"TWR","route_name":"Route 4 (Reversed)","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.930,25.600],[91.931,25.601]]},
  "properties":{"uuid":"DROP","route_name":"Route 5","parent_route_id":null,"has_children":0,"sync_status":"invalid","segment_order":null}}
]}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_importer.py`:
```python
import json, os
import importer

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.geojson")

def load():
    return json.load(open(FIX))["features"]

def test_intact_corridor_grouped_in_order():
    w = importer.build_workset(load())
    cors = w["corridors"]
    assert len(cors) == 1
    seg_uuids = [s["uuid"] for s in cors[0]["segments"]]
    assert seg_uuids == ["P1-S1", "P1-S2"]
    assert cors[0]["cor_code"] == "cor_001"

def test_broken_parent_child_becomes_standalone():
    w = importer.build_workset(load())
    sa = {s["uuid"] for s in w["standalone"]}
    assert "P2-S1" in sa          # surviving synced child of broken parent
    assert "P2-S2" not in sa      # unsynced child dropped entirely

def test_unsynced_leaf_dropped():
    w = importer.build_workset(load())
    all_uuids = {s["uuid"] for s in w["standalone"]} | {
        s["uuid"] for c in w["corridors"] for s in c["segments"]}
    assert "DROP" not in all_uuids
    assert "P1" not in all_uuids  # parent container row never in workset

def test_genuine_standalone_present():
    w = importer.build_workset(load())
    assert "SA1" in {s["uuid"] for s in w["standalone"]}

def test_twin_detection_links_both_ways():
    w = importer.build_workset(load())
    segs = w["standalone"] + [s for c in w["corridors"] for s in c["segments"]]
    twins = importer.detect_twins(segs)
    assert twins.get("TW") == "TWR"
    assert twins.get("TWR") == "TW"
    assert "SA1" not in twins

def test_normalise_strips_and_expands():
    assert importer.normalise("13-55, 132 Feet Ring Rd") == "132 Feet Ring Road"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_importer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'importer'`.

- [ ] **Step 4: Implement `importer.py`**

```python
import math, re

_HOUSE = re.compile(r'^\s*[A-Za-z0-9][A-Za-z0-9/\-]*\d[A-Za-z0-9/\-]*\s*,\s*')
_REV = re.compile(r'\s*\((Reversed|Reverse)\)\s*', re.I)

def normalise(name):
    if not name:
        return ""
    n = name.strip(); prev = None
    while prev != n:
        prev = n; n = _HOUSE.sub('', n).strip()
    for p, r in [(r'\bRd\b', 'Road'), (r'\bSt\b', 'Street'),
                 (r'\bAve\b', 'Avenue'), (r'\bHwy\b', 'Highway')]:
        n = re.sub(p, r, n)
    return n.strip(' ,')

def _coords(f):
    return [[float(x[0]), float(x[1])] for x in f["geometry"]["coordinates"]]

def _seg(f):
    p = f.get("properties") or {}
    return {"uuid": p.get("uuid"), "coords": _coords(f), "props": p,
            "route_name": p.get("route_name") or "",
            "parent_route_id": p.get("parent_route_id"),
            "segment_order": p.get("segment_order")}

def build_workset(features):
    """Apply the synced-leaf filter + all-or-nothing corridor rule."""
    feats = [f for f in features
             if (f.get("geometry") or {}).get("type") == "LineString"
             and len(f["geometry"]["coordinates"]) >= 2]
    by_props = [(f, f.get("properties") or {}) for f in feats]

    # index ALL children (any sync) by parent to evaluate intactness
    children = {}
    for f, p in by_props:
        if p.get("has_children") == 0 and p.get("parent_route_id"):
            children.setdefault(p["parent_route_id"], []).append((f, p))

    intact_parents = {par for par, kids in children.items()
                      if all(k[1].get("sync_status") == "synced" for k in kids)}

    corridors, standalone = [], []
    code = 0
    # intact corridors, ordered by segment_order
    for par in children:
        if par not in intact_parents:
            continue
        kids = sorted(children[par], key=lambda k: k[1].get("segment_order") or 0)
        code += 1
        corridors.append({"cor_code": f"cor_{code:03d}",
                          "segments": [_seg(f) for f, _ in kids]})

    # standalone = synced leaf that is parentless OR child of a broken parent
    for f, p in by_props:
        if p.get("has_children") != 0 or p.get("sync_status") != "synced":
            continue
        par = p.get("parent_route_id")
        if par and par in intact_parents:
            continue  # already placed in its corridor
        standalone.append(_seg(f))

    return {"corridors": corridors, "standalone": standalone}

def _near(a, b, tol=1e-4):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol

def detect_twins(segs):
    """Link uuids whose base-name matches AND geometry is a start<->end reversal."""
    base = lambda n: _REV.sub('', n or '').strip()
    groups = {}
    for s in segs:
        groups.setdefault(base(s["route_name"]), []).append(s)
    out = {}
    for grp in groups.values():
        if len(grp) < 2:
            continue
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                a, b = grp[i], grp[j]
                if _near(a["coords"][0], b["coords"][-1]) and \
                   _near(a["coords"][-1], b["coords"][0]):
                    out[a["uuid"]] = b["uuid"]; out[b["uuid"]] = a["uuid"]
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_importer.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add importer.py tests/fixtures/sample.geojson tests/test_importer.py
git commit -m "feat: import filtering, corridor derivation, twin detection"
```

---

## Task 3: Persist import + project CRUD API

**Files:**
- Create: `app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `db.connect/init_db`, `importer.build_workset/detect_twins`.
- Produces FastAPI app `app.app` with:
  - `POST /api/projects` (multipart `file`) → `{"project_id","name","leaves","corridors"}`
  - `GET /api/projects` → list with `seg_count`, `named_count`
  - `GET /api/projects/{id}` → `{"project","corridors":[...],"segments":[...]}` where each segment includes `coords`, `mid`, `named` (bool), `twin_name` (str|None)
  - `DELETE /api/projects/{id}` → `{"ok":true}`

- [ ] **Step 1: Write the failing test**

`tests/test_api.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL (no `app` module / no route).

- [ ] **Step 3: Implement `app.py`**

```python
import json, os, math
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
import db, importer

load_dotenv()  # read keys from root .env if present
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("ROADNAMER_DB", os.path.join(HERE, "roadnamer.db"))

app = FastAPI(title="Road Namer")

def conn():
    return db.connect(DB)

# initialise schema on import so tables exist for any connection (incl. TestClient
# constructed without a context manager). init_db is idempotent (CREATE IF NOT EXISTS).
_c = conn(); db.init_db(_c); _c.close()

def _point_at(coords, frac):
    def hav(a, b):
        R = 6371000.0
        dLat = math.radians(b[1]-a[1]); dLng = math.radians(a[0]-b[0])
        h = math.sin(dLat/2)**2 + math.cos(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.sin(dLng/2)**2
        return 2*R*math.asin(min(1, math.sqrt(h)))
    tot = sum(hav(coords[i-1], coords[i]) for i in range(1, len(coords))) * frac
    acc = 0
    for i in range(1, len(coords)):
        d = hav(coords[i-1], coords[i])
        if acc + d >= tot:
            t = (tot-acc)/d if d else 0
            return [coords[i-1][0]+(coords[i][0]-coords[i-1][0])*t,
                    coords[i-1][1]+(coords[i][1]-coords[i-1][1])*t]
        acc += d
    return coords[-1]

@app.post("/api/projects")
async def create_project(file: UploadFile = File(...)):
    try:
        raw = json.loads((await file.read()).decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"could not parse GeoJSON: {e}")
    feats = raw.get("features", []) if isinstance(raw, dict) else []
    work = importer.build_workset(feats)
    all_segs = work["standalone"] + [s for c in work["corridors"] for s in c["segments"]]
    if not all_segs:
        raise HTTPException(400, "no syncable (synced leaf) features found")
    twins = importer.detect_twins(all_segs)

    c = conn()
    pid = c.execute("INSERT INTO projects(name,created_at) VALUES(?,datetime('now'))",
                    (file.filename or "project",)).lastrowid

    def ins_seg(s, corridor_id, seq):
        c.execute("""INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
                     route_name_imported,twin_uuid) VALUES(?,?,?,?,?,?,?,?)""",
                  (pid, s["uuid"], corridor_id, seq, json.dumps(s["coords"]),
                   json.dumps(s["props"]), s["route_name"], twins.get(s["uuid"])))

    for i, cor in enumerate(work["corridors"]):
        cid = c.execute("INSERT INTO corridors(project_id,cor_code,order_index) VALUES(?,?,?)",
                        (pid, cor["cor_code"], i)).lastrowid
        for k, s in enumerate(cor["segments"]):
            ins_seg(s, cid, k)
    for s in work["standalone"]:
        ins_seg(s, None, 0)
    c.commit(); c.close()
    return {"project_id": pid, "name": file.filename or "project",
            "leaves": len(all_segs), "corridors": len(work["corridors"])}

@app.get("/api/projects")
def list_projects():
    c = conn()
    rows = c.execute("""SELECT p.*,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id) seg_count,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id AND s.name<>'') named_count
        FROM projects p ORDER BY p.id DESC""").fetchall()
    out = [dict(r) for r in rows]; c.close(); return out

@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    c = conn(); c.execute("DELETE FROM projects WHERE id=?", (pid,)); c.commit(); c.close()
    return {"ok": True}

@app.get("/api/projects/{pid}")
def get_project(pid: int):
    c = conn()
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "no such project")
    name_by_uuid = {r["uuid"]: r["name"]
                    for r in c.execute("SELECT uuid,name FROM segments WHERE project_id=?", (pid,))}
    corrs = [dict(r) for r in c.execute(
        "SELECT * FROM corridors WHERE project_id=? ORDER BY order_index,id", (pid,))]
    segs = []
    for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id,seq", (pid,)):
        coords = json.loads(r["geom"])
        twin = r["twin_uuid"]
        twin_name = name_by_uuid.get(twin) or None if twin else None
        segs.append({"id": r["id"], "uuid": r["uuid"], "corridor_id": r["corridor_id"],
                     "seq": r["seq"], "coords": coords, "mid": _point_at(coords, 0.5),
                     "route_name_imported": r["route_name_imported"], "name": r["name"],
                     "named": r["name"] != "", "sug_geocode": r["sug_geocode"],
                     "sug_roads": r["sug_roads"], "twin_uuid": twin,
                     "twin_name": twin_name if twin_name else None})
    c.close()
    return {"project": dict(p), "corridors": corrs, "segments": segs}

# static frontend (mounted last; added in Task 9)
```

> `export.py` is created later (Task 7). Do **not** import `export` in `app.py` yet — Task 7 adds that import together with the export endpoint. `app.py` here imports only `db, importer`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_api.py
git commit -m "feat: import persistence + project CRUD API"
```

---

## Task 4: Naming endpoints (segment + corridor)

**Files:**
- Modify: `app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `PATCH /api/segments/{id}` body `{"name": str}` → `{"ok":true,"named":bool}`; `PATCH /api/corridors/{id}` body `{"name": str}` → `{"ok":true}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -k patch -v`
Expected: FAIL (405/404 — routes missing).

- [ ] **Step 3: Implement the endpoints in `app.py`** (above the static mount comment)

```python
@app.patch("/api/segments/{sid}")
def patch_segment(sid: int, body: dict = Body(...)):
    name = (body.get("name") or "").strip()
    c = conn(); c.execute("UPDATE segments SET name=? WHERE id=?", (name, sid))
    c.commit(); c.close()
    return {"ok": True, "named": name != ""}

@app.patch("/api/corridors/{cid}")
def patch_corridor(cid: int, body: dict = Body(...)):
    name = (body.get("name") or "").strip()
    c = conn(); c.execute("UPDATE corridors SET name=? WHERE id=?", (name, cid))
    c.commit(); c.close()
    return {"ok": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_api.py
git commit -m "feat: segment + corridor naming endpoints (name=>named derived)"
```

---

## Task 5: Google cache + geocode/roads helpers

**Files:**
- Create: `enrich.py`
- Test: `tests/test_enrich.py`

**Interfaces:**
- Produces:
  - `enrich.Cache(conn, offline: bool)` with `.get(k)`, `.put(k,v)`.
  - `enrich.road_geocode(lat, lng, key, cache) -> str` (reverse geocode `route` component, normalised, cached key `gc:lat,lng`).
  - `enrich.road_via_roads(lng, lat, key, cache) -> str` (nearestRoads snap → reverse-geocode snapped point for road name; cached `rd:lat,lng`). *Uses reverse geocoding for the snapped point name (cheaper than Place Details — see spec §4 optimization).*
  - `enrich._http(url, params) -> dict` (retry wrapper) — patchable in tests.

- [ ] **Step 1: Write the failing test (network mocked)**

`tests/test_enrich.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enrich.py -v`
Expected: FAIL (no `enrich`).

- [ ] **Step 3: Implement `enrich.py`** (helpers; CLI added in Task 6)

```python
import json, time, requests
from importer import normalise

GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
ROADS = "https://roads.googleapis.com/v1/nearestRoads"

class Cache:
    def __init__(self, conn, offline):
        self.c = conn; self.offline = offline
    def get(self, k):
        r = self.c.execute("SELECT v FROM gcache WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else None
    def put(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO gcache(k,v) VALUES(?,?)", (k, json.dumps(v)))
        self.c.commit()

def _http(url, params):
    for t in range(3):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(0.8 * (t + 1))
    return {}

def _route_from_geocode(js):
    for r in js.get("results", []):
        for comp in r.get("address_components", []):
            if "route" in comp.get("types", []):
                return comp["long_name"]
    return ""

def road_geocode(lat, lng, key, cache):
    k = f"gc:{lat:.5f},{lng:.5f}"
    v = cache.get(k)
    if v is not None:
        return v
    if cache.offline:
        return ""
    js = _http(GEOCODE, {"key": key, "latlng": f"{lat},{lng}", "result_type": "route"})
    name = normalise(_route_from_geocode(js)); cache.put(k, name); return name

def road_via_roads(lng, lat, key, cache):
    k = f"rd:{lat:.6f},{lng:.6f}"
    v = cache.get(k)
    if v is not None:
        return v
    if cache.offline:
        return ""
    js = _http(ROADS, {"key": key, "points": f"{lat},{lng}"})
    pts = js.get("snappedPoints", [])
    if not pts:
        cache.put(k, ""); return ""
    loc = pts[0]["location"]
    name = road_geocode(loc["latitude"], loc["longitude"], key, cache)
    cache.put(k, name); return name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_enrich.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add enrich.py tests/test_enrich.py
git commit -m "feat: cached geocode + roads road-name helpers"
```

---

## Task 6: Enrichment batch CLI + corridor suggestion

**Files:**
- Modify: `enrich.py`
- Test: `tests/test_enrich.py`

**Interfaces:**
- Produces: `enrich.run(project_id, key, db_path, offline=False) -> dict` filling `segments.sug_geocode`/`sug_roads` for every leaf (midpoint sampled) and `corridors.suggested` = modal non-empty child road suggestion; sets `projects.enriched=1`. Returns `{"leaves": int, "calls": int}`.
- CLI: `python enrich.py <project_id> [--offline]` reads key from env `GOOGLE_ENRICH_KEY`, db from `ROADNAMER_DB`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrich.py`:
```python
import json, importer, enrich, db

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enrich.py -k run -v`
Expected: FAIL (`run` not defined).

- [ ] **Step 3: Implement `run` + CLI in `enrich.py`**

```python
import json as _json, math, os, sys
from collections import Counter
import db as _db

def _mid(coords):
    # reuse app's haversine midpoint via simple param: midpoint by length
    R = 6371000.0
    def hav(a, b):
        dLat = math.radians(b[1]-a[1]); dLng = math.radians(a[0]-b[0])
        h = math.sin(dLat/2)**2 + math.cos(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.sin(dLng/2)**2
        return 2*R*math.asin(min(1, math.sqrt(h)))
    tot = sum(hav(coords[i-1], coords[i]) for i in range(1, len(coords))) / 2
    acc = 0
    for i in range(1, len(coords)):
        d = hav(coords[i-1], coords[i])
        if acc + d >= tot:
            t = (tot-acc)/d if d else 0
            return [coords[i-1][0]+(coords[i][0]-coords[i-1][0])*t,
                    coords[i-1][1]+(coords[i][1]-coords[i-1][1])*t]
        acc += d
    return coords[-1]

def run(project_id, key, db_path, offline=False):
    conn = _db.connect(db_path)
    cache = Cache(conn, offline)
    segs = conn.execute("SELECT * FROM segments WHERE project_id=?", (project_id,)).fetchall()
    for s in segs:
        coords = _json.loads(s["geom"]); m = _mid(coords)
        g = road_geocode(m[1], m[0], key, cache)
        r = road_via_roads(m[0], m[1], key, cache)
        conn.execute("UPDATE segments SET sug_geocode=?,sug_roads=? WHERE id=?", (g, r, s["id"]))
    conn.commit()
    for cor in conn.execute("SELECT * FROM corridors WHERE project_id=?", (project_id,)).fetchall():
        roads = [r["sug_geocode"] or r["sug_roads"]
                 for r in conn.execute("SELECT sug_geocode,sug_roads FROM segments WHERE corridor_id=?", (cor["id"],))
                 if (r["sug_geocode"] or r["sug_roads"])]
        sug = Counter(roads).most_common(1)[0][0] if roads else ""
        conn.execute("UPDATE corridors SET suggested=? WHERE id=?", (sug, cor["id"]))
    conn.execute("UPDATE projects SET enriched=1 WHERE id=?", (project_id,))
    conn.commit(); conn.close()
    return {"leaves": len(segs), "calls": len(segs) * 2}

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()  # read GOOGLE_ENRICH_KEY from root .env if present
    offline = "--offline" in sys.argv
    pid = int([a for a in sys.argv[1:] if not a.startswith("--")][0])
    key = os.environ.get("GOOGLE_ENRICH_KEY", "")
    path = os.environ.get("ROADNAMER_DB", "roadnamer.db")
    if not offline and not key:
        sys.exit("set GOOGLE_ENRICH_KEY (or pass --offline)")
    print(run(pid, key, path, offline))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_enrich.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add enrich.py tests/test_enrich.py
git commit -m "feat: enrichment batch CLI + corridor suggestion"
```

---

## Task 7: Export builder + endpoint

**Files:**
- Modify: `export.py`, `app.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Produces: `export.build_export(project_row, corridor_rows, segment_rows) -> dict` →
  `{"leaves": {"type":"FeatureCollection","features":[...]}, "corridors":[{"id":cor_code,"name":..,"segment_uuids":[..]}]}`. Each leaf feature: geometry LineString + properties incl. `uuid`, `name` (final), original props. Standalone leaves absent from `corridors`.
- `GET /api/projects/{id}/export` streams it as a download.

- [ ] **Step 1: Write the failing test**

`tests/test_export.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_export.py -v`
Expected: FAIL (placeholder `build_export` returns `{}`).

- [ ] **Step 3: Implement `export.py`**

```python
import json

def build_export(project, corridors, segments):
    cor_by_id = {c["id"]: c for c in corridors}
    cor_segs = {c["id"]: [] for c in corridors}
    features = []
    for s in segments:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": json.loads(s["geom"])},
            "properties": {**json.loads(s["props"] or "{}"),
                           "uuid": s["uuid"], "name": s["name"]},
        })
        if s["corridor_id"] in cor_segs:
            cor_segs[s["corridor_id"]].append(s["uuid"])
    corridors_out = [{"id": cor_by_id[cid]["cor_code"],
                      "name": cor_by_id[cid]["name"],
                      "segment_uuids": uuids}
                     for cid, uuids in cor_segs.items()]
    return {"leaves": {"type": "FeatureCollection", "features": features},
            "corridors": corridors_out}
```

- [ ] **Step 4: Add the endpoint in `app.py`**

First add these imports at the top of `app.py` (alongside the existing imports): `import io`, `import export`, and `from fastapi.responses import StreamingResponse`. Then add the endpoint:

```python
@app.get("/api/projects/{pid}/export")
def export_project(pid: int):
    c = conn()
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "no such project")
    corrs = [dict(r) for r in c.execute("SELECT * FROM corridors WHERE project_id=?", (pid,))]
    segs = [dict(r) for r in c.execute("SELECT * FROM segments WHERE project_id=?", (pid,))]
    c.close()
    payload = export.build_export(dict(p), corrs, segs)
    buf = io.BytesIO(json.dumps(payload, indent=2).encode())
    return StreamingResponse(buf, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="project_{pid}_named.json"'})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_export.py tests/test_api.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add export.py app.py tests/test_export.py
git commit -m "feat: export builder (leaves + corridor mapping) + endpoint"
```

---

## Task 8: Config endpoint + static mount

**Files:**
- Modify: `app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `GET /api/config` → `{"maps_key": <env GOOGLE_MAPS_JS_KEY or "">}`. Static files mounted at `/`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:
```python
def test_config_returns_maps_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_JS_KEY", "MAPS123")
    c = make_client(tmp_path)
    assert c.get("/api/config").json()["maps_key"] == "MAPS123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -k config -v`
Expected: FAIL (404).

- [ ] **Step 3: Implement in `app.py`** (config route, then static mount at very end of file)

First add `from fastapi.staticfiles import StaticFiles` at the top of `app.py`. Then add the config route and, as the **last** statements in the file, the static mount + entrypoint:

```python
@app.get("/api/config")
def config():
    return {"maps_key": os.environ.get("GOOGLE_MAPS_JS_KEY", "")}

app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("Road Namer -> http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -v`
Expected: PASS (entire suite green).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_api.py
git commit -m "feat: config endpoint + static mount + entrypoint"
```

---

## Frontend tasks (build-and-verify)

> The frontend is a single `static/index.html` (vanilla JS + Google Maps JS API). Per the agreed approach, these tasks are verified by **running the app and looking**, not unit tests. Each task lists explicit manual acceptance checks. A `GOOGLE_MAPS_JS_KEY` env var must be set to a Maps-JS-enabled key before verifying. Use the real file `shillong_p74_all.geojson` (enrich with `--offline` first so suggestions are blank but the flow works, or with a real key).

### Task 9: Map base + render corridors/standalone with status colors

**Files:** Create `static/index.html`

- [ ] **Step 1: Build the shell** — load Google Maps JS using the key from `GET /api/config`; full-screen split: map left (~62%), panel right. Home view: project list (`GET /api/projects`) + file input that `POST`s to `/api/projects`. On open, fetch `GET /api/projects/{id}`.
- [ ] **Step 2: Render geometry** — draw each **corridor** (its segments joined) and each **standalone** leaf as a `google.maps.Polyline`. Color by status: unnamed = blue (`#4f7bbf`), named = green (`#54c08a`). Child segments are **not** drawn as separate clickable polylines yet (only corridor/standalone level is clickable). Same color for corridors and standalone (structural difference only).
- [ ] **Step 3: Verify** — run `GOOGLE_MAPS_JS_KEY=… python app.py`, import `shillong_p74_all.geojson`, open the project.
  - **Accept:** map shows Shillong with corridors + standalone as blue lines; POIs visible on the Google base map; no console errors; named items (none yet) would be green.
- [ ] **Step 4: Commit** — `git add static/index.html && git commit -m "feat: map base + status-colored corridor/standalone rendering"`

### Task 10: Panel — corridor list, single-open accordion, segment cards

**Files:** Modify `static/index.html`

- [ ] **Step 1:** Render the right panel: filter chips (All / Unnamed / Named with live counts), then a list of corridor rows (`cor_code` + `corridor.name || suggested` + "N segs · M unnamed") and standalone rows (same color, labelled "standalone").
- [ ] **Step 2:** Make corridor rows a **single-open accordion**: clicking one expands its segment cards and collapses any previously open one. Each segment card shows: greyed reference (`route_name_imported`), an **empty** name `<input>`, suggestion chips from `sug_geocode`/`sug_roads` (render one chip if equal, none if both empty), and a twin chip "↔ other direction: {twin_name}" only when `twin_name` is set.
- [ ] **Step 3: Verify** — **Accept:** only one corridor is open at a time; segment name boxes start empty; the imported placeholder shows as muted reference; tapping a chip fills the box; filters change the visible set.
- [ ] **Step 4: Commit** — `git commit -am "feat: panel accordion + segment naming cards"`

### Task 11: Selection, map↔panel sync, hover, street view, deselect

**Files:** Modify `static/index.html`

- [ ] **Step 1:** Click a corridor/standalone (map polyline **or** panel row) → select it: highlight on map (selected color `#4f8cff`, others faded), panel scrolls to + opens that row. Two-way: selecting in panel highlights the map and vice versa.
- [ ] **Step 2:** When a corridor is selected, draw its child segments as individual clickable polylines (alternating shades + endpoint nodes). Clicking a child segment **focuses** its card in the panel (scrolls/outlines it) — **never** changes membership. Hover a corridor → tooltip (name · #segs · #unnamed). Street View: enable the default pegman control. `Esc` or empty-map click deselects.
- [ ] **Step 2: Verify** — **Accept:** selecting works from both sides; child segments only appear/clickable after corridor selection; clicking a segment just focuses (nothing is reparented); hover tooltip shows; pegman opens Street View; Esc deselects. *(This is where the three old annoyances must be visibly gone.)*
- [ ] **Step 3: Commit** — `git commit -am "feat: selection, map/panel sync, hover, street view, deselect"`

### Task 12: Naming actions — autosave, Next-unnamed, progress, corridor name

**Files:** Modify `static/index.html`

- [ ] **Step 1:** Typing in a name box → debounced ~800ms `PATCH /api/segments/{id}` (and immediate on blur); tapping a suggestion/twin chip fills the box and PATCHes immediately. On success, update that item's color (blue→green) on the map and the filter counts.
- [ ] **Step 2:** Header: progress counter + bar ("Named X / Y"). A persistent **"Next unnamed →"** button and the `Enter` key jump focus to the next segment with `named==false` (within corridor, then next corridor), auto-panning/selecting it on the map. Corridor name: editable, with a one-tap "use suggestion" (the corridor `suggested` or modal of current child names) → `PATCH /api/corridors/{id}`.
- [ ] **Step 3: Verify** — **Accept:** edits persist across reload; map turns green as you name; Enter advances to the next unnamed and pans the map; progress bar climbs; corridor name applies.
- [ ] **Step 4: Commit** — `git commit -am "feat: debounced autosave, next-unnamed accelerator, progress, corridor naming"`

### Task 13: Export button

**Files:** Modify `static/index.html`

- [ ] **Step 1:** Header "Export" button → downloads `GET /api/projects/{id}/export`.
- [ ] **Step 2: Verify** — **Accept:** downloaded JSON has `leaves` (FeatureCollection with `uuid`+`name`) and `corridors` (`cor_001 → segment_uuids`); a named leaf shows its real name; standalone leaves are absent from `corridors`.
- [ ] **Step 3: Commit** — `git commit -am "feat: export button"`

---

## Task 14: End-to-end verification on real data + cleanup

**Files:** Modify `README.md`; delete obsolete code paths.

- [ ] **Step 1:** Remove dead code from the old app if any remains (old `build_corridors`, split/merge/move/reorder endpoints, `divided`). Confirm nothing imports them: `grep -rn "build_corridors\|divided\|/split\|/merge" app.py importer.py` → no hits.
- [ ] **Step 2:** Full run: `GOOGLE_ENRICH_KEY=… python enrich.py <pid>` (or `--offline`) after importing `shillong_p74_all.geojson`; then `GOOGLE_MAPS_JS_KEY=… python app.py`; name a few leaves across an intact corridor, a broken-parent orphan, and a reversed-twin pair; export.
  - **Accept:** 1,031 synced leaves imported; 154 corridors + 463 standalone; suggestions present (if real key); twin chip appears on the twin pair; export round-trips with correct uuids.
- [ ] **Step 3:** Update `README.md` "Using the app" + "Run" + the two-key setup (`GOOGLE_ENRICH_KEY`, `GOOGLE_MAPS_JS_KEY`) and the admin enrich step. Update `start.command` only if the run command changed.
- [ ] **Step 4:** Run full test suite: `python -m pytest -v` → all green.
- [ ] **Step 5: Commit** — `git commit -am "chore: e2e verification, remove dead corridor-editing code, docs"`

---

## Self-Review

**Spec coverage:** import filter + broken-parent rule (T2) ✓ · corridors from parent/order (T2–T3) ✓ · uuid preserved (T2–T3, T7) ✓ · two suggestions, merge-if-equal (T6, T10) ✓ · twins independent + optional chip (T2, T10) ✓ · corridor name from children, display-only (T6, T12) ✓ · no carriageway (schema omits it) ✓ · precompute admin batch, two keys config-time (T5–T6, T8) ✓ · map + POIs + street view + status color (T9, T11) ✓ · single-open accordion, select/focus-only, next-unnamed, debounced save (T10–T12) ✓ · export leaves+corridors JSON (T7, T13) ✓ · multi-project (T3, T9) ✓ · cost/POI/Gemini omissions are non-build (docs) ✓.

**Placeholder scan:** `export.py` ships a temporary stub in T3 and is fully implemented in T7 (called out explicitly) — not a hidden placeholder. No other TBDs.

**Type consistency:** `seg` dict shape (`uuid/coords/props/route_name/parent_route_id/segment_order`) is defined in T2 and consumed identically in T3. `build_export(project, corridors, segments)` signature matches between T3 stub, T7 impl, and the T7 endpoint call. `road_geocode(lat,lng,...)` vs `road_via_roads(lng,lat,...)` arg order is intentional and used consistently in T6 `run`. Segment API fields (`named`, `twin_name`, `sug_geocode`, `sug_roads`) defined in T3 are the ones the frontend reads in T10–T12.
