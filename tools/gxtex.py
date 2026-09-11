#!/usr/bin/env python3
"""
Extract textures from Next Encounter's CLIMAX containers.

The glob image holds a singly linked list of texture nodes whose head is at
`image + 0x18` (walked by the loader at 0x8012d758). Field layout comes from
the GX setup path at 0x80131cb4, which feeds them straight to GXInitTexObj:

    struct CcTexture {
        CcTexture* next;      // +0x00
        u32        format;    // +0x04  engine format id, mapped to GX below
        u32        width;     // +0x08  power of two; loader warns and forces
        u32        height;    // +0x0c  1024 otherwise
        s32        lodCount;  // +0x10  >0 selects trilinear + max LOD
        u32        _pad14;    // +0x14
        void*      pixels;    // +0x18
        u32        handle;    // +0x1c  runtime only; loader zeroes it, so the
                              //        value on disc is stale exporter garbage
        u32        _pad20;    // +0x20
        SubRect*   subs;      // +0x24  sub-rects; the loader scales fields
                              //        [4]/[5] by 1/width, 1/height -> UVs
    };

Engine format id -> GX format, from the switch at 0x80131cf4:

    1 -> GX_TF_RGB5A3 (5)   2 -> GX_TF_RGBA8 (6)   3 -> GX_TF_CMPR (14)
    4 -> GX_TF_IA4 (2)      anything else -> GX_TF_RGB565 (4)

PNG output is written without external dependencies.

Usage:
    python tools/gxtex.py list    orig/files/Data.ssg
    python tools/gxtex.py extract orig/files/Data.ssg -o build/textures/
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container  # noqa: E402

# engine id -> (GX id, human name)
FORMATS = {
    1: (5, "RGB5A3"),
    2: (6, "RGBA8"),
    3: (14, "CMPR"),
    4: (2, "IA4"),
}
DEFAULT_FORMAT = (4, "RGB565")

NODE_NEXT, NODE_FMT, NODE_W, NODE_H = 0x00, 0x04, 0x08, 0x0C
NODE_LOD, NODE_PIXELS, NODE_HANDLE, NODE_SUBS = 0x10, 0x18, 0x1C, 0x24


class Texture:
    __slots__ = ("offset", "fmt", "gx", "name", "width", "height",
                 "lods", "pixels", "subs")

    def __init__(self, img: bytes, off: int):
        u32 = lambda o: struct.unpack_from(">I", img, o)[0]  # noqa: E731
        self.offset = off
        self.fmt = u32(off + NODE_FMT)
        self.gx, self.name = FORMATS.get(self.fmt, DEFAULT_FORMAT)
        self.width = u32(off + NODE_W)
        self.height = u32(off + NODE_H)
        self.lods = struct.unpack_from(">i", img, off + NODE_LOD)[0]
        self.pixels = u32(off + NODE_PIXELS)
        self.subs = u32(off + NODE_SUBS)


# Which root slot holds the texture list depends on the resource type:
#   GLOB (.ssg)              image + 0x18   (walker at 0x8012d758)
#   MESH / WORLDMESH (.ssw)  image + 0x0c   (walker at 0x8012e2c0)
HEAD_SLOTS = (0x18, 0x0C)


def _plausible(img: bytes, node: int) -> bool:
    """Does this node look like a CcTexture? Power-of-two dimensions are the
    strong signal - the loader itself warns and clamps when they are not."""
    if node + 0x28 > len(img):
        return False
    u32 = lambda o: struct.unpack_from(">I", img, o)[0]  # noqa: E731
    w, h, fmt, px = u32(node + NODE_W), u32(node + NODE_H), u32(node + NODE_FMT), u32(node + NODE_PIXELS)
    return (bool(w) and not (w & (w - 1)) and w <= 4096
            and bool(h) and not (h & (h - 1)) and h <= 4096
            and 0 <= fmt <= 8 and 0 < px < len(img))


def _chain(img: bytes, head: int, limit: int = 20000):
    node, seen = head, set()
    while node and node + 0x28 <= len(img) and node not in seen and len(seen) < limit:
        seen.add(node)
        yield node
        node = struct.unpack_from(">I", img, node + NODE_NEXT)[0]


def find_head(img: bytes) -> int | None:
    """Pick the root slot whose chain actually looks like textures."""
    best, best_n = None, 0
    for slot in HEAD_SLOTS:
        if slot + 4 > len(img):
            continue
        head = struct.unpack_from(">I", img, slot)[0]
        if not (0 < head < len(img) - 0x28):
            continue
        nodes = list(_chain(img, head))
        if not nodes:
            continue
        good = sum(1 for n in nodes if _plausible(img, n))
        if good > best_n and good / len(nodes) > 0.8:
            best, best_n = slot, good
    return best


def walk(img: bytes, head_field: int | None = None):
    """Yield Texture nodes. With no `head_field`, the slot is auto-detected."""
    if head_field is None:
        head_field = find_head(img)
        if head_field is None:
            return
    head = struct.unpack_from(">I", img, head_field)[0]
    for node in _chain(img, head):
        yield Texture(img, node)


# --- GX detiling / decoding -------------------------------------------------

def _rgb565(v: int):
    return ((v >> 11 & 31) * 255 // 31, (v >> 5 & 63) * 255 // 63,
            (v & 31) * 255 // 31, 255)


def _rgb5a3(v: int):
    if v & 0x8000:                       # opaque RGB555
        return ((v >> 10 & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31,
                (v & 31) * 255 // 31, 255)
    return ((v >> 8 & 15) * 255 // 15, (v >> 4 & 15) * 255 // 15,
            (v & 15) * 255 // 15, (v >> 12 & 7) * 255 // 7)


def _blocks(w: int, h: int, bw: int, bh: int):
    for by in range(0, h, bh):
        for bx in range(0, w, bw):
            yield bx, by


def decode(data: bytes, off: int, w: int, h: int, gx: int) -> bytearray:
    """Decode one mip level to a straight RGBA byte buffer."""
    out = bytearray(w * h * 4)

    def put(x, y, rgba):
        if x < w and y < h:
            i = (y * w + x) * 4
            out[i:i + 4] = bytes(rgba)

    if gx == 14:                                     # CMPR
        p = off
        for bx, by in _blocks(w, h, 8, 8):
            # an 8x8 block is four DXT1 sub-blocks, row-major
            for sy in (0, 4):
                for sx in (0, 4):
                    if p + 8 > len(data):
                        return out
                    c0, c1 = struct.unpack_from(">HH", data, p)
                    bits = struct.unpack_from(">I", data, p + 4)[0]
                    p += 8
                    p0, p1 = _rgb565(c0), _rgb565(c1)
                    if c0 > c1:
                        pal = (p0, p1,
                               tuple((2 * a + b) // 3 for a, b in zip(p0, p1)),
                               tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)))
                    else:
                        pal = (p0, p1,
                               tuple((a + b) // 2 for a, b in zip(p0, p1)),
                               (0, 0, 0, 0))
                    for py in range(4):
                        for px in range(4):
                            # GC packs the 2-bit indices MSB-first
                            idx = (bits >> (30 - 2 * (py * 4 + px))) & 3
                            c = pal[idx]
                            if idx == 3 and c0 <= c1:
                                c = (0, 0, 0, 0)
                            put(bx + sx + px, by + sy + py, c)
        return out

    if gx in (4, 5):                                 # RGB565 / RGB5A3, 4x4
        conv = _rgb565 if gx == 4 else _rgb5a3
        p = off
        for bx, by in _blocks(w, h, 4, 4):
            for py in range(4):
                for px in range(4):
                    if p + 2 > len(data):
                        return out
                    put(bx + px, by + py,
                        conv(struct.unpack_from(">H", data, p)[0]))
                    p += 2
        return out

    if gx == 6:                                      # RGBA8, 4x4, AR then GB
        p = off
        for bx, by in _blocks(w, h, 4, 4):
            if p + 64 > len(data):
                return out
            for i in range(16):
                a, r = data[p + i * 2], data[p + i * 2 + 1]
                g, b = data[p + 32 + i * 2], data[p + 32 + i * 2 + 1]
                put(bx + i % 4, by + i // 4, (r, g, b, a))
            p += 64
        return out

    if gx == 2:                                      # IA4, 8x4
        p = off
        for bx, by in _blocks(w, h, 8, 4):
            for py in range(4):
                for px in range(8):
                    if p >= len(data):
                        return out
                    v = data[p]
                    i = (v & 0x0F) * 255 // 15
                    a = (v >> 4) * 255 // 15
                    put(bx + px, by + py, (i, i, i, a))
                    p += 1
        return out

    raise ValueError(f"unsupported GX format {gx}")


# --- PNG (no dependencies) --------------------------------------------------

def write_png(path: Path, w: int, h: int, rgba: bytes) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                  # filter: none
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b""))


# --- subcommands ------------------------------------------------------------

def cmd_list(args) -> int:
    for path in args.files:
        c = Container(path)
        img = c.image
        print(f"=== {path} ===")
        n = 0
        for t in walk(img, args.head):
            print(f"  0x{t.offset:07x}  {t.name:<7} (id {t.fmt})  "
                  f"{t.width:>5}x{t.height:<5} lods={t.lods:<3} "
                  f"pixels=0x{t.pixels:07x} subs=0x{t.subs:x}")
            n += 1
        print(f"  {n} textures")
    return 0


def cmd_extract(args) -> int:
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    total = failed = 0
    for path in args.files:
        c = Container(path)
        img = c.image
        for i, t in enumerate(walk(img, args.head)):
            total += 1
            if not (t.width and t.height) or t.width > 4096 or t.height > 4096:
                print(f"  skip {i}: implausible {t.width}x{t.height}")
                failed += 1
                continue
            try:
                rgba = decode(img, t.pixels, t.width, t.height, t.gx)
            except Exception as e:                      # noqa: BLE001
                print(f"  skip {i} ({t.name}): {e}")
                failed += 1
                continue
            name = f"{path.stem}_{i:03d}_{t.name}_{t.width}x{t.height}.png"
            write_png(out / name, t.width, t.height, bytes(rgba))
            if args.verbose:
                print(f"  {name}")
    print(f"{total - failed}/{total} textures written to {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--head", type=lambda s: int(s, 0), default=None,
                    help="image offset holding the list head "
                         "(default: auto-detect 0x18 glob / 0x0c mesh)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list texture nodes")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("extract", help="decode textures to PNG")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/textures"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
