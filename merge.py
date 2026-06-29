"""Pure chain-ordering and geometry helpers for merging route segments.

A merge takes >=2 segments that form ONE continuous directed chain in their
stored directions (no reversing) and joins them into a single LineString.
"""
from importer import _near


class MergeError(Exception):
    """Raised when the selected segments cannot form one directed chain."""


def order_chain(segments, tol=1e-4):
    """Return `segments` ordered head-to-tail, or raise MergeError.

    Each segment is a dict with "coords" = [[lng,lat], ...]. Direction is fixed:
    segment i's end must be ~equal to segment i+1's start (within tol).
    """
    if len(segments) < 2:
        raise MergeError("Select at least two segments to merge.")

    def start(s): return s["coords"][0]
    def end(s):   return s["coords"][-1]

    # A start segment's start point is not the end of any other segment.
    starts = [s for s in segments
              if not any(o is not s and _near(start(s), end(o), tol) for o in segments)]
    if len(starts) == 0:
        if len(segments) == 2 and _near(start(segments[0]), end(segments[1]), tol) \
                and _near(end(segments[0]), start(segments[1]), tol):
            raise MergeError("Two selected segments run in opposite directions.")
        raise MergeError("Selected segments form a loop.")
    if len(starts) > 1:
        raise MergeError("Segments don't connect end to end.")

    ordered = [starts[0]]
    used = {id(starts[0])}
    cur = starts[0]
    while len(ordered) < len(segments):
        nxts = [s for s in segments if id(s) not in used and _near(end(cur), start(s), tol)]
        if len(nxts) == 0:
            raise MergeError("Segments don't connect end to end.")
        if len(nxts) > 1:
            raise MergeError("Selected segments branch; pick a single path.")
        cur = nxts[0]
        ordered.append(cur)
        used.add(id(cur))
    return ordered


def merge_coords(ordered):
    """Concatenate ordered segments, dropping each later segment's first point
    (the shared junction) so there are no duplicate vertices."""
    coords = list(ordered[0]["coords"])
    for s in ordered[1:]:
        coords.extend(s["coords"][1:])
    return coords
