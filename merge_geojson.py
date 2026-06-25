#!/usr/bin/env python3
"""Append routes from a GeoJSON file into an EXISTING project.

Usage:
    python merge_geojson.py <project_id> <file.geojson> [--include-unsynced]

By default applies the same synced-leaf filter + all-or-nothing corridor rule as a
normal import. With --include-unsynced it keeps every leaf regardless of sync_status
and forms a corridor from every parent_route_id group (used to add unsynced routes,
e.g. outside-jurisdiction roads).

New corridors get cor_codes continuing after the project's existing ones; segments
already present in the project (matched by uuid) are skipped, so re-running is safe.
"""
import json, os, sys
import db as _db
import importer


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    include_unsynced = "--include-unsynced" in sys.argv
    if len(args) != 2:
        sys.exit("usage: python merge_geojson.py <project_id> <file.geojson> [--include-unsynced]")
    pid = int(args[0])
    path = args[1]
    db_path = os.environ.get("ROADNAMER_DB", "roadnamer.db")

    feats = (json.load(open(path)) or {}).get("features", [])
    work = importer.build_workset(feats, require_synced=not include_unsynced)
    all_segs = work["standalone"] + [s for c in work["corridors"] for s in c["segments"]]
    if not all_segs:
        sys.exit("no syncable leaf features found (try --include-unsynced)")
    twins = importer.detect_twins(all_segs)

    conn = _db.connect(db_path)
    if not conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
        sys.exit(f"no such project: {pid}")

    existing = {r["uuid"] for r in conn.execute(
        "SELECT uuid FROM segments WHERE project_id=?", (pid,))}
    oi = conn.execute("SELECT COALESCE(MAX(order_index),-1) m FROM corridors WHERE project_id=?",
                      (pid,)).fetchone()["m"]
    code = 0
    for r in conn.execute("SELECT cor_code FROM corridors WHERE project_id=?", (pid,)):
        cc = r["cor_code"] or ""
        if cc.startswith("cor_"):
            try:
                code = max(code, int(cc[4:]))
            except ValueError:
                pass

    added_segs = 0

    def ins_seg(s, corridor_id, seq):
        nonlocal added_segs
        if s["uuid"] in existing:
            return
        conn.execute(
            """INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
               route_name_imported,twin_uuid) VALUES(?,?,?,?,?,?,?,?)""",
            (pid, s["uuid"], corridor_id, seq, json.dumps(s["coords"]),
             json.dumps(s["props"]), s["route_name"], twins.get(s["uuid"])))
        existing.add(s["uuid"])
        added_segs += 1

    added_cors = 0
    for cor in work["corridors"]:
        if all(s["uuid"] in existing for s in cor["segments"]):
            continue  # nothing new to add
        oi += 1
        code += 1
        cid = conn.execute(
            "INSERT INTO corridors(project_id,cor_code,order_index) VALUES(?,?,?)",
            (pid, f"cor_{code:03d}", oi)).lastrowid
        added_cors += 1
        for k, s in enumerate(cor["segments"]):
            ins_seg(s, cid, k)
    for s in work["standalone"]:
        ins_seg(s, None, 0)

    conn.commit()
    conn.close()
    print(json.dumps({
        "project": pid, "include_unsynced": include_unsynced,
        "added_corridors": added_cors, "added_segments": added_segs,
        "skipped_existing": len(all_segs) - added_segs,
    }))


if __name__ == "__main__":
    main()
