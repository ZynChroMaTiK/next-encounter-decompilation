#!/usr/bin/env python3
"""
NE's AI classes, named by themselves: every AI task ("directive") class has a
virtual method that returns its own name, `lis r3, hi; addi r3, r3, lo; blr`
returning a C string such as "cWalkTo" (docs/enemies.md, "Behaviour").

This reads main.dol directly, no Ghidra: it finds every such name-returning
function, every CodeWarrior vtable that points at one (8-byte entries
{s16 this-adjust, 0, function}), and prints each class with its vtable and
methods.

    python tools/aiclasses.py            # every named class
    python tools/aiclasses.py --json build/ai_classes.json
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOL = ROOT / "orig" / "sys" / "main.dol"


def load():
    d = DOL.read_bytes()
    offs = struct.unpack_from(">18I", d, 0)
    addr = struct.unpack_from(">18I", d, 0x48)
    size = struct.unpack_from(">18I", d, 0x90)
    sections = [(o, a, s, i < 7) for i, (o, a, s) in enumerate(zip(offs, addr, size)) if s]
    return d, sections


def reader(d, sections):
    def at(va):
        for o, a, s, _text in sections:
            if a <= va < a + s:
                return o + va - a
        return None

    def u32(va):
        o = at(va)
        return None if o is None or o + 4 > len(d) else struct.unpack_from(">I", d, o)[0]

    def cstr(va, limit=64):
        o = at(va)
        if o is None:
            return None
        raw = d[o:o + limit].split(b"\0")[0]
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError:
            return None
    return at, u32, cstr


def name_functions(d, sections, cstr):
    """{function address: class name} for `lis r3,hi; addi r3,r3,lo; blr`."""
    out = {}
    for o, a, s, text in sections:
        if not text:
            continue
        for k in range(0, s - 12, 4):
            w0, w1, w2 = struct.unpack_from(">III", d, o + k)
            if w0 >> 16 == 0x3C60 and w1 >> 16 == 0x3863 and w2 == 0x4E800020:
                hi, lo = w0 & 0xFFFF, w1 & 0xFFFF
                va = (hi << 16) + (lo - 0x10000 if lo & 0x8000 else lo)
                s_ = cstr(va)
                if s_ and re.fullmatch(r"c[A-Z][A-Za-z0-9_]{2,60}", s_):
                    out[a + k] = s_
    return out


def vtables(d, sections, u32, names):
    """Vtables whose entries include a name function: {vtable: (class, entries)}."""
    found = {}
    for o, a, s, text in sections:
        if text:
            continue
        for k in range(0, s - 8, 4):
            fn = struct.unpack_from(">I", d, o + k + 4)[0]
            if fn in names and struct.unpack_from(">I", d, o + k)[0] & 0xFFFF == 0:
                # walk back to the vtable start: entries are 8 bytes, the first
                # header entry is 0 / 0
                start = a + k
                while True:
                    prev = u32(start - 4)
                    if prev is None or not (0x80003100 <= prev < 0x80300000):
                        break
                    start -= 8
                entries = []
                p = start
                while True:
                    fp = u32(p + 4)
                    if fp is None or not (0x80003100 <= fp < 0x80300000):
                        break
                    entries.append(fp)
                    p += 8
                found[start - 8] = (names[fn], entries)
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    d, sections = load()
    at, u32, cstr = reader(d, sections)
    names = name_functions(d, sections, cstr)
    vts = vtables(d, sections, u32, names)
    by_class = {}
    for vt, (cls, entries) in sorted(vts.items()):
        by_class.setdefault(cls, []).append({"vtable": "%08x" % vt, "methods": ["%08x" % e for e in entries]})
    for cls in sorted(by_class, key=str.lower):
        for v in by_class[cls]:
            print("%-32s vtable %s  %d methods: %s" % (cls, v["vtable"], len(v["methods"]), " ".join(v["methods"][:6])))
    unused = sorted(set(names.values()) - set(by_class))
    print("%d name functions, %d classes with a vtable; %d names with no vtable found: %s"
          % (len(names), len(by_class), len(unused), " ".join(unused[:20])))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(by_class, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
