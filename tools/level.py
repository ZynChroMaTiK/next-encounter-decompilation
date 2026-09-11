#!/usr/bin/env python3
"""
Level (.ssw / WORLDMESH) reader for Next Encounter.

**Vertex array table** at `image + 0xb4` — 13 entries of

    struct CcArray { void* data; u32 byteSize; u32 count; u32 stride; };

`count * stride == byteSize` holds exactly for every entry on every level, which
makes the table self-verifying: the run of valid entries ends at 13, and the
bytes after it are the exporter's AutoVSS metadata strings. This is the GX
indexed-vertex setup — separate arrays for positions, normals, texture
coordinates and colours, addressed by index. Entry 0 is positions (float3).

**Lights** at `image + 0x34` — a linked list of 84-byte nodes. Field names come
from the game's own debug print at 0x8013f314, `LightDefault %d col=<..>
pos=<..> dir=<..> as=%0.2f ae=%0.2f`:

    struct CcLight {
        CcLight* next;      // +0x00
        u32      type;      // +0x04  1 static, 2 runtime point, 3 directional
        float    col[3];    // +0x08  x255 and clamped at runtime; all-negative
                            //        = subtractive "dark" light
        float    pos[3];    // +0x14
        float    dir[3];    // +0x20  non-zero on exactly the 48 type-3 lights
        float    as;        // +0x2c  attenuation start (SE1: hot-spot)
        float    ae;        // +0x30  attenuation end   (SE1: fall-off)
        u32      index;     // +0x34
        u32      flags;     // +0x38  0, 1 or 2
        ...                 // +0x3c.. runtime; +0x40 receives the light object
    };

Only type 2 lights are instantiated by the level light setup (0x801386cc);
type 3 goes through the directional path (0x8013f0ec); type 1 is never
created there.

**Materials** at `image + 0x08` — the CcMaterial list, per CcMaterial_Setup at
0x8012e37c. Nodes are variable-length and *contain* their display-list
descriptors, so each node's extent [offset, next) is what ties geometry to its
texture.

**Geometry** — GX display lists, reached through descriptors. See the notes on
`_parse_prims` and `_dl_valid`, which record two mistakes worth not repeating.

Usage:
    python tools/level.py arrays    orig/files/Levels/Rlevel1_1.ssw
    python tools/level.py lights    orig/files/Levels/Rlevel1_1.ssw --limit 10
    python tools/level.py materials orig/files/Levels/Rlevel1_1.ssw
    python tools/level.py map       orig/files/Levels/Rlevel1_1.ssw -o build/maps/
    python tools/level.py obj       orig/files/Levels/Rlevel1_1.ssw -o build/obj/
    python tools/level.py mesh      orig/files/Levels/Rlevel1_1.ssw -o build/mesh/
    python tools/level.py render    orig/files/Levels/Rlevel1_1.ssw -o build/render/
    python tools/level.py verify    orig/files/Levels
"""

from __future__ import annotations

import argparse
import bisect
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container                            # noqa: E402
from gxtex import write_png, walk as _texwalk, decode as _decode  # noqa: E402

ARRAY_TABLE = 0xB4
ARRAY_COUNT = 13
LIGHT_LIST = 0x34
MATERIAL_LIST = 0x08
MAT_DIFFUSE = 0x10          # CcTexture*, per CcMaterial_Setup
MAT_SECOND = 0x18
_MAT_NAME = re.compile(rb"Mat \d+ \([^\)]*\)[^\x00]*")

# Movable geometry lives in its own list at image+0x48: doors, gates and moving
# brushes. Each node is material-like (its own CcTexture at +0x58) and owns its
# display-list descriptors, but its vertices are authored **in local space** --
# every one sits within a few units of the origin -- and the matrix at +0x08
# places it. That field points straight at the owning entity's own transform,
# which keeps the brush and its gameplay entity in step: all 958 Movers and all
# 116 DestroyableArch entities on the disc own a brush this way; 135 brushes
# have no owning entity and carry an inline matrix.
#
# Exporting these without the transform piles every door at the world origin.
SECTOR_LIST = 0x48
SEC_XFORM = 0x08
SEC_DIFFUSE = 0x58

# Render geometry is GX display lists: [opcode][u16 vertexCount][vertex data],
# each vertex a tuple of u16 indices into the CcArray table. A descriptor is a
# pointer field followed by {u32 paddedSize, u32 usedSize, u32 vertexType};
# paddedSize rounds usedSize up to 32, and consecutive lists sit back to back
# (0x452e60 + 384 == 0x452fe0).
#
# Vertex field -> array table slot. Both vertex types share the same field
# order; type 3 simply carries four more fields. Field 2 is not an index at
# all -- it reads as packed bytes (0x0300, 0x0500), consistent with GX's
# matrix-index attributes.
#
# Established by fit rather than assumed: across all 49 levels this map gives
# 478 (level, field) pairs with **zero** out-of-range indices, and 429 of them
# consume the array exactly to its last entry (max index == count - 1).
#
# An earlier attempt used field k -> array k, which fails immediately, and the
# conclusion drawn from that failure -- "fields 1..10 point at different arrays
# on different levels" -- was wrong. The map is uniform; only the *set* of
# arrays that carry real data varies.
FIELD_ARRAY = {0: 0, 1: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9, 9: 10, 10: 11}
F_POS, F_NRM, F_TEX0, F_TEX1, F_TEX2, F_CLR = 0, 1, 3, 4, 5, 6
TEXCOORD_ARRAYS = (4, 5, 6)          # TEX0/1/2; slot 12 has u == 0 throughout
DL_STRIDE = {1: 14, 3: 22}
PRIMS = {0x80: "QUADS", 0x90: "TRIANGLES", 0x98: "TRISTRIP", 0xA0: "TRIFAN"}


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def u16(img, o):
    return struct.unpack_from(">H", img, o)[0]


# --- arrays, lights ---------------------------------------------------------

def arrays(img):
    """Yield (index, data, size, count, stride) for the vertex array table.

    Stops early if an entry fails count*stride==size, so a level with a shorter
    table degrades cleanly instead of reading into the metadata strings.
    """
    for i in range(ARRAY_COUNT):
        o = ARRAY_TABLE + i * 16
        if o + 16 > len(img):
            return
        data, size, count, stride = (u32(img, o), u32(img, o + 4),
                                     u32(img, o + 8), u32(img, o + 12))
        if count * stride != size:
            return
        yield i, data, size, count, stride


def positions(img):
    """Entry 0 of the array table, as float3."""
    for i, data, size, count, stride in arrays(img):
        if i != 0:
            break
        if stride != 12 or not data or data + size > len(img):
            return []
        return [struct.unpack_from(">3f", img, data + n * 12) for n in range(count)]
    return []


def texcoords(img, slot=4):
    """A float2 texcoord array. Slot 4 is TEX0, the diffuse set.

    Values run well outside [0,1] -- 6.0, -8.5 and larger -- because the
    world textures tile (GX_REPEAT). That is expected, not a decode error.
    """
    for i, data, size, count, stride in arrays(img):
        if i != slot:
            continue
        if stride != 8 or not data or data + size > len(img):
            return []
        return [struct.unpack_from(">2f", img, data + n * 8)
                for n in range(count)]
    return []


def lights(img):
    node = u32(img, LIGHT_LIST)
    seen = set()
    while node and node + 0x3C <= len(img) and node not in seen:
        seen.add(node)
        r, g, b = struct.unpack_from(">3f", img, node + 0x08)
        x, y, z = struct.unpack_from(">3f", img, node + 0x14)
        att_start, att_end = struct.unpack_from(">2f", img, node + 0x2C)
        yield {
            "offset": node,
            "type": u32(img, node + 0x04),
            "colour": (r, g, b),
            "pos": (x, y, z),
            "dir": struct.unpack_from(">3f", img, node + 0x20),
            "att_start": att_start,
            "att_end": att_end,
            "range": att_end,                 # kept for older callers
            "index": u32(img, node + 0x34),
            "flags": u32(img, node + 0x38),
        }
        node = u32(img, node)


# --- materials --------------------------------------------------------------

def _chain(img, head, minsize=0x50):
    """Walk a next-pointer list, returning [(offset, next), ...]."""
    node, out, seen = head, [], set()
    while node and node + minsize <= len(img) and node not in seen:
        seen.add(node)
        nxt = u32(img, node)
        out.append((node, nxt))
        node = nxt
    return out


def materials(container):
    """The CcMaterial list at image+0x08.

    Nodes are variable-length and contain their own display-list descriptors,
    so [offset, next) gives ownership -- no proximity matching.

    The last node has no `next`, so its extent has to be closed by hand. Left
    open at len(image) it swallows everything downstream: on Rlevel1_1 that
    handed material 98 all 109 of the movable-geometry descriptors, hiding them
    inside a static material. It is clamped to the start of the sector list.
    """
    img = container.image
    tex = {t.offset: i for i, t in enumerate(_texwalk(img))}
    chain = _chain(img, u32(img, MATERIAL_LIST))
    secs = [off for off, _ in _chain(img, u32(img, SECTOR_LIST), 0x40)]
    for i, (start, nxt) in enumerate(chain):
        if nxt > start:
            end = nxt
        else:
            after = [s for s in secs if s > start]
            end = min(after) if after else len(img)
        m = _MAT_NAME.search(img[start:min(end, start + 0x400)])
        yield {
            "index": i,
            "kind": "material",
            "offset": start,
            "end": end,
            "diffuse": tex.get(u32(img, start + MAT_DIFFUSE)),
            "second": tex.get(u32(img, start + MAT_SECOND)),
            "name": m.group().decode("ascii", "replace") if m else None,
            "xform": None,
        }


def sectors(container):
    """The movable-geometry list at image+0x48 -- doors, gates, moving brushes.

    Each node owns its descriptors like a material does and carries its own
    diffuse texture, plus the transform that places its local-space vertices.
    """
    img = container.image
    n = len(img)
    tex = {t.offset: i for i, t in enumerate(_texwalk(img))}
    chain = _chain(img, u32(img, SECTOR_LIST), 0x40)
    for i, (start, nxt) in enumerate(chain):
        end = nxt if nxt > start else n
        xf = u32(img, start + SEC_XFORM)
        rows = trans = None
        if 0 < xf and xf + 48 <= n:
            f = struct.unpack_from(">12f", img, xf)
            if all(v == v and abs(v) < 1e9 for v in f):
                rows = (f[0:3], f[4:7], f[8:11])
                trans = (f[3], f[7], f[11])
        m = _MAT_NAME.search(img[start:min(end, start + 0x400)])
        yield {
            "index": i,
            "kind": "sector",
            "offset": start,
            "end": end,
            "diffuse": tex.get(u32(img, start + SEC_DIFFUSE)),
            "second": None,
            "name": m.group().decode("ascii", "replace") if m else None,
            "xform": (rows, trans) if rows else None,
        }


def apply_xform(xform, p):
    """Local-space point -> world, through a 3x4 row-major matrix."""
    if xform is None:
        return p
    rows, t = xform
    return tuple(sum(r[k] * p[k] for k in range(3)) + t[j]
                 for j, r in enumerate(rows))


# --- display lists ----------------------------------------------------------

def _parse_prims(img, p, used, stride):
    """A display list is a *sequence* of primitives, not one.

    Assuming one was the costliest mistake here: it silently discarded half the
    descriptors, whose usedSize simply did not equal 3 + n*stride, and pinned
    geometry coverage at 27% instead of 98%. Requiring the primitives to tile
    exactly into usedSize is also a strong validity check in its own right.
    """
    o, end, prims = p, p + used, []
    while o + 3 <= end:
        op = img[o]
        if op == 0:                       # trailing padding
            break
        if (op & 0xF8) not in PRIMS:
            return None
        n = u16(img, o + 1)
        if n == 0 or o + 3 + n * stride > end:
            return None
        prims.append((op, n, o + 3))
        o += 3 + n * stride
    if not prims or not (0 <= end - o < stride + 3):
        return None
    return prims


def _dl_valid(img, npos, base, n, stride) -> bool:
    """Field 0 of every vertex must index the position array.

    Only field 0 is checked, deliberately -- see the DL_STRIDE comment above
    for why a fixed per-field map does not generalise. The real strength of the
    test is that the primitives have to tile exactly into usedSize.
    """
    if base + n * stride > len(img):
        return False
    for v in range(n):
        if u16(img, base + v * stride) >= npos:
            return False
    return True


def display_lists(container):
    """Yield (descriptorOffset, dataOffset, stride, prims).

    Descriptor-driven. Scanning aligned offsets instead admits lists that are
    not real -- they render as triangles spanning the level -- so descriptors
    stay the entry point even though they look like lower recall.
    """
    img = container.image
    counts = {i: cnt for i, d, sz, cnt, st in arrays(img)}
    npos = counts.get(0, 0)
    if not npos:
        return
    seen = set()
    for o in container.pointer_offsets():
        if o + 16 > len(img):
            continue
        p = u32(img, o)
        if not (0 < p < len(img) - 8) or (img[p] & 0xF8) not in PRIMS:
            continue
        size, used, vtype = (u32(img, o + 4), u32(img, o + 8), u32(img, o + 12))
        stride = DL_STRIDE.get(vtype)
        if stride is None or p in seen:
            continue
        if not (used <= size <= used + 32) or p + size > len(img):
            continue
        prims = _parse_prims(img, p, used, stride)
        if prims is None:
            continue
        if not all(_dl_valid(img, npos, base, n, stride)
                   for _op, n, base in prims):
            continue
        seen.add(p)
        yield o, p, stride, prims


def _to_triangles(opcode, idx):
    n = len(idx)
    kind = opcode & 0xF8
    if kind == 0x80:                          # quads
        for i in range(0, n - 3, 4):
            yield idx[i], idx[i + 1], idx[i + 2]
            yield idx[i], idx[i + 2], idx[i + 3]
    elif kind == 0x90:                        # independent triangles
        for i in range(0, n - 2, 3):
            yield idx[i], idx[i + 1], idx[i + 2]
    elif kind == 0x98:                        # strip, alternating winding
        for i in range(n - 2):
            yield ((idx[i], idx[i + 1], idx[i + 2]) if i % 2 == 0
                   else (idx[i + 1], idx[i], idx[i + 2]))
    elif kind == 0xA0:                        # fan
        for i in range(1, n - 1):
            yield idx[0], idx[i], idx[i + 1]


def batches(container):
    """Yield (material or None, [(v0,v1,v2), ...]) for each display list.

    Each v is the vertex's whole field tuple, so callers can take positions
    (F_POS), texcoords (F_TEX0) or anything else in FIELD_ARRAY.
    """
    img = container.image
    # Sectors are checked first: a descriptor inside a sector node belongs to
    # that movable object, not to whichever material node precedes it.
    groups = list(sectors(container)) + list(materials(container))
    order = sorted(range(len(groups)), key=lambda i: groups[i]["offset"])
    starts = [groups[i]["offset"] for i in order]
    secs = [g for g in groups if g["kind"] == "sector"]
    sec_starts = sorted(g["offset"] for g in secs)
    sec_by_start = {g["offset"]: g for g in secs}

    def owner(desc_off):
        j = bisect.bisect_right(sec_starts, desc_off) - 1
        if j >= 0:
            g = sec_by_start[sec_starts[j]]
            if desc_off < g["end"]:
                return g
        j = bisect.bisect_right(starts, desc_off) - 1
        if j < 0:
            return None
        g = groups[order[j]]
        return g if desc_off < g["end"] else None

    for desc_off, _p, stride, prims in display_lists(container):
        nfields = stride // 2
        tris = []
        for op, n, base in prims:
            verts = [tuple(u16(img, base + v * stride + k * 2)
                           for k in range(nfields))
                     for v in range(n)]
            for a, b, c in _to_triangles(op, list(range(n))):
                tris.append((verts[a], verts[b], verts[c]))
        yield owner(desc_off), tris


def triangles(container):
    """Yield (i0,i1,i2) position indices for every primitive of every list.

    Indices only, so movable geometry comes out in local space. Use
    `world_triangles` for anything that is going to be drawn.
    """
    for _owner, tris in batches(container):
        for a, b, c in tris:
            yield a[F_POS], b[F_POS], c[F_POS]


def world_triangles(container):
    """Yield ((x,y,z), (x,y,z), (x,y,z)) with sector transforms applied."""
    pts = positions(container.image)
    n = len(pts)
    for owner, tris in batches(container):
        xf = owner["xform"] if owner else None
        for tri in tris:
            if max(v[F_POS] for v in tri) >= n:
                continue
            yield tuple(apply_xform(xf, pts[v[F_POS]]) for v in tri)


# --- subcommands ------------------------------------------------------------

def cmd_arrays(args) -> int:
    for path in args.files:
        img = Container(path).image
        print(f"=== {path} ===")
        for i, data, size, count, stride in arrays(img):
            guess = {12: "float3 (pos/normal)", 8: "float2 (texcoord)",
                     4: "u32 (colour/index)"}.get(stride, "")
            print(f"  [{i:>2}] data=0x{data:07x} size={size:>9,} "
                  f"count={count:>8,} stride={stride:>3}  {guess}")
    return 0


def cmd_lights(args) -> int:
    for path in args.files:
        img = Container(path).image
        ls = list(lights(img))
        print(f"=== {path}: {len(ls)} lights ===")
        for l in ls[:args.limit]:
            r, g, b = l["colour"]
            x, y, z = l["pos"]
            print(f"  [{l['index']:>4}] type={l['type']} "
                  f"rgb=({r:.3f},{g:.3f},{b:.3f}) "
                  f"pos=({x:8.2f},{y:7.2f},{z:8.2f}) range={l['range']:.1f}")
        if len(ls) > args.limit:
            print(f"  ... {len(ls) - args.limit} more")
    return 0


def cmd_materials(args) -> int:
    for path in args.files:
        cont = Container(path)
        ms = list(materials(cont))
        counts: dict[int, int] = {}
        for mat, tris in batches(cont):
            key = mat["index"] if mat else -1
            counts[key] = counts.get(key, 0) + len(tris)
        named = sum(1 for m in ms if m["name"])
        withtex = sum(1 for m in ms if m["diffuse"] is not None)
        print(f"=== {path}: {len(ms)} materials "
              f"({named} named, {withtex} with a diffuse texture) ===")
        for m in ms[:args.limit]:
            print(f"  [{m['index']:>3}] tex={str(m['diffuse']):>4} "
                  f"tris={counts.get(m['index'], 0):>6}  {m['name'] or ''}")
        if len(ms) > args.limit:
            print(f"  ... {len(ms) - args.limit} more")
        if -1 in counts:
            print(f"  WARNING: {counts[-1]} triangles have no owning material")
    return 0


def cmd_map(args) -> int:
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        img = Container(path).image
        pts = positions(img)
        if not pts:
            print(f"{path}: no position array")
            continue
        ls = list(lights(img))
        W = H = args.size
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]

        # Clip to percentiles: a few stray vertices (skybox corners, sentinels)
        # otherwise squash the whole level into a handful of pixels.
        def band(vals, lo=args.clip, hi=100.0 - args.clip):
            v = sorted(vals)
            n = len(v) - 1
            return v[int(n * lo / 100)], v[int(n * hi / 100)]

        x0, x1 = band(xs)
        z0, z1 = band(zs)
        y0, y1 = band(ys)
        s = min((W - 20) / max(1e-6, x1 - x0), (H - 20) / max(1e-6, z1 - z0))
        buf = bytearray(b"\x10\x12\x18\xff" * (W * H))

        def put(x, z, rgb):
            if 0 <= x < W and 0 <= z < H:
                i = (z * W + x) * 4
                buf[i:i + 3] = bytes(rgb)

        for x, y, z in pts:                        # height-coloured points
            cx, cz = int(10 + (x - x0) * s), int(10 + (z - z0) * s)
            t = min(1.0, max(0.0, (y - y0) / max(1e-6, y1 - y0)))
            rgb = (int(60 + 195 * t), int(200 - 120 * t), int(255 - 200 * t))
            for dx in (0, 1):
                for dz in (0, 1):
                    put(cx + dx, cz + dz, rgb)

        for l in ls:                               # lights in red
            lx, _, lz = l["pos"]
            cx, cz = int(10 + (lx - x0) * s), int(10 + (lz - z0) * s)
            for dx in range(-2, 3):
                for dz in range(-2, 3):
                    if dx * dx + dz * dz <= 4:
                        put(cx + dx, cz + dz, (255, 60, 60))

        dest = args.output / (path.stem + "_map.png")
        write_png(dest, W, H, bytes(buf))
        print(f"{path.name}: {len(pts):,} verts, {len(ls)} lights -> {dest}")
    return 0


def cmd_obj(args) -> int:
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        img = Container(path).image
        pts = positions(img)
        if not pts:
            print(f"{path}: no position array")
            continue
        dest = args.output / (path.stem + ".obj")
        with dest.open("w") as fh:
            fh.write(f"# {path.name}: {len(pts)} vertices from CcArray[0]\n")
            fh.write("# every position, unconnected. Use 'mesh' for triangles.\n")
            for x, y, z in pts:
                fh.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
        print(f"{path.name}: {len(pts):,} vertices -> {dest}")
    return 0


def _gname(key):
    kind, idx = key
    return {"material": "mat%d", "sector": "sector%d"}.get(
        kind, "unowned%d") % idx if idx >= 0 else "unowned"


def cmd_mesh(args) -> int:
    """OBJ + MTL, with faces grouped by the material that owns them."""
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        cont = Container(path)
        img = cont.image
        pts = positions(img)
        if not pts:
            print(f"{path}: no position array")
            continue
        texs = list(_texwalk(img))
        uvs = texcoords(img, args.uv_array)

        # Keyed by (kind, index): a sector and a material can share an index,
        # and a position index reused by two sectors needs a vertex each,
        # because each carries a different transform.
        groups: dict[tuple, list] = {}
        matinfo: dict[tuple, dict] = {}
        nlists = 0
        for owner, tris in batches(cont):
            nlists += 1
            key = (owner["kind"], owner["index"]) if owner else ("unowned", -1)
            groups.setdefault(key, []).extend(
                t for t in tris if max(v[F_POS] for v in t) < len(pts))
            if owner and key not in matinfo:
                matinfo[key] = owner

        # (position index, owning group) -> world-space vertex
        vkey: dict[tuple, int] = {}
        verts: list[tuple] = []
        for key in sorted(groups):
            xf = matinfo.get(key, {}).get("xform")
            for tri in groups[key]:
                for v in tri:
                    k = (v[F_POS], key if xf else None)
                    if k not in vkey:
                        vkey[k] = len(verts) + 1        # OBJ is 1-based
                        verts.append(apply_xform(xf, pts[v[F_POS]]))
        used_uv = sorted({v[F_TEX0] for tl in groups.values() for t in tl
                          for v in t if v[F_TEX0] < len(uvs)})
        remap_uv = {v: i + 1 for i, v in enumerate(used_uv)}
        total = sum(len(t) for t in groups.values())
        moved = sum(len(groups[k]) for k in groups
                    if matinfo.get(k, {}).get("xform"))
        textured = 0

        obj = args.output / (path.stem + ".obj")
        mtl = args.output / (path.stem + ".mtl")
        with obj.open("w") as fh:
            fh.write(f"# {path.name}\n")
            fh.write(f"# {nlists} display lists, {len(groups)} groups -> "
                     f"{total} triangles, {len(verts)} vertices\n")
            fh.write(f"# texcoords from CcArray[{args.uv_array}] (TEX0); "
                     f"v is flipped for OBJ's bottom-left origin\n")
            fh.write(f"# {moved} triangles belong to movable sectors and are "
                     f"written in world space, not at the model origin\n")
            fh.write(f"mtllib {mtl.name}\n")
            for x, y, z in verts:
                fh.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
            for t in used_uv:
                u, w = uvs[t]
                fh.write(f"vt {u:.6f} {1.0 - w:.6f}\n")
            for key in sorted(groups):
                xf = matinfo.get(key, {}).get("xform")
                fh.write(f"o {_gname(key)}\n" if xf else "")
                fh.write(f"usemtl {_gname(key)}\n")
                for tri in groups[key]:
                    ids = [vkey[(v[F_POS], key if xf else None)] for v in tri]
                    if all(v[F_TEX0] in remap_uv for v in tri):
                        textured += 1
                        fh.write("f " + " ".join(
                            f"{i}/{remap_uv[v[F_TEX0]]}"
                            for i, v in zip(ids, tri)) + "\n")
                    else:
                        fh.write("f " + " ".join(str(i) for i in ids) + "\n")

        wanted: dict[str, int] = {}
        with mtl.open("w") as fh:
            fh.write(f"# materials for {path.name}\n")
            for key in sorted(groups):
                m = matinfo.get(key)
                name = _gname(key)
                fh.write(f"\nnewmtl {name}\n")
                if m and m["name"]:
                    fh.write(f"# {m['name']}\n")
                fh.write("Kd 0.8 0.8 0.8\n")
                if m and m["diffuse"] is not None and m["diffuse"] < len(texs):
                    t = texs[m["diffuse"]]
                    png = (f"{path.stem}_{m['diffuse']:03d}_"
                           f"{t.name}_{t.width}x{t.height}.png")
                    wanted[png] = m["diffuse"]
                    fh.write(f"map_Kd {png}\n")

        # Write the referenced textures next to the OBJ. An OBJ whose map_Kd
        # names do not resolve is not a usable export, and only the textures
        # this level actually references are worth writing.
        written = 0
        if not args.no_textures:
            for png, ti in wanted.items():
                dest = args.output / png
                if dest.exists():
                    continue
                t = texs[ti]
                try:
                    rgba = _decode(img, t.pixels, t.width, t.height, t.gx)
                except Exception as e:                       # noqa: BLE001
                    print(f"  {path.stem}: texture {ti} ({t.name}): {e}")
                    continue
                write_png(dest, t.width, t.height, bytes(rgba))
                written += 1

        nsec = sum(1 for k in groups if k[0] == "sector")
        print(f"{path.name}: {nlists} lists, {len(groups)} groups "
              f"({nsec} movable), {total:,} triangles "
              f"({textured * 100 // max(1, total)}% with UVs, {moved:,} "
              f"transformed), {len(verts):,} vertices, {len(used_uv):,} "
              f"texcoords, {len(wanted)} textures ({written} new) "
              f"-> {obj.name} + {mtl.name}")
    return 0


def cmd_render(args) -> int:
    """Top-down render with the diffuse texture actually sampled through the UVs.

    This is the check that the texcoord mapping is right. A wrong field or a
    wrong array gives noise or smearing; a right one gives readable tiling --
    floor slabs, brickwork, painted markings lining up with the geometry.
    """
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        cont = Container(path)
        img = cont.image
        pts = positions(img)
        uvs = texcoords(img, args.uv_array)
        if not pts or not uvs:
            print(f"{path}: no positions or no texcoords")
            continue
        texs = list(_texwalk(img))

        def band(vals, lo=1.0, hi=99.0):
            v = sorted(vals)
            n = len(v) - 1
            return v[int(n * lo / 100)], v[int(n * hi / 100)]

        x0, x1 = band([p[0] for p in pts])
        z0, z1 = band([p[2] for p in pts])
        W = args.size
        s = (W - 20) / max(1e-6, x1 - x0)
        H = max(64, min(4096, int((z1 - z0) * s) + 20))
        buf = bytearray(b"\x0e\x10\x16\xff" * (W * H))
        ybuf = [-1e30] * (W * H)                  # keep the highest surface
        cache: dict[int, bytearray] = {}

        # Ceilings and cliff tops would otherwise win the height test and hide
        # the floor -- Clevel7_1's canyon walls reach y=6554 over a floor at
        # y=185, so an unclipped top-down view is nothing but wall tops.
        ys = sorted(p[1] for p in pts)
        ceiling = ys[min(len(ys) - 1, int(len(ys) * args.clip_height / 100))]

        def sample(ti, u, v):
            t = texs[ti]
            px = cache.get(ti)
            if px is None:
                px = cache[ti] = _decode(img, t.pixels, t.width, t.height, t.gx)
            x = int(u * t.width) % t.width
            y = int(v * t.height) % t.height
            i = (y * t.width + x) * 4
            return px[i:i + 3]

        drawn = skipped = 0
        for owner, tris in batches(cont):
            ti = owner["diffuse"] if owner else None
            xf = owner["xform"] if owner else None
            if ti is None or ti >= len(texs) or not texs[ti].width:
                skipped += len(tris)
                continue
            for tri in tris:
                if any(v[F_POS] >= len(pts) or v[F_TEX0] >= len(uvs)
                       for v in tri):
                    skipped += 1
                    continue
                p3 = [apply_xform(xf, pts[v[F_POS]]) for v in tri]
                if sum(q[1] for q in p3) / 3.0 > ceiling:
                    skipped += 1
                    continue
                drawn += 1
                t3 = [uvs[v[F_TEX0]] for v in tri]
                sx = [10 + (p[0] - x0) * s for p in p3]
                sy = [10 + (p[2] - z0) * s for p in p3]
                _tex_tri(buf, ybuf, W, H, sx, sy,
                         [p[1] for p in p3], t3, sample, ti)

        dest = args.output / (path.stem + "_textured.png")
        write_png(dest, W, H, bytes(buf))
        print(f"{path.name}: {drawn:,} triangles textured, {skipped:,} skipped, "
              f"{len(cache)} textures -> {dest}")
    return 0


def _tex_tri(buf, ybuf, W, H, sx, sy, hs, uv, sample, ti):
    """Barycentric fill with UV and height interpolation."""
    minx, maxx = max(0, int(min(sx))), min(W - 1, int(max(sx)) + 1)
    miny, maxy = max(0, int(min(sy))), min(H - 1, int(max(sy)) + 1)
    if minx > maxx or miny > maxy or (maxx - minx) * (maxy - miny) > 4_000_000:
        return
    x0, y0 = sx[0], sy[0]
    d1x, d1y = sx[1] - x0, sy[1] - y0
    d2x, d2y = sx[2] - x0, sy[2] - y0
    den = d1x * d2y - d2x * d1y
    if abs(den) < 1e-9:
        return
    for py in range(miny, maxy + 1):
        wy = py + 0.5 - y0
        for px in range(minx, maxx + 1):
            wx = px + 0.5 - x0
            b1 = (wx * d2y - d2x * wy) / den
            b2 = (d1x * wy - wx * d1y) / den
            b0 = 1.0 - b1 - b2
            if b0 < -0.002 or b1 < -0.002 or b2 < -0.002:
                continue
            i = py * W + px
            h = b0 * hs[0] + b1 * hs[1] + b2 * hs[2]
            if h < ybuf[i]:
                continue
            ybuf[i] = h
            u = b0 * uv[0][0] + b1 * uv[1][0] + b2 * uv[2][0]
            v = b0 * uv[0][1] + b1 * uv[1][1] + b2 * uv[2][1]
            buf[i * 4:i * 4 + 3] = sample(ti, u, v)


def cmd_verify(args) -> int:
    roots = args.paths or [Path("orig/files/Levels")]
    files: list[Path] = []
    for r in roots:
        r = Path(r)
        files.extend(sorted(r.rglob("*.ssw")) if r.is_dir() else [r])
    bad = 0
    tot_v = tot_l = 0
    for f in sorted(set(files)):
        img = Container(f).image
        tbl = list(arrays(img))
        pts = positions(img)
        ls = list(lights(img))
        tot_v += len(pts)
        tot_l += len(ls)
        ok = len(tbl) == ARRAY_COUNT and bool(pts) and bool(ls)
        # Colour components sit in [-4,4]. Negative values are real: they are
        # subtractive "dark" lights, e.g. rgb=(-2.3,-2.3,-2.3) in Alevel9_1.
        if ls:
            comps = [c for l in ls for c in l["colour"]]
            ok = ok and all(-4 <= c <= 4 for c in comps)
        if not ok:
            bad += 1
            print(f"  FAIL {f.name}: table={len(tbl)}/13 verts={len(pts)} "
                  f"lights={len(ls)}")
    print(f"{len(set(files))} levels: {tot_v:,} vertices, {tot_l:,} lights, "
          f"{bad} failures")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("arrays", help="dump the vertex array table")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_arrays)

    p = sub.add_parser("lights", help="dump the light list")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_lights)

    p = sub.add_parser("materials", help="materials with texture and triangle counts")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_materials)

    p = sub.add_parser("map",
                       help="top-down PNG of raw vertices and lights "
                            "(no sector transforms; use 'render' for those)")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/maps"))
    p.add_argument("--size", type=int, default=768)
    p.add_argument("--clip", type=float, default=1.0,
                   help="percentile clipped from each end of the bounds")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("obj", help="export every position as an OBJ point cloud")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/obj"))
    p.set_defaults(func=cmd_obj)

    p = sub.add_parser("mesh", help="export geometry as OBJ + MTL, with UVs")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/mesh"))
    p.add_argument("--uv-array", type=int, default=4, choices=TEXCOORD_ARRAYS,
                   help="texcoord array slot (4=TEX0 diffuse, 5, 6)")
    p.add_argument("--no-textures", action="store_true",
                   help="skip writing the PNGs the MTL references")
    p.set_defaults(func=cmd_mesh)

    p = sub.add_parser("render",
                       help="top-down render with textures sampled through UVs")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/render"))
    p.add_argument("--size", type=int, default=1100)
    p.add_argument("--uv-array", type=int, default=4, choices=TEXCOORD_ARRAYS)
    p.add_argument("--clip-height", type=float, default=90.0,
                   help="drop triangles above this percentile of vertex height "
                        "so ceilings and cliff tops do not hide the floor")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("verify", help="check the table and lights on every level")
    p.add_argument("paths", nargs="*", type=Path)
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
