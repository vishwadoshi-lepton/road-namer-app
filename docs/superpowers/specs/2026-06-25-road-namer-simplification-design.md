# Road Namer — Phase 1 Simplification (Design Spec)

**Date:** 2026-06-25
**Status:** Approved-pending-review
**Goal:** Rebuild the Road Corridor Namer into a tool a **non-technical traffic-police
inspector** can use to do one job well — give roads their real names — by removing
everything that exists only to reconstruct structure the source data already provides.

---

## 1. Background & motivation

The current app ([app.py](../../../app.py)) treats input as unstructured GeoJSON
LineStrings and spends most of its complexity *reconstructing* corridors via geometry
(`build_corridors()` greedy bearing-chaining), plus a full corridor-editing UI
(split / merge / move / reorder) and per-segment carriageway metadata.

Analysis of a real export (`shillong_p74_all.geojson`, 3,255 features) shows that
**corridor structure is already explicit in the data**, so most of that machinery is
overkill. The redesign deletes the inference and the editing, and focuses the UI on
naming.

### Validated facts about the source data

The input is a Trafficure GeoJSON export. Every feature carries rich properties, the
relevant ones being: `uuid`, `route_name`, `parent_route_id`, `has_children`,
`segment_order`, `sync_status`.

The hierarchy is exactly two levels and fully explicit:

| Kind | Condition | Count (sample file) |
|---|---|---|
| Parent routes (containers) | `has_children=1` | 435 |
| Child segments | `route_type=segment`, `parent_route_id` set, `has_children=0`, `segment_order=1..N` | 2,588 |
| Standalone routes | `has_children=0`, no parent | 232 |

- Referential integrity is perfect: every child's `parent_route_id` resolves to a real
  parent flagged `has_children=1`. Zero orphans.
- `segment_order` is present on all children and contiguous `1..N` within a parent.
- **Corridors = `GROUP BY parent_route_id ORDER BY segment_order`.** No geometry needed.

---

## 2. Scope

### In scope (Phase 1)
- Import a Trafficure GeoJSON; filter to syncable leaves; derive read-only corridors.
- Precompute two name suggestions per leaf (Google geocode + roads), offline/admin batch.
- Map + panel UI for naming each leaf, with auto-suggested corridor names.
- Multi-project support (each imported file is a resumable project).
- Export: per-leaf `{uuid, name}` + a corridor→segment-uuids mapping.

### Out of scope → [Phase 2](../../PHASE_2.md)
- Corridor editing (split / merge / move / reorder).
- Gemini as a 3rd suggestion (segments + corridors).
- Direct API push back to Trafficure (Phase 1 exports a file instead).

### Explicitly removed
- `build_corridors()` geometric chaining and `reversed`/`seq` inference.
- Corridor-editing endpoints (`/move`, `/split`, `/merge`, `/reorder`).
- Carriageway / `divided` field (UI + data).

---

## 3. Data model & import rules

### Import filtering (deterministic, no geometry)

Applied when a GeoJSON is imported into a project:

1. **Keep only syncable leaves:** `has_children == 0` **AND** `sync_status == "synced"`.
   Everything else is dropped (parent container rows; any non-synced leaf).
2. **Corridor formation — all-or-nothing per parent:** a parent becomes a corridor
   **only if *all* of its original children are synced.** If even one child is not
   synced, the parent is "broken": it forms **no** corridor, and its surviving synced
   children become **standalone**.
3. **Standalone** = a synced leaf that is either genuinely parentless, or an orphan from
   a broken parent. Standalone items are corridor-less (not corridors-of-one).

Sample-file result: **1,031** nameable leaves → **154** intact corridors (568 segments)
+ **463** standalone (272 orphans + 191 genuine). Numbers reconcile exactly.

### Tables (SQLite; evolves the current schema)

```
projects(id, name, created_at, enriched)

corridors(id, project_id, cor_code,        -- cor_code: stable "cor_001" style id for export
          name, suggested,                 -- name = inspector's; suggested = auto from children
          order_index)

segments(id, project_id, uuid,             -- uuid = Trafficure join key, PRESERVED end-to-end
         corridor_id NULL,                 -- NULL => standalone
         seq,                              -- = source segment_order (display order only)
         geom, props,                      -- geom JSON [lng,lat]; props = original feature props
         route_name_imported,              -- pre-fills the name box
         name,                             -- inspector's final value
         sug_geocode, sug_roads,           -- precomputed suggestions
         twin_uuid NULL,                   -- detected reversed twin (for optional suggestion only)
         status)                           -- 'named' | 'unnamed' (derived from name)

gcache(k PRIMARY KEY, v)                   -- Google response cache (unchanged)
```

Notes:
- `divided` column removed. `reversed` column removed (not needed for naming).
- `uuid` is never edited; it rides import → DB → export untouched.
- `corridors.cor_code` is assigned at import (`cor_001`, `cor_002`, …) for a stable export key.

---

## 4. Auto-name precompute (admin / prep-time)

Suggestions are **never** generated during an inspector's session — no API-key field, no
loader. Instead an **admin batch** runs once per uploaded file:

- For each leaf, sample the midpoint and resolve **two** road-name suggestions:
  - `sug_geocode` — via Geocoding API `route` component.
  - `sug_roads` — via Roads API snap → Place Details name.
- Both go through the existing `gcache` cache (coordinate-keyed), so re-runs and shared
  endpoints cost nothing.
- The Google **enrichment** key (Places/Geocoding/Roads) lives only at prep time.

This is exposed as a per-project "prepare/enrich" step the admin runs — **a CLI/script
command by default** in Phase 1 (an admin-only UI button is optional), kept distinct from
the inspector flow. After it completes, the project opens with suggestions already attached.

> The **Google Maps JS / Street View** key (for the base map the inspector sees) is a
> separate browser key configured in app/server config — also not entered by the inspector.

### Cost & deliberate omissions (validated 2026 pricing)

- Per leaf: 1 Geocoding ($5/1k) + 1 Roads snap ($10/1k) + 1 Place Details ($17/1k).
  For the 1,031-leaf sample file: **≈ $24–34 one-time** at list price (~$24 with
  `placeId` dedup on Place Details). It is a **one-time** spend — `gcache` makes
  re-runs and the inspector's daily use cost **$0**.
- Monthly free tiers (10k Geocoding, ~5k Roads, ~5k Place Details) plausibly make a
  single city **$0 out of pocket**. Rule of thumb: ~$24–34 per 1,000 leaves.
- **Cost optimization:** prefer reverse-geocoding the roads-snapped point ($5/1k) over
  Place Details ($17/1k) when the resolved name matches — cuts the priciest line ~3×.
- **Deliberately skipped:** Nearby Search / POI-to-POI naming ($32/1k, the most
  expensive SKU) and Gemini synthesis — both deferred to [Phase 2](../../PHASE_2.md).

---

## 5. Naming model

- **Unit of naming = each synced leaf**, named individually.
- The name box is **pre-filled** with `route_name_imported` (e.g. `"Route 385 - Segment 2"`);
  the inspector edits or replaces it — never a blank box.
- **Suggestions** are one-tap chips: `sug_geocode` and `sug_roads`, **merged to one chip
  when identical**.
- **Reversed twins (decision: independent naming):** twins (`Route 273` /
  `Route 273 (Reversed)`) are named **separately** — the inspector keeps control. Detection
  (base-name match **and** geometry-confirmed start↔end reversal) is used **only** to offer
  the twin's chosen name as an *optional* extra suggestion chip ("↔ other direction: …").
  Suggest, never enforce. Lonely reversed routes (no forward twin) appear as normal items.
- **Carriageway:** removed entirely.
- **Corridor name:** auto-`suggested` from the modal/most-common of its children's names,
  editable, one-tap apply. **Display/navigation only — not synced** (corridors aren't
  Trafficure entities). Its `cor_code` + name + child uuids are exported as a mapping.

---

## 6. UX — layout, map, flow

### Layout
- **Left (~60–65%):** Google Maps base, POIs visible, Street View pegman.
- **Right (~35–40%):** naming panel — filter chips (All / Unnamed / Named) → corridor list.
- **Header:** live progress counter + bar, Export button.

### Map
- Shows **corridors + standalone only**; child segments are **not** rendered/clickable until
  their corridor is selected. Standalone uses the **same color** as corridors (only structural
  difference: no children).
- **Status coloring:** unnamed = blue, named = green, selected = highlight. The map is a live
  progress view.
- **Street View:** drag pegman onto a road → dismissible overlay, to confirm a real-world name.

### Interactions (map ↔ panel are two-way synced)
- **Hover** a corridor → highlight + tooltip (name · #segs · #unnamed); pointer cursor.
- **Click a corridor** (map row or panel) → **select**: corridor + its segments highlight on
  the map; panel scrolls to it and **expands** it. **Single-open accordion** — selecting a new
  corridor collapses the previous (fixes "corridors never close").
- **Click a segment** (only after its corridor is selected) → **focuses** that segment's card
  in the panel and nudges map zoom. **Selection/focus only — never changes membership** (fixes
  "clicking adds to current corridor").
- **Deselect:** click empty map, press `Esc`, or select another corridor.
- No split / merge / move controls exist (fixes "merge below is useless").

### Advance model (chosen: free browse + accelerator)
- Free browse + click anything, **plus** a persistent **"Next unnamed →"** button and the
  `Enter` key that jump to the next still-unnamed segment, auto-panning the map. Control for
  the curious, a one-key fast lane for grinding.

### Saving
- **Event-driven, not interval.** Tapping a chip / corridor-name apply → save immediately.
  Typing in a name box → save **debounced ~800ms** after typing stops, and on blur / advance.
  No 1–2s polling timer.

---

## 7. Export

Phase 1 produces a **single downloadable JSON file** (no direct Trafficure API call) with two
top-level keys:

1. **`leaves`** — the syncable payload, a GeoJSON FeatureCollection where each leaf feature
   carries its `uuid`, final `name`, original `props`, and geometry. (A flat `{uuid: name}`
   index may be included alongside for easy ingestion.)
2. **`corridors`** — the grouping mapping:
   ```json
   { "corridors": [
       { "id": "cor_001", "name": "G.S. Road",
         "segment_uuids": ["uuid1", "uuid2", "uuid3"] }
   ]}
   ```

Standalone leaves belong to no corridor — absent from `corridors`, present in `leaves`.

---

## 8. Architecture

Keep it boring, evolve rather than rewrite:

- **Backend:** FastAPI + SQLite, single `app.py`. Reuse persistence, `gcache`, enrichment
  helpers (`route_at`, `_road_via_roads`, `poi_at`). **Remove** `build_corridors` (replace
  with parent grouping at import), the corridor-editing endpoints, and the `divided` field.
- **Frontend:** single static page, vanilla JS. **Swap Leaflet → Google Maps JS API** (for
  POIs + Street View + the base map). Rebuild the panel around the read-only corridor list,
  single-open accordion, suggestion chips, and the "Next unnamed" accelerator.
- **Two keys, both config-time:** enrichment key (prep batch) and Maps JS key (browser base
  map). Neither is entered by the inspector.

**Alternative considered:** full rewrite / new framework. Rejected — the existing FastAPI +
SQLite + vanilla-JS shape is already minimal and proven; the work is *subtractive* plus a map
library swap, not a new foundation.

---

## 9. Success criteria

- An inspector opens a prepared project and names roads using only: read the map, pick a
  suggestion or type, advance. No API key, no loader, no corridor editing, no jargon.
- Corridors and segment order come entirely from the source data; re-import is deterministic.
- `uuid` integrity: every exported leaf name maps back to exactly one Trafficure route.
- Export yields both the per-uuid leaf names and the `cor_xxx → [uuids]` mapping.
- The three known annoyances (merge-below, non-closing corridors, mystery map-clicks) are gone.
