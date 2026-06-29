# Merged-Segment Parts Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Preview a merged segment's immediate parts (one level, drillable) on the map + an inline panel list, read-only, with bidirectional hover sync.

**Architecture:** One read-only backend endpoint returns a segment's immediate parts (works on soft-deleted rows for drill-down). The single-file frontend adds preview state, a "Preview parts" button on merged cards, a dimmed-map overlay of the current level's parts (alternating colours + badges + arrows), an inline breadcrumb+list block rendered via `buildSegCard`, drill-down, and hover sync.

**Tech Stack:** Python/FastAPI/SQLite, pytest, vanilla JS + Google Maps.

## Global Constraints

- Read-only: preview never mutates data.
- One level per drill step (breadcrumb to climb); not a full flatten.
- Parts ordered by the parent's `props.merged_from` order.
- Spec: `docs/superpowers/specs/2026-06-29-parts-preview-design.md`.

---

### Task 1: Backend — `GET /api/segments/{sid}/parts`

**Files:** Modify `app.py`; Test `tests/test_api.py`.

**Interfaces:** `{parent_id, parts:[{id,uuid,coords,name,route_name_imported,is_merged,merged_count}]}`; 400 if not merged; 404 if missing.

- [ ] **Step 1: Failing tests** — append to `tests/test_api.py`:

```python
def test_parts_of_whole_corridor_merge(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    mid = c.post(f"/api/projects/{pid}/merge",
                 json={"segment_ids": [ids["PA-S1"], ids["PA-S2"], ids["PA-S3"]], "name": "A"}
                 ).json()["merged_segment_id"]
    r = c.get(f"/api/segments/{mid}/parts")
    assert r.status_code == 200
    parts = r.json()["parts"]
    assert [p["uuid"] for p in parts] == ["PA-S1", "PA-S2", "PA-S3"]   # chain order
    assert all(p["is_merged"] is False for p in parts)
    assert all("coords" in p and len(p["coords"]) >= 2 for p in parts)

def test_parts_nested_and_drilldown(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    ids = _ids_by_uuid(c, pid)
    m1 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids": [ids["PA-S1"], ids["PA-S2"], ids["PA-S3"]], "name": "A"}
                ).json()["merged_segment_id"]
    m2 = c.post(f"/api/projects/{pid}/merge",
                json={"segment_ids": [m1, ids["SC"]], "name": "AC"}).json()["merged_segment_id"]
    top = c.get(f"/api/segments/{m2}/parts").json()["parts"]
    assert len(top) == 2
    m1part = [p for p in top if p["id"] == m1][0]
    assert m1part["is_merged"] is True and m1part["merged_count"] == 3
    # drill into M1 (now soft-deleted) by its id
    deep = c.get(f"/api/segments/{m1}/parts").json()["parts"]
    assert [p["uuid"] for p in deep] == ["PA-S1", "PA-S2", "PA-S3"]

def test_parts_rejects_non_merged(tmp_path):
    c = make_client(tmp_path)
    pid = upload_merge(c).json()["project_id"]
    sid = _ids_by_uuid(c, pid)["SC"]
    assert c.get(f"/api/segments/{sid}/parts").status_code == 400

def test_parts_404_missing(tmp_path):
    c = make_client(tmp_path)
    upload_merge(c)
    assert c.get(f"/api/segments/999999/parts").status_code == 404
```

- [ ] **Step 2: Run to fail** — `python -m pytest tests/test_api.py -q` → FAIL (405/404).

- [ ] **Step 3: Implement** in `app.py` (after the unmerge endpoint):

```python
@app.get("/api/segments/{sid}/parts")
def segment_parts(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        order = json.loads(r["props"] or "{}").get("merged_from") or []
        if not order:
            raise HTTPException(400, "Segment is not a merged segment.")
        rows = [dict(x) for x in c.execute(
            "SELECT * FROM segments WHERE merged_into=? AND project_id=?", (r["uuid"], r["project_id"]))]
    finally:
        c.close()
    rank = {u: i for i, u in enumerate(order)}
    rows.sort(key=lambda x: rank.get(x["uuid"], len(order)))
    parts = []
    for x in rows:
        mf = json.loads(x["props"] or "{}").get("merged_from")
        parts.append({"id": x["id"], "uuid": x["uuid"], "coords": json.loads(x["geom"]),
                      "name": x["name"], "route_name_imported": x["route_name_imported"],
                      "is_merged": bool(mf), "merged_count": len(mf) if mf else 0})
    return {"parent_id": sid, "parts": parts}
```

- [ ] **Step 4: Run to pass** — `python -m pytest tests/test_api.py -q` → PASS.
- [ ] **Step 5: Full suite + commit** — `python -m pytest -q`, then:
```bash
git add app.py tests/test_api.py
git commit -m "feat(api): GET /segments/{id}/parts for merged-segment preview"
```

---

### Task 2: Frontend — preview state, button, CSS

**Files:** Modify `static/index.html`.

- [ ] **Step 1: State** (near other merge state):
```javascript
let previewRootId = null;     // seg id whose card hosts the preview block
let previewStack = [];        // breadcrumb [{id,label}]
let previewParts = [];        // current level's parts
let previewPolylines = {};    // partId -> {core, casing, badge, arrow}
let previewHoverId = null;
```

- [ ] **Step 2: CSS** (near the merged-tag / unmerge CSS):
```css
.preview-btn { margin-top:7px; margin-left:6px; padding:4px 10px; font-size:11px; border-radius:6px; border:1px solid #c7d2fe; background:#eef2ff; color:#4338ca; cursor:pointer; }
.preview-btn:hover { background:#e0e7ff; }
.preview-block { margin-top:8px; border-top:1px dashed #e2e8f0; padding-top:8px; }
.preview-crumbs { font-size:11px; color:#64748b; margin-bottom:6px; }
.preview-crumbs .crumb { color:#4338ca; cursor:pointer; }
.preview-crumbs .crumb:hover { text-decoration:underline; }
.preview-part { display:flex; align-items:center; gap:8px; border:1px solid #e2e8f0; border-radius:6px; padding:6px 8px; margin:4px 0; }
.preview-part.pv-hover { border-color:#1e293b; background:#f8fafc; }
.preview-part.pv-merged { cursor:pointer; }
.preview-part .pv-num { width:18px;height:18px;border-radius:50%;color:#fff;font-size:10px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0; }
.preview-part .pv-name { flex:1;min-width:0;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }
.preview-part .pv-len { font-size:11px;color:#94a3b8;flex-shrink:0; }
.preview-close { margin-top:6px; padding:4px 10px; font-size:11px; border-radius:6px; border:1px solid #e2e8f0; background:#fff; color:#64748b; cursor:pointer; }
```

- [ ] **Step 3: Two-colour helper** — add:
```javascript
const PREVIEW_COLORS = ['#7c3aed', '#f59e0b'];
function previewColor(i) { return PREVIEW_COLORS[i % PREVIEW_COLORS.length]; }
```

- [ ] **Step 4: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): parts-preview state + styles"
```

---

### Task 3: Frontend — button on the card + inline block in buildSegCard

**Files:** Modify `static/index.html` (`buildSegCard`, `wireSegCard`).

- [ ] **Step 1: Render button + block** — in `buildSegCard`, after `unmergeBtn`:
```javascript
  const previewBtn = seg.is_merged ? `<button class="preview-btn" data-seg-id="${seg.id}">${previewRootId === seg.id ? 'Hide parts' : 'Preview parts'}</button>` : '';
  const previewBlock = (previewRootId === seg.id) ? buildPreviewBlock() : '';
```
and add to the returned card markup, after `${unmergeBtn}`:
```javascript
      ${previewBtn}
      ${previewBlock}
```

- [ ] **Step 2: buildPreviewBlock** — add:
```javascript
function buildPreviewBlock() {
  const crumbs = previewStack.map((c, i) =>
    `<span class="crumb" data-crumb="${i}">${esc(c.label)}</span>`).join(' › ');
  const rows = previewParts.map((p, i) => {
    const nm = p.name || p.route_name_imported || ('Segment ' + p.id);
    const tag = p.is_merged ? ` <span class="merged-tag">merged (${p.merged_count})</span>` : '';
    const len = fmtLen(segLengthM(p.coords));
    return `<div class="preview-part${p.is_merged ? ' pv-merged' : ''}" data-part-id="${p.id}">
      <span class="pv-num" style="background:${previewColor(i)}">${i + 1}</span>
      <span class="pv-name">${esc(nm)}${tag}</span>
      <span class="pv-len">${len}</span></div>`;
  }).join('');
  return `<div class="preview-block">
    <div class="preview-crumbs">Parts: ${crumbs}</div>
    ${rows}
    <button class="preview-close">Close preview</button>
  </div>`;
}
```

- [ ] **Step 3: Wire** — in `wireSegCard`, after the unmerge wiring:
```javascript
  const previewBtn = card.querySelector('.preview-btn');
  if (previewBtn) previewBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (previewRootId === seg.id) closePreview(); else enterPreview(seg);
  });
  if (previewRootId === seg.id) {
    card.querySelectorAll('.preview-crumbs .crumb').forEach(cr =>
      cr.addEventListener('click', (e) => { e.stopPropagation(); gotoCrumb(Number(cr.dataset.crumb)); }));
    card.querySelectorAll('.preview-part').forEach(row => {
      const pid = Number(row.dataset.partId);
      row.addEventListener('mouseenter', () => setPreviewHover(pid, true));
      row.addEventListener('mouseleave', () => setPreviewHover(pid, false));
      const part = previewParts.find(p => p.id === pid);
      if (part && part.is_merged) row.addEventListener('click', (e) => { e.stopPropagation(); drillPreview(part); });
    });
    const closeBtn = card.querySelector('.preview-close');
    if (closeBtn) closeBtn.addEventListener('click', (e) => { e.stopPropagation(); closePreview(); });
  }
```

- [ ] **Step 4: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): preview button + inline parts block with breadcrumb"
```

---

### Task 4: Frontend — preview map overlay, drill, hover sync, lifecycle

**Files:** Modify `static/index.html`.

- [ ] **Step 1: Core functions** — add (near the merge section):
```javascript
async function loadPreviewLevel(id) {
  const res = await apiFetch(`/api/segments/${id}/parts`);
  previewParts = res.parts || [];
}

async function enterPreview(seg) {
  if (mergeMode) return;
  previewRootId = seg.id;
  previewStack = [{ id: seg.id, label: seg.name || seg.route_name_imported || ('Segment ' + seg.id) }];
  try { await loadPreviewLevel(seg.id); } catch (e) { alert('Preview failed: ' + e.message); clearPreviewState(); return; }
  renderCorrList();
  renderPreviewMap();
  // keep the hosting card open
  const card = document.getElementById(`seg-card-${seg.id}`);
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function drillPreview(part) {
  previewStack.push({ id: part.id, label: part.name || part.route_name_imported || ('Segment ' + part.id) });
  try { await loadPreviewLevel(part.id); } catch (e) { alert('Preview failed: ' + e.message); return; }
  renderCorrList();
  renderPreviewMap();
}

async function gotoCrumb(i) {
  previewStack = previewStack.slice(0, i + 1);
  try { await loadPreviewLevel(previewStack[i].id); } catch (e) { return; }
  renderCorrList();
  renderPreviewMap();
}

function clearPreviewState() {
  previewRootId = null; previewStack = []; previewParts = [];
  Object.values(previewPolylines).forEach(o => { Object.values(o).forEach(x => x && x.setMap && x.setMap(null)); });
  previewPolylines = {}; previewHoverId = null;
}

function closePreview() {
  clearPreviewState();
  renderMap();
  renderCorrList();
}

function renderPreviewMap() {
  if (!googleMap) return;
  // dim existing
  Object.values(corridorPolylines).forEach(pl => pl.setOptions({ strokeOpacity: 0.12 }));
  childPolylines.forEach(p => { if (p.setOptions) p.setOptions({ strokeOpacity: 0.12 }); });
  // clear prior preview polylines
  Object.values(previewPolylines).forEach(o => Object.values(o).forEach(x => x && x.setMap && x.setMap(null)));
  previewPolylines = {};
  previewParts.forEach((part, i) => {
    const path = lngLatToGM(part.coords);
    if (path.length < 2) return;
    const color = previewColor(i);
    const casing = new google.maps.Polyline({ path, map: null, strokeColor: '#ffffff', strokeWeight: 12, strokeOpacity: 0.95, zIndex: 30, clickable: false });
    const core = new google.maps.Polyline({ path, map: googleMap, strokeColor: color, strokeWeight: 7, strokeOpacity: 1, zIndex: 31 });
    const mid = midpointGM(part.coords);
    const badge = new google.maps.Marker({ position: mid, map: googleMap, zIndex: 50, clickable: false,
      icon: { path: google.maps.SymbolPath.CIRCLE, scale: 11, fillColor: color, fillOpacity: 1, strokeColor: '#fff', strokeWeight: 1.5 },
      label: { text: String(i + 1), color: '#fff', fontSize: '11px', fontWeight: '700' } });
    const a = path[path.length - 2], b = path[path.length - 1];
    const arrow = new google.maps.Marker({ position: b, map: googleMap, zIndex: 51, clickable: false,
      icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, scale: 3.8, rotation: headingDeg(a, b), fillColor: color, fillOpacity: 1, strokeColor: '#fff', strokeWeight: 1 } });
    previewPolylines[part.id] = { core, casing, badge, arrow };
    core.addListener('mouseover', () => setPreviewHover(part.id, true));
    core.addListener('mouseout', () => setPreviewHover(part.id, false));
  });
  // fit to the parts
  const bounds = new google.maps.LatLngBounds();
  previewParts.forEach(p => p.coords.forEach(([lng, lat]) => bounds.extend({ lat, lng })));
  if (!bounds.isEmpty()) googleMap.fitBounds(bounds, { top: 60, right: 60, bottom: 60, left: 60 });
}

function setPreviewHover(partId, on) {
  previewHoverId = on ? partId : (previewHoverId === partId ? null : previewHoverId);
  Object.entries(previewPolylines).forEach(([id, o]) => {
    const hov = Number(id) === previewHoverId;
    if (o.casing) o.casing.setMap(hov ? googleMap : null);
    if (o.core) o.core.setOptions({ zIndex: hov ? 41 : 31 });
  });
  document.querySelectorAll('.preview-part').forEach(row =>
    row.classList.toggle('pv-hover', Number(row.dataset.partId) === previewHoverId));
}
```

- [ ] **Step 2: Auto-close hooks** — add `if (previewRootId) clearPreviewState();` at the top of: `selectCorridor`, `deselectCorridor`, `enterMergeMode`, and the `btn-back` handler; and in the `panel-search` input handler before `renderPanel()`. (These already re-render the map/list afterwards, so the dimmed overlay is replaced by a fresh normal render.)

- [ ] **Step 3: Verify (manual + repro in Task 5).**

- [ ] **Step 4: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): preview map overlay, drill-down, hover sync, auto-close"
```

---

### Task 5: Verify end-to-end

- [ ] **Step 1: JS syntax** — extract `<script>` and `node --check` it. Expect "JS SYNTAX OK".
- [ ] **Step 2: Full pytest** — `python -m pytest -q`. Expect all pass.
- [ ] **Step 3: Headless-Chrome repro** — with maps disabled (panel path) plus a maps-enabled pass if practical: merge a corridor, open "Preview parts", assert the inline parts list renders and `/parts` returned 200; for a nested merge, click a `merged` part row → breadcrumb shows two crumbs and the deeper parts list renders; click Close → block gone. Capture console errors (expect none).
- [ ] **Step 4: Commit any fixes**, then report.

---

## Self-Review

**Spec coverage:** endpoint (T1), preview button + inline block + breadcrumb (T3), map overlay + drill + hover sync + auto-close (T4), state/colours/CSS (T2), tests (T1 + T5). ✓
**Placeholder scan:** none. ✓
**Type consistency:** `previewParts` item shape (`id,uuid,coords,name,route_name_imported,is_merged,merged_count`) produced in T1 and consumed in T3/T4; `enterPreview/drillPreview/gotoCrumb/closePreview/clearPreviewState/setPreviewHover/renderPreviewMap/buildPreviewBlock` names consistent across T3/T4. ✓
