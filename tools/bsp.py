#!/usr/bin/env python3
"""
BSP trees in a WORLDMESH (.ssw) image, reached through root slot +0x74.

    image+0x74 -> A  {next = B, count, array, count, array}   runtime-sized
    B          =     {Plane* pool, Node* collision, Header* h, Node* region}
    h          =     {..., two more tree roots, ...}

A node is `{Plane* plane, child0, child1}`; a plane record is
`{Vec3* normal, float d}` whose normal is a shared unit vector. A child that is
not a node is a leaf. Plane records are recognised structurally -- a relocated
pointer to a unit vector followed by a plain float -- rather than by address
range, because the pool is not one contiguous run.

**Classification** -- established by pushing points through the region tree:

    s = dot(normal, p) + d;   s >= 0 -> child at +4, otherwise child at +8

Points just under liquid surfaces: 20 of 20 lava points land in lava leaves on
Rlevel3_1, 8 of 12 and 6 of 7 water points in water leaves on Rlevel1_1 and
Alevel10_2; none of 4,500 points above dry geometry lands in a liquid leaf.
The other three sign/order conventions score close to zero.

**Region leaf** -- the record the game's region query returns (the caller at
0x80031830 tests the byte at +4 for water):

    struct CcRegion {
        u32     _00;
        u8      content;       // +0x04  0 none, 1 water, 2 lava -- SE1's own
                               //        content numbering
        u8      environment;   // +0x05  13 on all 23 water leaves -- SE1
                               //        environment type 13 is "Underwater"
        u16     _06;
        CcFog*  fog;           // +0x08  {u32 on; f32 start, end; f32 rgb[3]}
        void*   effect;        // +0x0c  {CcTexture* ramp; Mtx* projection;
                               //         u32; f32 rgb[3]} -- a projected tint
    };

The collision tree's leaves list `(u16 index, u16 flags)` entries and pointers
to brush-like records; the two trees in the header are still unidentified (they
are *not* keyed by TouchField volume index -- tested and rejected).

Usage:
    python tools/bsp.py info orig/files/Levels/Rlevel3_1.ssw
"""

from __future__ import annotations

import argparse
import itertools
import math
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container                     # noqa: E402
from level import world_triangles             # noqa: E402

TREES_SLOT = 0x74
EPS = 1e-3


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def f32(img, o):
    return struct.unpack_from(">f", img, o)[0]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class Trees:
    def __init__(self, container):
        self.img = img = container.image
        n = len(img)
        self.ptrs = ptrs = set(container.pointer_offsets())

        def unit(v):
            return 0 < v < n - 12 and abs(math.sqrt(
                sum(f32(img, v + 4 * i) ** 2 for i in range(3))) - 1.0) < 0.01

        self.planes = {o for o in ptrs
                       if (o + 4) not in ptrs and unit(u32(img, o))}
        self.collision_root = self.region_root = 0
        self.extra_roots = []
        a = u32(img, TREES_SLOT)
        if not (0 < a < n - 8):
            return
        b = u32(img, a)
        if not (0 < b < n - 16):
            return
        self.collision_root = u32(img, b + 4)
        header = u32(img, b + 8)
        self.region_root = u32(img, b + 12)
        if 0 < header < n - 32:
            self.extra_roots = [
                r for r in (u32(img, header + 4 * i) for i in range(8))
                if self.is_node(r) and r not in (self.collision_root,
                                                 self.region_root, header)]

    def is_node(self, x):
        return 0 < x < len(self.img) - 12 and u32(self.img, x) in self.planes

    def plane(self, x):
        p = u32(self.img, x)
        nrm = u32(self.img, p)
        return (tuple(f32(self.img, nrm + 4 * i) for i in range(3)),
                f32(self.img, p + 4))

    def classify(self, root, p):
        x = root
        for _ in range(512):
            if not x:
                return None
            if not self.is_node(x):
                return x
            nrm, d = self.plane(x)
            x = (u32(self.img, x + 4) if _dot(nrm, p) + d >= 0
                 else u32(self.img, x + 8))
        return None

    def walk(self, root):
        """(nodes, leaves) reachable from root."""
        seen, leaves, stack = set(), set(), [root]
        while stack:
            x = stack.pop()
            if not x or x in seen:
                continue
            seen.add(x)
            if self.is_node(x):
                stack += [u32(self.img, x + 4), u32(self.img, x + 8)]
            else:
                leaves.add(x)
        return seen - leaves, leaves

    def cells(self, root, bounds, keep):
        """Yield (leaf, half-spaces a.p + b >= 0) for each path ending in a
        leaf for which keep(leaf) is true; `bounds` is appended to close it."""
        stack = [(root, [])]
        while stack:
            x, hs = stack.pop()
            if not x:
                continue
            if not self.is_node(x):
                if keep(x):
                    yield x, hs + bounds
                continue
            nrm, d = self.plane(x)
            stack.append((u32(self.img, x + 4), hs + [(nrm, d)]))
            stack.append((u32(self.img, x + 8),
                          hs + [(tuple(-v for v in nrm), -d)]))

    def region(self, leaf):
        img = self.img
        fog = None
        fp = u32(img, leaf + 8) if (leaf + 8) in self.ptrs else 0
        if fp:
            fog = {"on": u32(img, fp), "start": f32(img, fp + 4),
                   "end": f32(img, fp + 8),
                   "rgb": [f32(img, fp + 12 + 4 * i) for i in range(3)]}
        return {"content": img[leaf + 4], "environment": img[leaf + 5],
                "fog": fog,
                "effect": (leaf + 12) in self.ptrs and bool(u32(img, leaf + 12))}


def _solve3(a1, a2, a3, r):
    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    m = [list(a1), list(a2), list(a3)]
    dm = det(m)
    if abs(dm) < 1e-9:
        return None
    out = []
    for i in range(3):
        mm = [row[:] for row in m]
        for k in range(3):
            mm[k][i] = r[k]
        out.append(det(mm) / dm)
    return tuple(out)


def polytope(hs, ncap):
    """Vertices and faces of the convex set {p : a.p + b >= 0 for (a, b) in hs}.

    Each face lists vertex indices counter-clockwise as seen from inside, i.e.
    looking along its plane normal `a`, which points into the volume. The last
    `ncap` half-spaces are the artificial bounding box; faces on them are
    marked `cap`.
    """
    verts = []
    for (a1, b1), (a2, b2), (a3, b3) in itertools.combinations(hs, 3):
        p = _solve3(a1, a2, a3, (-b1, -b2, -b3))
        if p is None:
            continue
        if all(_dot(a, p) + b >= -EPS for a, b in hs) and \
                not any(math.dist(p, q) < EPS for q in verts):
            verts.append(p)
    faces, seen = [], set()
    for k, (a, b) in enumerate(hs):
        on = [i for i, v in enumerate(verts) if abs(_dot(a, v) + b) < 10 * EPS]
        key = frozenset(on)
        if len(on) < 3 or key in seen:
            continue
        seen.add(key)
        c = [sum(verts[i][j] for i in on) / len(on) for j in range(3)]
        ref = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
        u = (a[1] * ref[2] - a[2] * ref[1], a[2] * ref[0] - a[0] * ref[2],
             a[0] * ref[1] - a[1] * ref[0])
        lu = math.sqrt(_dot(u, u))
        u = tuple(x / lu for x in u)
        w = (a[1] * u[2] - a[2] * u[1], a[2] * u[0] - a[0] * u[2],
             a[0] * u[1] - a[1] * u[0])
        on.sort(key=lambda i: math.atan2(
            _dot([verts[i][j] - c[j] for j in range(3)], w),
            _dot([verts[i][j] - c[j] for j in range(3)], u)))
        faces.append({"plane": [a[0], a[1], a[2], b], "verts": on,
                      "cap": k >= len(hs) - ncap})
    return verts, faces


def region_cells(container, pad=10.0):
    """Every region-tree cell that ends in a typed leaf, as a closed convex
    volume bounded by the level's box (padded by `pad`)."""
    t = Trees(container)
    if not t.region_root:
        return []
    vs = [q for tri in world_triangles(container) for q in tri]
    if not vs:
        return []
    lo = [min(v[i] for v in vs) - pad for i in range(3)]
    hi = [max(v[i] for v in vs) + pad for i in range(3)]
    box = []
    for i in range(3):
        e = [0.0, 0.0, 0.0]
        e[i] = 1.0
        box.append((tuple(e), -lo[i]))
        box.append((tuple(-x for x in e), hi[i]))
    out = []
    for leaf, hs in t.cells(t.region_root, box, lambda x: t.img[x + 4] != 0):
        v, f = polytope(hs, len(box))
        if len(v) >= 4 and len(f) >= 4:
            rec = t.region(leaf)
            rec.update(leaf=leaf,
                       vertices=[[round(c, 4) for c in p] for p in v],
                       faces=f)
            out.append(rec)
    return out


def cmd_info(args):
    for path in args.files:
        t = Trees(Container(path))
        print("=== %s ===" % path.name)
        for name, root in (("collision", t.collision_root),
                           ("region", t.region_root)) + tuple(
                ("extra", r) for r in t.extra_roots):
            if not root:
                continue
            nodes, leaves = t.walk(root)
            types = Counter(t.img[x + 4] for x in leaves)
            print("  %-9s 0x%07x  %5d nodes  %4d leaves  byte@+4 %s"
                  % (name, root, len(nodes), len(leaves), dict(types)))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="the trees at root +0x74")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_info)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
