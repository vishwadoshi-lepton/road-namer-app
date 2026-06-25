# Road Namer — Phase 2 backlog

Items intentionally **deferred** out of the Phase 1 redesign to keep the first
release simple for non-technical traffic-police users. Phase 1 ships read-only
corridors (derived from `parent_route_id`) and two precomputed name suggestions
(geocode + roads). The items below are the planned next layer.

---

## 1. Corridor editing — split / merge / move / reorder

**What:** Let the inspector reshape corridors instead of only consuming the ones
inferred from the source data:

- **Split** a corridor at a chosen segment (everything after becomes a new corridor).
- **Merge** one corridor's segments onto another, then drop the empty one.
- **Move** a single segment to a different corridor (or pop it out as standalone).
- **Reorder** segments within a corridor.

**Why deferred:** In Phase 1, corridors come directly and reliably from
`parent_route_id` + `segment_order` in the GeoJSON, so editing isn't needed for the
common case. Adding it now would reintroduce exactly the UI complexity
(merge/split controls, drag-and-drop) we set out to remove, plus a data-sync
question (below) that needs its own decision.

**Open design questions for Phase 2:**
- The `uuid` is the canonical join key back to Trafficure, and corridors map to
  parent routes. If the inspector regroups segments locally, **does the new
  grouping sync back to Trafficure, or is "corridor" purely a local naming
  convenience?** Decide before building.
- If it does sync back: how do we represent a corridor that no longer matches any
  single `parent_route_id`? (e.g. a local `corridor_override` table keyed by child
  uuids, leaving the original parent linkage intact.)
- Re-derive vs. preserve: re-importing the same file must not silently clobber
  manual regrouping.

**Reference:** the original `app.py` already implemented these as endpoints
(`/api/segments/{id}/move`, `/api/corridors/{id}/split`, `/api/corridors/{id}/merge`,
`/api/corridors/{id}/reorder`) — the SQL logic can be ported rather than rewritten.

---

## 2. Gemini as a 3rd name suggestion (segments + corridors)

**What:** Add a third precomputed suggestion alongside geocode and roads, generated
by the Gemini API, for **both** segments and corridors.

- **Segment:** feed Gemini the **facts already extracted by the Maps APIs** (the
  geocode road name + the roads road name for that leaf, plus its imported name) and
  ask it to pick/normalize the cleanest human-readable road name.
- **Corridor:** synthesize a corridor name from its child segment names + the modal
  road, e.g. resolve "G.S. Road / Jail Road / G.S. Road" into one sensible corridor
  label.

### Architecture rule (non-negotiable)

```
Maps APIs = FACTS      (geocode route + roads place name)
Gemini    = WORDSMITH  over those facts ONLY — normalize variants, pick best,
                        synthesize corridor names. NEVER invents.
Human     = AUTHORITY  (approve / override)
```

**Why this matters:** this is an authoritative dataset synced back to Trafficure by
uuid. An LLM has no ground-truth database of local (e.g. Shillong) roads — fed
coordinates or sparse data it will **hallucinate** plausible-but-fake names. So the
prompt must **constrain Gemini to only the road names provided in the input**, forbid
inventing any name not present, and return an explicit "unknown" when the Maps facts
are blank (those leaves fall to the inspector's local knowledge). Use **low
temperature** and **structured JSON output**.

### How it fits Phase 1's model
- Precompute in the same **admin batch** as geocode/roads (one-time per uploaded file;
  key lives at prep time, never shown to the inspector).
- **Batch per corridor**, not per coordinate: one call returns the corridor name + all
  its child suggestions together (cheaper, and gives Gemini context). Standalone leaves
  can be batched in groups.
- Surface as a third one-tap chip; **merge/dedupe** when it equals geocode/roads.
- Cache responses so re-runs cost nothing.

### Cost (validated 2026 pricing)
- Gemini 2.5 Flash: $0.30 / 1M input, $2.50 / 1M output. Flash-Lite (batch): $0.05 /
  $0.20. For the full 1,031-leaf Shillong file, batched per corridor (~700 calls):
  **< $1** (≈ $0.10 with Flash-Lite batch). Negligible next to the Maps APIs.

### Deliberately NOT included (decided in Phase 1)
- **No Nearby Search / POI layer** ($32/1k, most expensive SKU). POI-to-POI naming was
  dropped; only add landmark context later if road-name coverage proves too thin.

**Needs:** a Gemini API key (prep-time/admin) and a prompt template per target
(segment vs corridor) with the no-invention guardrail baked in.

---

_Phase 1 scope is defined in the design spec under `docs/superpowers/specs/`._
