#!/usr/bin/env python3
"""
Stage 2 textures: every level texture a polygon uses, diffuse or in texture
layer 1 or 2, as a Serious Engine `.tex` (docs/world-conversion.md,
"Textures").

Levels carry their own copies of shared textures, so identical images (same
size and decoded pixels) become one file, named after the first level and
texture index that use it. For each one this writes

    pc/engine/SamTSE/Textures/NextEncounter/<name>.tga    32-bit, top-down
    build/wld/textures.lst     "<tga path> <mex width>" for WldWriter --tex
    build/wld/textures.json    {level: {texture index: {"tex", "mex"}}}

**Size in mexels.** A `.tex` has a size in the world as well as in pixels:
1024 mex is 1 metre, and the mex width must be the pixel width times a power
of two (`CTextureData::Create_t`). The power chosen is the one closest to the
world size of one UV tile on the triangles that use the texture (median, in
the UV set each use draws with), so the polygon mappings come out near
stretch 1, as a designer would set them.

    python tools/wldtex.py            # all levels
    python tools/wldtex.py Rlevel0_1
    python tools/wldtex.py --check Rlevel0_1

`--check` measures a converted level two ways, for layer 0 and each texture
layer. The fit: how far each polygon's `uv` and `layer` maps (tools/wldprep.py)
are from the disc's own UVs at its corners. The world: `WldWriter --dump` lists the saved brushes, and the
texture coordinate SE1 draws at each brush vertex from the plane's default
mapping vectors, as the engine builds them, and the polygon's mapping
(s·UoS + t·UoT − UOffset: CMappingDefinition::MakeMappingVectors as the
renderer uses it, not GetTextureCoordinates, which adds the offset) is
compared with the
maps of the source polygons that share the vertex, layer and texture, modulo
whole tiles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import level                                                      # noqa: E402
from gxtex import decode, walk                                    # noqa: E402
from ssg import Container                                         # noqa: E402

GAME = ROOT / "pc" / "engine" / "SamTSE"
BS = chr(92)
TEX_DIR = "Textures" + BS + "NextEncounter"


def owners(container):
    """{owner key: [(texture index, UV field)]} for materials and brush nodes:
    the diffuse texture and each texture layer's."""
    out = {}
    for m in list(level.materials(container)) + list(level.sectors(container)):
        out[(m["kind"], m["index"])] = [(m["diffuse"], level.F_TEX0)] + [
            (x["texture"], x["field"]) for x in m["layers"]]
    return out


def tile_sizes(container, pts, uvsets):
    """{(owner key, UV field): [metres per UV tile, one per textured triangle]}."""
    out = defaultdict(list)
    for owner, tris in level.batches(container):
        key = (owner["kind"], owner["index"]) if owner else None
        fields = [level.F_TEX0] + [x["field"] for x in (owner or {}).get("layers", ())]
        for f in fields:
            uvs = uvsets[f]
            for t in tris:
                if max(v[level.F_POS] for v in t) >= len(pts) or max(v[f] for v in t) >= len(uvs):
                    continue
                p = [pts[v[level.F_POS]] for v in t]
                q = [uvs[v[f]] for v in t]
                e1 = [p[1][k] - p[0][k] for k in range(3)]
                e2 = [p[2][k] - p[0][k] for k in range(3)]
                c = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
                area = math.sqrt(sum(x * x for x in c))
                uv_area = abs((q[1][0] - q[0][0]) * (q[2][1] - q[0][1]) - (q[2][0] - q[0][0]) * (q[1][1] - q[0][1]))
                if area > 1e-4 and uv_area > 1e-8:
                    out[(key, f)].append(math.sqrt(area / uv_area))
    return out


def tga(path, w, h, rgba):
    """Uncompressed TGA, rows top-down (flag 0x20); 32-bit only if any pixel
    is not opaque. -> whether it has alpha."""
    alpha = any(rgba[i] != 255 for i in range(3, len(rgba), 4))
    head = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, w, h, 32 if alpha else 24, 0x20)
    body = bytearray()
    for i in range(0, len(rgba), 4):
        body += bytes((rgba[i + 2], rgba[i + 1], rgba[i])) + (bytes((rgba[i + 3],)) if alpha else b"")
    path.write_bytes(head + bytes(body))
    return alpha


SLIVER = 0.01          # square units


def _area(loop):
    n = [0.0, 0.0, 0.0]
    for p0, p1 in zip(loop, loop[1:] + loop[:1]):
        n = [n[0] + p0[1] * p1[2] - p0[2] * p1[1], n[1] + p0[2] * p1[0] - p0[0] * p1[2],
             n[2] + p0[0] * p1[1] - p0[1] * p1[0]]
    return math.sqrt(sum(x * x for x in n)) / 2


def check(stem):
    import subprocess
    sys.path.insert(0, str(ROOT / "tools"))
    import wldprep

    # 1. the fit, against the disc's corner UVs, for the diffuse texture and each layer
    groups = wldprep.level_polygons(stem)
    worst_fit, fitted, unfit, slivers = [0.0] * 3, [0] * 3, [0] * 3, [0] * 3
    for g in groups.values():
        textures = {0: g["texture"]}
        textures.update({k: x["texture"] for k, x in g["layers"].items()})
        for loops, _pieces, samples, _light in g["polygons"]:
            for k, tex in textures.items():
                if not tex:
                    continue
                m = wldprep.fit_uv(samples, tex["mex"], k)
                if m is None:
                    unfit[k] += 1
                    continue
                if _area(loops[0]) < SLIVER:
                    slivers[k] += 1              # too thin for a plane, and invisible
                    continue
                a, a0, b, b0 = m
                mw, mh = tex["mex"]
                for p, uvs in samples:
                    if uvs[k] is None:
                        continue
                    du = (sum(a[i] * p[i] for i in range(3)) + a0) / mw - uvs[k][0]
                    dv = (sum(b[i] * p[i] for i in range(3)) + b0) / mh - uvs[k][1]
                    worst_fit[k] = max(worst_fit[k], abs(du), abs(dv))
                fitted[k] += 1
    for k in range(3):
        print("fit, layer %d: %d polygons mapped, %d without a map; worst corner %.5f of a tile (%d slivers under "
              "%g square units not measured)" % (k, fitted[k], unfit[k], worst_fit[k], slivers[k], SLIVER))

    # 2. the saved world: SE1's coordinates at each brush vertex, in every layer
    src = (ROOT / "build" / "wld" / (stem + ".wldsrc")).read_text().split("\n")
    # (layer, texture, grid cell) -> [(vertex, (a, a0, b, b0, mexW, mexH))].
    # Found by distance, not by rounding: object-local coordinates sit on exact
    # halves (4.0625) give or take 1e-6, which rounds either way.
    maps = defaultdict(list)

    def near(k, tex, p):
        cell = tuple(math.floor(c * 100) for c in p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for q, m in maps.get((k, tex, (cell[0] + dx, cell[1] + dy, cell[2] + dz)), ()):
                        if max(abs(q[i] - p[i]) for i in range(3)) < 1e-3:
                            yield m
    verts, mats = [], []
    left, cur_poly, cur_mat = 0, None, None

    def remember(k, d, clamp=0):
        if cur_mat is None or k >= len(cur_mat) or cur_mat[k] == "-":
            return
        m = (d[2:5], d[5], d[6:9], d[9], d[0], d[1], clamp)
        for p in cur_poly:
            maps[(k, cur_mat[k].lower(), tuple(math.floor(c * 100) for c in p))].append((p, m))
    base = None                  # a uv line's map, kept in case a layer 0 line clamps it
    for line in src:
        t = line.split()
        if not t:
            continue
        if left:
            verts.append(tuple(float(x) for x in t))
            left -= 1
            continue
        if t[0] == "verts":
            verts, left = [], int(t[1])
        elif t[0] in ("sector", "entity"):
            mats = []
        elif t[0] == "mat":
            mats.append(t[1:])
        elif t[0] == "poly":
            if base:
                remember(0, base)
                base = None
            cur_mat, cur_poly = mats[int(t[1])], [verts[int(i)] for i in t[3:]]
        elif t[0] in ("hole", "split"):
            cur_poly = cur_poly + [verts[int(i)] for i in t[2:]]
        elif t[0] == "uv":
            base = [float(x) for x in t[1:]]
        elif t[0] == "layer":
            if t[1] == "0":
                base = None
            remember(int(t[1]), [float(x) for x in t[6:]], int(t[4], 16) & 3)
    if base:
        remember(0, base)
    game = GAME / "Bin"
    dump = ROOT / "build" / "wldlog" / (stem + ".dump.txt")
    dump.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(game / "WldWriter.exe"), "--dump", BS.join(["Levels", "NextEncounter", stem + ".wld"]),
                    str(dump)], cwd=game, check=True, capture_output=True)
    checked, matched, skipped, worst = [0] * 3, [0] * 3, [0] * 3, [0.0] * 3
    poly = None

    def _brush_area(pl):
        # edges in any order: the sum of their cross products is the area vector
        n = [0.0, 0.0, 0.0]
        for a_, b_ in pl["edges"]:
            n = [n[0] + a_[1] * b_[2] - a_[2] * b_[1], n[1] + a_[2] * b_[0] - a_[0] * b_[2],
                 n[2] + a_[0] * b_[1] - a_[1] * b_[0]]
        return math.sqrt(sum(x * x for x in n)) / 2

    def tile_error(p, se1, m):
        # whole tiles do not count on an axis that repeats; they do where it clamps
        a, a0, b, b0, mw, mh, clamp = m
        du = (se1[0] - (sum(a[i] * p[i] for i in range(3)) + a0)) / mw
        dv = (se1[1] - (sum(b[i] * p[i] for i in range(3)) + b0)) / mh
        return max(abs(du if clamp & 1 else du - round(du)), abs(dv if clamp & 2 else dv - round(dv)))

    def finish():
        # A brush polygon's layer passes if one source map found at its corners
        # fits all its vertices, including the ones import adds where a
        # neighbour's corner lies on its edge (those sit in the neighbour's list).
        if poly is None:
            return
        o, u_ax, v_ax = poly["mv"]
        for k, (md, tex) in poly["layers"].items():
            cands = {id(m): m for p in poly["verts"] for m in near(k, tex.lower(), p)}
            if not cands:
                continue
            if _brush_area(poly) < SLIVER:
                skipped[k] += 1
                continue
            se1 = []
            for p in poly["verts"]:
                off = [p[i] - o[i] for i in range(3)]
                s_ = sum(u_ax[i] * off[i] for i in range(3))
                t_ = sum(v_ax[i] * off[i] for i in range(3))
                se1.append(((s_ * md[0] + t_ * md[1] - md[4]) * 1024.0, (s_ * md[2] + t_ * md[3] - md[5]) * 1024.0))
            best = min(max(tile_error(p, c, m) for p, c in zip(poly["verts"], se1)) for m in cands.values())
            checked[k] += 1
            matched[k] += best < 1e-2
            worst[k] = max(worst[k], best)

    for line in dump.read_text().splitlines():
        t = line.split()
        if t[0] == "poly":
            finish()
            poly = {"plane": [float(x) for x in t[2:6]], "layers": {}, "verts": [], "edges": []}
        elif t[0] == "mv":
            m = [float(x) for x in t[1:10]]
            poly["mv"] = (m[0:3], m[3:6], m[6:9])
        elif t[0] == "layer" and t[12] != "-":
            poly["layers"][int(t[1])] = ([float(x) for x in t[6:12]], t[12])
        elif t[0] == "e":
            e = [float(x) for x in t[1:7]]
            poly["verts"].append(tuple(e[0:3]))
            poly["edges"].append((e[0:3], e[3:6]))
    finish()
    for k in range(3):
        print("world, layer %d: %d brush polygons checked, %d where SE1's coordinates at every vertex are within 1%% "
              "of a tile of a source map (worst %.4f; %d slivers not measured)"
              % (k, checked[k], matched[k], worst[k], skipped[k]))
    # layers join polygons within 2% of a tile (tools/polygons.py, LAYER_UV_EPS)
    ok = checked[0] and all(matched[k] == checked[k] and worst_fit[k] < (1e-2 if k == 0 else 2e-2) for k in range(3))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("levels", nargs="*")
    ap.add_argument("--check", metavar="LEVEL", help="measure a converted level's texture mapping")
    args = ap.parse_args(argv)
    if args.check:
        return check(args.check)
    paths = ([ROOT / "orig" / "files" / "Levels" / (n + ".ssw") for n in args.levels] if args.levels
             else sorted((ROOT / "orig" / "files" / "Levels").glob("*.ssw")))

    registry = {}                    # pixel hash -> record
    per_level = {}
    for path in paths:
        cont = Container(path)
        img = cont.image
        texs = list(walk(img))
        pts = level.positions(img)
        uvsets = {f: level.texcoords(img, level.FIELD_ARRAY[f]) for f in (level.F_TEX0, level.F_TEX1, level.F_TEX2)}
        sizes = tile_sizes(cont, pts, uvsets)
        used = defaultdict(list)
        for key, uses in owners(cont).items():
            for ti, f in uses:
                if ti is not None and ti < len(texs):
                    used[ti] += sizes.get((key, f), [])
        per_level[path.stem] = {}
        for ti in sorted(used):
            t = texs[ti]
            rgba = bytes(decode(img, t.pixels, t.width, t.height, t.gx))
            digest = hashlib.sha1(struct.pack("<II", t.width, t.height) + rgba).hexdigest()
            rec = registry.get(digest)
            if rec is None:
                rec = registry[digest] = {"name": "%s_%03d" % (path.stem, ti), "width": t.width,
                                          "height": t.height, "rgba": rgba, "tiles": [], "uses": 0}
            rec["tiles"] += used[ti]
            rec["uses"] += 1
            per_level[path.stem][ti] = digest
        print("%-12s %3d textures used" % (path.stem, len(used)))

    out_dir = GAME / "Textures" / "NextEncounter"
    out_dir.mkdir(parents=True, exist_ok=True)
    lst, alphas = [], 0
    for digest, rec in registry.items():
        tiles = sorted(rec["tiles"])
        metres = tiles[len(tiles) // 2] if tiles else 1.0
        power = max(0, round(math.log2(max(metres * 1024.0 / rec["width"], 1e-9))))
        power = min(power, 16)
        rec["mex"] = [rec["width"] << power, rec["height"] << power]
        alpha = tga(out_dir / (rec["name"] + ".tga"), rec["width"], rec["height"], rec.pop("rgba"))
        rec["alpha"] = alpha
        alphas += alpha
        lst.append("%s %d" % (TEX_DIR + BS + rec["name"] + ".tga", rec["mex"][0]))

    wld = ROOT / "build" / "wld"
    wld.mkdir(parents=True, exist_ok=True)
    (wld / "textures.lst").write_text("\n".join(lst) + "\n")
    table = {lvl: {str(ti): {"tex": TEX_DIR + BS + registry[d]["name"] + ".tex", "mex": registry[d]["mex"],
                             "alpha": registry[d]["alpha"]}
                   for ti, d in m.items()} for lvl, m in per_level.items()}
    (wld / "textures.json").write_text(json.dumps(table, indent=1))
    uses = sum(len(m) for m in per_level.values())
    print("%d level textures -> %d distinct images (%d with alpha) -> %s" % (uses, len(registry), alphas, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
