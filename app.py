#!/usr/bin/env python3
"""
Road Corridor Namer — local web app (FastAPI + SQLite).

Run:
    pip install -r requirements.txt
    python app.py
    open http://localhost:8000

What it does:
  * Import a GeoJSON of LineString segments  -> a saved Project (resumable).
  * Auto-chain segments into corridors (straightest-path through junctions).
  * Google enrichment (optional) fills a FIRST-LAYER suggestion you can accept or override:
        segment  = "POI to POI"     corridor = "Road: firstPOI to lastPOI"
  * Every Google response is cached in the DB (gcache) — the API is NEVER called twice
    for the same point, and `offline` mode never calls Google at all.
  * Review + edit on a map: rename segments/corridors, and change corridor membership
    (click segments on the map to add/remove, drag in the list, split / merge / new corridor).
  * Everything persists in roadnamer.db — close it, come back, keep going.  Export GeoJSON anytime.
"""
import json, math, os, re, sqlite3, threading, time, io
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("ROADNAMER_DB", os.path.join(HERE, "roadnamer.db"))

# ----------------------------------------------------------------------------- DB
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT,
        snap_tol REAL DEFAULT 25, max_turn REAL DEFAULT 45, enriched INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS corridors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, order_index INTEGER,
        name TEXT DEFAULT '', suggested TEXT DEFAULT '',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS segments(
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, seg_index INTEGER,
        geom TEXT, props TEXT,
        corridor_id INTEGER, seq INTEGER, reversed INTEGER DEFAULT 0,
        road TEXT DEFAULT '', suggested TEXT DEFAULT '', name TEXT DEFAULT '', divided TEXT DEFAULT '',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS gcache(k TEXT PRIMARY KEY, v TEXT);
    """)
    c.commit(); c.close()

# ----------------------------------------------------------------------------- geo
R = 6371000.0
def hav(a, b):
    dLat=math.radians(b[1]-a[1]); dLng=math.radians(a[0]-b[0])
    h=math.sin(dLat/2)**2+math.cos(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.sin(dLng/2)**2
    return 2*R*math.asin(min(1,math.sqrt(h)))
def brg(a,b):
    y=math.sin(math.radians(b[0]-a[0]))*math.cos(math.radians(b[1]))
    x=math.cos(math.radians(a[1]))*math.sin(math.radians(b[1]))-math.sin(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.cos(math.radians(b[0]-a[0]))
    return (math.degrees(math.atan2(y,x))+360)%360
def turn(a,b):
    d=abs(a-b)%360; return 360-d if d>180 else d
def line_len(c): return sum(hav(c[i-1],c[i]) for i in range(1,len(c)))
def point_at(c, frac):
    tot=line_len(c)*frac; acc=0
    for i in range(1,len(c)):
        d=hav(c[i-1],c[i])
        if acc+d>=tot:
            t=(tot-acc)/d if d else 0
            return [c[i-1][0]+(c[i][0]-c[i-1][0])*t, c[i-1][1]+(c[i][1]-c[i-1][1])*t]
        acc+=d
    return c[-1]

def build_corridors(segs, tol, maxturn):
    """segs: list of dicts {id, coords}. Returns list of chains [(id,flip)...]."""
    byid={s["id"]:s for s in segs}; unused=set(byid)
    start=lambda s:s["coords"][0]; end=lambda s:s["coords"][-1]
    def oc(i,flip): c=byid[i]["coords"]; return c[::-1] if flip else c[:]
    def best(p,inh):
        bb=None;bt=1e9
        for j in unused:
            s=byid[j]; flip=None
            if hav(start(s),p)<=tol: flip=False
            elif hav(end(s),p)<=tol: flip=True
            if flip is None: continue
            c=oc(j,flip); t=turn(inh,brg(c[0],c[1]))
            if t<bt: bt=t; bb=(j,flip,c)
        return bb if (bb and bt<=maxturn) else None
    corr=[]
    while unused:
        seed=next(iter(unused)); unused.discard(seed); chain=[(seed,False)]
        c=oc(seed,False); tail=c[-1]; inh=brg(c[-2],c[-1])
        while True:
            n=best(tail,inh)
            if not n: break
            unused.discard(n[0]); chain.append((n[0],n[1])); tail=n[2][-1]; inh=brg(n[2][-2],n[2][-1])
        c=oc(seed,False); head=c[0]; outh=(brg(c[0],c[1])+180)%360
        while True:
            n=best(head,outh)
            if not n: break
            unused.discard(n[0]); cc=oc(n[0],not n[1]); chain.insert(0,(n[0],not n[1])); head=cc[0]; outh=(brg(cc[0],cc[1])+180)%360
        corr.append(chain)
    corr.sort(key=lambda ch:-sum(line_len(byid[i]["coords"]) for i,_ in ch))
    return corr

# ----------------------------------------------------------------------------- naming helpers
ROADS="https://roads.googleapis.com/v1/nearestRoads"
DETAILS="https://maps.googleapis.com/maps/api/place/details/json"
GEOCODE="https://maps.googleapis.com/maps/api/geocode/json"
NEARBY="https://maps.googleapis.com/maps/api/place/nearbysearch/json"
_HOUSE=re.compile(r'^\s*[A-Za-z0-9][A-Za-z0-9/\-]*\d[A-Za-z0-9/\-]*\s*,\s*')
def normalise(name):
    if not name: return ""
    n=name.strip(); prev=None
    while prev!=n: prev=n; n=_HOUSE.sub('',n).strip()
    for p,r in [(r'\bRd\b','Road'),(r'\bSt\b','Street'),(r'\bAve\b','Avenue'),(r'\bHwy\b','Highway')]:
        n=re.sub(p,r,n)
    return n.strip(' ,')
_JUNCTION_RE=re.compile(r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:Circle|Cross Road|Crossing|Char Rasta|Chowk|Chokdi|Flyover|Over\s?bridge|Bridge|Junction|Darwaja|Gam|Naka))')
_SKIP={"locality","political","sublocality","sublocality_level_1","sublocality_level_2","postal_code",
       "administrative_area_level_1","administrative_area_level_2","administrative_area_level_3","country"}
def _scan_junction(results):
    from collections import Counter
    t=Counter()
    for p in results or []:
        if _SKIP & set(p.get("types",[])): continue
        for m in _JUNCTION_RE.findall((p.get("name","")+" | "+p.get("vicinity",""))): t[m.strip()]+=1
    return t.most_common(1)[0][0] if t else ""

class Cache:
    """DB-backed Google cache. get() returns the stored value or None.
       In offline mode, misses raise Offline so we never call Google."""
    def __init__(self, conn, offline): self.c=conn; self.offline=offline
    def get(self,k):
        r=self.c.execute("SELECT v FROM gcache WHERE k=?",(k,)).fetchone()
        return json.loads(r["v"]) if r else None
    def put(self,k,v):
        self.c.execute("INSERT OR REPLACE INTO gcache(k,v) VALUES(?,?)",(k,json.dumps(v))); self.c.commit()

class Offline(Exception): pass

def _get(url, params):
    for t in range(3):
        try:
            r=requests.get(url,params=params,timeout=20)
            if r.status_code==200: return r.json()
        except requests.RequestException: pass
        time.sleep(0.8*(t+1))
    return {}

def route_at(lat,lng,key,cache):
    k=f"gc:{lat:.5f},{lng:.5f}"
    c=cache.get(k)
    if c is not None: return c
    if cache.offline: return ""
    js=_get(GEOCODE,{"key":key,"latlng":f"{lat},{lng}","result_type":"route"})
    route=""
    for r in js.get("results",[]):
        for comp in r.get("address_components",[]):
            if "route" in comp.get("types",[]): route=comp["long_name"]; break
        if route: break
    route=normalise(route); cache.put(k,route); return route

def poi_at(lat,lng,key,cache):
    k=f"nb:{lat:.5f},{lng:.5f}"
    c=cache.get(k)
    if c is not None: return c
    if cache.offline: return ""
    js=_get(NEARBY,{"key":key,"location":f"{lat},{lng}","radius":200})
    nm=_scan_junction(js.get("results",[]))
    if not nm:
        js=_get(NEARBY,{"key":key,"location":f"{lat},{lng}","rankby":"distance","type":"transit_station"})
        for r in js.get("results",[]):
            if not (_SKIP & set(r.get("types",[]))): nm=r.get("name",""); break
    if not nm:
        js=_get(NEARBY,{"key":key,"location":f"{lat},{lng}","rankby":"distance","type":"point_of_interest"})
        for r in js.get("results",[]):
            if not (_SKIP & set(r.get("types",[]))): nm=r.get("name",""); break
    cache.put(k,nm); return nm

def join_poi(a,b):
    if a and b and a!=b: return f"{a} to {b}"
    if a or b: return f"near {a or b}"
    return ""

# ----------------------------------------------------------------------------- app
app = FastAPI(title="Road Corridor Namer")
PROGRESS={}  # project_id -> {running,phase,done,total,calls}

@app.on_event("startup")
def _startup(): init_db()

def coords_of(seg_row):
    return json.loads(seg_row["geom"])

def reindex_corridors(c, pid):
    """ensure corridor order_index is contiguous (by current order_index then id)."""
    rows=c.execute("SELECT id FROM corridors WHERE project_id=? ORDER BY order_index,id",(pid,)).fetchall()
    for i,r in enumerate(rows):
        c.execute("UPDATE corridors SET order_index=? WHERE id=?",(i,r["id"]))
    c.commit()

@app.post("/api/projects")
async def create_project(file: UploadFile = File(...)):
    try:
        raw=json.loads((await file.read()).decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"could not parse GeoJSON: {e}")
    name=file.filename or "project"
    if not isinstance(raw, dict):
        raise HTTPException(400, "file is not a GeoJSON object")
    feats=[f for f in raw.get("features",[]) if (f.get("geometry") or {}).get("type") in ("LineString","MultiLineString")]
    # expand any MultiLineString into individual LineStrings
    exp=[]
    for f in feats:
        g=f["geometry"]
        if g["type"]=="LineString": exp.append(f)
        else:
            for part in g["coordinates"]:
                exp.append({"type":"Feature","geometry":{"type":"LineString","coordinates":part},"properties":f.get("properties") or {}})
    feats=exp
    if not feats: raise HTTPException(400,"no LineString features found in the file")
    c=db()
    cur=c.execute("INSERT INTO projects(name,created_at) VALUES(?,datetime('now'))",(name,)); pid=cur.lastrowid
    segs=[]
    for i,f in enumerate(feats):
        coords=[[float(x[0]),float(x[1])] for x in f["geometry"]["coordinates"]]
        if len(coords)<2: continue
        segs.append({"id":i,"coords":coords,"props":f.get("properties") or {}})
    chains=build_corridors(segs,25,45)
    byid={s["id"]:s for s in segs}
    seg_db_id={}
    for ci,chain in enumerate(chains):
        cc=c.execute("INSERT INTO corridors(project_id,order_index,name,suggested) VALUES(?,?,?,?)",(pid,ci,"","")); cid=cc.lastrowid
        for k,(sid,flip) in enumerate(chain):
            s=byid[sid]
            r=c.execute("""INSERT INTO segments(project_id,seg_index,geom,props,corridor_id,seq,reversed)
                           VALUES(?,?,?,?,?,?,?)""",
                        (pid,sid,json.dumps(s["coords"]),json.dumps(s["props"]),cid,k,1 if flip else 0))
            seg_db_id[sid]=r.lastrowid
    c.commit(); c.close()
    return {"project_id":pid,"name":name,"segments":len(segs),"corridors":len(chains)}

@app.get("/api/projects")
def list_projects():
    c=db(); rows=c.execute("""SELECT p.*,
        (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id) seg_count,
        (SELECT COUNT(*) FROM corridors k WHERE k.project_id=p.id) corr_count
        FROM projects p ORDER BY p.id DESC""").fetchall()
    out=[dict(r) for r in rows]; c.close(); return out

@app.delete("/api/projects/{pid}")
def delete_project(pid:int):
    c=db(); c.execute("DELETE FROM projects WHERE id=?",(pid,)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/projects/{pid}")
def get_project(pid:int):
    c=db()
    p=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone()
    if not p: raise HTTPException(404,"no such project")
    corrs=[dict(r) for r in c.execute("SELECT * FROM corridors WHERE project_id=? ORDER BY order_index,id",(pid,)).fetchall()]
    segs=[]
    for r in c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id,seq",(pid,)).fetchall():
        coords=json.loads(r["geom"]); mid=point_at(coords,0.5)
        segs.append({"id":r["id"],"seg_index":r["seg_index"],"corridor_id":r["corridor_id"],"seq":r["seq"],
                     "reversed":r["reversed"],"road":r["road"],"suggested":r["suggested"],"name":r["name"],
                     "divided":r["divided"],"coords":coords,"mid":mid})
    c.close()
    return {"project":dict(p),"corridors":corrs,"segments":segs,"progress":PROGRESS.get(pid)}

@app.patch("/api/segments/{sid}")
def patch_segment(sid:int, body:dict=Body(...)):
    c=db(); fields=[]; vals=[]
    for k in ("name","divided"):
        if k in body: fields.append(f"{k}=?"); vals.append(body[k])
    if body.get("accept_suggestion"):
        row=c.execute("SELECT suggested FROM segments WHERE id=?",(sid,)).fetchone()
        if row: fields.append("name=?"); vals.append(row["suggested"])
    if fields:
        vals.append(sid); c.execute(f"UPDATE segments SET {','.join(fields)} WHERE id=?",vals); c.commit()
    c.close(); return {"ok":True}

@app.patch("/api/corridors/{cid}")
def patch_corridor(cid:int, body:dict=Body(...)):
    c=db()
    if "name" in body: c.execute("UPDATE corridors SET name=? WHERE id=?",(body["name"],cid)); c.commit()
    if body.get("accept_suggestion"):
        row=c.execute("SELECT suggested FROM corridors WHERE id=?",(cid,)).fetchone()
        if row: c.execute("UPDATE corridors SET name=? WHERE id=?",(row["suggested"],cid)); c.commit()
    c.close(); return {"ok":True}

@app.post("/api/segments/{sid}/move")
def move_segment(sid:int, body:dict=Body(...)):
    """move a segment to corridor_id (int) or 'new' (its own new corridor)."""
    c=db()
    seg=c.execute("SELECT * FROM segments WHERE id=?",(sid,)).fetchone()
    if not seg: raise HTTPException(404,"no such segment")
    pid=seg["project_id"]; target=body.get("corridor_id")
    if target=="new" or target is None:
        mo=c.execute("SELECT COALESCE(MAX(order_index),-1)+1 m FROM corridors WHERE project_id=?",(pid,)).fetchone()["m"]
        cc=c.execute("INSERT INTO corridors(project_id,order_index) VALUES(?,?)",(pid,mo)); target=cc.lastrowid
    nxt=c.execute("SELECT COALESCE(MAX(seq),-1)+1 m FROM segments WHERE corridor_id=?",(target,)).fetchone()["m"]
    c.execute("UPDATE segments SET corridor_id=?,seq=? WHERE id=?",(target,nxt,sid))
    c.commit()
    # drop empty corridors
    c.execute("DELETE FROM corridors WHERE project_id=? AND id NOT IN (SELECT DISTINCT corridor_id FROM segments WHERE project_id=?)",(pid,pid))
    reindex_corridors(c,pid); c.close(); return {"ok":True,"corridor_id":target}

@app.post("/api/corridors/{cid}/split")
def split_corridor(cid:int, body:dict=Body(...)):
    """create a new corridor containing all segments with seq > after_seq."""
    after=body.get("after_seq",0); c=db()
    cor=c.execute("SELECT * FROM corridors WHERE id=?",(cid,)).fetchone()
    if not cor: raise HTTPException(404)
    pid=cor["project_id"]
    mo=c.execute("SELECT COALESCE(MAX(order_index),-1)+1 m FROM corridors WHERE project_id=?",(pid,)).fetchone()["m"]
    nc=c.execute("INSERT INTO corridors(project_id,order_index) VALUES(?,?)",(pid,mo)).lastrowid
    movers=c.execute("SELECT id FROM segments WHERE corridor_id=? AND seq>? ORDER BY seq",(cid,after)).fetchall()
    for k,r in enumerate(movers): c.execute("UPDATE segments SET corridor_id=?,seq=? WHERE id=?",(nc,k,r["id"]))
    c.commit(); reindex_corridors(c,pid); c.close(); return {"ok":True,"new_corridor":nc,"moved":len(movers)}

@app.post("/api/corridors/{cid}/merge")
def merge_corridor(cid:int, body:dict=Body(...)):
    """append target corridor's segments onto cid, then delete target."""
    tid=body.get("target_id"); c=db()
    a=c.execute("SELECT * FROM corridors WHERE id=?",(cid,)).fetchone()
    b=c.execute("SELECT * FROM corridors WHERE id=?",(tid,)).fetchone()
    if not a or not b: raise HTTPException(404)
    nxt=c.execute("SELECT COALESCE(MAX(seq),-1)+1 m FROM segments WHERE corridor_id=?",(cid,)).fetchone()["m"]
    for k,r in enumerate(c.execute("SELECT id FROM segments WHERE corridor_id=? ORDER BY seq",(tid,)).fetchall()):
        c.execute("UPDATE segments SET corridor_id=?,seq=? WHERE id=?",(cid,nxt+k,r["id"]))
    c.execute("DELETE FROM corridors WHERE id=?",(tid,)); c.commit()
    reindex_corridors(c,a["project_id"]); c.close(); return {"ok":True}

@app.post("/api/corridors/{cid}/reorder")
def reorder_segments(cid:int, body:dict=Body(...)):
    """body {order:[seg_id,...]} sets seq to match the given order."""
    order=body.get("order",[]); c=db()
    for k,sid in enumerate(order): c.execute("UPDATE segments SET seq=? WHERE id=? AND corridor_id=?",(k,sid,cid))
    c.commit(); c.close(); return {"ok":True}

# ---- enrichment (background, cached) ----
def _enrich(pid, key, mode, offline):
    c=db(); cache=Cache(c,offline)
    prog=PROGRESS[pid]
    try:
        segs=c.execute("SELECT * FROM segments WHERE project_id=? ORDER BY corridor_id,seq",(pid,)).fetchall()
        prog["total"]=len(segs); prog["phase"]="segments"
        # node POI cache within this run (also persisted via gcache)
        for i,s in enumerate(segs):
            coords=json.loads(s["geom"]);
            if s["reversed"]: coords=coords[::-1]
            a=coords[0]; b=coords[-1]
            pa=poi_at(a[1],a[0],key,cache); pb=poi_at(b[1],b[0],key,cache)
            seg_name=join_poi(pa,pb)
            if mode=="roads":
                # nearestRoads snap of midpoint -> placeId -> details
                road=_road_via_roads(point_at(coords,0.5),key,cache) or route_at(*point_at(coords,0.5)[::-1],key=key,cache=cache)
            else:
                mid=point_at(coords,0.5); road=route_at(mid[1],mid[0],key,cache)
            c.execute("UPDATE segments SET road=?,suggested=?, name=CASE WHEN name='' THEN ? ELSE name END WHERE id=?",
                      (road,seg_name,seg_name,s["id"]))
            c.commit(); prog["done"]=i+1
        # corridor suggestions
        prog["phase"]="corridors"
        from collections import Counter
        for cor in c.execute("SELECT * FROM corridors WHERE project_id=?",(pid,)).fetchall():
            rows=c.execute("SELECT * FROM segments WHERE corridor_id=? ORDER BY seq",(cor["id"],)).fetchall()
            if not rows: continue
            roads=[r["road"] for r in rows if r["road"]]
            road=Counter(roads).most_common(1)[0][0] if roads else ""
            f0=json.loads(rows[0]["geom"]);  f0=f0[::-1] if rows[0]["reversed"] else f0
            fl=json.loads(rows[-1]["geom"]); fl=fl[::-1] if rows[-1]["reversed"] else fl
            span=join_poi(poi_at(f0[0][1],f0[0][0],key,cache), poi_at(fl[-1][1],fl[-1][0],key,cache))
            sug=(f"{road}: {span}" if (road and span) else (road or span or ""))
            c.execute("UPDATE corridors SET suggested=?, name=CASE WHEN name='' THEN ? ELSE name END WHERE id=?",
                      (sug,sug,cor["id"])); c.commit()
        c.execute("UPDATE projects SET enriched=1 WHERE id=?",(pid,)); c.commit()
    finally:
        prog["running"]=False; prog["phase"]="done"; c.close()

def _road_via_roads(lnglat,key,cache):
    """snap a [lng,lat] to a road placeId then resolve its route name (cached)."""
    lat,lng=lnglat[1],lnglat[0]
    k=f"rd:{lat:.6f},{lng:.6f}"
    cv=cache.get(k)
    if cv is not None: return cv
    if cache.offline: return ""
    js=_get(ROADS,{"key":key,"points":f"{lat},{lng}"})
    pid=None
    for sp in js.get("snappedPoints",[]): pid=sp.get("placeId"); break
    name=""
    if pid:
        pk="pd:"+pid; pv=cache.get(pk)
        if pv is None:
            dj=_get(DETAILS,{"key":key,"place_id":pid,"fields":"name,types"})
            pv=normalise((dj.get("result") or {}).get("name","")); cache.put(pk,pv)
        name=pv
    cache.put(k,name); return name

@app.post("/api/projects/{pid}/enrich")
def enrich(pid:int, body:dict=Body(...)):
    if PROGRESS.get(pid,{}).get("running"): raise HTTPException(409,"already running")
    key=body.get("key",""); mode=body.get("mode","geocode"); offline=bool(body.get("offline"))
    if not offline and not key: raise HTTPException(400,"api key required unless offline")
    PROGRESS[pid]={"running":True,"phase":"starting","done":0,"total":0}
    threading.Thread(target=_enrich,args=(pid,key,mode,offline),daemon=True).start()
    return {"started":True}

@app.get("/api/projects/{pid}/enrich/status")
def enrich_status(pid:int): return PROGRESS.get(pid,{"running":False,"phase":"idle"})

@app.get("/api/projects/{pid}/export")
def export(pid:int):
    c=db(); rows=c.execute("""SELECT s.*, k.name corr_name, k.order_index corr_oi
        FROM segments s JOIN corridors k ON s.corridor_id=k.id WHERE s.project_id=? ORDER BY k.order_index,s.seq""",(pid,)).fetchall()
    feats=[]
    for r in rows:
        props=json.loads(r["props"]); props.update({
            "name":r["name"] or r["suggested"], "road_name":r["road"],
            "corridor_name":r["corr_name"], "corridor_id":r["corr_oi"]+1, "seq_in_corridor":r["seq"],
            "reversed_for_walk":bool(r["reversed"]), "divided":r["divided"]})
        feats.append({"type":"Feature","geometry":{"type":"LineString","coordinates":json.loads(r["geom"])},"properties":props})
    c.close()
    buf=io.BytesIO(json.dumps({"type":"FeatureCollection","features":feats},indent=2).encode())
    return StreamingResponse(buf,media_type="application/geo+json",
        headers={"Content-Disposition":f'attachment; filename="project_{pid}_named.geojson"'})

# static frontend
app.mount("/", StaticFiles(directory=os.path.join(HERE,"static"), html=True), name="static")

if __name__=="__main__":
    import uvicorn
    print("Road Corridor Namer  ->  http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
