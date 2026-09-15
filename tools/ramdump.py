#!/usr/bin/env python3
"""
Read a Dolphin MEM1 dump (Debug > Memory > Dump MEM1: 24 MiB at 0x80000000)
against the disc. docs/toolchain.md, "RAM dumps".

    python tools/ramdump.py info [DUMP]       # level, image base, touch objects
    python tools/ramdump.py level [DUMP]      # just which level is loaded

`info` finds the loaded level by locating chunks of each level image in RAM.
Shared textures put chunks of many levels in RAM, but only the loaded level's
chunks all agree on one base address. It then checks the find exactly: every
pointer word of the image must equal base + its on-disc value (the loader
relocates by adding the base). Finally it reports what the trigger-volume work
needs:
  - the scene object `DAT_802ab3a4` and its render-object list;
  - the TouchField and Bouncer index tables;
  - each TouchField entity (vtable 0x80209348 at +0x48) with its RAM fields.
"""

from __future__ import annotations

import argparse
import glob
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from ssg import Container                                        # noqa: E402

DUMP = ROOT / "build" / "ramdump" / "mem1.raw"
RAM = 0x80000000
SCENE_PTR = 0x802ab3a4        # DAT_802ab3a4: the scene / render-object list
TOUCH_TABLE, TOUCH_SLOTS = 0x802d970c, 128
BOUNCER_TABLE, BOUNCER_SLOTS = 0x802d8708, 32
TOUCHFIELD_VTABLE = 0x80209348
ENTITY_VTABLE_AT = 0x48


class Ram:
    def __init__(self, path=DUMP):
        self.m = Path(path).read_bytes()

    def ok(self, va):
        return va is not None and RAM <= va < RAM + len(self.m) - 3

    def u32(self, va):
        return struct.unpack_from(">I", self.m, va - RAM)[0] if self.ok(va) else None

    def f32(self, va):
        return struct.unpack_from(">f", self.m, va - RAM)[0] if self.ok(va) else None

    def find_level(self):
        """(stem, base, agreeing chunks) of the image resident in RAM, or None."""
        best = None
        for f in sorted(glob.glob(str(ROOT / "orig" / "files" / "Levels" / "*.ssw"))):
            img = Container(Path(f)).image
            bases = Counter()
            for k in range(1, 12):
                o = (len(img) * k // 12) & ~3
                chunk = img[o:o + 64]
                if chunk.count(b"\0") > 40:
                    continue
                r = self.m.find(chunk)
                if r >= 0:
                    bases[RAM + r - o] += 1
            if bases:
                base, votes = bases.most_common(1)[0]
                if best is None or votes > best[2]:
                    best = (Path(f).stem, base, votes)
        return best

    def check_image(self, stem, base):
        c = Container(ROOT / "orig" / "files" / "Levels" / (stem + ".ssw"))
        img, ptrs = c.image, set(c.pointer_offsets())
        good = total = 0
        for o in ptrs:
            d = struct.unpack_from(">I", img, o)[0]
            total += 1
            good += self.u32(base + o) == ((base + d) & 0xFFFFFFFF if d else 0)
        return good, total


def cmd_level(args):
    r = Ram(args.dump)
    found = r.find_level()
    if not found:
        print("no level image found in RAM")
        return 1
    stem, base, votes = found
    good, total = r.check_image(stem, base)
    print("%s at %#x (%d chunks agree); relocated pointers %d of %d" % (stem, base, votes, good, total))
    return 0 if good == total else 1


def cmd_info(args):
    r = Ram(args.dump)
    if cmd_level(args):
        return 1
    scene = r.u32(SCENE_PTR)
    print("scene object %#x, vtable %#x" % (scene, r.u32(scene)))
    kinds, x, seen = Counter(), r.u32(scene + 0x28), set()
    while r.ok(x) and x not in seen:
        seen.add(x)
        kinds[r.u32(x + 0x24)] += 1
        x = r.u32(x + 8)
    print("render objects %d, kinds at +0x24: %s" % (len(seen), dict(kinds)))
    for name, tab, n in (("touch", TOUCH_TABLE, TOUCH_SLOTS), ("bouncer", BOUNCER_TABLE, BOUNCER_SLOTS)):
        live = [(i, r.u32(tab + 4 * i)) for i in range(n) if r.u32(tab + 4 * i)]
        print("%s table: %d live %s" % (name, len(live), [(i, hex(e)) for i, e in live[:12]]))
    ents = [RAM + o - ENTITY_VTABLE_AT for o in range(0, len(r.m) - 3, 4)
            if struct.unpack_from(">I", r.m, o)[0] == TOUCHFIELD_VTABLE]
    print("TouchField entity objects in RAM: %d" % len(ents))
    for e in ents[:12]:
        print("  %#x: %s" % (e, " ".join("%08x" % (r.u32(e + 4 * k) or 0) for k in range(0x40))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("info", cmd_info), ("level", cmd_level)):
        p = sub.add_parser(name)
        p.add_argument("dump", nargs="?", default=DUMP, type=Path)
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
