# Merge UX Upgrades — Design Spec

**Date:** 2026-06-29
**Status:** Approved (brainstorm complete)
**Builds on:** `docs/superpowers/specs/2026-06-29-merge-segments-design.md` (segment merge + Merge mode already shipped)

## 1. Overview

Four UX upgrades to the segment-merge feature:

1. **Guided selection** in Merge mode — show only what can connect, colour-coded, so the
   selection is always a valid chain.
2. **Quick merge** — a one-click "merge the whole corridor" action on chainable corridors.
3. **Unmerge** — reverse a merge one level at a time (recursive).
4. **Fix:** the panel search box now works inside Merge mode (today it redraws the
   normal UI); hide the named-status chips in Merge mode.

No new export format. Items 2 and 1 need **no backend change** (they reuse the existing
merge endpoint and client logic). Items 3 needs `is_merged`/`merged_from` in `get_project`
and a new unmerge endpoint. Item 4 is frontend-only.

## 2. Guided selection (Merge mode)

A partial selection forms a chain with two open ends: **head** = `start` of the first
ordered segment, **tail** = `end` of the last. Because direction is fixed, only two
kinds of live segment can extend it:
- **append** (back): `start ≈ tail` and `end ≄ head` (would not close a loop)
- **prepend** (front): `end ≈ head` and `start ≄ tail`

(`≈` uses the same `1e-4` tolerance as the server / `near2` on the client.)

**Colours / states** (after the first pick):
- selected chain → **blue**
- any append/prepend candidate → **red**
- everything else → **dimmed grey**

**Before the first pick:** normal status colours (named/unnamed), nothing dimmed — any
segment may start a chain.

**Always-valid invariant:** you may only *add* a red candidate, and only *remove* from
the two ends. The selection is therefore always a valid chain and **Merge is enabled at
2+**. No error banner states.
- Click a **dimmed** segment → no-op + hint toast ("doesn't connect to your selection").
- Click a selected **middle** segment → no-op + hint ("you can only remove from the ends").
- Click a selected **end** segment → removes it.

**Map and panel share one selection.** The flattened panel list mirrors the map: selected
rows blue, candidate rows red-outlined, ineligible rows dimmed; clicking a row runs the
same guarded toggle. You can start on the map and finish in the panel or vice-versa.

**Smart overlap chooser:** on a map click covering several stacked segments, compute the
*actionable* ones = (red candidates) ∪ (the current two end segments, which are removable).
- 0 actionable → no-op + hint.
- exactly 1 actionable → do it silently (add or remove).
- 2+ actionable → show the chooser (hover a row → highlight that segment on the map with a
  direction arrow; click → toggle). At the first pick everything is actionable, so a twin
  pair still prompts — letting you choose the starting direction.

### Client functions
- `near2(a,b,tol=1e-4)` — exists.
- `orderedSelectionSegs()` → segments in chain order (trivial for 0/1; `validateChain` for 2+).
- `mergeCandidates()` → `Set` of eligible-to-add segment ids (all live when selection empty).
- `toggleMergeSeg(id)` — **guarded**: add only if candidate; remove only if an end; else hint.
- `renderMergeMap()` / `renderMergePanel()` — colour by selected/candidate/dim.
- `showHint(msg)` — transient toast.

## 3. Quick merge (per corridor)

- In the normal panel, a **"Quick-merge corridor"** button renders inside a corridor's body
  **only when** it is a real corridor with ≥2 segments whose geometry forms one valid chain
  (`validateChain(ids).ok`, client-side). Non-chainable corridors show no button.
- Click → **styled in-app modal** with a **name field pre-filled by smart cascade**:
  `corr.name` → else `corrSuggestion(corr)` (suggestion or most-common child name) → else blank.
  Buttons: Confirm / Cancel.
- Confirm → `POST /api/projects/{pid}/merge` with all the corridor's segment ids and the name.
  The whole corridor is consumed, so by the existing rule the result is **standalone** and the
  corridor drops out. Refetch + re-render; focus the new segment.
- Reuses the existing merge endpoint — **no backend change**.

## 4. Unmerge (one level, recursive)

**Mechanism (no schema change).** Each merge already records its *immediate* parts: the
children get `merged_into = <merged uuid>`. To unmerge a live merged segment `M`:
1. reactivate its immediate children: `UPDATE segments SET merged_into=NULL WHERE merged_into = M.uuid`.
2. delete `M`'s row.

This is naturally recursive: if a child was itself a merged segment (e.g. `M1` inside `M2`),
reactivating it brings `M1` back as a (still-merged) live segment; its own children remain
pointing at `M1.uuid` until `M1` is unmerged in turn.
- Merge S1,S2,S3→M1, then M1,S4,S5→M2. Unmerge **M2** → M1, S4, S5. Unmerge **M1** → S1, S2, S3.

Children return with their stored geometry, name, and `corridor_id`/`seq`. If unmerging
refills a corridor that had collapsed (0 live segments), that corridor **reappears** with its
saved name (its row was only hidden, never deleted).

**Backend**
- `get_project`: parse each segment's `props`; add `merged_from` (list or null) and
  `is_merged` (bool) to the segment dict.
- New endpoint `POST /api/segments/{sid}/unmerge`:
  - 404 if the segment/project is missing.
  - 400 if the segment is not live (`merged_into` set) or has no immediate children
    (not a merged segment).
  - else reactivate children, delete the row, return `{ok, restored_ids:[...], count}`.

**Frontend**
- A merged segment's panel card shows a **"merged (N)" badge** (N = `merged_from.length`)
  and an **Unmerge** button. The badge also shows in the merge-mode list rows.
- Unmerge → **small styled confirm** ("Unmerge into N segments?") → POST → refetch + render.
- *Simplification:* the Unmerge **button** lives in the normal UI only (merge-mode rows are
  click-to-select); the badge still appears in merge mode for awareness.

## 5. Search inside Merge mode + chips (fix)

- Introduce a single `renderPanel()` dispatcher: `mergeMode ? renderMergePanel() : renderCorrList()`.
  The `#panel-search` input handler calls `renderPanel()` so searching in Merge mode filters
  the **merge list** (via the existing `itemMatchesSearch`) instead of redrawing the normal UI.
- `enterMergeMode` hides `#filter-bar` (the All/Unnamed/Named chips); `exitMergeMode` restores
  it. `#search-bar` stays visible in both modes.

## 6. Styled modal + hint toast (shared UI)

- A reusable modal: `showModal({title, message, withInput, inputValue, confirmText, onConfirm})`
  rendered into a hidden `#app-modal` overlay; Confirm calls `onConfirm(value)` then closes;
  Cancel / overlay-click / Esc closes. Used by Quick merge (with input) and Unmerge (confirm only).
- A hint toast `#hint-toast` + `showHint(msg)` for the guided no-op messages (~1.5s).

## 7. Testing

Backend (pytest):
- Unmerge restores immediate children one level — the nested M1/M2 case (merge whole corridor
  → M1; merge M1 + connected standalone → M2; unmerge M2 → M1 + standalone live; unmerge M1 →
  the three originals live).
- Unmerge revives a collapsed corridor (whole-corridor merge → cor hidden; unmerge → cor back
  with its segments).
- `get_project` exposes `is_merged`/`merged_from`.
- Unmerge rejected (400) on a non-merged segment.
- Quick merge needs no new endpoint (covered by existing merge tests).

Frontend (guided colouring, modals, badges, search-in-merge-mode) verified in-app by the user
and JS syntax-checked (`node --check`).

## 8. Files touched

- `app.py` — `get_project` adds `is_merged`/`merged_from`; new `POST /api/segments/{sid}/unmerge`.
- `static/index.html` — guided selection, Quick-merge button + modal, Unmerge badge/button +
  confirm, `renderPanel()` dispatcher + search fix + hide chips in merge mode, shared modal +
  hint toast.
- `tests/test_api.py` — unmerge + is_merged tests.

## 9. Out of scope (unchanged)

Split, vertex editing, manual direction reverse, full-flatten unmerge.
