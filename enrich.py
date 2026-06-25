import json, time, requests, math, os, sys
from collections import Counter
from importer import normalise
import db as _db

GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
ROADS = "https://roads.googleapis.com/v1/nearestRoads"

class Cache:
    def __init__(self, conn, offline):
        self.c = conn; self.offline = offline
    def get(self, k):
        r = self.c.execute("SELECT v FROM gcache WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else None
    def put(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO gcache(k,v) VALUES(?,?)", (k, json.dumps(v)))
        self.c.commit()

def _http(url, params):
    for t in range(3):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(0.8 * (t + 1))
    return {}

def _route_from_geocode(js):
    for r in js.get("results", []):
        for comp in r.get("address_components", []):
            if "route" in comp.get("types", []):
                return comp["long_name"]
    return ""

def road_geocode(lat, lng, key, cache):
    k = f"gc:{lat:.5f},{lng:.5f}"
    v = cache.get(k)
    if v is not None:
        return v
    if cache.offline:
        return ""
    js = _http(GEOCODE, {"key": key, "latlng": f"{lat},{lng}", "result_type": "route"})
    name = normalise(_route_from_geocode(js)); cache.put(k, name); return name

def road_via_roads(lng, lat, key, cache):
    k = f"rd:{lat:.6f},{lng:.6f}"
    v = cache.get(k)
    if v is not None:
        return v
    if cache.offline:
        return ""
    js = _http(ROADS, {"key": key, "points": f"{lat},{lng}"})
    pts = js.get("snappedPoints", [])
    if not pts:
        cache.put(k, ""); return ""
    loc = pts[0]["location"]
    name = road_geocode(loc["latitude"], loc["longitude"], key, cache)
    cache.put(k, name); return name

def _mid(coords):
    # reuse app's haversine midpoint via simple param: midpoint by length
    R = 6371000.0
    def hav(a, b):
        dLat = math.radians(b[1]-a[1]); dLng = math.radians(a[0]-b[0])
        h = math.sin(dLat/2)**2 + math.cos(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.sin(dLng/2)**2
        return 2*R*math.asin(min(1, math.sqrt(h)))
    tot = sum(hav(coords[i-1], coords[i]) for i in range(1, len(coords))) / 2
    acc = 0
    for i in range(1, len(coords)):
        d = hav(coords[i-1], coords[i])
        if acc + d >= tot:
            t = (tot-acc)/d if d else 0
            return [coords[i-1][0]+(coords[i][0]-coords[i-1][0])*t,
                    coords[i-1][1]+(coords[i][1]-coords[i-1][1])*t]
        acc += d
    return coords[-1]

def run(project_id, key, db_path, offline=False):
    conn = _db.connect(db_path)
    cache = Cache(conn, offline)
    segs = conn.execute("SELECT * FROM segments WHERE project_id=?", (project_id,)).fetchall()
    for s in segs:
        coords = json.loads(s["geom"]); m = _mid(coords)
        g = road_geocode(m[1], m[0], key, cache)
        r = road_via_roads(m[0], m[1], key, cache)
        conn.execute("UPDATE segments SET sug_geocode=?,sug_roads=? WHERE id=?", (g, r, s["id"]))
    conn.commit()
    for cor in conn.execute("SELECT * FROM corridors WHERE project_id=?", (project_id,)).fetchall():
        roads = [r["sug_geocode"] or r["sug_roads"]
                 for r in conn.execute("SELECT sug_geocode,sug_roads FROM segments WHERE corridor_id=?", (cor["id"],))
                 if (r["sug_geocode"] or r["sug_roads"])]
        sug = Counter(roads).most_common(1)[0][0] if roads else ""
        conn.execute("UPDATE corridors SET suggested=? WHERE id=?", (sug, cor["id"]))
    conn.execute("UPDATE projects SET enriched=1 WHERE id=?", (project_id,))
    conn.commit(); conn.close()
    return {"leaves": len(segs), "calls": len(segs) * 2}

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()  # read GOOGLE_ENRICH_KEY from root .env if present
    offline = "--offline" in sys.argv
    pid = int([a for a in sys.argv[1:] if not a.startswith("--")][0])
    key = os.environ.get("GOOGLE_ENRICH_KEY", "")
    path = os.environ.get("ROADNAMER_DB", "roadnamer.db")
    if not offline and not key:
        sys.exit("set GOOGLE_ENRICH_KEY (or pass --offline)")
    print(run(pid, key, path, offline))
