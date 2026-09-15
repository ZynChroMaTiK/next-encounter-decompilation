#!/usr/bin/env python3
"""
Rebuild a level's SE1 polygons from its triangles.

NE's levels were built as Serious Editor worlds, and Climax triangulated every
polygon on the way to the GameCube. The render display lists were re-stripped
across polygon borders, so they no longer say where a polygon ended: 407 of
Rlevel1_2's 489 strips and 296 of its 540 fans are not even flat. The
collision mesh kept it. Its triangles are listed polygon by polygon, and render
and collision share one vertex array, so a render triangle finds its collision
triangle by its three vertex indices (837,099 of 841,032 static triangles and
41,114 of 43,281 brush triangles on the disc).

**The rule.** A polygon is a run of consecutive collision triangles that

  - have the same flags and value words,
  - all lie within EPS of the plane of the run's largest triangle, and face
    the same way,

split into the parts that shared edges hold together. On the render side a
polygon also keeps to one material and one texture mapping: two groups of
triangles join only if every corner of the smaller agrees with the texture map
of the larger's biggest triangle, up to whole tiles (the disc restarts UVs at
seams the repeating texture hides), and the other way round when the smaller
brings the bigger triangle: checking one way only let big floors drift a third
of a tile across 36 tiles.

**Measured on an original.** `dev/` holds a Serious Editor world of the
waterfall area of Rlevel1_2 from before the conversion (an early version: it
matches no disc geometry vertex for vertex). Its polygons, listed in order
with their own stored triangulation and put through the rule (`replay`), come
back 1,552 of 1,922 exactly. 121 rebuilt polygons are unions of whole
originals -- neighbours that were coplanar with the same texture, mapping and
flags, which nothing on the disc tells apart and which render the same -- 7
are pieces of one original and 1 is mixed. The vertex fields were tried as
polygon markers and are not: every one of them runs continuously across most
polygon borders.

Triangles without a collision counterpart join by the render-side conditions
and the plane test alone. A group whose outline is not one simple loop (a
hole, or two parts touching at a corner), or is longer than MAX_VERTICES, is
split into pieces that are.

    python tools/polygons.py verify [LEVELS...]
    python tools/polygons.py replay build/dev/wld/waterfallArea.txt
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import collision                                                  # noqa: E402
import level                                                      # noqa: E402
from ssg import Container                                         # noqa: E402

EPS = 0.02            # world units off the polygon's plane
MIN_AREA = 1e-6       # twice the area below which a triangle has no plane
UV_EPS = 2e-3         # texture repeats
# Detail layers tile densely (up to 4 tiles a unit), so a small triangle's map
# carried across a large polygon drifts by 0.002-0.01 of a tile on float
# positions; genuine seams between the designers' polygons are 0.1-0.5 tiles.
LAYER_UV_EPS = 2e-2
MAX_VERTICES = 128    # corners of all loops; bounds the triangulator's O(n^3)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def plane(p0, p1, p2):
    """-> ((normal, d), twice the area), or (None, area) for a sliver."""
    n = _cross(_sub(p1, p0), _sub(p2, p0))
    area = math.sqrt(_dot(n, n))
    if area < MIN_AREA:
        return None, area
    n = (n[0] / area, n[1] / area, n[2] / area)
    return (n, _dot(n, p0)), area


def _on(pl, p):
    return abs(_dot(pl[0], p) - pl[1]) < EPS


def _components(members, corners):
    """Split triangle ids into the groups shared edges hold together."""
    parent = {m: m for m in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_edge = defaultdict(list)
    for m in members:
        a, b, c = corners[m]
        for e in ((a, b), (b, c), (c, a)):
            by_edge[frozenset(e)].append(m)
    for ms in by_edge.values():
        for m in ms[1:]:
            parent[find(m)] = find(ms[0])
    out = defaultdict(list)
    for m in members:
        out[find(m)].append(m)
    return list(out.values())


def runs(keys, corners, points):
    """The rule, over triangles in list order -> a polygon id per triangle.

    keys[i] is what must match (flags and value), corners[i] the three vertex
    ids and points[id] a vertex position."""
    ids = [0] * len(keys)
    nxt = i = 0
    while i < len(keys):
        pl, area = plane(*(points[k] for k in corners[i]))
        run, j = [i], i + 1
        while j < len(keys) and keys[j] == keys[i]:
            q, qa = plane(*(points[k] for k in corners[j]))
            if qa > area and q is not None:
                if not all(_on(q, points[k]) for m in run for k in corners[m]):
                    break
                if pl is not None and _dot(q[0], pl[0]) < 0:
                    break
                pl, area = q, qa
            elif pl is not None:
                if not all(_on(pl, points[k]) for k in corners[j]):
                    break
                if q is not None and qa > 1e-3 and _dot(q[0], pl[0]) < 0:
                    break
            run.append(j)
            j += 1
        for part in _components(run, corners):
            for m in part:
                ids[m] = nxt
            nxt += 1
        i = j
    return ids


def outlines(tris, points):
    """Triangles (vertex ids, render winding) -> every loop of their outline,
    the one with the largest area first; the rest are holes, wound the other
    way. None if two triangles use the same edge the same way (they overlap).

    A Serious Engine polygon is a set of edges, not a single loop, so holes
    and loops that touch at a corner are both representable."""
    directed = Counter()
    for a, b, c in tris:
        directed[(a, b)] += 1
        directed[(b, c)] += 1
        directed[(c, a)] += 1
    if any(n > 1 for n in directed.values()):
        return None
    out_edges = defaultdict(list)
    for (a, b) in directed:
        if (b, a) not in directed:
            out_edges[a].append(b)
    loops = []
    for start in list(out_edges):
        while out_edges[start]:
            loop, cur = [start], out_edges[start].pop()
            while cur != start:
                if not out_edges[cur]:
                    return None
                loop.append(cur)
                cur = out_edges[cur].pop()
            loops.append(loop)
    if not loops:
        return None

    def area(loop):
        n = [0.0, 0.0, 0.0]
        for a, b in zip(loop, loop[1:] + loop[:1]):
            c = _cross(points[a], points[b])
            n = [n[k] + c[k] for k in range(3)]
        return _dot(n, n)

    loops.sort(key=area, reverse=True)
    return loops

def _pieces(members, corners, points):
    """Split a group whose outline is not one simple loop into pieces that
    are, growing each piece across shared edges."""
    by_vertex = defaultdict(list)
    for m in members:
        for k in corners[m]:
            by_vertex[k].append(m)
    left, out = set(members), []
    while left:
        seed = min(left)
        piece, queue = [seed], deque([seed])
        left.discard(seed)
        while queue:
            m = queue.popleft()
            for k in corners[m]:
                for n in by_vertex[k]:
                    if n not in left or len(set(corners[n]) & set(corners[m])) < 2:
                        continue
                    loops = outlines([corners[x] for x in piece + [n]], points)
                    if loops is None or len(loops) > 1 or len(loops[0]) > MAX_VERTICES:
                        continue
                    piece.append(n)
                    left.discard(n)
                    queue.append(n)
        out.append(piece)
    return out


def _convex(loop, points, normal):
    turns = set()
    for i in range(len(loop)):
        a, b, c = points[loop[i - 1]], points[loop[i]], points[loop[(i + 1) % len(loop)]]
        d = _dot(_cross(_sub(b, a), _sub(c, b)), normal)
        if abs(d) > 1e-6:
            turns.add(d > 0)
    return len(turns) <= 1


def convex_pieces(members, corners, points, normal):
    """A polygon's triangles -> outlines of convex pieces that cover them,
    grown greedily across shared edges. The writer falls back to these where
    SE1's triangulator rejects the whole polygon; a convex polygon always
    triangulates."""
    by_vertex = defaultdict(list)
    for m in members:
        for k in corners[m]:
            by_vertex[k].append(m)
    left, out = set(members), []
    while left:
        seed = min(left)
        piece, loop, queue = [seed], list(corners[seed]), deque([seed])
        left.discard(seed)
        while queue:
            m = queue.popleft()
            for k in corners[m]:
                for n in by_vertex[k]:
                    if n not in left or len(set(corners[n]) & set(corners[m])) < 2:
                        continue
                    loops = outlines([corners[x] for x in piece + [n]], points)
                    if loops is None or len(loops) > 1 or not _convex(loops[0], points, normal):
                        continue
                    piece.append(n)
                    loop = loops[0]
                    left.discard(n)
                    queue.append(n)
        out.append(loop)
    return out

def _uv_map_predicts(p, t, q):
    """The UV at point q by triangle p's affine texture map (t = its UVs)."""
    e1, e2, w = _sub(p[1], p[0]), _sub(p[2], p[0]), _sub(q, p[0])
    a11, a12, a22 = _dot(e1, e1), _dot(e1, e2), _dot(e2, e2)
    det = a11 * a22 - a12 * a12
    if det < 1e-12:
        return None
    b1, b2 = _dot(w, e1), _dot(w, e2)
    s, r = (b1 * a22 - b2 * a12) / det, (a11 * b2 - a12 * b1) / det
    return tuple(t[0][k] + s * (t[1][k] - t[0][k]) + r * (t[2][k] - t[0][k]) for k in range(2))


def polygons(container, stats=None):
    """{owner key: {"owner", "polygons"}} for every display-list owner --
    ("material", i), ("sector", i) or ("unowned", -1), the keys `level.py
    mesh` groups by, with the owner record from `level.batches`.

    A polygon is {"loops", "uvs", "triangles"}, plus "convex" when it is
    concave or has holes. loops[0] is its outline and any further loops its
    holes, each a list of corners in render winding; a corner is the vertex's
    whole field tuple (level.F_POS, level.F_TEX0, ...). "uvs" follows "loops"
    with each corner's texture coordinate (None without one), shifted by whole
    tiles where the disc wrapped it, so one affine map fits them all.
    "triangles" are the render triangles it was built from, "convex" outlines
    of convex pieces covering them. "layer_uvs" does for each texture layer's
    UV field ({field: loops}) what "uvs" does for the diffuse texture's: a
    polygon keeps to one map in every UV set its owner draws."""
    stats = Counter() if stats is None else stats
    img = container.image
    pts = level.positions(img)
    uvsets = {f: level.texcoords(img, level.FIELD_ARRAY[f]) for f in (level.F_TEX0, level.F_TEX1, level.F_TEX2)}
    uvs = uvsets[level.F_TEX0]

    def collision_ids(mesh, tag):
        if not mesh:
            return {}
        tris = mesh["tris"]
        ids = runs([(t[0], t[4]) for t in tris], [t[1:4] for t in tris], mesh["verts"])
        return {frozenset(t[1:4]): (tag, ids[i]) for i, t in reversed(list(enumerate(tris)))}

    static = collision_ids(collision.root_mesh(container), "root")
    brush = {i: collision_ids(m, i) for i, m in collision.brush_meshes(container).items()}

    owned, owners = defaultdict(list), {}
    for owner, tris in level.batches(container):
        key = (owner["kind"], owner["index"]) if owner else ("unowned", -1)
        owners[key] = owner
        owned[key].extend(t for t in tris if max(v[level.F_POS] for v in t) < len(pts))

    out = {}
    for key, tris in owned.items():
        lookup = brush.get(key[1], {}) if key[0] == "sector" else static
        kept, corners, cid = [], [], []
        for t in tris:
            ids = tuple(v[level.F_POS] for v in t)
            if len(set(ids)) < 3 or plane(*(pts[k] for k in ids))[0] is None:
                stats["degenerate triangles dropped"] += 1
                continue
            kept.append(t)
            corners.append(ids)
            cid.append(lookup.get(frozenset(ids)))
        stats["triangles"] += len(kept)
        stats["triangles with a collision triangle"] += sum(c is not None for c in cid)

        fields = [level.F_TEX0] + [x["field"] for x in (owners[key] or {}).get("layers", ())]
        clamps = {x["field"]: x["clamp"] for x in (owners[key] or {}).get("layers", ())}
        clamps[level.F_TEX0] = (owners[key] or {}).get("clamp", 0)

        def uv(v, f=level.F_TEX0):
            return uvsets[f][v[f]] if v[f] < len(uvsets[f]) else None

        # join across edges shared by exactly two triangles
        parent = list(range(len(kept)))
        members = {i: [i] for i in range(len(kept))}
        best = {i: plane(*(pts[k] for k in corners[i])) for i in range(len(kept))}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        by_edge = defaultdict(list)
        for i, ids in enumerate(corners):
            for e in ((ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[0])):
                by_edge[frozenset(e)].append(i)
        # A group's texture map is its largest textured triangle's. A group
        # joins another only if every corner of the smaller one agrees with
        # the larger one's map, up to whole tiles: the disc restarts UVs at
        # seams inside flat areas, which the repeating texture hides, and a
        # group that closes around (a ring floor) can meet itself across one.
        # The same holds in each texture layer's UVs: coplanar polygons can
        # share the diffuse map and still place a decal or detail differently.
        ref = {}
        for i in range(len(kept)):
            maps = {}
            for f in fields:
                u3 = [uv(v, f) for v in kept[i]]
                maps[f] = None if None in u3 else u3
            ref[i] = None if maps[level.F_TEX0] is None else ([pts[k] for k in corners[i]], maps, best[i][1])

        def agrees(r, group):
            for m in group:
                for v in kept[m]:
                    got = uv(v)
                    if (r is None) != (got is None):
                        return False
                    if r is None:
                        continue
                    for f in fields:
                        got = uv(v, f)
                        if r[1][f] is None or got is None:
                            if (r[1][f] is None) != (got is None):
                                return False
                            continue
                        guess = _uv_map_predicts(r[0], r[1][f], pts[v[level.F_POS]])
                        eps = UV_EPS if f == level.F_TEX0 else LAYER_UV_EPS
                        if guess is None or not _same_uv_tiled(guess, got, eps, clamps[f]):
                            if f != level.F_TEX0:
                                stats["joins refused by a layer's UVs"] += 1
                            return False
            return True

        for e, pair in by_edge.items():
            if len(pair) != 2:
                continue
            i, j = pair
            ri, rj = find(i), find(j)
            if ri == rj or cid[i] != cid[j]:
                continue
            if len(members[ri]) > len(members[rj]):
                ri, rj = rj, ri                  # ri: the smaller group
            if not agrees(ref[rj], members[ri]):
                continue
            takes_over = ref[ri] is not None and (ref[rj] is None or ref[ri][2] > ref[rj][2])
            if takes_over and not agrees(ref[ri], members[rj]):
                continue                         # the new map must fit the old corners too
            if cid[i] is None:
                (pli, ai), (plj, aj) = best[ri], best[rj]
                pl = pli if ai >= aj else plj
                if not all(_on(pl, pts[k]) for m in members[ri] + members[rj] for k in corners[m]):
                    continue
                if _dot(pli[0], plj[0]) < 0:
                    continue
                best[rj] = (pl, max(ai, aj))
            if takes_over:
                ref[rj] = ref[ri]
            parent[ri] = rj
            members[rj] += members.pop(ri)

        polys = []
        for root, group in members.items():
            group.sort()
            loops = outlines([corners[m] for m in group], pts)
            if loops is not None and sum(len(x) for x in loops) <= MAX_VERTICES:
                parts = [(group, loops)]
            else:
                stats["groups split into pieces"] += 1
                parts = [(p, outlines([corners[m] for m in p], pts)) for p in _pieces(group, corners, pts)]
            for part, loops in parts:
                field = {}
                for m in part:
                    for v in kept[m]:
                        field.setdefault(v[level.F_POS], v)
                _check(stats, [[pts[k] for k in x] for x in loops],
                       [[pts[k] for k in corners[m]] for m in part])
                r = ref[root]

                def unwrapped(k, f=level.F_TEX0):
                    got = uv(field[k], f)
                    if got is None or r is None or r[1][f] is None:
                        return got
                    guess = _uv_map_predicts(r[0], r[1][f], pts[k])
                    if guess is None:                # a sliver has no map to follow
                        return got
                    return (got[0] - (0 if clamps[f] & level.CLAMP_U else round(got[0] - guess[0])),
                            got[1] - (0 if clamps[f] & level.CLAMP_V else round(got[1] - guess[1])))

                rec = {"loops": [[field[k] for k in x] for x in loops],
                       "uvs": [[unwrapped(k) for k in x] for x in loops],
                       "layer_uvs": {f: [[unwrapped(k, f) for k in x] for x in loops] for f in fields[1:]},
                       "triangles": [kept[m] for m in part]}
                normal = _newell([pts[k] for k in loops[0]])
                if normal is not None and (len(loops) > 1 or not _convex(loops[0], pts, normal)):
                    rec["convex"] = [[field[k] for k in x]
                                     for x in convex_pieces(part, corners, pts, normal)]
                    stats["complex polygons (convex pieces offered)"] += 1
                polys.append((part[0], rec))
                stats["polygons"] += 1
                stats["from collision" if cid[part[0]] is not None else "without collision"] += 1
                stats["polygons with holes"] += len(loops) > 1
                stats["holes"] += len(loops) - 1
                n = len(loops[0])
                stats["outer corners %s" % (n if n < 8 else "8+")] += 1
        polys.sort(key=lambda p: p[0])
        out[key] = {"owner": owners.get(key), "polygons": [p[1] for p in polys]}
    return out


def _newell(loop):
    n = [0.0, 0.0, 0.0]
    for a, b in zip(loop, loop[1:] + loop[:1]):
        c = _cross(a, b)
        n = [n[k] + c[k] for k in range(3)]
    length = math.sqrt(_dot(n, n))
    return None if length < MIN_AREA else [c / length for c in n]


def _same_uv_tiled(a, b, eps=UV_EPS, clamp=0):
    """The same texture coordinate, up to whole tiles on an axis that repeats;
    a clamped axis draws its texture once, so there it must be the same."""
    du, dv = a[0] - b[0], a[1] - b[1]
    if not clamp & level.CLAMP_U:
        du -= round(du)
    if not clamp & level.CLAMP_V:
        dv -= round(dv)
    return abs(du) < eps and abs(dv) < eps


def _same_uv(a, b):
    if a is None or b is None:
        return a is b
    return abs(a[0] - b[0]) < UV_EPS and abs(a[1] - b[1]) < UV_EPS


def _check(stats, loops, tris):
    """The outline must cover exactly its triangles; how flat it is is measured.

    The Newell vectors of an outline's loops sum to its triangles' area
    vectors when the loops bound exactly those triangles, bent or not."""
    n = [0.0, 0.0, 0.0]
    for loop in loops:
        for a, b in zip(loop, loop[1:] + loop[:1]):
            c = _cross(a, b)
            n = [n[k] + c[k] for k in range(3)]
    t_sum = [0.0, 0.0, 0.0]
    for t in tris:
        c = _cross(_sub(t[1], t[0]), _sub(t[2], t[0]))
        t_sum = [t_sum[k] + c[k] for k in range(3)]
    d = _sub(n, t_sum)
    if math.sqrt(_dot(d, d)) > 1e-3 * max(1.0, math.sqrt(_dot(t_sum, t_sum))):
        stats["outline area mismatches"] += 1
    # the best-fit plane: the Newell normal through the corners' centroid
    corners = [p for loop in loops for p in loop]
    length = math.sqrt(_dot(n, n))
    if length > MIN_AREA:
        normal = [c / length for c in n]
        mid = [sum(p[k] for p in corners) / len(corners) for k in range(3)]
        fit = (normal, _dot(normal, mid))
        off = max(abs(_dot(normal, p) - fit[1]) for p in corners)
        stats["most off the best-fit plane (mm)"] = max(stats["most off the best-fit plane (mm)"], round(off * 1000))
        if off > EPS:
            # slivers: the rule holds corners within EPS of the run's plane,
            # but a thin outline's own fit can tilt
            stats["polygons off their best-fit plane by more than EPS"] += 1
        if len(loops) == 1 and len(loops[0]) > 3:
            loop, turns = loops[0], set()
            for i in range(len(loop)):
                a, b, c = loop[i - 1], loop[i], loop[(i + 1) % len(loop)]
                d = _dot(_cross(_sub(b, a), _sub(c, b)), normal)
                if abs(d) > 1e-6:
                    turns.add(d > 0)
            if len(turns) > 1:
                stats["concave polygons"] += 1


def _levels(names):
    base = ROOT / "orig" / "files" / "Levels"
    if not names:
        return sorted(base.glob("*.ssw"))
    return [base / (n if n.endswith(".ssw") else n + ".ssw") for n in names]


def cmd_verify(args):
    tot = Counter()
    for path in _levels(args.levels):
        st = Counter()
        polygons(Container(path), st)
        worst = st.pop("most off the best-fit plane (mm)", 0)
        tot += st
        tot["most off the best-fit plane (mm)"] = max(tot["most off the best-fit plane (mm)"], worst)
        tot["levels"] += 1
        if args.each:
            print("%-12s %7d triangles -> %7d polygons" % (path.stem, st["triangles"], st["polygons"]))
    order = ["levels", "triangles", "degenerate triangles dropped", "triangles with a collision triangle",
             "polygons", "from collision", "without collision", "joins refused by a layer's UVs",
             "polygons with holes", "holes",
             "groups split into pieces", "complex polygons (convex pieces offered)",
             "concave polygons", "polygons off their best-fit plane by more than EPS",
             "most off the best-fit plane (mm)", "outline area mismatches"]
    for k in order + sorted(k for k in tot if k.startswith("outer corners")):
        print("  %-38s %9d" % (k, tot[k]))
    problems = tot["outline area mismatches"]
    print("%d problems" % problems)
    return 1 if problems else 0


def _read_dump(path):
    """WldWriter --dump -> [(sector polygons)], each polygon (key, [triangles])."""
    sectors, poly = [], None
    for line in open(path):
        t = line.split()
        if not t:
            continue
        if t[0] == "sector":
            sectors.append([])
        elif t[0] == "poly":
            poly = ((t[1], tuple(t[7:13]), tuple(t[13:16])), [])
            sectors[-1].append(poly)
        elif t[0] == "t":
            v = [float(x) for x in t[1:10]]
            poly[1].append((tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9])))
    return sectors


def cmd_replay(args):
    """Run the rule over an original world's polygons, listed in order with
    their own triangulation, and score what comes back."""
    tot = Counter()
    for polys in _read_dump(args.dump):
        points, index = [], {}

        def vid(p):
            k = tuple(round(c * 64) for c in p)
            if k not in index:
                index[k] = len(points)
                points.append(p)
            return index[k]

        keys, corners, owner = [], [], []
        for pi, (key, tris) in enumerate(polys):
            for t in tris:
                ids = tuple(vid(p) for p in t)
                if len(set(ids)) < 3:
                    continue
                keys.append(key)
                corners.append(ids)
                owner.append(pi)
        ids = runs(keys, corners, points)
        truth, got = defaultdict(set), defaultdict(set)
        for i, (o, g) in enumerate(zip(owner, ids)):
            truth[o].add(i)
            got[g].add(i)
        want = {frozenset(v) for v in truth.values()}
        have = {frozenset(v) for v in got.values()}
        tot["original polygons"] += len(want)
        tot["rebuilt polygons"] += len(have)
        tot["recovered exactly"] += len(want & have)
        for g in have - want:
            os_ = {owner[i] for i in g}
            if len(os_) == 1:
                tot["piece of one original"] += 1
            elif all(truth[o] <= g for o in os_):
                tot["union of whole originals"] += 1
                tot["  originals inside those unions"] += len(os_)
            else:
                tot["mixed"] += 1
    for k in ("original polygons", "rebuilt polygons", "recovered exactly",
              "union of whole originals", "  originals inside those unions",
              "piece of one original", "mixed"):
        print("  %-26s %6d" % (k, tot[k]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="rebuild every level's polygons and check them")
    v.add_argument("levels", nargs="*")
    v.add_argument("--each", action="store_true", help="a line per level")
    v.set_defaults(func=cmd_verify)
    r = sub.add_parser("replay", help="score the rule on an original world (WldWriter --dump)")
    r.add_argument("dump", type=Path)
    r.set_defaults(func=cmd_replay)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
