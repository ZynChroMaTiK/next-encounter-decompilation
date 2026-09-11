#!/usr/bin/env python3
"""
Reader for Climax's "CLIMAX" container, used by Next Encounter's .ssg (models,
GUI, skins) and .ssw (levels) files.

Format fully recovered from the loader at 0x801206b8 in main.dol
("IMPORT Read Old"), cross-checked against all 63 containers on the disc.

    0x00  8   magic       "CLIMAX-\\0" (new style) or "CLIMAX.\\0" (old style)
    0x08  u32 reloc_count entries in the relocation table
    0x0c  u32 alloc_size  bytes to allocate; the inflated image size
    0x10  u32 main_csize  compressed size of the image stream (0 => stored raw)
    0x14  u32 reloc_csize compressed size of the reloc stream (0 => stored raw)
    0x18  u32 stage_off   offset used to stage the reloc table in the buffer
    0x1c  u32 stage_lim   size the loader compares against to pick that strategy
    0x20  32  zero padding
    0x40      stream 1: the image,           main_csize  bytes -> alloc_size
              stream 2: the relocation table, reloc_csize bytes -> reloc_count*4

Both streams are plain zlib; the game links zlib 1.1.4 (the version string is
passed to inflateInit_ at 0x80160b8c) and inflates in 128 KB chunks.

New-style headers are 0x40 bytes and the image starts at the buffer base.
Old-style headers are 0x10 bytes, and the loader copies those 16 bytes to the
front of the buffer, so the relocation base sits 0x10 *before* the image. Every
container shipped on the disc is new style.

Relocations are RLE-coded. Walking the table (loop at 0x801208f8):

    v = table[i]
    if v & 0x80000000:                 # a run of evenly spaced pointers
        count  = (v >> 10) & 0x1fffff
        stride = v & 0x3ff
        off    = table[i+1]            # run start, consumes the next word
        for _ in range(count):
            *(u32*)(base + off) += base;  off += stride
    else:                              # a single pointer at offset v
        *(u32*)(base + v) += base

So the table names *every pointer field in the image*. That is the useful part:
the payload is a serialised memory image with no type tags, and the relocation
map is what makes its object graph walkable.

Usage:
    python tools/ssg.py info    orig/files/Data.ssg
    python tools/ssg.py unpack  orig/files/Data.ssg -d build/
    python tools/ssg.py strings orig/files/Levels/Rlevel1_1.ssw
    python tools/ssg.py relocs  orig/files/Data.ssg --limit 20
    python tools/ssg.py verify  orig/files
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import zlib
from pathlib import Path

MAGIC_NEW = b"CLIMAX-\x00"
MAGIC_OLD = b"CLIMAX.\x00"
HEADER_NEW = 0x40
HEADER_OLD = 0x10
FIELDS = ("reloc_count", "alloc_size", "main_csize",
          "reloc_csize", "stage_off", "stage_lim")


class Container:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        magic = self.raw[:8]
        if magic == MAGIC_NEW:
            self.old_style = False
            self.header_size = HEADER_NEW
        elif magic == MAGIC_OLD:
            self.old_style = True
            self.header_size = HEADER_OLD
        else:
            raise ValueError(f"{self.path}: not a CLIMAX container (magic {magic!r})")

        values = struct.unpack_from(">6I", self.raw, 8)
        self.header = dict(zip(FIELDS, values))
        self._image: bytes | None = None
        self._relocs: tuple[int, ...] | None = None

    # --- streams ---------------------------------------------------------

    def _stream(self, start: int, csize: int, raw_size: int) -> bytes:
        """A stream is zlib unless its compressed size field is 0."""
        if csize == 0:
            return self.raw[start:start + raw_size]
        return zlib.decompress(self.raw[start:start + csize])

    @property
    def image(self) -> bytes:
        """The inflated memory image, relocations NOT applied."""
        if self._image is None:
            h = self.header
            self._image = self._stream(self.header_size, h["main_csize"],
                                       h["alloc_size"])
            if len(self._image) != h["alloc_size"]:
                print(f"warning: {self.path.name} image inflated to "
                      f"{len(self._image):,}, header says {h['alloc_size']:,}",
                      file=sys.stderr)
        return self._image

    @property
    def reloc_words(self) -> tuple[int, ...]:
        """Raw relocation table words, before RLE expansion."""
        if self._relocs is None:
            h = self.header
            n = h["reloc_count"]
            start = self.header_size + h["main_csize"]
            blob = self._stream(start, h["reloc_csize"], n * 4)
            self._relocs = struct.unpack(f">{n}I", blob[:n * 4])
        return self._relocs

    def pointer_offsets(self):
        """Every offset in the image holding a relocatable pointer."""
        words = self.reloc_words
        n = len(words)
        i = 0
        while i < n:
            v = words[i]
            if v & 0x80000000:
                count = (v >> 10) & 0x1FFFFF
                stride = v & 0x3FF
                i += 1
                if i >= n:
                    break
                off = words[i]
                for _ in range(count):
                    yield off
                    off += stride
            else:
                yield v
            i += 1

    def relocated(self, base: int = 0) -> bytearray:
        """The image with every pointer field rebased by `base`.

        base=0 leaves pointers as image-relative offsets, which is what you
        want for parsing. The console used the real load address.
        """
        buf = bytearray(self.image)
        if base == 0:
            return buf
        for off in self.pointer_offsets():
            if off + 4 > len(buf):
                continue
            v = struct.unpack_from(">I", buf, off)[0]
            struct.pack_into(">I", buf, off, (v + base) & 0xFFFFFFFF)
        return buf

    def strings(self, min_len: int = 5):
        for m in re.finditer(rb"[ -~]{%d,}" % min_len, self.image):
            yield m.start(), m.group().decode("ascii")


# --- subcommands ------------------------------------------------------------

def cmd_info(args) -> int:
    for path in args.files:
        c = Container(path)
        size = len(c.raw)
        h = c.header
        print(f"{path}")
        print(f"  style              {'old (0x10)' if c.old_style else 'new (0x40)'}")
        print(f"  file size          {size:,}")
        for k in FIELDS:
            print(f"  {k:<18} {h[k]:,} (0x{h[k]:x})")
        streams = h["main_csize"] + h["reloc_csize"]
        payload = size - c.header_size
        print(f"  streams sum        {streams:,}  vs payload {payload:,}"
              f"  {'OK' if streams == payload else 'MISMATCH'}")
        try:
            img = c.image
            ptrs = sum(1 for _ in c.pointer_offsets())
            print(f"  image              {len(img):,} bytes  ({size / len(img):.1%} of original)")
            print(f"  pointer fields     {ptrs:,}")
        except zlib.error as e:
            print(f"  inflate FAILED: {e}")
    return 0


def cmd_unpack(args) -> int:
    for path in args.files:
        c = Container(path)
        dest = (args.output if args.output and len(args.files) == 1
                else (args.output_dir or path.parent) / (path.stem + ".bin"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(c.relocated(args.base))
        print(f"{path} -> {dest}  ({len(c.image):,} bytes, base=0x{args.base:x})")
        if args.relocs_out:
            ro = Path(args.relocs_out) / (path.stem + ".relocs.txt")
            ro.parent.mkdir(parents=True, exist_ok=True)
            with ro.open("w") as fh:
                for off in c.pointer_offsets():
                    fh.write(f"{off:#x}\n")
            print(f"    relocations -> {ro}")
    return 0


def cmd_strings(args) -> int:
    for path in args.files:
        c = Container(path)
        print(f"=== {path} ===")
        for off, s in c.strings(args.min_len):
            print(f"  0x{off:08x}  {s}")
    return 0


def cmd_relocs(args) -> int:
    for path in args.files:
        c = Container(path)
        words = c.reloc_words
        runs = sum(1 for v in words if v & 0x80000000)
        offs = list(c.pointer_offsets())
        print(f"=== {path} ===")
        print(f"  table words {len(words):,}  runs {runs:,}  "
              f"singles {len(words) - runs * 2:,}  -> {len(offs):,} pointers")
        img = c.image
        for off in offs[:args.limit]:
            val = struct.unpack_from(">I", img, off)[0] if off + 4 <= len(img) else None
            print(f"    +0x{off:08x} -> {'0x%08x' % val if val is not None else '<oob>'}")
    return 0


def cmd_verify(args) -> int:
    roots = args.paths or [Path("orig/files")]
    files: list[Path] = []
    for r in roots:
        r = Path(r)
        files.extend(sorted(r.rglob("*.ssg")) if r.is_dir() else [r])
        if r.is_dir():
            files.extend(sorted(r.rglob("*.ssw")))
    bad = 0
    total_ptr = 0
    for f in sorted(set(files)):
        try:
            c = Container(f)
            img = c.image
            offs = list(c.pointer_offsets())
            oob = [o for o in offs if o + 4 > len(img)]
            total_ptr += len(offs)
            streams = c.header["main_csize"] + c.header["reloc_csize"]
            ok = (len(img) == c.header["alloc_size"]
                  and streams == len(c.raw) - c.header_size
                  and not oob)
            if not ok:
                bad += 1
                print(f"  FAIL {f}: image={len(img)} expect={c.header['alloc_size']} "
                      f"streams={streams} payload={len(c.raw) - c.header_size} oob={len(oob)}")
        except Exception as e:
            bad += 1
            print(f"  FAIL {f}: {e}")
    print(f"{len(set(files))} containers checked, {total_ptr:,} pointer fields, {bad} failures")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="header fields and derived sizes")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("unpack", help="write the inflated image to a .bin")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("-d", "--output-dir", type=Path)
    p.add_argument("--base", type=lambda s: int(s, 0), default=0,
                   help="rebase pointer fields by this value (default 0)")
    p.add_argument("--relocs-out", type=Path, help="also dump the pointer offset list")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("strings", help="ASCII strings inside the image")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--min-len", type=int, default=5)
    p.set_defaults(func=cmd_strings)

    p = sub.add_parser("relocs", help="decode the relocation table")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--limit", type=int, default=32)
    p.set_defaults(func=cmd_relocs)

    p = sub.add_parser("verify", help="check every container round-trips")
    p.add_argument("paths", nargs="*", type=Path)
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    try:
        return args.func(args)
    except (ValueError, zlib.error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
