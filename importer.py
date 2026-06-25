import re

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

def build_workset(features, require_synced=True):
    """Build corridors + standalone from leaf routes.

    require_synced=True (default): keep only synced leaves; a parent forms a corridor
      only if ALL its children are synced, otherwise its synced children become standalone.
    require_synced=False: keep every leaf regardless of sync_status; every
      parent_route_id group forms a corridor; parentless leaves become standalone.
      Used to add unsynced routes (e.g. outside-jurisdiction roads).
    """
    feats = [f for f in features
             if (f.get("geometry") or {}).get("type") == "LineString"
             and len(f["geometry"]["coordinates"]) >= 2]
    by_props = [(f, f.get("properties") or {}) for f in feats]

    def keep(p):
        if p.get("has_children") != 0:
            return False
        return p.get("sync_status") == "synced" if require_synced else True

    # index ALL children (any sync) by parent
    children = {}
    for f, p in by_props:
        if p.get("has_children") == 0 and p.get("parent_route_id"):
            children.setdefault(p["parent_route_id"], []).append((f, p))

    if require_synced:
        # a parent is a corridor only if every one of its children is synced
        intact_parents = {par for par, kids in children.items()
                          if all(k[1].get("sync_status") == "synced" for k in kids)}
    else:
        # sync-agnostic: every parent group forms a corridor
        intact_parents = set(children.keys())

    corridors, standalone = [], []
    code = 0
    # corridors, ordered by segment_order
    for par in children:
        if par not in intact_parents:
            continue
        kids = sorted([(f, p) for f, p in children[par] if keep(p)],
                      key=lambda k: k[1].get("segment_order") or 0)
        if not kids:
            continue
        code += 1
        corridors.append({"cor_code": f"cor_{code:03d}",
                          "segments": [_seg(f) for f, _ in kids]})

    # standalone = kept leaf that is parentless OR child of a non-corridor parent
    for f, p in by_props:
        if not keep(p):
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
