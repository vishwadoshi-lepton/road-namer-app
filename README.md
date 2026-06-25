# Road Corridor Namer

A small full-stack tool for turning raw road **segments** (GeoJSON LineStrings) into named
**corridors**. Google Maps provides a *first-layer* name suggestion that a human accepts or
overrides; corridors can be re-shaped (add/remove/split/merge segments); everything persists in
a local SQLite DB and is resumable. All Google responses are cached so the API is never called
twice for the same point.

This document is the engineering handoff. For the quick "how do I use it" version, see
[Using the app](#using-the-app).

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | **FastAPI** (Python 3.9+) | single file `app.py` |
| DB | **SQLite** | file `roadnamer.db`, created on first run; path override via `ROADNAMER_DB` env |
| Server | **uvicorn** | `python app.py` or `uvicorn app:app` |
| Frontend | **Vanilla JS + Leaflet** | single file `static/index.html`, no build step |
| External | Google **Geocoding / Roads / Places** APIs | optional; only used by enrichment |

No build tooling, no node_modules, no framework on the frontend — intentionally kept boring and
hackable.

## Project layout

```
road-namer-app/
├── app.py              # FastAPI backend: routes, SQLite schema, geometry, enrichment, gcache
├── static/
│   └── index.html      # whole frontend (map, name review, corridor editor) in one file
├── requirements.txt
├── start.command       # macOS double-click launcher (pip install + run + open browser)
├── README.md
└── roadnamer.db        # created at runtime (gitignore this)
```

## Run (dev)

```bash
cd road-namer-app
python -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt
python app.py                                          # -> http://localhost:8000
# or: uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` (not the `index.html` file directly — it must be served so the
relative `/api/*` calls resolve).

---

## Data model (SQLite)

```
projects(id, name, created_at, snap_tol, max_turn, enriched)
corridors(id, project_id→projects, order_index, name, suggested)
segments(id, project_id→projects, seg_index, geom, props,
         corridor_id→corridors, seq, reversed,
         road, suggested, name, divided)
gcache(k PRIMARY KEY, v)        -- Google response cache, key = "gc:lat,lng" | "nb:lat,lng" | "rd:lat,lng" | "pd:<placeId>"
```

- `geom` is a JSON array of `[lng,lat]` pairs (GeoJSON order). `props` is the original feature's
  properties JSON.
- A segment belongs to exactly one corridor (`corridor_id`) at position `seq`. `reversed` records
  the orientation chosen when chaining (travel direction within the corridor).
- Naming fields are split deliberately:
  - `road` — the underlying road name (used to label/group corridors).
  - `suggested` — Google's first-layer suggestion (segment = `POI to POI`).
  - `name` — the human's final value (starts equal to `suggested`, edited freely).
  - Corridor `suggested` = `Road: firstPOI to lastPOI`; `name` = final.
- `ON DELETE CASCADE` cleans corridors+segments when a project is deleted (`PRAGMA foreign_keys=ON`).

## Core algorithms (in `app.py`)

- **Corridor chaining** — `build_corridors(segs, tol, maxturn)`: greedy straightest-path walk.
  Connect segments whose endpoints are within `tol` metres; at a junction follow the smallest
  bearing change; break the corridor when the only continuation turns more than `maxturn`.
  Sorted longest-first. Endpoints are matched with a haversine distance; bearings via great-circle.
- **Enrichment** — background thread (`_enrich`), progress in the in-memory `PROGRESS` dict, polled
  by the frontend. Two passes:
  1. per segment: POI at each end (`poi_at`) → segment `suggested = "A to B"`; road name via
     `route_at` (geocode) or `_road_via_roads` (snap + place details).
  2. per corridor: modal road of its segments + POI(first node)→POI(last node) → corridor `suggested`.
- **Junction extraction** — `_scan_junction` regex-matches POI names/addresses for
  `Circle | Cross Road | Char Rasta | Chowk | Flyover | Bridge | Junction | Darwaja | …`; falls back
  to nearest transit stop, then nearest prominent POI. `normalise()` strips house-number prefixes
  ("13-55, 132 Feet Ring Rd" → "132 Feet Ring Road").

## The Google cache (important)

`class Cache` wraps the `gcache` table. **Every** Google call goes through it:
- key hit → return stored value, **no network call**.
- key miss + online → call Google, store, return.
- key miss + **offline=True** → return empty, never call.

Implications:
- Re-running enrichment, editing, or testing costs nothing for points already seen.
- The frontend's **offline (cache only)** checkbox sets `offline=True` for a zero-cost pass.
- Cache is keyed by coordinate rounded to 5 dp (~1 m) and by placeId, so adjacent segments sharing
  an endpoint resolve to one call.

## HTTP API

| Method | Path | Body / notes |
|---|---|---|
| POST | `/api/projects` | multipart `file` (GeoJSON). Returns `{project_id,...}` |
| GET | `/api/projects` | list with seg/corridor counts |
| GET | `/api/projects/{id}` | full project: project, corridors, segments (with coords+mid), progress |
| DELETE | `/api/projects/{id}` | |
| POST | `/api/projects/{id}/enrich` | `{key, mode:"geocode"|"roads", offline:bool}` → starts background job |
| GET | `/api/projects/{id}/enrich/status` | `{running,phase,done,total}` |
| PATCH | `/api/segments/{id}` | `{name?, divided?, accept_suggestion?}` |
| PATCH | `/api/corridors/{id}` | `{name?, accept_suggestion?}` |
| POST | `/api/segments/{id}/move` | `{corridor_id: int|"new"}` |
| POST | `/api/corridors/{id}/split` | `{after_seq}` → new corridor with seq>after |
| POST | `/api/corridors/{id}/merge` | `{target_id}` → appends target onto this, deletes target |
| POST | `/api/corridors/{id}/reorder` | `{order:[seg_id,...]}` |
| GET | `/api/projects/{id}/export` | downloads enriched GeoJSON |

Exported feature properties: `name` (segment POI→POI), `road_name`, `corridor_name`,
`corridor_id`, `seq_in_corridor`, `reversed_for_walk`, `divided` (+ original props passed through).

## Using the app

1. **Import GeoJSON** (FeatureCollection of LineStrings; MultiLineString auto-split).
2. **✨ Auto-name** with a Google key — `geocode` (cheap) or `roads` (accurate snapping). Tick
   **offline** to use only cached results (no spend). Fills suggestions you accept/override.
3. **Names tab** — focused map shows only the current corridor; rename segment & corridor (💡 to
   accept Google's suggestion), set divided/undivided. Autosaves.
4. **Corridors tab** — active corridor is bold, others faint. Click a faint segment to add it, click
   a member to pop it out; drag between lists; ✂ split / merge ↓. Fixes over/under-long corridors.
5. **Export** the named GeoJSON.

## Deployment notes

- Stateless app + a SQLite file. For a shared server: `uvicorn app:app --host 0.0.0.0 --port 8000`
  behind nginx/Caddy. Put `roadnamer.db` on a **persistent volume** (don't lose it on redeploy).
- Single-process is fine for SQLite. If you scale to multiple workers, switch to Postgres
  (the SQL is plain; the `gcache` table and queries port directly) and add a row lock around
  corridor edits.
- Google key: currently passed from the browser per enrichment call and used server-side. For a
  shared deployment, move the key to a server env var and drop the client field.

## Known limitations / suggested next steps for eng

- **No auth / multi-user** — last-write-wins; add accounts + per-edit attribution if a team uses it.
- **Enrichment progress is in-memory** (`PROGRESS` dict) — lost on restart; move to a DB column or
  a task queue (RQ/Celery) for robustness and to survive redeploys mid-run.
- **`roads` mode** snaps one point per segment; sampling 2–3 points and taking the modal road would
  be more robust at curves. Roads API supports 100 points/request (batch).
- **No undo** for corridor edits — a simple `edits` audit table would enable undo + history.
- **Geocode mode** can mis-pick a cross-street at junctions; `roads` mode avoids it. Consider always
  using `roads` for the road name and `geocode` only as fallback.
- Cost guardrail: add a per-project API-call counter / budget cap surfaced in the UI.
- Validation: large files are parsed synchronously in the request; for very large uploads, stream/parse
  in a background job.
