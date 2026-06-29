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
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id AND s.merged_into IS NULL) seg_count,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id AND s.merged_into IS NULL AND s.name<>'') named_count
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
    name_by_uuid = {r["uuid"]: r["name"]
                    for r in c.execute("SELECT uuid,name FROM segments WHERE project_id=? AND merged_into IS NULL", (pid,))}
    corrs = [dict(r) for r in c.execute(
        "SELECT * FROM corridors WHERE project_id=? ORDER BY order_index,id", (pid,))]
    segs = []
    for r in c.execute("SELECT * FROM segments WHERE project_id=? AND merged_into IS NULL ORDER BY corridor_id,seq", (pid,)):
        coords = json.loads(r["geom"])
        twin = r["twin_uuid"]
        twin_name = (name_by_uuid.get(twin) or None) if twin else None
        segs.append({"id": r["id"], "uuid": r["uuid"], "corridor_id": r["corridor_id"],
                     "seq": r["seq"], "coords": coords, "mid": _point_at(coords, 0.5),
                     "route_name_imported": r["route_name_imported"], "name": r["name"],
                     "named": r["name"] != "", "sug_geocode": r["sug_geocode"],
                     "sug_roads": r["sug_roads"], "twin_uuid": twin,
                     "twin_name": twin_name if twin_name else None})
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
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(400, "Select at least two segments to merge.")
    c = conn()
    try:
        rows = []
        for sid in ids:
            r = c.execute(
                "SELECT * FROM segments WHERE id=? AND project_id=? AND merged_into IS NULL",
                (sid, pid)).fetchone()
            if not r:
                raise HTTPException(400, f"Segment {sid} not found or already merged.")
            rows.append(dict(r))
        segs = [{"coords": json.loads(r["geom"]), "row": r} for r in rows]
        try:
            ordered = merge.order_chain(segs)
        except merge.MergeError as e:
            raise HTTPException(400, str(e))
        merged_coords = merge.merge_coords(ordered)
        ordered_rows = [o["row"] for o in ordered]

        # Corridor placement: standalone unless every segment is in one corridor
        # that still keeps other live segments after the merge.
        corr_ids = {r["corridor_id"] for r in ordered_rows}
        new_corr, new_seq = None, 0
        if len(corr_ids) == 1 and next(iter(corr_ids)) is not None:
            cid = next(iter(corr_ids))
            ph = ",".join("?" * len(ids))
            remaining = c.execute(
                f"SELECT COUNT(*) n FROM segments WHERE corridor_id=? AND merged_into IS NULL "
                f"AND id NOT IN ({ph})", (cid, *ids)).fetchone()["n"]
            if remaining > 0:
                new_corr = cid
                new_seq = min(r["seq"] for r in ordered_rows)

        # Props: copy first segment's props, drop stale ids, add provenance.
        props = json.loads(ordered_rows[0]["props"] or "{}")
        for k in ("parent_route_id", "segment_order", "uuid"):
            props.pop(k, None)
        props["merged_from"] = [r["uuid"] for r in ordered_rows]

        # route_name_imported: distinct non-empty source names joined.
        seen, names = set(), []
        for r in ordered_rows:
            rn = (r["route_name_imported"] or "").strip()
            if rn and rn not in seen:
                seen.add(rn); names.append(rn)
        route_name_imported = " + ".join(names) if names else "Merged segment"

        merged_uuid = str(uuidlib.uuid4())
        new_id = c.execute(
            """INSERT INTO segments(project_id,uuid,corridor_id,seq,geom,props,
               route_name_imported,name,sug_geocode,sug_roads,twin_uuid,merged_into)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, merged_uuid, new_corr, new_seq, json.dumps(merged_coords),
             json.dumps(props), route_name_imported, name, "", "", None, None)).lastrowid

        ph = ",".join("?" * len(ids))
        c.execute(f"UPDATE segments SET merged_into=? WHERE id IN ({ph})", (merged_uuid, *ids))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "merged_segment_id": new_id,
            "merged_uuid": merged_uuid, "corridor_id": new_corr}

@app.get("/api/projects/{pid}/export")
def export_project(pid: int):
    c = conn()
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "no such project")
    corrs = [dict(r) for r in c.execute("SELECT * FROM corridors WHERE project_id=?", (pid,))]
    segs = [dict(r) for r in c.execute("SELECT * FROM segments WHERE project_id=? AND merged_into IS NULL ORDER BY corridor_id, seq", (pid,))]
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
