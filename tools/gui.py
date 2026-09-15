#!/usr/bin/env python3
"""
Game.Gui -- the front-end and in-game menu layout (docs/gui.md).

The file is a DXFF relocatable blob, loaded whole by CcSystem_Init
(0x80161cc8) -> FUN_800f52e0, which fixes it up with FUN_800f53c0 and logs
"(GUI) %i Components":

    char magic[4]  "DXFF"
    u32  0
    u32  reloc_offset       relocation table, relative to the data base
    u32  reloc_count
    ---- data base (file offset 0x10) ----
    u32  count              components
    u32  component[count]   relocated pointers (offsets from the base)
    ...  components, their sub-records, then the strings
    u32  reloc[reloc_count] data offsets of every pointer field; each field
                            holds an offset from the base until fixed up

Every component starts with the same header:

    u16 kind       0..7; each kind has one record size (0 = 76, 1 = 88, ...)
    u16 index      its own position in the table
    u16 screen     the kind-0 component (a screen) it belongs to; on a screen
                   itself 0xFFFF, or the outer screen when it is nested
    u16 _06
    u16 _08        small values (0, 1, 2, 4 ...); meaning unread
    u16 _0a
    u32 flags      +0x0c
    u16 nav[2]     +0x10, component indices (e.g. 0x5d, 0x5e)
    ...
    u32 4          +0x28
    f32 x, y       +0x2c, +0x30  position on a 640x480 screen centred on 0
    f32 w, h       +0x34, +0x38  640, 480 on most components
    u32 rgba       +0x3c
    ptr  +0x40     kinds 2 and 3: the config key the widget edits
    ptr  +0x4c     kind 6: the sprite it draws, "Atlas:Sprite"
    ... more pointers to sub-records at +0x44, +0x48, +0x5c

The file holds no .tdb text: no word in it resolves as a text hash. The labels
come from the menu code; the file's strings are the config keys the widgets
bind to (GameSetup.RulesCoop.Difficulty, SystemSetup.SFXVolume ...) and sprite
names (GuiA:ArrowL, Netricsa:title_etricsa ...).

    python tools/gui.py list
    python tools/gui.py dump            # -> build/gui/game_gui.json
    python tools/gui.py verify
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "orig" / "files" / "Game.Gui"
OUT = ROOT / "build" / "gui" / "game_gui.json"
BASE = 0x10


class Gui:
    def __init__(self, path=SRC):
        b = self.b = Path(path).read_bytes()
        magic, _z, self.reloc_offset, self.reloc_count = struct.unpack_from(">4sIII", b, 0)
        if magic != b"DXFF":
            raise ValueError("%s: not a DXFF blob" % path)
        self.relocs = [self.u32(self.reloc_offset + 4 * i) for i in range(self.reloc_count)]
        self.pointer_fields = set(self.relocs)
        self.count = self.u32(0)
        self.offsets = [self.u32(4 + 4 * i) for i in range(self.count)]

    # all offsets below are relative to the data base
    def u32(self, o):
        return struct.unpack_from(">I", self.b, BASE + o)[0]

    def f32(self, o):
        return struct.unpack_from(">f", self.b, BASE + o)[0]

    def u16(self, o):
        return struct.unpack_from(">H", self.b, BASE + o)[0]

    def cstr(self, o):
        """The C string at o, if o holds one that is printable."""
        e = self.b.find(b"\0", BASE + o)
        if e < 0:
            return None
        s = self.b[BASE + o:e]
        return s.decode("latin1") if len(s) >= 2 and all(32 <= c < 127 for c in s) else None

    def component(self, i):
        p = self.offsets[i]
        end = self.offsets[i + 1] if i + 1 < self.count else self.reloc_offset
        ptrs = {}
        for k in range(0, min(end - p, 0x80), 4):
            if p + k in self.pointer_fields:
                t = self.u32(p + k)
                ptrs["+0x%02x" % k] = {"offset": t, "string": self.cstr(t)}
        screen = self.u16(p + 4)
        return {
            "index": self.u16(p + 2), "kind": self.u16(p), "offset": p, "size": end - p,
            "screen": None if screen == 0xFFFF else screen,
            "field_08": self.u16(p + 8), "flags": self.u32(p + 0x0C),
            "nav": [self.u16(p + 0x10), self.u16(p + 0x12)],
            "pos": [self.f32(p + 0x2C), self.f32(p + 0x30)],
            "size_2d": [self.f32(p + 0x34), self.f32(p + 0x38)],
            "rgba": "%08x" % self.u32(p + 0x3C),
            "pointers": ptrs,
        }

    def components(self):
        return [self.component(i) for i in range(self.count)]

    def strings(self):
        """Every string a relocated pointer points at, in file order."""
        out = {}
        for r in self.relocs:
            t = self.u32(r)
            s = self.cstr(t)
            if s:
                out[t] = s
        return [out[k] for k in sorted(out)]


def cmd_list(args):
    g = Gui()
    for c in g.components():
        strs = [v["string"] for v in c["pointers"].values() if v["string"]]
        print("#%-3d kind %d screen %-4s (%7.1f,%7.1f) %s %s"
              % (c["index"], c["kind"], "-" if c["screen"] is None else c["screen"],
                 c["pos"][0], c["pos"][1], c["rgba"], strs[:2]))
    return 0


def cmd_dump(args):
    g = Gui()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": "orig/files/Game.Gui", "count": g.count,
                               "components": g.components(), "strings": g.strings()},
                              indent=1))
    print("%d components -> %s" % (g.count, OUT.relative_to(ROOT)))
    return 0


def cmd_verify(args):
    g = Gui()
    comps = g.components()
    data_len = g.reloc_offset
    tot, problems = Counter(), []
    tot["relocations"] = g.reloc_count
    tot["relocation fields inside the data"] = sum(r < data_len for r in g.relocs)
    tot["relocated values inside the data"] = sum(r < data_len and g.u32(r) < data_len
                                                  for r in g.relocs)
    tot["table ends at end of file"] = int(BASE + g.reloc_offset + 4 * g.reloc_count == len(g.b))
    tot["components"] = g.count
    tot["component table fields relocated"] = sum((4 + 4 * i) in g.pointer_fields
                                                  for i in range(g.count))
    tot["index == own position"] = sum(c["index"] == i for i, c in enumerate(comps))
    latest, nested = None, []
    for i, c in enumerate(comps):
        if c["kind"] == 0:
            if c["screen"] is not None:
                nested.append(i)
            latest = i
            tot["screen rule holds"] += 1
        elif c["screen"] == latest:
            tot["screen rule holds"] += 1
        elif c["screen"] is not None and comps[c["screen"]]["kind"] == 0:
            tot["member of an outer screen"] += 1
    tot["screens (kind 0)"] = sum(c["kind"] == 0 for c in comps)
    tot["nested screens"] = len(nested)
    sys.path.insert(0, str(ROOT / "tools"))
    from text import resolves
    tot["words that resolve as .tdb hashes"] = sum(
        resolves(g.u32(o)) for o in range(0, data_len - 3, 4))
    sizes = {}
    for c in comps[:-1]:                 # the last one runs on into the strings
        sizes.setdefault(c["kind"], set()).add(c["size"])
    tot["kinds"] = len(sizes)
    tot["kinds with one record size"] = sum(len(v) == 1 for v in sizes.values())
    for k in ("relocations", "relocation fields inside the data",
              "relocated values inside the data", "table ends at end of file", "components",
              "component table fields relocated", "index == own position", "screens (kind 0)",
              "nested screens", "screen rule holds", "member of an outer screen",
              "words that resolve as .tdb hashes", "kinds", "kinds with one record size"):
        print("  %-36s %s" % (k, tot[k]))
    print("  record size by kind                  %s"
          % {k: sorted(v) for k, v in sorted(sizes.items())})
    if tot["relocated values inside the data"] != g.reloc_count:
        problems.append("relocations point outside the data")
    if tot["index == own position"] != g.count:
        problems.append("component indices out of order")
    if tot["screen rule holds"] + tot["member of an outer screen"] != g.count:
        problems.append("%d components belong to no screen"
                        % (g.count - tot["screen rule holds"] - tot["member of an outer screen"]))
    for p in problems:
        print("  PROBLEM " + p)
    print("%d problems" % len(problems))
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("list", cmd_list), ("dump", cmd_dump), ("verify", cmd_verify)):
        sub.add_parser(name).set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
