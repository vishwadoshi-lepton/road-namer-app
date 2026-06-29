# Merge Membership Model + Extend Merge — Design Spec

**Date:** 2026-06-29
**Status:** Approved (brainstorm complete)
**Supersedes the merge core of:** the earlier merge / unmerge / parts-preview specs (single-parent `merged_into`).

## 1. Overview

Two linked changes:

1. **Membership model.** Replace the single-parent `merged_into` pointer with a
   many-to-many `merge_members` table. A physical segment may belong to **multiple**
   merged roads (overlap), so the same atom can be reused in another merge (the
   customer's S3-in-M1-and-M2 case). Original atoms are never consumed; they are
   hidden from naming/count/export while they belong to ≥1 merge, and remain
   selectable for further merges.
2. **Extend merge.** A button on a merged road opens Merge mode pre-seeded with that
   road's atoms; on save the user chooses **Modify this stretch** (update the same
   road in place) or **Create new stretch** (make a new overlapping road).

No data loss: the migration is additive and idempotent, validated against a copy of
the production `roadnamer.db`.

## 2. Data model

New table:

```sql
CREATE TABLE IF NOT EXISTS merge_members(
  project_id INTEGER, merge_uuid TEXT, member_uuid TEXT, seq INTEGER,
  PRIMARY KEY(project_id, merge_uuid, member_uuid),
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
```

- A merged road's contents = its `merge_members` rows (members ordered by `seq`).
- A member is an atom **or** another merge (legacy nesting still works).
- **The one rule:** a segment is **hidden** (not named, not counted, not exported,
  dropped from its corridor) iff its uuid is a `member_uuid` of any merge in the
  project. Overlap follows naturally.
- `is_merged(seg)` = the segment **has** members. `merged_from` = its ordered member
  uuids. Both derived from `merge_members` (no longer from `props`).
- `merged_into` column is kept but **vestigial** (never read for decisions; aids
  rollback). New merges do **not** set it.
- Cycles are impossible: a merge is created from pre-existing segments and members
  are only ever added by *newer* merges, so the graph is acyclic by construction.

### Migration (`db.init_db`, idempotent, additive)
1. Create `merge_members` (via schema).
2. **Back-fill** from legacy data: for every segment whose `props.merged_from`
   exists, insert `merge_members(project_id, seg.uuid, child_uuid, seq)` with
   `INSERT OR IGNORE`. This reconstructs both flat and nested existing roads.
3. No segment row is modified or deleted.

## 3. Backend endpoints

Helper `_membership(c, pid)` → `(member_set, members_of)` where `member_set` is the
set of hidden uuids and `members_of[merge_uuid] = [member_uuid…]` in seq order.

- **`get_project`** — visible segments = those whose uuid ∉ `member_set`. `is_merged`
  / `merged_from` from `members_of`. Corridors with no visible segment are hidden
  (unchanged behavior).
- **`list_projects`** — `seg_count` / `named_count` count visible segments only
  (uuid not a member).
- **`export_project`** — export visible segments; for each merged road inject
  `merged_from` (its members) into the feature properties. Empty corridors dropped.
- **`POST /api/projects/{pid}/merge`** — body `{segment_ids, name, modify_id?}`.
  - Validate the ids exist in the project and form one directed chain (`order_chain`).
  - **No `modify_id` (create):** insert a new merge segment (new uuid, concatenated
    geom, name, recomputed `route_name_imported`); insert `merge_members` rows for
    the selected ids in chain order. Corridor placement: standalone unless all
    selected belong to one corridor that still keeps a visible segment (uses
    membership for "visible"). Does **not** set `merged_into`.
  - **With `modify_id` (modify in place):** target must be a merge in the project and
    not itself selected; replace its `merge_members` with the new chain; update its
    `geom`, `name`, `route_name_imported`; keep its uuid, corridor_id, seq. Removed
    members become visible if unused; if the target was nested, a former sub-merge it
    no longer references simply becomes a visible road again.
  - Returns `{ok, merged_segment_id, merged_uuid, corridor_id}`.
- **`POST /api/segments/{sid}/unmerge`** — target must be visible (not a member) and a
  merge. Delete its `merge_members` rows and the merge row. Members reappear iff not
  still a member of another merge. Returns `{ok, count, restored_ids}` where
  `count` = number of members removed and `restored_ids` = members now visible.
- **`GET /api/segments/{sid}/parts`** — immediate members (from `merge_members`,
  ordered), each `{id, uuid, coords, name, route_name_imported, is_merged,
  merged_count}`. Drill-down unchanged. 400 if not a merge.
- **NEW `GET /api/projects/{pid}/atoms`** — every atomic segment (uuid not a
  `merge_uuid`, i.e. has no members), **including ones hidden in merges**. Each
  `{id, uuid, coords, name, route_name_imported, corridor_id, cor_code, in_merges}`
  where `in_merges` = how many merges contain it. This is Merge mode's selectable set.
- **NEW `GET /api/segments/{sid}/leaf_atoms`** — the road's flattened leaf atoms in
  order, `[{id, uuid}]` (recursively expands member merges). Used to pre-seed Extend.
  400 if not a merge.

## 4. Frontend

### 4.1 Merge mode over atoms
- Entering Merge mode loads `GET /atoms` into `mergeAtoms` and uses that as the
  selectable universe (so already-merged atoms like S3 are pickable again). All the
  merge-mode helpers (`validateChain`, `mergeCandidates`, `orderedSelectionSegs`,
  `renderMergeMap`, `renderMergePanel`, overlap hit-testing) operate on `mergeAtoms`
  instead of the visible-segment list. Lookups go through an `atomById(id)` map.
- Atoms already used in a road (`in_merges > 0`) are **marked** — a small "in N" tag
  in the list and a subtly distinct style on the map — so reuse is visible.
- The panel list groups atoms by `cor_code` (+ a Standalone group).

### 4.2 Extend merge
- An **"Extend merge"** button on a merged road's card (next to Unmerge / Preview).
- Click → `GET /leaf_atoms`, set `mergeSelection` to those atom ids, set
  `extendContext = {id, name}`, enter Merge mode (loads atoms), render with the
  pre-seeded selection (guided red candidates appear front/back; full add+remove).
- When `extendContext` is set, the save area shows a **name field** (prefilled with
  `extendContext.name`, editable) and two buttons: **Modify this stretch** and
  **Create new stretch** (+ Cancel).
  - Create new → `doMerge()` (POST `/merge` with `segment_ids` + `name`).
  - Modify → POST `/merge` with `segment_ids` + `name` + `modify_id = extendContext.id`.
- After either, exit Merge mode, refetch project, re-render.

## 5. Testing

Existing API tests must still pass (the general merge endpoint preserves the
nesting/recursive-unmerge tests). New tests:
- **Overlap:** merge S1,S2,S3→M1; merge S3,S4,S5→M2; both exported; S3 hidden; both
  roads contain S3 (`/parts`).
- **Unmerge frees-if-unused:** unmerge M1 → S1,S2 visible; S3 still hidden (in M2).
- **`/atoms`:** returns all atoms incl. hidden, with `in_merges` counts.
- **`leaf_atoms`:** flat and nested return the leaf atoms in order.
- **Modify (`modify_id`):** extend M1 with S0 then modify → same uuid, new geom +
  members; old members freed if removed.
- **Migration:** back-fill reconstructs membership from a legacy `props.merged_from`
  fixture; and a live run against a **copy of the real `roadnamer.db`** converts with
  no error and no row loss.

Frontend verified with headless-Chrome repros (atoms feed selection incl. a reused
atom; Extend → Modify; Extend → Create new) + `node --check`.

## 6. Files touched

- `db.py` — `merge_members` schema + back-fill migration (+ `import json`).
- `app.py` — `_membership` helper; rewrite get_project/list/export/merge/unmerge/parts
  to membership; add `modify_id`; new `/atoms` and `/leaf_atoms`.
- `static/index.html` — merge mode over atoms feed + reuse marks; Extend button +
  extend mode + Modify/Create-new save.
- `tests/` — membership/overlap/atoms/leaf_atoms/modify/migration tests; fixture
  additions (an `SD` atom after `SC` for a 3-atom overlapping M2).

## 7. Out of scope

Selecting whole merged roads as units in the UI (N2); reordering a road's members;
deleting the vestigial `merged_into` column.
