# Merge Membership Model + Extend Merge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Replace single-parent `merged_into` with a many-to-many `merge_members` model (overlap), and add Extend merge (modify-in-place or create-new).

**Architecture:** `merge_members(project_id, merge_uuid, member_uuid, seq)` is the source of truth; a segment is hidden iff it is a member of any merge. Backend endpoints rewritten to membership; new `/atoms`, `/leaf_atoms`, and `modify_id` on `/merge`. Frontend Merge mode runs off the `/atoms` feed; Extend pre-seeds it and offers Modify/Create-new.

**Tech Stack:** Python/FastAPI/SQLite, pytest, vanilla JS + Google Maps.

## Global Constraints

- Hidden = `uuid IN (SELECT member_uuid FROM merge_members WHERE project_id=?)`.
- Migration additive + idempotent; no row modified/deleted; test on a copy of the real DB.
- `order_chain`/`merge_coords` (merge.py) unchanged. Tolerance `1e-4`.
- Spec: `docs/superpowers/specs/2026-06-29-merge-membership-extend-design.md`.

---

### Task 1: `merge_members` table + back-fill migration

**Files:** `db.py`; Test `tests/test_db.py`.

- [ ] **Step 1: failing tests** — append to `tests/test_db.py`:
```python
import json as _json

def test_merge_members_table_exists(tmp_path):
    conn = db.connect(str(tmp_path / "t.db")); db.init_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "merge_members" in names

def test_migration_backfills_from_props(tmp_path):
    p = str(tmp_path / "legacy.db")
    conn = db.connect(p); db.init_db(conn)
    conn.execute("INSERT INTO projects(id,name) VALUES(1,'p')")
    # a legacy merged road M with props.merged_from = [A,B,C]
    conn.execute("INSERT INTO segments(project_id,uuid,geom,props) VALUES(1,'M','[]',?)",
                 (_json.dumps({"merged_from": ["A", "B", "C"]}),))
    conn.commit()
    db.init_db(conn)   # re-run -> back-fill
    rows = conn.execute("SELECT member_uuid, seq FROM merge_members WHERE merge_uuid='M' ORDER BY seq").fetchall()
    assert [(r["member_uuid"], r["seq"]) for r in rows] == [("A",0),("B",1),("C",2)]
    db.init_db(conn)   # idempotent
    assert conn.execute("SELECT COUNT(*) n FROM merge_members").fetchone()["n"] == 3
```

- [ ] **Step 2: run -> fail.** `python -m pytest tests/test_db.py -q`

- [ ] **Step 3: implement** — in `db.py`: add `import json` at top; add table to `SCHEMA_SQL` (after the segments table):
```sql
CREATE TABLE IF NOT EXISTS merge_members(
  project_id INTEGER, merge_uuid TEXT, member_uuid TEXT, seq INTEGER,
  PRIMARY KEY(project_id, merge_uuid, member_uuid),
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
```
and extend `init_db`:
```python
def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE segments ADD COLUMN merged_into TEXT DEFAULT NULL")
    # Back-fill membership from legacy props.merged_from (idempotent, additive).
    for r in conn.execute("SELECT project_id, uuid, props FROM segments"):
        try:
            mf = json.loads(r["props"] or "{}").get("merged_from")
        except Exception:
            mf = None
        if mf:
            for seq, cu in enumerate(mf):
                conn.execute("INSERT OR IGNORE INTO merge_members(project_id,merge_uuid,member_uuid,seq) VALUES(?,?,?,?)",
                             (r["project_id"], r["uuid"], cu, seq))
    conn.commit()
```

- [ ] **Step 4: run -> pass.** Then `python -m pytest -q` (existing still pass — back-fill is a no-op for fresh test DBs because they have no `merged_from`... but existing merge tests CREATE merges with `props.merged_from` set, and init_db runs once at import, before those rows exist, so no interference). Commit:
```bash
git add db.py tests/test_db.py
git commit -m "feat(db): merge_members table + back-fill migration"
```

---

### Task 2: Rewrite endpoints to membership + new endpoints

**Files:** `app.py`, `export.py`; Test `tests/test_api.py`.

**Interfaces produced:** `_membership(c,pid)`; visible logic; `/merge` w/ `modify_id`;
`/unmerge` (edge delete); `/parts` (membership); `/atoms`; `/leaf_atoms`.

- [ ] **Step 1: failing tests** — append to `tests/test_api.py` (after existing). First add an `SD` atom to the fixture so M2 = PA-S3,SC,SD (3 atoms) is possible:

Add to `tests/fixtures/merge_sample.geojson` (a feature after `SC`, before the twins):
```json
 {"type":"Feature","geometry":{"type":"LineString","coordinates":[[91.14,25.14],[91.15,25.15]]},
  "properties":{"uuid":"SD","route_name":"Route D","parent_route_id":null,"has_children":0,"sync_status":"synced","segment_order":null}},
```

Tests:
```python
def test_overlap_atom_in_two_merges(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    m1 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids":[ids["PA-S1"],ids["PA-S2"],ids["PA-S3"]],"name":"A"}).json()
    # PA-S3 is now hidden but still selectable; build M2 = PA-S3, SC, SD
    ids2 = _ids_by_uuid(c, pid)   # PA-S3 no longer visible -> use atoms feed
    atoms = {a["uuid"]: a["id"] for a in c.get(f"/api/projects/{pid}/atoms").json()["atoms"]}
    m2 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids":[atoms["PA-S3"],atoms["SC"],atoms["SD"]],"name":"D"}).json()
    assert m2.get("merged_segment_id")
    exp = c.get(f"/api/projects/{pid}/export").json()
    feats = {f["properties"]["uuid"]: f for f in exp["leaves"]["features"]}
    assert m1["merged_uuid"] in feats and m2["merged_uuid"] in feats     # both exported
    assert feats[m1["merged_uuid"]]["properties"]["merged_from"] == ["PA-S1","PA-S2","PA-S3"]
    assert feats[m2["merged_uuid"]]["properties"]["merged_from"] == ["PA-S3","SC","SD"]
    # PA-S3 is in both
    p1 = c.get(f"/api/segments/{m1['merged_segment_id']}/parts").json()["parts"]
    p2 = c.get(f"/api/segments/{m2['merged_segment_id']}/parts").json()["parts"]
    assert "PA-S3" in [x["uuid"] for x in p1] and "PA-S3" in [x["uuid"] for x in p2]

def test_atoms_feed_includes_hidden_with_counts(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    c.post(f"/api/projects/{pid}/merge",
           json={"segment_ids":[ids["PA-S1"],ids["PA-S2"],ids["PA-S3"]],"name":"A"})
    atoms = {a["uuid"]: a for a in c.get(f"/api/projects/{pid}/atoms").json()["atoms"]}
    assert "PA-S1" in atoms and atoms["PA-S1"]["in_merges"] == 1   # hidden but present
    assert atoms["SC"]["in_merges"] == 0
    # the merge itself is NOT an atom
    assert all(not a["uuid"].count("-") >= 4 for a in atoms.values())  # uuids are source codes, not uuid4

def test_unmerge_frees_only_unused(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    m1 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids":[ids["PA-S1"],ids["PA-S2"],ids["PA-S3"]],"name":"A"}).json()["merged_segment_id"]
    atoms = {a["uuid"]: a["id"] for a in c.get(f"/api/projects/{pid}/atoms").json()["atoms"]}
    c.post(f"/api/projects/{pid}/merge", json={"segment_ids":[atoms["PA-S3"],atoms["SC"],atoms["SD"]],"name":"D"})
    c.post(f"/api/segments/{m1}/unmerge")
    vis = {s["uuid"] for s in c.get(f"/api/projects/{pid}").json()["segments"]}
    assert "PA-S1" in vis and "PA-S2" in vis    # freed
    assert "PA-S3" not in vis                    # still in M2

def test_leaf_atoms_flat_and_nested(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    m1 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids":[ids["PA-S1"],ids["PA-S2"],ids["PA-S3"]],"name":"A"}).json()["merged_segment_id"]
    la = c.get(f"/api/segments/{m1}/leaf_atoms").json()["atoms"]
    assert [x["uuid"] for x in la] == ["PA-S1","PA-S2","PA-S3"]
    # nested: M2 = M1 + SC (general endpoint allows selecting a merge)
    m2 = c.post(f"/api/projects/{pid}/merge", json={"segment_ids":[m1, ids["SC"]],"name":"AC"}).json()["merged_segment_id"]
    la2 = c.get(f"/api/segments/{m2}/leaf_atoms").json()["atoms"]
    assert [x["uuid"] for x in la2] == ["PA-S1","PA-S2","PA-S3","SC"]

def test_modify_in_place(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    m1 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids":[ids["PA-S2"],ids["PA-S3"]],"name":"A"}).json()
    mid, muuid = m1["merged_segment_id"], m1["merged_uuid"]
    atoms = {a["uuid"]: a["id"] for a in c.get(f"/api/projects/{pid}/atoms").json()["atoms"]}
    # extend to PA-S1,PA-S2,PA-S3 and modify in place
    r = c.post(f"/api/projects/{pid}/merge",
               json={"segment_ids":[atoms["PA-S1"],atoms["PA-S2"],atoms["PA-S3"]],"name":"A2","modify_id":mid})
    assert r.status_code == 200 and r.json()["merged_segment_id"] == mid
    seg = [s for s in c.get(f"/api/projects/{pid}").json()["segments"] if s["id"] == mid][0]
    assert seg["uuid"] == muuid and seg["name"] == "A2"          # same road, new name
    parts = c.get(f"/api/segments/{mid}/parts").json()["parts"]
    assert [x["uuid"] for x in parts] == ["PA-S1","PA-S2","PA-S3"]
```

- [ ] **Step 2: run -> fail.**

- [ ] **Step 3: implement `app.py`.** Add the helper near the top (after `_point_at`):
```python
def _membership(c, pid):
    member_set = set(); members_of = {}
    for r in c.execute("SELECT merge_uuid, member_uuid FROM merge_members WHERE project_id=? ORDER BY merge_uuid, seq", (pid,)):
        members_of.setdefault(r["merge_uuid"], []).append(r["member_uuid"])
        member_set.add(r["member_uuid"])
    return member_set, members_of
```
Rewrite `get_project` segment loop to fetch ALL segments and filter by `member_set`; set `merged_from = members_of.get(uuid)`, `is_merged = uuid in members_of`. Keep the corridor-hiding block.

`list_projects`: change both subqueries to
`AND s.uuid NOT IN (SELECT member_uuid FROM merge_members mm WHERE mm.project_id=p.id)`.

`export_project`: fetch all segments, keep those whose uuid ∉ member_set; for each whose uuid ∈ members_of, set `props["merged_from"] = members_of[uuid]` (re-dump into the row's `props`) so `build_export` emits it. (build_export already drops empty corridors.)

Rewrite `merge_segments` to accept `modify_id` and use membership (full new body):
```python
@app.post("/api/projects/{pid}/merge")
def merge_segments(pid: int, body: dict = Body(...)):
    ids = body.get("segment_ids") or []
    name = (body.get("name") or "").strip()
    modify_id = body.get("modify_id")
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(400, "Select at least two segments to merge.")
    c = conn()
    try:
        rows = []
        for sid in ids:
            r = c.execute("SELECT * FROM segments WHERE id=? AND project_id=?", (sid, pid)).fetchone()
            if not r:
                raise HTTPException(400, f"Segment {sid} not found.")
            rows.append(dict(r))
        segs = [{"coords": json.loads(r["geom"]), "row": r} for r in rows]
        try:
            ordered = merge.order_chain(segs)
        except merge.MergeError as e:
            raise HTTPException(400, str(e))
        merged_coords = merge.merge_coords(ordered)
        ordered_rows = [o["row"] for o in ordered]
        member_uuids = [r["uuid"] for r in ordered_rows]

        # route_name_imported
        seen, names = set(), []
        for r in ordered_rows:
            rn = (r["route_name_imported"] or "").strip()
            if rn and rn not in seen:
                seen.add(rn); names.append(rn)
        route_name_imported = " + ".join(names) if names else "Merged segment"

        if modify_id is not None:
            tgt = c.execute("SELECT * FROM segments WHERE id=? AND project_id=?", (modify_id, pid)).fetchone()
            if not tgt:
                raise HTTPException(400, "modify target not found.")
            if modify_id in ids:
                raise HTTPException(400, "a road cannot contain itself.")
            has_members = c.execute("SELECT 1 FROM merge_members WHERE merge_uuid=? AND project_id=? LIMIT 1", (tgt["uuid"], pid)).fetchone()
            if not has_members:
                raise HTTPException(400, "modify target is not a merged road.")
            c.execute("UPDATE segments SET geom=?, name=?, route_name_imported=? WHERE id=?",
                      (json.dumps(merged_coords), name, route_name_imported, modify_id))
            c.execute("DELETE FROM merge_members WHERE merge_uuid=? AND project_id=?", (tgt["uuid"], pid))
            for seq, mu in enumerate(member_uuids):
                c.execute("INSERT OR IGNORE INTO merge_members(project_id,merge_uuid,member_uuid,seq) VALUES(?,?,?,?)",
                          (pid, tgt["uuid"], mu, seq))
            c.commit()
            return {"ok": True, "merged_segment_id": modify_id, "merged_uuid": tgt["uuid"], "corridor_id": tgt["corridor_id"]}

        # create new
        member_set, _ = _membership(c, pid)
        corr_ids = {r["corridor_id"] for r in ordered_rows}
        new_corr, new_seq = None, 0
        if len(corr_ids) == 1 and next(iter(corr_ids)) is not None:
            cid = next(iter(corr_ids))
            remaining = 0
            for r in c.execute("SELECT id, uuid FROM segments WHERE corridor_id=? AND project_id=?", (cid, pid)):
                if r["id"] not in ids and r["uuid"] not in member_set:
                    remaining += 1
            if remaining > 0:
                new_corr = cid
                new_seq = min(r["seq"] for r in ordered_rows)

        props = json.loads(ordered_rows[0]["props"] or "{}")
        for k in ("parent_route_id", "segment_order", "uuid", "merged_from"):
            props.pop(k, None)
        merged_uuid = str(uuidlib.uuid4())
        new_id = c.execute(
            """INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
               route_name_imported,name,sug_geocode,sug_roads,twin_uuid,merged_into)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, merged_uuid, new_corr, new_seq, json.dumps(merged_coords),
             json.dumps(props), route_name_imported, name, "", "", None, None)).lastrowid
        for seq, mu in enumerate(member_uuids):
            c.execute("INSERT OR IGNORE INTO merge_members(project_id,merge_uuid,member_uuid,seq) VALUES(?,?,?,?)",
                      (pid, merged_uuid, mu, seq))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "merged_segment_id": new_id, "merged_uuid": merged_uuid, "corridor_id": new_corr}
```

Rewrite `unmerge_segment`:
```python
@app.post("/api/segments/{sid}/unmerge")
def unmerge_segment(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        pid = r["project_id"]
        if c.execute("SELECT 1 FROM merge_members WHERE member_uuid=? AND project_id=? LIMIT 1", (r["uuid"], pid)).fetchone():
            raise HTTPException(400, "Segment is part of another road; unmerge that first.")
        members = [x["member_uuid"] for x in c.execute(
            "SELECT member_uuid FROM merge_members WHERE merge_uuid=? AND project_id=? ORDER BY seq", (r["uuid"], pid))]
        if not members:
            raise HTTPException(400, "Segment is not a merged segment.")
        c.execute("DELETE FROM merge_members WHERE merge_uuid=? AND project_id=?", (r["uuid"], pid))
        c.execute("DELETE FROM segments WHERE id=?", (sid,))
        # which freed members are now visible?
        freed = []
        for mu in members:
            if not c.execute("SELECT 1 FROM merge_members WHERE member_uuid=? AND project_id=? LIMIT 1", (mu, pid)).fetchone():
                row = c.execute("SELECT id FROM segments WHERE uuid=? AND project_id=?", (mu, pid)).fetchone()
                if row: freed.append(row["id"])
        c.commit()
    finally:
        c.close()
    return {"ok": True, "restored_ids": freed, "count": len(members)}
```

Rewrite `segment_parts` to use membership:
```python
@app.get("/api/segments/{sid}/parts")
def segment_parts(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        pid = r["project_id"]
        member_uuids = [x["member_uuid"] for x in c.execute(
            "SELECT member_uuid FROM merge_members WHERE merge_uuid=? AND project_id=? ORDER BY seq", (r["uuid"], pid))]
        if not member_uuids:
            raise HTTPException(400, "Segment is not a merged segment.")
        _, members_of = _membership(c, pid)
        parts = []
        for mu in member_uuids:
            x = c.execute("SELECT * FROM segments WHERE uuid=? AND project_id=?", (mu, pid)).fetchone()
            if not x: continue
            mm = members_of.get(mu)
            parts.append({"id": x["id"], "uuid": x["uuid"], "coords": json.loads(x["geom"]),
                          "name": x["name"], "route_name_imported": x["route_name_imported"],
                          "is_merged": bool(mm), "merged_count": len(mm) if mm else 0})
    finally:
        c.close()
    return {"parent_id": sid, "parts": parts}
```

Add new endpoints:
```python
@app.get("/api/projects/{pid}/atoms")
def project_atoms(pid: int):
    c = conn()
    try:
        merges = {r["merge_uuid"] for r in c.execute("SELECT DISTINCT merge_uuid FROM merge_members WHERE project_id=?", (pid,))}
        incnt = {}
        for r in c.execute("SELECT member_uuid, COUNT(*) n FROM merge_members WHERE project_id=? GROUP BY member_uuid", (pid,)):
            incnt[r["member_uuid"]] = r["n"]
        cor_code = {r["id"]: r["cor_code"] for r in c.execute("SELECT id, cor_code FROM corridors WHERE project_id=?", (pid,))}
        atoms = []
        for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id, seq", (pid,)):
            if r["uuid"] in merges:
                continue   # it's a merge, not an atom
            atoms.append({"id": r["id"], "uuid": r["uuid"], "coords": json.loads(r["geom"]),
                          "name": r["name"], "route_name_imported": r["route_name_imported"],
                          "corridor_id": r["corridor_id"], "cor_code": cor_code.get(r["corridor_id"]),
                          "in_merges": incnt.get(r["uuid"], 0)})
    finally:
        c.close()
    return {"atoms": atoms}

@app.get("/api/segments/{sid}/leaf_atoms")
def segment_leaf_atoms(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        pid = r["project_id"]
        _, members_of = _membership(c, pid)
        if r["uuid"] not in members_of:
            raise HTTPException(400, "Segment is not a merged segment.")
        out = []
        seen = set()
        def walk(u):
            if u in members_of:
                for m in members_of[u]:
                    walk(m)
            else:
                if u in seen: return
                seen.add(u)
                row = c.execute("SELECT id, uuid FROM segments WHERE uuid=? AND project_id=?", (u, pid)).fetchone()
                if row: out.append({"id": row["id"], "uuid": row["uuid"]})
        walk(r["uuid"])
    finally:
        c.close()
    return {"atoms": out}
```

- [ ] **Step 4: run -> pass** (`python -m pytest tests/test_api.py tests/test_export.py -q`), then full `python -m pytest -q`. Commit:
```bash
git add app.py export.py tests/test_api.py tests/fixtures/merge_sample.geojson
git commit -m "feat(api): membership model (overlap) + atoms/leaf_atoms + modify_id"
```

---

### Task 3: Frontend — Merge mode over `/atoms`

**Files:** `static/index.html`.

- [ ] **Step 1:** add state `let mergeAtoms = []; const atomBy = new Map();` and a loader:
```javascript
async function loadMergeAtoms() {
  const res = await apiFetch(`/api/projects/${currentProjectId}/atoms`);
  mergeAtoms = res.atoms || [];
  atomBy.clear(); mergeAtoms.forEach(a => atomBy.set(a.id, a));
}
function atomById(id) { return atomBy.get(id); }
```
- [ ] **Step 2:** make `enterMergeMode` async and load atoms first; replace its body's `renderMergeMap()/renderMergePanel()` to run after `await loadMergeAtoms()`. In the merge-mode helpers, replace `liveSegments()` and `projectData.segments.find(...)` with `mergeAtoms` / `atomById(...)`:
  - `orderedSelectionSegs`, `mergeCandidates`, `validateChain` → resolve segs via `atomById`.
  - `renderMergeMap`, `onMergeMapClick`, `segmentNearLatLng` → iterate `mergeAtoms`.
  - `renderMergePanel` → group `mergeAtoms` by `cor_code` (+ 'Standalone'); row shows an `in N` tag when `a.in_merges>0`; selected/candidate/dim classes as today.
- [ ] **Step 3:** map "reused" style — atoms with `in_merges>0` drawn with a dashed/secondary stroke when not selected; list rows get a muted `· in N` suffix.
- [ ] **Step 4: verify (puppeteer in Task 5).** Commit:
```bash
git add static/index.html
git commit -m "feat(ui): merge mode runs off the atoms feed (overlap-aware, marks reused)"
```

---

### Task 4: Frontend — Extend merge + Modify/Create-new save

**Files:** `static/index.html`.

- [ ] **Step 1:** state `let extendContext = null;` and an Extend button in `buildSegCard` (merged segments only), wired in `wireSegCard`:
```javascript
const extendBtn = seg.is_merged ? `<button class="preview-btn" data-extend="${seg.id}">Extend merge</button>` : '';
```
(reuse `.preview-btn` style; place after the Preview button)
Wire:
```javascript
const extendBtn = card.querySelector('[data-extend]');
if (extendBtn) extendBtn.addEventListener('click', (e) => { e.stopPropagation(); startExtend(seg); });
```
- [ ] **Step 2:** functions:
```javascript
async function startExtend(seg) {
  if (mergeMode) return;
  let la;
  try { la = await apiFetch(`/api/segments/${seg.id}/leaf_atoms`); }
  catch (e) { alert('Extend failed: ' + e.message); return; }
  extendContext = { id: seg.id, name: seg.name || '' };
  await enterMergeMode();                       // loads atoms, clears selection
  mergeSelection = new Set((la.atoms || []).map(a => a.id));
  mergeName = extendContext.name;
  renderMergeMap(); renderMergePanel();
}
```
(ensure `enterMergeMode` resets `extendContext` to null only when NOT called from extend — set `extendContext=null` in the plain Merge-button handler instead, and have `enterMergeMode` leave it as-is.)
- [ ] **Step 3:** in `renderMergePanel`, when `extendContext` is set, render the save area as a name input (value `mergeName`) + two buttons instead of the single Merge button:
```javascript
// inside #merge-controls, replace the single button when extendContext:
extendContext
 ? `<input id="merge-name" type="text" value="${esc(mergeName)}" placeholder="Name…"/>
    <div style="display:flex;gap:6px">
      <button id="btn-modify-merge" ${v.ok?'':'disabled'}>Modify this stretch</button>
      <button id="btn-create-merge" ${v.ok?'':'disabled'}>Create new stretch</button>
    </div>`
 : `<input ...single... /><button id="btn-do-merge" ...>Merge…</button>`
```
Wire `btn-modify-merge` → `doMerge(extendContext.id)`, `btn-create-merge` → `doMerge(null)`; keep `btn-do-merge` → `doMerge(null)`.
- [ ] **Step 4:** update `doMerge(modifyId)`:
```javascript
async function doMerge(modifyId = null) {
  const ids = [...mergeSelection];
  const body = { segment_ids: ids, name: mergeName };
  if (modifyId) body.modify_id = modifyId;
  try {
    const res = await apiFetch(`/api/projects/${currentProjectId}/merge`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    exitMergeMode();
    projectData = await apiFetch(`/api/projects/${currentProjectId}`);
    updateProgressUI(); renderCorrList(); renderMap();
    if (res.merged_segment_id) {
      const corrId = res.corridor_id !== null ? res.corridor_id : ('sa_' + res.merged_segment_id);
      selectCorridor(corrId, 'merge');
    }
    showSaveIndicator();
  } catch (err) { alert('Merge failed: ' + err.message); }
}
```
- [ ] **Step 5:** clear `extendContext = null` in `exitMergeMode` and in the header Merge-button handler (so plain merge isn't in extend mode). Commit:
```bash
git add static/index.html
git commit -m "feat(ui): Extend merge (pre-seed) with Modify-in-place / Create-new"
```

---

### Task 5: Verify (incl. migration on a copy of the real DB)

- [ ] **Step 1:** `node --check` the extracted `<script>` → "JS OK".
- [ ] **Step 2:** `python -m pytest -q` → all pass.
- [ ] **Step 3: migration on real data** — copy `roadnamer.db` to scratch, run the server against the copy, hit `/api/projects` and one project's `/api/projects/{pid}` and `/export`; assert no 500s and that segment counts are sane (no rows lost). Confirms the back-fill converts production data cleanly.
- [ ] **Step 4: puppeteer** — (a) enter Merge mode, confirm the list includes a reused atom after a merge; (b) Extend a merged road → Modify (same road id, new parts); (c) Extend → Create new (original still present, new road created). Capture console errors (expect none).
- [ ] **Step 5:** commit any fixes; report.

## Self-Review
- Spec coverage: membership table+migration (T1); endpoint rewrites + overlap + modify + atoms + leaf_atoms (T2); merge mode over atoms (T3); extend + modify/create save (T4); migration-on-real-db + frontend repros (T5). ✓
- Placeholders: none. ✓
- Type consistency: `/atoms` item shape and `leaf_atoms` `{id,uuid}` produced in T2, consumed in T3/T4; `doMerge(modifyId)` + `modify_id` body field consistent across T2/T4. ✓
