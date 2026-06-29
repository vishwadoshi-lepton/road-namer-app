# Merge Segments — Design Spec

**Date:** 2026-06-29
**Status:** Approved (brainstorm complete)
**Related:** `docs/PHASE_2.md` §1 (corridor editing — this implements the *segment* merge slice)

## 1. Overview

Let the inspector **merge two or more route segments into one new segment**. The
merged segment gets a **new UUID** and replaces its constituents everywhere — in
the panel, on the map, and in the export. The original segments are kept in the
database but **soft-deleted** (marked as merged) so the operation is reversible
later and full provenance is preserved.

Example: merge `r1, r2, r3` (uuids `u1, u2, u3`) → new `r4` (uuid `u4`). After the
merge, export contains only `r4`, not `r1/r2/r3`. `r4.properties.merged_from =
[u1, u2, u3]` records where it came from.

This is **segment merge**, not corridor merge. We are combining route geometries
into a single LineString, not regrouping corridor membership lists.

## 2. Goals / Non-goals

**Goals**
- Merge ≥2 connected segments into one new segment with a fresh UUID.
- Respect each segment's stored **direction** — never reverse geometry.
- Persist the merge in the DB; export and all views reflect it automatically.
- Handle corridor membership correctly (whole corridor, partial, cross-corridor,
  standalone).
- Bundle two small panel improvements requested alongside:
  1. Corridor header shows the **saved name** (not always `cor_code`).
  2. A **search box** in the right panel filters the list by saved names.

**Non-goals (v1)**
- Unmerge UI (the data model supports it; build later).
- Segment splitting, vertex editing, manual direction reversal.
- Corridor merge/move/reorder (separate Phase 2 items).

## 3. Data model change

Add one nullable column to `segments` (in `db.py`):

```sql
merged_into TEXT DEFAULT NULL
```

- `merged_into IS NULL` → the segment is **live** (active).
- `merged_into = '<uuid>'` → this segment was absorbed into the merged segment
  with that UUID; it is hidden from all live views/exports but never physically
  deleted.

Migration is **idempotent** and lives in `db.init_db`: after `executescript`,
run a guarded `ALTER TABLE segments ADD COLUMN merged_into TEXT` (catch the
"duplicate column" error) so the existing `roadnamer.db` is upgraded in place.
`CREATE TABLE IF NOT EXISTS` alone will not add the column to an existing DB.

The merged segment row carries provenance in its `props` JSON:

```json
{ "merged_from": ["u1", "u2", "u3"], ... }
```

**Definition used everywhere:**
- A segment is **live** iff `merged_into IS NULL`.
- A corridor is **shown/exported** iff it has ≥1 live segment.
- Nothing is physically deleted by a merge (originals soft-deleted; corridors with
  no live segments are kept in DB but excluded from API/export output).

## 4. Merge eligibility & validation

A merge requires **≥2 live segments that form one continuous directed chain in
their stored directions** (no flipping).

- **Ordering** is computed by connectivity: there must be an ordering
  `s₁ … sₙ` such that `end(sᵢ) ≈ start(sᵢ₊₁)` within tolerance, for all `i`.
- **Tolerance:** reuse the existing nearness check `importer._near(a, b, tol=1e-4)`
  (degrees). The merge module imports/uses the same `tol`.
- The chain is found by: locate the unique start segment (its start point matches
  no other segment's end), then walk end→start. A valid result must consume **all**
  selected segments exactly once.

**Reject with a clear, human-readable reason (HTTP 400) when:**
- Fewer than 2 live segments are selected.
- Any selected id is not in the project, or is already merged (not live).
- The segments do not connect (gap > tolerance) → *"Segments don't connect end to
  end."*
- The chain branches — a point is shared by more than two segment endpoints in a
  way that prevents a single path → *"Selected segments branch; pick a single
  path."*
- The selection is **anti-parallel** — e.g. a segment and its reverse twin, which
  cannot be chained head-to-tail in their stored directions → *"Two selected
  segments run in opposite directions; pick one direction."*
- The chain forms a closed loop → *"Selected segments form a loop."*

The client performs the same validation live (to enable/disable the Merge button
and show chain order), but the **server is authoritative** and re-validates.

## 5. Geometry construction

Concatenate the ordered segments' coordinates into one GeoJSON `LineString`. At
each junction, drop the **first** coordinate of each subsequent segment (it equals
the previous segment's last point within tolerance) so there is no duplicate
vertex. No length is stored — the UI computes length from coords as it does today.

```
merged_coords = s1.coords + s2.coords[1:] + s3.coords[1:] + …
```

## 6. Corridor placement (result)

Compute the set of distinct **live corridors** among the selected segments:

| Selection | Merged segment lands as | Donor corridor(s) |
|---|---|---|
| All standalone | **Standalone** (`corridor_id = NULL`) | — |
| All in **one** corridor that **keeps** other live segments | **In that corridor**, `seq = min(seq of merged)` | shrinks; remaining keep their `seq` (gaps are harmless — ordering is by `seq`) |
| All in **one** corridor = **the whole** corridor (no live segs left) | **Standalone** (collapse) | corridor now has 0 live segments → excluded from UI/export (row kept) |
| Spanning **multiple** corridors (± standalone) | **Standalone** | each donor: excluded if emptied, else shrinks |

**Net rule:** the merged segment is **standalone**, *except* when every selected
segment belongs to a single corridor that still has other live segments left —
then it stays in that corridor at `seq = min(seq of merged)`.

## 7. Backend API

New endpoint:

```
POST /api/projects/{pid}/merge
body: { "segment_ids": [int, …], "name": "<optional string>" }
```

Steps:
1. Load the selected rows for `pid`; reject if any are missing or not live (400).
2. Validate + order the chain (§4); on failure return 400 with the reason.
3. Build merged geometry (§5); decide corridor placement (§6).
4. Generate a new UUID via Python `uuid.uuid4()` (string).
5. Insert the merged segment:
   - `uuid` = new uuid
   - `geom` = merged coords (JSON)
   - `name` = trimmed `body.name` (may be empty)
   - `corridor_id` / `seq` per §6
   - `route_name_imported` = distinct non-empty source `route_name_imported`
     joined with `" + "`, fallback `"Merged segment"`
   - `props` = a shallow copy of the **first** ordered segment's props, minus
     `parent_route_id`, `segment_order`, `uuid`, plus `merged_from = [uuids in
     chain order]`
   - `twin_uuid` = NULL, `sug_geocode`/`sug_roads` = "" (suggestions don't carry
     over)
6. `UPDATE segments SET merged_into = '<new uuid>' WHERE id IN (selected)`.
7. Commit. Return `{ "ok": true, "merged_segment_id": <int>, "merged_uuid":
   "<uuid>", "corridor_id": <int|null> }` so the frontend can refetch and focus
   the new segment.

**Pure logic lives in a new `merge.py`** (so it is unit-testable without a DB):
- `order_chain(segments, tol=1e-4)` → ordered list, or raises `MergeError(reason)`.
  `segments` are dicts with at least `coords` and `id`.
- `merge_coords(ordered)` → concatenated coords with junction de-dup.
- `MergeError(Exception)` carries a human-readable message.

`app.py` handles DB I/O, corridor placement, UUID generation, and maps
`MergeError` → `HTTPException(400, reason)`.

## 8. Active-segment filtering (the sweep)

Add `merged_into IS NULL` (or equivalent filtering) to every **live** read so
merged-away rows disappear and emptied corridors drop out:

- `app.get_project` — the segment loop **and** the `name_by_uuid` map; also exclude
  corridors that have no live segments from the returned `corridors`.
- `app.export_project` query — only live segments.
- `app.list_projects` counts (`seg_count`, `named_count`).
- `export.build_export` — only place live corridors in `corridors_out`; skip any
  corridor with an empty `segment_uuids`. (Belt-and-suspenders: it only receives
  live segments anyway.)
- `app.patch_segment` — naming a merged-away segment is a no-op concern; low risk,
  optionally guard with `AND merged_into IS NULL`.

## 9. Export impact

**No format change.** Because the merge is persisted and reads filter to live
segments:
- Export emits only `r4` (not `r1/r2/r3`).
- `corridors` lists only live membership; emptied corridors drop out.
- `merged_from` rides along inside the feature's `properties` (export already
  spreads `props`), giving downstream consumers (e.g. Trafficure) the provenance
  needed to reconcile the new UUID against the old ones — answering the open sync
  question in `docs/PHASE_2.md` §1.

## 10. Frontend

### 10.1 Dedicated Merge mode

A **Merge** toggle button in the header (next to Export; enabled only when a
project is open). Entering merge mode:

- **Map:** render **all live segments individually** and clickable (not the
  corridor-overview lines). Suspend normal corridor hover/select and segment
  naming. Selected segments are highlighted (distinct color/casing) and labeled
  with their chain-order number (①②③) and a direction arrow.
- **Panel:** keep the segment **list** (it is the selector) but render it
  **flattened / auto-expanded** — corridors become non-collapsible section headers,
  plus a "Standalone" group — so every live segment is visible and clickable
  without hunting. Each row is a click-to-toggle. A **sticky header** above the
  list holds the merge controls:
  - selected count, validation banner (`✓ Connected chain of N` / `✗ <reason>`),
  - a **Name** input pre-filled only if all selected segments share the same
    non-empty name (else blank),
  - **Merge** (disabled until ≥2 selected and valid) and **Cancel** buttons.
- **Selection set** is shared across map, panel rows, and the overlap chooser. A
  plain click toggles a segment in/out — no modifier key.
- **Overlap chooser:** clicking a map spot that covers >1 segment opens a small
  popup at the cursor listing the candidates (each with a direction arrow + name /
  endpoints). **Hovering a popup row highlights that segment on the map with its
  direction arrows.** Clicking a row toggles its selection. (Candidates are found
  by hit-testing all rendered live segment polylines against the click point within
  a small pixel threshold.)
- **Exit:** Merge success or Cancel/Esc leaves merge mode, clears the selection,
  refetches the project, and (on success) focuses the new merged segment.

Implementation notes:
- New state: `mergeMode` (bool), `mergeSelection` (array/Set of segment ids),
  `mergeOrder` (computed ordered ids for badge numbering).
- Reuse `segLengthM`, `headingDeg`, arrow `Symbol` rendering, and casing patterns
  from the existing child-segment drawing.
- Client-side chain validation mirrors `merge.order_chain` (same `1e-4` tolerance);
  the server re-validates on submit.

### 10.2 Corridor header shows saved name (bug fix)

In `renderCorrList`, the non-standalone branch currently renders
`corr.cor_code || label`, so it always shows `cor_001` even when named
(`static/index.html` ~line 1041). Change to show **`corr.name || corr.cor_code`**
(saved name first, fall back to the code), mirroring standalone. When a name is
present, show the `cor_code` as a small muted secondary label so the code is still
discoverable.

### 10.3 Panel search box

Add a search input at the top of the project panel (in/under the filter bar).
Behavior:
- Case-insensitive **substring** match against saved names: corridor `name` and
  segment `name`.
- An item is shown if its corridor name matches **or** any of its segments' names
  match; matching segments cause their corridor to render expanded.
- Search **combines (AND)** with the active All/Unnamed/Named chip.
- Empty search → current behavior unchanged.
- The same filter also applies to the merge-mode list.
- New state: `searchQuery` (string); `renderCorrList` (and the merge list) honor it
  via a helper like `itemMatchesSearch(item)`.

## 11. Edge cases

- **< 2 selected** → Merge disabled (client) / 400 (server).
- **Anti-parallel twin pair selected** → not chainable → rejected; the overlap
  chooser is how the user avoids picking both.
- **Branch / Y-junction** → rejected.
- **Gap > tolerance** → rejected.
- **Already-merged segment** somehow referenced → server rejects (not live).
- **Whole corridor consumed** → merged becomes standalone; corridor excluded.
- **Name pre-fill** only when unanimous; user may always edit; empty name allowed
  (merged segment is then unnamed and appears in the Unnamed queue).
- **Progress counts** update after merge (fewer total segments).

## 12. Testing (TDD, pytest)

Pure-logic unit tests (`merge.py`):
- Valid 2- and 3-segment chains order correctly (including when ids are passed
  out of order).
- `merge_coords` de-dups shared junction vertices and preserves direction.
- Reject: gap, anti-parallel pair, branch, loop, < 2.

API/integration tests (`app`, `export`):
- Merge a whole corridor → merged segment is standalone; corridor excluded from
  `get_project` and export; old uuids absent; new uuid present with `merged_from`.
- Merge a subset of a ≥3-segment corridor → merged stays in corridor at min seq;
  remaining live segment present; counts correct.
- Merge across corridors / standalone → result standalone; emptied donors excluded.
- `list_projects` counts ignore merged-away segments.
- Export contains only live segments and `merged_from` in properties; 400 on
  invalid selections with a reason.

A new fixture `tests/fixtures/merge_sample.geojson` provides a ≥3-segment connected
corridor plus connected standalone segments and an anti-parallel twin to exercise
all rules.

Frontend is verified manually by the user (vanilla JS single file); the dev server
is smoke-tested to confirm the new endpoint and pages load.

## 13. Files touched

- `db.py` — `merged_into` column + idempotent migration.
- `merge.py` — **new** pure chain-ordering / geometry module.
- `app.py` — `POST /api/projects/{pid}/merge`; active-segment filters in
  `get_project`, `export_project`, `list_projects`.
- `export.py` — exclude empty corridors / live-only.
- `static/index.html` — Merge mode UI, corridor-name display fix, panel search.
- `tests/` — `test_merge.py`, additions to `test_api.py`/`test_export.py`, new
  fixture.
