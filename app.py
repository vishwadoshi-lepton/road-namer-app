import json, os, math
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
import db, importer

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
    c.commit(); c.close()
    return {"project_id": pid, "name": file.filename or "project",
            "leaves": len(all_segs), "corridors": len(work["corridors"])}

@app.get("/api/projects")
def list_projects():
    c = conn()
    rows = c.execute("""SELECT p.*,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id) seg_count,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id AND s.name<>'') named_count
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
                    for r in c.execute("SELECT uuid,name FROM segments WHERE project_id=?", (pid,))}
    corrs = [dict(r) for r in c.execute(
        "SELECT * FROM corridors WHERE project_id=? ORDER BY order_index,id", (pid,))]
    segs = []
    for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id,seq", (pid,)):
        coords = json.loads(r["geom"])
        twin = r["twin_uuid"]
        twin_name = name_by_uuid.get(twin) or None if twin else None
        segs.append({"id": r["id"], "uuid": r["uuid"], "corridor_id": r["corridor_id"],
                     "seq": r["seq"], "coords": coords, "mid": _point_at(coords, 0.5),
                     "route_name_imported": r["route_name_imported"], "name": r["name"],
                     "named": r["name"] != "", "sug_geocode": r["sug_geocode"],
                     "sug_roads": r["sug_roads"], "twin_uuid": twin,
                     "twin_name": twin_name if twin_name else None})
    c.close()
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

# static frontend (mounted last; added in Task 9)
