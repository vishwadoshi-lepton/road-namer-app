# Map Places Search — Implementation Plan

> **For agentic workers:** This is a single-file frontend feature verified in a real browser. Steps use checkbox (`- [ ]`) syntax. The pure coordinate parser is verified by an inline `node` assertion; everything else is verified by driving the live app in a browser.

**Goal:** Add a Google-Maps-style search box at the top-center of the map that finds places (autocomplete) or raw `lat,lng`, drops a single red marker at the result, and clears (× / Esc) to remove the marker and search again.

**Architecture:** Vanilla-JS module added to `static/index.html`. Calls the **Places API (New) REST** (`https://places.googleapis.com/v1`) directly from the browser using the existing `GOOGLE_MAPS_JS_KEY`. No backend, no `places` JS library. One `searchMarker`.

**Tech Stack:** Google Maps JS API (already loaded), Places API (New) REST via `fetch`, plain DOM/CSS.

## Global Constraints

- New Places API only — endpoints `POST /v1/places:autocomplete` and `GET /v1/places/{id}`. No deprecated `AutocompleteService`/`PlacesService`/`Autocomplete` widget.
- Auth: header `X-Goog-Api-Key: <GOOGLE_MAPS_JS_KEY>`; minimal `X-Goog-FieldMask` per call (`suggestions.placePrediction.placeId,suggestions.placePrediction.text,suggestions.placePrediction.structuredFormat` for autocomplete; `location` for details).
- One **session token** (UUID) per search session, sent on autocomplete + details, regenerated after a selection/clear.
- Placement: overlay **top-center of the map**; do not move/obscure existing controls.
- Marker: a single **default red `google.maps.Marker`**, high `zIndex`, no popup. Locator only — never touches corridor selection or naming.
- × visibility reuses the existing `input:not(:placeholder-shown) + .clear` CSS pattern.
- Coordinates: `|lat| ≤ 90`, `|lng| ≤ 180`; invalid → inline error, no API call.
- Degrade gracefully if Places API (New) is not enabled on the key (403) — log, close dropdown, map unaffected.

---

## File Structure

All changes in `static/index.html`:
- **Markup:** a `#places-search` container (input + 🔍 + × + `#places-dropdown`) placed inside the map column so it overlays the map.
- **CSS:** bar/input/×/dropdown-row/error styles; the map column must be `position: relative` so the absolute bar anchors to it.
- **JS module:** capture the maps key at config-load; `looksLikeCoords`/`parseCoords`; `placesAutocomplete`/`placeLocation` (REST); `renderDropdown`/`closeDropdown`; `showSearchMarker`/`clearSearch`; debounce + keyboard handlers; `initPlacesSearch()` called once the map + key exist.

---

## Task 1: Capture the maps key for Places REST

**Files:** Modify `static/index.html` (the `/api/config` → script-loader path).

**Interfaces:**
- Produces: module-level `let MAPS_KEY = ''` set to the config's `maps_key` before the Maps script loads, readable by the Places fetch helpers.

- [ ] **Step 1:** Find where `/api/config` is fetched and `script.src = …key=${mapsKey}…` is built. Add a module-scope `let MAPS_KEY = '';` near the other state declarations, and set `MAPS_KEY = mapsKey;` right where `mapsKey` is obtained.
- [ ] **Step 2: Verify** the page still loads and the map renders (no behavior change yet). `node --check` the extracted script.
- [ ] **Step 3: Commit** `git add static/index.html && git commit -m "feat(search): capture maps key for Places REST"`

---

## Task 2: Coordinate parser (pure, asserted)

**Files:** Modify `static/index.html` (JS module).

**Interfaces:**
- Produces:
  - `looksLikeCoords(s) -> boolean` — true iff the trimmed input is exactly two parseable numbers.
  - `parseCoords(s) -> {lat:number,lng:number} | {error:string}` — validated, ranges enforced.

- [ ] **Step 1: Implement** in the JS module:

```javascript
function looksLikeCoords(s) {
  const parts = (s || '').trim().split(/[,\s]+/).filter(Boolean);
  return parts.length === 2 && !isNaN(parseFloat(parts[0])) && !isNaN(parseFloat(parts[1]));
}

function parseCoords(s) {
  const parts = (s || '').trim().split(/[,\s]+/).filter(Boolean);
  if (parts.length < 2) return { error: 'Enter both lat and lng' };
  if (parts.length > 2) return { error: 'Use: lat, lng' };
  const lat = parseFloat(parts[0]), lng = parseFloat(parts[1]);
  if (isNaN(lat) || isNaN(lng)) return { error: 'Invalid numbers' };
  if (Math.abs(lat) > 90) return { error: 'Latitude must be -90..90' };
  if (Math.abs(lng) > 180) return { error: 'Longitude must be -180..180' };
  return { lat, lng };
}
```

- [ ] **Step 2: Verify** with an inline assertion script (no test file needed):

```bash
node -e '
const looksLikeCoords=(s)=>{const p=(s||"").trim().split(/[,\s]+/).filter(Boolean);return p.length===2&&!isNaN(parseFloat(p[0]))&&!isNaN(parseFloat(p[1]));};
const parseCoords=(s)=>{const p=(s||"").trim().split(/[,\s]+/).filter(Boolean);if(p.length<2)return{error:"Enter both lat and lng"};if(p.length>2)return{error:"Use: lat, lng"};const lat=parseFloat(p[0]),lng=parseFloat(p[1]);if(isNaN(lat)||isNaN(lng))return{error:"Invalid numbers"};if(Math.abs(lat)>90)return{error:"Latitude must be -90..90"};if(Math.abs(lng)>180)return{error:"Longitude must be -180..180"};return{lat,lng};};
const a=(c,m)=>{if(!c){console.error("FAIL",m);process.exit(1);}};
a(looksLikeCoords("25.57, 91.88"),"comma coords"); a(looksLikeCoords("25.57 91.88"),"space coords");
a(!looksLikeCoords("Police Bazar"),"name not coords"); a(!looksLikeCoords("25.57"),"single not coords");
a(parseCoords("25.57, 91.88").lat===25.57,"parse lat"); a(parseCoords("25.57").error,"missing lng err");
a(parseCoords("200, 0").error,"lat range err"); a(parseCoords("0, 999").error,"lng range err");
console.log("parseCoords OK");
'
```
Expected: `parseCoords OK`.

- [ ] **Step 3: Commit** `git add static/index.html && git commit -m "feat(search): coordinate parser"`

---

## Task 3: Places REST helpers + session token + marker

**Files:** Modify `static/index.html` (JS module).

**Interfaces:**
- Consumes: `MAPS_KEY`, `googleMap`.
- Produces: `placesAutocomplete(input) -> Promise<prediction[]>` (each `{placeId, text:{text}, structuredFormat:{mainText:{text}, secondaryText:{text}}}`); `placeLocation(placeId) -> Promise<{latitude,longitude}>`; `showSearchMarker(lat,lng)`; `clearSearch()`; `newSessionToken()`; module vars `searchMarker`, `placesSessionToken`.

- [ ] **Step 1: Implement:**

```javascript
const PLACES_BASE = 'https://places.googleapis.com/v1';
let searchMarker = null;
let placesSessionToken = null;

function newSessionToken() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

async function placesAutocomplete(input) {
  const c = googleMap.getCenter();
  const res = await fetch(`${PLACES_BASE}/places:autocomplete`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Goog-Api-Key': MAPS_KEY,
      'X-Goog-FieldMask': 'suggestions.placePrediction.placeId,suggestions.placePrediction.text,suggestions.placePrediction.structuredFormat'
    },
    body: JSON.stringify({
      input, sessionToken: placesSessionToken,
      locationBias: { circle: { center: { latitude: c.lat(), longitude: c.lng() }, radius: 50000 } }
    })
  });
  if (!res.ok) throw new Error('autocomplete ' + res.status);
  const j = await res.json();
  return (j.suggestions || []).map(s => s.placePrediction).filter(Boolean);
}

async function placeLocation(placeId) {
  const res = await fetch(
    `${PLACES_BASE}/places/${encodeURIComponent(placeId)}?sessionToken=${encodeURIComponent(placesSessionToken || '')}`,
    { headers: { 'X-Goog-Api-Key': MAPS_KEY, 'X-Goog-FieldMask': 'location' } });
  if (!res.ok) throw new Error('details ' + res.status);
  return (await res.json()).location;
}

function showSearchMarker(lat, lng) {
  if (searchMarker) searchMarker.setMap(null);
  searchMarker = new google.maps.Marker({ position: { lat, lng }, map: googleMap, zIndex: 9999 });
  googleMap.panTo({ lat, lng });
  if ((googleMap.getZoom() || 0) < 14) googleMap.setZoom(16);
}
```

- [ ] **Step 2: Verify** `node --check` the extracted `<script>` passes (no runtime call yet).
- [ ] **Step 3: Commit** `git add static/index.html && git commit -m "feat(search): Places REST helpers + session token + marker"`

---

## Task 4: Search bar markup + CSS + clear (×)

**Files:** Modify `static/index.html` (markup inside the map column + CSS).

**Interfaces:**
- Produces DOM: `#places-search` (container), `#places-input` (text input, placeholder `Search place or lat, lng`), `.places-clear` (×), `#places-dropdown` (list), `#places-error` (inline error). Produces `clearSearch()`.

- [ ] **Step 1: Add markup** inside the map column (the element that contains `#map`), so it overlays:

```html
<div id="places-search">
  <span class="places-icon">⌕</span>
  <input id="places-input" type="text" placeholder="Search place or lat, lng" autocomplete="off" spellcheck="false"/>
  <button class="places-clear" tabindex="-1" title="Clear">×</button>
  <div id="places-error"></div>
  <div id="places-dropdown"></div>
</div>
```

- [ ] **Step 2: Add CSS** (map column needs `position: relative`; if it isn't already, add it to that selector):

```css
#places-search { position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
  z-index: 5; width: min(380px, 60%); }
#places-search .places-icon { position: absolute; left: 12px; top: 9px; color: #5f6368; font-size: 16px; pointer-events: none; }
#places-input { width: 100%; box-sizing: border-box; height: 38px; padding: 0 34px 0 34px;
  border: 1px solid #dadce0; border-radius: 22px; background: #fff; font-size: 14px; color: #202124;
  box-shadow: 0 1px 4px rgba(0,0,0,.18); outline: none; }
#places-input:focus { border-color: #4f8cff; box-shadow: 0 1px 6px rgba(79,140,255,.35); }
.places-clear { position: absolute; right: 8px; top: 7px; width: 24px; height: 24px; border: none;
  background: transparent; color: #5f6368; font-size: 18px; line-height: 1; cursor: pointer; display: none; }
#places-input:not(:placeholder-shown) ~ .places-clear { display: block; }
#places-dropdown { position: absolute; top: 42px; left: 0; right: 0; background: #fff; border-radius: 10px;
  box-shadow: 0 2px 10px rgba(0,0,0,.22); overflow: hidden; display: none; }
#places-dropdown.open { display: block; }
.places-row { padding: 8px 12px; cursor: pointer; font-size: 13px; }
.places-row.active, .places-row:hover { background: #f1f5fb; }
.places-row .main { color: #202124; }
.places-row .sec { color: #80868b; font-size: 12px; }
.places-row.empty { color: #80868b; cursor: default; }
#places-error { position: absolute; top: 42px; left: 12px; font-size: 12px; color: #d93025;
  background: #fff; padding: 2px 8px; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.15); display: none; }
#places-error.show { display: block; }
```

- [ ] **Step 3: Wire the × + Esc** (clearSearch) and a `closeDropdown` helper:

```javascript
function closeDropdown() {
  const dd = document.getElementById('places-dropdown');
  dd.classList.remove('open'); dd.innerHTML = '';
}
function clearSearch() {
  const inp = document.getElementById('places-input');
  inp.value = '';
  closeDropdown();
  document.getElementById('places-error').classList.remove('show');
  if (searchMarker) { searchMarker.setMap(null); searchMarker = null; }
  placesSessionToken = null;
}
```

- [ ] **Step 4: Verify** `node --check` passes; load the app and confirm the bar appears top-center over the map, the × shows once you type, and clicking × empties the field. (Marker/search not wired until Task 5.)
- [ ] **Step 5: Commit** `git add static/index.html && git commit -m "feat(search): search bar UI + clear button"`

---

## Task 5: Wire behavior — debounce, dropdown, keyboard, coordinate mode

**Files:** Modify `static/index.html` (JS module).

**Interfaces:**
- Consumes everything above.
- Produces `initPlacesSearch()` (called once after the map exists). Module vars `acResults` (prediction[]), `acIndex` (number), `placesDebounce` (timer).

- [ ] **Step 1: Implement** the wiring:

```javascript
let acResults = [];
let acIndex = -1;
let placesDebounce = null;

function showCoordError(msg) {
  const e = document.getElementById('places-error');
  if (msg) { e.textContent = msg; e.classList.add('show'); } else { e.classList.remove('show'); }
}

function renderDropdown() {
  const dd = document.getElementById('places-dropdown');
  if (!acResults.length) { dd.innerHTML = '<div class="places-row empty">No matches</div>'; dd.classList.add('open'); return; }
  dd.innerHTML = acResults.map((p, i) => {
    const sf = p.structuredFormat || {};
    const main = (sf.mainText && sf.mainText.text) || (p.text && p.text.text) || '';
    const sec = (sf.secondaryText && sf.secondaryText.text) || '';
    return `<div class="places-row${i === acIndex ? ' active' : ''}" data-i="${i}">
      <div class="main">${esc(main)}</div>${sec ? `<div class="sec">${esc(sec)}</div>` : ''}</div>`;
  }).join('');
  dd.classList.add('open');
  dd.querySelectorAll('.places-row[data-i]').forEach(row =>
    row.addEventListener('mousedown', e => { e.preventDefault(); selectPrediction(Number(row.dataset.i)); }));
}

async function selectPrediction(i) {
  const p = acResults[i]; if (!p) return;
  const inp = document.getElementById('places-input');
  inp.value = (p.text && p.text.text) || '';
  closeDropdown();
  try {
    const loc = await placeLocation(p.placeId);
    if (loc) showSearchMarker(loc.latitude, loc.longitude);
  } catch (err) { console.error('[places] details', err); showCoordError("Couldn't load that place"); }
  placesSessionToken = null; // session ends on selection
}

async function runAutocomplete(value) {
  if (!placesSessionToken) placesSessionToken = newSessionToken();
  try {
    acResults = await placesAutocomplete(value);
    acIndex = -1;
    renderDropdown();
  } catch (err) {
    console.error('[places] autocomplete', err);
    acResults = []; closeDropdown();
    if (String(err.message).includes('403')) {
      const dd = document.getElementById('places-dropdown');
      dd.innerHTML = '<div class="places-row empty">Search unavailable</div>'; dd.classList.add('open');
    }
  }
}

function onPlacesInput(value) {
  clearTimeout(placesDebounce);
  showCoordError('');
  if (!value.trim()) { closeDropdown(); if (searchMarker) { searchMarker.setMap(null); searchMarker = null; } return; }
  if (looksLikeCoords(value)) { closeDropdown(); return; }     // coordinate mode: wait for Enter
  if (value.trim().length < 3) { closeDropdown(); return; }
  placesDebounce = setTimeout(() => runAutocomplete(value.trim()), 300);
}

function onPlacesKeydown(e) {
  const inp = e.target;
  if (e.key === 'Escape') { clearSearch(); inp.blur(); return; }
  if (looksLikeCoords(inp.value)) {
    if (e.key === 'Enter') {
      const r = parseCoords(inp.value);
      if (r.error) { showCoordError(r.error); } else { showCoordError(''); showSearchMarker(r.lat, r.lng); closeDropdown(); }
    }
    return;
  }
  if (e.key === 'ArrowDown') { e.preventDefault(); if (acResults.length) { acIndex = Math.min(acIndex + 1, acResults.length - 1); renderDropdown(); } }
  else if (e.key === 'ArrowUp') { e.preventDefault(); if (acResults.length) { acIndex = Math.max(acIndex - 1, -1); renderDropdown(); } }
  else if (e.key === 'Enter') { e.preventDefault(); if (acResults.length) selectPrediction(acIndex >= 0 ? acIndex : 0); }
}

function initPlacesSearch() {
  const inp = document.getElementById('places-input');
  if (!inp) return;
  inp.addEventListener('input', () => onPlacesInput(inp.value));
  inp.addEventListener('keydown', onPlacesKeydown);
  document.querySelector('#places-search .places-clear')
    .addEventListener('click', () => { clearSearch(); inp.focus(); });
  document.addEventListener('click', e => {
    if (!e.target.closest('#places-search')) closeDropdown();
  });
}
```

- [ ] **Step 2: Call `initPlacesSearch()`** once the map is ready — add it next to `initMapClickDeselect();` in the map-init path.
- [ ] **Step 3: Verify** `node --check` passes; full browser pass in Task 6.
- [ ] **Step 4: Commit** `git add static/index.html && git commit -m "feat(search): debounce, dropdown, keyboard, coordinate mode"`

---

## Task 6: End-to-end browser verification

**Files:** none (verification + any fixups).

- [ ] **Step 1:** Serve the app (`GOOGLE_MAPS_JS_KEY` with Places API (New) enabled). Confirm in a real browser:
  - Search bar shows top-center over the map; × appears only when non-empty.
  - Typing a place (≥3 chars, e.g. "Police Bazar") shows a suggestions dropdown biased to the view; selecting one drops a **red pin** and pans there; the input fills with the place text.
  - ↓/↑ highlight rows; **Enter** selects; **Esc** clears input + removes the pin.
  - Typing `25.57, 91.88` + Enter drops a pin at exactly those coords (no API call); invalid (`25.57` or `200, 0`) shows the inline error and no pin.
  - **×** empties the field and removes the marker; a fresh search works (new session token).
  - Route rendering / corridor selection / naming are unaffected.
- [ ] **Step 2:** Fix any issues found; re-verify.
- [ ] **Step 3: Commit** any fixups `git add static/index.html && git commit -m "fix(search): e2e verification fixups"`

---

## Self-Review

**Spec coverage:** browser REST new Places API (T3) ✓ · reuse maps key (T1) ✓ · top-center overlay (T4) ✓ · red pin, no popup, locator-only (T3) ✓ · autocomplete + dropdown + keyboard + debounce + bias (T5) ✓ · coordinate detect/validate (T2,T5) ✓ · × / Esc clears input + marker + session (T4,T5) ✓ · session token lifecycle (T3,T5) ✓ · graceful 403 degrade (T5) ✓ · field masks/endpoints exact (T3, Global Constraints) ✓ · manual verification (T6) ✓.

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `placesAutocomplete` returns `placePrediction[]` whose `.placeId/.text/.structuredFormat` are consumed identically in `renderDropdown`/`selectPrediction`; `placeLocation` returns `{latitude,longitude}` consumed by `showSearchMarker(lat,lng)`; `clearSearch`/`closeDropdown`/`showSearchMarker`/`newSessionToken` names match across tasks; `MAPS_KEY` defined in T1 used in T3.
