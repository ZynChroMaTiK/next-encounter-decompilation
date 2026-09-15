#!/usr/bin/env python3
"""
Rooms for the zoning world base: sectors that are closed volumes made of the
level's own polygons, uncut, joined by portal polygons (docs/world-conversion.md,
"Rooms").

NE kept no sectors and no portals, and its static mesh is open (no ceilings,
no wall tops, no sky), so the designers' rooms cannot be read back. They are
built from the polygons.

**Cells.** A BSP of the level's box along the planes of the world base's own
polygons (the most coplanar area first, less for each polygon a plane passes
through) until every polygon lies on the faces of the cells in front of it.
The cells are only scaffolding: no polygon is clipped for the world. Each cell
knows the parts of the polygons that lie on it.

**Rooms are unions of cells.**

  - A polygon is never cut: the cells in front of one polygon are one room,
    so each polygon is written whole.
  - Groups of cells join where the opening between them, the part of their
    shared faces no polygon covers, is a large part (ROOM_OPENING) of the
    smaller group's whole boundary. A roof's plane cuts the cells above a
    courtyard from those below, but the courtyard's whole top is open, so it
    stays one room; a house joins its yard only through a door, small beside
    its walls, floor and ceiling, so it stays a room of its own. Openness is
    judged between whole groups, never one cell face: the cell inside a
    doorway is all opening.
  - A room of SMALL_ROOM_POLYGONS or fewer (a niche, a pocket around an entity)
    joins the neighbour it is most open to, or failing that the one it shares
    most face with. SE1's sectors need not be convex.
  - A cell that holds nothing at all (no polygon, and no separate object,
    brush, prop, light or other entity) is void outside the level and belongs
    to no room.

**Closing a room.** Its boundary is the outside of its cells. Where that
boundary faces another room, the parts not covered by the room's own polygons
on that plane (facing into it) become a portal facing into the room; SE1
renders with span occlusion, so a portal must not lie over one of the room's
walls. Toward void and on the level's box the uncovered parts are closed with
invisible polygons.

Every sector's vertices closer than 0.001 are welded when written
(tools/wldprep.py): NE's neighbouring polygons disagree by ~1e-6, portals are
cut along them, and SE1's triangulator fails on the zero-length edges the
near-duplicates leave.

    python tools/rooms.py render Rlevel1_1              # build/render/<level>_rooms.png
    python tools/rooms.py render Rlevel1_1 --below 20   # ... everything above 20 left out
"""

from __future__ import annotations

import argparse
import heapq
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

EPS = 0.02                # a vertex this close to a plane is on it (tools/polygons.py)
GEOM_EPS = 1e-6           # the same, for the cells' own convex faces
MARGIN = 8.0              # the level box around the geometry
CANDIDATES = 32           # planes tried per cell, largest coplanar area first
CUT_WEIGHT = 1.0          # a split plane's polygon area counts less for each polygon it passes through
MIN_DEPTH = 0.05          # a plane divides a cell if the cell reaches this far past it on both sides
ROOM_OPENING = 0.15       # groups join where their opening is this much of the smaller one's boundary
SMALL_ROOM_POLYGONS = 12  # a room with this few polygons joins a neighbour
MIN_PIECE_AREA = 1e-4
MIN_BOUNDARY_AREA = 0.1   # a portal or closing polygon smaller than this is left out


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _newell(pts):
    x = y = z = 0.0
    for i in range(len(pts)):
        p, q = pts[i - 1], pts[i]
        x += (p[1] - q[1]) * (p[2] + q[2])
        y += (p[2] - q[2]) * (p[0] + q[0])
        z += (p[0] - q[0]) * (p[1] + q[1])
    return (x, y, z)


def area(pts):
    n = _newell(pts)
    return math.sqrt(_dot(n, n)) / 2


def _unit(v):
    ln = math.sqrt(_dot(v, v))
    return None if ln < 1e-12 else (v[0] / ln, v[1] / ln, v[2] / ln)


def _cut(p, q, dp, dq):
    """The point where segment p-q crosses the plane, computed the same way
    from either end so that both sides of a cut share it exactly."""
    if p > q:
        p, q, dp, dq = q, p, dq, dp
    t = dp / (dp - dq)
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t)


def clip(pts, n, d, front, eps):
    """Sutherland-Hodgman: the part of a loop in front of (or behind) the
    plane n.p = d. Vertices within eps of the plane stay where they are."""
    out = []
    k = len(pts)
    if k == 0:
        return out
    ds = [_dot(n, p) - d for p in pts]
    if not front:
        ds = [-x for x in ds]
    for i in range(k):
        p, q = pts[i], pts[(i + 1) % k]
        dp, dq = ds[i], ds[(i + 1) % k]
        if dp >= -eps:
            out.append(p)
        if (dp > eps and dq < -eps) or (dp < -eps and dq > eps):
            out.append(_cut(p, q, dp if front else -dp, dq if front else -dq))
    res = []
    for p in out:
        if not res or max(abs(p[j] - res[-1][j]) for j in range(3)) > 1e-9:
            res.append(p)
    if len(res) > 1 and max(abs(res[0][j] - res[-1][j]) for j in range(3)) <= 1e-9:
        res.pop()
    return res


class Poly:
    """A level polygon (or a part of one lying in one cell): its loops, the
    convex pieces covering it, and its plane."""
    __slots__ = ("src", "loops", "pieces", "n", "d", "area", "centre", "radius")

    def __init__(self, src, loops, pieces):
        self.src, self.loops, self.pieces = src, loops, pieces
        nv = _newell(loops[0])
        for h in loops[1:]:
            hn = _newell(h)
            nv = (nv[0] + hn[0], nv[1] + hn[1], nv[2] + hn[2])
        ln = math.sqrt(_dot(nv, nv))
        self.area = ln / 2
        self.n = (0.0, 1.0, 0.0) if ln < 1e-12 else (nv[0] / ln, nv[1] / ln, nv[2] / ln)
        self.d = _dot(self.n, loops[0][0])
        ps = loops[0]
        c = tuple(sum(p[j] for p in ps) / len(ps) for j in range(3))
        self.centre = c
        self.radius = max(math.sqrt(_dot(_sub(p, c), _sub(p, c))) for p in ps)

    def convex(self):
        return self.pieces if self.pieces else [self.loops[0]]

    def side(self, n, d):
        """1 front, -1 back, 0 on the plane, 2 across it."""
        c = _dot(n, self.centre) - d
        if c > self.radius + EPS:
            return 1
        if c < -self.radius - EPS:
            return -1
        lo = hi = 0.0
        for loop in self.loops:
            for p in loop:
                x = _dot(n, p) - d
                lo, hi = min(lo, x), max(hi, x)
        if hi <= EPS and lo >= -EPS:
            return 0
        if lo >= -EPS:
            return 1
        if hi <= EPS:
            return -1
        return 2

    def split(self, n, d):
        """-> (front part, back part), either None."""
        out = []
        for front in (True, False):
            loops = [clip(x, n, d, front, EPS) for x in self.loops]
            if len(loops[0]) < 3 or area(loops[0]) < MIN_PIECE_AREA:
                out.append(None)
                continue
            loops = [loops[0]] + [x for x in loops[1:] if len(x) >= 3 and area(x) >= MIN_PIECE_AREA]
            pieces = [clip(x, n, d, front, EPS) for x in self.pieces]
            pieces = [x for x in pieces if len(x) >= 3 and area(x) >= MIN_PIECE_AREA]
            out.append(Poly(self.src, loops, pieces))
        return out[0], out[1]


# --- convex cells -------------------------------------------------------------

def _order(pts, n):
    """Points on a plane, in counter-clockwise order seen along n."""
    c = tuple(sum(p[j] for p in pts) / len(pts) for j in range(3))
    ref = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _unit(_cross(n, ref))
    w = _cross(n, u)
    return sorted(pts, key=lambda p: math.atan2(_dot(_sub(p, c), w), _dot(_sub(p, c), u)))


def _dedupe(pts, tol=1e-7):
    out = []
    for p in pts:
        if all(max(abs(p[j] - q[j]) for j in range(3)) > tol for q in out):
            out.append(p)
    return out


def section(faces, n, d):
    """A convex cell (its faces) cut by a plane -> the cut polygon, wound
    counter-clockwise seen along n, or None."""
    pts = []
    for f in faces:
        ps = f["pts"]
        ds = [_dot(n, p) - d for p in ps]
        for i in range(len(ps)):
            p, q, dp, dq = ps[i], ps[(i + 1) % len(ps)], ds[i], ds[(i + 1) % len(ps)]
            if abs(dp) <= GEOM_EPS:
                pts.append(p)
            elif (dp > GEOM_EPS and dq < -GEOM_EPS) or (dp < -GEOM_EPS and dq > GEOM_EPS):
                pts.append(_cut(p, q, dp, dq))
    pts = _dedupe(pts)
    if len(pts) < 3:
        return None
    pts = _order(pts, n)
    return pts if area(pts) > MIN_PIECE_AREA else None


def split_region(faces, n, d, tag_front, tag_back):
    front, back = [], []
    for f in faces:
        a = clip(f["pts"], n, d, True, GEOM_EPS)
        b = clip(f["pts"], n, d, False, GEOM_EPS)
        if len(a) >= 3 and area(a) > MIN_PIECE_AREA:
            front.append(dict(f, pts=a))
        if len(b) >= 3 and area(b) > MIN_PIECE_AREA:
            back.append(dict(f, pts=b))
    cap = section(faces, n, d)
    if cap is not None:
        # a cell's face is wound to face into the cell
        front.append({"pts": cap, "inward": n, "tag": tag_front})
        back.append({"pts": list(reversed(cap)), "inward": (-n[0], -n[1], -n[2]), "tag": tag_back})
    return front, back


def box_faces(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [((0, 3, 2, 1), (0, 0, 1)), ((4, 5, 6, 7), (0, 0, -1)),
             ((0, 4, 7, 3), (1, 0, 0)), ((1, 2, 6, 5), (-1, 0, 0)),
             ((0, 1, 5, 4), (0, 1, 0)), ((3, 7, 6, 2), (0, -1, 0))]
    out = []
    for idx, inward in quads:
        pts = [c[i] for i in idx]
        if _dot(_newell(pts), inward) < 0:
            pts.reverse()
        out.append({"pts": pts, "inward": tuple(float(x) for x in inward), "tag": ("box",)})
    return out


def _candidates(polys):
    """Planes carrying the cell's polygons, largest coplanar area first:
    [(n, d, polygons on it)]."""
    buckets = defaultdict(list)
    for p in polys:
        n, d = p.n, p.d
        s = 1.0                                   # one bucket for both facings
        for x in n:
            if abs(x) > 1e-6:
                s = 1.0 if x > 0 else -1.0
                break
        key = tuple(round(s * x, 3) for x in n)
        buckets[key].append((s * d, p, s))
    planes = []
    for key, items in buckets.items():
        items.sort(key=lambda t: t[0])
        group = [items[0]]
        for it in items[1:]:
            if it[0] - group[-1][0] <= EPS:
                group.append(it)
            else:
                planes.append(group)
                group = [it]
        planes.append(group)
    out = []
    for group in planes:
        a = sum(p.area for _d, p, _s in group)
        big = max(group, key=lambda t: t[1].area)
        s = big[2]
        n = tuple(s * x for x in big[1].n)
        out.append((a, n, s * big[1].d, [p for _d, p, _s in group]))
    out.sort(key=lambda t: -t[0])
    return [(n, d, ps) for _a, n, d, ps in out[:CANDIDATES]]


def _depths(faces, n, d):
    lo = hi = 0.0
    for f in faces:
        for p in f["pts"]:
            x = _dot(n, p) - d
            lo, hi = min(lo, x), max(hi, x)
    return -lo, hi


def _choose(faces, polys, stats):
    """The split plane for a cell: of the planes of its polygons that still
    divide it, the one with the most polygon area, less for every polygon it
    passes through. None once every polygon lies on the cell's faces."""
    best = None
    for n, d, on in _candidates(polys):
        back_depth, front_depth = _depths(faces, n, d)
        if back_depth < MIN_DEPTH or front_depth < MIN_DEPTH:
            continue
        a = sum(p.area for p in on)
        cuts = sum(1 for p in polys if p.side(n, d) == 2)
        score = a / (1.0 + CUT_WEIGHT * cuts)
        if best is None or score > best[0]:
            best = (score, n, d)
    if best is None:
        return None
    stats["splits"] += 1
    return best[1], best[2]


def cells(items, stats=None):
    """items: [(loops, pieces)] of points -> (cells, tree). A cell is a convex
    region {"polys": [Poly], "faces": [face], "id"}; a face is {"pts",
    "inward", "tag"} with tag ("box",) or (node, side) for the split plane it
    lies on."""
    stats = defaultdict(int) if stats is None else stats
    polys = [Poly(i, loops, pieces) for i, (loops, pieces) in enumerate(items)]
    pts = [p for P in polys for p in P.loops[0]]
    lo = tuple(min(p[j] for p in pts) - MARGIN for j in range(3))
    hi = tuple(max(p[j] for p in pts) + MARGIN for j in range(3))
    out = []

    def build(faces, polys):
        plane = _choose(faces, polys, stats) if polys else None
        if plane is None:
            out.append({"polys": polys, "faces": faces, "id": len(out)})
            return ("leaf", len(out) - 1)
        n, d = plane
        node = {"n": n, "d": d}
        ff, bf = split_region(faces, n, d, (node, 1), (node, -1))
        fp, bp = [], []
        for P in polys:
            s = P.side(n, d)
            if s == 1:
                fp.append(P)
            elif s == -1:
                bp.append(P)
            elif s == 0:
                (fp if _dot(P.n, n) > 0 else bp).append(P)
            else:
                a, b = P.split(n, d)
                stats["polygons across a cell boundary"] += 1
                if a is not None:
                    fp.append(a)
                if b is not None:
                    bp.append(b)
        node["front"] = build(ff, fp)
        node["back"] = build(bf, bp)
        return ("node", node)

    root = build(box_faces(lo, hi), polys)
    stats["cells"] = len(out)
    return out, root


def _descend(tree, pts, out):
    """A convex polygon on a split plane, pushed down the other side's subtree
    -> [(cell id, piece)]."""
    if tree[0] == "leaf":
        out.append((tree[1], pts))
        return
    node = tree[1]
    n, d = node["n"], node["d"]
    ds = [_dot(n, p) - d for p in pts]
    if max(ds) <= GEOM_EPS * 10 and min(ds) >= -GEOM_EPS * 10:
        _descend(node["front"], pts, out)
        return
    a = clip(pts, n, d, True, GEOM_EPS)
    b = clip(pts, n, d, False, GEOM_EPS)
    if len(a) >= 3 and area(a) > MIN_PIECE_AREA:
        _descend(node["front"], a, out)
    if len(b) >= 3 and area(b) > MIN_PIECE_AREA:
        _descend(node["back"], b, out)


def locate(tree, p):
    """The cell a point is in."""
    while tree[0] != "leaf":
        node = tree[1]
        tree = node["front"] if _dot(node["n"], p) - node["d"] >= 0 else node["back"]
    return tree[1]


def subtract(pts, covers, n):
    """Convex polygon minus convex polygons on its plane (all wound
    counter-clockwise seen along n) -> convex pieces."""
    pieces = [pts]
    for c in covers:
        k = len(c)
        if k < 3 or not pieces:
            continue
        nxt = []
        for f in pieces:
            rest = f
            for i in range(k):
                a, b = c[i], c[(i + 1) % k]
                m = _unit(_cross(_sub(b, a), n))       # away from the cover's inside
                if m is None:
                    continue
                dm = _dot(m, a)
                out = clip(rest, m, dm, True, GEOM_EPS)
                if len(out) >= 3 and area(out) > MIN_PIECE_AREA and max(_dot(m, p) - dm for p in out) > GEOM_EPS:
                    nxt.append(out)
                rest = clip(rest, m, dm, False, GEOM_EPS)
                if len(rest) < 3 or area(rest) <= MIN_PIECE_AREA:
                    break
        pieces = nxt
    return pieces


def _covers(cell, n, d):
    """Convex pieces of a cell's polygon parts lying on the plane n.p = d and
    facing along n, wound counter-clockwise seen along n."""
    out = []
    for P in cell["polys"]:
        if _dot(P.n, n) > 0.999 and abs(P.d - d) <= EPS and P.side(n, d) == 0:
            out += P.convex()
    return out


def merge_convex(pieces, n):
    """Convex polygons on one plane, sharing edges -> fewer convex polygons:
    two join where they share an edge and their union is still convex."""
    pieces = [list(x) for x in pieces]

    def key(p):
        return tuple(round(c, 5) for c in p)

    def convex(loop):
        for i in range(len(loop)):
            a, b, c = loop[i - 1], loop[i], loop[(i + 1) % len(loop)]
            if _dot(_cross(_sub(b, a), _sub(c, b)), n) < -1e-7:
                return False
        return True

    changed = True
    while changed and len(pieces) > 1:
        changed = False
        edges = {}
        for i, loop in enumerate(pieces):
            for j in range(len(loop)):
                edges[(key(loop[j]), key(loop[(j + 1) % len(loop)]))] = (i, j)
        for i, loop in enumerate(pieces):
            for j in range(len(loop)):
                other = edges.get((key(loop[(j + 1) % len(loop)]), key(loop[j])))
                if other is None or other[0] == i:
                    continue
                k, m = other
                o = pieces[k]
                # loop runs ... a b ..., o runs ... b a ...: splice o in between
                joined = loop[:j + 1] + [o[(m + 2 + t) % len(o)] for t in range(len(o) - 2)] + loop[j + 1:]
                cleaned = []
                for q in joined:
                    if not cleaned or key(q) != key(cleaned[-1]):
                        cleaned.append(q)
                if len(cleaned) > 1 and key(cleaned[0]) == key(cleaned[-1]):
                    cleaned.pop()
                # drop vertices in the middle of a straight edge
                straight = []
                for t in range(len(cleaned)):
                    a_, b_, c_ = cleaned[t - 1], cleaned[t], cleaned[(t + 1) % len(cleaned)]
                    cr = _cross(_sub(b_, a_), _sub(c_, b_))
                    if math.sqrt(_dot(cr, cr)) > 1e-7 * (1 + math.sqrt(_dot(_sub(c_, a_), _sub(c_, a_)))):
                        straight.append(b_)
                if len(straight) >= 3 and convex(straight):
                    pieces[i] = straight
                    del pieces[k]
                    changed = True
                    break
            if changed:
                break
    return pieces


# --- rooms --------------------------------------------------------------------

def rooms(items, occupied=(), stats=None):
    """items: [(loops, pieces)] of points; occupied: points where something of
    the level is (separate objects, brushes, entities) -> rooms, each
    {"polys": [(src, loops, pieces)], "portals": [(room, [convex pieces])],
    "closing": [[convex pieces]]}. Polygons come out whole, as given."""
    stats = defaultdict(int) if stats is None else stats
    cl, root = cells(items, stats)

    # cells that hold nothing are void outside the level
    keep = [bool(c["polys"]) for c in cl]
    for p in occupied:
        keep[locate(root, p)] = True
    stats["void cells left out"] = keep.count(False)

    # every face shared by two cells, once, from its front side: what is left
    # of it on each side once that side's polygons on it are taken away, and
    # what neither side's polygons cover
    shared = []
    for a in cl:
        for face in a["faces"]:
            if face["tag"][0] == "box" or face["tag"][1] != 1:
                continue
            node = face["tag"][0]
            n, d = node["n"], node["d"]
            nb = (-n[0], -n[1], -n[2])
            found = []
            _descend(node["back"], face["pts"], found)
            cov_a = _covers(a, n, d) if keep[a["id"]] else []
            for bid, piece in found:
                if not keep[a["id"]] and not keep[bid]:
                    continue
                if _dot(_newell(piece), n) < 0:
                    piece = list(reversed(piece))
                cov_b = _covers(cl[bid], nb, -d) if keep[bid] else []
                left_a = subtract(piece, cov_a, n) if keep[a["id"]] else []
                left_b = subtract(list(reversed(piece)), cov_b, nb) if keep[bid] else []
                open_area = 0.0
                if keep[a["id"]] and keep[bid]:
                    both = cov_a + [list(reversed(c)) for c in cov_b]
                    open_area = sum(area(x) for x in subtract(piece, both, n))
                shared.append((a["id"], bid, n, d, left_a, left_b, area(piece), open_area))

    parent = list(range(len(cl)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # a polygon is never cut: the cells in front of it are one room
    cells_of = defaultdict(set)
    for c in cl:
        for P in c["polys"]:
            cells_of[P.src].add(c["id"])
    for ids_ in cells_of.values():
        first = find(min(ids_))
        for other in ids_:
            ro = find(other)
            if ro != first:
                parent[ro] = first
                stats["cells joined by a polygon"] += 1

    # rooms grow across openings large for the smaller of the two
    shared_area = defaultdict(float)       # (cell, cell) -> area of faces between them
    open_between = defaultdict(float)      # (cell, cell) -> open area between them
    for ia, ib, _n, _d, _la, _lb, piece_area, open_area in shared:
        if keep[ia] and keep[ib]:
            shared_area[(ia, ib)] += piece_area
            shared_area[(ib, ia)] += piece_area
            open_between[(ia, ib)] += open_area
            open_between[(ib, ia)] += open_area
    gb = defaultdict(float)                              # group -> boundary area
    gs = defaultdict(lambda: defaultdict(float))         # group -> group -> shared area
    go = defaultdict(lambda: defaultdict(float))         # group -> group -> open area
    for c in cl:
        if keep[c["id"]]:
            gb[find(c["id"])] += sum(area(f["pts"]) for f in c["faces"])
    for (ia, ib), w in shared_area.items():
        ga, gb_ = find(ia), find(ib)
        if ga != gb_:
            gs[ga][gb_] += w
            go[ga][gb_] += open_between[(ia, ib)]
        else:
            gb[ga] -= w                    # a face inside a group is not its boundary

    def ratio(x, y):
        return go[x][y] / max(1e-9, min(gb[x], gb[y]))

    heap = [(-ratio(x, y), x, y) for x in list(go) for y in list(go[x]) if x < y]
    heapq.heapify(heap)
    while heap:
        r, x, y = heapq.heappop(heap)
        if -r < ROOM_OPENING:
            break
        if find(x) != x or find(y) != y or abs(-r - ratio(x, y)) > 1e-12:
            continue                       # stale
        parent[x] = y
        stats["groups joined across openings"] += 1
        gb[y] = gb[x] + gb[y] - 2 * gs[x][y]
        for z in list(gs[x]):
            if z == y:
                continue
            gs[y][z] += gs[x][z]
            gs[z][y] += gs[x][z]
            go[y][z] += go[x][z]
            go[z][y] += go[x][z]
            gs[z].pop(x, None)
            go[z].pop(x, None)
        gs[y].pop(x, None)
        go[y].pop(x, None)
        gs.pop(x, None)
        go.pop(x, None)
        for z in list(go[y]):
            lo_, hi_ = min(y, z), max(y, z)
            heapq.heappush(heap, (-ratio(lo_, hi_), lo_, hi_))

    # a room with few polygons joins the neighbour it is most open to, or the
    # one it shares most face with
    size = defaultdict(int)
    for ids_ in cells_of.values():
        size[find(min(ids_))] += 1
    neighbours = defaultdict(set)
    for (ia, ib) in shared_area:
        neighbours[ia].add(ib)
    members = defaultdict(list)
    for c in cl:
        if keep[c["id"]]:
            members[find(c["id"])].append(c["id"])
    for r0 in sorted(members, key=lambda r: size[r]):
        r = find(r0)
        if size[r] > SMALL_ROOM_POLYGONS:
            continue
        for measure in (open_between, shared_area):
            best = defaultdict(float)
            for m in members[r0]:
                for other in neighbours[m]:
                    ro = find(other)
                    if ro != r and measure[(m, other)] > 0:
                        best[ro] += measure[(m, other)]
            if best:
                target = max(best, key=best.get)
                parent[r] = target
                size[target] += size[r]
                members[target] += members[r0]
                stats["small rooms joined to a neighbour"] += 1
                break

    ids = {}
    for c in cl:
        if keep[c["id"]]:
            ids.setdefault(find(c["id"]), len(ids))
    room = {c["id"]: ids[find(c["id"])] for c in cl if keep[c["id"]]}
    out = [{"polys": [], "portals": defaultdict(list), "closing": defaultdict(list)} for _ in ids]

    def key(n, d):
        return (round(n[0], 4), round(n[1], 4), round(n[2], 4), round(d, 3))

    for ia, ib, n, d, left_a, left_b, _pa, _oa in shared:
        nb = (-n[0], -n[1], -n[2])
        if keep[ia] and keep[ib] and room[ia] == room[ib]:
            continue
        if keep[ia] and left_a:
            where, tag = ("portals", (room[ib],) + key(n, d)) if keep[ib] else ("closing", key(n, d))
            out[room[ia]][where][tag] += left_a
        if keep[ib] and left_b:
            where, tag = ("portals", (room[ia],) + key(nb, -d)) if keep[ia] else ("closing", key(nb, -d))
            out[room[ib]][where][tag] += left_b
    for c in cl:
        if not keep[c["id"]]:
            continue
        for face in c["faces"]:
            if face["tag"][0] != "box":
                continue
            n = face["inward"]
            d = _dot(n, face["pts"][0])
            left = subtract(face["pts"], _covers(c, n, d), n)
            if left:
                out[room[c["id"]]]["closing"][key(n, d)] += left

    # polygons: whole, in the room of the cells in front of them
    for src, ids_ in sorted(cells_of.items()):
        loops, pieces = items[src]
        out[room[min(ids_)]]["polys"].append((src, loops, pieces))

    for r in out:
        # slivers left by gaps in NE's mesh close nothing that matters, and a
        # sliver portal is what fails to link or to triangulate
        r["portals"] = [(k[0], merge_convex(v, k[1:4])) for k, v in r["portals"].items()
                        if sum(area(x) for x in v) >= MIN_BOUNDARY_AREA]
        r["closing"] = [merge_convex(v, k[0:3]) for k, v in r["closing"].items()
                        if sum(area(x) for x in v) >= MIN_BOUNDARY_AREA]
        stats["portal polygons"] += len(r["portals"])
        stats["portal pieces"] += sum(len(v) for _k, v in r["portals"])
        stats["closing polygons"] += len(r["closing"])
    stats["rooms"] = len(out)
    return out


# --- a look at it -------------------------------------------------------------

def level_items(level):
    """The zoning world base's polygons of a level, as tools/wldprep.py
    writes them: ([(loops, pieces, samples, light)], occupied points)."""
    import json
    import wldprep
    groups = wldprep.level_polygons(level)
    desc = json.loads((ROOT / "build" / "world" / (level + ".json")).read_text())
    items, occupied = [], []
    for sector in desc["worldbase"]["sectors"]:
        mats, polys = wldprep.gather(groups, sector["groups"])
        if sector.get("liquid_surface"):
            items += [p for _m, p in polys]
        else:
            parts = wldprep.pieces(polys)
            items += [polys[i][1] for i in parts[0]]
            occupied += [polys[i][1][0][0][0] for ix in parts[1:] for i in ix]
    occupied += occupied_points(desc, groups)
    return items, occupied


def occupied_points(desc, groups):
    """Where the level has something besides the world base: brush entity
    polygons, props, effects and every entity with a position."""
    out = []
    for b in desc["brush_entities"]:
        g = groups.get(b["group"]) if b.get("group") else None
        if g:
            out += [loops[0][0] for loops, _p, _s, _l in g["polygons"]]
        out.append(tuple(b["matrix"]["translation"]))
    for key in ("props", "effects"):
        for e in desc.get(key, ()):
            m = e.get("matrix") or {}
            if "translation" in m:
                out.append(tuple(m["translation"]))
    for key in ("lights", "touch_fields", "bouncers", "sounds", "messages", "cameras", "music", "haze_markers"):
        for e in desc.get(key, ()):
            if isinstance(e, dict) and e.get("position") is not None:
                out.append(tuple(e["position"]))
    return out


def cmd_render(args):
    """An oblique view of a level's rooms: every polygon facing the camera,
    coloured by its room and shaded by its slope; portal edges in white.
    --below leaves out polygons entirely above a height, to look inside."""
    import colorsys
    from gxtex import write_png
    items, occupied = level_items(args.level)
    stats = defaultdict(int)
    rs = rooms([(loops, pieces) for loops, pieces, _s, _l in items], occupied, stats)
    print(dict(stats))
    counts = sorted(len(r["polys"]) for r in rs)
    print("polygons per room: empty %d, median %d, largest %s" % (
        sum(1 for c in counts if c == 0), counts[len(counts) // 2], counts[-5:]))

    yaw, pitch = math.radians(args.yaw), math.radians(args.pitch)
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    away = (sy * cp, -sp, cy * cp)         # the camera looks along this
    up = (sy * sp, cp, cy * sp)

    def project(p):
        return (p[0] * cy - p[2] * sy, -_dot(p, up), _dot(p, away))     # u, v, depth

    if args.closing:
        shown = [(r, [ps], []) for r, room in enumerate(rs) for pieces in room["closing"] for ps in pieces]
    else:
        shown = [(r, loops, pieces) for r, room in enumerate(rs) for _src, loops, pieces in room["polys"]]
    shown = [x for x in shown if args.below is None or min(q[1] for q in x[1][0]) < args.below]
    pts = [project(q) for _r, loops, _p in shown for q in loops[0]]
    lo_u, hi_u = min(p[0] for p in pts), max(p[0] for p in pts)
    lo_v, hi_v = min(p[1] for p in pts), max(p[1] for p in pts)
    sc = (args.size - 8) / max(hi_u - lo_u, hi_v - lo_v)
    w, h = int((hi_u - lo_u) * sc) + 8, int((hi_v - lo_v) * sc) + 8
    img = bytearray([16, 16, 20, 255] * (w * h))
    zbuf = [1e30] * (w * h)
    colours = [colorsys.hsv_to_rgb((i * 0.618034) % 1.0, 0.7, 1.0) for i in range(len(rs))]

    def screen(p):
        u, v, depth = project(p)
        return ((u - lo_u) * sc + 4, (v - lo_v) * sc + 4, depth)

    def fill(tri, rgb):
        (ax, ay, az), (bx, by, bz), (cx_, cy_, cz) = [screen(q) for q in tri]
        den = (by - cy_) * (ax - cx_) + (cx_ - bx) * (ay - cy_)
        if abs(den) < 1e-9:
            return
        for iy in range(max(0, int(min(ay, by, cy_))), min(h, int(max(ay, by, cy_)) + 1)):
            for ix in range(max(0, int(min(ax, bx, cx_))), min(w, int(max(ax, bx, cx_)) + 1)):
                l1 = ((by - cy_) * (ix - cx_) + (cx_ - bx) * (iy - cy_)) / den
                l2 = ((cy_ - ay) * (ix - cx_) + (ax - cx_) * (iy - cy_)) / den
                l3 = 1 - l1 - l2
                if l1 < -0.01 or l2 < -0.01 or l3 < -0.01:
                    continue
                depth = l1 * az + l2 * bz + l3 * cz
                idx = iy * w + ix
                if depth < zbuf[idx]:
                    zbuf[idx] = depth
                    img[idx * 4:idx * 4 + 3] = rgb

    for r, loops, pieces in shown:
        for ps in (pieces or loops[:1]):
            nrm = _unit(_newell(ps))
            if nrm is None or (_dot(nrm, away) >= 0 and not args.closing):
                continue                         # faces away from the camera
            shade = 0.35 + 0.65 * max(0.0, nrm[1] * 0.8 + 0.2 * abs(nrm[0]))
            rgb = bytes(int(255 * c * shade) for c in colours[r])
            for i in range(1, len(ps) - 1):
                fill((ps[0], ps[i], ps[i + 1]), rgb)
    for room in rs:
        for _other, pieces in room["portals"]:
            for ps in pieces:
                if args.below is not None and min(q[1] for q in ps) >= args.below:
                    continue
                for i in range(len(ps)):
                    (ax, ay, az), (bx, by, bz) = screen(ps[i - 1]), screen(ps[i])
                    steps = int(max(abs(ax - bx), abs(ay - by))) + 1
                    for t in range(steps + 1):
                        f = t / steps
                        ix, iy = int(ax + (bx - ax) * f), int(ay + (by - ay) * f)
                        if 0 <= ix < w and 0 <= iy < h:
                            idx = iy * w + ix
                            if az + (bz - az) * f <= zbuf[idx] + 0.5:
                                img[idx * 4:idx * 4 + 3] = bytes((255, 255, 255))
    name = (args.level + "_rooms" + ("_closing" if args.closing else "")
            + ("" if args.below is None else "_below%g" % args.below) + ".png")
    out = ROOT / "build" / "render" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out, w, h, bytes(img))
    print("rooms", len(rs), "->", out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="oblique view of a level's rooms: polygons coloured by room, portals white")
    p.add_argument("level")
    p.add_argument("--size", type=int, default=1400)
    p.add_argument("--yaw", type=float, default=30.0, help="degrees around the vertical")
    p.add_argument("--pitch", type=float, default=50.0, help="degrees down from level")
    p.add_argument("--below", type=float, help="leave out polygons entirely above this height")
    p.add_argument("--closing", action="store_true", help="draw the invisible closing polygons instead")
    p.set_defaults(func=cmd_render)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
