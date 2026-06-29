# Merge Segments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge 2+ connected route segments into one new segment (fresh UUID, `merged_from` provenance), respecting stored directions, persisted via soft-delete, with a dedicated Merge mode UI; plus two bundled panel changes (corridor-name display, search box).

**Architecture:** Pure chain-ordering/geometry logic in a new `merge.py`; a `POST /api/projects/{pid}/merge` endpoint in FastAPI that inserts the merged segment and soft-deletes originals (`merged_into` column); all live reads filter `merged_into IS NULL`; export is unchanged in format. Frontend is the existing vanilla-JS single file with a new Merge mode plus list display/search tweaks.

**Tech Stack:** Python 3 / FastAPI / SQLite, pytest + httpx TestClient, vanilla JS + Google Maps.

## Global Constraints

- Connection tolerance = `importer._near(a, b, tol=1e-4)` (degrees) — reuse, do not redefine.
- New UUID via Python `uuid.uuid4()` (string).
- Never reverse a segment's geometry; chain must connect in stored directions.
- Nothing physically deleted by merge: originals get `merged_into = <new uuid>`; corridors with 0 live segments are kept in DB but excluded from API/export output.
- "Live" segment = `merged_into IS NULL`. Corridor shown/exported iff it has ≥1 live segment.
- Coordinates are `[lng, lat]`; geometry is GeoJSON `LineString`.
- Spec: `docs/superpowers/specs/2026-06-29-merge-segments-design.md`.

---

### Task 1: DB migration — `merged_into` column

**Files:**
- Modify: `db.py` (SCHEMA_SQL segments table; `init_db`)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `segments.merged_into TEXT` column; `db.init_db(conn)` is idempotent and upgrades an existing DB in place.

- [ ] **Step 1: Write the failing test** — append to `tests/test_db.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db.py -q`
Expected: FAIL — column missing.

- [ ] **Step 3: Implement** — in `db.py`, add the column to `SCHEMA_SQL` segments table (after `twin_uuid TEXT,`):

```sql
  twin_uuid TEXT, merged_into TEXT DEFAULT NULL,
```

and make `init_db` migrate existing DBs:

```python
def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE segments ADD COLUMN merged_into TEXT DEFAULT NULL")
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): add merged_into column + idempotent migration"
```

---

### Task 2: `merge.py` — chain ordering + geometry (pure logic)

**Files:**
- Create: `merge.py`
- Test: `tests/test_merge.py`

**Interfaces:**
- Produces:
  - `merge.MergeError(Exception)` — message is human-readable.
  - `merge.order_chain(segments, tol=1e-4) -> list` — `segments` is a list of dicts each with `"coords"` (list of `[lng,lat]`). Returns the same dicts ordered head-to-tail, or raises `MergeError`.
  - `merge.merge_coords(ordered) -> list` — concatenated coords, junction de-duplicated.

- [ ] **Step 1: Write the failing tests** — create `tests/test_merge.py`:

```python
import pytest
import merge

def seg(coords): return {"coords": coords}

A = seg([[0.0, 0.0], [1.0, 1.0]])
B = seg([[1.0, 1.0], [2.0, 2.0]])
C = seg([[2.0, 2.0], [3.0, 3.0]])

def test_orders_two_in_chain_order_regardless_of_input_order():
    out = merge.order_chain([B, A])
    assert out == [A, B]

def test_orders_three():
    out = merge.order_chain([C, A, B])
    assert out == [A, B, C]

def test_merge_coords_dedups_junction():
    assert merge.merge_coords([A, B, C]) == [[0.0,0.0],[1.0,1.0],[2.0,2.0],[3.0,3.0]]

def test_rejects_fewer_than_two():
    with pytest.raises(merge.MergeError):
        merge.order_chain([A])

def test_rejects_gap():
    far = seg([[5.0, 5.0], [6.0, 6.0]])
    with pytest.raises(merge.MergeError):
        merge.order_chain([A, far])

def test_rejects_anti_parallel_twin():
    rev = seg([[1.0, 1.0], [0.0, 0.0]])   # reverse of A
    with pytest.raises(merge.MergeError):
        merge.order_chain([A, rev])

def test_rejects_branch():
    # A ends at (1,1); both B and B2 start at (1,1) -> branch
    b2 = seg([[1.0, 1.0], [2.0, 9.0]])
    with pytest.raises(merge.MergeError):
        merge.order_chain([A, B, b2])

def test_tolerance_allows_near_join():
    near = seg([[1.00005, 1.00005], [2.0, 2.0]])  # within 1e-4 of A's end
    out = merge.order_chain([A, near])
    assert out == [A, near]
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_merge.py -q`
Expected: FAIL — `No module named 'merge'`.

- [ ] **Step 3: Implement** — create `merge.py`:

```python
"""Pure chain-ordering and geometry helpers for merging route segments.

A merge takes >=2 segments that form ONE continuous directed chain in their
stored directions (no reversing) and joins them into a single LineString.
"""
from importer import _near


class MergeError(Exception):
    """Raised when the selected segments cannot form one directed chain."""


def order_chain(segments, tol=1e-4):
    """Return `segments` ordered head-to-tail, or raise MergeError.

    Each segment is a dict with "coords" = [[lng,lat], ...]. Direction is fixed:
    segment i's end must be ~equal to segment i+1's start (within tol).
    """
    if len(segments) < 2:
        raise MergeError("Select at least two segments to merge.")

    def start(s): return s["coords"][0]
    def end(s):   return s["coords"][-1]

    # A start segment's start point is not the end of any other segment.
    starts = [s for s in segments
              if not any(o is not s and _near(start(s), end(o), tol) for o in segments)]
    if len(starts) == 0:
        if len(segments) == 2 and _near(start(segments[0]), end(segments[1]), tol) \
                and _near(end(segments[0]), start(segments[1]), tol):
            raise MergeError("Two selected segments run in opposite directions.")
        raise MergeError("Selected segments form a loop.")
    if len(starts) > 1:
        raise MergeError("Segments don't connect end to end.")

    ordered = [starts[0]]
    used = {id(starts[0])}
    cur = starts[0]
    while len(ordered) < len(segments):
        nxts = [s for s in segments if id(s) not in used and _near(end(cur), start(s), tol)]
        if len(nxts) == 0:
            raise MergeError("Segments don't connect end to end.")
        if len(nxts) > 1:
            raise MergeError("Selected segments branch; pick a single path.")
        cur = nxts[0]
        ordered.append(cur)
        used.add(id(cur))
    return ordered


def merge_coords(ordered):
    """Concatenate ordered segments, dropping each later segment's first point
    (the shared junction) so there are no duplicate vertices."""
    coords = list(ordered[0]["coords"])
    for s in ordered[1:]:
        coords.extend(s["coords"][1:])
    return coords
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_merge.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add merge.py tests/test_merge.py
git commit -m "feat(merge): pure chain-ordering + geometry join logic"
```

---

### Task 3: Merge fixture + backend endpoint + active-segment filtering

**Files:**
- Create: `tests/fixtures/merge_sample.geojson`
- Modify: `app.py` (new `import merge`, `import uuid`; new endpoint; filters in `get_project`, `export_project`, `list_projects`)
- Modify: `export.py` (`build_export` excludes empty corridors)
- Test: `tests/test_api.py` (append merge tests)

**Interfaces:**
- Consumes: `merge.order_chain`, `merge.merge_coords`, `merge.MergeError` (Task 2); `segments.merged_into` (Task 1).
- Produces: `POST /api/projects/{pid}/merge` body `{segment_ids:[int], name?:str}` → `{ok, merged_segment_id, merged_uuid, corridor_id}`; live-only `get_project`/`export`/counts.

- [ ] **Step 1: Create the fixture** `tests/fixtures/merge_sample.geojson`:

```json
{"type":"FeatureCollection","features":[
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.10,25.10],[91.11,25.11]]},
  "properties":{"uuid":"PA-S1","route_name":"Route A - Segment 1","parent_route_id":"PA","has_children":0,"sync_status":"synced","segment_order":1}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.11,25.11],[91.12,25.12]]},
  "properties":{"uuid":"PA-S2","route_name":"Route A - Segment 2","parent_route_id":"PA","has_children":0,"sync_status":"synced","segment_order":2}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.12,25.12],[91.13,25.13]]},
  "properties":{"uuid":"PA-S3","route_name":"Route A - Segment 3","parent_route_id":"PA","has_children":0,"sync_status":"synced","segment_order":3}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.13,25.13],[91.14,25.14]]},
  "properties":{"uuid":"SC","route_name":"Route C","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.20,25.20],[91.21,25.21]]},
  "properties":{"uuid":"SB1","route_name":"Route B 1","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.21,25.21],[91.22,25.22]]},
  "properties":{"uuid":"SB2","route_name":"Route B 2","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.30,25.30],[91.31,25.31]]},
  "properties":{"uuid":"TWa","route_name":"Route T","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.31,25.31],[91.30,25.30]]},
  "properties":{"uuid":"TWb","route_name":"Route T (Reversed)","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}}
]}
```

This yields corridor `cor_001` = [PA-S1, PA-S2, PA-S3] and standalone {SC, SB1, SB2, TWa, TWb}.

- [ ] **Step 2: Write the failing tests** — append to `tests/test_api.py`:

```python
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
    # PA-S3 still live and in the same corridor
    s3 = [s for s in full["segments"] if s["uuid"] == "PA-S3"][0]
    assert s3["corridor_id"] == cid

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
```

- [ ] **Step 3: Run to verify fail**

Run: `python -m pytest tests/test_api.py -q`
Expected: FAIL — 404/405 (endpoint missing) and originals still present.

- [ ] **Step 4: Implement endpoint + filters in `app.py`**

Add imports at top (line 1 area): `import json, os, math, io, uuid as uuidlib` and `import db, importer, export, merge`.

In `get_project`, filter live segments and empty corridors:
- `name_by_uuid` query: add `AND merged_into IS NULL`.
- segment loop query: `SELECT * FROM segments WHERE project_id=? AND merged_into IS NULL ORDER BY corridor_id,seq`.
- After building `segs`, restrict corridors:

```python
    live_corr_ids = {s["corridor_id"] for s in segs if s["corridor_id"] is not None}
    corrs = [co for co in corrs if co["id"] in live_corr_ids]
```

(Keep `corrs` fetched as before, then apply the filter line right before `return`.)

In `export_project`, segment query: add `AND merged_into IS NULL`.

In `list_projects`, both subqueries add `AND s.merged_into IS NULL`.

Add the endpoint (place after `patch_corridor`):

```python
@app.post("/api/projects/{pid}/merge")
def merge_segments(pid: int, body: dict = Body(...)):
    ids = body.get("segment_ids") or []
    name = (body.get("name") or "").strip()
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(400, "Select at least two segments to merge.")
    c = conn()
    try:
        rows = []
        for sid in ids:
            r = c.execute(
                "SELECT * FROM segments WHERE id=? AND project_id=? AND merged_into IS NULL",
                (sid, pid)).fetchone()
            if not r:
                raise HTTPException(400, f"Segment {sid} not found or already merged.")
            rows.append(dict(r))
        segs = [{"coords": json.loads(r["geom"]), "row": r} for r in rows]
        try:
            ordered = merge.order_chain(segs)
        except merge.MergeError as e:
            raise HTTPException(400, str(e))
        merged_coords = merge.merge_coords(ordered)
        ordered_rows = [o["row"] for o in ordered]

        # Corridor placement
        corr_ids = {r["corridor_id"] for r in ordered_rows}
        new_corr, new_seq = None, 0
        if len(corr_ids) == 1 and next(iter(corr_ids)) is not None:
            cid = next(iter(corr_ids))
            ph = ",".join("?" * len(ids))
            remaining = c.execute(
                f"SELECT COUNT(*) n FROM segments WHERE corridor_id=? AND merged_into IS NULL "
                f"AND id NOT IN ({ph})", (cid, *ids)).fetchone()["n"]
            if remaining > 0:
                new_corr = cid
                new_seq = min(r["seq"] for r in ordered_rows)

        # Props: copy first segment's props, drop stale ids, add provenance.
        props = json.loads(ordered_rows[0]["props"] or "{}")
        for k in ("parent_route_id", "segment_order", "uuid"):
            props.pop(k, None)
        props["merged_from"] = [r["uuid"] for r in ordered_rows]

        # route_name_imported: distinct non-empty source names joined.
        seen, names = set(), []
        for r in ordered_rows:
            rn = (r["route_name_imported"] or "").strip()
            if rn and rn not in seen:
                seen.add(rn); names.append(rn)
        route_name_imported = " + ".join(names) if names else "Merged segment"

        merged_uuid = str(uuidlib.uuid4())
        new_id = c.execute(
            """INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
               route_name_imported,name,sug_geocode,sug_roads,twin_uuid,merged_into)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, merged_uuid, new_corr, new_seq, json.dumps(merged_coords),
             json.dumps(props), route_name_imported, name, "", "", None, None)).lastrowid

        ph = ",".join("?" * len(ids))
        c.execute(f"UPDATE segments SET merged_into=? WHERE id IN ({ph})", (merged_uuid, *ids))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "merged_segment_id": new_id,
            "merged_uuid": merged_uuid, "corridor_id": new_corr}
```

- [ ] **Step 5: Implement `export.py` empty-corridor exclusion**

Change the `corridors_out` comprehension to skip empties:

```python
    corridors_out = [{"id": cor_by_id[cid]["cor_code"],
                      "name": cor_by_id[cid]["name"],
                      "segment_uuids": uuids}
                     for cid, uuids in cor_segs.items() if uuids]
```

- [ ] **Step 6: Run to verify pass**

Run: `python -m pytest tests/test_api.py tests/test_export.py -q`
Expected: PASS (existing + new merge tests).

- [ ] **Step 7: Full suite + commit**

```bash
python -m pytest -q
git add app.py export.py tests/test_api.py tests/fixtures/merge_sample.geojson
git commit -m "feat(api): merge segments endpoint + live-only reads"
```

---

### Task 4: Corridor header shows saved name (frontend bug fix)

**Files:**
- Modify: `static/index.html` (`renderCorrList`, ~line 1037-1042)

**Interfaces:** none (display only).

- [ ] **Step 1: Implement** — in `renderCorrList`, replace the `.corr-code` line so a named corridor shows its name with the code as a muted suffix:

Replace:
```html
          <div class="corr-code">${esc(isStandalone ? label : (corr.cor_code || label))}${isStandalone ? ' <em style="font-style:normal;font-weight:400;color:#94a3b8;font-size:11px">standalone</em>' : ''}</div>
```
With:
```html
          <div class="corr-code">${esc(isStandalone ? label : (corr.name || corr.cor_code || label))}${
            isStandalone
              ? ' <em style="font-style:normal;font-weight:400;color:#94a3b8;font-size:11px">standalone</em>'
              : (corr.name ? ` <em style="font-style:normal;font-weight:400;color:#94a3b8;font-size:11px">${esc(corr.cor_code)}</em>` : '')
          }</div>
```

- [ ] **Step 2: Verify (manual)** — start server (`python app.py`), open a project, name a corridor; its header now shows the saved name with the `cor_xxx` code as a small grey suffix. After server restart / refetch it still shows the name.

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "fix(ui): corridor header shows saved name, code as suffix"
```

---

### Task 5: Panel search box (filter by saved names)

**Files:**
- Modify: `static/index.html` (HTML in `#project-view`; CSS; state `searchQuery`; `itemMatchesSearch`; `renderCorrList`; wire input)

**Interfaces:**
- Produces: `searchQuery` (string), `itemMatchesSearch(item)`; `renderCorrList` honors both filter and search.

- [ ] **Step 1: Add the search input** — in `#project-view`, between `#filter-bar` and `#corridor-list`:

```html
        <div id="search-bar" style="padding:8px 12px;border-bottom:1px solid #e2e8f0;">
          <input id="panel-search" type="text" placeholder="Search saved names…"
                 autocomplete="off"
                 style="width:100%;box-sizing:border-box;border:1px solid #e2e8f0;border-radius:6px;padding:6px 9px;font-size:13px;outline:none;"/>
        </div>
```

- [ ] **Step 2: Add state + matcher** — near `let activeFilter`:

```javascript
let searchQuery = '';            // panel search (matches saved names)
```

Add helper near `itemMatchesFilter`:

```javascript
function itemMatchesSearch(item) {
  const q = searchQuery.trim().toLowerCase();
  if (!q) return true;
  if (!item.isStandalone && (item.name || '').toLowerCase().includes(q)) return true;
  return segmentsOfItem(item).some(s => (s.name || '').toLowerCase().includes(q));
}
```

- [ ] **Step 3: Apply in `renderCorrList`** — change the filter line and auto-expand on search:

Replace:
```javascript
  const corrs = getVisibleCorridors().filter(c => itemMatchesFilter(c));
```
With:
```javascript
  const corrs = getVisibleCorridors().filter(c => itemMatchesFilter(c) && itemMatchesSearch(c));
```

And so matches are visible, treat a corridor as open when a non-empty search matches it. Replace:
```javascript
    const isOpen = corr.id === openCorrId;
```
With:
```javascript
    const isOpen = corr.id === openCorrId || (!!searchQuery.trim() && !corr.isStandalone);
```

- [ ] **Step 4: Wire the input** — in `boot()` controls section:

```javascript
  document.getElementById('panel-search').addEventListener('input', e => {
    searchQuery = e.target.value;
    renderCorrList();
  });
```

- [ ] **Step 5: Verify (manual)** — name a couple of segments/corridors, type part of a saved name; the list narrows to matching corridors (expanded) and standalone items; combining with Unnamed/Named chips still works; clearing restores the list.

- [ ] **Step 6: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): panel search box filters list by saved names"
```

---

### Task 6: Merge mode UI

**Files:**
- Modify: `static/index.html` (header button; CSS; state; merge-mode map + panel render; selection + validation; overlap chooser; submit/exit; wire button + Esc)

**Interfaces:**
- Consumes: `POST /api/projects/{pid}/merge` (Task 3).
- Produces: `mergeMode`, `mergeSelection` (Set), client `validateChain(ids)`, `enterMergeMode()/exitMergeMode()`, `toggleMergeSeg(id)`, `doMerge()`.

- [ ] **Step 1: Header button** — after the Export button in `#header`:

```html
    <button id="btn-merge" style="display:none">Merge</button>
```

Add CSS near `#btn-export`:

```css
#btn-merge { padding: 6px 14px; background:#fff; color:#6d3fbf; border:1.5px solid #c4b5fd; border-radius:6px; font-size:13px; cursor:pointer; white-space:nowrap; font-weight:500; }
#btn-merge:hover { background:#f5f3ff; }
#btn-merge.active { background:#6d3fbf; color:#fff; border-color:#6d3fbf; }
/* Merge panel */
#merge-controls { padding:10px 12px; border-bottom:1px solid #e2e8f0; background:#faf9ff; position:sticky; top:0; z-index:2; }
#merge-controls .mc-row { display:flex; align-items:center; gap:8px; }
#merge-controls .mc-banner { font-size:12px; margin:6px 0; }
#merge-controls .mc-ok { color:#15803d; }
#merge-controls .mc-bad { color:#b91c1c; }
#merge-controls input { width:100%; box-sizing:border-box; border:1px solid #e2e8f0; border-radius:6px; padding:6px 9px; font-size:13px; outline:none; margin:6px 0; }
#merge-controls button { padding:6px 12px; border-radius:6px; font-size:13px; cursor:pointer; border:1px solid transparent; }
#btn-do-merge { background:#6d3fbf; color:#fff; }
#btn-do-merge:disabled { opacity:.4; cursor:default; }
#btn-cancel-merge { background:#fff; color:#64748b; border-color:#e2e8f0; }
.merge-group-h { font-size:11px; font-weight:700; color:#64748b; padding:8px 6px 2px; }
.merge-seg-row { display:flex; align-items:center; gap:8px; border:1px solid #e2e8f0; border-radius:7px; padding:8px 10px; margin:4px 0; cursor:pointer; }
.merge-seg-row.sel { border-color:#6d3fbf; background:#f5f3ff; }
.merge-seg-row .ms-badge { width:20px;height:20px;border-radius:50%;background:#6d3fbf;color:#fff;font-size:11px;font-weight:700;display:none;align-items:center;justify-content:center;flex-shrink:0; }
.merge-seg-row.sel .ms-badge { display:inline-flex; }
.merge-seg-row .ms-name { flex:1;min-width:0;font-size:13px; }
.merge-seg-row .ms-len { color:#94a3b8;font-size:11px; }
#merge-chooser { position:fixed; z-index:10000; background:#fff; border-radius:8px; box-shadow:0 2px 12px rgba(0,0,0,.25); overflow:hidden; display:none; min-width:180px; }
#merge-chooser .mch-row { padding:8px 12px; font-size:12px; cursor:pointer; display:flex; gap:6px; align-items:center; }
#merge-chooser .mch-row:hover { background:#f5f3ff; }
```

Add chooser container before `</body>` (near `#map-tooltip`): `<div id="merge-chooser"></div>`.

- [ ] **Step 2: State** — near other state vars:

```javascript
let mergeMode = false;
let mergeSelection = new Set();    // selected segment ids
let mergeOverlayPolylines = [];    // all live-segment polylines drawn in merge mode
let mergeHighlight = [];           // transient highlight (arrows) for chooser hover
```

- [ ] **Step 3: Client-side chain validation** (mirrors `merge.order_chain`):

```javascript
function near2(a, b, tol = 1e-4) { return Math.abs(a[0]-b[0]) < tol && Math.abs(a[1]-b[1]) < tol; }

// ids -> {ok, reason, order:[ids]}
function validateChain(ids) {
  const segs = ids.map(id => projectData.segments.find(s => s.id === id)).filter(Boolean);
  if (segs.length < 2) return { ok: false, reason: 'Select at least two segments.', order: [] };
  const start = s => s.coords[0], end = s => s.coords[s.coords.length - 1];
  const starts = segs.filter(s => !segs.some(o => o !== s && near2(start(s), end(o))));
  if (starts.length === 0) {
    if (segs.length === 2 && near2(start(segs[0]), end(segs[1])) && near2(end(segs[0]), start(segs[1])))
      return { ok: false, reason: 'Two segments run in opposite directions.', order: [] };
    return { ok: false, reason: 'Segments form a loop.', order: [] };
  }
  if (starts.length > 1) return { ok: false, reason: "Segments don't connect end to end.", order: [] };
  const order = [starts[0]]; const used = new Set([starts[0].id]); let cur = starts[0];
  while (order.length < segs.length) {
    const nxts = segs.filter(s => !used.has(s.id) && near2(end(cur), start(s)));
    if (nxts.length === 0) return { ok: false, reason: "Segments don't connect end to end.", order: [] };
    if (nxts.length > 1) return { ok: false, reason: 'Segments branch; pick a single path.', order: [] };
    cur = nxts[0]; order.push(cur); used.add(cur.id);
  }
  return { ok: true, reason: `Connected chain of ${order.length}`, order: order.map(s => s.id) };
}
```

- [ ] **Step 4: Enter/exit + toggle**

```javascript
function enterMergeMode() {
  if (!projectData) return;
  mergeMode = true;
  mergeSelection = new Set();
  selectedCorrId = null; openCorrId = null;
  document.getElementById('btn-merge').classList.add('active');
  document.getElementById('btn-merge').textContent = 'Exit merge';
  renderMergeMap();
  renderMergePanel();
}

function exitMergeMode() {
  mergeMode = false;
  mergeSelection = new Set();
  clearMergeOverlay();
  document.getElementById('merge-chooser').style.display = 'none';
  const btn = document.getElementById('btn-merge');
  btn.classList.remove('active'); btn.textContent = 'Merge';
  renderMap();
  renderCorrList();
}

function toggleMergeSeg(id) {
  if (mergeSelection.has(id)) mergeSelection.delete(id); else mergeSelection.add(id);
  renderMergeMap();
  renderMergePanel();
}

function clearMergeOverlay() {
  mergeOverlayPolylines.forEach(p => p.setMap(null));
  mergeOverlayPolylines = [];
  mergeHighlight.forEach(m => m.setMap(null));
  mergeHighlight = [];
}
```

- [ ] **Step 5: Merge-mode map** — draws every live segment, click toggles; map click hit-tests for overlap:

```javascript
function liveSegments() { return projectData ? projectData.segments : []; }

function renderMergeMap() {
  if (!googleMap || !projectData) return;
  // hide normal layers
  Object.values(corridorPolylines).forEach(pl => pl.setMap(null));
  clearChildPolylines();
  clearMergeOverlay();

  liveSegments().forEach(seg => {
    const path = lngLatToGM(seg.coords);
    if (path.length < 2) return;
    const sel = mergeSelection.has(seg.id);
    const pl = new google.maps.Polyline({
      path, map: googleMap,
      strokeColor: sel ? '#6d3fbf' : (seg.named ? COLOR_NAMED : COLOR_UNNAMED),
      strokeWeight: sel ? 8 : 6, strokeOpacity: 1, zIndex: sel ? 5 : 2,
      _segId: seg.id
    });
    pl.addListener('click', (e) => onMergeMapClick(seg.id, e));
    mergeOverlayPolylines.push(pl);
    if (sel) {
      // direction arrow at end
      const a = path[path.length - 2], b = path[path.length - 1];
      const arrow = new google.maps.Marker({ position: b, map: googleMap, clickable: false, zIndex: 6,
        icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, scale: 3.5,
                rotation: headingDeg(a, b), fillColor: '#6d3fbf', fillOpacity: 1, strokeColor: '#fff', strokeWeight: 1 } });
      mergeOverlayPolylines.push(arrow);
    }
  });
}

// On a polyline click, gather ALL live segments whose geometry passes near the
// click point; if >1, show the chooser, else toggle directly.
function onMergeMapClick(segId, e) {
  const pt = e.latLng;
  const candidates = liveSegments().filter(s => segmentNearLatLng(s, pt));
  if (candidates.length <= 1) { toggleMergeSeg(segId); return; }
  showMergeChooser(candidates, e);
}

function segmentNearLatLng(seg, latLng) {
  // true if any vertex/edge of seg is within ~6px of latLng (geometry overlap test)
  const proj = googleMap.getProjection();
  if (!proj) return false;
  const z = googleMap.getZoom();
  const scale = Math.pow(2, z);
  const toPx = ll => { const p = proj.fromLatLngToPoint(ll); return { x: p.x * scale, y: p.y * scale }; };
  const c = toPx(latLng);
  const path = seg.coords.map(([lng, lat]) => toPx(new google.maps.LatLng(lat, lng)));
  const TH = 8;
  for (let i = 1; i < path.length; i++) {
    if (ptSegDistPx(c, path[i-1], path[i]) <= TH) return true;
  }
  return false;
}

function ptSegDistPx(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const l2 = dx*dx + dy*dy;
  let t = l2 ? ((p.x-a.x)*dx + (p.y-a.y)*dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  const x = a.x + t*dx, y = a.y + t*dy;
  return Math.hypot(p.x - x, p.y - y);
}
```

- [ ] **Step 6: Overlap chooser** (hover highlights with arrow):

```javascript
function showMergeChooser(candidates, e) {
  const box = document.getElementById('merge-chooser');
  box.innerHTML = candidates.map(s => {
    const nm = s.name || s.route_name_imported || ('Segment ' + s.id);
    const sel = mergeSelection.has(s.id) ? '✓ ' : '';
    return `<div class="mch-row" data-id="${s.id}">${sel}${esc(nm)} · ${fmtLen(segLengthM(s.coords))}</div>`;
  }).join('');
  const ev = e.domEvent || {};
  box.style.left = ((ev.clientX || 100) + 6) + 'px';
  box.style.top  = ((ev.clientY || 100) + 6) + 'px';
  box.style.display = 'block';
  box.querySelectorAll('.mch-row').forEach(row => {
    const id = Number(row.dataset.id);
    row.addEventListener('mouseenter', () => highlightSegOnMap(id));
    row.addEventListener('mouseleave', clearMergeHighlight);
    row.addEventListener('click', () => { box.style.display = 'none'; clearMergeHighlight(); toggleMergeSeg(id); });
  });
}

function highlightSegOnMap(id) {
  clearMergeHighlight();
  const seg = projectData.segments.find(s => s.id === id);
  if (!seg) return;
  const path = lngLatToGM(seg.coords);
  const hl = new google.maps.Polyline({ path, map: googleMap, strokeColor: '#f59e0b', strokeWeight: 10, strokeOpacity: .9, zIndex: 20, clickable: false });
  const a = path[path.length - 2], b = path[path.length - 1];
  const arrow = new google.maps.Marker({ position: b, map: googleMap, clickable: false, zIndex: 21,
    icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, scale: 4, rotation: headingDeg(a, b), fillColor: '#f59e0b', fillOpacity: 1, strokeColor: '#fff', strokeWeight: 1 } });
  mergeHighlight.push(hl, arrow);
}
function clearMergeHighlight() { mergeHighlight.forEach(m => m.setMap(null)); mergeHighlight = []; }
```

- [ ] **Step 7: Merge-mode panel** (sticky controls + flattened toggle list):

```javascript
function renderMergePanel() {
  const container = document.getElementById('corridor-list');
  if (!projectData) { container.innerHTML = ''; return; }
  const ids = [...mergeSelection];
  const v = ids.length >= 2 ? validateChain(ids) : { ok: false, reason: ids.length ? 'Select at least two segments.' : 'Select segments to merge.', order: [] };
  const orderIndex = {}; v.order.forEach((id, i) => orderIndex[id] = i + 1);

  // name pre-fill: unanimous saved name
  const selSegs = ids.map(id => projectData.segments.find(s => s.id === id)).filter(Boolean);
  const names = [...new Set(selSegs.map(s => s.name).filter(Boolean))];
  const prefill = (names.length === 1) ? names[0] : '';

  const groups = getVisibleCorridors().filter(c => itemMatchesSearch(c));
  const rowHtml = (seg) => {
    const sel = mergeSelection.has(seg.id);
    const num = orderIndex[seg.id] || '';
    const nm = seg.name || seg.route_name_imported || ('Segment ' + seg.id);
    return `<div class="merge-seg-row${sel ? ' sel' : ''}" data-id="${seg.id}">
      <span class="ms-badge">${num}</span>
      <span class="ms-name">${esc(nm)}</span>
      <span class="ms-len">${fmtLen(segLengthM(seg.coords))}</span></div>`;
  };

  container.innerHTML = `
    <div id="merge-controls">
      <div class="mc-row"><strong>Merge</strong> · <span>${ids.length} selected</span>
        <span style="flex:1"></span>
        <button id="btn-cancel-merge">Cancel</button></div>
      <div class="mc-banner ${v.ok ? 'mc-ok' : 'mc-bad'}">${ids.length < 2 ? 'Pick 2+ connected segments' : (v.ok ? '✓ ' + esc(v.reason) : '✗ ' + esc(v.reason))}</div>
      <input id="merge-name" type="text" placeholder="Name (optional)…" value="${esc(prefill)}"/>
      <button id="btn-do-merge" ${v.ok ? '' : 'disabled'}>Merge ${ids.length} → 1</button>
    </div>
    ${groups.map(g => `
      <div class="merge-group-h">${esc(g.isStandalone ? 'Standalone' : (g.name || g.cor_code))}</div>
      ${segmentsOfItem(g).map(rowHtml).join('')}
    `).join('')}
  `;

  container.querySelectorAll('.merge-seg-row').forEach(row =>
    row.addEventListener('click', () => toggleMergeSeg(Number(row.dataset.id))));
  document.getElementById('btn-cancel-merge').addEventListener('click', exitMergeMode);
  document.getElementById('btn-do-merge').addEventListener('click', doMerge);
}
```

Note: the "Standalone" group header repeats once per standalone item (each standalone is its own item). Acceptable; or dedupe by rendering standalone rows under a single header — keep simple for v1.

- [ ] **Step 8: Submit**

```javascript
async function doMerge() {
  const ids = [...mergeSelection];
  const name = (document.getElementById('merge-name') || {}).value || '';
  try {
    const res = await apiFetch(`/api/projects/${currentProjectId}/merge`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segment_ids: ids, name })
    });
    exitMergeMode();
    projectData = await apiFetch(`/api/projects/${currentProjectId}`);
    updateProgressUI();
    renderCorrList();
    renderMap();
    if (res.merged_segment_id) {
      const corrId = res.corridor_id !== null ? res.corridor_id : ('sa_' + res.merged_segment_id);
      selectCorridor(corrId, 'merge');
    }
    showSaveIndicator();
  } catch (err) {
    alert('Merge failed: ' + err.message);
  }
}
```

- [ ] **Step 9: Wire button, show on project open, Esc to exit**

In `openProject`, after showing next-unnamed button: `document.getElementById('btn-merge').style.display = 'block';`
In `btn-back` handler: `document.getElementById('btn-merge').style.display = 'none'; if (mergeMode) exitMergeMode();`
In `boot()` controls: 
```javascript
  document.getElementById('btn-merge').addEventListener('click', () => mergeMode ? exitMergeMode() : enterMergeMode());
```
In the global keydown Escape handler, handle merge first:
```javascript
  if (e.key === 'Escape') {
    const box = document.getElementById('merge-chooser');
    if (box && box.style.display === 'block') { box.style.display = 'none'; clearMergeHighlight(); return; }
    if (mergeMode) { exitMergeMode(); return; }
    deselectCorridor();
    return;
  }
```
Also dismiss the chooser on outside click — in `boot()`:
```javascript
  document.addEventListener('click', e => {
    const box = document.getElementById('merge-chooser');
    if (box && box.style.display === 'block' && !e.target.closest('#merge-chooser')) {
      box.style.display = 'none'; clearMergeHighlight();
    }
  });
```

- [ ] **Step 10: Verify (manual)** — start server; open project; click **Merge**: map shows all segments individually, panel shows flattened toggle list + sticky controls. Select 2+ connected segments (map or panel) → banner turns green, badges number them; click an overlap spot (twin) → chooser appears, hovering a row highlights it with an arrow. Click **Merge** → originals vanish, new merged segment appears (standalone or in corridor) and is selected; **Export** contains only the merged one with `merged_from`. Esc/Cancel exits without changes.

- [ ] **Step 11: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): dedicated Merge mode (select, validate, overlap chooser, submit)"
```

---

## Self-Review

**Spec coverage:**
- Data model `merged_into` → Task 1. ✓
- Validation/ordering/geometry (`order_chain`, `merge_coords`, tolerance, reject reasons) → Task 2. ✓
- Endpoint + corridor placement + provenance + new UUID → Task 3. ✓
- Active-segment filtering (get_project, export, list_projects, build_export) → Task 3. ✓
- Export impact (only live, merged_from in props) → Task 3 tests. ✓
- Corridor-name display fix → Task 4. ✓
- Panel search → Task 5. ✓
- Merge mode (dedicated, plain-click toggle, both map+panel, overlap chooser w/ hover arrows, sticky controls, name pre-fill) → Task 6. ✓
- Edge cases (anti-parallel, branch, gap, <2, whole-corridor collapse) → Tasks 2 & 3 tests. ✓

**Placeholder scan:** No TBD/TODO; all steps carry real code/commands. ✓

**Type consistency:** `order_chain`/`merge_coords`/`MergeError` names match across Tasks 2-3; endpoint returns `{merged_segment_id, merged_uuid, corridor_id}` consumed verbatim in Task 6 `doMerge`; `validateChain` shape `{ok, reason, order}` used consistently in Task 6. ✓

**Out of scope confirmed:** unmerge UI, split, reverse — not in any task (matches spec §2). ✓
