#!/usr/bin/env python3
"""
Object models: the enemies, characters, pickups, weapons, projectiles and
debris the game names in LevelModels.cfg ("ObjectModel<N>"). None has a file
of its own. Each level image embeds the ones it uses, and Data.ssg,
FrontEnd.ssg and the Multiplayer skins hold the shared ones. See
docs/image-format.md, "Object models".

Each model is a nested CLIMAX image, named by the pointer just before it:

    struct ObjModel {               // an image: {1, 0x64, ...}
        u32       version;          // +0x00  1
        u32       magic;            // +0x04  0x64
        MeshNode* mesh;             // +0x08  chain via +0x00
        u32       _0c;
        f32*      bounds;           // +0x10  min xyz, max xyz
        u32       _14;
        u32       flag;             // +0x18  1 on debris (gibs, shards)
        CcArray*  pos, nrm, clr, uv;  // +0x1c..+0x28
        u32       kind;             // +0x2c  0 static; 3, 5 or 7 animated
        Bone*     bones;            // +0x30  chain {next, name*, u32 index, ...}
        Anim*     anims;            // +0x34  chain {next, name*, 0, u32 tracks, ...}
    };
    struct MeshNode {               // one per texture
        MeshNode* next;             // +0x00
        char*     textureName;      // +0x04
        u32       _08;
        ListRecord* lists;          // +0x0c  record chain, in draw order
        CcTexture* texture;         // +0x10
        ...
    };
    struct ListRecord {             // one per display list
        ListRecord* next;           // 0 on the last
        u32   hasPalette;           // 1 on type-0 records
        Desc* desc;                 // = this + 0x1c
        u16 (*palette)[2];          // {bone, pnmtx} pairs
        u32   count;
        u32   _14, _18;
        Desc  descriptor;           // {ptr, paddedSize, usedSize, vertexType}
    };

Animation (docs/image-format.md, "Animation"): a frame is one u16 key index
per bone into the model's key pool {keys*, count, sx, sy, sz}; a key is ten
s16 decoding to a 3x4 skinning matrix (bind pose -> posed). Vertices are
skinned on the CPU from groups in the bone records, or on the GPU through
the type-0 palettes, whose slot -> bone map persists through the whole model
in draw order.

Static models share all four arrays with a library image. Characters own
their positions and normals, and share colours and UVs.

Display lists are GX, as in the levels:
    vertex type 5, stride 7: {u16 pos, u16 nrm, u8 clr, u16 uv}
    vertex type 0, stride 8: {u8 pnmtx, u16 pos, u16 nrm, u8 clr, u16 uv}
pnmtx takes only 0, 3, ..., 27 -- GX's PNMTX0..9. A type-0 list is the same
triangles as the model's type-5 lists, cut into pieces that each use at most
ten bone matrices (GPU skinning); positions are the bind pose in model space.

Usage:
    python tools/objmodel.py list   orig/files/Levels/Alevel9_1.ssw
    python tools/objmodel.py export [files] -o build/objmodels
    python tools/objmodel.py render [files] -o build/maps
    python tools/objmodel.py pose   [files] -o build/maps   # posed mid-animation
    python tools/objmodel.py verify [files]
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container                                        # noqa: E402
from level import _parse_prims, _to_triangles, PRIMS            # noqa: E402
from entity import _fill_tri                                     # noqa: E402
from gxtex import walk as texwalk, decode, write_png             # noqa: E402
from model import _array, _write_obj                             # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAGIC = (1, 0x64)
H_MESH, H_BOUNDS, H_FLAG = 0x08, 0x10, 0x18
H_POS, H_NRM, H_CLR, H_UV = 0x1C, 0x20, 0x24, 0x28
H_KIND, H_BONES, H_ANIMS = 0x2C, 0x30, 0x34
M_NEXT, M_TEXNAME, M_LISTS, M_TEX = 0x00, 0x04, 0x0C, 0x10   # M_LISTS: record chain head
NAME_REACH = 0x40           # the name pointer sits within this before a header
EXTENT_CAP = 0x100000       # the last model in address order
# vertex type -> (stride, pnmtx field or None, pos, nrm, uv field offsets)
LAYOUT = {5: (7, None, 0, 2, 5), 0: (8, 0, 1, 3, 6)}
CFG = ROOT / "build" / "cfg" / "LevelModels.json"

# Animation. A frame is one u16 key index per bone (frame table entries are
# 0x14 bytes, frame pointer first). A key is ten s16, decoded by the frame
# build at 0x80129334 (GQR5 = s16, scale 0; the scaling is explicit):
#     c0 = k[0:3]/4096, c1 = k[3:6]/4096, c2 = (c0 x c1) * k[6]/2048,
#     t = k[7:10] * (sx, sy, sz),  M = [c0 c1 c2 | t], v' = M v.
# The keys are skinning matrices (bind pose -> posed), so no hierarchy is
# needed to pose a vertex.
KEY_SIZE, FRAME_ENTRY = 20, 0x14
ROT_SCALE, KEY_SCALE = 1 / 4096, 1 / 2048
# CPU skin groups in each bone record: positions and normals, each a count,
# model-space points and u32 vertex indices; positions also weights.
B_NPOS, B_POS, B_PIDX, B_WEIGHT = 0x0C, 0x10, 0x14, 0x18
B_NNRM, B_NRM, B_NIDX = 0x28, 0x2C, 0x30
# Every display list sits in a record {next, flag, desc*, palette*, count, 0,
# 0, descriptor, palette[count]}; the palette is {u16 bone, u16 pnmtx} pairs
# and only type-0 records have one. Records chain by `next` in draw order.
REC_PALETTE, REC_COUNT, REC_DESC = 0x0C, 0x10, 0x1C
IDENTITY = ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], (0.0, 0.0, 0.0))


def _apply(mx, p):
    R, t = mx
    return tuple(R[i][0] * p[0] + R[i][1] * p[1] + R[i][2] * p[2] + t[i] for i in range(3))


def _stretch(verts, bind, tris):
    """Median and 95th percentile of |posed edge / bind edge - 1|."""
    r = []
    for t in tris:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            l0 = math.dist(bind[a], bind[b])
            if l0 > 1e-4:
                r.append(abs(math.dist(verts[a], verts[b]) / l0 - 1))
    if not r:
        return 0.0, 0.0
    r.sort()
    return r[len(r) // 2], r[int(len(r) * 0.95)]


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def u16(img, o):
    return struct.unpack_from(">H", img, o)[0]


class Models:
    """Every object model embedded in one container."""

    def __init__(self, container, stem):
        self.stem = stem
        self.img = img = container.image
        self.ptrs = sorted(container.pointer_offsets())
        self.pset = set(self.ptrs)
        self.texs = list(texwalk(img))
        self.texidx = {t.offset: i for i, t in enumerate(self.texs)}
        heads = []
        for p in self.ptrs:
            h = p - 8
            if h >= 4 and (u32(img, h), u32(img, h + 4)) == MAGIC:
                nm = self._name_before(h)
                if nm:
                    heads.append((h, nm))
        heads.sort()
        self.models = [self._model(h, nm, heads[k + 1][0] if k + 1 < len(heads)
                                   else min(h + EXTENT_CAP, len(img)))
                       for k, (h, nm) in enumerate(heads)]

    def _cstr(self, o):
        img = self.img
        if not (0 < o < len(img)):
            return None
        e = img.find(b"\0", o, o + 80)
        s = img[o:e] if e > o else b""
        return s.decode("latin-1") if s and all(32 <= b < 127 for b in s) else None

    def _name_before(self, h):
        for back in range(4, NAME_REACH, 4):
            if h - back in self.pset:
                s = self._cstr(u32(self.img, h - back))
                if s:
                    return s
        return None

    def _chain(self, head, limit=256):
        out, node = [], head
        while node and node not in out and len(out) < limit and 0 < node < len(self.img):
            out.append(node)
            node = u32(self.img, node) if node in self.pset else 0
        return out

    def _model(self, h, name, end):
        img = self.img
        m = {"name": name, "header": h, "end": end,
             "flag": u32(img, h + H_FLAG), "kind": u32(img, h + H_KIND),
             "pos": _array(img, u32(img, h + H_POS)),
             "nrm": _array(img, u32(img, h + H_NRM)),
             "uv": _array(img, u32(img, h + H_UV)),
             "bounds": struct.unpack_from(">6f", img, u32(img, h + H_BOUNDS)),
             "bones": [], "anims": []}
        nodes = self._chain(u32(img, h + H_MESH))
        m["meshes"] = [{"node": nd, "texture": self.texidx.get(u32(img, nd + M_TEX)),
                        "texture_name": self._cstr(u32(img, nd + M_TEXNAME)),
                        "lists": {0: [], 5: []}, "other_lists": 0}
                       for nd in nodes]
        order = sorted((x["node"], i) for i, x in enumerate(m["meshes"]))
        starts = [a for a, _ in order]
        lo, hi = bisect.bisect_left(self.ptrs, h), bisect.bisect_left(self.ptrs, end)
        for o in self.ptrs[lo:hi]:
            p = u32(img, o)
            if not (0 < p < len(img) - 8) or (img[p] & 0xF8) not in PRIMS:
                continue
            size, used, vt = u32(img, o + 4), u32(img, o + 8), u32(img, o + 12)
            if vt > 15 or not (used <= size <= used + 32) or not order:
                continue
            # a list belongs to the mesh node it follows
            k = max(bisect.bisect_right(starts, o) - 1, 0)
            mesh = m["meshes"][order[k][1]]
            if vt in LAYOUT:
                mesh["lists"][vt].append((p, used, o))
            else:
                mesh["other_lists"] += 1
        if m["kind"]:
            for b in self._chain(u32(img, h + H_BONES)):
                m["bones"].append({"index": u32(img, b + 8), "record": b,
                                   "name": self._cstr(u32(img, b + 4))})
            for a in self._chain(u32(img, h + H_ANIMS)):
                m["anims"].append({"name": self._cstr(u32(img, a + 4)),
                                   "frames": u32(img, a + 12),
                                   "table": u32(img, a + 16)})
        return m

    def triangles(self, mesh, vt):
        """((pos, nrm, uv), ...) triples, or None when a list fails to tile."""
        img, out = self.img, []
        stride, _fm, fp, fn, fu = LAYOUT[vt]
        for p, used, _o in mesh["lists"][vt]:
            prims = _parse_prims(img, p, used, stride)
            if not prims:
                return None
            for op, cnt, base in prims:
                vs = [tuple(u16(img, base + stride * v + f) for f in (fp, fn, fu))
                      for v in range(cnt)]
                for a, b, c in _to_triangles(op, list(range(cnt))):
                    out.append((vs[a], vs[b], vs[c]))
        return out

    def geometry(self, m):
        """The model as model.py's mesh dicts, compacted to the positions and
        UVs its own lists use -- a static model indexes a slice of arrays it
        shares with every other model in the image. Type-5 lists, or type 0
        when a model has none. Normal indices stay raw (the OBJ omits them)."""
        pmap, umap, pos, uv, mats = {}, {}, [], [], []
        for mesh in m["meshes"]:
            vt = 5 if mesh["lists"][5] else 0
            tris = []
            for t in self.triangles(mesh, vt) or []:
                if any(v[0] >= len(m["pos"]) or v[2] >= len(m["uv"]) for v in t):
                    continue
                out = []
                for p, n, u in t:
                    if p not in pmap:
                        pmap[p] = len(pos)
                        pos.append(m["pos"][p])
                    if u not in umap:
                        umap[u] = len(uv)
                        uv.append(m["uv"][u])
                    out.append((pmap[p], n, umap[u]))
                tris.append(tuple(out))
            mats.append({"name": mesh["texture_name"] or "", "diffuse": mesh["texture"],
                         "tris": tris})
        return {"name": m["name"], "pos": pos, "uv": uv, "materials": mats}

    # --- animation (docs/image-format.md, "Animation") ----------------------

    def frame_keys(self, m, anim, frame):
        """One frame: a key index per bone, in bone-chain order."""
        fp = u32(self.img, anim["table"] + FRAME_ENTRY * (frame % anim["frames"]))
        return [u16(self.img, fp + 2 * j) for j in range(len(m["bones"]))]

    def key_pool(self, m):
        """{keys, count, scale}: the pointer inside the model whose count is
        one past the largest key index its frames use (exact on every
        animated model), followed by the per-axis translation scales."""
        if not m["anims"]:
            return None
        top = max(max(self.frame_keys(m, a, f)) for a in m["anims"]
                  for f in range(a["frames"]))
        img = self.img
        lo = bisect.bisect_left(self.ptrs, m["header"])
        hi = bisect.bisect_left(self.ptrs, m["end"])
        # A count alone is ambiguous for tiny pools (a static one-bone rig has
        # count 1), so candidates are scored: a real pool's keys have
        # orthogonal rotation columns and its scales are small floats >= 0 --
        # exactly 0 on a rig that never translates (Alevel12_4's Pelvis: one
        # identity key, scales 0, 0, 0).
        best = None
        for o in self.ptrs[lo:hi]:
            if o + 20 > len(img) or u32(img, o + 4) != top + 1:
                continue
            keys, sc = u32(img, o), struct.unpack_from(">3f", img, o + 8)
            if not all(0 <= s < 10 for s in sc) or keys + KEY_SIZE * (top + 1) > len(img):
                continue
            good = 0
            for k in range(top + 1):
                v = struct.unpack_from(">6h", img, keys + KEY_SIZE * k)
                c0, c1 = [x * ROT_SCALE for x in v[:3]], [x * ROT_SCALE for x in v[3:]]
                good += (abs(sum(a * b for a, b in zip(c0, c1))) < 0.02
                         and abs(math.sqrt(sum(x * x for x in c0)) - 1) < 0.2)
            score = good / (top + 1)
            if best is None or score > best[0]:
                best = (score, keys, sc)
        if best is None or best[0] < 0.5:
            return None
        return {"keys": best[1], "count": top + 1, "scale": best[2]}

    def key_matrix(self, pool, k, columns=True):
        """Key k as a row-major 3x4 matrix. columns=False is the wrong
        reading (the six rotation values as rows), kept for verify."""
        v = struct.unpack_from(">10h", self.img, pool["keys"] + KEY_SIZE * k)
        c0 = [x * ROT_SCALE for x in v[0:3]]
        c1 = [x * ROT_SCALE for x in v[3:6]]
        s = v[6] * KEY_SCALE
        c2 = [(c0[1] * c1[2] - c0[2] * c1[1]) * s, (c0[2] * c1[0] - c0[0] * c1[2]) * s,
              (c0[0] * c1[1] - c0[1] * c1[0]) * s]
        t = tuple(v[7 + i] * pool["scale"][i] for i in range(3))
        rows = [[c0[i], c1[i], c2[i]] for i in range(3)] if columns else [c0, c1, c2]
        return rows, t

    def skin(self, m):
        """CPU skin groups: [(bone, vertex, weight, bind point)]."""
        img, out = self.img, []
        for bi, b in enumerate(m["bones"]):
            r = b["record"]
            n = u32(img, r + B_NPOS)
            if not n or r + B_POS not in self.pset:
                continue
            pp, ip, wp = (u32(img, r + f) for f in (B_POS, B_PIDX, B_WEIGHT))
            for k in range(n):
                out.append((bi, u32(img, ip + 4 * k),
                            struct.unpack_from(">f", img, wp + 4 * k)[0],
                            struct.unpack_from(">3f", img, pp + 12 * k)))
        return out

    def slot_maps(self, m):
        """{type-0 descriptor offset: {pnmtx slot: bone}} for a model.

        Draw order is the mesh-node chain, and within each node the record
        chain its +0x0c heads. GX matrix memory persists through the whole
        model -- across lists and across mesh nodes -- and each list reloads
        only the slots it changes: the sniper rifle's fourth mesh draws with
        slots its three predecessors loaded. Carried per mesh in address
        order instead, 22,741 type-0 vertices on the disc point at slots
        nothing loaded. A chain's last record has next = 0, which is not a
        relocated pointer, so records are recognised by their desc* field."""
        img, slots, out = self.img, {}, {}
        for mesh in m["meshes"]:
            r, seen = u32(img, mesh["node"] + M_LISTS), set()
            while r and r not in seen and r + 8 in self.pset:
                seen.add(r)
                desc = u32(img, r + 8)
                if desc == r + REC_DESC and u32(img, desc + 12) == 0:
                    for j in range(u32(img, r + REC_COUNT)):
                        bone, slot = struct.unpack_from(
                            ">HH", img, u32(img, r + REC_PALETTE) + 4 * j)
                        slots[slot] = bone
                    out[desc] = dict(slots)
                r = u32(img, r)
        return out

    def posed(self, m, mats):
        """(vertices, triangles) with one 3x4 matrix per bone: type-0 lists
        through their palettes where a mesh has them, else its type-5 lists
        over the CPU-skinned positions. Vertices no skin group names keep the
        bind pose."""
        img, P = self.img, m["pos"]
        base, acc = list(P), {}
        for bi, v, w, p in self.skin(m):
            q = _apply(mats[bi], p)
            a = acc.setdefault(v, [0.0, 0.0, 0.0])
            for i in range(3):
                a[i] += w * q[i]
        for v, a in acc.items():
            base[v] = tuple(a)
        verts, tris = list(base), []
        maps = self.slot_maps(m) if any(x["lists"][0] for x in m["meshes"]) else {}
        for mesh in m["meshes"]:
            if mesh["lists"][0]:
                work = [(lst, maps.get(lst[2], {}), 0) for lst in mesh["lists"][0]]
            else:
                work = [(lst, None, 5) for lst in mesh["lists"][5]]
            for (p, used, _o), slots, vt in work:
                stride, fm, fp, _fn, _fu = LAYOUT[vt]
                for op, cnt, bs in _parse_prims(img, p, used, stride) or []:
                    ids = []
                    for k in range(cnt):
                        pi = u16(img, bs + stride * k + fp)
                        if vt == 0:
                            bone = slots.get(img[bs + stride * k + fm])
                            verts.append(_apply(mats[bone] if bone is not None
                                                else IDENTITY, P[pi]))
                            ids.append(len(verts) - 1)
                        else:
                            ids.append(pi)
                    for a, b, c in _to_triangles(op, list(range(cnt))):
                        tris.append((ids[a], ids[b], ids[c]))
        return verts, tris

    def texname(self, i):
        t = self.texs[i]
        return "%s_%03d_%s_%dx%d.png" % (self.stem, i, t.name, t.width, t.height)

    def write_texture(self, dest, i):
        path = dest / self.texname(i)
        if not path.exists():
            t = self.texs[i]
            write_png(path, t.width, t.height,
                      bytes(decode(self.img, t.pixels, t.width, t.height, t.gx)))
        return path.name


BITS_PER_TEXEL = {14: 4, 2: 8, 4: 16, 5: 16, 6: 32}   # GX CMPR, IA4, RGB565, RGB5A3, RGBA8


def fingerprint(M, g):
    """Content key over the compacted geometry and the texture's bytes, so the
    same model in two containers exports once."""
    h = hashlib.sha1()
    for p in g["pos"]:
        h.update(struct.pack(">3f", *p))
    for t in g["uv"]:
        h.update(struct.pack(">2f", *t))
    for mat in g["materials"]:
        h.update(repr([(v[0], v[2]) for t in mat["tris"] for v in t]).encode())
        if mat["diffuse"] is not None:
            t = M.texs[mat["diffuse"]]
            n = t.width * t.height * BITS_PER_TEXEL.get(t.gx, 16) // 8
            h.update(struct.pack(">III", t.width, t.height, t.fmt))
            h.update(M.img[t.pixels:t.pixels + n])
    return h.hexdigest()[:12]


def _files(paths):
    roots = [Path(p) for p in paths] or [ROOT / "orig" / "files"]
    out = []
    for r in roots:
        out.extend(sorted(r.rglob("*.ss[gw]")) if r.is_dir() else [r])
    return out


def cfg_names():
    """LevelModels.cfg's model names, as the game would look them up."""
    if not CFG.exists():
        return []
    cfg = json.loads(CFG.read_text())
    return sorted({v["value"] for sec in cfg.values() if isinstance(sec, dict)
                   for v in sec.values() if isinstance(v, dict)
                   and isinstance(v.get("value"), str) and v["value"]})


# --- subcommands ------------------------------------------------------------

def cmd_list(args):
    for path in _files(args.files):
        M = Models(Container(path), path.stem)
        print("=== %s: %d object models ===" % (path.name, len(M.models)))
        for m in M.models:
            g = M.geometry(m)
            tris = sum(len(x["tris"]) for x in g["materials"])
            print("  %-28s %5d verts %5d tris  %d mesh(es)  kind %d  %2d bones  %2d anims%s"
                  % (m["name"], len(m["pos"]), tris, len(m["meshes"]), m["kind"],
                     len(m["bones"]), len(m["anims"]),
                     "  " + ", ".join(a["name"] or "?" for a in m["anims"][:6]) if m["anims"] else ""))
    return 0


def cmd_export(args):
    out = args.output
    tex_dir = out / "textures"
    tex_dir.mkdir(parents=True, exist_ok=True)
    index = {"models": {}, "containers": {}}
    seen = {}
    variants = Counter()
    for path in _files(args.files):
        M = Models(Container(path), path.stem)
        here = index["containers"][path.stem] = []
        for m in M.models:
            g = M.geometry(m)
            key = fingerprint(M, g)
            if key not in seen:
                # numbered by the filename, case-folded: the disc has both
                # "firecracker" and "Firecracker", one file on Windows. Not
                # "name~2": that is an 8.3 short-name alias, and opening
                # "demonA~2.obj" overwrote whichever long name owned it.
                base = re.sub(r"[^A-Za-z0-9_.-]", "_", m["name"])
                variants[base.lower()] += 1
                k = variants[base.lower()]
                label = base if k == 1 else "%s-v%d" % (base, k)
                seen[key] = label
                mats = {}
                with open(out / (label + ".obj"), "w") as fh, \
                        open(out / (label + ".mtl"), "w") as mtl:
                    fh.write("# %s from %s\nmtllib %s.mtl\n" % (m["name"], path.name, label))
                    mats = _write_obj(fh, None, [(label, g, None)], label)
                    mtl.write("# %s\n" % m["name"])
                    for mk, diffuse in sorted(mats.items()):
                        mtl.write("\nnewmtl %s\nKd 0.8 0.8 0.8\n" % mk)
                        if diffuse is not None:
                            mtl.write("map_Kd textures/%s\n" % M.write_texture(tex_dir, diffuse))
                index["models"][label] = {
                    "name": m["name"], "from": path.stem, "kind": m["kind"],
                    "vertices": len(g["pos"]),
                    "triangles": sum(len(x["tris"]) for x in g["materials"]),
                    "bones": [b["name"] for b in m["bones"]],
                    "anims": [a["name"] for a in m["anims"]]}
            here.append(seen[key])
        print("%s: %d models" % (path.name, len(M.models)))
    (out / "index.json").write_text(json.dumps(index, indent=1))
    print("%d distinct models (%d names) -> %s"
          % (len(index["models"]), len(variants), out))
    # every label must be its own file: Windows folds case and resolves 8.3
    # aliases, and either silently merges two writes into one file
    on_disk = {p.stem for p in out.glob("*.obj")}
    lost = sorted(set(index["models"]) - on_disk)
    if lost:
        print("PROBLEM %d models share a file with another: %s" % (len(lost), lost[:8]))
        return 1
    return 0


def cmd_render(args):
    """Front-on gallery of every model in a container: blue-grey static,
    orange for models that carry type-0 (skinned) lists."""
    args.output.mkdir(parents=True, exist_ok=True)
    for path in _files(args.files):
        M = Models(Container(path), path.stem)
        S, cols = 200, 8
        rows = (len(M.models) + cols - 1) // cols or 1
        W, H = S * cols, S * rows
        buf = bytearray(b"\x16\x18\x20\xff" * (W * H))
        for k, m in enumerate(M.models):
            ox, oy = (k % cols) * S, (k // cols) * S

            def put(x, y, rgb, ox=ox, oy=oy):
                if ox <= x < ox + S and oy <= y < oy + S:
                    i = (y * W + x) * 4
                    buf[i:i + 3] = bytes(rgb)
            g = M.geometry(m)
            P = g["pos"]
            tris = [t for mat in g["materials"] for t in mat["tris"]]
            if not tris:
                continue
            skinned = any(x["lists"][0] for x in m["meshes"])
            xs = [P[v[0]][0] for t in tris for v in t]
            ys = [P[v[0]][1] for t in tris for v in t]
            sc = (S - 20) / max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
            x0, y1 = min(xs), max(ys)
            for t in sorted(tris, key=lambda t: sum(P[v[0]][2] for v in t)):
                a, b, c = (P[v[0]] for v in t)
                uu = [b[i] - a[i] for i in range(3)]
                ww = [c[i] - a[i] for i in range(3)]
                nn = (uu[1] * ww[2] - uu[2] * ww[1], uu[2] * ww[0] - uu[0] * ww[2],
                      uu[0] * ww[1] - uu[1] * ww[0])
                sh = 0.25 + 0.75 * abs((0.4 * nn[0] + 0.7 * nn[1] + 0.6 * nn[2])
                                       / (math.sqrt(sum(x * x for x in nn)) or 1))
                rgb = ((int(120 + 130 * sh), int(80 + 90 * sh), int(60 + 50 * sh)) if skinned
                       else (int(90 + 150 * sh), int(110 + 120 * sh), int(130 + 90 * sh)))
                _fill_tri(put, [(int(ox + 10 + (p[0] - x0) * sc),
                                 int(oy + 10 + (y1 - p[1]) * sc)) for p in (a, b, c)], rgb)
        dest = args.output / (path.stem + "_objmodels.png")
        write_png(dest, W, H, bytes(buf))
        print("%s: %d models -> %s" % (path.name, len(M.models), dest))
        for k, m in enumerate(M.models):
            print("  %2d %s" % (k, m["name"]))
    return 0


def _gallery(dest, cells, S=200, cols=6):
    """Front-on shaded gallery of (vertices, triangles) cells."""
    rows = (len(cells) + cols - 1) // cols or 1
    W, H = S * cols, S * rows
    buf = bytearray(b"\x16\x18\x20\xff" * (W * H))
    for k, (V, T) in enumerate(cells):
        ox, oy = (k % cols) * S, (k // cols) * S

        def put(x, y, rgb, ox=ox, oy=oy):
            if ox <= x < ox + S and oy <= y < oy + S:
                i = (y * W + x) * 4
                buf[i:i + 3] = bytes(rgb)
        if not T:
            continue
        xs = [V[v][0] for t in T for v in t]
        ys = [V[v][1] for t in T for v in t]
        sc = (S - 20) / max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
        x0, y1 = min(xs), max(ys)
        for t in sorted(T, key=lambda t: sum(V[v][2] for v in t)):
            a, b, c = (V[v] for v in t)
            uu = [b[i] - a[i] for i in range(3)]
            ww = [c[i] - a[i] for i in range(3)]
            nn = (uu[1] * ww[2] - uu[2] * ww[1], uu[2] * ww[0] - uu[0] * ww[2],
                  uu[0] * ww[1] - uu[1] * ww[0])
            sh = 0.25 + 0.75 * abs((0.4 * nn[0] + 0.7 * nn[1] + 0.6 * nn[2])
                                   / (math.sqrt(sum(x * x for x in nn)) or 1))
            _fill_tri(put, [(int(ox + 10 + (p[0] - x0) * sc), int(oy + 10 + (y1 - p[1]) * sc))
                            for p in (a, b, c)],
                      (int(120 + 130 * sh), int(80 + 90 * sh), int(60 + 50 * sh)))
    write_png(dest, W, H, bytes(buf))


def cmd_pose(args):
    """Each animated model at the middle frame of its first three
    animations, posed with the decoded keys and skinning."""
    args.output.mkdir(parents=True, exist_ok=True)
    for path in _files(args.files):
        M = Models(Container(path), path.stem)
        cells, labels = [], []
        for m in M.models:
            pool = M.key_pool(m) if m["kind"] else None
            if pool is None:
                continue
            for a in m["anims"][:3]:
                fr = a["frames"] // 2
                cells.append(M.posed(m, [M.key_matrix(pool, k)
                                         for k in M.frame_keys(m, a, fr)]))
                labels.append("%s / %s frame %d" % (m["name"], a["name"], fr))
        dest = args.output / (path.stem + "_posed.png")
        _gallery(dest, cells)
        print("%s: %d poses -> %s" % (path.name, len(cells), dest))
        for k, lab in enumerate(labels):
            print("  %2d %s" % (k, lab))
    return 0


def verify_animation(M, tot, problems, stretched, unbound):
    """Animation checks for one container."""
    img = M.img
    for m in M.models:
        if not m["kind"]:
            continue
        pool = M.key_pool(m)
        tot["anim: key pool found"] += pool is not None
        if pool is None:
            problems.append("%s/%s: no key pool" % (M.stem, m["name"]))
            continue
        tot["anim: keys"] += pool["count"]
        for k in range(pool["count"]):
            v = struct.unpack_from(">6h", img, pool["keys"] + KEY_SIZE * k)
            c0 = [x * ROT_SCALE for x in v[:3]]
            c1 = [x * ROT_SCALE for x in v[3:]]
            tot["anim: keys, columns orthogonal"] += abs(sum(a * b for a, b in zip(c0, c1))) < 0.01
            tot["anim: keys, columns unit"] += (abs(math.sqrt(sum(x * x for x in c0)) - 1) < 0.01
                                                and abs(math.sqrt(sum(x * x for x in c1)) - 1) < 0.01)
        P, wsum = m["pos"], defaultdict(float)
        for _bi, v, w, p in M.skin(m):
            tot["skin: refs"] += 1
            tot["skin: point == bind position"] += v < len(P) and all(
                abs(p[i] - P[v][i]) < 1e-4 for i in range(3))
            wsum[v] += w
        tot["skin: vertices"] += len(wsum)
        tot["skin: weights sum to 1"] += sum(1 for s in wsum.values() if abs(s - 1) < 1e-3)
        drawn5 = set()
        maps = M.slot_maps(m)
        for mesh in m["meshes"]:
            if mesh["lists"][0]:
                for p, used, o in mesh["lists"][0]:
                    slots = maps.get(o, {})
                    tot["gpu: lists on a chain"] += o in maps
                    for op, cnt, bs in _parse_prims(img, p, used, 8) or []:
                        tot["gpu: type-0 vertices"] += cnt
                        tot["gpu: slot resolved"] += sum(img[bs + 8 * k] in slots for k in range(cnt))
            else:
                drawn5 |= {v[0] for t in (M.triangles(mesh, 5) or []) for v in t}
        free = drawn5 - set(wsum)
        if free:
            tot["animated models with unbound vertices"] += 1
            if len(unbound) < 10 and m["name"] not in [x.split("/")[1].split(" ")[0] for x in unbound]:
                unbound.append("%s/%s (%d of %d)" % (M.stem, m["name"], len(free), len(drawn5)))
        if m["name"] in stretched:
            continue
        stretched.add(m["name"])
        a = m["anims"][0]
        keys = M.frame_keys(m, a, a["frames"] // 2)
        bind, tris = M.posed(m, [IDENTITY] * len(m["bones"]))
        c95 = _stretch(M.posed(m, [M.key_matrix(pool, k) for k in keys])[0], bind, tris)[1]
        r95 = _stretch(M.posed(m, [M.key_matrix(pool, k, False) for k in keys])[0], bind, tris)[1]
        tot["stretch: models tested"] += 1
        if c95 < r95 - 1e-3:
            tot["stretch: columns tighter"] += 1
        elif r95 < c95 - 1e-3:
            tot["stretch: rows tighter"] += 1
            problems.append("%s/%s: the rows reading stretches less" % (M.stem, m["name"]))
        else:
            tot["stretch: tie (all rigid)"] += 1


def cmd_verify(args):
    tot = Counter()
    problems = []
    names = set()
    no_texture = []
    outside = []
    stretched, unbound = set(), []
    for path in _files(args.files):
        M = Models(Container(path), path.stem)
        tot["containers"] += 1
        for m in M.models:
            tot["models"] += 1
            names.add(m["name"])
            P, N, T = len(m["pos"]), len(m["nrm"]), len(m["uv"])
            # bounds: over the positions the model's own lists use -- static
            # models index a slice of a library array shared by many models
            lo, hi = m["bounds"][:3], m["bounds"][3:]
            pts = M.geometry(m)["pos"]
            inside = all(lo[i] - 1e-3 <= p[i] <= hi[i] + 1e-3 for p in pts for i in range(3))
            tight = bool(pts) and all(
                abs(min(p[i] for p in pts) - lo[i]) < 1e-3
                and abs(max(p[i] for p in pts) - hi[i]) < 1e-3 for i in range(3))
            tot["models inside their bounds"] += inside
            tot["bounds exactly fit"] += bool(tight)
            if not inside and len(outside) < 6:
                outside.append("%s/%s" % (path.stem, m["name"]))
            both = False
            for mesh in m["meshes"]:
                tot["mesh nodes"] += 1
                if mesh["texture"] is None:
                    no_texture.append("%s/%s" % (path.stem, m["name"]))
                tot["lists type 5"] += len(mesh["lists"][5])
                tot["lists type 0"] += len(mesh["lists"][0])
                tot["lists other"] += mesh["other_lists"]
                if not mesh["lists"][5] and not mesh["lists"][0]:
                    tot["mesh nodes with no list"] += 1
                t5, t0 = M.triangles(mesh, 5), M.triangles(mesh, 0)
                if t5 is None or t0 is None:
                    problems.append("%s/%s: a display list does not tile" % (path.stem, m["name"]))
                    continue
                for t in t5 + t0:
                    for p_, n_, u_ in t:
                        tot["index refs"] += 3
                        tot["index refs out of bounds"] += (p_ >= P) + (n_ >= N) + (u_ >= T)
                tot["triangles"] += len(t5) if t5 else len(t0)
                if t5 and t0:
                    both = True
                    same = ({frozenset(v[0] for v in t) for t in t5}
                            == {frozenset(v[0] for v in t) for t in t0})
                    tot["type-0 == type-5 geometry"] += same
                    tot["meshes with both types"] += 1
            tot["skinned models (type-0 lists)"] += both
            tot["animated models (kind > 0)"] += m["kind"] > 0
            tot["animated models with bones"] += m["kind"] > 0 and bool(m["bones"])
            tot["animated models with anims"] += m["kind"] > 0 and bool(m["anims"])
            tot["bones"] += len(m["bones"])
            tot["animations"] += len(m["anims"])
            if m["bones"] and [b["index"] for b in m["bones"]] != list(range(len(m["bones"]))):
                tot["bone chains not numbered 0..n-1"] += 1
        verify_animation(M, tot, problems, stretched, unbound)
    tot["distinct names"] = len(names)
    if tot["gpu: slot resolved"] != tot["gpu: type-0 vertices"]:
        problems.append("%d type-0 vertices use a matrix slot no palette loaded"
                        % (tot["gpu: type-0 vertices"] - tot["gpu: slot resolved"]))
    if tot["skin: point == bind position"] != tot["skin: refs"]:
        problems.append("%d skin points differ from the bind position"
                        % (tot["skin: refs"] - tot["skin: point == bind position"]))
    cfg = cfg_names()
    lower = {n.lower() for n in names}
    found = [n for n in cfg if n.lower() in lower or n.split("/")[-1].lower() in lower]
    tot["LevelModels.cfg names found"] = len(found)
    tot["LevelModels.cfg names"] = len(cfg)
    if tot["index refs out of bounds"]:
        problems.append("%d index refs out of bounds" % tot["index refs out of bounds"])
    if tot["type-0 == type-5 geometry"] != tot["meshes with both types"]:
        problems.append("type-0 and type-5 geometry differ on %d meshes"
                        % (tot["meshes with both types"] - tot["type-0 == type-5 geometry"]))
    # +0x10 is a bounding box on most models, not all: reported, not enforced
    for k in ("containers", "models", "distinct names", "mesh nodes",
              "mesh nodes with no list", "lists type 5", "lists type 0", "lists other",
              "triangles", "index refs", "index refs out of bounds",
              "meshes with both types", "type-0 == type-5 geometry",
              "skinned models (type-0 lists)", "animated models (kind > 0)",
              "animated models with bones", "animated models with anims", "bones",
              "bone chains not numbered 0..n-1", "animations",
              "models inside their bounds", "bounds exactly fit", "LevelModels.cfg names",
              "LevelModels.cfg names found", "anim: key pool found", "anim: keys",
              "anim: keys, columns orthogonal", "anim: keys, columns unit", "skin: refs",
              "skin: point == bind position", "skin: vertices", "skin: weights sum to 1",
              "gpu: lists on a chain", "gpu: type-0 vertices", "gpu: slot resolved",
              "animated models with unbound vertices", "stretch: models tested",
              "stretch: columns tighter", "stretch: tie (all rigid)", "stretch: rows tighter"):
        print("  %-34s %s" % (k, format(tot[k], ",")))
    print("  mesh nodes without a texture       %d  %s" % (len(no_texture), no_texture[:4]))
    print("  outside their +0x10 box, e.g.      %s" % outside)
    print("  unbound vertices, e.g.             %s" % unbound)
    print("  LevelModels.cfg names not on disc  %s" % [n for n in cfg if n not in found])
    for p in problems[:20]:
        print("  PROBLEM " + p)
    print("%d problems" % len(problems))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, hlp in (("list", cmd_list, "models in each container"),
                          ("export", cmd_export, "distinct models as OBJ + textures"),
                          ("render", cmd_render, "front-on gallery per container"),
                          ("pose", cmd_pose, "animated models posed mid-animation"),
                          ("verify", cmd_verify, "invariants across containers")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("files", nargs="*", type=Path,
                       help="containers or folders (default: orig/files)")
        if name in ("export", "render", "pose"):
            p.add_argument("-o", "--output", type=Path,
                           default=ROOT / "build" / ("objmodels" if name == "export" else "maps"))
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
