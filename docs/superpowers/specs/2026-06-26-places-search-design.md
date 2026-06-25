# Map Places Search (Design Spec)

**Date:** 2026-06-26
**Status:** Approved
**Goal:** Add a Google-Maps-style search box on the map so an inspector can jump to a
place (by name, with autocomplete) or to raw `lat, lng` coordinates, see a marker at
the result, and clear it to search again — a pure locator to help orient while naming.

---

## 1. Decisions (locked)

- **New Places API via browser REST** (`https://places.googleapis.com/v1`), exactly like
  the reference `MapSearchBar.tsx`. No backend, no `places` JS library.
- **Reuses `GOOGLE_MAPS_JS_KEY`** (already in the browser for the map) as `X-Goog-Api-Key`.
- **Placement:** overlay at the **top-center of the map**, clear of existing controls.
- **Marker:** a single, distinct **red pin** (no popup), visually separate from the
  blue/green route markers. Locator only — does not touch route selection or naming.
- **Vanilla JS**, added to `static/index.html` as one self-contained module.

## 2. Prerequisite (ops)

`GOOGLE_MAPS_JS_KEY` must have **"Places API (New)"** enabled in Google Cloud Console, and
its HTTP-referrer restriction must allow this app's origin (e.g. `localhost`). If Places
is not enabled, the API returns 403 — the app degrades gracefully (see §6), the map is
unaffected.

## 3. Architecture & data flow

Two REST calls, browser-side, with headers `X-Goog-Api-Key: <maps key>`,
`Content-Type: application/json`, and a minimal `X-Goog-FieldMask`:

1. **Autocomplete** — `POST /v1/places:autocomplete`
   - Body: `{ input, locationBias: { circle: { center: {latitude,longitude}, radius } }, sessionToken }`
     where `center` = current map center, `radius` ≈ 50000 (m).
   - FieldMask: `suggestions.placePrediction.placeId,suggestions.placePrediction.text,suggestions.placePrediction.structuredFormat`.
   - Returns `{ suggestions: [{ placePrediction: { placeId, text:{text}, structuredFormat:{mainText:{text}, secondaryText:{text}} } }] }`.
2. **Place location** — `GET /v1/places/{placeId}?sessionToken=…`
   - FieldMask header: `location` (only — we just need lat/lng).
   - Returns `{ location: { latitude, longitude } }`.

**Session token:** a UUID generated when a search session starts (first keystroke after
empty/after a prior selection), sent with every autocomplete call and the final details
call, then regenerated. Ties a session together for correct/cheaper billing.

```
input → debounce 300ms → looksLikeCoords(input)?
  ├─ yes → validateLatLng → (Enter) → showMarker(lat,lng) + pan/zoom
  └─ no  → length ≥ 3 → :autocomplete (bias=map center) → dropdown
                 └─ select/Enter → places/{id} (location) → showMarker + pan/zoom
```

## 4. UI

A `position:absolute` container centered at the top of the `#map` element (`z-index`
above the map tiles, below modals): a 🔍 icon, a text `input`
(`placeholder="Search place or lat, lng"`), and an **× clear button** shown only when the
field is non-empty (via the existing `input:not(:placeholder-shown) + .clear` CSS trick).

- **Autocomplete dropdown:** absolutely positioned under the input; each row shows bold
  `mainText` + grey `secondaryText`. Mouse click or keyboard selects.
- **Keyboard:** ↓/↑ move the highlighted row; **Enter** selects the highlighted row (or
  the first row, or — if the text is coordinates — drops the coordinate marker); **Esc**
  clears input + closes dropdown + removes marker.
- **Debounce** 300 ms; autocomplete fires only when not coordinates and `length ≥ 3`.
- **Coordinate mode:** when the input matches two numbers (comma/space separated),
  autocomplete is suppressed; invalid coordinates show a small inline red error and make
  no API call.
- Clicking outside closes the dropdown (the marker persists until explicitly cleared).

### Coordinate parsing (`parseCoords`)
Split the trimmed input on `[,\s]+`. Valid iff exactly two finite numbers with
`|lat| ≤ 90` and `|lng| ≤ 180` → `{lat, lng}`; otherwise an error string
(`"Enter both lat and lng"`, `"Latitude must be -90..90"`, etc.). `looksLikeCoords`
is the lighter check (exactly two parseable numbers) used to choose mode.

## 5. Marker behavior

One module-level `searchMarker` (a `google.maps.Marker` with the default **red** icon,
high `zIndex`). `showSearchMarker(lat, lng)`: remove any existing marker, create a new one,
`map.panTo` it, and `map.setZoom(16)` if the current zoom < 14. `clearSearch()`: empty the
input, close the dropdown, `searchMarker.setMap(null)`, reset state + session token. The
marker is independent of corridor selection/child segments and never affects naming.

## 6. Error handling & degradation

- **Places disabled / 403 / network error on autocomplete:** log once, close the dropdown,
  do not block the map. If the *first* autocomplete in the session fails with an auth/permission
  error, show a single unobtrusive note in the dropdown area ("Search unavailable").
- **Place details fails:** show "Couldn't load that place", leave the input as-is.
- **No suggestions:** show a "No matches" row.
- **Invalid coordinates:** inline red message; no network call.

## 7. Scope guards (YAGNI)

No place photos/ratings/details panel, no search history, no reverse-geocode of the pin,
no "name the nearest route", no multi-marker. Just search → marker → clear.

## 8. File & structure

All in `static/index.html`:
- Markup: the search-bar container + dropdown inside the map column.
- CSS: bar, input, clear ×, dropdown rows, error text, red-pin marker is the Google default.
- JS module (grouped, commented): `parseCoords` / `looksLikeCoords`, `fetchAutocomplete`,
  `fetchPlaceLocation`, `renderDropdown`, `showSearchMarker`, `clearSearch`, plus the
  debounce + keyboard handlers and a `PLACES_BASE`/session-token helper. Initialized from
  the existing map-init path once `googleMap` and the maps key exist.

## 9. Testing

Frontend feature → **manual verification in a real browser** (controller-driven):
place search → dropdown appears → select → red pin + map pans; coordinate search
(`25.57, 91.88`) → pin; **×**/Esc clears input and removes the marker; invalid coords
(`25.57`) → inline error, no marker; ↓/↑/Enter keyboard nav. The pure `parseCoords`
function is small and deterministic — verified with a handful of inline cases during
implementation. No Python/back-end tests change.

## 10. Success criteria

- Typing a place name shows live suggestions biased to the current view; selecting one
  drops a red pin and pans there.
- Typing `lat, lng` and pressing Enter drops a pin at exactly those coordinates.
- The × button (and Esc) clears the input **and** removes the marker, ready for a fresh search.
- Uses the new Places API only (no deprecated `AutocompleteService`/`PlacesService`).
- The map, route rendering, and naming flow are unaffected.
