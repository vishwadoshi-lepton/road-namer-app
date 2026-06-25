import json, time, requests
from importer import normalise

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
