#!/usr/bin/env python3
"""
Flatten a level description into the flat record file `pc/wldwriter` reads.

Stage 1 (`tools/world.py`) produces `build/world/<level>.json` plus the OBJ in
`build/mesh/`. The C++ writer stays thin: this turns those into a token stream
it can read with no JSON or OBJ parsing, so the conversion logic stays here,
where it is testable.

    python tools/wldprep.py Rlevel0_1
    python tools/wldprep.py --all

Format (whitespace separated, '#' starts a comment line):

    world <name>
    polyflags <hex>        flags every polygon gets, before its own
    ambient <hex>          ambient of an entity brush's own sector
    sector <content> <ambient-hex> <name>
      sectorflags <haze> <environment>   of an entity's sector, just after it
      verts <n>            followed by n lines of "x y z"
      mat <texture-path> [<layer 1 path> <layer 2 path>]
                           materials of this sector, in index order; "-" for
                           a texture layer with no texture
      poly <mat> <n> <i0> <i1> ...
      hole <n> <i0> <i1> ...   a hole in the polygon just above
      split <n> <i0> <i1> ...  a convex piece of it, used instead of the whole
                               polygon if SE1 cannot triangulate that
      uv <mexW> <mexH> <ax> <ay> <az> <a0> <bx> <by> <bz> <b0>
                               its texture map in mex: U = a.p + a0, V = b.p + b0
      layer <slot> <scroll> <blend> <flags-hex> <color-hex> <mexW> <mexH> <ax> <ay> <az> <a0> <bx> <by> <bz> <b0>
                               texture layer 1 or 2: SE1's scroll, blend,
                               texture flags and colour, and its map as for
                               uv; layer 0 when the diffuse texture clamps
    endsector
    entity <class> <x y z> <3x3 rotation, row-major>
      fprop/bprop/iprop <value> <name>, cprop <RRGGBBAA> <name>,
      sprop <name> = <string>, eprop <entity index> <name>,
      aprop <x> <y> <z> <name>     an angle property facing that direction
      verts ... mat ... poly ...   the entity's brush, one sector
      sector ... endsector         or sectors of its own, as above
    endentity
    end

Vertices are per sector and already in the original space (docs/world-conversion.md,
"Axes"). Polygons are the SE1 polygons rebuilt from the level's triangles
(`tools/polygons.py`), read from the level itself: the OBJ has no holes.

The static geometry's largest connected piece is the world base, zoning, its
sectors rooms made of its whole polygons and joined by portals
(tools/rooms.py). Every other piece becomes a WorldBase entity of its own, cut
into sectors of at most MAX_SECTOR_POLYGONS whole polygons, compact in space.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import level                                                      # noqa: E402
import space                                                      # noqa: E402
from polygons import polygons                                     # noqa: E402
import lightmap                                                   # noqa: E402
import rooms                                                      # noqa: E402
from ssg import Container                                         # noqa: E402
# A group whose texture has not been converted (tools/wldtex.py) gets the
# editor default that ships in the retail data.
DEFAULT_TEXTURE = r"Textures\Editor\Default.tex"
# NE kept no sectors: no portals, no sector records, and the collision order
# does not delimit them (docs/world-conversion.md, "Sectors and separate
# objects"). The world base's sectors are rooms built from its whole polygons
# (tools/rooms.py); a separate object's are cut by size. The designers'
# waterfall area has 1,934 polygons in 11 sectors.
MAX_SECTOR_POLYGONS = 256
# The level's baked lightmap is black wherever no light reaches, on every
# level, with or without a sun (docs/world-conversion.md, "Lights"): the
# designers' sectors had no ambient of their own.
AMBIENT = 0x00000000
# Every polygon of the designers' waterfallArea.wld accepts the directional
# light and its ambient. The level's lightmap says which of NE's did: one that
# took the sun's fill is nowhere darker than it, and indoor polygons read
# black where no lamp reaches (`lighting`).
BPOF_HASDIRECTIONALLIGHT = 0x10000
BPOF_HASDIRECTIONALAMBIENT = 0x1000000
BPOF_FULLBRIGHT = 0x4000
POLYGON_FLAGS = BPOF_HASDIRECTIONALLIGHT | BPOF_HASDIRECTIONALAMBIENT
FULLBRIGHT_GREY, FULLBRIGHT_TOLERANCE = 127, 6
LIGHT_TYPES = {"LT_POINT": 0, "LT_AMBIENT": 1, "LT_STRONG_AMBIENT": 2, "LT_DIRECTIONAL": 3}
WELD = 1e-3                # a sector's points this close are one vertex
OPOF_PORTAL = 0x1          # Math/Object3D.h; import adds BPOF_PORTAL|BPOF_PASSABLE
BPOF_INVISIBLE = 0x20000   # Brushes/Brush.h
BPOF_PASSABLE = 0x800
# Polygons nothing sees: full bright, so the bake finds no light on them.
UNLIT = BPOF_FULLBRIGHT
FA_LINEAR = 0              # EntitiesMP/FogMarker.es, FogAttenuationType
MUSIC_TYPES = ("MT_LIGHT", "MT_MEDIUM", "MT_HEAVY")   # EntitiesMP/MusicHolder.es, MusicType
# Where the files entities name go under the game root, as SE1 paths.
GAME = ROOT / "pc" / "engine" / "SamTSE"
DEPLOY = {"sound": r"Sounds\NextEncounter", "music": r"Music\NextEncounter",
          "message": r"Data\Messages\NextEncounter"}
BPTF_CLAMP = 0x3           # BPTF_CLAMPU | BPTF_CLAMPV
BPTF_DISCARDABLE = 0x4
BPT_BLEND_BLEND = 2
# A Blend layer's colour alpha when its texture has none. NE's 365 layers of
# Blend with clamp V (setting 3074) are mostly opaque textures stretched over
# large areas, which NE draws with a TEV operation not yet decoded; the
# designers' own waterfallArea.wld blends its opaque "rockblend" texture this
# way through the layer colour's alpha, 0x82 to 0xBC over 109 polygons. Half
# of their median 0x8A, which judged in the editor was too opaque. Without an
# alpha the layer would cover the base texture.
OPAQUE_BLEND_ALPHA = 0x45


def lighting(samples, fill):
    """A polygon's lighting flags from its baked lightmap samples (RGB).

    Flat SE1 neutral grey everywhere: full bright. Otherwise, on a level with
    a sun, the directional light and its ambient unless some sample is darker
    than half the sun's fill, which a polygon taking that fill never is. With
    no samples (a node with no atlas) or no sun, the designers' default."""
    if not samples:
        return POLYGON_FLAGS
    if all(max(abs(c - FULLBRIGHT_GREY) for c in s) <= FULLBRIGHT_TOLERANCE for s in samples):
        return BPOF_FULLBRIGHT
    if fill > 0 and min(sum(s) / 3.0 for s in samples) < fill / 2.0:
        return 0
    return POLYGON_FLAGS


def sun_fill(desc):
    """Mean of the directional ambient a polygon taking it gets from all the
    level's directional lights, 0 without one."""
    return sum(sum(l["props"].get("Directional ambient", (0, 0, 0))) / 3.0
               for l in desc["lights"] if l["props"]["Type"] == "LT_DIRECTIONAL" and not l.get("skip"))


def level_polygons(stem):
    """-> {group name: {"texture", "polygons"}}.

    "texture" is the group's converted diffuse texture ({"tex", "mex"}, from
    build/wld/textures.json) or None, "layers" its texture layers 1 and 2 as
    {slot: layer} (tools/level.py, `layers`, with "texture" the converted
    record or None). A polygon is (loops, pieces, samples): its loops (outline
    first, then holes) and, for a concave or holed one, the outlines of convex
    pieces covering it, each a list of world-space points in the original
    space, wound as the OBJ faces are; samples are (point, uvs) for every
    corner that has texture coordinates, uvs[0] the diffuse texture's (u, v)
    and uvs[slot] a layer's, None where it has none. The fourth item is the
    polygon's lighting flags (`lighting`)."""
    cont = Container(ROOT / "orig" / "files" / "Levels" / (stem + ".ssw"))
    pts = level.positions(cont.image)
    uvs = level.texcoords(cont.image, 4)
    table_path = ROOT / "build" / "wld" / "textures.json"
    table = json.loads(table_path.read_text()).get(stem, {}) if table_path.exists() else {}
    desc_path = ROOT / "build" / "world" / (stem + ".json")
    fill = sun_fill(json.loads(desc_path.read_text())) if desc_path.exists() else 0.0
    baked = lightmap.Baked(cont)
    out = {}
    for key, rec in polygons(cont).items():
        owner = rec["owner"] or {}
        xf = owner.get("xform")

        def point(v):
            return space.world(level.apply_xform(xf, pts[v[level.F_POS]]))

        def place(loops):
            return [[point(v) for v in space.face(loop)] for loop in loops]

        lays = {x["slot"]: dict(x, texture=table.get(str(x["texture"]))) for x in owner.get("layers", ())}
        polys = []
        for poly in rec["polygons"]:
            sets = [poly["uvs"]] + [poly["layer_uvs"][lays[k]["field"]] if k in lays else None for k in (1, 2)]
            samples = []
            for i, loop in enumerate(poly["loops"]):
                for j, v in enumerate(loop):
                    uvs = tuple(x[i][j] if x is not None else None for x in sets)
                    if uvs[0] is not None:
                        samples.append((point(v), uvs))
            light = lighting(baked.samples(rec["owner"], poly["triangles"]), fill)
            polys.append((place(poly["loops"]), place(poly.get("convex", ())), samples, light))
        out[level._gname(key)] = {"texture": table.get(str(owner.get("diffuse"))), "layers": lays,
                                  "clamp": owner.get("clamp", 0),
                                  "polygons": polys}
    return out


def fit_uv(samples, mex, layer=0):
    """(point, uvs) samples of one flat polygon -> the texture map in mex of
    uvs[layer], (a, a0, b, b0) with U = a.p + a0 and V = b.p + b0, a and b in
    the plane; None if the samples do not span it."""
    samples = [(p, uvs[layer]) for p, uvs in samples if uvs[layer] is not None]
    if len(samples) < 3:
        return None
    ps = [p for p, _uv in samples]
    n = [0.0, 0.0, 0.0]
    for p0, p1 in zip(ps, ps[1:] + ps[:1]):
        n = [n[0] + p0[1] * p1[2] - p0[2] * p1[1], n[1] + p0[2] * p1[0] - p0[0] * p1[2],
             n[2] + p0[0] * p1[1] - p0[1] * p1[0]]
    ln = math.sqrt(sum(x * x for x in n))
    if ln < 1e-9:
        return None
    n = [x / ln for x in n]
    helper = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = [helper[1] * n[2] - helper[2] * n[1], helper[2] * n[0] - helper[0] * n[2],
          helper[0] * n[1] - helper[1] * n[0]]
    l1 = math.sqrt(sum(x * x for x in e1))
    e1 = [x / l1 for x in e1]
    e2 = [n[1] * e1[2] - n[2] * e1[1], n[2] * e1[0] - n[0] * e1[2], n[0] * e1[1] - n[1] * e1[0]]
    c = [sum(p[k] for p in ps) / len(ps) for k in range(3)]
    # least squares for [x y 1] -> u and v, by the 3x3 normal equations
    m = [[0.0] * 3 for _ in range(3)]
    ru, rv = [0.0] * 3, [0.0] * 3
    for p, (u, v) in samples:
        d = [p[k] - c[k] for k in range(3)]
        f = (sum(d[k] * e1[k] for k in range(3)), sum(d[k] * e2[k] for k in range(3)), 1.0)
        for i in range(3):
            for j in range(3):
                m[i][j] += f[i] * f[j]
            ru[i] += f[i] * u
            rv[i] += f[i] * v
    try:
        inv = inverse3(m)
    except ValueError:
        return None
    cu = [sum(inv[i][j] * ru[j] for j in range(3)) for i in range(3)]
    cv = [sum(inv[i][j] * rv[j] for j in range(3)) for i in range(3)]
    a = [(cu[0] * e1[k] + cu[1] * e2[k]) * mex[0] for k in range(3)]
    b = [(cv[0] * e1[k] + cv[1] * e2[k]) * mex[1] for k in range(3)]
    a0 = cu[2] * mex[0] - sum(a[k] * c[k] for k in range(3))
    b0 = cv[2] * mex[1] - sum(b[k] * c[k] for k in range(3))
    return a, a0, b, b0


def compact(polys, place=lambda v: v):
    """[(loops, pieces, samples, light)] of points -> (vertices, [(loops,
    pieces, samples, light)] with loops and pieces as vertex indices and
    samples placed)."""
    used, remap, grid = [], {}, defaultdict(list)

    def ids(loop):
        # Points closer than WELD are one vertex. NE's neighbouring polygons
        # disagree by ~1e-6 and the rooms' portals are cut along them; left
        # apart, SE1 splits edges at the near-duplicates and its triangulator
        # fails on the zero-length edges that leaves.
        out = []
        for v in loop:
            j = remap.get(v)
            if j is None:
                cell = tuple(math.floor(c / WELD) for c in v)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for dz in (-1, 0, 1):
                            for k, q in grid.get((cell[0] + dx, cell[1] + dy, cell[2] + dz), ()):
                                if j is None and max(abs(q[t] - v[t]) for t in range(3)) <= WELD:
                                    j = k
                if j is None:
                    j = len(used)
                    used.append(place(v))
                    grid[cell].append((j, v))
                remap[v] = j
            if not out or out[-1] != j:
                out.append(j)
        if len(out) > 1 and out[0] == out[-1]:
            out.pop()
        return out

    return used, [([ids(x) for x in loops], [ids(x) for x in pieces],
                   [(place(p), uv) for p, uv in samples], light)
                  for loops, pieces, samples, light in polys]


def poly_lines(mat, poly, texture, flags=0, layers=None, clamp=0):
    loops, pieces, samples, light = poly
    flags |= light
    out = ["poly %d %d %s" % (mat, len(loops[0]), " ".join(str(i) for i in loops[0]))]
    out += ["hole %d %s" % (len(h), " ".join(str(i) for i in h)) for h in loops[1:]]
    out += ["split %d %s" % (len(c), " ".join(str(i) for i in c)) for c in pieces]
    if flags:
        out.append("flags %x" % flags)
    uv = fit_uv(samples, texture["mex"]) if texture else None
    if uv:
        a, a0, b, b0 = uv
        out.append("uv %d %d %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g" % (
            texture["mex"][0], texture["mex"][1], a[0], a[1], a[2], a0, b[0], b[1], b[2], b0))
        if clamp:
            out.append("layer 0 0 0 %x FFFFFFFF %d %d %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g" % (
                BPTF_DISCARDABLE | clamp, texture["mex"][0], texture["mex"][1],
                a[0], a[1], a[2], a0, b[0], b[1], b[2], b0))
    for slot, lay in sorted((layers or {}).items()):
        uv = fit_uv(samples, lay["texture"]["mex"], slot)
        if uv:
            a, a0, b, b0 = uv
            # Clamped where NE clamps (tools/level.py, MAT_CLAMP_U), not where
            # the label says: NE repeats the label's clamp-V layers (setting
            # 3074), and clamped they draw their edge texels only.
            flags = (lay["flags"] & ~BPTF_CLAMP) | lay["clamp"]
            alpha = 0xFF
            if lay["blend"] == BPT_BLEND_BLEND and not lay["texture"].get("alpha"):
                alpha = OPAQUE_BLEND_ALPHA
            out.append("layer %d 0 %d %x %08X %d %d %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g" % (
                slot, lay["blend"], flags, 0xFFFFFF00 | alpha, lay["texture"]["mex"][0], lay["texture"]["mex"][1],
                a[0], a[1], a[2], a0, b[0], b[1], b[2], b0))
    return out


def material(g):
    """A group's material: (the mat line's texture names, its diffuse texture
    record or None, its converted texture layers {slot: layer}). A layer whose
    texture was not converted is left out."""
    tex = (g["texture"] or {}).get("tex", DEFAULT_TEXTURE)
    lays = {k: x for k, x in (g.get("layers") or {}).items() if x["texture"]}
    names = tex
    if lays:
        names += " " + " ".join(lays[k]["texture"]["tex"] if k in lays else "-" for k in (1, 2))
    return names, g["texture"], lays, g.get("clamp", 0)


def gather(groups, names):
    """Material groups -> (materials, [(material, polygon)]); a material as
    `material` makes it."""
    mats, polys = [], []
    for name in names:
        g = groups.get(name)
        if not g or not g["polygons"]:
            continue
        mats.append(material(g))
        polys += [(len(mats) - 1, p) for p in g["polygons"]]
    return mats, polys


def pieces(polys):
    """[(material, polygon)] -> lists of their indices, one per connected
    piece (polygons sharing a corner), largest first, then in order."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, (_m, (loops, _pieces, _samples, _light)) in enumerate(polys):
        a = find(("p", i))
        for loop in loops:
            for v in loop:
                b = find(tuple(round(c, 4) for c in v))
                if a != b:
                    parent[b] = a
    out = {}
    for i in range(len(polys)):
        out.setdefault(find(("p", i)), []).append(i)
    return sorted(out.values(), key=lambda ix: (-len(ix), ix[0]))


def split_sectors(polys, limit=MAX_SECTOR_POLYGONS):
    """[(material, polygon)] -> index lists of at most `limit` polygons,
    compact in space: halved at the median polygon centre along the longest
    side, until small enough. Each keeps its polygons' order."""
    centre = []
    for _m, (loops, _pieces, _samples, _light) in polys:
        o = loops[0]
        centre.append(tuple(sum(p[k] for p in o) / len(o) for k in range(3)))

    def halve(ix):
        if len(ix) <= limit:
            return [ix]
        lo = [min(centre[i][k] for i in ix) for k in range(3)]
        hi = [max(centre[i][k] for i in ix) for k in range(3)]
        axis = max(range(3), key=lambda k: hi[k] - lo[k])
        by = sorted(ix, key=lambda i: (centre[i][axis], i))
        half = len(by) // 2
        return halve(sorted(by[:half])) + halve(sorted(by[half:]))

    return halve(list(range(len(polys))))


def sector_lines(content, ambient, name, mats, polys, place=lambda v: v, flags=None):
    """One sector block: its own vertices and the materials its polygons use.
    `flags`, if given, holds each polygon's flags."""
    used_mats = sorted({m for m, _p in polys})
    remap = {m: i for i, m in enumerate(used_mats)}
    verts, indexed = compact([p for _m, p in polys], place)
    lines = ["sector %d %08X %s" % (content, ambient, name), "verts %d" % len(verts)]
    lines += ["%.6f %.6f %.6f" % v for v in verts]
    lines += ["mat %s" % mats[m][0] for m in used_mats]
    for i, ((m, _p), poly) in enumerate(zip(polys, indexed)):
        lines += poly_lines(remap[m], poly, mats[m][1], flags[i] if flags else 0, mats[m][2], mats[m][3])
    lines.append("endsector")
    return lines


def room_lines(mats, polys, occupied, stats):
    """The zoning world base: [(material, polygon)] -> its sectors, rooms
    made of its whole polygons, each closed by portal and invisible polygons. `occupied`
    are points where the level has anything else (tools/rooms.py)."""
    rs = rooms.rooms([(loops, pieces) for _m, (loops, pieces, _s, _l) in polys], occupied, stats)
    mats = mats + [(DEFAULT_TEXTURE, None, {}, 0)]
    blank = len(mats) - 1
    lines = []
    for i, r in enumerate(rs):
        entries, flags = [], []
        for src, loops, pieces in r["polys"]:
            m, (_l, _p, samples, light) = polys[src]
            entries.append((m, (loops, pieces, samples, light)))
            flags.append(0)
        for _other, pieces in r["portals"]:
            entries.append((blank, (pieces, pieces if len(pieces) > 1 else [], [], 0)))
            flags.append(OPOF_PORTAL)
        for pieces in r["closing"]:
            entries.append((blank, (pieces, pieces if len(pieces) > 1 else [], [], 0)))
            flags.append(BPOF_INVISIBLE | UNLIT)
        lines += sector_lines(0, AMBIENT, "room_%d" % i, mats, entries, flags=flags)
    return lines, len(rs)


def inverse3(rows):
    """Inverse of a 3x3 row-major matrix."""
    (a, b, c), (d, e, f), (g, h, i) = rows
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("singular brush matrix")
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def to_local(rows, trans, v):
    inv = inverse3(rows)
    d = [v[k] - trans[k] for k in range(3)]
    return tuple(sum(inv[r][k] * d[k] for k in range(3)) for r in range(3))


def entity_lines(cls, position, rows, props=(), links=(), geometry=None):
    """One entity block: placement, properties, entity links, optional brush."""
    flat = " ".join("%.6f" % x for r in rows for x in r)
    out = ["entity %s %.6f %.6f %.6f %s" % (cls, position[0], position[1], position[2], flat)]
    for kind, name, value in props:
        out.append("%sprop %s %s" % (kind, value, name))
    for name, index in links:
        out.append("eprop %d %s" % (index, name))
    if geometry:
        verts, mats, polys = geometry
        out.append("verts %d" % len(verts))
        out += ["%.6f %.6f %.6f" % v for v in verts]
        out += ["mat %s" % m[0] for m in mats]
        for m, poly in polys:
            out += poly_lines(m, poly, mats[m][1], 0, mats[m][2], mats[m][3])
    out.append("endentity")
    return out


def se1_props(d):
    """SE1 property dict -> (kind, name, value) records; kind is f/b/i."""
    out = []
    for name, value in (d or {}).items():
        if name == "Target" or value is None:
            continue                      # links are emitted separately
        if isinstance(value, bool):
            out.append(("b", name, 1 if value else 0))
        elif isinstance(value, float):
            out.append(("f", name, "%.6f" % value))
        elif isinstance(value, int):
            out.append(("i", name, value))
    return out


def facing(direction):
    """A rotation whose front, SE1's -z (AnglesToDirectionVector), is `direction`."""
    z = [-c for c in direction]
    n = math.sqrt(sum(c * c for c in z))
    z = [c / n for c in z]
    up = (0.0, 1.0, 0.0) if abs(z[1]) < 0.99 else (1.0, 0.0, 0.0)
    x = [up[1] * z[2] - up[2] * z[1], up[2] * z[0] - up[0] * z[2], up[0] * z[1] - up[1] * z[0]]
    n = math.sqrt(sum(c * c for c in x))
    x = [c / n for c in x]
    y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
    return [[x[r], y[r], z[r]] for r in range(3)]      # columns are the axes


def rgba(rgb):
    return "%02X%02X%02XFF" % tuple(rgb)


def region_lines(desc, first):
    """NE's region cells (docs/world-conversion.md, "NE's region system") as
    one more WorldBase, and a HazeMarker per region fog; `first` is the
    entity index the first of them gets.

    The brush zones: SE1 relates an entity only to zoning brushes' sectors
    (CEntity::FindSectorsAroundEntity), and a movable entity takes its content
    from those. Each cell is a closed convex volume of invisible, passable
    polygons whose sector carries content, environment and haze.
    """
    cells = desc["region_brush"]["sectors"]
    if not cells:
        return [], 0
    lines, index = [], first
    identity = " ".join("%.6f" % x for x in (1, 0, 0, 0, 1, 0, 0, 0, 1))
    markers = {}
    for h in desc["haze_markers"]:
        se = h["se1"]
        users = [c for c in cells if c["haze"] == h["slot"]]
        ps = [v for c in users for v in c["vertices"]] or [(0.0, 0.0, 0.0)]
        at = [(min(v[k] for v in ps) + max(v[k] for v in ps)) / 2 for k in range(3)]
        lines.append("entity HazeMarker %.6f %.6f %.6f %s" % (at[0], at[1], at[2], identity))
        lines.append("sprop Name = Region fog %d" % h["slot"])
        lines.append("iprop %d Attenuation Type" % FA_LINEAR)
        lines.append("fprop %.6f Near" % se["Near"])
        lines.append("fprop %.6f Far" % se["Far"])
        lines.append("cprop %s Base Color" % rgba(se["Base Color"]))
        lines.append("endentity")
        markers[h["slot"]] = index
        index += 1
    lines.append("entity WorldBase 0 0 0 %s" % identity)
    lines.append("sprop Name = Regions")
    lines.append("bprop 1 Zoning")
    for slot, i in sorted(markers.items()):
        lines.append("eprop %d Haze %d" % (i, slot))
    for c in cells:
        lines.append("sector %d %08X %s" % (c["content"], AMBIENT, c["name"].replace(" ", "_")))
        lines.append("sectorflags %d %d" % (c["haze"], c["environment"]))
        lines.append("verts %d" % len(c["vertices"]))
        lines += ["%.6f %.6f %.6f" % tuple(v) for v in c["vertices"]]
        lines.append("mat %s" % DEFAULT_TEXTURE)
        for face in c["faces"]:
            ix = face["verts"]
            lines.append("poly 0 %d %s" % (len(ix), " ".join(str(i) for i in ix)))
            lines.append("flags %x" % (BPOF_INVISIBLE | BPOF_PASSABLE | UNLIT))
        lines.append("endsector")
    lines.append("endentity")
    return lines, index - first + 1


def deploy(src, kind):
    """Copy a built file under the game root; -> its SE1 path."""
    src = ROOT / src
    rel = DEPLOY[kind] + "\\" + src.name
    dst = GAME / rel.replace("\\", "/")
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return rel


def placed(cls, position, rows=None):
    flat = " ".join("%.6f" % x for r in (rows or [[1, 0, 0], [0, 1, 0], [0, 0, 1]]) for x in r)
    return "entity %s %.6f %.6f %.6f %s" % (cls, position[0], position[1], position[2], flat)


def volume_lines(volume, at, flags=0):
    """A touch field's or bouncer's collision triangles as its brush, local to `at`."""
    lines = ["verts %d" % len(volume["vertices"])]
    lines += ["%.6f %.6f %.6f" % tuple(v[k] - at[k] for k in range(3)) for v in volume["vertices"]]
    lines.append("mat %s" % DEFAULT_TEXTURE)
    for face in volume["faces"]:
        lines.append("poly 0 %d %s" % (len(face), " ".join(str(i) for i in face)))
        if flags:
            lines.append("flags %x" % flags)
    return lines


# What each SE1 class listens for (its `on (E...)` handlers in EntitiesMP):
# T trigger, S start, P stop, A activate, D deactivate. A Trigger slot's event
# is picked from these, so NE's trigger / on / off reach the target as an
# event it handles (docs/world-conversion.md, "Scripting as written").
HANDLES = {
    "Trigger": "TSAD", "EnemySpawner": "TSPD", "WatchPlayers": "AD", "Teleport": "AD",
    "DoorController": "TAD", "MovingBrush": "TSPAD", "TouchField": "AD",
    "MessageHolder": "TAD", "SoundHolder": "SP", "Camera": "T", "Copier": "T",
    "Damager": "T", "MusicChanger": "T", "PlayerMarker": "T", "WorldLink": "T",
    # EnemyBase and the stand-ins built on it
    "Boneman": "TSP", "Headman": "TSP", "Woman": "TSP", "Walker": "TSP", "Werebull": "TSP",
    "Demon": "TSP", "Fish": "TSP", "Scorpman": "TSP", "Gizmo": "TSP", "Devil": "TSP",
    # NE's own, on EnemyBase too (pc/entities)
    "GCEnemyGenericDumDumLarge": "TSP",
}
EVENT_ORDER = {"trigger": "TSA", "on": "AST", "off": "DP"}
EET = {"S": 0, "P": 1, "T": 2, "A": 4, "D": 5}
EET_IGNORE = 3


def link(ne_id, name):
    """An entity property pointing at NE entity `ne_id`, resolved later."""
    return "eprop @%d %s" % (ne_id, name)


def event(ne_id, ne_event, name):
    """A Trigger slot's event type, chosen once the target's class is known."""
    return "iprop @%d:%s %s" % (ne_id, ne_event, name)


def resolve_links(lines, ids, stats):
    """Replace NE ids with entity indices; drop links to entities not written.
    `ids` maps NE id -> (entity index, SE1 class)."""
    out = []
    for line in lines:
        if line.startswith("eprop @"):
            ne, name = line[len("eprop @"):].split(" ", 1)
            hit = ids.get(int(ne))
            stats["links set" if hit else "links to entities not written"] += 1
            if hit:
                out.append("eprop %d %s" % (hit[0], name))
            continue
        if line.startswith("iprop @"):
            ref, name = line[len("iprop @"):].split(" ", 1)
            ne, ev = ref.split(":")
            hit = ids.get(int(ne))
            if not hit:
                continue
            handles = HANDLES.get(hit[1], "")
            pick = next((c for c in EVENT_ORDER[ev] if c in handles), None)
            stats["trigger events: %s -> %s" % (ev, {None: "ignore", "T": "trigger", "S": "start",
                                                     "P": "stop", "A": "activate",
                                                     "D": "deactivate"}[pick])] += 1
            out.append("iprop %d %s" % (EET[pick] if pick else EET_IGNORE, name))
            continue
        out.append(line)
    return out


def script_lines(desc, first, stats, ids):
    """NE's scripting and pickups (tools/world.py, script_description) as the
    SE1 classes that do the same; `first` is the entity index the first gets."""
    lines, index = [], first
    for rec in desc["scripts"]["entities"]:
        se = dict(rec["se1"])
        cls = se.pop("class")
        lines.append(placed(cls, rec["position"], rec["rows"]))
        for name, value in se.items():
            if isinstance(value, bool):
                lines.append("bprop %d %s" % (1 if value else 0, name))
            elif isinstance(value, float):
                lines.append("fprop %.6f %s" % (value, name))
            elif isinstance(value, int):
                lines.append("iprop %d %s" % (value, name))
            elif isinstance(value, str):
                lines.append("sprop %s = %s" % (name, value))
        for x in rec["links"]:
            lines.append(link(x["id"], x["property"]))
            if "event" in x:
                lines.append(event(x["id"], x["event"], x["event_property"]))
        lines.append("endentity")
        ids[rec["entity_id"]] = (index, cls)
        index += 1
        stats[cls] += 1
    for why, n in desc["scripts"]["skipped"].items():
        stats["not written: " + why] += n
    return lines, index - first


def point_lines(desc, first, stats, ids):
    """The entities Stage 1 already gives an SE1 form (docs/world-conversion.md):
    cameras and their markers, music, sounds, messages, touch fields and
    bouncers; `first` is the entity index the first of them gets."""
    lines, index = [], first
    for cam in desc["cameras"]:
        se, markers = cam["se1"], cam["markers"]
        ids[cam["entity_id"]] = (index, "Camera")
        lines.append(placed("Camera", cam["position"], cam["rotation"]))
        lines.append("sprop Name = %s" % se["Name"])
        lines += ["fprop %.6f %s" % (se[k], k) for k in ("FOV", "Time")]
        if markers and se.get("Target") is not None:
            lines.append("eprop %d Target" % (index + 1 + se["Target"]))
        lines.append("endentity")
        base = index + 1
        index += 1 + len(markers)
        for m in markers:
            ms = m["se1"]
            lines.append(placed("CameraMarker", m["position"], m["rotation"]))
            lines += ["fprop %.6f %s" % (ms[k], k) for k in ("Delta time", "Tension", "Bias", "Continuity", "FOV")]
            lines += ["bprop %d %s" % (1 if ms[k] else 0, k) for k in ("Stop moving", "Skip to next")]
            if ms.get("Target") is not None:
                lines.append("eprop %d Target" % (base + ms["Target"]))
            if ms.get("Trigger") is not None:
                lines.append(link(ms["Trigger"], "Trigger"))
            lines.append("endentity")
        stats["cameras"] += 1
        stats["camera markers"] += len(markers)
    for mu in desc["music"]:
        se = mu["se1"]
        if se["class"] == "MusicHolder":
            ids[mu["entity_id"]] = (index, "MusicHolder")
            lines.append(placed("MusicHolder", mu["position"]))
            for k in ("Music Light", "Music Medium", "Music Heavy"):
                lines.append("sprop %s = %s" % (k, deploy(se[k], "music")))
            lines += ["fprop %.6f %s" % (se[k], k) for k in ("Score Medium", "Score Heavy")]
            lines.append("endentity")
            index += 1
            stats["music holders"] += 1
        else:
            # NE swaps a whole three-layer track; SE1 changes one channel a
            # changer. A trigger aimed at the NE entity reaches the first.
            ids[mu["entity_id"]] = (index, "MusicChanger")
            for layer, mtype in zip(se["layers"], MUSIC_TYPES):
                lines.append(placed("MusicChanger", mu["position"]))
                lines.append("sprop Music = %s" % deploy(layer, "music"))
                lines.append("iprop %d Type" % MUSIC_TYPES.index(mtype))
                lines.append("endentity")
                index += 1
                stats["music changers"] += 1
    for snd in desc["sounds"]:
        se = snd.get("se1")
        if not se:
            continue
        ids[snd["entity_id"]] = (index, "SoundHolder")
        lines.append(placed("SoundHolder", snd["position"]))
        lines.append("sprop Name = %s" % se["Name"])
        lines.append("sprop Sound = %s" % deploy(se["Sound"], "sound"))
        lines += ["fprop %.6f %s" % (se[k], k) for k in ("Fall-off", "Hot-spot", "Volume")]
        lines += ["bprop %d %s" % (1 if se[k] else 0, k) for k in ("Looping", "Auto start", "Destroyable")]
        lines.append("endentity")
        index += 1
        stats["sound holders"] += 1
    for msg in desc["messages"]:
        se = msg["se1"]
        ids[msg["entity_id"]] = (index, "MessageHolder")
        lines.append(placed("MessageHolder", msg["position"]))
        lines.append("sprop Name = %s" % se["Name"])
        lines.append("sprop Message = %s" % deploy(se["Message"], "message"))
        lines.append("bprop %d Active" % (1 if se["Active"] else 0))
        lines.append("endentity")
        index += 1
        stats["message holders"] += 1
    for tf in desc["touch_fields"]:
        if not tf.get("volume"):
            stats["touch fields without a volume"] += 1
            continue
        se = tf["se1"]
        ids[tf["entity_id"]] = (index, "TouchField")
        lines.append(placed("TouchField", tf["position"]))
        lines.append("sprop Name = %s" % tf["name"])
        lines.append("bprop %d Active" % (1 if se["Active"] else 0))
        if se.get("Enter Target") is not None:
            lines.append(link(se["Enter Target"], "Enter Target"))
        lines += volume_lines(tf["volume"], tf["position"])
        lines.append("endentity")
        index += 1
        stats["touch fields"] += 1
    for bo in desc["bouncers"]:
        ids[bo["entity_id"]] = (index, "Bouncer")
        lines.append(placed("Bouncer", bo["position"]))
        lines.append("sprop Name = %s" % bo["name"])
        lines.append("fprop %.6f Speed [m/s]" % bo["se1"]["Speed"])
        lines.append("aprop %.6f %.6f %.6f Direction" % tuple(bo["direction"]))
        # the pad it covers is drawn by the level already
        lines += volume_lines(bo["volume"], bo["position"], BPOF_INVISIBLE | UNLIT)
        lines.append("endentity")
        index += 1
        stats["bouncers"] += 1
    return lines, index - first


def light_lines(desc):
    """The level's lights as Light entities (tools/world.py, se1_light)."""
    lines, count = [], 0
    identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    for l in desc["lights"]:
        if l.get("skip"):
            continue
        p = l["props"]
        rows = facing(l["direction"]) if "direction" in l else identity
        flat = " ".join("%.6f" % x for r in rows for x in r)
        pos = l["position"]
        lines.append("entity Light %.6f %.6f %.6f %s" % (pos[0], pos[1], pos[2], flat))
        lines.append("sprop Name = %s" % p["Name"])
        lines.append("iprop %d Type" % LIGHT_TYPES[p["Type"]])
        lines.append("cprop %s Color" % rgba(p["Color"]))
        if "Directional ambient" in p:
            lines.append("cprop %s Directional ambient" % rgba(p["Directional ambient"]))
        lines.append("bprop %d Dark light" % (1 if p["Dark light"] else 0))
        lines.append("bprop %d Dynamic" % (1 if p["Dynamic"] else 0))
        for name in ("Hot-spot", "Fall-off"):
            if name in p:
                lines.append("fprop %.6f %s" % (p[name], name))
        lines.append("endentity")
        count += 1
    return lines, count


def brush_entities(desc, groups, ids):
    """Brush entities and their motion markers, as entity blocks.

    Geometry goes in local space (the polygons come in world space), which is
    what the entity's own brush expects.
    """
    lines, index = [], 0
    for b in desc["brush_entities"]:
        rows, trans = b["matrix"]["brush_rows"], b["matrix"]["translation"]
        geometry = None
        g = groups.get(b["group"]) if b["group"] else None
        if g and g["polygons"]:
            used, indexed = compact(g["polygons"], lambda v: to_local(rows, trans, v))
            mat = material(g)
            geometry = (used, [mat], [(0, x) for x in indexed])
        motion = b.get("motion") or {}
        markers = motion.get("markers") or []
        iBrush = index
        if b["entity_id"] is not None:
            ids[b["entity_id"]] = (iBrush, b["class"])
        index += 1
        iFirstMarker = index
        index += len(markers)
        links = [("Target", iFirstMarker)] if markers else []
        lines += entity_lines(b["class"], trans, rows, se1_props(motion.get("se1")),
                              links, geometry)
        for im, m in enumerate(markers):
            target = (m.get("se1") or {}).get("Target")
            mlinks = [("Target", iFirstMarker + target)] if target is not None else []
            lines += entity_lines("MovingBrushMarker", m["position"], m["rows"],
                                  se1_props(m.get("se1")), mlinks)
    return lines, index


def write_source(stem, out_path):
    desc = json.loads((ROOT / "build" / "world" / (stem + ".json")).read_text())
    groups = level_polygons(stem)
    lines = ["# generated by tools/wldprep.py from build/world/%s.json" % stem,
             "world %s" % stem,
             "polyflags 0",
             "ambient %08x" % AMBIENT]
    stats = {"sectors": 0, "polygons": 0, "objects": 0, "rooms": 0, "portals": 0}
    objects = []                          # (materials, polygons) of each separate piece

    def sectors_of(content, ambient, name, mats, polys, place=lambda v: v):
        out = []
        for ix in split_sectors(polys):
            out += sector_lines(content, ambient, name, mats, [polys[i] for i in ix], place)
            stats["sectors"] += 1
        stats["polygons"] += len(polys)
        return out

    base_mats, base_polys = [], []         # the zoning world base, all its sectors' polygons
    for sector in desc["worldbase"]["sectors"]:
        mats, polys = gather(groups, sector["groups"])
        if not polys:
            continue
        ambient = sector["ambient"] if sector["ambient"] is not None else AMBIENT
        if not sector.get("liquid_surface"):
            # The static geometry: its largest connected piece is the world
            # base, every other piece (a column, a statue, a floating
            # platform) a WorldBase of its own, as separate brushes.
            parts = pieces(polys)
            objects += [(sector["content"], ambient, mats, [polys[i] for i in ix])
                        for ix in sorted(parts[1:], key=lambda ix: ix[0])]
            polys = [polys[i] for i in parts[0]]
        base_polys += [(m + len(base_mats), p) for m, p in polys]
        base_mats += mats
    room_stats = defaultdict(int)
    occupied = [loops[0][0] for _c, _a, _m, ps in objects for _mi, (loops, _p, _s, _l) in ps]
    occupied += rooms.occupied_points(desc, groups)
    rlines, nrooms = room_lines(base_mats, base_polys, occupied, room_stats)
    lines += rlines
    stats["sectors"] += nrooms
    stats["polygons"] += len(base_polys)
    stats["rooms"] = nrooms
    stats["portals"] = room_stats["portal polygons"]
    stats["room stats"] = dict(room_stats)
    # Brush entities first: their entity links count from the first entity.
    ids = {}                              # NE entity id -> (entity index, SE1 class)
    elines, nent = brush_entities(desc, groups, ids)
    lines += elines
    identity = " ".join("%.6f" % x for x in (1, 0, 0, 0, 1, 0, 0, 0, 1))
    for content, ambient, mats, polys in objects:
        ps = [p for _m, (loops, _pieces, _samples, _light) in polys for p in loops[0]]
        lo = [min(p[k] for p in ps) for k in range(3)]
        hi = [max(p[k] for p in ps) for k in range(3)]
        at = tuple((lo[k] + hi[k]) / 2 for k in range(3))
        lines.append("entity WorldBase %.6f %.6f %.6f %s" % (at[0], at[1], at[2], identity))
        lines += sectors_of(content, ambient, "object", mats, polys,
                            lambda v, at=at: tuple(v[k] - at[k] for k in range(3)))
        lines.append("endentity")
        stats["objects"] += 1
        nent += 1
    llines, nlights = light_lines(desc)
    lines += llines
    nent += nlights
    stats["lights"] = nlights
    rlines, nregion = region_lines(desc, nent)
    lines += rlines
    nent += nregion
    point_stats = defaultdict(int)
    plines, npoints = point_lines(desc, nent, point_stats, ids)
    lines += plines
    nent += npoints
    stats["points"] = dict(point_stats)
    script_stats = defaultdict(int)
    slines, nscripts = script_lines(desc, nent, script_stats, ids)
    lines += slines
    nent += nscripts
    lines = resolve_links(lines, ids, script_stats)
    stats["scripts"] = dict(script_stats)
    stats["cells"] = len(desc["region_brush"]["sectors"])
    lines.append("end")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # names come from the disc's text as Latin-1 bytes, as the writer reads them
    out_path.write_text("\n".join(lines) + "\n", encoding="latin-1", errors="replace")
    return stats, nent


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("levels", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-o", "--output", type=Path, default=ROOT / "build" / "wld")
    args = ap.parse_args(argv)
    stems = ([p.stem for p in sorted((ROOT / "build" / "world").glob("*.json"))]
             if args.all else args.levels)
    if not stems:
        ap.error("name a level, or --all")
    for stem in stems:
        out = args.output / (stem + ".wldsrc")
        stats, nent = write_source(stem, out)
        (args.output / (stem + ".stats.json")).write_text(json.dumps(stats, indent=1, default=str))
        print("%-12s %d sectors (%d rooms, %d portals), %d polygons, %d separate objects, %d lights, %d region cells, %d entities -> %s\n             %s"
              % (stem, stats["sectors"], stats["rooms"], stats["portals"], stats["polygons"], stats["objects"],
                 stats["lights"], stats["cells"], nent, out.relative_to(ROOT),
                 ", ".join("%d %s" % (v, k) for k, v in sorted(stats["points"].items()))))
        print("             " + ", ".join("%d %s" % (v, k) for k, v in sorted(stats["scripts"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
