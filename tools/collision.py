#!/usr/bin/env python3
"""
The level collision meshes, and the TouchField and Bouncer volumes inside them
(docs/world-conversion.md, "TouchField and Bouncer").

The level root's slot +0x50 points to the static collision mesh record:

    +0x04  header           bounding sphere, ...
    +0x08  Vec3 verts[]     the level's one shared vertex array
    +0x0c  CcColTri tris[ntris]
    +0x10  u16 lists        (+0x14 the same; +0x18 more)
    +0x40  u32 nverts       vertices this mesh uses (the first ones)
    +0x44  u32              another count -- NOT the shared array's size
    +0x48  u32 ntris

    struct CcColTri {      // 10 bytes
        u16 flags;         // 0x1f solid on ~98% of triangles; bit 0x40 = trigger
        u16 v[3];          // indices into the shared array
        u16 value;         // kept by a hit only when flags & 2
    };

**Triangle order is polygon order.** The triangles are listed polygon by
polygon of the original Serious Editor world, and the render mesh shares
the vertex array, so this order is what rebuilds the level's polygons
(tools/polygons.py).

What a hit on a triangle means is decided when a scene query copies its hits
out (Scene_SweepQuery 0x80140668 and its siblings): the output type is the
collision object's type byte (+0xb4 >> 24: 0 level, 2 brush, 4 model), then

    flags & 0x40      -> type 3, TouchField, index value & 0x7f; value bits
                         0x400 / 0x800 are the field's props words 5 and 6
    value & 0x200     -> type 1, Bouncer, index value & 0x3f

and Collision_HandleHits (0x80090b74) hands type 3 to TouchField_GetByIndex and
type 1 to Bouncer_GetByIndex. A TouchField volume is a group of non-solid
trigger triangles; a Bouncer volume is a group of *solid* (0x1f) triangles,
the pad itself, often open (a quad, or a box without its bottom).

The collision query tests `(flags_a & flags_b & 0xfc)`, the category bits.
Player_TouchFields asks with 0x49, which reaches the 0x40 triangles.

**Mover meshes.** Every other record whose +0x08 is the same vertex array has
the same layout: a brush instance's collision mesh (type 2 at run time). Each
image+0x48 brush node points at its own record at +0x28 (`brush_meshes`);
1,059 of the 1,209 have triangles, 135 have vertices only, and 15 are empty.
A record's triangles index the shared array past the root's vertices, and
those vertices are in the brush's local space. None holds a trigger or bouncer triangle, so
both kinds of volume are the root mesh's alone.

The shared array's size is not stored anywhere found so far. +0x44 is 11,470
on Rlevel1_2, where the highest index any mesh uses is 7,878, and it is below
the root's own +0x40 on Clevel8_4 and Rlevel3_3a. So the reader sizes the
array from the highest index a mesh actually uses.

    python tools/collision.py verify [LEVELS...]
    python tools/collision.py list LEVEL
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from ssg import Container                                        # noqa: E402

MESH_SLOT = 0x50
SOLID = 0x1F
TRIGGER = 0x40          # flags: TouchField triangle
KEEP_VALUE = 0x02       # flags: a hit keeps the triangle's value
TOUCH_INDEX = 0x7F      # value & this = TouchField volume index
BOUNCER = 0x200         # value: Bouncer triangle
BOUNCER_INDEX = 0x3F    # value & this = Bouncer volume index
BRUSH_RECORD = 0x28     # image+0x48 brush node: its collision mesh record
TOUCHFIELD_CLASS = 4170
BOUNCER_CLASS = 4220
MAX_COORD = 1e6


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def read_mesh(img, rec, limit=None):
    """The mesh record at rec -> {"rec", "verts", "tris"}, or None.

    `verts` holds the shared array up to the highest index the triangles use.
    If `limit` is given, every index must be below it (the root: +0x40)."""
    n = len(img)
    if not 0 < rec < n - 0x4c:
        return None
    vp, tp, nt = u32(img, rec + 8), u32(img, rec + 0x0C), u32(img, rec + 0x48)
    if not (0 < vp < n and 1 <= nt < 200000) or tp + 10 * nt > n:
        return None
    tris = list(struct.iter_unpack(">5H", img[tp:tp + 10 * nt]))
    need = max(max(t[1:4]) for t in tris) + 1
    if (limit is not None and need > limit) or vp + 12 * need > n:
        return None
    verts = list(struct.iter_unpack(">3f", img[vp:vp + 12 * need]))
    used = {i for t in tris for i in t[1:4]}
    if not all(all(math.isfinite(c) and abs(c) < MAX_COORD for c in verts[i]) for i in used):
        return None
    return {"rec": rec, "verts": verts, "tris": tris}


def root_mesh(container):
    img = container.image
    rec = u32(img, MESH_SLOT)
    if not 0 < rec < len(img) - 0x4c:
        return None
    return read_mesh(img, rec, limit=u32(img, rec + 0x40))


def mover_meshes(container):
    """The brush instances' collision meshes: records whose +0x08 is the root's
    vertex array. Their vertices are local to the brush."""
    img, ptrs = container.image, set(container.pointer_offsets())
    rec = u32(img, MESH_SLOT)
    vp = u32(img, rec + 8)
    out = []
    for p in sorted(ptrs):
        r = p - 8
        if r != rec and u32(img, p) == vp:
            m = read_mesh(img, r)
            if m:
                out.append(m)
    return out


def brush_meshes(container):
    """{brush (image+0x48 node) index: its collision mesh, or None if the
    record has no triangles}. Vertices are in the brush's local space; the
    node's transform places them."""
    from level import sectors
    img = container.image
    return {s["index"]: read_mesh(img, u32(img, s["offset"] + BRUSH_RECORD))
            for s in sectors(container) if s["xform"]}


def _closed(faces):
    e = Counter()
    for a, b, c in faces:
        for x, y in ((a, b), (b, c), (c, a)):
            e[(min(x, y), max(x, y))] += 1
    return all(v == 2 for v in e.values())


def is_touch(t):
    return bool(t[0] & TRIGGER)


def is_bouncer(t):
    return bool(t[0] & KEEP_VALUE and t[4] & BOUNCER)


def _volumes(mesh, pick, key):
    """Group the triangles `pick` selects by `key(tri)` ->
    {index: {"values", "vertices", "faces", "closed"}}, vertices compacted."""
    groups = defaultdict(list)
    for t in mesh["tris"]:
        if pick(t):
            groups[key(t)].append(t)
    out = {}
    for k, tris in groups.items():
        remap = {}
        for t in tris:
            for i in t[1:4]:
                remap.setdefault(i, len(remap))
        vs = [None] * len(remap)
        for i, j in remap.items():
            vs[j] = list(mesh["verts"][i])
        faces = [[remap[t[1]], remap[t[2]], remap[t[3]]] for t in tris]
        out[k] = {"values": sorted({t[4] for t in tris}), "vertices": vs, "faces": faces,
                  "closed": _closed(faces)}
    return out


def touch_volumes(container, mesh=None):
    """{TouchField volume index: {"flags", "vertices", "faces", "closed"}}, NE
    space. "flags" is the value's high byte: the field's props words 5 (4), 6 (8)."""
    mesh = mesh or root_mesh(container)
    if not mesh:
        return {}
    vols = _volumes(mesh, is_touch, lambda t: t[4] & TOUCH_INDEX)
    for v in vols.values():
        v["flags"] = sorted({x >> 8 for x in v.pop("values")})
    return vols


def bouncer_volumes(container, mesh=None):
    """{Bouncer volume index: {"vertices", "faces", "closed"}}, NE space: the
    solid pad triangles whose value is 0x200 | index."""
    mesh = mesh or root_mesh(container)
    if not mesh:
        return {}
    vols = _volumes(mesh, is_bouncer, lambda t: t[4] & BOUNCER_INDEX)
    for v in vols.values():
        v.pop("values")
    return vols


def _levels(names):
    base = ROOT / "orig" / "files" / "Levels"
    return [base / (n + ".ssw") for n in names] if names else sorted(base.glob("*.ssw"))


def _entities(stem, cls):
    d = json.loads((ROOT / "build" / "entities" / (stem + ".json")).read_text())
    return [e for e in d["entities"] if e["cls"] == cls]


def _inside(pos, vs, pad):
    return all(min(p[k] for p in vs) - pad <= pos[k] <= max(p[k] for p in vs) + pad
               for k in range(3))


def cmd_verify(args):
    tot, problems, missing = Counter(), [], []
    for path in _levels(args.levels):
        c = Container(path)
        mesh = root_mesh(c)
        if not mesh:
            problems.append("%s: no root collision mesh" % path.stem)
            continue
        tot["levels"] += 1
        tot["collision triangles"] += len(mesh["tris"])
        tot["solid (0x1f)"] += sum(t[0] == SOLID for t in mesh["tris"])
        tot["trigger triangles"] += sum(map(is_touch, mesh["tris"]))
        tot["bouncer triangles"] += sum(map(is_bouncer, mesh["tris"]))
        tot["bouncer triangles that are solid"] += sum(
            t[0] == SOLID for t in mesh["tris"] if is_bouncer(t))
        movers = mover_meshes(c)
        tot["mover meshes"] += len(movers)
        tot["mover triangles"] += sum(len(m["tris"]) for m in movers)
        tot["trigger/bouncer triangles in movers"] += sum(
            is_touch(t) or is_bouncer(t) for m in movers for t in m["tris"])
        brushes = brush_meshes(c)
        tot["brushes"] += len(brushes)
        tot["brushes with collision triangles"] += sum(m is not None for m in brushes.values())
        if {m["rec"] for m in brushes.values() if m} != {m["rec"] for m in movers}:
            problems.append("%s: brush records differ from the mover meshes" % path.stem)

        vols = touch_volumes(c, mesh)
        fields = _entities(path.stem, TOUCHFIELD_CLASS)
        idx = {e["volume_index"] for e in fields}
        tot["trigger volumes"] += len(vols)
        tot["volumes that name no field"] += len(set(vols) - idx)
        tot["closed volumes"] += sum(v["closed"] for v in vols.values())
        tot["boxes (12 triangles)"] += sum(len(v["faces"]) == 12 for v in vols.values())
        for e in fields:
            tot["touch fields"] += 1
            v = vols.get(e["volume_index"])
            if not v:
                missing.append("%s#%d" % (path.stem, e["volume_index"]))
                continue
            tot["fields with a volume"] += 1
            w = struct.unpack_from(">9I", c.image, e["props"])
            want = (4 if w[5] else 0) | (8 if w[6] else 0)
            tot["high byte == props words 5, 6"] += v["flags"] == [want]
            tot["position inside its box"] += _inside(e["pos"], v["vertices"], 0.01)

        bvols = bouncer_volumes(c, mesh)
        bouncers = _entities(path.stem, BOUNCER_CLASS)
        bidx = {e["volume_index"] for e in bouncers}
        tot["bouncer volumes"] += len(bvols)
        tot["bouncer volumes that name no bouncer"] += len(set(bvols) - bidx)
        for e in bouncers:
            tot["bouncers"] += 1
            v = bvols.get(e["volume_index"])
            if not v:
                problems.append("%s: bouncer #%d has no volume" % (path.stem, e["volume_index"]))
                continue
            tot["bouncers with a volume"] += 1
            tot["bouncer volumes closed"] += v["closed"]
            tot["bouncer position inside its box"] += _inside(e["pos"], v["vertices"], 0.5)
    for k in ("levels", "collision triangles", "solid (0x1f)", "trigger triangles",
              "bouncer triangles", "bouncer triangles that are solid",
              "mover meshes", "mover triangles", "trigger/bouncer triangles in movers",
              "brushes", "brushes with collision triangles",
              "touch fields", "fields with a volume", "trigger volumes",
              "volumes that name no field", "closed volumes", "boxes (12 triangles)",
              "high byte == props words 5, 6", "position inside its box",
              "bouncers", "bouncers with a volume", "bouncer volumes",
              "bouncer volumes that name no bouncer", "bouncer volumes closed",
              "bouncer position inside its box"):
        print("  %-38s %s" % (k, format(tot[k], ",")))
    print("  fields with no volume (%d): %s" % (len(missing), " ".join(missing)))
    if tot["volumes that name no field"]:
        problems.append("%d trigger volumes name no TouchField" % tot["volumes that name no field"])
    if tot["bouncer volumes that name no bouncer"]:
        problems.append("%d bouncer volumes name no Bouncer" % tot["bouncer volumes that name no bouncer"])
    if tot["high byte == props words 5, 6"] != tot["fields with a volume"]:
        problems.append("%d volumes whose flags disagree with their props"
                        % (tot["fields with a volume"] - tot["high byte == props words 5, 6"]))
    if tot["bouncer position inside its box"] != tot["bouncers with a volume"]:
        problems.append("%d bouncers outside their volume's box"
                        % (tot["bouncers with a volume"] - tot["bouncer position inside its box"]))
    if tot["trigger/bouncer triangles in movers"]:
        problems.append("trigger or bouncer triangles in mover meshes")
    for p in problems:
        print("  PROBLEM " + p)
    print("%d problems" % len(problems))
    return 1 if problems else 0


def _box(v):
    vs = v["vertices"]
    lo = [round(min(p[k] for p in vs), 2) for k in range(3)]
    hi = [round(max(p[k] for p in vs), 2) for k in range(3)]
    return "%2d tris, box %s .. %s%s" % (len(v["faces"]), lo, hi, "" if v["closed"] else "  (open)")


def cmd_list(args):
    path = _levels([args.level])[0]
    c = Container(path)
    vols, bvols = touch_volumes(c), bouncer_volumes(c)
    for e in _entities(path.stem, TOUCHFIELD_CLASS):
        v = vols.get(e["volume_index"])
        print("touch   #%-3d id %-5d %s" % (e["volume_index"], e["id"],
              "flags %s, %s" % (v["flags"], _box(v)) if v else "no volume"))
    for e in _entities(path.stem, BOUNCER_CLASS):
        v = bvols.get(e["volume_index"])
        print("bouncer #%-3d id %-5d %s" % (e["volume_index"], e["id"], _box(v) if v else "no volume"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("verify"); p.add_argument("levels", nargs="*"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("list"); p.add_argument("level"); p.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
