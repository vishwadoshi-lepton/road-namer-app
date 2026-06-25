# Road Namer

A full-stack tool for naming road **leaves** (individual GeoJSON segments from
Trafficure) and grouping them into **corridors** derived from the source data's
`parent_route_id`. A human inspector names each leaf; two Google-precomputed
suggestions (geocode + roads) are offered as one-tap chips. Everything persists in a
local SQLite DB and is resumable. All Google responses are cached so the API is never
called twice for the same point.

This document is the engineering handoff. For the quick "how do I use it" version,
see [Workflow / Using the app](#workflow--using-the-app).

> **Phase 1 rebuild note:** the previous version used Leaflet, geometric corridor
> chaining (`build_corridors`), a `divided`/carriageway field, and live split/merge/move/reorder
> corridor editing — all removed. Corridors now come deterministically from
> `parent_route_id`; editing is deferred to Phase 2 (see [Phase 2](#phase-2)).

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | **FastAPI** (Python 3.9+) | split across focused modules (`app.py`, `db.py`, `importer.py`, `enrich.py`, `export.py`) |
| DB | **SQLite** | path via `ROADNAMER_DB` env, defaults to `roadnamer.db` in cwd |
| Server | **uvicorn** | `python app.py` or `uvicorn app:app` |
| Frontend | **Vanilla JS + Google Maps JS API** | single file `static/index.html`, no build step |
| External | Google **Geocoding + Roads APIs** | admin-only, server-side, precomputed; browser key for base map |

No build tooling, no node_modules, no framework on the frontend.

---

## Project layout

```
road-namer-app/
├── app.py              # FastAPI wiring, static mount, entrypoint
├── db.py               # schema + connection helper
├── importer.py         # import filtering, corridor derivation, twin detection
├── enrich.py           # Google geocode/roads helpers (cached) + admin batch CLI
├── export.py           # export builder
├── static/
│   └── index.html      # whole frontend (Google Maps base map + Street View, name review)
├── requirements.txt
├── .env.example        # copy to .env and fill in both keys
├── start.command       # macOS double-click launcher
├── tests/
└── README.md
```

---

## Data model (SQLite)

Four tables, created by `db.py` on first run:

```
projects(id, name, created_at)
corridors(id, project_id→projects, name, suggested, cor_key)
segments(id, project_id→projects, corridor_id→corridors, segment_order,
         uuid, props, geom, name, suggested_geocode, suggested_roads, twin_id)
gcache(k PRIMARY KEY, v)
```

Key points:
- `uuid` is preserved end-to-end as the canonical join key back to Trafficure.
- `geom` is stored as the original GeoJSON geometry JSON.
- `suggested_geocode` and `suggested_roads` are precomputed by the admin enrichment
  batch and never changed by the inspector.
- `name` is the inspector's final value (starts empty; blank = unnamed).
- `twin_id` links a leaf to its reversed-direction counterpart when detected.
- `gcache` stores Google API responses keyed by coordinate/placeId; hits are free.
- `ON DELETE CASCADE` cleans corridors + segments when a project is deleted.

---

## Two API keys

Both keys live in a root `.env` file (copy `.env.example`). Neither is ever entered
by the inspector — they are config-time admin settings.

| Variable | Used by | Purpose |
|---|---|---|
| `GOOGLE_ENRICH_KEY` | `enrich.py` (server, admin CLI only) | Geocoding API + Roads API to precompute name suggestions |
| `GOOGLE_MAPS_JS_KEY` | browser (served via `GET /api/config`) | Google Maps JS base map + Street View |

---

## Run (dev)

```bash
cd road-namer-app
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
cp .env.example .env   # then fill in both keys
python app.py          # → http://localhost:8000
# or: uvicorn app:app --port 8077   # if 8000 is taken
```

Open `http://localhost:8000` (serve via the app, not the HTML file directly — the
`/api/*` calls must resolve).

Run tests:

```bash
python -m pytest -v
```

---

## Import rules

Filtering is deterministic and geometry-free (in `importer.py`):

- A feature is kept as a **leaf** iff `has_children == 0` AND `sync_status == "synced"`.
- A **corridor** is formed from a `parent_route_id` iff ALL its children are synced;
  segments are ordered by `segment_order`.
- If any child of a parent is unsynced, that parent forms **no corridor** and its
  synced children become standalone leaves.
- **Twin detection:** leaves whose geometry is the same road traversed in opposite
  directions are linked via `twin_id`; the inspector can tap the twin's name as a chip.

---

## Workflow / Using the app

### 1. Admin: Import

Upload a Trafficure GeoJSON via the home screen (or `POST /api/projects`). Multi-project
is supported — each upload is independent.

### 2. Admin: Precompute suggestions (once per project)

```bash
python enrich.py <project_id>            # calls Geocoding + Roads APIs; costs money once
python enrich.py <project_id> --offline  # skip Google; leaves suggestions blank
```

Results are stored in `gcache`; re-runs cost nothing for already-cached points.
`GOOGLE_ENRICH_KEY` is read from `.env` automatically.

### 3. Inspector: Name leaves

Open the app, pick the project. For each leaf:
- The **name box starts empty**; the imported name is shown greyed as a reference.
- Tap a **suggestion chip** (geocode / roads suggestion; merged into one chip if
  identical) or type a custom name.
- If a reversed-direction **twin** exists, its current name is offered as an optional chip.
- **Corridors** are read-only groupings with an auto-suggested display name; corridor
  names can be overridden.
- **"Next unnamed →"** button (or Enter key) advances to the next unnamed leaf.
- Names **autosave** (debounced) — no explicit Save button needed.

### 4. Export

```
GET /api/projects/{id}/export
```

Downloads a single JSON with:
- `leaves` — FeatureCollection of all leaf features, each with its `uuid` + final `name`.
- `corridors` — mapping of `cor_001 → [segment_uuids, ...]`.

Standalone leaves (no parent corridor) appear in `leaves` but in no corridor mapping.

---

## HTTP API

| Method | Path | Body / Notes |
|---|---|---|
| POST | `/api/projects` | multipart `file` (GeoJSON) → `{project_id, name, leaves, corridors}` |
| GET | `/api/projects` | list with `seg_count`, `named_count` |
| GET | `/api/projects/{id}` | `{project, corridors, segments}` |
| DELETE | `/api/projects/{id}` | cascades to corridors + segments |
| PATCH | `/api/segments/{id}` | `{name}` → `{ok, named}` |
| PATCH | `/api/corridors/{id}` | `{name}` → `{ok}` |
| GET | `/api/projects/{id}/export` | JSON download (leaves FeatureCollection + corridors map) |
| GET | `/api/config` | `{maps_key}` — browser fetches this to init the map |

Admin CLI (not HTTP):

```bash
python enrich.py <project_id> [--offline]
```

---

## Deployment notes

- Stateless app + a SQLite file. For a shared server:
  `uvicorn app:app --host 0.0.0.0 --port 8000` behind nginx/Caddy.
- Put the DB on a **persistent volume** (don't lose it on redeploy).
- Single-process is fine for SQLite. Multi-worker would require Postgres + row locks
  around corridor edits.
- Do **not** expose `GOOGLE_ENRICH_KEY` to the browser; it never leaves the server.
  `GOOGLE_MAPS_JS_KEY` is intentionally served to the browser for the map — restrict
  it by HTTP referrer in the Google Cloud Console.

---

## Phase 2

Items deferred from Phase 1 (see `docs/PHASE_2.md` for full design notes):

1. **Corridor editing** — split / merge / move / reorder segments within corridors.
   Deferred because `parent_route_id` derivation covers the common case and the
   UI complexity wasn't needed for the initial release. An open design question
   (does manual regrouping sync back to Trafficure?) must be answered first.

2. **Gemini as a 3rd name suggestion** — a Gemini API call (admin batch, same pattern
   as geocode/roads) that normalizes the Maps-API facts into a clean road name and
   synthesizes corridor names. Constrained to never invent names not already present
   in the Maps API output.
