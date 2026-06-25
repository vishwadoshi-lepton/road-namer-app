import json, os
import importer

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.geojson")

def load():
    return json.load(open(FIX))["features"]

def test_intact_corridor_grouped_in_order():
    w = importer.build_workset(load())
    cors = w["corridors"]
    assert len(cors) == 1
    seg_uuids = [s["uuid"] for s in cors[0]["segments"]]
    assert seg_uuids == ["P1-S1", "P1-S2"]
    assert cors[0]["cor_code"] == "cor_001"

def test_broken_parent_child_becomes_standalone():
    w = importer.build_workset(load())
    sa = {s["uuid"] for s in w["standalone"]}
    assert "P2-S1" in sa          # surviving synced child of broken parent
    assert "P2-S2" not in sa      # unsynced child dropped entirely

def test_unsynced_leaf_dropped():
    w = importer.build_workset(load())
    all_uuids = {s["uuid"] for s in w["standalone"]} | {
        s["uuid"] for c in w["corridors"] for s in c["segments"]}
    assert "DROP" not in all_uuids
    assert "P1" not in all_uuids  # parent container row never in workset

def test_genuine_standalone_present():
    w = importer.build_workset(load())
    assert "SA1" in {s["uuid"] for s in w["standalone"]}

def test_twin_detection_links_both_ways():
    w = importer.build_workset(load())
    segs = w["standalone"] + [s for c in w["corridors"] for s in c["segments"]]
    twins = importer.detect_twins(segs)
    assert twins.get("TW") == "TWR"
    assert twins.get("TWR") == "TW"
    assert "SA1" not in twins

def test_normalise_strips_and_expands():
    assert importer.normalise("13-55, 132 Feet Ring Rd") == "132 Feet Ring Road"

def test_require_synced_false_includes_unsynced():
    w = importer.build_workset(load(), require_synced=False)
    cors = w["corridors"]
    seg_uuids = {s["uuid"] for c in cors for s in c["segments"]} | {s["uuid"] for s in w["standalone"]}
    # every leaf kept, including the ones dropped in synced mode
    assert "DROP" in seg_uuids          # unsynced standalone — dropped in synced mode
    assert "P2-S2" in seg_uuids         # broken-parent's unsynced child — kept now
    assert len(seg_uuids) == 8
    # P2 now forms a full corridor (both children), not standalone
    assert len(cors) == 2
    p2 = [c for c in cors if {s["uuid"] for s in c["segments"]} == {"P2-S1", "P2-S2"}]
    assert len(p2) == 1
