import json

def build_export(project, corridors, segments):
    cor_by_id = {c["id"]: c for c in corridors}
    cor_segs = {c["id"]: [] for c in corridors}
    features = []
    for s in segments:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": json.loads(s["geom"])},
            "properties": {**json.loads(s["props"] or "{}"),
                           "uuid": s["uuid"], "name": s["name"]},
        })
        if s["corridor_id"] in cor_segs:
            cor_segs[s["corridor_id"]].append(s["uuid"])
    corridors_out = [{"id": cor_by_id[cid]["cor_code"],
                      "name": cor_by_id[cid]["name"],
                      "segment_uuids": uuids}
                     for cid, uuids in cor_segs.items()]
    return {"leaves": {"type": "FeatureCollection", "features": features},
            "corridors": corridors_out}
