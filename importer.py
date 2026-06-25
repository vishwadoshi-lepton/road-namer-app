import math, re

_HOUSE = re.compile(r'^\s*[A-Za-z0-9][A-Za-z0-9/\-]*\d[A-Za-z0-9/\-]*\s*,\s*')
_REV = re.compile(r'\s*\((Reversed|Reverse)\)\s*', re.I)

def normalise(name):
    if not name:
        return ""
    n = name.strip(); prev = None
    while prev != n:
        prev = n; n = _HOUSE.sub('', n).strip()
    for p, r in [(r'\bRd\b', 'Road'), (r'\bSt\b', 'Street'),
                 (r'\bAve\b', 'Avenue'), (r'\bHwy\b', 'Highway')]:
        n = re.sub(p, r, n)
    return n.strip(' ,')

def _coords(f):
    return [[float(x[0]), float(x[1])] for x in f["geometry"]["coordinates"]]

def _seg(f):
    p = f.get("properties") or {}
    return {"uuid": p.get("uuid"), "coords": _coords(f), "props": p,
            "route_name": p.get("route_name") or "",
            "parent_route_id": p.get("parent_route_id"),
            "segment_order": p.get("segment_order")}

def build_workset(features):
    """Apply the synced-leaf filter + all-or-nothing corridor rule."""
    feats = [f for f in features
             if (f.get("geometry") or {}).get("type") == "LineString"
             and len(f["geometry"]["coordinates"]) >= 2]
    by_props = [(f, f.get("properties") or {}) for f in feats]

    # index ALL children (any sync) by parent to evaluate intactness
    children = {}
    for f, p in by_props:
        if p.get("has_children") == 0 and p.get("parent_route_id"):
            children.setdefault(p["parent_route_id"], []).append((f, p))

    intact_parents = {par for par, kids in children.items()
                      if all(k[1].get("sync_status") == "synced" for k in kids)}

    corridors, standalone = [], []
    code = 0
    # intact corridors, ordered by segment_order
    for par in children:
        if par not in intact_parents:
            continue
        kids = sorted(children[par], key=lambda k: k[1].get("segment_order") or 0)
        code += 1
        corridors.append({"cor_code": f"cor_{code:03d}",
                          "segments": [_seg(f) for f, _ in kids]})

    # standalone = synced leaf that is parentless OR child of a broken parent
    for f, p in by_props:
        if p.get("has_children") != 0 or p.get("sync_status") != "synced":
            continue
        par = p.get("parent_route_id")
        if par and par in intact_parents:
            continue  # already placed in its corridor
        standalone.append(_seg(f))

    return {"corridors": corridors, "standalone": standalone}

def _near(a, b, tol=1e-4):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol

def detect_twins(segs):
    """Link uuids whose base-name matches AND geometry is a start<->end reversal."""
    base = lambda n: _REV.sub('', n or '').strip()
    groups = {}
    for s in segs:
        groups.setdefault(base(s["route_name"]), []).append(s)
    out = {}
    for grp in groups.values():
        if len(grp) < 2:
            continue
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                a, b = grp[i], grp[j]
                if _near(a["coords"][0], b["coords"][-1]) and \
                   _near(a["coords"][-1], b["coords"][0]):
                    out[a["uuid"]] = b["uuid"]; out[b["uuid"]] = a["uuid"]
    return out
