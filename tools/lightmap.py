#!/usr/bin/env python3
"""
A level's baked lightmap, NE's own beside the one SE1 bakes from its lights.

NE's world lighting is baked: every world material binds a 1024x1024 atlas
(`CcMaterial+0x20`, 2 to 4 per level) and vertex field 6 indexes array 7, a
u16 pair per vertex that addresses it in 1/65536ths (docs/image-format.md,
"The lightmap atlas"). Its values are SE1 shadow map values: 127 is a surface
at the texture's own brightness.

`pc/wldwriter` re-bakes the converted world from its `Light` entities
(`WldWriter --lightmap` dumps every polygon's mixed shadow map). This draws
both top-down from the same view, the highest surface under the ceiling
clip, and measures them where both draw the same surface.

    python tools/lightmap.py render Rlevel1_1 [--dump build/render/Rlevel1_1_se1_lightmap.txt]

Writes build/render/<level>_lightmap.png: NE | SE1 | difference (grey is
equal, red SE1 brighter, blue SE1 darker).
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import level                                                      # noqa: E402
import space                                                      # noqa: E402
from gxtex import write_png                                       # noqa: E402
from ssg import Container                                         # noqa: E402

MAT_ATLAS = 0x20          # CcMaterial: the lightmap atlas, bound to TEV stage 5
SEC_ATLAS = 0x68          # movable geometry node: its lightmap atlas (46 of 62 on Rlevel1_1)
F_LIGHTMAP = 6            # vertex field 6 -> array 7: u16 u, u16 v
LIGHTMAP_ARRAY = 7
UV_ONE = 65536.0
SAME_SURFACE = 0.05       # heights this close are the same surface in both views
BACKGROUND = (14, 16, 22)


def lightmap_uvs(img):
    for i, data, size, count, stride in level.arrays(img):
        if i == LIGHTMAP_ARRAY and stride == 4:
            return [struct.unpack_from(">HH", img, data + n * 4) for n in range(count)]
    return []


class Baked:
    """A level's baked lightmap, sampled over render triangles."""

    # barycentric points per triangle: its centre and towards each corner
    POINTS = ((1 / 3, 1 / 3, 1 / 3), (2 / 3, 1 / 6, 1 / 6), (1 / 6, 2 / 3, 1 / 6), (1 / 6, 1 / 6, 2 / 3))

    def __init__(self, container):
        self.img = container.image
        self.uvs = lightmap_uvs(self.img)
        self.texs = list(level._texwalk(self.img))
        self.by_offset = {t.offset: k for k, t in enumerate(self.texs)}
        self.pixels = {}

    def atlas(self, owner):
        """(width, height, RGBA pixels) of the owner's atlas, or None."""
        field = {"material": MAT_ATLAS, "sector": SEC_ATLAS}.get((owner or {}).get("kind"))
        if field is None:
            return None
        ti = self.by_offset.get(level.u32(self.img, owner["offset"] + field))
        if ti is None:
            return None
        px = self.pixels.get(ti)
        if px is None:
            t = self.texs[ti]
            px = self.pixels[ti] = (t.width, t.height, level._decode(self.img, t.pixels, t.width, t.height, t.gx))
        return px

    def samples(self, owner, triangles):
        """RGB of the lightmap at POINTS of every triangle; [] without one."""
        a = self.atlas(owner)
        if a is None:
            return []
        w, h, px = a
        out = []
        for tri in triangles:
            if any(v[F_LIGHTMAP] >= len(self.uvs) for v in tri):
                continue
            uv = [self.uvs[v[F_LIGHTMAP]] for v in tri]
            for b in self.POINTS:
                u = sum(b[k] * uv[k][0] for k in range(3)) / UV_ONE
                v = sum(b[k] * uv[k][1] for k in range(3)) / UV_ONE
                k = ((int(v * h) % h) * w + int(u * w) % w) * 4
                out.append((px[k], px[k + 1], px[k + 2]))
        return out


class View:
    """Top-down: x across, original-space z down, keeping the highest surface."""

    def __init__(self, pts, width, clip=99.0):
        xs = sorted(p[0] for p in pts)
        zs = sorted(p[2] for p in pts)
        n = len(xs) - 1
        self.x0, x1 = xs[n // 100], xs[n * 99 // 100]
        self.z0, z1 = zs[n // 100], zs[n * 99 // 100]
        self.W = width
        self.s = (width - 20) / max(1e-6, x1 - self.x0)
        self.H = max(64, min(4096, int((z1 - self.z0) * self.s) + 20))
        ys = sorted(p[1] for p in pts)
        self.ceiling = ys[min(len(ys) - 1, int(len(ys) * clip / 100))]

    def canvas(self):
        return [BACKGROUND] * (self.W * self.H), [-1e30] * (self.W * self.H)

    def fill(self, rgb, height, p3, shade):
        """Rasterize triangle p3 (absolute points); shade(b0, b1, b2) -> rgb."""
        if sum(q[1] for q in p3) / 3.0 > self.ceiling:
            return
        W, H = self.W, self.H
        sx = [10 + (p[0] - self.x0) * self.s for p in p3]
        sy = [10 + (p[2] - self.z0) * self.s for p in p3]
        minx, maxx = max(0, int(min(sx))), min(W - 1, int(max(sx)) + 1)
        miny, maxy = max(0, int(min(sy))), min(H - 1, int(max(sy)) + 1)
        if minx > maxx or miny > maxy or (maxx - minx) * (maxy - miny) > 4_000_000:
            return
        ax, ay = sx[0], sy[0]
        d1x, d1y = sx[1] - ax, sy[1] - ay
        d2x, d2y = sx[2] - ax, sy[2] - ay
        den = d1x * d2y - d2x * d1y
        if abs(den) < 1e-9:
            return
        for py in range(miny, maxy + 1):
            wy = py + 0.5 - ay
            for px in range(minx, maxx + 1):
                wx = px + 0.5 - ax
                b1 = (wx * d2y - d2x * wy) / den
                b2 = (d1x * wy - wx * d1y) / den
                b0 = 1.0 - b1 - b2
                if b0 < -0.002 or b1 < -0.002 or b2 < -0.002:
                    continue
                i = py * W + px
                h = b0 * p3[0][1] + b1 * p3[1][1] + b2 * p3[2][1]
                if h < height[i]:
                    continue
                height[i] = h
                rgb[i] = shade(b0, b1, b2)


def draw_ne(stem, view=None, width=1024):
    """NE's baked lightmap, sampled through each world triangle's lightmap UVs."""
    cont = Container(ROOT / "orig" / "files" / "Levels" / (stem + ".ssw"))
    img = cont.image
    pts = level.positions(img)
    luv = lightmap_uvs(img)
    baked = Baked(cont)
    world = [space.world(p) for p in pts]
    view = view or View(world, width)
    rgb, height = view.canvas()

    for owner, tris in level.batches(cont):
        a = baked.atlas(owner)
        if a is None:
            continue
        w, h, px = a
        xf = owner.get("xform")
        for tri in tris:
            if any(v[level.F_POS] >= len(pts) or v[F_LIGHTMAP] >= len(luv) for v in tri):
                continue
            uv = [luv[v[F_LIGHTMAP]] for v in tri]

            def shade(b0, b1, b2, uv=uv, w=w, h=h, px=px):
                u = (b0 * uv[0][0] + b1 * uv[1][0] + b2 * uv[2][0]) / UV_ONE
                v = (b0 * uv[0][1] + b1 * uv[1][1] + b2 * uv[2][1]) / UV_ONE
                k = ((int(v * h) % h) * w + int(u * w) % w) * 4
                return px[k], px[k + 1], px[k + 2]

            p3 = ([world[v[level.F_POS]] for v in tri] if xf is None else
                  [space.world(level.apply_xform(xf, pts[v[level.F_POS]])) for v in tri])
            view.fill(rgb, height, p3, shade)
    return view, rgb, height


def read_dump(path):
    """WldWriter --lightmap: (frame, triangles, texels) per polygon."""
    with open(path) as f:
        head = None
        tris = []
        for line in f:
            t = line.split()
            if not t:
                continue
            if t[0] == "p":
                w, h = int(t[1]), int(t[2])
                o = tuple(map(float, t[3:6]))
                u = tuple(map(float, t[6:9]))
                v = tuple(map(float, t[9:12]))
                head = (w, h, o, u, v)
                tris = []
            elif t[0] == "t":
                c = list(map(float, t[1:10]))
                tris.append((tuple(c[0:3]), tuple(c[3:6]), tuple(c[6:9])))
            elif t[0] == "m":
                yield head, tris, bytes.fromhex(t[1])


def draw_se1(dump, view):
    rgb, height = view.canvas()
    for (w, h, o, u, v), tris, texels in read_dump(dump):
        uu = sum(c * c for c in u) or 1.0
        vv = sum(c * c for c in v) or 1.0

        def shade_at(p, w=w, h=h, o=o, u=u, v=v, uu=uu, vv=vv, texels=texels):
            d = (p[0] - o[0], p[1] - o[1], p[2] - o[2])
            fi = (d[0] * u[0] + d[1] * u[1] + d[2] * u[2]) / uu
            fj = (d[0] * v[0] + d[1] * v[1] + d[2] * v[2]) / vv
            i0, j0 = int(math.floor(fi)), int(math.floor(fj))
            ai, aj = fi - i0, fj - j0
            out = [0.0, 0.0, 0.0]
            for di, dj, wt in ((0, 0, (1 - ai) * (1 - aj)), (1, 0, ai * (1 - aj)),
                               (0, 1, (1 - ai) * aj), (1, 1, ai * aj)):
                i = min(w - 1, max(0, i0 + di))
                j = min(h - 1, max(0, j0 + dj))
                k = (j * w + i) * 3
                for c in range(3):
                    out[c] += wt * texels[k + c]
            return tuple(int(round(x)) for x in out)

        for p3 in tris:
            def shade(b0, b1, b2, p3=p3):
                return shade_at(tuple(b0 * p3[0][k] + b1 * p3[1][k] + b2 * p3[2][k] for k in range(3)))

            view.fill(rgb, height, p3, shade)
    return rgb, height


def compare(view, ne, ne_h, se, se_h):
    """Per-pixel difference where both views show the same surface."""
    diff = [BACKGROUND] * len(ne)
    n = 0
    sums = [0.0, 0.0, 0.0]
    signed = [0.0, 0.0, 0.0]
    close = 0
    for i in range(len(ne)):
        if ne_h[i] < -1e29 or se_h[i] < -1e29 or abs(ne_h[i] - se_h[i]) > SAME_SURFACE:
            continue
        a, b = ne[i], se[i]
        d = [b[c] - a[c] for c in range(3)]
        n += 1
        for c in range(3):
            sums[c] += abs(d[c])
            signed[c] += d[c]
        close += max(abs(x) for x in d) <= 16
        lum = sum(d) / 3.0
        g = 128
        diff[i] = (max(0, min(255, int(g + lum))), max(0, min(255, int(g - abs(lum) * 0.5))),
                   max(0, min(255, int(g - lum))))
    stats = {"pixels": n,
             "mean abs difference": tuple(round(s / max(1, n), 1) for s in sums),
             "mean SE1 - NE": tuple(round(s / max(1, n), 1) for s in signed),
             "within 16 levels": round(close / max(1, n), 3)}
    return diff, stats


def cmd_render(args):
    stem = args.level
    dump = args.dump or ROOT / "build" / "render" / (stem + "_se1_lightmap.txt")
    view, ne, ne_h = draw_ne(stem, width=args.size)
    se, se_h = draw_se1(dump, view)
    diff, stats = compare(view, ne, ne_h, se, se_h)
    W, H = view.W, view.H
    out = bytearray()
    for y in range(H):
        for part in (ne, se, diff):
            for x in range(W):
                out += bytes(part[y * W + x]) + b"\xff"
    dest = args.output / (stem + "_lightmap.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_png(dest, W * 3, H, bytes(out))
    print(stem, stats, "->", dest)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="NE's and SE1's lightmaps top-down, and their difference")
    p.add_argument("level")
    p.add_argument("--dump", type=Path, help="WldWriter --lightmap output")
    p.add_argument("--size", type=int, default=800)
    p.add_argument("-o", "--output", type=Path, default=ROOT / "build" / "render")
    p.set_defaults(func=cmd_render)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
