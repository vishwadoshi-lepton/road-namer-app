import json, os, math, io, uuid as uuidlib
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
import db, importer, export, merge

load_dotenv()  # read keys from root .env if present
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("ROADNAMER_DB", os.path.join(HERE, "roadnamer.db"))

app = FastAPI(title="Road Namer")

def conn():
    return db.connect(DB)

# initialise schema on import so tables exist for any connection (incl. TestClient
# constructed without a context manager). init_db is idempotent (CREATE IF NOT EXISTS).
_c = conn(); db.init_db(_c); _c.close()

def _point_at(coords, frac):
    def hav(a, b):
        R = 6371000.0
        dLat = math.radians(b[1]-a[1]); dLng = math.radians(a[0]-b[0])
        h = math.sin(dLat/2)**2 + math.cos(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.sin(dLng/2)**2
        return 2*R*math.asin(min(1, math.sqrt(h)))
    tot = sum(hav(coords[i-1], coords[i]) for i in range(1, len(coords))) * frac
    acc = 0
    for i in range(1, len(coords)):
        d = hav(coords[i-1], coords[i])
        if acc + d >= tot:
            t = (tot-acc)/d if d else 0
            return [coords[i-1][0]+(coords[i][0]-coords[i-1][0])*t,
                    coords[i-1][1]+(coords[i][1]-coords[i-1][1])*t]
        acc += d
    return coords[-1]

def _membership(c, pid):
    """Return (member_set, members_of) for a project.

    member_set: uuids that belong to >=1 merge (hidden from naming/count/export).
    members_of: merge_uuid -> [member_uuid in seq order].
    """
    member_set = set(); members_of = {}
    for r in c.execute("SELECT merge_uuid, member_uuid FROM merge_members WHERE project_id=? ORDER BY merge_uuid, seq", (pid,)):
        members_of.setdefault(r["merge_uuid"], []).append(r["member_uuid"])
        member_set.add(r["member_uuid"])
    return member_set, members_of

@app.post("/api/projects")
async def create_project(file: UploadFile = File(...)):
    try:
        raw = json.loads((await file.read()).decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"could not parse GeoJSON: {e}")
    feats = raw.get("features", []) if isinstance(raw, dict) else []
    work = importer.build_workset(feats)
    all_segs = work["standalone"] + [s for c in work["corridors"] for s in c["segments"]]
    if not all_segs:
        raise HTTPException(400, "no syncable (synced leaf) features found")
    twins = importer.detect_twins(all_segs)

    c = conn()
    try:
        pid = c.execute("INSERT INTO projects(name,created_at) VALUES(?,datetime('now'))",
                        (file.filename or "project",)).lastrowid

        def ins_seg(s, corridor_id, seq):
            c.execute("""INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
                         route_name_imported,twin_uuid) VALUES(?,?,?,?,?,?,?,?)""",
                      (pid, s["uuid"], corridor_id, seq, json.dumps(s["coords"]),
                       json.dumps(s["props"]), s["route_name"], twins.get(s["uuid"])))

        for i, cor in enumerate(work["corridors"]):
            cid = c.execute("INSERT INTO corridors(project_id,cor_code,order_index) VALUES(?,?,?)",
                            (pid, cor["cor_code"], i)).lastrowid
            for k, s in enumerate(cor["segments"]):
                ins_seg(s, cid, k)
        for s in work["standalone"]:
            ins_seg(s, None, 0)
        c.commit()
    finally:
        c.close()
    return {"project_id": pid, "name": file.filename or "project",
            "leaves": len(all_segs), "corridors": len(work["corridors"])}

@app.get("/api/projects")
def list_projects():
    c = conn()
    rows = c.execute("""SELECT p.*,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id
            AND s.uuid NOT IN (SELECT member_uuid FROM merge_members mm WHERE mm.project_id=p.id)) seg_count,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id AND s.name<>''
            AND s.uuid NOT IN (SELECT member_uuid FROM merge_members mm WHERE mm.project_id=p.id)) named_count
        FROM projects p ORDER BY p.id DESC""").fetchall()
    out = [dict(r) for r in rows]; c.close(); return out

@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    c = conn(); c.execute("DELETE FROM projects WHERE id=?", (pid,)); c.commit(); c.close()
    return {"ok": True}

@app.get("/api/projects/{pid}")
def get_project(pid: int):
    c = conn()
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "no such project")
    member_set, members_of = _membership(c, pid)
    name_by_uuid = {r["uuid"]: r["name"]
                    for r in c.execute("SELECT uuid,name FROM segments WHERE project_id=?", (pid,))
                    if r["uuid"] not in member_set}
    corrs = [dict(r) for r in c.execute(
        "SELECT * FROM corridors WHERE project_id=? ORDER BY order_index,id", (pid,))]
    segs = []
    for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id,seq", (pid,)):
        if r["uuid"] in member_set:
            continue   # hidden: belongs to >=1 merge
        coords = json.loads(r["geom"])
        twin = r["twin_uuid"]
        twin_name = (name_by_uuid.get(twin) or None) if twin else None
        mf = members_of.get(r["uuid"])
        segs.append({"id": r["id"], "uuid": r["uuid"], "corridor_id": r["corridor_id"],
                     "seq": r["seq"], "coords": coords, "mid": _point_at(coords, 0.5),
                     "route_name_imported": r["route_name_imported"], "name": r["name"],
                     "named": r["name"] != "", "sug_geocode": r["sug_geocode"],
                     "sug_roads": r["sug_roads"], "twin_uuid": twin,
                     "twin_name": twin_name if twin_name else None,
                     "merged_from": mf if mf else None, "is_merged": bool(mf)})
    c.close()
    # Hide corridors that have no live segments left (e.g. fully merged away).
    live_corr_ids = {s["corridor_id"] for s in segs if s["corridor_id"] is not None}
    corrs = [co for co in corrs if co["id"] in live_corr_ids]
    return {"project": dict(p), "corridors": corrs, "segments": segs}

@app.patch("/api/segments/{sid}")
def patch_segment(sid: int, body: dict = Body(...)):
    name = (body.get("name") or "").strip()
    c = conn(); c.execute("UPDATE segments SET name=? WHERE id=?", (name, sid))
    c.commit(); c.close()
    return {"ok": True, "named": name != ""}

@app.patch("/api/corridors/{cid}")
def patch_corridor(cid: int, body: dict = Body(...)):
    name = (body.get("name") or "").strip()
    c = conn(); c.execute("UPDATE corridors SET name=? WHERE id=?", (name, cid))
    c.commit(); c.close()
    return {"ok": True}

@app.post("/api/projects/{pid}/merge")
def merge_segments(pid: int, body: dict = Body(...)):
    ids = body.get("segment_ids") or []
    name = (body.get("name") or "").strip()
    modify_id = body.get("modify_id")
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(400, "Select at least two segments to merge.")
    c = conn()
    try:
        rows = []
        for sid in ids:
            r = c.execute("SELECT * FROM segments WHERE id=? AND project_id=?", (sid, pid)).fetchone()
            if not r:
                raise HTTPException(400, f"Segment {sid} not found.")
            rows.append(dict(r))
        segs = [{"coords": json.loads(r["geom"]), "row": r} for r in rows]
        try:
            ordered = merge.order_chain(segs)
        except merge.MergeError as e:
            raise HTTPException(400, str(e))
        merged_coords = merge.merge_coords(ordered)
        ordered_rows = [o["row"] for o in ordered]
        member_uuids = [r["uuid"] for r in ordered_rows]

        # route_name_imported: distinct non-empty source names joined.
        seen, names = set(), []
        for r in ordered_rows:
            rn = (r["route_name_imported"] or "").strip()
            if rn and rn not in seen:
                seen.add(rn); names.append(rn)
        route_name_imported = " + ".join(names) if names else "Merged segment"

        # ── Modify an existing road in place (Extend → "Modify this stretch") ──
        if modify_id is not None:
            tgt = c.execute("SELECT * FROM segments WHERE id=? AND project_id=?", (modify_id, pid)).fetchone()
            if not tgt:
                raise HTTPException(400, "modify target not found.")
            if modify_id in ids:
                raise HTTPException(400, "a road cannot contain itself.")
            if not c.execute("SELECT 1 FROM merge_members WHERE merge_uuid=? AND project_id=? LIMIT 1",
                             (tgt["uuid"], pid)).fetchone():
                raise HTTPException(400, "modify target is not a merged road.")
            c.execute("UPDATE segments SET geom=?, name=?, route_name_imported=? WHERE id=?",
                      (json.dumps(merged_coords), name, route_name_imported, modify_id))
            c.execute("DELETE FROM merge_members WHERE merge_uuid=? AND project_id=?", (tgt["uuid"], pid))
            for seq, mu in enumerate(member_uuids):
                c.execute("INSERT OR IGNORE INTO merge_members(project_id,merge_uuid,member_uuid,seq) VALUES(?,?,?,?)",
                          (pid, tgt["uuid"], mu, seq))
            c.commit()
            return {"ok": True, "merged_segment_id": modify_id,
                    "merged_uuid": tgt["uuid"], "corridor_id": tgt["corridor_id"]}

        # ── Create a new road ──
        member_set, _ = _membership(c, pid)
        corr_ids = {r["corridor_id"] for r in ordered_rows}
        new_corr, new_seq = None, 0
        if len(corr_ids) == 1 and next(iter(corr_ids)) is not None:
            cid = next(iter(corr_ids))
            remaining = 0
            for r in c.execute("SELECT id, uuid FROM segments WHERE corridor_id=? AND project_id=?", (cid, pid)):
                if r["id"] not in ids and r["uuid"] not in member_set:
                    remaining += 1
            if remaining > 0:
                new_corr = cid
                new_seq = min(r["seq"] for r in ordered_rows)

        # Props: copy first segment's props, drop stale/source-specific keys.
        props = json.loads(ordered_rows[0]["props"] or "{}")
        for k in ("parent_route_id", "segment_order", "uuid", "merged_from"):
            props.pop(k, None)

        merged_uuid = str(uuidlib.uuid4())
        new_id = c.execute(
            """INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
               route_name_imported,name,sug_geocode,sug_roads,twin_uuid,merged_into)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, merged_uuid, new_corr, new_seq, json.dumps(merged_coords),
             json.dumps(props), route_name_imported, name, "", "", None, None)).lastrowid
        for seq, mu in enumerate(member_uuids):
            c.execute("INSERT OR IGNORE INTO merge_members(project_id,merge_uuid,member_uuid,seq) VALUES(?,?,?,?)",
                      (pid, merged_uuid, mu, seq))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "merged_segment_id": new_id,
            "merged_uuid": merged_uuid, "corridor_id": new_corr}

@app.post("/api/segments/{sid}/unmerge")
def unmerge_segment(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        pid = r["project_id"]
        if c.execute("SELECT 1 FROM merge_members WHERE member_uuid=? AND project_id=? LIMIT 1", (r["uuid"], pid)).fetchone():
            raise HTTPException(400, "Segment is part of another road; unmerge that first.")
        members = [x["member_uuid"] for x in c.execute(
            "SELECT member_uuid FROM merge_members WHERE merge_uuid=? AND project_id=? ORDER BY seq", (r["uuid"], pid))]
        if not members:
            raise HTTPException(400, "Segment is not a merged segment.")
        c.execute("DELETE FROM merge_members WHERE merge_uuid=? AND project_id=?", (r["uuid"], pid))
        c.execute("DELETE FROM segments WHERE id=?", (sid,))
        # Members that are no longer used by any merge become visible again.
        freed = []
        for mu in members:
            if not c.execute("SELECT 1 FROM merge_members WHERE member_uuid=? AND project_id=? LIMIT 1", (mu, pid)).fetchone():
                row = c.execute("SELECT id FROM segments WHERE uuid=? AND project_id=?", (mu, pid)).fetchone()
                if row:
                    freed.append(row["id"])
        c.commit()
    finally:
        c.close()
    return {"ok": True, "restored_ids": freed, "count": len(members)}

@app.get("/api/segments/{sid}/parts")
def segment_parts(sid: int):
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        pid = r["project_id"]
        member_uuids = [x["member_uuid"] for x in c.execute(
            "SELECT member_uuid FROM merge_members WHERE merge_uuid=? AND project_id=? ORDER BY seq", (r["uuid"], pid))]
        if not member_uuids:
            raise HTTPException(400, "Segment is not a merged segment.")
        _, members_of = _membership(c, pid)
        parts = []
        for mu in member_uuids:
            x = c.execute("SELECT * FROM segments WHERE uuid=? AND project_id=?", (mu, pid)).fetchone()
            if not x:
                continue
            mm = members_of.get(mu)
            parts.append({"id": x["id"], "uuid": x["uuid"], "coords": json.loads(x["geom"]),
                          "name": x["name"], "route_name_imported": x["route_name_imported"],
                          "is_merged": bool(mm), "merged_count": len(mm) if mm else 0})
    finally:
        c.close()
    return {"parent_id": sid, "parts": parts}

@app.get("/api/projects/{pid}/atoms")
def project_atoms(pid: int):
    """Every atomic segment (no members of its own), INCLUDING ones hidden inside a
    merge, with how many merges each belongs to. This is Merge mode's selectable set."""
    c = conn()
    try:
        merges = {r["merge_uuid"] for r in c.execute(
            "SELECT DISTINCT merge_uuid FROM merge_members WHERE project_id=?", (pid,))}
        incnt = {}
        for r in c.execute("SELECT member_uuid, COUNT(*) n FROM merge_members WHERE project_id=? GROUP BY member_uuid", (pid,)):
            incnt[r["member_uuid"]] = r["n"]
        cor_code = {r["id"]: r["cor_code"] for r in c.execute("SELECT id, cor_code FROM corridors WHERE project_id=?", (pid,))}
        atoms = []
        for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id, seq", (pid,)):
            if r["uuid"] in merges:
                continue   # it's a merge, not an atom
            atoms.append({"id": r["id"], "uuid": r["uuid"], "coords": json.loads(r["geom"]),
                          "name": r["name"], "route_name_imported": r["route_name_imported"],
                          "corridor_id": r["corridor_id"], "cor_code": cor_code.get(r["corridor_id"]),
                          "in_merges": incnt.get(r["uuid"], 0)})
    finally:
        c.close()
    return {"atoms": atoms}

@app.get("/api/segments/{sid}/leaf_atoms")
def segment_leaf_atoms(sid: int):
    """The road's flattened leaf atoms in order (recursively expands member merges)."""
    c = conn()
    try:
        r = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such segment")
        pid = r["project_id"]
        _, members_of = _membership(c, pid)
        if r["uuid"] not in members_of:
            raise HTTPException(400, "Segment is not a merged segment.")
        out, seen = [], set()
        def walk(u):
            if u in members_of:
                for m in members_of[u]:
                    walk(m)
            elif u not in seen:
                seen.add(u)
                row = c.execute("SELECT id, uuid FROM segments WHERE uuid=? AND project_id=?", (u, pid)).fetchone()
                if row:
                    out.append({"id": row["id"], "uuid": row["uuid"]})
        walk(r["uuid"])
    finally:
        c.close()
    return {"atoms": out}

@app.get("/api/projects/{pid}/export")
def export_project(pid: int):
    c = conn()
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "no such project")
    corrs = [dict(r) for r in c.execute("SELECT * FROM corridors WHERE project_id=?", (pid,))]
    member_set, members_of = _membership(c, pid)
    segs = []
    for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id, seq", (pid,)):
        if r["uuid"] in member_set:
            continue   # hidden: belongs to >=1 merge
        d = dict(r)
        mf = members_of.get(r["uuid"])
        if mf:   # carry provenance into the exported feature properties
            props = json.loads(d["props"] or "{}")
            props["merged_from"] = mf
            d["props"] = json.dumps(props)
        segs.append(d)
    c.close()
    payload = export.build_export(dict(p), corrs, segs)
    buf = io.BytesIO(json.dumps(payload, indent=2).encode())
    return StreamingResponse(buf, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="project_{pid}_named.json"'})

@app.get("/api/config")
def config():
    return {"maps_key": os.environ.get("GOOGLE_MAPS_JS_KEY", "")}

app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("Road Namer -> http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
