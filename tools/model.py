#!/usr/bin/env python3
"""
Prop meshes (the `.clm` models) embedded in a WORLDMESH level, and their
placed instances.

The level's model meshes hang off root slot `image + 0x5c`, one node per
distinct mesh:

    struct CcMesh {
        CcMesh*     next;        // +0x00
        CcMaterial* materials;   // +0x04  the world's CcMaterial node shape;
                                 //        each material owns its display lists
        ...
        CcArray*    posRef;      // +0x0c  -> +0x50
        CcArray*    nrmRef;      // +0x10  -> +0x60
        CcArray*    uvRef;       // +0x18  -> +0x70
        ...
        CcArray     positions;   // +0x50  float3, local space
        CcArray     normals;     // +0x60  float3
        CcArray     texcoords;   // +0x70  float2
        CcMaterial  first;       // +0x80  name at +0x04 is the Maya shading
                                 //        group ('bushSG', 'phong4SG')
    };

Geometry is GX display lists of **vertex type 2, stride 8**:
`{u16 position, u16 normal, u16 packed, u16 texcoord}`. Established the way
the world's layout was, by exact fit against each mesh's own arrays: field 0
is the position index on 16 of 21 meshes on Rlevel1_1 (max index == count-1),
field 1 the normal on 16, field 3 the texcoord on 19. Field 2 is not an index,
exactly like the world's field 2.

Instances live in the model list at `image + 0x58` (tools/entity.py
`model_instances`). An instance points straight at its mesh node: on Rlevel1_1
299 of 299 instances that carry a `.clm` path do, and no mesh is shared across
two different `.clm` names. The other 181 instances there are flames with no
path and no mesh -- particle effects. Disc-wide 7,171 of 7,176 `.clm`
instances link; the 5 that cannot are `models\\editor\\axis.clm`, an editor
gizmo whose mesh the shipped image does not carry.

Breakable props point at more meshes after the first: their **damage
stages** `<name>_d1.clm`, `<name>_d2.clm`, each in its own record inside the
instance node. 1,105 instances carry them, over 134 meshes. 1,080 of those
instances are named by a class-4200 Prop entity (the breakables), against 302
of the 6,066 without. Renders alone suggested LODs -- a damaged statue keeps
its outline with half the polygons -- but the barrel and cabinets gain
triangles when damaged, and the Prop correlation settles it.

Usage:
    python tools/model.py list    orig/files/Levels/Rlevel1_1.ssw
    python tools/model.py meshes  orig/files/Levels/Rlevel1_1.ssw -o build/models/
    python tools/model.py props   orig/files/Levels/Rlevel1_1.ssw -o build/mesh/
    python tools/model.py render  orig/files/Levels/Rlevel1_1.ssw -o build/maps/
    python tools/model.py verify  orig/files/Levels
"""

from __future__ import annotations

import argparse
import bisect
import math
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container                                        # noqa: E402
from level import (PRIMS, _parse_prims, _to_triangles, _chain,   # noqa: E402
                   apply_xform, world_triangles, MAT_DIFFUSE)
from entity import model_instances, _fill_tri                    # noqa: E402
from gxtex import walk as texwalk, decode, write_png             # noqa: E402

MESH_LIST = 0x5C
MESH_MATERIALS = 0x04
MESH_POS, MESH_NRM, MESH_UV = 0x50, 0x60, 0x70
VTYPE, STRIDE = 2, 8
F_POS, F_NRM, F_UV = 0, 1, 3
MESH_CAP = 0x20000          # extent of the last mesh node in address order


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def u16(img, o):
    return struct.unpack_from(">H", img, o)[0]


def _cstr(img, o, limit=64):
    end = img.find(b"\x00", o, o + limit)
    return img[o:end if end >= 0 else o + limit].decode("ascii", "replace")


def _array(img, off):
    data, size, count, stride = (u32(img, off + 4 * i) for i in range(4))
    if not count or count * stride != size or not (0 < data < len(img)):
        return []
    return [struct.unpack_from(">%df" % (stride // 4), img, data + i * stride)
            for i in range(count)]


class Level:
    """Meshes and instances of one level, with the lookups they share."""

    def __init__(self, container, stem):
        self.cont, self.stem = container, stem
        self.img = img = container.image
        self.ptrs = sorted(container.pointer_offsets())
        self.pset = set(self.ptrs)
        self.texs = list(texwalk(img))
        self.texidx = {t.offset: i for i, t in enumerate(self.texs)}
        nodes = sorted(o for o, _ in _chain(img, u32(img, MESH_LIST), 0x80))
        self.mesh_nodes = nodes
        self.mesh_end = dict(zip(nodes, nodes[1:]))
        self.instances = model_instances(container)
        order = sorted(m["offset"] for m in self.instances)
        inst_end = dict(zip(order, order[1:]))
        nodeset = set(nodes)
        for m in self.instances:
            # The first mesh pointer is the intact model. Further ones are
            # its damage stages, each in a record of its own:
            # {name*, path*, mesh*, ...} with its own .clm path (Wine_barrel
            # -> Wine_barrel_d1; birtchtree -> _d1 -> _d2). Not LODs, though
            # statues and trees render like them: 1,080 of the 1,105
            # instances carrying them are named by a breakable Prop entity.
            m["mesh"], m["damage"] = None, []
            lo = m["offset"]
            hi = min(inst_end.get(lo, lo + 0x400), lo + 0x400)
            for o in range(lo, hi, 4):
                if o in self.pset and u32(img, o) in nodeset:
                    if m["mesh"] is None:
                        m["mesh"] = u32(img, o)
                    else:
                        m["damage"].append((u32(img, o), self._record_path(o)))
        # a mesh is named by the .clm its instances carry
        self.names = {}
        for m in self.instances:
            if m["mesh"] is not None and m["path"]:
                self.names.setdefault(m["mesh"], Counter())[
                    m["path"].replace("\\", "/").split("/")[-1]] += 1
        # a damage-state mesh by its own record's path; inline strings are sometimes
        # cut short ('Chinese_tree_1_d1.'), so fall back to the _dK pattern
        # every complete path follows
        self.damage_names = {}
        for m in self.instances:
            base = self.mesh_name(m["mesh"]) if m["mesh"] is not None else None
            for k, (node, path) in enumerate(m["damage"], 1):
                if node in self.names or base is None:
                    continue
                if not (path and path.lower().endswith(".clm")):
                    path = "%s_d%d.clm" % (base, k)
                self.damage_names.setdefault(node, Counter())[path] += 1

    def _record_path(self, o):
        """The .clm basename in the record holding the mesh pointer at o."""
        for d in (-4, -8):
            if o + d in self.pset:
                v = u32(self.img, o + d)
                if 0 < v < len(self.img):
                    s = _cstr(self.img, v)
                    if ".cl" in s.lower():
                        return s.replace("\\", "/").split("/")[-1]
        return None

    def mesh_name(self, node):
        c = self.names.get(node) or getattr(self, "damage_names", {}).get(node)
        base = c.most_common(1)[0][0] if c else "mesh_%x.clm" % node
        return base[:-4] if base.lower().endswith(".clm") else base

    def texname(self, i):
        t = self.texs[i]
        return "%s_%03d_%s_%dx%d.png" % (self.stem, i, t.name, t.width, t.height)

    def mesh(self, node):
        """Arrays plus a list of materials, each with its triangles as
        ((pos, nrm, uv), ...) index triples."""
        img = self.img
        # A mesh's descriptors always sit before its own vertex data, so the
        # array data bounds the search. Without it the last mesh in address
        # order (capped at MESH_CAP) read the resource glob that follows it:
        # Rlevel1_1's tollgate statue and Alevel11_1's submarine both picked
        # up the same foreign lists, max indices (3541, 1919, 1521).
        data = [u32(img, node + off) for off in (MESH_POS, MESH_NRM, MESH_UV)]
        end = min([self.mesh_end.get(node, node + MESH_CAP)]
                  + [d for d in data if d > node])
        out = {"node": node, "name": self.mesh_name(node),
               "pos": _array(img, node + MESH_POS),
               "nrm": _array(img, node + MESH_NRM),
               "uv": _array(img, node + MESH_UV),
               "materials": [], "unparsed": 0}
        chain = _chain(img, u32(img, node + MESH_MATERIALS), 0x50)
        chain = [(o, n) for o, n in chain if node <= o < end]
        starts = [o for o, _ in chain]
        for k, (mo, _nx) in enumerate(chain):
            # Close each material at the next one, and the last at the mesh's
            # own end -- an open extent is what once let the world's last
            # material swallow every movable brush.
            mend = starts[k + 1] if k + 1 < len(starts) else end
            nm = u32(img, mo + 0x04)
            mat = {"name": _cstr(img, nm) if 0 < nm < len(img) else "",
                   "diffuse": self.texidx.get(u32(img, mo + MAT_DIFFUSE)),
                   "tris": []}
            i = bisect.bisect_left(self.ptrs, mo)
            for o in self.ptrs[i:bisect.bisect_left(self.ptrs, mend)]:
                if o + 16 > len(img):
                    continue
                p = u32(img, o)
                if not (0 < p < len(img) - 8) or (img[p] & 0xF8) not in PRIMS:
                    continue
                size, used, vt = u32(img, o + 4), u32(img, o + 8), u32(img, o + 12)
                if vt > 15 or not (used <= size <= used + 32):
                    continue
                if vt != VTYPE:
                    out["unparsed"] += 1
                    continue
                prims = _parse_prims(img, p, used, STRIDE)
                if not prims:
                    out["unparsed"] += 1
                    continue
                for op, n, base in prims:
                    vs = [tuple(u16(img, base + v * STRIDE + 2 * f)
                                for f in (F_POS, F_NRM, F_UV)) for v in range(n)]
                    for a, b, c in _to_triangles(op, list(range(n))):
                        mat["tris"].append((vs[a], vs[b], vs[c]))
            out["materials"].append(mat)
        return out

    def ensure_textures(self, dest, wanted):
        written = 0
        for i in wanted:
            path = dest / self.texname(i)
            if path.exists():
                continue
            t = self.texs[i]
            try:
                rgba = decode(self.img, t.pixels, t.width, t.height, t.gx)
            except Exception:                                  # noqa: BLE001
                continue
            write_png(path, t.width, t.height, bytes(rgba))
            written += 1
        return written


def _valid(m, tri):
    np_, nu = len(m["pos"]), len(m["uv"])
    return all(v[0] < np_ for v in tri), all(v[2] < nu for v in tri)


def _write_obj(fh, mtl_fh, meshes_with_xform, stem_label):
    """meshes_with_xform: [(group label, mesh dict, xform or None)]."""
    vbase = tbase = 1
    materials = {}
    for label, m, xf in meshes_with_xform:
        fh.write("o %s\n" % label)
        for p in m["pos"]:
            x, y, z = apply_xform(xf, p) if xf else p
            fh.write("v %.4f %.4f %.4f\n" % (x, y, z))
        for u, v in m["uv"]:
            fh.write("vt %.6f %.6f\n" % (u, 1.0 - v))
        for k, mat in enumerate(m["materials"]):
            key = "%s_%d" % (m["name"], k)
            materials[key] = mat["diffuse"]
            fh.write("usemtl %s\n" % key)
            for tri in mat["tris"]:
                okp, oku = _valid(m, tri)
                if not okp:
                    continue
                if oku:
                    fh.write("f " + " ".join("%d/%d" % (vbase + v[0], tbase + v[2])
                                             for v in tri) + "\n")
                else:
                    fh.write("f " + " ".join(str(vbase + v[0]) for v in tri) + "\n")
        vbase += len(m["pos"])
        tbase += len(m["uv"])
    return materials


def _write_mtl(fh, level, materials, label):
    fh.write("# materials for %s\n" % label)
    wanted = set()
    for key, diffuse in sorted(materials.items()):
        fh.write("\nnewmtl %s\nKd 0.8 0.8 0.8\n" % key)
        if diffuse is not None:
            fh.write("map_Kd %s\n" % level.texname(diffuse))
            wanted.add(diffuse)
    return wanted


# --- subcommands ------------------------------------------------------------

def cmd_list(args):
    for path in args.files:
        L = Level(Container(path), path.stem)
        use = Counter(m["mesh"] for m in L.instances if m["mesh"] is not None)
        print("=== %s: %d meshes, %d instances (%d with a mesh) ==="
              % (path.name, len(L.mesh_nodes), len(L.instances), sum(use.values())))
        for node in L.mesh_nodes:
            m = L.mesh(node)
            tris = sum(len(x["tris"]) for x in m["materials"])
            print("  0x%07x %-24s %4d verts %5d tris %d materials %3d instances%s"
                  % (node, m["name"], len(m["pos"]), tris, len(m["materials"]),
                     use.get(node, 0),
                     "  (%d unparsed lists)" % m["unparsed"] if m["unparsed"] else ""))
    return 0


def cmd_meshes(args):
    for path in args.files:
        L = Level(Container(path), path.stem)
        dest = args.output / path.stem
        dest.mkdir(parents=True, exist_ok=True)
        wanted = set()
        for node in L.mesh_nodes:
            m = L.mesh(node)
            obj, mtl = dest / (m["name"] + ".obj"), dest / (m["name"] + ".mtl")
            with obj.open("w") as fh, mtl.open("w") as mf:
                fh.write("# %s from %s, local space\nmtllib %s\n"
                         % (m["name"], path.name, mtl.name))
                mats = _write_obj(fh, mf, [(m["name"], m, None)], m["name"])
                wanted |= _write_mtl(mf, L, mats, m["name"])
        n = L.ensure_textures(dest, wanted)
        print("%s: %d meshes -> %s (%d textures written)"
              % (path.name, len(L.mesh_nodes), dest, n))
    return 0


def cmd_props(args):
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        L = Level(Container(path), path.stem)
        cache = {}
        placed = []
        for i, inst in enumerate(L.instances):
            if inst["mesh"] is None or not inst["rot"]:
                continue
            if inst["mesh"] not in cache:
                cache[inst["mesh"]] = L.mesh(inst["mesh"])
            m = cache[inst["mesh"]]
            placed.append(("prop%d_%s" % (i, m["name"]), m,
                           (inst["rot"], inst["pos"])))
        obj = args.output / (path.stem + "_props.obj")
        mtl = args.output / (path.stem + "_props.mtl")
        with obj.open("w") as fh, mtl.open("w") as mf:
            fh.write("# %d placed props from %s, world space\nmtllib %s\n"
                     % (len(placed), path.name, mtl.name))
            mats = _write_obj(fh, mf, placed, path.stem)
            wanted = _write_mtl(mf, L, mats, path.stem)
        n = L.ensure_textures(args.output, wanted)
        tris = sum(len(x["tris"]) for _, m, _ in placed for x in m["materials"])
        print("%s: %d props placed (%d meshes), %s triangles -> %s (%d textures written)"
              % (path.name, len(placed), len(cache), format(tris, ","), obj, n))
    return 0


def cmd_render(args):
    """Two checks: a front-on gallery of every mesh, and every instance placed
    through its transform, drawn top-down over the level."""
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        cont = Container(path)
        L = Level(cont, path.stem)
        meshes = [L.mesh(n) for n in L.mesh_nodes]
        # gallery
        S = 180
        cols = 8
        rows = (len(meshes) + cols - 1) // cols or 1
        W, H = S * cols, S * rows
        buf = bytearray(b"\x16\x18\x20\xff" * (W * H))
        for k, m in enumerate(meshes):
            ox, oy = (k % cols) * S, (k // cols) * S

            def put(x, y, rgb, ox=ox, oy=oy):
                if ox <= x < ox + S and oy <= y < oy + S:
                    i = (y * W + x) * 4
                    buf[i:i + 3] = bytes(rgb)
            tris = [t for mat in m["materials"] for t in mat["tris"]
                    if _valid(m, t)[0]]
            if not tris:
                continue
            P = m["pos"]
            xs = [P[v[0]][0] for t in tris for v in t]
            ys = [P[v[0]][1] for t in tris for v in t]
            sc = (S - 20) / max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
            x0, y1 = min(xs), max(ys)
            for t in sorted(tris, key=lambda t: sum(P[v[0]][2] for v in t)):
                a, b, c = (P[v[0]] for v in t)
                u = [b[i] - a[i] for i in range(3)]
                w = [c[i] - a[i] for i in range(3)]
                n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2],
                     u[0] * w[1] - u[1] * w[0])
                ln = math.sqrt(sum(x * x for x in n)) or 1
                sh = 0.25 + 0.75 * abs((0.4 * n[0] + 0.7 * n[1] + 0.6 * n[2]) / ln)
                rgb = (int(90 + 150 * sh), int(110 + 120 * sh), int(130 + 90 * sh))
                _fill_tri(put, [(int(ox + 10 + (p[0] - x0) * sc),
                                 int(oy + 10 + (y1 - p[1]) * sc)) for p in (a, b, c)], rgb)
        g = args.output / (path.stem + "_meshes.png")
        write_png(g, W, H, bytes(buf))
        # placed instances top-down
        vs = [q for tri in world_triangles(cont) for q in tri]
        lo = [min(v[i] for v in vs) for i in range(3)]
        hi = [max(v[i] for v in vs) for i in range(3)]
        W2 = args.size
        s = (W2 - 20) / max(1e-6, hi[0] - lo[0])
        H2 = max(64, min(4096, int((hi[2] - lo[2]) * s) + 20))
        buf2 = bytearray(b"\x0e\x10\x16\xff" * (W2 * H2))

        def put2(x, z, rgb):
            if 0 <= x < W2 and 0 <= z < H2:
                i = (z * W2 + x) * 4
                buf2[i:i + 3] = bytes(rgb)

        def px(p):
            return int(10 + (p[0] - lo[0]) * s), int(10 + (p[2] - lo[2]) * s)
        for tri in world_triangles(cont):
            _fill_tri(put2, [px(q) for q in tri], (52, 56, 66))
        bym = {m["node"]: m for m in meshes}
        placed = 0
        for inst in L.instances:
            m = bym.get(inst["mesh"])
            if not m or not inst["rot"]:
                continue
            placed += 1
            xf = (inst["rot"], inst["pos"])
            for mat in m["materials"]:
                for t in mat["tris"]:
                    if _valid(m, t)[0]:
                        _fill_tri(put2, [px(apply_xform(xf, m["pos"][v[0]])) for v in t],
                                  (120, 210, 120))
        d = args.output / (path.stem + "_props.png")
        write_png(d, W2, H2, bytes(buf2))
        print("%s: gallery of %d meshes -> %s; %d instances placed -> %s"
              % (path.name, len(meshes), g, placed, d))
    return 0


def cmd_verify(args):
    roots = args.paths or [Path("orig/files/Levels")]
    files = []
    for r in roots:
        r = Path(r)
        files.extend(sorted(r.rglob("*.ssw")) if r.is_dir() else [r])
    tot = Counter()
    names = Counter()
    for f in sorted(set(files)):
        L = Level(Container(f), f.stem)
        tot["levels"] += 1
        tot["meshes"] += len(L.mesh_nodes)
        for node in L.mesh_nodes:
            m = L.mesh(node)
            tris = [t for mat in m["materials"] for t in mat["tris"]]
            tot["materials"] += len(m["materials"])
            tot["triangles"] += len(tris)
            tot["bad position refs"] += sum(1 for t in tris if not _valid(m, t)[0])
            tot["bad texcoord refs"] += sum(1 for t in tris if not _valid(m, t)[1])
            tot["unparsed lists"] += m["unparsed"]
            tot["meshes with no triangles"] += not tris
            tot["materials with a texture"] += sum(
                1 for x in m["materials"] if x["diffuse"] is not None)
            names[m["name"]] += 1
        for inst in L.instances:
            tot["instances"] += 1
            if inst["path"]:
                tot["instances with a .clm"] += 1
                tot["... linked to a mesh"] += inst["mesh"] is not None
                # models\editor\axis.clm is an editor gizmo with no mesh in
                # the shipped image: expected to stay unlinked
                tot["... editor placeholders"] += (
                    inst["mesh"] is None and "editor" in inst["path"].lower())
        mixed = sum(1 for c in L.names.values() if len(c) > 1)
        mixed += sum(1 for c in L.damage_names.values() if len(c) > 1)
        tot["meshes shared across .clm names"] += mixed
        tot["damage-state meshes"] += len(L.damage_names)
        tot["meshes named by no instance"] += sum(
            1 for n in L.mesh_nodes if n not in L.names and n not in L.damage_names)
        tot["instances with damage states"] += sum(1 for m in L.instances if m["damage"])
    for k in ("levels", "meshes", "materials", "materials with a texture",
              "triangles", "meshes with no triangles", "bad position refs",
              "bad texcoord refs", "unparsed lists", "instances",
              "instances with a .clm", "... linked to a mesh",
              "... editor placeholders", "meshes shared across .clm names",
              "instances with damage states", "damage-state meshes",
              "meshes named by no instance"):
        print("  %-32s %s" % (k, format(tot[k], ",")))
    print("  %-32s %d" % ("distinct mesh names", len(names)))
    bad = (tot["bad position refs"] + tot["meshes shared across .clm names"]
           + (tot["instances with a .clm"] - tot["... linked to a mesh"]
              - tot["... editor placeholders"]))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="meshes with sizes and instance counts")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("meshes", help="one OBJ + MTL per mesh, local space")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/models"))
    p.set_defaults(func=cmd_meshes)
    p = sub.add_parser("props", help="every placed instance in one world-space OBJ")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/mesh"))
    p.set_defaults(func=cmd_props)
    p = sub.add_parser("render", help="mesh gallery and top-down placement")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/maps"))
    p.add_argument("--size", type=int, default=1000)
    p.set_defaults(func=cmd_render)
    p = sub.add_parser("verify", help="invariants across levels")
    p.add_argument("paths", nargs="*", type=Path)
    p.set_defaults(func=cmd_verify)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
