#!/usr/bin/env python3
"""
Reader for Climax's `.clm` ("ClimaxModel") files — the Maya export format the
developer material in dev/ is full of (docs/dev-material.md). Every enemy,
pickup and FMA model on the disc was built from these; they are not on the
disc themselves.

The file is one tree of chunks, little-endian:

    char  name[]       NUL-terminated, zero-padded to a multiple of 4
    u32   nchildren
    u32   size         bytes of payload that follow
    u8    data[size]
    ...   nchildren chunks

    python tools/clm.py tree FILE [--depth N]
    python tools/clm.py meshes FILE...        # mesh, vertex count, weighted?, parent bone
    python tools/clm.py verify [DIR]          # every .clm parses exactly to EOF
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Chunk:
    __slots__ = ("name", "data", "children", "offset")

    def __init__(self, name, data, children, offset):
        self.name, self.data, self.children, self.offset = name, data, children, offset

    def find(self, name):
        return [c for c in self.children if c.name == name]

    def first(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def text(self):
        """Payload as a NUL-terminated string (Name, Mesh Ref, ...)."""
        return self.data.split(b"\0", 1)[0].decode("latin1")


def _chunk(b, o):
    e = b.index(b"\0", o)
    name = b[o:e].decode("latin1")
    p = o + ((e - o + 4) & ~3)          # padded relative to the chunk: data can be odd-sized
    n, size = struct.unpack_from("<II", b, p)
    data = b[p + 8:p + 8 + size]
    q = p + 8 + size
    kids = []
    for _ in range(n):
        c, q = _chunk(b, q)
        kids.append(c)
    return Chunk(name, data, kids, o), q


def load(path):
    """The root is a bare header, `"ClimaxModel\\0"` and one u32 (0 on every
    file); the top-level chunks follow it to the end of the file."""
    b = Path(path).read_bytes()
    if not b.startswith(b"ClimaxModel\0"):
        raise ValueError("%s: not a ClimaxModel" % path)
    q, kids = 16, []
    while q < len(b):
        c, q = _chunk(b, q)
        kids.append(c)
    return Chunk("ClimaxModel", b[12:16], kids, 0), q, len(b)


def meshes(root):
    """[(mesh name, vertices, weighted, parent bone or None)]. A mesh with no
    Weights List is rigid; the Skeleton says which bone carries it (a Bone
    whose child is a `Mesh Ref` naming the mesh)."""
    parent = {}
    for c in root.walk():
        if c.name == "Bone":
            for r in c.find("Mesh Ref"):
                parent[r.text()] = c.text()
    out = []
    for m in root.find("Mesh"):
        v = m.first("Vertex List")
        name = m.first("Name")
        nm = name.text() if name else "?"
        weighted = v is not None and v.first("Weights List") is not None
        out.append((nm, len(v.data) // 12 if v else 0, weighted, parent.get(nm)))
    return out


def vertices(mesh):
    v = mesh.first("Vertex List")
    return [struct.unpack_from("<3f", v.data, 12 * i) for i in range(len(v.data) // 12)]


def cmd_tree(args):
    root, _, _ = load(args.file)

    def show(c, d):
        if d > args.depth:
            return
        extra = ""
        if c.name in ("Name", "Mesh Ref", "Bone", "Animation Clip") or len(c.data) < 24:
            extra = repr(c.text()) if c.data[:1].isalpha() else c.data[:24].hex()
        print("  " * d + "%s  (%d children, %d bytes) %s" % (c.name, len(c.children), len(c.data), extra))
        kinds = Counter(k.name for k in c.children)
        shown = Counter()
        for k in c.children:
            shown[k.name] += 1
            if shown[k.name] <= 3:
                show(k, d + 1)
            elif shown[k.name] == 4:
                print("  " * (d + 1) + "... %d more %s" % (kinds[k.name] - 3, k.name))
    show(root, 0)
    return 0


def cmd_meshes(args):
    for f in args.files:
        root, _, _ = load(f)
        print(f)
        for nm, n, w, par in meshes(root):
            print("   %-32s %5d verts  %-8s %s" % (nm, n, "weighted" if w else "rigid",
                                                 "on bone " + par if par else ""))
    return 0


def cmd_verify(args):
    base = Path(args.dir) if args.dir else ROOT / "dev"
    tot, problems = Counter(), []
    for f in sorted(base.rglob("*")):
        if f.suffix.lower() != ".clm":
            continue
        tot["files"] += 1
        try:
            root, end, size = load(f)
        except Exception as e:                      # noqa: BLE001
            problems.append("%s: %s" % (f, e))
            continue
        tot["parse to EOF"] += end == size
        if end != size:
            problems.append("%s: tree ends at %d of %d" % (f, end, size))
        for nm, n, w, par in meshes(root):
            tot["meshes"] += 1
            tot["weighted meshes"] += w
            tot["rigid meshes"] += not w
            tot["rigid meshes on a bone"] += (not w) and par is not None
        tot["with a Skeleton"] += any(c.name == "Skeleton" for c in root.walk())
    for k in ("files", "parse to EOF", "meshes", "weighted meshes", "rigid meshes",
              "rigid meshes on a bone", "with a Skeleton"):
        print("  %-24s %s" % (k, format(tot[k], ",")))
    for p in problems[:20]:
        print("  PROBLEM " + p)
    print("%d problems" % len(problems))
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tree"); p.add_argument("file"); p.add_argument("--depth", type=int, default=3)
    p.set_defaults(fn=cmd_tree)
    p = sub.add_parser("meshes"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_meshes)
    p = sub.add_parser("verify"); p.add_argument("dir", nargs="?"); p.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
