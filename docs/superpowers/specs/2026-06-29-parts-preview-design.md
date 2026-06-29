# Merged-Segment Parts Preview — Design Spec

**Date:** 2026-06-29
**Status:** Approved (brainstorm complete)
**Builds on:** the segment-merge feature + UX upgrades (merge, unmerge, guided selection)

## 1. Overview

Let the inspector **preview what a merged segment is made of** before deciding to
unmerge it. A "Preview parts" button on a merged segment shows its **immediate**
parts (one level down) both on the map and as an inline list in the right panel.
A part that is itself a merged segment can be **drilled into** (breadcrumb focus),
so the user can explore the merge tree level by level. Preview is **read-only**.

## 2. Goals / Non-goals

**Goals**
- Show a merged segment's immediate parts on the map (alternating two colours,
  numbered badges, direction arrows) and as an inline read-only list under its card.
- Drill into a part that is itself merged (one level at a time, breadcrumb to climb).
- Bidirectional hover sync between map parts and panel rows (matches existing hover).
- Dim the rest of the map while previewing so the parts stand out.

**Non-goals**
- Acting from inside the preview (unmerge stays its own button).
- Previewing non-merged segments.
- A flattened all-the-way-down view (we go one level per drill step).

## 3. Backend

New read-only endpoint:

```
GET /api/segments/{sid}/parts
```

- Load the segment by id (it may be **live or soft-deleted** — drill-down queries
  soft-deleted parts). 404 if it does not exist.
- Read `props.merged_from` (the ordered immediate-child uuids). If absent → 400
  ("Segment is not a merged segment.").
- Fetch the immediate children: `SELECT * FROM segments WHERE merged_into = <uuid>
  AND project_id = <pid>`.
- **Order** the children by the parent's `merged_from` order (uuid → index; any not
  found appended at the end).
- Each part returns:
  `{id, uuid, coords (parsed list), name, route_name_imported, is_merged (bool),
  merged_count (int)}` where `is_merged`/`merged_count` come from the part's own
  `props.merged_from`.
- Response: `{parent_id: <sid>, parts: [ ... ]}`.

The same endpoint serves drill-down: to preview a part's parts, call it with that
part's id.

## 4. Frontend

### 4.1 Entry point
- Every merged segment's panel card (normal UI) gets a **"Preview parts"** button
  next to **Unmerge**. Clicking toggles preview for that segment. Merge mode is
  unaffected (badge only there, as today).

### 4.2 State
- `previewRootId` — the segment id whose card hosts the preview block (null = off).
- `previewStack` — breadcrumb path `[{id, label}, …]`; the last entry is the level
  currently shown.
- `previewParts` — the current level's parts (from the endpoint).
- `previewPolylines` — `{partId: {core, casing, badge, arrow}}` for the map overlay.
- `previewHoverId` — currently hovered part id.

### 4.3 Map overlay (`renderPreviewMap`)
- Dim every existing polyline (corridor overviews + any child polylines) to low
  opacity so only the preview stands out.
- Draw each current-level part with an **alternating two-colour** scheme
  (`#7c3aed` / `#f59e0b`), a **numbered badge** at its midpoint, and a **direction
  arrow** at its end. Store handles in `previewPolylines`.
- Wire each part polyline `mouseover/mouseout` → `setPreviewHover(partId, on)`.

### 4.4 Inline panel block (rendered via `buildSegCard`)
- When `seg.id === previewRootId`, `buildSegCard` appends a preview block inside the
  card containing:
  - a **breadcrumb** row (e.g. `Parts: M › M1`), each crumb clickable to climb back;
  - the **parts list** for the current level — one row per part: number, name (or
    imported ref), length, and a `merged (N)` tag if the part is itself merged;
  - a **Close** button.
- Rendering from state means a normal `renderCorrList()` re-render keeps the block.
- Each part row: `mouseenter/leave` → `setPreviewHover`; a row whose part `is_merged`
  is clickable to **drill** into it.

### 4.5 Interactions
- `enterPreview(seg)` → `previewStack=[{id:seg.id,label}]`, fetch parts, render map +
  panel.
- `drillPreview(part)` → push `{id:part.id,label}`, fetch its parts, re-render.
- `gotoCrumb(i)` → truncate stack to `i+1`, fetch that level's parts, re-render.
- `setPreviewHover(id, on)` → emphasise that part's polyline (white casing/halo +
  bring to front) and toggle a highlight class on its panel row — both directions.
- `closePreview()` → clear preview state + overlay, then `renderMap()` +
  `renderCorrList()` to restore the normal view.
- **Auto-close** (clear preview state) when the user selects/deselects a corridor,
  enters Merge mode, edits the search, or leaves the project — so no stale overlay.

### 4.6 Colours
- Parts alternate `#7c3aed` (violet) and `#f59e0b` (amber) — distinct from
  unnamed-blue, named-green, candidate-red, and selection-blue used elsewhere.

## 5. Testing

Backend (pytest):
- `/parts` of a whole-corridor merge returns the 3 originals in chain order, each
  `is_merged=false`.
- Nested: merge M1 then M1+SC→M2; `/parts` of M2 returns `[M1, SC]` with
  `M1.is_merged=true, merged_count=3`; `/parts` of M1's id (soft-deleted) returns the
  3 originals (drill-down).
- 400 on a non-merged segment; 404 on a missing id.

Frontend: a headless-Chrome repro (open preview → parts drawn + listed → drill into a
merged part → breadcrumb back → close), plus the user's manual check. JS syntax via
`node --check`.

## 6. Files touched

- `app.py` — `GET /api/segments/{sid}/parts`.
- `static/index.html` — preview button, state, map overlay, inline block, drill,
  hover sync, auto-close hooks, CSS.
- `tests/test_api.py` — `/parts` tests.

## 7. Out of scope

Acting from preview, previewing non-merged segments, full recursive flatten in one view.
