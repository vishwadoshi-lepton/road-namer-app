# Merge UX Upgrades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add guided selection, Quick merge, Unmerge, and the merge-mode search fix to the existing segment-merge feature.

**Architecture:** Backend gains `is_merged`/`merged_from` in `get_project` and a `POST /api/segments/{sid}/unmerge` endpoint (unmerge = reactivate children by `merged_into`, delete the parent). Everything else is frontend in the single `static/index.html`: a shared modal + hint toast, a `renderPanel()` dispatcher, guided-selection colouring/guards, a per-corridor Quick-merge button, and Unmerge badges/buttons.

**Tech Stack:** Python/FastAPI/SQLite, pytest, vanilla JS + Google Maps.

## Global Constraints

- Tolerance `1e-4` everywhere (`near2` on client; `importer._near` on server).
- Never reverse geometry. Selection chains connect in stored directions.
- Unmerge restores exactly the immediate children (one level), recursive across nested merges.
- Live = `merged_into IS NULL`. Nothing physically deleted except the parent row removed by an explicit unmerge.
- Spec: `docs/superpowers/specs/2026-06-29-merge-ux-upgrades-design.md`.

---

### Task 1: Backend — `is_merged`/`merged_from` + unmerge endpoint

**Files:** Modify `app.py`; Test `tests/test_api.py`.

**Interfaces:**
- `get_project` segment dicts gain `merged_from: list|None`, `is_merged: bool`.
- `POST /api/segments/{sid}/unmerge` → `{ok, restored_ids:[int], count:int}`; 400 if not a live merged segment.

- [ ] **Step 1: Write failing tests** — append to `tests/test_api.py` (uses existing `upload_merge`, `_ids_by_uuid`, `make_client`):

```python
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
```

- [ ] **Step 2: Run to verify fail** — `python -m pytest tests/test_api.py -q` → FAIL (missing fields / 405).

- [ ] **Step 3: Implement** in `app.py`.

In `get_project`, parse props and expose the new fields. Replace the `segs.append({...})` block so it includes:

```python
        props = json.loads(r["props"] or "{}")
        mf = props.get("merged_from")
        segs.append({"id": r["id"], "uuid": r["uuid"], "corridor_id": r["corridor_id"],
                     "seq": r["seq"], "coords": coords, "mid": _point_at(coords, 0.5),
                     "route_name_imported": r["route_name_imported"], "name": r["name"],
                     "named": r["name"] != "", "sug_geocode": r["sug_geocode"],
                     "sug_roads": r["sug_roads"], "twin_uuid": twin,
                     "twin_name": twin_name if twin_name else None,
                     "merged_from": mf if mf else None, "is_merged": bool(mf)})
```

Add the endpoint (after the merge endpoint):

```python
@app.post("/api/segments/{sid}/unmerge")
def unmerge_segment(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=? AND merged_into IS NULL", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        children = [dict(x) for x in c.execute(
            "SELECT id FROM segments WHERE merged_into=? AND project_id=?", (r["uuid"], r["project_id"]))]
        if not children:
            raise HTTPException(400, "Segment is not a merged segment.")
        c.execute("UPDATE segments SET merged_into=NULL WHERE merged_into=? AND project_id=?",
                  (r["uuid"], r["project_id"]))
        c.execute("DELETE FROM segments WHERE id=?", (sid,))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "restored_ids": [x["id"] for x in children], "count": len(children)}
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_api.py -q` → PASS.
- [ ] **Step 5: Full suite + commit** — `python -m pytest -q` then:
```bash
git add app.py tests/test_api.py
git commit -m "feat(api): expose is_merged + unmerge endpoint (one-level recursive)"
```

---

### Task 2: Frontend — shared modal + hint toast

**Files:** Modify `static/index.html` (HTML containers, CSS, `showModal`/`closeModal`/`showHint`).

- [ ] **Step 1: Add containers** before `</body>` (near `#merge-chooser`):
```html
<div id="hint-toast"></div>
<div id="app-modal-overlay"><div id="app-modal"></div></div>
```

- [ ] **Step 2: CSS** (near other overlay CSS):
```css
#hint-toast { position:fixed; bottom:60px; left:50%; transform:translateX(-50%); z-index:10001;
  background:#1a1a2e; color:#fff; padding:8px 16px; border-radius:8px; font-size:13px; opacity:0;
  transition:opacity .2s; pointer-events:none; box-shadow:0 2px 10px rgba(0,0,0,.25); }
#hint-toast.show { opacity:.96; }
#app-modal-overlay { position:fixed; inset:0; background:rgba(15,19,32,.45); z-index:10002;
  display:none; align-items:center; justify-content:center; }
#app-modal-overlay.show { display:flex; }
#app-modal { background:#fff; border-radius:12px; padding:18px 18px 16px; width:min(380px,92%);
  box-shadow:0 10px 40px rgba(0,0,0,.3); }
#app-modal h3 { font-size:15px; font-weight:700; margin-bottom:8px; }
#app-modal p { font-size:13px; color:#475569; margin-bottom:12px; }
#app-modal input { width:100%; box-sizing:border-box; border:1px solid #e2e8f0; border-radius:7px;
  padding:8px 10px; font-size:13px; outline:none; margin-bottom:14px; }
#app-modal input:focus { border-color:#4f7bbf; }
#app-modal .modal-actions { display:flex; justify-content:flex-end; gap:8px; }
#app-modal button { padding:7px 14px; border-radius:7px; font-size:13px; cursor:pointer; border:1px solid transparent; }
#app-modal .m-cancel { background:#fff; color:#64748b; border-color:#e2e8f0; }
#app-modal .m-confirm { background:#6d3fbf; color:#fff; }
```

- [ ] **Step 3: JS helpers** (add near the merge functions):
```javascript
let _hintTimer = null;
function showHint(msg) {
  const t = document.getElementById('hint-toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(_hintTimer);
  _hintTimer = setTimeout(() => t.classList.remove('show'), 1600);
}

function closeModal() {
  document.getElementById('app-modal-overlay').classList.remove('show');
  document.getElementById('app-modal').innerHTML = '';
}
function showModal({ title, message = '', withInput = false, inputValue = '', confirmText = 'Confirm', onConfirm }) {
  const m = document.getElementById('app-modal');
  m.innerHTML = `
    <h3>${esc(title)}</h3>
    ${message ? `<p>${esc(message)}</p>` : ''}
    ${withInput ? `<input id="modal-input" type="text" value="${esc(inputValue)}" placeholder="Name…"/>` : ''}
    <div class="modal-actions">
      <button class="m-cancel">Cancel</button>
      <button class="m-confirm">${esc(confirmText)}</button>
    </div>`;
  document.getElementById('app-modal-overlay').classList.add('show');
  const input = document.getElementById('modal-input');
  if (input) { input.focus(); input.select(); }
  m.querySelector('.m-cancel').addEventListener('click', closeModal);
  m.querySelector('.m-confirm').addEventListener('click', () => {
    const val = input ? input.value : undefined;
    closeModal();
    if (onConfirm) onConfirm(val);
  });
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') m.querySelector('.m-confirm').click(); });
}
```

- [ ] **Step 4: Overlay click + Esc to close** — in `boot()` add:
```javascript
  document.getElementById('app-modal-overlay').addEventListener('click', e => {
    if (e.target.id === 'app-modal-overlay') closeModal();
  });
```
In the global Escape handler, close the modal first if open:
```javascript
    const ov = document.getElementById('app-modal-overlay');
    if (ov && ov.classList.contains('show')) { closeModal(); return; }
```
(place this as the first check inside the `if (e.key === 'Escape')` block)

- [ ] **Step 5: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): shared modal + hint toast"
```

---

### Task 3: Frontend — renderPanel dispatcher + search-in-merge fix + hide chips

**Files:** Modify `static/index.html`.

- [ ] **Step 1: Dispatcher** — add:
```javascript
function renderPanel() { if (mergeMode) renderMergePanel(); else renderCorrList(); }
```

- [ ] **Step 2: Search handler uses it** — change the `#panel-search` input listener body to:
```javascript
    searchQuery = e.target.value;
    renderPanel();
```

- [ ] **Step 3: Hide chips in merge mode** — in `enterMergeMode()` add
`document.getElementById('filter-bar').style.display = 'none';`
and in `exitMergeMode()` add
`document.getElementById('filter-bar').style.display = 'flex';`

- [ ] **Step 4: Verify (manual)** — enter Merge mode, type in search → the merge list filters (corridors/segments by saved name) and stays in merge mode; the All/Unnamed/Named chips are hidden; exit restores them.

- [ ] **Step 5: Commit**
```bash
git add static/index.html
git commit -m "fix(ui): search works inside Merge mode; hide status chips there"
```

---

### Task 4: Frontend — guided selection

**Files:** Modify `static/index.html` (`mergeCandidates`, `orderedSelectionSegs`, guarded `toggleMergeSeg`, `renderMergeMap`, `renderMergePanel`, `onMergeMapClick`).

- [ ] **Step 1: Helpers** — add:
```javascript
function orderedSelectionSegs() {
  const ids = [...mergeSelection];
  if (ids.length === 0) return [];
  if (ids.length === 1) { const s = projectData.segments.find(x => x.id === ids[0]); return s ? [s] : []; }
  const v = validateChain(ids);
  const order = v.ok ? v.order : ids;
  return order.map(id => projectData.segments.find(s => s.id === id)).filter(Boolean);
}

function mergeCandidates() {
  const segs = liveSegments();
  if (mergeSelection.size === 0) return new Set(segs.map(s => s.id));
  const order = orderedSelectionSegs();
  if (!order.length) return new Set();
  const head = order[0].coords[0];
  const tail = order[order.length - 1].coords[order[order.length - 1].coords.length - 1];
  const cand = new Set();
  segs.forEach(s => {
    if (mergeSelection.has(s.id)) return;
    const st = s.coords[0], en = s.coords[s.coords.length - 1];
    const appendOK = near2(st, tail) && !near2(en, head);
    const prependOK = near2(en, head) && !near2(st, tail);
    if (appendOK || prependOK) cand.add(s.id);
  });
  return cand;
}
```

- [ ] **Step 2: Guarded toggle** — replace `toggleMergeSeg` with:
```javascript
function toggleMergeSeg(id) {
  if (mergeSelection.has(id)) {
    const order = orderedSelectionSegs();
    const isEnd = order.length && (id === order[0].id || id === order[order.length - 1].id);
    if (!isEnd) { showHint('You can only remove from the ends'); return; }
    mergeSelection.delete(id);
  } else {
    if (!mergeCandidates().has(id)) { showHint("That segment doesn't connect to your selection"); return; }
    mergeSelection.add(id);
  }
  if (!mergeName) {
    const names = [...new Set([...mergeSelection]
      .map(sid => projectData.segments.find(s => s.id === sid))
      .filter(Boolean).map(s => s.name).filter(Boolean))];
    if (names.length === 1) mergeName = names[0];
  }
  renderMergeMap();
  renderMergePanel();
}
```

- [ ] **Step 3: Colour the map** — replace the body of `renderMergeMap`'s `liveSegments().forEach` loop colour logic so each polyline uses selected/candidate/dim:
```javascript
  const cands = mergeSelection.size ? mergeCandidates() : null;
  liveSegments().forEach(seg => {
    const path = lngLatToGM(seg.coords);
    if (path.length < 2) return;
    const isSel = mergeSelection.has(seg.id);
    let color, weight, opacity;
    if (isSel) { color = '#2563eb'; weight = 8; opacity = 1; }
    else if (mergeSelection.size === 0) { color = seg.named ? COLOR_NAMED : COLOR_UNNAMED; weight = 6; opacity = 1; }
    else if (cands.has(seg.id)) { color = '#ef4444'; weight = 7; opacity = 1; }
    else { color = '#cbd5e1'; weight = 5; opacity = 0.5; }
    const pl = new google.maps.Polyline({ path, map: googleMap, strokeColor: color, strokeWeight: weight, strokeOpacity: opacity, zIndex: isSel ? 5 : (color === '#ef4444' ? 4 : 2) });
    pl.addListener('click', (e) => onMergeMapClick(seg.id, e));
    mergeOverlayPolylines.push(pl);
    if (isSel) {
      const a = path[path.length - 2], b = path[path.length - 1];
      mergeOverlayPolylines.push(new google.maps.Marker({ position: b, map: googleMap, clickable: false, zIndex: 6,
        icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, scale: 3.5, rotation: headingDeg(a, b), fillColor: '#2563eb', fillOpacity: 1, strokeColor: '#fff', strokeWeight: 1 } }));
    }
  });
```
(Keep the existing `Object.values(corridorPolylines)...; clearChildPolylines(); clearMergeOverlay();` lines at the top of `renderMergeMap`.)

- [ ] **Step 4: Overlap auto-resolve + endpoint removal** — replace `onMergeMapClick`:
```javascript
function onMergeMapClick(segId, e) {
  const under = liveSegments().filter(s => segmentNearLatLng(s, e.latLng));
  const cands = mergeCandidates();
  const order = orderedSelectionSegs();
  const endIds = order.length ? [order[0].id, order[order.length - 1].id] : [];
  const actionable = under.filter(s => cands.has(s.id) || endIds.includes(s.id));
  if (actionable.length === 0) {
    showHint(under.length ? "That segment doesn't connect to your selection" : 'No segment here');
    return;
  }
  if (actionable.length === 1) { toggleMergeSeg(actionable[0].id); return; }
  showMergeChooser(actionable, e);
}
```

- [ ] **Step 5: Colour the panel rows** — in `renderMergePanel`, compute `const cands = mergeSelection.size ? mergeCandidates() : null;` and change `rowHtml` to add state classes:
```javascript
  const rowHtml = (seg) => {
    const sel = mergeSelection.has(seg.id);
    const num = orderIndex[seg.id] || '';
    const nm = seg.name || seg.route_name_imported || ('Segment ' + seg.id);
    const badge = seg.is_merged ? ` <span style="font-size:10px;color:#6d3fbf;font-weight:700">merged (${seg.merged_from.length})</span>` : '';
    let cls = 'merge-seg-row';
    if (sel) cls += ' sel';
    else if (mergeSelection.size && !cands.has(seg.id)) cls += ' dim';
    else if (mergeSelection.size && cands.has(seg.id)) cls += ' cand';
    return `<div class="${cls}" data-id="${seg.id}">
      <span class="ms-badge">${num}</span>
      <span class="ms-name">${esc(nm)}${badge}</span>
      <span class="ms-len">${fmtLen(segLengthM(seg.coords))}</span></div>`;
  };
```
Add CSS:
```css
.merge-seg-row.cand { border-color:#ef4444; }
.merge-seg-row.dim { opacity:.45; }
```

- [ ] **Step 6: Verify (manual)** — in Merge mode: first click colours that segment blue, connectable ones red, rest dimmed; clicking a dimmed one shows a hint and changes nothing; clicking another red one extends the chain (badges renumber); clicking a selected end removes it, a selected middle shows the "ends only" hint; a twin overlap auto-resolves once a direction is fixed, and prompts only when both are valid. Map and panel stay in sync.

- [ ] **Step 7: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): guided merge selection (blue/red/dim, auto-resolve, ends-only removal)"
```

---

### Task 5: Frontend — Quick merge

**Files:** Modify `static/index.html` (`buildCorrBody`, wire in `renderCorrList`, `openQuickMergeModal`).

- [ ] **Step 1: Helper to test chainability + the modal**:
```javascript
function corridorChainable(corr) {
  if (corr.isStandalone) return false;
  const segs = segmentsOfCorridor(corr.id);
  if (segs.length < 2) return false;
  return validateChain(segs.map(s => s.id)).ok;
}

function openQuickMergeModal(corr) {
  const segs = segmentsOfCorridor(corr.id);
  const prefill = corr.name || corrSuggestion(corr) || '';
  showModal({
    title: 'Quick-merge corridor',
    message: `Merge all ${segs.length} segments of this corridor into one standalone segment?`,
    withInput: true, inputValue: prefill, confirmText: 'Merge',
    onConfirm: (name) => quickMerge(corr, name)
  });
}

async function quickMerge(corr, name) {
  const ids = segmentsOfCorridor(corr.id).map(s => s.id);
  try {
    const res = await apiFetch(`/api/projects/${currentProjectId}/merge`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segment_ids: ids, name: name || '' })
    });
    projectData = await apiFetch(`/api/projects/${currentProjectId}`);
    updateProgressUI(); renderCorrList(); renderMap();
    if (res.merged_segment_id) selectCorridor('sa_' + res.merged_segment_id, 'merge');
    showSaveIndicator();
  } catch (err) { alert('Quick merge failed: ' + err.message); }
}
```

- [ ] **Step 2: Render the button** — in `buildCorrBody` (non-standalone branch), add a Quick-merge button into the `.corr-name-editor` row, shown only when chainable:
```javascript
  const sug = corrSuggestion(corr);
  const corrNameVal = corr.name || '';
  const quick = corridorChainable(corr)
    ? `<button class="use-sug quick-merge-btn" title="Merge whole corridor into one segment">⤳ Quick-merge</button>` : '';
  return `
    <div class="corr-name-editor">
      <div class="input-wrap" style="flex:1">
        <input class="corr-name-input" type="text" value="${esc(corrNameVal)}" placeholder="Corridor name…"/>
        <button class="clear-input" title="Clear" tabindex="-1">×</button>
      </div>
      ${sug ? `<button class="use-sug" title="${esc(sug)}">Use: ${esc(sug.length > 20 ? sug.slice(0,20) + '…' : sug)}</button>` : ''}
      ${quick}
    </div>
    <div class="seg-cards">
      ${segs.map((seg, idx) => buildSegCard(seg, idx)).join('')}
    </div>
  `;
```

- [ ] **Step 3: Wire the button** — in `renderCorrList`, inside the `if (!isStandalone)` block, after the `useSugBtn` wiring:
```javascript
      const quickBtn = row.querySelector('.quick-merge-btn');
      if (quickBtn) quickBtn.addEventListener('click', (e) => { e.stopPropagation(); openQuickMergeModal(corr); });
```

- [ ] **Step 4: Verify (manual)** — expand a corridor whose segments connect end-to-end → a "⤳ Quick-merge" button shows; click → modal with pre-filled name → Merge → corridor collapses into one standalone segment (selected). A corridor whose segments don't chain shows no button.

- [ ] **Step 5: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): per-corridor Quick-merge (chainable only) with name modal"
```

---

### Task 6: Frontend — Unmerge badge + button

**Files:** Modify `static/index.html` (`buildSegCard`, `wireSegCard`, `unmergeSegment`, CSS).

- [ ] **Step 1: Badge + button in the card** — in `buildSegCard`, add to the `.seg-ref` line and after the chips:
```javascript
function buildSegCard(seg, idx) {
  const named = seg.named;
  const chips = buildChips(seg);
  const badgeNum = (typeof idx === 'number') ? idx + 1 : '';
  const badgeHtml = badgeNum ? `<span class="seg-badge">${badgeNum}</span>` : '';
  const mergedTag = seg.is_merged ? `<span class="merged-tag">merged (${seg.merged_from.length})</span>` : '';
  const unmergeBtn = seg.is_merged ? `<button class="unmerge-btn" data-seg-id="${seg.id}">Unmerge</button>` : '';
  return `
    <div class="seg-card${named ? ' named' : ''}" id="seg-card-${seg.id}">
      <div class="seg-ref">${badgeHtml}${esc(seg.route_name_imported || '')}${mergedTag}<span class="seg-len">${fmtLen(segLengthM(seg.coords))}</span></div>
      <div class="input-wrap">
        <input class="seg-name-input${named ? ' named' : ''}" id="seg-input-${seg.id}" type="text" value="${esc(seg.name || '')}" placeholder="Enter road name…" data-seg-id="${seg.id}"/>
        <button class="clear-input" title="Clear" tabindex="-1">×</button>
      </div>
      <div class="seg-chips">${chips}</div>
      ${unmergeBtn}
    </div>
  `;
}
```
CSS:
```css
.merged-tag { margin-left:6px; font-size:10px; font-weight:700; color:#6d3fbf; background:#f5f3ff; border:1px solid #d4c5f9; border-radius:10px; padding:1px 7px; }
.unmerge-btn { margin-top:7px; padding:4px 10px; font-size:11px; border-radius:6px; border:1px solid #d4c5f9; background:#f5f3ff; color:#6d3fbf; cursor:pointer; }
.unmerge-btn:hover { background:#ede9fe; }
```

- [ ] **Step 2: Wire + endpoint call** — in `wireSegCard`, add:
```javascript
  const unmergeBtn = card.querySelector('.unmerge-btn');
  if (unmergeBtn) unmergeBtn.addEventListener('click', (e) => { e.stopPropagation(); unmergeSegment(seg); });
```
Add the function:
```javascript
async function unmergeSegment(seg) {
  const n = (seg.merged_from || []).length;
  showModal({
    title: 'Unmerge segment',
    message: `Unmerge into ${n} segment${n !== 1 ? 's' : ''}?`,
    confirmText: 'Unmerge',
    onConfirm: async () => {
      try {
        await apiFetch(`/api/segments/${seg.id}/unmerge`, { method: 'POST' });
        const keepCorr = seg.corridor_id;
        projectData = await apiFetch(`/api/projects/${currentProjectId}`);
        updateProgressUI(); renderCorrList(); renderMap();
        showSaveIndicator();
      } catch (err) { alert('Unmerge failed: ' + err.message); }
    }
  });
}
```

- [ ] **Step 3: Verify (manual)** — a merged segment shows a "merged (N)" tag and an Unmerge button; clicking → confirm → it splits back into its immediate parts (a nested merge peels one level; a collapsed corridor reappears). The badge also shows in the merge-mode list.

- [ ] **Step 4: Commit**
```bash
git add static/index.html
git commit -m "feat(ui): unmerge button + merged badge on segment cards"
```

---

## Self-Review

**Spec coverage:** guided selection (T4), quick merge (T5), unmerge (T1 backend + T6 UI),
search-in-merge fix + hide chips (T3), shared modal + hint toast (T2), is_merged/merged_from
(T1). ✓
**Placeholder scan:** none. ✓
**Type consistency:** `mergeCandidates`/`orderedSelectionSegs`/`toggleMergeSeg` names consistent
across T4; `showModal({...,onConfirm})` signature used by T5/T6; `seg.is_merged`/`seg.merged_from`
produced in T1 and consumed in T4/T6. ✓
